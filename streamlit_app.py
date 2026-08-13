#!/usr/bin/env python3
"""
Washington Nationals — GPS Workload Dashboard (Streamlit)
==========================================================


"""

from __future__ import annotations

import io
import json
import hmac
import math
import os
import re
import threading
import time
import unicodedata
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import streamlit as st
import plotly.graph_objects as go

# -----------------------------------------------------------------------------
# Embedded report engine
# -----------------------------------------------------------------------------
# The report modules are embedded into this file so Streamlit Cloud can run the
# dashboard when only streamlit_app.py is uploaded to GitHub. They are installed
# into sys.modules under their original names, so the supplied report code keeps
# its original imports and behavior.
import sys as _sys
import types as _types

def _install_embedded_module(_name: str, _source: str):
    _module = _types.ModuleType(_name)
    _module.__file__ = f"<embedded {_name}.py>"
    _sys.modules[_name] = _module
    exec(compile(_source, _module.__file__, "exec"), _module.__dict__)
    return _module

_install_embedded_module('name_utils', '"""Name normalization shared by the standalone-style GPS report modules."""\nfrom __future__ import annotations\nimport re\nimport unicodedata\nimport pandas as pd\n\n\ndef normalize_name(value) -> str:\n    if value is None or (isinstance(value, float) and pd.isna(value)):\n        return ""\n    text = str(value).strip()\n    if not text:\n        return ""\n    if "," in text:\n        parts = [p.strip() for p in text.split(",", 1)]\n        if len(parts) == 2 and all(parts):\n            text = f"{parts[1]} {parts[0]}"\n    text = unicodedata.normalize("NFKD", text)\n    text = "".join(ch for ch in text if not unicodedata.combining(ch))\n    text = text.casefold()\n    text = re.sub(r"[^a-z0-9]+", " ", text)\n    return " ".join(text.split())\n')
_install_embedded_module('gps_report_data', '"""Minimal data helpers used by the original GPS workload report engine.\n\nThis keeps the reporting/flagging files structurally identical to the supplied\nstandalone report without importing their local-machine Google Sheets loader.\nThe Streamlit dashboard supplies already-loaded DataFrames to the report engine.\n"""\nfrom __future__ import annotations\n\nimport pandas as pd\n\nMETRICS = [\n    ("top_speed_ms",      "Top Speed",   "m/s", False, False),\n    ("n_sprints",         "Sprints",     "",    True,  "rolling_pct"),\n    ("sprint_distance_m", "Sprint Dist", "m",   True,  "zscore"),\n    ("n_accelerations",   "Accels",      "#",   True,  "rolling_pct"),\n    ("hsr_distance_m",    "HSR",         "m",   True,  "zscore"),\n    ("total_distance_m",  "Total Dist",  "m",   True,  "zscore"),\n    ("duration_min",      "Duration",    "min", False, False),\n]\nMETRIC_COLS = [m[0] for m in METRICS]\n\n_DRILL_AGG = {\n    "top_speed_ms": "max",\n    "n_sprints": "sum",\n    "sprint_distance_m": "sum",\n    "n_accelerations": "sum",\n    "hsr_distance_m": "sum",\n    "total_distance_m": "sum",\n    "duration_min": "sum",\n}\n\nYARDS_TO_M = 0.9144\nMETERS_TO_YARDS = 1 / YARDS_TO_M\nPITCHER_POSITIONS = {"P", "SP", "RP", "RHP", "LHP", "PITCHER"}\n\n\ndef parse_sheet_dates(series):\n    """Handles mixed spreadsheet dates, Excel serials, and tz-aware timestamps."""\n    parsed = pd.to_datetime(series, errors="coerce", format="mixed")\n    if hasattr(parsed, "dt") and parsed.dt.tz is not None:\n        parsed = parsed.dt.tz_localize(None)\n    nat = parsed.isna()\n    if nat.any():\n        def _serial(v):\n            try:\n                n = float(v)\n                if 30_000 < n < 60_000:\n                    return pd.Timestamp("1899-12-30") + pd.Timedelta(days=n)\n            except (TypeError, ValueError):\n                pass\n            return pd.NaT\n        parsed.loc[nat] = series.loc[nat].apply(_serial)\n    return parsed\n\n\ndef aggregate_drills(df):\n    """Supplied report aggregation: remove duplicate Entire Session rows, then\n    collapse drills to one athlete/date session row."""\n    if df is None or df.empty:\n        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()\n\n    df = df.copy()\n    if "Date" in df.columns:\n        df["Date"] = parse_sheet_dates(df["Date"])\n\n    if "drill_name" in df.columns:\n        drill = df["drill_name"].fillna("").astype(str)\n        es_mask = drill.str.contains("Entire Session", case=False, na=False)\n        has_other = df.assign(_drill=drill).groupby(["Athlete", "Date"])["_drill"].transform(\n            lambda x: (~x.str.contains("Entire Session", case=False, na=False)).any()\n        )\n        df = df[~(es_mask & has_other)].copy()\n\n    if df.empty:\n        return df\n\n    group_cols = ["Athlete", "Date"]\n    for col in ["Team", "Position"]:\n        if col in df.columns and not df[col].fillna("").astype(str).eq("").all():\n            group_cols.append(col)\n\n    agg = {col: rule for col, rule in _DRILL_AGG.items() if col in df.columns}\n    for ctx in ["session_name", "week", "week_start"]:\n        if ctx in df.columns:\n            agg[ctx] = "first"\n    for col in ["Team", "Position"]:\n        if col in df.columns and col not in group_cols:\n            agg[col] = "first"\n\n    if not agg:\n        return df\n    return df.groupby(group_cols, sort=False).agg(agg).reset_index()\n')
_install_embedded_module('gps_flags', '"""\ngps_flags.py — GPS Workload flag engine for the standalone report.\n\nPorted from gps_workload_report.py\'s compute_athlete_windows(),\ncompute_game_classifications(), classify_combined_load(), and\nclassify_workload_status() -- same math, adapted to import from gps_data.py\ninstead of Streamlit/milb_app.py. See the GPS Workload Report reference doc\nfor the full rationale behind each rule.\n"""\n\nimport logging\nimport numpy as np\nimport pandas as pd\n\nfrom gps_report_data import (\n    METRICS, aggregate_drills, parse_sheet_dates,\n)\nfrom name_utils import normalize_name\n\nlogger = logging.getLogger("gps_trends.flags")\n\n# ── ACWR zones ─────────────────────────────────────────────────────────────\nACWR_OPTIMAL_LOW = 0.8\nACWR_ELEVATED    = 1.3\nACWR_HIGH_RISK   = 1.5\n\n# ── Same-day position-group z-score (Sprint Dist, HSR, Total Dist) ─────────\nREVIEW_Z        = 2.0\nMONITOR_Z       = 1.5\nMIN_GROUP_SIZE  = 4\n\n# ── 14-day rolling position-group z-score (Sprints, Accelerations) ─────────\nROLLING_WINDOW_DAYS       = 14\nROLLING_MIN_SESSIONS      = 3\nROLLING_REVIEW_Z          = 2.0\nROLLING_MONITOR_Z         = 1.5\nROLLING_MIN_DELTA_SPRINTS = 3\nROLLING_MIN_DELTA_ACCELS  = 5\n_ROLLING_MIN_DELTA = {"n_sprints": ROLLING_MIN_DELTA_SPRINTS, "n_accelerations": ROLLING_MIN_DELTA_ACCELS}\n\n# ── Sprint / HSR exposure ───────────────────────────────────────────────────\nMEANINGFUL_SPRINT_THRESHOLD = 1\nMEANINGFUL_SPRINT_DIST_M    = 10\nMEANINGFUL_HSR_THRESHOLD_M  = 20\nMAX_DAYS_WITHOUT_SPRINT     = 3\nMAX_DAYS_WITHOUT_HSR        = 3\nLOW_7DAY_SPRINT_DIST_M      = 30\nLOW_7DAY_HSR_M              = 50\n\nYARDS_TO_M = 0.9144\nGAME_MIN_PRIOR = 3\n\n\ndef compute_game_classifications(df):\n    """\n    Classify each game appearance vs. that player\'s OWN expanding baseline\n    (prior games only -- shift(1) excludes the current game from its own norm).\n    Requires >=3 prior appearances to activate.\n      High     - ME Runs >= (mean+1SD)  OR  ME Dist >= (mean+1SD)\n      Low      - ME Runs <= (mean-1SD)  AND ME Dist <= (mean-1SD)\n      Moderate - everything else\n      "-"      - fewer than 3 prior appearances\n    """\n    if df.empty:\n        return df\n\n    RUN_COL, DIST_COL = "max_effort_runs", "max_effort_distance_covered_yards"\n    df = df.copy().sort_values(["batter", "game_date"]).reset_index(drop=True)\n    groups = []\n\n    for _, grp in df.groupby("batter", sort=False):\n        grp = grp.copy().reset_index(drop=True)\n        for col in [RUN_COL, DIST_COL]:\n            shifted = grp[col].shift(1)\n            grp[f"_mean_{col}"] = shifted.expanding().mean()\n            grp[f"_sd_{col}"] = shifted.expanding().std().replace(0, np.nan)\n        grp["_prior"] = range(len(grp))\n\n        def _classify(row):\n            if row["_prior"] < GAME_MIN_PRIOR:\n                return "\\u2014"\n            runs, dist = row.get(RUN_COL), row.get(DIST_COL)\n            m_run, s_run = row[f"_mean_{RUN_COL}"], row[f"_sd_{RUN_COL}"]\n            m_dst, s_dst = row[f"_mean_{DIST_COL}"], row[f"_sd_{DIST_COL}"]\n            if pd.isna(runs) and pd.isna(dist):\n                return "\\u2014"\n            high_run = (not pd.isna(runs) and not pd.isna(s_run) and runs >= m_run + s_run)\n            high_dist = (not pd.isna(dist) and not pd.isna(s_dst) and dist >= m_dst + s_dst)\n            low_run = (pd.isna(runs) or pd.isna(s_run) or runs <= m_run - s_run)\n            low_dist = (pd.isna(dist) or pd.isna(s_dst) or dist <= m_dst - s_dst)\n            if high_run or high_dist:\n                return "High"\n            if low_run and low_dist:\n                return "Low"\n            return "Moderate"\n\n        grp["load_class"] = grp.apply(_classify, axis=1)\n        grp = grp.drop(columns=[c for c in grp.columns if c.startswith("_")])\n        groups.append(grp)\n\n    return pd.concat(groups, ignore_index=True)\n\n\ndef classify_combined_load(practice_level, game_load_class):\n    """practice_level: High|Moderate|Low. game_load_class: High|Moderate|Low|-."""\n    gl = game_load_class if game_load_class in ("High", "Moderate", "Low") else "Low"\n    if practice_level == "High" and gl == "High":\n        return "Major Load Concern"\n    if practice_level == "High":\n        return "Practice-Driven Spike"\n    if practice_level == "Moderate" and gl == "High":\n        return "Game-Driven Load"\n    if practice_level == "Moderate":\n        return "Normal / Monitor"\n    if practice_level == "Low" and gl == "High":\n        return "Game-Driven Load"\n    if practice_level == "Low" and gl == "Moderate":\n        return "Normal / Monitor"\n    return "Possible Underload"   # Low + Low\n\n\ndef compute_athlete_windows(df, report_date, df_game=None, roster_df=None):\n    """\n    For every athlete present on report_date, compute per-metric value,\n    personal 7d/28d context averages, and same-day/rolling position-group\n    z-score flags. Sprint/HSR exposure incorporates BOTH GPS practice data\n    AND game max-effort runs (>=95th percentile sprint speed counts as a\n    sprint). Returns one row per athlete, sorted Team -> flag_count desc -> name.\n    """\n    report_ts = pd.Timestamp(report_date).normalize()\n    cut_7 = report_ts - pd.Timedelta(days=7)\n    cut_28 = report_ts - pd.Timedelta(days=28)\n\n    df = aggregate_drills(df)\n    on_day = df[df["Date"].dt.normalize() == report_ts].copy()\n\n    # Roster athletes with NO GPS session today still get a row -- flows\n    # through the same exposure-tracking logic below with NaN metric\n    # values, landing on Data Check via classify_workload_status\'s\n    # has_gps=False path -- instead of silently not appearing at all.\n    # roster_df is expected to already be filtered to this team by the\n    # caller (gps_report_html.generate_report_html).\n    if roster_df is not None and not roster_df.empty and "Athlete" in roster_df.columns:\n        present = set(on_day["Athlete"]) if not on_day.empty else set()\n        missing = roster_df[~roster_df["Athlete"].isin(present)]\n        if not missing.empty:\n            metric_cols = [m[0] for m in METRICS]\n            synth_rows = []\n            for _, rrow in missing.drop_duplicates(subset=["Athlete"]).iterrows():\n                synth = {c: np.nan for c in metric_cols}\n                synth["Athlete"] = rrow["Athlete"]\n                synth["Team"] = str(rrow.get("Team", "")).strip()\n                synth["Position"] = str(rrow.get("Position", "")).strip()\n                synth["Date"] = report_ts\n                synth["_synthetic"] = True\n                synth_rows.append(synth)\n            on_day = pd.concat([on_day, pd.DataFrame(synth_rows)], ignore_index=True)\n\n    if on_day.empty:\n        return pd.DataFrame()\n\n    records = []\n    for _, row in on_day.iterrows():\n        athlete = row["Athlete"]\n        team = str(row.get("Team", "")).strip()\n        pos = str(row.get("Position", "")).strip()\n\n        prior = df[(df["Athlete"] == athlete) & (df["Date"].dt.normalize() < report_ts)]\n        prior_7 = prior[prior["Date"].dt.normalize() >= cut_7]\n        prior_28 = prior[prior["Date"].dt.normalize() >= cut_28]\n\n        rec = {"Athlete": athlete, "Team": team, "Position": pos}\n        for col, short, unit, *_ in METRICS:\n            val = pd.to_numeric(row.get(col), errors="coerce")\n            avg7 = prior_7[col].mean() if (col in prior_7.columns and len(prior_7) >= 1) else np.nan\n            avg28 = prior_28[col].mean() if (col in prior_28.columns and len(prior_28) >= 1) else np.nan\n            rec[f"{col}_val"] = round(float(val), 1) if pd.notna(val) else np.nan\n            rec[f"{col}_7d"] = round(float(avg7), 1) if pd.notna(avg7) else np.nan\n            rec[f"{col}_28d"] = round(float(avg28), 1) if pd.notna(avg28) else np.nan\n            rec[f"{col}_flag"] = None\n            rec[f"{col}_z"] = np.nan\n\n        rec["flag_count"] = 0\n        # Strict identity check against True, NOT bool(). Once ANY synthetic\n        # row gets concatenated onto on_day, pandas aligns columns -- every\n        # REAL row then has an ACTUAL NaN in "_synthetic" (not a missing\n        # key), and bool(NaN) is True in Python. row.get(...) == True would\n        # also be wrong (NaN == True is False, but so is NaN == False --\n        # equality with NaN is always False). "is True" is the only check\n        # that correctly treats NaN, None, and missing all as "not synthetic".\n        rec["has_gps"] = row.get("_synthetic") is not True\n\n        sc, sdc, hc = "n_sprints", "sprint_distance_m", "hsr_distance_m"\n        today_sprints = pd.to_numeric(row.get(sc, np.nan), errors="coerce")\n        today_sdist = pd.to_numeric(row.get(sdc, np.nan), errors="coerce")\n        today_hsr = pd.to_numeric(row.get(hc, np.nan), errors="coerce")\n\n        today_has_sprint_gps = (\n            (pd.notna(today_sprints) and today_sprints >= MEANINGFUL_SPRINT_THRESHOLD) or\n            (pd.notna(today_sdist) and today_sdist >= MEANINGFUL_SPRINT_DIST_M)\n        )\n        today_has_hsr_gps = (pd.notna(today_hsr) and today_hsr >= MEANINGFUL_HSR_THRESHOLD_M)\n\n        game_days_sprint, game_dist_by_date, game_hsr_by_date, game_runs_by_date = set(), {}, {}, {}\n        if df_game is not None and not df_game.empty:\n            gc_ = df_game.copy()\n            gc_.columns = gc_.columns.str.strip().str.lower()\n            if "batter" in gc_.columns and "game_date" in gc_.columns:\n                gc_["_nname"] = gc_["batter"].apply(lambda x: normalize_name(str(x)))\n                gc_["_gdate"] = parse_sheet_dates(gc_["game_date"]).dt.normalize()\n                gc_["_runs"] = pd.to_numeric(gc_.get("max_effort_runs", pd.Series(dtype=float)), errors="coerce").fillna(0)\n                gc_["_dist_m"] = pd.to_numeric(gc_.get("max_effort_distance_covered_yards", pd.Series(dtype=float)), errors="coerce").fillna(0) * YARDS_TO_M\n                ath_key = normalize_name(athlete)\n                ath_game = gc_[gc_["_nname"] == ath_key]\n                for gd, ggrp in ath_game.groupby("_gdate"):\n                    runs, dist_m = float(ggrp["_runs"].sum()), float(ggrp["_dist_m"].sum())\n                    if runs >= 1:\n                        game_days_sprint.add(gd)\n                    game_runs_by_date[gd] = runs\n                    game_dist_by_date[gd] = dist_m\n                    game_hsr_by_date[gd] = dist_m  # max-effort runs are >=95th pct -> count as HSR too\n\n        today_game_sprint = report_ts in game_days_sprint\n        today_game_runs = game_runs_by_date.get(report_ts, 0.0)\n        today_game_dist = game_dist_by_date.get(report_ts, 0.0)\n        today_game_hsr = game_hsr_by_date.get(report_ts, 0.0)\n\n        today_has_sprint = today_has_sprint_gps or today_game_sprint\n        today_has_hsr = today_has_hsr_gps or today_game_hsr >= MEANINGFUL_HSR_THRESHOLD_M\n\n        # Days since last sprint / HSR exposure (numeric -- still drives the\n        # Needs Exposure threshold check) + the actual date (for display --\n        # see last_sprint_date below).\n        if today_has_sprint:\n            rec["days_since_sprint"] = 0\n            rec["last_sprint_date"] = report_ts\n        else:\n            sprint_hist_gps = prior[(prior[sc].fillna(0) >= MEANINGFUL_SPRINT_THRESHOLD) |\n                                     (prior[sdc].fillna(0) >= MEANINGFUL_SPRINT_DIST_M)] if (sc in prior.columns and sdc in prior.columns) else pd.DataFrame()\n            gps_sprint_dates = set(sprint_hist_gps["Date"].dt.normalize().tolist()) if not sprint_hist_gps.empty else set()\n            game_sprint_dates_prior = {d for d in game_days_sprint if d < report_ts}\n            all_sprint_dates = gps_sprint_dates | game_sprint_dates_prior\n            if all_sprint_dates:\n                last_date = max(all_sprint_dates)\n                rec["last_sprint_date"] = last_date\n                # -1 day adjustment: reports always cover the PRIOR day\'s\n                # session (generated/read the day after), so a raw gap of 1\n                # day (last sprint = "yesterday" relative to report_date) is\n                # effectively "today" by the time anyone reads the report --\n                # floored at 0, never negative. Still used for the Needs\n                # Exposure threshold check; the report itself now shows\n                # last_sprint_date directly instead of this number.\n                raw_days = int((report_ts - last_date).days)\n                rec["days_since_sprint"] = max(0, raw_days - 1)\n            else:\n                rec["days_since_sprint"] = None\n                rec["last_sprint_date"] = None\n\n        if today_has_hsr:\n            rec["days_since_hsr"] = 0\n        else:\n            hsr_hist_gps = prior[prior[hc].fillna(0) >= MEANINGFUL_HSR_THRESHOLD_M] if hc in prior.columns else pd.DataFrame()\n            gps_hsr_dates = set(hsr_hist_gps["Date"].dt.normalize().tolist()) if not hsr_hist_gps.empty else set()\n            game_hsr_dates_prior = {d for d, dist in game_hsr_by_date.items() if d < report_ts and dist >= MEANINGFUL_HSR_THRESHOLD_M}\n            all_hsr_dates = gps_hsr_dates | game_hsr_dates_prior\n            rec["days_since_hsr"] = int((report_ts - max(all_hsr_dates)).days) if all_hsr_dates else None\n\n        # 7-day rolling totals (GPS prior + game prior + today, same-date guard)\n        prior_7_sc = prior_7[sc].fillna(0) if sc in prior_7.columns else pd.Series(dtype=float)\n        prior_7_sdc = prior_7[sdc].fillna(0) if sdc in prior_7.columns else pd.Series(dtype=float)\n        prior_7_hsc = prior_7[hc].fillna(0) if hc in prior_7.columns else pd.Series(dtype=float)\n        prior_7_dates = set(prior_7["Date"].dt.normalize().tolist()) if not prior_7.empty else set()\n\n        today_sprint_v = float(today_sprints) if pd.notna(today_sprints) else 0.0\n        today_sdist_v = float(today_sdist) if pd.notna(today_sdist) else 0.0\n        today_hsr_v = float(today_hsr) if pd.notna(today_hsr) else 0.0\n\n        game_7d_sprint_days, game_7d_sprint_dist, game_7d_hsr = 0, 0.0, 0.0\n        cut_7_norm = cut_7.normalize()\n        for gd, gdist in game_dist_by_date.items():\n            if cut_7_norm <= gd < report_ts and gd not in prior_7_dates:\n                if gd in game_days_sprint:\n                    game_7d_sprint_days += 1\n                game_7d_sprint_dist += gdist\n                game_7d_hsr += game_hsr_by_date.get(gd, 0.0)\n\n        gps_7d_sprint_days = int(((prior_7_sc >= MEANINGFUL_SPRINT_THRESHOLD) | (prior_7_sdc >= MEANINGFUL_SPRINT_DIST_M)).sum())\n\n        rec["r7_sprint_days"] = gps_7d_sprint_days + game_7d_sprint_days + (1 if today_has_sprint else 0)\n        rec["r7_sprints"] = float(prior_7_sc.sum()) + today_sprint_v\n        rec["r7_sprint_dist"] = float(prior_7_sdc.sum()) + game_7d_sprint_dist + today_sdist_v + today_game_dist\n        rec["r7_hsr"] = float(prior_7_hsc.sum()) + game_7d_hsr + today_hsr_v + today_game_hsr\n\n        rec["today_game_runs"] = today_game_runs\n        rec["today_game_sprint_dist_m"] = today_game_dist\n        rec["today_game_hsr_m"] = today_game_hsr\n        records.append(rec)\n\n    result = pd.DataFrame(records)\n    if result.empty:\n        return result\n\n    # ── Pass 2: flagging ──────────────────────────────────────────────────────\n    cut_rolling = report_ts - pd.Timedelta(days=ROLLING_WINDOW_DAYS)\n\n    for (team, pos), grp_idx in result.groupby(["Team", "Position"]).groups.items():\n        grp = result.loc[grp_idx]\n        n = len(grp)\n\n        rolling_stats = {}\n        hist_pos = df[(df["Team"] == team) & (df["Position"] == pos) &\n                      (df["Date"].dt.normalize() >= cut_rolling) & (df["Date"].dt.normalize() < report_ts)]\n        for col, short, unit, flag_enabled, flag_mode in METRICS:\n            if flag_mode != "rolling_pct":\n                continue\n            if col in hist_pos.columns:\n                hvals = pd.to_numeric(hist_pos[col], errors="coerce").dropna()\n                rolling_stats[col] = (float(hvals.mean()), float(hvals.std(ddof=1))) if len(hvals) >= ROLLING_MIN_SESSIONS else (np.nan, np.nan)\n            else:\n                rolling_stats[col] = (np.nan, np.nan)\n\n        for col, short, unit, flag_enabled, flag_mode in METRICS:\n            val_col, flag_col, z_col = f"{col}_val", f"{col}_flag", f"{col}_z"\n            if not flag_enabled:\n                continue\n\n            if flag_mode == "rolling_pct":\n                r_mean, r_sd = rolling_stats.get(col, (np.nan, np.nan))\n                min_delta = _ROLLING_MIN_DELTA.get(col, 0)\n                use_rolling = pd.notna(r_mean) and pd.notna(r_sd) and r_sd > 0\n\n                if not use_rolling:\n                    fallback_vals = grp[val_col].dropna()\n                    if n < MIN_GROUP_SIZE or len(fallback_vals) < MIN_GROUP_SIZE:\n                        continue\n                    fb_mean, fb_sd = fallback_vals.mean(), fallback_vals.std(ddof=1)\n                    if pd.isna(fb_sd) or fb_sd == 0:\n                        continue\n                    for idx in grp_idx:\n                        v = result.at[idx, val_col]\n                        if pd.isna(v):\n                            continue\n                        z = (v - fb_mean) / fb_sd\n                        result.at[idx, z_col] = round(z, 2)\n                        if z >= REVIEW_Z:\n                            result.at[idx, flag_col] = "review"\n                            result.at[idx, "flag_count"] += 1\n                        elif z >= MONITOR_Z and result.at[idx, flag_col] is None:\n                            result.at[idx, flag_col] = "monitor"\n                            result.at[idx, "flag_count"] += 1\n                else:\n                    for idx in grp_idx:\n                        v = result.at[idx, val_col]\n                        if pd.isna(v):\n                            continue\n                        z = (v - r_mean) / r_sd\n                        result.at[idx, z_col] = round(z, 2)\n                        abs_delta = v - r_mean\n                        if z >= ROLLING_REVIEW_Z and abs_delta >= min_delta:\n                            result.at[idx, flag_col] = "review"\n                            result.at[idx, "flag_count"] += 1\n                        elif z >= ROLLING_MONITOR_Z and abs_delta >= min_delta and result.at[idx, flag_col] is None:\n                            result.at[idx, flag_col] = "monitor"\n                            result.at[idx, "flag_count"] += 1\n            else:\n                vals = grp[val_col].dropna()\n                if n < MIN_GROUP_SIZE or len(vals) < MIN_GROUP_SIZE:\n                    continue\n                g_mean, g_sd = vals.mean(), vals.std(ddof=1)\n                if pd.isna(g_sd) or g_sd == 0:\n                    continue\n                for idx in grp_idx:\n                    v = result.at[idx, val_col]\n                    if pd.isna(v):\n                        continue\n                    z = (v - g_mean) / g_sd\n                    result.at[idx, z_col] = round(z, 2)\n                    if z >= REVIEW_Z:\n                        result.at[idx, flag_col] = "review"\n                        result.at[idx, "flag_count"] += 1\n                    elif z >= MONITOR_Z and result.at[idx, flag_col] is None:\n                        result.at[idx, flag_col] = "monitor"\n                        result.at[idx, "flag_count"] += 1\n\n    return result.sort_values(["Team", "flag_count", "Athlete"], ascending=[True, False, True]).reset_index(drop=True)\n\n\ndef classify_workload_status(row, acwr_val, game_row):\n    """\n    Returns (status, primary_driver, recommended_action, combined_load, practice_level).\n    Priority order: Data Check > Review > Monitor > Needs Exposure > Prepared.\n    """\n    has_gps = bool(row.get("has_gps", True))\n    gl_row = game_row if isinstance(game_row, dict) else (game_row.to_dict() if game_row is not None and not isinstance(game_row, dict) else None)\n    game_load_class = gl_row.get("load_class", "\\u2014") if gl_row is not None else "\\u2014"\n    acwr_is_nan = pd.isna(acwr_val)\n\n    flags = {col: row.get(f"{col}_flag") for col, *_ in METRICS}\n    has_review = any(f == "review" for f in flags.values())\n    has_monitor = any(f == "monitor" for f in flags.values())\n\n    practice_level = "High" if has_review else ("Moderate" if has_monitor else "Low")\n    combined_load = classify_combined_load(practice_level, game_load_class)\n\n    ds_sprint = row.get("days_since_sprint")\n    ds_hsr = row.get("days_since_hsr")\n    r7_sprint_dist = float(row.get("r7_sprint_dist") or 0)\n    r7_hsr = float(row.get("r7_hsr") or 0)\n\n    if not has_gps:\n        return ("Data Check", "No GPS session", "Confirm if off-day, injury, or device issue", combined_load, practice_level)\n\n    all_nan = all(pd.isna(row.get(f"{col}_val")) for col, *_ in METRICS)\n    if all_nan:\n        return ("Data Check", "Missing GPS data", "Confirm device sync and session upload", combined_load, practice_level)\n\n    if not acwr_is_nan and acwr_val >= ACWR_HIGH_RISK:\n        return ("Review", f"High ACWR ({acwr_val:.2f})", "Check soreness/readiness; consider modified next-day workload", combined_load, practice_level)\n    if combined_load == "Major Load Concern":\n        return ("Review", "High combined practice + game load", "Check soreness/readiness; avoid additional sprint volume tomorrow", combined_load, practice_level)\n    if game_load_class == "High" and not acwr_is_nan and acwr_val >= ACWR_ELEVATED:\n        return ("Review", f"High game load + elevated ACWR ({acwr_val:.2f})", "Avoid extra sprint volume tomorrow; check next-day readiness", combined_load, practice_level)\n    if has_review:\n        for col, short, unit, flag_enabled, flag_mode in METRICS:\n            if flag_enabled and flags.get(col) == "review":\n                z = row.get(f"{col}_z", np.nan)\n                zs = f" (z={z:.1f})" if pd.notna(z) else ""\n                direction = "spike" if (pd.notna(z) and z > 0) else "low vs group"\n                return ("Review", f"{short} {direction}{zs}", "Seek athlete context; consider modified next-day workload", combined_load, practice_level)\n\n    if not acwr_is_nan and acwr_val >= ACWR_ELEVATED:\n        return ("Monitor", f"Elevated ACWR ({acwr_val:.2f})", "Watch next session; avoid extra volume", combined_load, practice_level)\n    if combined_load == "Practice-Driven Spike":\n        return ("Monitor", "Practice load spike", "Watch next session; note load context", combined_load, practice_level)\n    if has_monitor:\n        for col, short, unit, flag_enabled, flag_mode in METRICS:\n            if flag_enabled and flags.get(col) == "monitor":\n                z = row.get(f"{col}_z", np.nan)\n                zs = f" (z={z:.1f})" if pd.notna(z) else ""\n                direction = "elevated" if (pd.notna(z) and z > 0) else "low vs group"\n                return ("Monitor", f"{short} {direction}{zs}", "Watch next session; note load trend", combined_load, practice_level)\n\n    # Needs Exposure -- actual rolling gaps only; a single quiet day ("Possible\n    # Underload" combined-load) is deliberately NOT sufficient on its own.\n    # pd.notna(), not "is not None": these values have already round-tripped\n    # through a DataFrame by the time they reach this function, and pandas\n    # silently converts None to NaN in that process.\n    today_has_sprint_exp = (pd.notna(ds_sprint) and ds_sprint == 0)\n    today_has_hsr_exp = (pd.notna(ds_hsr) and ds_hsr == 0)\n\n    if pd.notna(ds_sprint) and ds_sprint > MAX_DAYS_WITHOUT_SPRINT:\n        return ("Needs Exposure", f"No sprint exposure ({int(ds_sprint)}d)", "Add controlled sprint exposure if healthy", combined_load, practice_level)\n    if pd.notna(ds_hsr) and ds_hsr > MAX_DAYS_WITHOUT_HSR:\n        return ("Needs Exposure", f"No HSR exposure ({int(ds_hsr)}d)", "Add controlled HSR exposure if healthy", combined_load, practice_level)\n    if not today_has_sprint_exp and r7_sprint_dist < LOW_7DAY_SPRINT_DIST_M:\n        return ("Needs Exposure", f"Low 7-day sprint dist ({r7_sprint_dist:.0f}m)", "Add controlled sprint exposure if healthy", combined_load, practice_level)\n    if not today_has_hsr_exp and r7_hsr < LOW_7DAY_HSR_M:\n        return ("Needs Exposure", f"Low 7-day HSR ({r7_hsr:.0f}m)", "Increase high-speed running volume if healthy", combined_load, practice_level)\n\n    return ("Prepared", "Normal workload", "Maintain normal plan", combined_load, practice_level)')
_install_embedded_module('gps_report_html', '"""\ngps_report_html.py — GPS Workload Report, Chart.js + browser-print version.\n\nSame visual system as jump_report_html.py (CMJ Trend Report): navy header\nwith circular "W" mark, red accent rule, cover page, boxed section headers,\nbadges, footer, and real Chart.js line charts rendered via a headless\nbrowser (see pdf_render.py) rather than WeasyPrint.\n\nStructurally different from CMJ, on purpose: GPS Workload\'s underlying flag\nlogic (gps_flags.py) is inherently a DAILY snapshot -- every athlete vs\ntheir position group on one specific date -- not a personal longitudinal\ntrend. This report combines both views in one document:\n  1. Cover page -- today\'s snapshot summary\n  2. Daily Snapshot table -- every athlete today, sorted by severity\n  3. One trend page per Review/Monitor athlete -- their own GPS metrics\n     charted over the trailing 28 days, for longitudinal context\n  4. Data Check / no-session page, if applicable\n\nNote: the per-athlete trend charts show that athlete\'s own raw values over\ntime -- they do NOT reconstruct the historical day-by-day position-group\nz-score (that would require recomputing group stats for every past date,\nwhich is a lot of extra computation for a report meant to give trend\ncontext, not re-litigate every past day\'s flag). The daily snapshot table\nis where the actual position-group flagging logic is shown.\n"""\n\nimport json\nimport logging\nfrom datetime import date\n\nimport numpy as np\nimport pandas as pd\n\nfrom gps_report_data import METRICS, aggregate_drills, parse_sheet_dates, PITCHER_POSITIONS, METERS_TO_YARDS\nfrom gps_flags import (\n    compute_athlete_windows, compute_game_classifications,\n    classify_workload_status, ACWR_OPTIMAL_LOW, ACWR_ELEVATED, ACWR_HIGH_RISK,\n)\nfrom name_utils import normalize_name\n\nlogger = logging.getLogger("gps_trends.html")\n\nSTATUS_ORDER = {"Review": 4, "Monitor": 3, "Needs Exposure": 2, "Data Check": 1, "Prepared": 0}\nSTATUS_BADGE_CLASS = {\n    "Review": "bbh", "Monitor": "bmn", "Needs Exposure": "bex",\n    "Prepared": "bon", "Data Check": "bdc",\n}\nSTATUS_ICON = {\n    "Review": "\\u2717", "Monitor": "\\u25b3", "Needs Exposure": "\\u25cb",\n    "Prepared": "\\u2713", "Data Check": "?",\n}\n\nCSS = """\n:root{--N:#14225A;--R:#AB0003;--W:#FFFFFF;--SL:#A2A2A2;--D:#5C82A5;--SK:#48B8E7;\n      --SD:#D9C89D;--RA:#F0F2F8;--G:#2E7D32;--DK:#1A1A1A;}\n*{box-sizing:border-box;margin:0;padding:0;}\nbody{font-family:\'Gill Sans\',\'Gill Sans MT\',Calibri,\'Helvetica Neue\',Arial,sans-serif;\n     background:#c8ccda;color:var(--DK);}\n.pbtn{position:fixed;top:14px;right:16px;z-index:999;background:var(--N);color:#fff;\n      border:none;padding:8px 18px;font-size:7.5pt;font-family:inherit;font-weight:bold;\n      cursor:pointer;letter-spacing:.07em;text-transform:uppercase;border-radius:2px;}\n.pbtn:hover{background:#1c2e7a;}\n.pg{width:11in;min-height:8.5in;background:#fff;margin:.35in auto;position:relative;\n    box-shadow:0 4px 24px rgba(0,0,0,.18);overflow:hidden;}\n.pi{padding:.22in .55in .75in;}\n.pi-t{padding:.18in .55in .35in;}\n.ph{background:var(--N);padding:.15in .55in .11in;display:flex;align-items:center;\n    justify-content:space-between;}\n.pht{color:#fff;font-size:11.5pt;font-weight:bold;letter-spacing:.04em;}\n.phs{color:rgba(255,255,255,.55);font-size:7pt;margin-top:3px;}\n.phl{width:28px;height:28px;border:1.5px solid rgba(255,255,255,.3);border-radius:50%;\n     display:flex;align-items:center;justify-content:center;color:#fff;font-size:11pt;\n     font-weight:bold;flex-shrink:0;}\n.hr3{height:3px;background:var(--R);}\n.pf{position:absolute;bottom:0;left:0;right:0;padding:6px .55in 7px;display:flex;\n    justify-content:space-between;font-size:6.5pt;color:var(--SL);\n    border-top:.5px solid #d0d4e0;}\n.cv{padding:.28in .55in .75in;}\n.cvt{text-align:center;padding:.2in 0 .12in;}\n.cvh{font-size:24pt;font-weight:bold;color:var(--N);line-height:1.05;margin-bottom:5px;}\n.cvs{font-size:8.5pt;color:var(--SL);letter-spacing:.12em;text-transform:uppercase;}\n.cvr{height:2px;background:var(--R);width:56px;margin:10px auto;}\n.ib{border:1px solid #d5d8e8;border-top:3px solid var(--R);margin:14px 0;background:var(--RA);}\n.ir{display:flex;justify-content:space-between;align-items:center;padding:7px 14px;\n    border-bottom:.5px solid #cdd0e0;font-size:8.5pt;}\n.ir:last-child{border-bottom:none;}\n.il{color:var(--SL);font-size:7.5pt;}\n.iv{font-weight:bold;color:var(--N);}\n.sg{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin:14px 0;}\n.sc{border:1px solid #d5d8e8;border-top:3px solid var(--N);padding:11px 13px;background:#fff;}\n.sl2{font-size:6pt;color:var(--SL);text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px;}\n.sv{font-size:17pt;font-weight:bold;color:var(--N);line-height:1;}\n.ss{font-size:6.5pt;color:var(--SL);margin-top:4px;}\n.sh{background:var(--N);color:#fff;padding:6px 11px;font-size:9pt;font-weight:bold;\n    letter-spacing:.07em;text-transform:uppercase;border-left:4px solid var(--R);margin:14px 0 10px;}\n.sh:first-child{margin-top:0;}\n.cx{background:var(--RA);border-left:3px solid var(--N);padding:7px 11px;font-size:7.5pt;\n    color:var(--DK);margin-bottom:12px;line-height:1.6;}\ntable{width:100%;border-collapse:collapse;font-size:7pt;}\nthead tr{background:var(--N);color:#fff;}\nthead th{padding:5px 6px;font-weight:bold;font-size:6.5pt;text-align:center;\n         letter-spacing:.03em;text-transform:uppercase;}\nthead th:first-child{text-align:left;}\nthead th:nth-child(2){text-align:left;}\ntbody tr:nth-child(even){background:var(--RA);}\ntbody tr:nth-child(odd){background:#fff;}\ntbody td{padding:4px 6px;color:var(--DK);vertical-align:middle;text-align:center;}\ntbody td:first-child,tbody td:nth-child(2){text-align:left;}\n.bon{display:inline-block;background:#e8f5e9;color:var(--G);font-size:6.5pt;font-weight:bold;\n     padding:2px 7px;border-radius:2px;}\n.bbh{display:inline-block;background:#fee2e2;color:var(--R);font-size:6.5pt;font-weight:bold;\n     padding:2px 7px;border-radius:2px;}\n.bmn{display:inline-block;background:#fff8e1;color:#b45309;font-size:6.5pt;font-weight:bold;\n     padding:2px 7px;border-radius:2px;}\n.bex{display:inline-block;background:#e8f4fd;color:#1565C0;font-size:6.5pt;font-weight:bold;\n     padding:2px 7px;border-radius:2px;}\n.bdc{display:inline-block;background:#f5f5f5;color:#616161;font-size:6.5pt;font-weight:bold;\n     padding:2px 7px;border-radius:2px;}\n.stale-date{color:var(--R);font-weight:bold;}\n.ip-hdr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;\n        border-bottom:1.5px solid var(--N);padding-bottom:4px;}\n.ip-name{font-size:13pt;font-weight:bold;color:var(--N);}\n.ip-meta{font-size:7.5pt;color:var(--SL);margin-top:2px;}\n.ip-insight{font-size:8pt;color:var(--DK);margin-top:4px;}\n.ip-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px;\n         break-inside:avoid;page-break-inside:avoid;}\n.ip-chart{border:1px solid #e8eaf0;padding:5px 7px;break-inside:avoid;page-break-inside:avoid;}\n.ip-ct{font-size:7.5pt;font-weight:bold;color:var(--N);text-transform:uppercase;\n       letter-spacing:.05em;margin-bottom:1px;}\n.ip-cu{font-size:6pt;color:var(--SL);margin-bottom:2px;}\n.ip-cc{position:relative;height:140px;}\n.no-flags{font-size:10pt;color:var(--SL);padding:20px;}\n@media print{\n  body{background:#fff;}\n  .pbtn{display:none;}\n  .pg{margin:0;box-shadow:none;page-break-after:always;break-after:page;}\n  .pg:last-child{page-break-after:auto;}\n}\n"""\n\n\ndef _page_chrome(title, subtitle, team_name, page_num, page_total):\n    header = f"""<div class="ph"><div><div class="pht">{title}</div>\n        <div class="phs">{subtitle}</div></div><div class="phl">W</div></div><div class="hr3"></div>"""\n    footer = f"""<div class="pf"><span>Washington Nationals \\u2014 Player Development S&amp;C</span>\n        <span>{team_name} \\u00b7 GPS Workload Report</span>\n        <span>CONFIDENTIAL &nbsp;|&nbsp; Page {page_num} of {page_total}</span></div>"""\n    return header, footer\n\n\ndef _acwr_zone_label(acwr_val):\n    if pd.isna(acwr_val):\n        return "\\u2014"\n    if acwr_val >= ACWR_HIGH_RISK:\n        return "High Risk"\n    if acwr_val >= ACWR_ELEVATED:\n        return "Elevated"\n    if acwr_val < ACWR_OPTIMAL_LOW:\n        return "Low"\n    return "Optimal"\n\n\ndef _linreg_fit_ends(date_nums, vals):\n    if len(vals) < 2:\n        return None, None\n    try:\n        slope, intercept = np.polyfit(date_nums, vals, 1)\n    except Exception:\n        return None, None\n    return float(slope * date_nums[0] + intercept), float(slope * date_nums[-1] + intercept)\n\n\ndef _build_metric_chart(session_history, metric_col, canvas_id):\n    """session_history: DataFrame with Date + metric columns for ONE athlete,\n    already aggregated to one row per session, sorted chronologically."""\n    if metric_col not in session_history.columns:\n        return canvas_id, None\n    v = session_history[metric_col].dropna()\n    if v.empty:\n        return canvas_id, None\n\n    cutoff = session_history["Date"].max() - pd.Timedelta(days=14)\n    win = session_history[session_history["Date"] >= cutoff].dropna(subset=[metric_col])\n    if win.empty:\n        win = session_history.dropna(subset=[metric_col]).tail(1)\n\n    labels = [d.strftime("%-m/%-d") for d in win["Date"]]\n    vals = win[metric_col].round(2).tolist()\n    date_nums = (pd.to_datetime(win["Date"]) - pd.to_datetime(win["Date"].iloc[0])).dt.days.to_numpy(dtype=float)\n\n    fit_start, fit_end = _linreg_fit_ends(date_nums, vals)\n    trend_data = [None] * len(vals)\n    if fit_start is not None:\n        trend_data[0] = round(fit_start, 2)\n        trend_data[-1] = round(fit_end, 2)\n\n    range_vals = v.tolist()\n    if fit_start is not None:\n        range_vals.extend([fit_start, fit_end])\n    full_min, full_max = min(range_vals), max(range_vals)\n    span = full_max - full_min\n    pad = span * 0.15 if span > 0 else (abs(full_max) * 0.05 if full_max else 0.5)\n    y_min, y_max = round(max(0, full_min - pad), 3), round(full_max + pad, 3)\n\n    datasets = [{\n        "data": vals, "borderColor": "rgba(20,34,90,0.9)", "backgroundColor": "rgba(20,34,90,0.07)",\n        "borderWidth": 1.5, "pointRadius": 2, "fill": True, "tension": 0.25, "spanGaps": True,\n    }]\n    if fit_start is not None:\n        datasets.append({\n            "data": trend_data, "borderColor": "rgba(171,0,3,0.75)", "borderWidth": 1.5,\n            "borderDash": [5, 3], "pointRadius": 0, "fill": False, "tension": 0, "spanGaps": True,\n        })\n\n    config = {\n        "type": "line",\n        "data": {"labels": labels, "datasets": datasets},\n        "options": {\n            "responsive": True, "maintainAspectRatio": False,\n            "plugins": {"legend": {"display": False}},\n            "scales": {\n                "x": {"grid": {"display": False}, "ticks": {"font": {"size": 6}, "color": "#A2A2A2", "maxTicksLimit": 8}},\n                "y": {"grid": {"color": "#EEEEEE", "lineWidth": 0.4}, "ticks": {"font": {"size": 6.5}, "color": "#A2A2A2"},\n                      "min": y_min, "max": y_max},\n            },\n        },\n    }\n    return canvas_id, config\n\n\nLAMBDA_ACUTE = 2 / (7 + 1)     # 7-day acute EWMA smoothing constant\nLAMBDA_CHRONIC = 2 / (28 + 1)  # 28-day chronic EWMA smoothing constant\n\n\ndef compute_running_acwr(df_acwr, athlete, report_date=None):\n    """\n    Return the athlete\'s PP_ACWR sheet history.\n\n    The function name is retained for compatibility with the existing report\n    renderer, but ACWR is no longer recomputed from PP_Sprint. The authoritative\n    source is PP_ACWR. If report_date is supplied, only sheet observations on or\n    before that date are eligible.\n    """\n    empty = pd.DataFrame(columns=["game_date", "acwr"])\n    if df_acwr is None or df_acwr.empty:\n        return empty\n\n    a = df_acwr.copy()\n    a.columns = a.columns.str.strip().str.lower()\n    if "batter" not in a.columns or "ewma_acwr_7_28" not in a.columns:\n        return empty\n\n    ath_key = normalize_name(athlete)\n    a["_nname"] = a["batter"].apply(lambda x: normalize_name(str(x)))\n    a = a[a["_nname"] == ath_key].copy()\n    if a.empty:\n        return empty\n\n    a["acwr"] = pd.to_numeric(a["ewma_acwr_7_28"], errors="coerce")\n    if "date" in a.columns:\n        a["game_date"] = parse_sheet_dates(a["date"]).dt.normalize()\n    else:\n        a["game_date"] = pd.NaT\n\n    a = a.dropna(subset=["game_date", "acwr"]).copy()\n    if report_date is not None:\n        cutoff = pd.Timestamp(report_date).normalize()\n        a = a[a["game_date"] <= cutoff].copy()\n    if a.empty:\n        return empty\n\n    # The final sheet row wins if a player/date is duplicated.\n    a["_row_order"] = np.arange(len(a))\n    a = (\n        a.sort_values(["game_date", "_row_order"])\n         .drop_duplicates(subset=["game_date"], keep="last")\n         .sort_values("game_date")\n    )\n    return a[["game_date", "acwr"]].reset_index(drop=True)\n\n\ndef _build_acwr_chart(df_acwr, athlete, canvas_id, report_date=None):\n    """\n    ACWR time series for one athlete from PP_ACWR, with fixed zone-threshold\n    reference lines (0.8 / 1.3 / 1.5). No PP_Sprint-based ACWR is calculated.\n    """\n    acwr_series = compute_running_acwr(df_acwr, athlete, report_date=report_date)\n    acwr_series = acwr_series.dropna(subset=["acwr"])\n    if acwr_series.empty:\n        return canvas_id, None\n\n    cutoff = acwr_series["game_date"].max() - pd.Timedelta(days=14)\n    win = acwr_series[acwr_series["game_date"] >= cutoff]\n    if win.empty:\n        win = acwr_series.tail(1)\n\n    labels = [d.strftime("%-m/%-d") for d in win["game_date"]]\n    vals = win["acwr"].round(2).tolist()\n    n = len(vals)\n\n    range_vals = vals + [ACWR_OPTIMAL_LOW, ACWR_ELEVATED, ACWR_HIGH_RISK, 0]\n    full_min, full_max = min(range_vals), max(range_vals)\n    span = full_max - full_min\n    pad = span * 0.15 if span > 0 else 0.2\n    y_min, y_max = round(max(0, full_min - pad), 2), round(full_max + pad, 2)\n\n    datasets = [\n        {"data": vals, "borderColor": "rgba(20,34,90,0.9)", "backgroundColor": "rgba(20,34,90,0.07)",\n         "borderWidth": 1.5, "pointRadius": 2, "fill": True, "tension": 0.25, "spanGaps": True},\n        {"data": [ACWR_HIGH_RISK] * n, "borderColor": "rgba(171,0,3,0.65)", "borderWidth": 1,\n         "borderDash": [3, 3], "pointRadius": 0, "fill": False},\n        {"data": [ACWR_ELEVATED] * n, "borderColor": "rgba(180,83,9,0.6)", "borderWidth": 1,\n         "borderDash": [3, 3], "pointRadius": 0, "fill": False},\n        {"data": [ACWR_OPTIMAL_LOW] * n, "borderColor": "rgba(162,162,162,0.55)", "borderWidth": 1,\n         "borderDash": [2, 2], "pointRadius": 0, "fill": False},\n    ]\n    config = {\n        "type": "line",\n        "data": {"labels": labels, "datasets": datasets},\n        "options": {\n            "responsive": True, "maintainAspectRatio": False,\n            "plugins": {"legend": {"display": False}},\n            "scales": {\n                "x": {"grid": {"display": False}, "ticks": {"font": {"size": 6}, "color": "#A2A2A2", "maxTicksLimit": 8}},\n                "y": {"grid": {"color": "#EEEEEE", "lineWidth": 0.4}, "ticks": {"font": {"size": 6.5}, "color": "#A2A2A2"},\n                      "min": y_min, "max": y_max},\n            },\n        },\n    }\n    return canvas_id, config\n\n\ndef _last_game_load_str(game_row):\n    """\'4/47yd\' -- raw max-effort runs / distance, not the High/Mod/Low label."""\n    if game_row is None or not isinstance(game_row, dict):\n        return "\\u2014"\n    runs = game_row.get("max_effort_runs")\n    dist = game_row.get("max_effort_distance_covered_yards")\n    if pd.isna(runs) and pd.isna(dist):\n        return "\\u2014"\n    runs_str = f"{int(runs)}" if pd.notna(runs) else "\\u2014"\n    dist_str = f"{int(round(dist))}yd" if pd.notna(dist) else "\\u2014"\n    return f"{runs_str}/{dist_str}"\n\n\ndef _practice_load_yesterday_str(df_gps, athlete, report_date):\n    """\n    \'4/1/45yd\' -- accelerations / sprints / HSR distance (converted to\n    yards), for EXACTLY yesterday relative to report_date. Only yesterday\n    -- not the last recorded session, however old. \'-\' if no practice\n    session exists for that specific date.\n    """\n    yesterday = pd.Timestamp(report_date).normalize() - pd.Timedelta(days=1)\n    ath_df = df_gps[df_gps["Athlete"] == athlete].copy()\n    if ath_df.empty:\n        return "-"\n    ath_df = aggregate_drills(ath_df)\n    on_yesterday = ath_df[ath_df["Date"].dt.normalize() == yesterday]\n    if on_yesterday.empty:\n        return "-"\n    row = on_yesterday.iloc[0]\n    accels = row.get("n_accelerations")\n    sprints = row.get("n_sprints")\n    hsr_m = row.get("hsr_distance_m")\n    if pd.isna(accels) and pd.isna(sprints) and pd.isna(hsr_m):\n        return "-"\n    accels_str = f"{int(accels)}" if pd.notna(accels) else "\\u2014"\n    sprints_str = f"{int(sprints)}" if pd.notna(sprints) else "\\u2014"\n    hsr_yd_str = f"{int(round(hsr_m * METERS_TO_YARDS))}yd" if pd.notna(hsr_m) else "\\u2014"\n    return f"{accels_str}/{sprints_str}/{hsr_yd_str}"\n\n\ndef _athlete_session_history(df, athlete):\n    """One row per session (drill-aggregated), sorted chronologically, for one athlete."""\n    ath_df = df[df["Athlete"] == athlete].copy()\n    if ath_df.empty:\n        return pd.DataFrame()\n    ath_df = aggregate_drills(ath_df)\n    return ath_df.sort_values("Date").reset_index(drop=True)\n\n\ndef _build_cover_page(team_name, report_date, status_counts, page_total):\n    header, footer = _page_chrome(\n        "GPS WORKLOAD REPORT", f"Washington Nationals \\u00b7 {team_name}", team_name, 1, page_total,\n    )\n    return f"""<div class="pg">{header}\n    <div class="cv">\n        <div class="cvt">\n            <div class="cvh">GPS Workload Report<br>\n                <span style="font-size:13pt;font-weight:600;letter-spacing:0.04em;">{team_name}</span></div>\n            <div class="cvr"></div>\n            <div class="cvs">Daily Snapshot \\u00b7 StatSports GPS</div>\n        </div>\n        <div class="ib">\n            <div class="ir"><span class="il">Report Date</span><span class="iv">{report_date.strftime(\'%B %d, %Y\')}</span></div>\n            <div class="ir"><span class="il">Organization</span><span class="iv">Washington Nationals \\u2014 Player Development S&amp;C</span></div>\n            <div class="ir"><span class="il">Team</span><span class="iv">{team_name}</span></div>\n            <div class="ir"><span class="il">Flag Logic</span><span class="iv">Same-day / 14-day rolling position-group z-score + ACWR + exposure</span></div>\n        </div>\n        <div class="sg">\n            <div class="sc"><div class="sl2">Review</div><div class="sv" style="color:var(--R)">{status_counts.get(\'Review\',0)}</div><div class="ss">Prioritize today</div></div>\n            <div class="sc"><div class="sl2">Monitor</div><div class="sv" style="color:#b45309">{status_counts.get(\'Monitor\',0)}</div><div class="ss">Watch closely</div></div>\n            <div class="sc"><div class="sl2">Needs Exposure</div><div class="sv" style="color:#1565C0">{status_counts.get(\'Needs Exposure\',0)}</div><div class="ss">Rolling gap</div></div>\n            <div class="sc"><div class="sl2">Prepared</div><div class="sv" style="color:var(--G)">{status_counts.get(\'Prepared\',0)}</div><div class="ss">Normal workload</div></div>\n            <div class="sc"><div class="sl2">Data Check</div><div class="sv" style="color:var(--SL)">{status_counts.get(\'Data Check\',0)}</div><div class="ss">No/missing session</div></div>\n        </div>\n    </div>{footer}</div>"""\n\n\ndef _build_snapshot_table_page(rows, page_num, page_total, team_name, report_ts):\n    """rows: list of dicts with Athlete, Position, status, primary_driver,\n    combined_load, acwr_val, recommended_action, days_since_sprint,\n    last_sprint_date, game_row, practice_load_yesterday -- sorted by severity."""\n    header, footer = _page_chrome(\n        "DAILY SNAPSHOT \\u2014 ALL ATHLETES", "Sorted by severity", team_name, page_num, page_total,\n    )\n    trs = []\n    for r in rows:\n        badge_cls = STATUS_BADGE_CLASS.get(r["status"], "bdc")\n        icon = STATUS_ICON.get(r["status"], "")\n        acwr_str = f"{r[\'acwr_val\']:.2f}" if pd.notna(r["acwr_val"]) else "\\u2014"\n\n        # Last game load -- raw max-effort runs/distance (e.g. "4/47yd"),\n        # not the High/Moderate/Low label, per request.\n        game_load_str = _last_game_load_str(r.get("game_row"))\n\n        # Practice load -- ONLY yesterday relative to report date, "-" if\n        # no practice session recorded that specific day.\n        practice_load_str = r.get("practice_load_yesterday", "-")\n\n        # Last sprint exposure -- numeric days-ago, adjusted so 0 = yesterday,\n        # 1 = two days ago, 2 = three days ago, etc. (accounts for the\n        # built-in one-day reporting lag -- see gps_flags.compute_athlete_windows).\n        # Highlighted red/bold if more than 5.\n        ds = r.get("days_since_sprint")\n        if pd.isna(ds):\n            ds_str = "no history"\n        else:\n            ds_str = f\'<span class="stale-date">{int(ds)}</span>\' if ds > 5 else str(int(ds))\n\n        trs.append(f"""<tr>\n            <td>{r[\'Athlete\']}</td><td>{r[\'Position\']}</td>\n            <td><span class="{badge_cls}">{icon} {r[\'status\']}</span></td>\n            <td>{r[\'primary_driver\']}</td>\n            <td>{r[\'combined_load\']}</td>\n            <td>{acwr_str}</td>\n            <td>{game_load_str}</td>\n            <td>{practice_load_str}</td>\n            <td>{ds_str}</td>\n        </tr>""")\n    return f"""<div class="pg">{header}\n    <div class="pi-t">\n        <div class="sh">Today\'s Roster \\u00b7 {len(rows)} Athletes</div>\n        <div class="cx">Status hierarchy (highest wins): Review \\u2192 Monitor \\u2192 Needs Exposure \\u2192 Prepared. Data Check athletes had no GPS session or all metrics missing today. Last Game Load = max-effort runs/distance. Practice Load (Accel/Sprint/HSR) = yesterday only. Last Sprint = days ago (0 = yesterday, 1 = two days ago, etc.), highlighted if more than 5.</div>\n        <table><thead><tr><th>Athlete</th><th>Pos</th><th>Status</th><th>Primary Driver</th><th>Combined Load</th><th>ACWR</th><th>Last Game Load</th><th>Practice Load (Yest.)</th><th>Last Sprint</th></tr></thead>\n        <tbody>{\'\'.join(trs)}</tbody></table>\n    </div>{footer}</div>"""\n\n\ndef _build_athlete_trend_page(df, df_acwr, ath, snap_row, acwr_val, game_row, page_num, page_total, team_name, chart_registry, idx, report_date=None):\n    status, primary_driver, recommended_action, combined_load, practice_level = classify_workload_status(\n        snap_row, acwr_val, game_row\n    )\n    badge_cls = STATUS_BADGE_CLASS.get(status, "bdc")\n    icon = STATUS_ICON.get(status, "")\n\n    session_history = _athlete_session_history(df, ath)\n    n_sessions = len(session_history)\n    date_range = ""\n    if n_sessions:\n        d0, d1 = session_history["Date"].iloc[0], session_history["Date"].iloc[-1]\n        date_range = d0.strftime("%b %Y") if d0.month == d1.month else f"{d0.strftime(\'%b\')}\\u2013{d1.strftime(\'%b %Y\')}"\n\n    acwr_str = f"{acwr_val:.2f} ({_acwr_zone_label(acwr_val)})" if pd.notna(acwr_val) else "\\u2014"\n    game_load_display = _last_game_load_str(game_row)\n    game_str = f" \\u00b7 Last game load: {game_load_display}" if game_load_display != "\\u2014" else ""\n\n    tiles = []\n    for metric_idx, (col, short, unit, flag_enabled, flag_mode) in enumerate(METRICS):\n        canvas_id = f"gchart_{idx}_{metric_idx}"\n        cid, config = _build_metric_chart(session_history, col, canvas_id)\n        if config is not None:\n            chart_registry.append({"id": cid, "config": config})\n            unit_disp = unit if unit else ""\n            tiles.append(f"""\n            <div class="ip-chart">\n                <div class="ip-ct">{short}</div>\n                <div class="ip-cu">{unit_disp} \\u00b7 last 14 days \\u00b7 red dashed = trend</div>\n                <div class="ip-cc"><canvas id="{cid}"></canvas></div>\n            </div>""")\n\n    # ACWR tile -- appended after the 7 GPS metrics, using zone reference\n    # lines instead of a trend line (see _build_acwr_chart).\n    acwr_canvas_id = f"gchart_{idx}_acwr"\n    acwr_cid, acwr_config = _build_acwr_chart(df_acwr, ath, acwr_canvas_id, report_date=report_date)\n    if acwr_config is not None:\n        chart_registry.append({"id": acwr_cid, "config": acwr_config})\n        tiles.append(f"""\n        <div class="ip-chart">\n            <div class="ip-ct">ACWR</div>\n            <div class="ip-cu">ratio \\u00b7 last 14 days \\u00b7 dashed lines = 0.8 / 1.3 / 1.5 zones</div>\n            <div class="ip-cc"><canvas id="{acwr_cid}"></canvas></div>\n        </div>""")\n\n    header, footer = _page_chrome(\n        f"{ath.upper()} \\u2014 GPS TRENDS", f"Washington Nationals \\u00b7 {team_name} \\u00b7 GPS Workload Report",\n        team_name, page_num, page_total,\n    )\n    pos = snap_row.get("Position", "\\u2014")\n\n    return f"""<div class="pg">{header}\n    <div class="pi-t">\n        <div class="ip-hdr">\n            <div>\n                <div class="ip-name">{ath}</div>\n                <div class="ip-meta">{team_name} \\u00b7 {pos} \\u00b7 {n_sessions} sessions \\u00b7 {date_range}</div>\n                <div class="ip-insight">ACWR: {acwr_str}{game_str}</div>\n                <div class="ip-insight" style="margin-top:2px;"><b>{primary_driver}</b> \\u2014 {recommended_action}</div>\n            </div>\n            <div><span class="{badge_cls}">{icon} {status.upper()}</span></div>\n        </div>\n        <div class="ip-grid">{\'\'.join(tiles)}</div>\n    </div>{footer}</div>"""\n\n\ndef generate_report_html(df_gps, df_game, df_acwr, df_roster, team_name, report_date):\n    """Build the full report: cover, daily snapshot table, one trend page\n    per position player on the roster that day. Includes Print/PDF button +\n    Chart.js CDN.\n\n    ACWR comes directly from df_acwr (the PP_ACWR sheet). PP_Sprint remains\n    responsible for game-load context only; it is not used to derive ACWR.\n    """\n    report_ts = pd.Timestamp(report_date).normalize()\n\n    # Pitchers are excluded from roster injection specifically -- this\n    # report has never tracked the pitching staff\'s GPS workload, and\n    # synthesizing a "Data Check" row for every pitcher on the roster\n    # would flood the report with athletes who were never in scope. A\n    # pitcher can still appear if they happen to have a REAL GPS session\n    # (unchanged from before roster injection existed) -- only the\n    # synthetic no-data injection is position-filtered.\n    df_roster_position_players = None\n    if df_roster is not None and not df_roster.empty and "Position" in df_roster.columns:\n        df_roster_position_players = df_roster[\n            ~df_roster["Position"].astype(str).str.strip().str.upper().isin(PITCHER_POSITIONS)\n        ]\n\n    snapshot = compute_athlete_windows(df_gps, report_ts, df_game=df_game, roster_df=df_roster_position_players)\n    game_classified = compute_game_classifications(df_game) if df_game is not None and not df_game.empty else pd.DataFrame()\n\n    game_lk = {}\n    if not game_classified.empty:\n        tmp = game_classified.copy()\n        tmp["_gdate"] = parse_sheet_dates(tmp["game_date"]).dt.normalize()\n        tmp = tmp[tmp["_gdate"] <= report_ts].copy()\n        for pl, grp in tmp.groupby("batter"):\n            game_lk[normalize_name(pl)] = grp.sort_values("_gdate").iloc[-1].to_dict()\n\n    if snapshot.empty:\n        logger.warning("No GPS sessions found for %s on %s.", team_name, report_ts.date())\n        classified_rows = []\n    else:\n        classified_rows = []\n        for _, row in snapshot.iterrows():\n            key = normalize_name(row["Athlete"])\n            sheet_acwr = compute_running_acwr(df_acwr, row["Athlete"], report_date=report_ts)\n            acwr_val = float(sheet_acwr["acwr"].iloc[-1]) if not sheet_acwr.empty else np.nan\n            game_row = game_lk.get(key)\n            status, primary_driver, recommended_action, combined_load, practice_level = classify_workload_status(\n                row.to_dict(), acwr_val, game_row\n            )\n            classified_rows.append({\n                **row.to_dict(),\n                "status": status, "primary_driver": primary_driver,\n                "recommended_action": recommended_action, "combined_load": combined_load,\n                "practice_level": practice_level, "acwr_val": acwr_val, "game_row": game_row,\n                "practice_load_yesterday": _practice_load_yesterday_str(df_gps, row["Athlete"], report_ts),\n            })\n        classified_rows.sort(key=lambda r: (-STATUS_ORDER.get(r["status"], 0), r["Athlete"]))\n\n    status_counts = {}\n    for r in classified_rows:\n        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1\n\n    # All position players get a trend page now, not just Review/Monitor --\n    # pitchers excluded (matches the Hamstring Report\'s established\n    # position-player-only convention elsewhere in this org\'s reporting).\n    trend_athletes = [\n        r for r in classified_rows\n        if str(r.get("Position", "")).strip().upper() not in PITCHER_POSITIONS\n    ]\n\n    n_pages = 1 + (1 if classified_rows else 0) + len(trend_athletes)\n\n    chart_registry = []\n    pages_html = [_build_cover_page(team_name, report_ts, status_counts, n_pages)]\n\n    if classified_rows:\n        pages_html.append(_build_snapshot_table_page(classified_rows, 2, n_pages, team_name, report_ts))\n    else:\n        header, footer = _page_chrome("NO GPS SESSIONS", "Nothing to report for this date", team_name, 2, max(n_pages, 2))\n        pages_html.append(f\'<div class="pg">{header}<div class="pi-t"><div class="no-flags">No GPS sessions found for {team_name} on {report_ts.strftime("%B %d, %Y")}.</div></div>{footer}</div>\')\n        n_pages = 2\n\n    for idx, r in enumerate(trend_athletes):\n        page_num = 3 + idx\n        pages_html.append(_build_athlete_trend_page(\n            df_gps, df_acwr, r["Athlete"], r, r["acwr_val"], r["game_row"], page_num, n_pages, team_name, chart_registry, idx,\n            report_date=report_ts\n        ))\n\n    chart_js = "\\n".join(\n        f"try {{ new Chart(document.getElementById({json.dumps(c[\'id\'])}), {json.dumps(c[\'config\'])}); }} "\n        f"catch(e) {{ console.error(\'Chart failed:\', {json.dumps(c[\'id\'])}, e); }}"\n        for c in chart_registry\n    )\n\n    logger.info("Report for %s (%s): %d athletes, %d Review, %d Monitor, %d Needs Exposure, %d Prepared, %d Data Check.",\n                team_name, report_ts.date(), len(classified_rows),\n                status_counts.get("Review", 0), status_counts.get("Monitor", 0),\n                status_counts.get("Needs Exposure", 0), status_counts.get("Prepared", 0),\n                status_counts.get("Data Check", 0))\n\n    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">\n<title>{team_name} GPS Workload Report</title>\n<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>\n<style>{CSS}</style></head><body>\n<button class="pbtn" onclick="window.print()">Print / PDF</button>\n{\'\'.join(pages_html)}\n<script>\n{chart_js}\nwindow.__chartsReady = true;\n</script>\n</body></html>"""')
_install_embedded_module('pdf_render', '"""\npdf_render.py — Render the supplied Chart.js GPS report to PDF.\n\nKeeps the original browser-print behavior, with one Streamlit-Cloud addition:\nif Playwright\'s bundled Chromium is unavailable, use a system Chromium binary\ninstalled through packages.txt.\n"""\nimport logging\nimport shutil\nfrom pathlib import Path\n\nlogger = logging.getLogger("gps_trends.pdf_render")\n\n\ndef _system_chromium():\n    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):\n        path = shutil.which(name)\n        if path:\n            return path\n    return None\n\n\ndef render_html_to_pdf(html_path, pdf_path, timeout_ms=20000):\n    """Render the original Chart.js HTML report with a real Chromium engine."""\n    html_path = Path(html_path)\n    pdf_path = Path(pdf_path)\n\n    try:\n        from playwright.sync_api import sync_playwright\n    except ImportError:\n        logger.warning("Playwright is not installed; HTML report remains available.")\n        return False\n\n    try:\n        with sync_playwright() as p:\n            launch_kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}\n            system_chromium = _system_chromium()\n            if system_chromium:\n                launch_kwargs["executable_path"] = system_chromium\n            browser = p.chromium.launch(**launch_kwargs)\n            page = browser.new_page()\n            page.set_content(html_path.read_text(encoding="utf-8"), wait_until="load")\n            page.wait_for_function("typeof Chart !== \'undefined\' && window.__chartsReady === true", timeout=timeout_ms)\n            page.wait_for_timeout(300)\n            page.pdf(\n                path=str(pdf_path),\n                format="Letter",\n                landscape=True,\n                print_background=True,\n                margin={"top": "0in", "bottom": "0in", "left": "0in", "right": "0in"},\n            )\n            browser.close()\n        logger.info("PDF rendered to %s", pdf_path)\n        return True\n    except Exception as exc:\n        logger.warning("PDF render failed (%s). HTML report remains available at %s.", exc, html_path)\n        return False\n')

from gps_report_html import generate_report_html
from pdf_render import render_html_to_pdf


# =============================================================================
# CONFIGURATION
# =============================================================================

def _secret_value(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


SERVICE_ACCOUNT_FILE = os.getenv(
    "SERVICE_ACCOUNT_FILE",
    str(_secret_value("SERVICE_ACCOUNT_FILE", "") or ""),
)

PYTHON_REPORTS_SHEET_ID = os.getenv(
    "PYTHON_REPORTS_SHEET_ID",
    str(_secret_value("PYTHON_REPORTS_SHEET_ID", "") or ""),
)
STATSPORTS_SHEET_ID = os.getenv(
    "STATSPORTS_SHEET_ID",
    str(_secret_value("STATSPORTS_SHEET_ID", "") or ""),
)

STATSPORTS_TAB = os.getenv("STATSPORTS_TAB", str(_secret_value("STATSPORTS_TAB", "Raw Sessions")))
ROSTER_TAB = "Master Roster"
PP_SPRINT_TAB = os.getenv("PP_SPRINT_TAB", str(_secret_value("PP_SPRINT_TAB", "PP_Sprint")))
PP_ACWR_TAB = "PP_ACWR"

# STATSports API credentials belong in Streamlit Secrets / environment variables,
# never in the public GitHub repository.
STATSPORTS_API_BASE_URL = os.getenv(
    "STATSPORTS_API_BASE_URL",
    str(_secret_value("STATSPORTS_API_BASE_URL", "https://statsportsproseries.com") or "https://statsportsproseries.com"),
)
STATSPORTS_API_VERSION = os.getenv(
    "STATSPORTS_API_VERSION",
    str(_secret_value("STATSPORTS_API_VERSION", "7") or "7"),
)
STATSPORTS_API_TIMEOUT = int(os.getenv("STATSPORTS_API_TIMEOUT", "90"))
STATSPORTS_API_SLEEP_BETWEEN_DAYS = float(os.getenv("STATSPORTS_API_SLEEP_BETWEEN_DAYS", "0.10"))

# Optional local fallback for development/testing. Excel files are intentionally
# ignored by Git and are not needed on Streamlit Community Cloud.
SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_STATSPORTS_XLSX = Path(
    os.getenv("LOCAL_STATSPORTS_XLSX", str(SCRIPT_DIR / "StatSport Python (5).xlsx"))
)
LOCAL_REPORTS_XLSX = Path(
    os.getenv("LOCAL_REPORTS_XLSX", str(SCRIPT_DIR / "Python Reports (10)(3).xlsx"))
)
ALLOW_LOCAL_EXCEL_FALLBACK = os.getenv("ALLOW_LOCAL_EXCEL_FALLBACK", "0") != "0"


# =============================================================================
# STATUS SETTINGS — standalone GPS report flag engine defaults
# =============================================================================

# ACWR zones. ACWR values come directly from the PP_ACWR sheet
# (ewma_acwr_7_28); the app does not recompute ACWR from PP_Sprint.
ACWR_OPTIMAL_LOW = 0.80
ACWR_ELEVATED = 1.30
ACWR_HIGH_RISK = 1.50
LAMBDA_ACUTE = 2 / (7 + 1)
LAMBDA_CHRONIC = 2 / (28 + 1)

# Same-day position-group z-score thresholds for Sprint Distance, HSR, Total Distance.
REVIEW_Z = 2.00
MONITOR_Z = 1.50
MIN_GROUP_SIZE = 4

# 14-day rolling position-group baselines for Sprints and Accelerations.
ROLLING_WINDOW_DAYS = 14
ROLLING_MIN_SESSIONS = 3
ROLLING_REVIEW_Z = 2.00
ROLLING_MONITOR_Z = 1.50
ROLLING_MIN_DELTA_SPRINTS = 3.0
ROLLING_MIN_DELTA_ACCELS = 5.0

# Sprint / HSR exposure rules.
MEANINGFUL_SPRINT_THRESHOLD = 1.0
MEANINGFUL_SPRINT_DIST_M = 10.0
MEANINGFUL_HSR_THRESHOLD_M = 20.0
MAX_DAYS_WITHOUT_SPRINT = 3
MAX_DAYS_WITHOUT_HSR = 3
LOW_7DAY_SPRINT_DIST_M = 30.0
LOW_7DAY_HSR_M = 50.0
GAME_MIN_PRIOR = 3

# (column, display label, unit, flag enabled, flag mode)
FLAG_METRICS = [
    ("top_speed_ms", "Top Speed", "m/s", False, False),
    ("n_sprints", "Sprints", "", True, "rolling_pct"),
    ("sprint_distance_m", "Sprint Dist", "m", True, "zscore"),
    ("n_accelerations", "Accels", "#", True, "rolling_pct"),
    ("hsr_distance_m", "HSR", "m", True, "zscore"),
    ("total_distance_m", "Total Dist", "m", True, "zscore"),
    ("duration_min", "Duration", "min", False, False),
]

DEFAULT_FLAG_CRITERIA = {
    "review_acwr": ACWR_HIGH_RISK,
    "monitor_acwr": ACWR_ELEVATED,
    "optimal_low_acwr": ACWR_OPTIMAL_LOW,
    "review_z": REVIEW_Z,
    "monitor_z": MONITOR_Z,
    "rolling_review_z": ROLLING_REVIEW_Z,
    "rolling_monitor_z": ROLLING_MONITOR_Z,
    "rolling_window_days": ROLLING_WINDOW_DAYS,
    "rolling_min_sessions": ROLLING_MIN_SESSIONS,
    "rolling_min_delta_sprints": ROLLING_MIN_DELTA_SPRINTS,
    "rolling_min_delta_accels": ROLLING_MIN_DELTA_ACCELS,
    "meaningful_sprint_threshold": MEANINGFUL_SPRINT_THRESHOLD,
    "meaningful_sprint_dist_m": MEANINGFUL_SPRINT_DIST_M,
    "meaningful_hsr_m": MEANINGFUL_HSR_THRESHOLD_M,
    "max_days_without_sprint": MAX_DAYS_WITHOUT_SPRINT,
    "max_days_without_hsr": MAX_DAYS_WITHOUT_HSR,
    "low_7d_sprint_dist_m": LOW_7DAY_SPRINT_DIST_M,
    "low_7d_hsr_m": LOW_7DAY_HSR_M,
    "use_acwr": True,
    "use_gps_flags": True,
    "use_combined_load": True,
    "use_exposure_flags": True,
}

# Any of these are removed from practice totals to reduce obvious double-counting.
EXCLUDED_DRILL_NAMES = {
    "entire session",
    "lift",
    "lift 1",
    "lift 2",
    "sprint",
    "cages/isd",
    "cages",
    "isd",
}

TEAM_PATTERNS = [
    ("DSL", re.compile(r"\bdsl\b|dominican", re.I)),
    ("FCL", re.compile(r"\bfcl\b|florida complex", re.I)),
    ("Fredericksburg", re.compile(r"\bfreddy\b|\bfredericksburg\b|\bfred\b", re.I)),
    ("Wilmington", re.compile(r"\bwilmington\b", re.I)),
    ("Harrisburg", re.compile(r"\bharrisburg\b", re.I)),
    ("Rochester", re.compile(r"\brochester\b", re.I)),
    ("Rehab", re.compile(r"\brehab\b|rehabilitation", re.I)),
    ("Washington", re.compile(r"\bwashington\b|\bmlb\b|nationals", re.I)),
]

# Only these teams are available anywhere in the dashboard.
TEAM_ORDER = [
    "DSL", "FCL", "Fredericksburg", "Wilmington", "Harrisburg",
    "Rochester", "Washington", "Rehab",
]
ALLOWED_TEAMS = set(TEAM_ORDER)

STATUS_ORDER = {
    "Review": 0,
    "Monitor": 1,
    "Needs Exposure": 2,
    "Prepared": 3,
    "Data Check": 4,
}

# Nationals-ish palette
C_BG = "#F5F7FB"
C_WHITE = "#FFFFFF"
C_BORDER = "#DCE3EC"
C_RED = "#C8102E"
C_NAVY = "#11225A"
C_TEXT = "#172033"
C_MUTED = "#64748B"
C_GREEN = "#138A5B"
C_AMBER = "#D97706"
C_BLUE = "#2563EB"
C_PURPLE = "#7C3AED"
C_GRAY = "#8A94A6"

STATUS_COLORS = {
    "Review": C_RED,
    "Monitor": C_AMBER,
    "Needs Exposure": C_BLUE,
    "Prepared": C_GREEN,
    "Data Check": C_GRAY,
}

PRACTICE_NUMERIC = [
    "top_speed_ms", "max_accel_ms2", "n_sprints", "n_accelerations",
    "hsr_distance_m", "total_distance_m", "hmld_m", "sprint_distance_m",
    "mechanical_load", "duration_min",
]


# =============================================================================
# GLOBAL CACHE
# =============================================================================

_DATA_LOCK = threading.Lock()
DATA = {
    "raw_practice_source": pd.DataFrame(),
    "raw_practice": pd.DataFrame(),
    "raw_pp_sprint": pd.DataFrame(),
    "raw_pp_acwr": pd.DataFrame(),
    "acwr_daily": pd.DataFrame(),
    "practice_daily": pd.DataFrame(),
    "games_daily": pd.DataFrame(),
    "daily": pd.DataFrame(),
    "roster": pd.DataFrame(),
    "history_calendar": pd.DataFrame(),
    "loaded_at": None,
    "source": "Not loaded",
    "error": None,
}


# =============================================================================
# DATA HELPERS
# =============================================================================

def normalize_name(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "," in text:
        parts = [p.strip() for p in text.split(",", 1)]
        if len(parts) == 2 and all(parts):
            text = f"{parts[1]} {parts[0]}"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def clean_team(value) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in {"nan", "none"}:
        return ""
    key = text.casefold()
    lookup = {
        "rehab": "Rehab",
        "rehabilitation": "Rehab",
        "dsl": "DSL",
        "fcl": "FCL",
        "fredericksburg": "Fredericksburg",
        "wilmington": "Wilmington",
        "harrisburg": "Harrisburg",
        "rochester": "Rochester",
        "washington": "Washington",
    }
    return lookup.get(key, text)


def parse_team_from_session(session_name: str) -> str:
    text = str(session_name or "")
    for team, pattern in TEAM_PATTERNS:
        if pattern.search(text):
            return team
    first = re.split(r"[-–—|]", text)[0].strip()
    return clean_team(first)


def safe_num(series, fill=None):
    out = pd.to_numeric(series, errors="coerce")
    if fill is not None:
        out = out.fillna(fill)
    return out


def mode_or_last(series: pd.Series) -> str:
    vals = series.dropna().astype(str).str.strip()
    vals = vals[vals.ne("")]
    if vals.empty:
        return ""
    mode = vals.mode()
    return str(mode.iloc[0] if not mode.empty else vals.iloc[-1])


def _google_client(write: bool = False):
    scopes = (
        [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        if write
        else [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
    )

    # Preferred on Streamlit Cloud / local Streamlit development.
    try:
        secret_info = st.secrets["gcp_service_account"]
        info = dict(secret_info)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception:
        pass

    # Optional environment variable containing the entire service-account JSON.
    env_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if env_json:
        info = json.loads(env_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)

    # Local file fallback. Do not commit this file.
    if SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        return gspread.authorize(creds)

    raise RuntimeError(
        "Google credentials were not found. Add [gcp_service_account] to "
        ".streamlit/secrets.toml locally or Streamlit Community Cloud Secrets."
    )


def _read_google_tab(client, sheet_id: str, tab: str) -> pd.DataFrame:
    ws = client.open_by_key(sheet_id).worksheet(tab)
    return pd.DataFrame(ws.get_all_records())


def _read_google_tab_optional(client, sheet_id: str, tab: str) -> pd.DataFrame:
    try:
        return _read_google_tab(client, sheet_id, tab)
    except Exception:
        return pd.DataFrame()


def _read_local_excel(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_excel(path, sheet_name=sheet_name)


def _read_local_excel_optional(path: Path, sheet_name: str) -> pd.DataFrame:
    try:
        return _read_local_excel(path, sheet_name)
    except Exception:
        return pd.DataFrame()


def load_source_frames():
    """Return practice, roster, and PP_Sprint frames from Google, else local fallback."""
    google_error = None
    try:
        if not STATSPORTS_SHEET_ID or not PYTHON_REPORTS_SHEET_ID:
            raise RuntimeError(
                "Missing STATSPORTS_SHEET_ID or PYTHON_REPORTS_SHEET_ID in Streamlit secrets/environment."
            )
        client = _google_client()
        practice = _read_google_tab(client, STATSPORTS_SHEET_ID, STATSPORTS_TAB)
        roster = _read_google_tab(client, PYTHON_REPORTS_SHEET_ID, ROSTER_TAB)
        pp = _read_google_tab(client, PYTHON_REPORTS_SHEET_ID, PP_SPRINT_TAB)
        pp_acwr = _read_google_tab_optional(client, PYTHON_REPORTS_SHEET_ID, PP_ACWR_TAB)
        return practice, roster, pp, pp_acwr, "Google Sheets"
    except Exception as exc:
        google_error = exc

    if ALLOW_LOCAL_EXCEL_FALLBACK:
        try:
            practice = _read_local_excel(LOCAL_STATSPORTS_XLSX, STATSPORTS_TAB)
            roster = _read_local_excel(LOCAL_REPORTS_XLSX, ROSTER_TAB)
            pp = _read_local_excel(LOCAL_REPORTS_XLSX, PP_SPRINT_TAB)
            pp_acwr = _read_local_excel_optional(LOCAL_REPORTS_XLSX, PP_ACWR_TAB)
            return practice, roster, pp, pp_acwr, f"Local Excel fallback ({google_error})"
        except Exception as local_exc:
            raise RuntimeError(
                "Google Sheets load failed and local fallback also failed.\n"
                f"Google error: {google_error}\nLocal fallback error: {local_exc}"
            ) from local_exc

    raise RuntimeError(f"Google Sheets load failed: {google_error}")



# =============================================================================
# STATSports API SYNC
# =============================================================================

API_ROW_COLUMNS = [
    "date", "week", "week_start", "session_name", "player_name", "drill_name",
    "top_speed_ms", "max_accel_ms2", "n_sprints", "n_accelerations",
    "hsr_distance_m", "total_distance_m", "hmld_m", "sprint_distance_m",
    "mechanical_load", "duration_min",
]
API_KEY_COLUMNS = ["date", "session_name", "player_name", "drill_name"]


def _nested_secret(section_name: str, key: str, default=""):
    try:
        section = st.secrets.get(section_name, {})
        if hasattr(section, "get"):
            return section.get(key, default)
    except Exception:
        pass
    return default


def statsports_api_config() -> dict:
    """Read STATSports API configuration without exposing secrets in source code."""
    api_key = os.getenv(
        "STATSPORTS_API_KEY",
        str(_nested_secret("statsports_api", "api_key", _secret_value("STATSPORTS_API_KEY", "")) or ""),
    ).strip()
    third_party_id = os.getenv(
        "STATSPORTS_THIRD_PARTY_ID",
        str(
            _nested_secret(
                "statsports_api",
                "third_party_api_id",
                _secret_value("STATSPORTS_THIRD_PARTY_ID", ""),
            )
            or ""
        ),
    ).strip()
    base_url = os.getenv(
        "STATSPORTS_API_BASE_URL",
        str(
            _nested_secret(
                "statsports_api",
                "base_url",
                STATSPORTS_API_BASE_URL,
            )
            or STATSPORTS_API_BASE_URL
        ),
    ).strip()
    api_version = os.getenv(
        "STATSPORTS_API_VERSION",
        str(
            _nested_secret(
                "statsports_api",
                "api_version",
                STATSPORTS_API_VERSION,
            )
            or STATSPORTS_API_VERSION
        ),
    ).strip()

    # In the user's existing STATSports pull workflow, the third-party ID is the
    # same credential value as the API key. Preserve that behavior when only one
    # value is supplied, while still allowing separate secrets if needed later.
    if api_key and not third_party_id:
        third_party_id = api_key

    return {
        "api_key": api_key,
        "third_party_api_id": third_party_id,
        "base_url": base_url.rstrip("/"),
        "api_version": api_version,
    }


def statsports_api_is_configured() -> bool:
    cfg = statsports_api_config()
    return bool(cfg["api_key"] and cfg["third_party_api_id"])


def _statsports_http_session() -> tuple[requests.Session, dict]:
    cfg = statsports_api_config()
    if not cfg["api_key"] or not cfg["third_party_api_id"]:
        raise RuntimeError(
            "STATSports API credentials are not configured. Add [statsports_api] "
            "api_key and third_party_api_id to Streamlit Secrets."
        )

    http = requests.Session()
    http.headers.update(
        {
            "Internal": cfg["api_key"],
            "api-version": cfg["api_version"],
            "Content-Type": "application/json",
        }
    )
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    return http, cfg


def _api_post(http: requests.Session, cfg: dict, endpoint: str, payload: dict):
    url = f'{cfg["base_url"]}{endpoint}'
    try:
        response = http.post(url, json=payload, timeout=STATSPORTS_API_TIMEOUT)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {
                "error_type": "NON_JSON_RESPONSE",
                "detail": response.text[:750],
                "url": url,
            }
    except requests.exceptions.Timeout as exc:
        return 0, {"error_type": "TIMEOUT", "detail": repr(exc), "url": url}
    except requests.exceptions.SSLError as exc:
        return 0, {"error_type": "SSL_ERROR", "detail": repr(exc), "url": url}
    except requests.exceptions.ConnectionError as exc:
        return 0, {"error_type": "CONNECTION_ERROR", "detail": repr(exc), "url": url}
    except requests.exceptions.RequestException as exc:
        return 0, {"error_type": type(exc).__name__, "detail": repr(exc), "url": url}


def _api_player_name(session_player: dict) -> str:
    details = session_player.get("playerDetails") or {}
    for key in ["displayName", "name", "fullName"]:
        value = details.get(key)
        if value:
            return str(value).strip()
    first = details.get("firstName", "")
    last = details.get("lastName", "")
    if first or last:
        return f"{first} {last}".strip()
    return f"Player {session_player.get('id', '?')}"


def _api_kpi(drill: dict, field: str, cast=None):
    value = (drill.get("drillKpi") or {}).get(field)
    if cast is not None:
        try:
            return cast(value)
        except Exception:
            return np.nan
    try:
        number = float(value)
        return np.nan if math.isnan(number) else number
    except Exception:
        return np.nan


def pull_statsports_day(http: requests.Session, cfg: dict, day_value) -> tuple[list[dict], str | None]:
    """Pull one calendar day using the same endpoint/metrics as the existing pull app."""
    d = pd.Timestamp(day_value).date()
    d_str = d.isoformat()
    status, data = _api_post(
        http,
        cfg,
        "/thirdpartyapi/api/thirdPartyData/getFullSessionsByDateRange",
        {
            "thirdPartyApiId": cfg["third_party_api_id"],
            "sessionStartDate": f"{d_str}T00:00:00",
            "sessionEndDate": f"{d_str}T23:59:59",
        },
    )
    if status != 200:
        if isinstance(data, dict):
            error_type = data.get("error_type", "API_ERROR")
            detail = str(data.get("detail", data))[:750]
        else:
            error_type = "API_ERROR"
            detail = str(data)[:750]
        return [], f"{d_str}: HTTP {status} · {error_type} · {detail}"

    rows = []
    sessions = data if isinstance(data, list) else [data]
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_name = str(session.get("sessionName") or "")
        players = session.get("sessionPlayers") or session.get("players") or []
        for session_player in players:
            if not isinstance(session_player, dict):
                continue
            player_name_value = _api_player_name(session_player)
            for drill in session_player.get("drills") or []:
                if not isinstance(drill, dict):
                    continue
                drill_name = str(drill.get("drillName") or "")
                # Preserve the existing API-pull exclusion rule.
                if drill_name.lstrip().startswith("Birch -"):
                    continue
                rows.append(
                    {
                        "date": d_str,
                        "week": f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}",
                        "week_start": (d - timedelta(days=d.weekday())).isoformat(),
                        "session_name": session_name,
                        "player_name": player_name_value,
                        "drill_name": drill_name,
                        "top_speed_ms": _api_kpi(drill, "maxSpeed"),
                        "max_accel_ms2": _api_kpi(drill, "maxAcceleration"),
                        "n_sprints": _api_kpi(
                            drill,
                            "sprints",
                            lambda v: int(float(v)) if v is not None else np.nan,
                        ),
                        "n_accelerations": _api_kpi(
                            drill,
                            "accelerationsRel",
                            lambda v: int(float(v)) if v is not None else np.nan,
                        ),
                        "hsr_distance_m": _api_kpi(drill, "highSpeedRunningRel"),
                        "total_distance_m": _api_kpi(drill, "distanceTotal"),
                        "hmld_m": _api_kpi(drill, "hmld"),
                        "sprint_distance_m": _api_kpi(drill, "sprintDistance"),
                        "mechanical_load": _api_kpi(drill, "mechanicalLoad"),
                        "duration_min": _api_kpi(
                            drill,
                            "totalTime",
                            lambda v: round(float(v) / 60.0, 2) if v is not None else np.nan,
                        ),
                    }
                )
    return rows, None


def pull_statsports_range(start_value, end_value) -> tuple[pd.DataFrame, list[str]]:
    """Pull an inclusive date range from STATSports and return rows plus any day-level errors."""
    start = pd.Timestamp(start_value).date()
    end = pd.Timestamp(end_value).date()
    if end < start:
        raise ValueError("API pull end date must be on or after the start date.")

    http, cfg = _statsports_http_session()
    all_rows: list[dict] = []
    errors: list[str] = []
    current = start
    while current <= end:
        rows, error = pull_statsports_day(http, cfg, current)
        all_rows.extend(rows)
        if error:
            errors.append(error)
        current += timedelta(days=1)
        if current <= end:
            time.sleep(STATSPORTS_API_SLEEP_BETWEEN_DAYS)

    if not all_rows:
        return pd.DataFrame(columns=API_ROW_COLUMNS), errors

    df = pd.DataFrame(all_rows)
    for col in API_ROW_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan if col in PRACTICE_NUMERIC else ""
    df = df[API_ROW_COLUMNS]
    for col in PRACTICE_NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["date", "session_name", "player_name", "drill_name"]).reset_index(drop=True), errors


def _sheet_clean_value(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    return value


def _normalize_key_part(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip().casefold()


def _add_occurrence_key(df: pd.DataFrame) -> pd.DataFrame:
    """Mirror the existing pull app's safe date/session/player/drill + occurrence row identity."""
    out = df.copy()
    for col in API_KEY_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    base = out[API_KEY_COLUMNS].apply(
        lambda row: "||".join(_normalize_key_part(v) for v in row), axis=1
    )
    out["_base_upsert_key"] = base
    out["_occurrence"] = out.groupby("_base_upsert_key", sort=False).cumcount() + 1
    out["_upsert_key"] = out["_base_upsert_key"] + "||occ=" + out["_occurrence"].astype(str)
    return out


def sync_api_rows_to_google_sheet(api_df: pd.DataFrame) -> dict:
    """
    Append only API rows that are not already in Raw Sessions.

    Existing rows are intentionally not rewritten. This keeps the same safety
    behavior as the user's append-only STATSports pull app and preserves any
    manual/extra columns in the worksheet.
    """
    if api_df is None or api_df.empty:
        return {
            "pulled": 0,
            "appended": 0,
            "already_present": 0,
            "duplicate_existing": 0,
            "duplicate_api": 0,
        }
    if not STATSPORTS_SHEET_ID:
        raise RuntimeError("STATSPORTS_SHEET_ID is missing from Streamlit Secrets.")

    client = _google_client(write=True)
    spreadsheet = client.open_by_key(STATSPORTS_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(STATSPORTS_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=STATSPORTS_TAB, rows=10000, cols=30)

    values = ws.get_all_values()
    existing_headers = [str(v).strip() for v in values[0]] if values else []
    desired_headers = list(api_df.columns)

    if not existing_headers:
        sheet_headers = desired_headers
        end_cell = rowcol_to_a1(1, len(sheet_headers))
        ws.update([sheet_headers], range_name=f"A1:{end_cell}", value_input_option="USER_ENTERED")
        existing_df = pd.DataFrame(columns=sheet_headers)
    else:
        missing_headers = [c for c in desired_headers if c not in existing_headers]
        sheet_headers = existing_headers + missing_headers
        if missing_headers:
            end_cell = rowcol_to_a1(1, len(sheet_headers))
            ws.update([sheet_headers], range_name=f"A1:{end_cell}", value_input_option="USER_ENTERED")

        rows = values[1:] if len(values) > 1 else []
        width = len(existing_headers)
        padded = [(row + [""] * width)[:width] for row in rows]
        existing_df = pd.DataFrame(padded, columns=existing_headers)
        for col in sheet_headers:
            if col not in existing_df.columns:
                existing_df[col] = ""
        existing_df = existing_df[sheet_headers]

    api_out = api_df.copy()
    for col in api_out.select_dtypes(include="float").columns:
        api_out[col] = api_out[col].round(3)

    existing_keyed = _add_occurrence_key(existing_df)
    existing_keys = set(existing_keyed["_upsert_key"].dropna().astype(str))
    duplicate_existing = int(existing_keyed["_upsert_key"].duplicated().sum()) if not existing_keyed.empty else 0

    api_keyed = _add_occurrence_key(api_out)
    duplicate_api = int(api_keyed["_upsert_key"].duplicated().sum())
    missing = api_keyed[~api_keyed["_upsert_key"].isin(existing_keys)].copy()
    already_present = len(api_keyed) - len(missing)

    if not missing.empty:
        append_values = []
        for _, row in missing.iterrows():
            append_values.append([_sheet_clean_value(row.get(col, "")) for col in sheet_headers])
        for i in range(0, len(append_values), 1000):
            ws.append_rows(append_values[i:i + 1000], value_input_option="USER_ENTERED")
            if i + 1000 < len(append_values):
                time.sleep(0.5)

    return {
        "pulled": int(len(api_out)),
        "appended": int(len(missing)),
        "already_present": int(already_present),
        "duplicate_existing": duplicate_existing,
        "duplicate_api": duplicate_api,
    }


def latest_practice_date(bundle) -> pd.Timestamp | None:
    raw = bundle.get("raw_practice_source", pd.DataFrame())
    if raw is None or raw.empty:
        raw = bundle.get("raw_practice", pd.DataFrame())
    if raw is None or raw.empty or "date" not in raw.columns:
        return None
    values = pd.to_datetime(raw["date"], errors="coerce").dropna()
    return values.max().normalize() if not values.empty else None


def run_api_sync(start_value, end_value) -> tuple[str, bool]:
    """Pull from API, append missing rows, and return a UI message + success flag."""
    start = pd.Timestamp(start_value).date()
    end = pd.Timestamp(end_value).date()
    api_df, errors = pull_statsports_range(start, end)

    if api_df.empty:
        if errors:
            return "API pull failed for the requested range:\n" + "\n".join(errors[:5]), False
        return (
            f"API check complete for {start:%b %d, %Y}–{end:%b %d, %Y}. "
            "No STATSports rows were returned.",
            True,
        )

    result = sync_api_rows_to_google_sheet(api_df)
    message = (
        f"STATSports API sync complete for {start:%b %d, %Y}–{end:%b %d, %Y}: "
        f"{result['pulled']:,} rows pulled, {result['appended']:,} appended, "
        f"{result['already_present']:,} already present."
    )
    if errors:
        message += f" {len(errors)} day(s) returned API errors."
    if result["duplicate_existing"] or result["duplicate_api"]:
        message += (
            f" Duplicate-key warning: sheet={result['duplicate_existing']}, "
            f"pull={result['duplicate_api']}."
        )
    return message, True


def clean_roster(roster: pd.DataFrame) -> pd.DataFrame:
    if roster is None or roster.empty:
        return pd.DataFrame(columns=["player_key", "player_name", "roster_team", "position", "is_pitcher"])

    r = roster.copy()
    rename = {}
    for c in r.columns:
        lc = str(c).strip().casefold()
        if lc == "athlete":
            rename[c] = "player_name"
        elif lc == "team":
            rename[c] = "roster_team"
        elif lc == "position":
            rename[c] = "position"
    r = r.rename(columns=rename)

    for c in ["player_name", "roster_team", "position"]:
        if c not in r.columns:
            r[c] = ""

    r["player_name"] = r["player_name"].astype(str).str.strip()
    r = r[r["player_name"].ne("")].copy()
    r["player_key"] = r["player_name"].apply(normalize_name)
    r["roster_team"] = r["roster_team"].apply(clean_team)
    r["position"] = r["position"].astype(str).str.strip()

    # A player can appear more than once in Master Roster. The prior version kept
    # only the final duplicate row, which could erase a P designation when a later
    # historical/administrative row had a blank Position. Preserve a pitcher flag
    # if ANY roster row for that normalized player is labeled P/Pitcher.
    r["_is_pitcher"] = r["position"].apply(is_pitcher_position)
    pitcher_by_key = r.groupby("player_key")["_is_pitcher"].any().to_dict()

    def last_nonblank(series):
        vals = series.dropna().astype(str).str.strip()
        vals = vals[~vals.str.casefold().isin({"", "nan", "none"})]
        return vals.iloc[-1] if not vals.empty else ""

    r = (
        r.groupby("player_key", as_index=False)
         .agg(
             player_name=("player_name", last_nonblank),
             roster_team=("roster_team", last_nonblank),
             position=("position", last_nonblank),
         )
    )
    r["is_pitcher"] = r["player_key"].map(pitcher_by_key).fillna(False).astype(bool)
    return r[["player_key", "player_name", "roster_team", "position", "is_pitcher"]]


def clean_practice(raw: pd.DataFrame, roster: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = ["date", "session_name", "player_name", "drill_name"]
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"STATSports tab '{STATSPORTS_TAB}' is missing columns: {missing}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"]).copy()
    for c in ["session_name", "player_name", "drill_name"]:
        df[c] = df[c].astype(str).str.strip()
    df = df[df["player_name"].ne("")].copy()
    df["player_key"] = df["player_name"].apply(normalize_name)

    # Exclude pitchers before any practice aggregation so they cannot leak into
    # team totals, status calculations, charts, player selectors, or PDFs.
    if not roster.empty:
        if "is_pitcher" in roster.columns:
            pitcher_keys = set(roster.loc[roster["is_pitcher"], "player_key"].dropna())
        else:
            pitcher_keys = set(
                roster.loc[roster["position"].apply(is_pitcher_position), "player_key"].dropna()
            )
        if pitcher_keys:
            df = df[~df["player_key"].isin(pitcher_keys)].copy()

    for c in PRACTICE_NUMERIC:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = safe_num(df[c])

    drill_clean = df["drill_name"].str.casefold().str.strip()
    excluded = drill_clean.isin(EXCLUDED_DRILL_NAMES) | drill_clean.str.contains(
        r"\bcages\b|\bisd\b", regex=True, na=False
    )
    df = df[~excluded].copy()

    df["practice_team"] = df["session_name"].apply(parse_team_from_session)

    roster_map_team = roster.set_index("player_key")["roster_team"].to_dict() if not roster.empty else {}
    roster_map_pos = roster.set_index("player_key")["position"].to_dict() if not roster.empty else {}
    df["practice_team"] = df.apply(
        lambda r: r["practice_team"] or roster_map_team.get(r["player_key"], ""), axis=1
    )
    df["position"] = df["player_key"].map(roster_map_pos).fillna("")

    agg_map = {
        "session_name": "nunique",
        "drill_name": "nunique",
        "practice_team": mode_or_last,
        "player_name": mode_or_last,
        "position": mode_or_last,
        "top_speed_ms": "max",
        "max_accel_ms2": "max",
        "n_sprints": "sum",
        "n_accelerations": "sum",
        "hsr_distance_m": "sum",
        "total_distance_m": "sum",
        "hmld_m": "sum",
        "sprint_distance_m": "sum",
        "mechanical_load": "sum",
        "duration_min": "sum",
    }
    daily = (
        df.groupby(["player_key", "date"], as_index=False)
          .agg(agg_map)
          .rename(columns={"session_name": "practice_sessions", "drill_name": "practice_drills"})
    )
    daily["practice_observed"] = 1
    return df, daily


def clean_games(pp: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    if pp is None or pp.empty:
        return pd.DataFrame(columns=[
            "player_key", "date", "game_team", "game_days", "game_max_effort_runs",
            "game_max_effort_distance_yards", "game_distance_yards", "game_observed"
        ])

    df = pp.copy()
    df.columns = [str(c).strip() for c in df.columns]
    name_col = "batter" if "batter" in df.columns else "player_name"
    date_col = "game_date" if "game_date" in df.columns else "date"
    if name_col not in df.columns or date_col not in df.columns:
        return pd.DataFrame()

    df["player_name"] = df[name_col].astype(str).str.strip()
    df["player_key"] = df["player_name"].apply(normalize_name)
    df["date"] = pd.to_datetime(df[date_col], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date"])
    df = df[df["player_key"].ne("")].copy()

    # Exclude pitchers from PP_Sprint/game data too.
    if not roster.empty:
        if "is_pitcher" in roster.columns:
            pitcher_keys = set(roster.loc[roster["is_pitcher"], "player_key"].dropna())
        else:
            pitcher_keys = set(
                roster.loc[roster["position"].apply(is_pitcher_position), "player_key"].dropna()
            )
        if pitcher_keys:
            df = df[~df["player_key"].isin(pitcher_keys)].copy()

    metric_map = {
        "max_effort_runs": "game_max_effort_runs",
        "max_effort_distance_covered_yards": "game_max_effort_distance_yards",
        "distance_covered_yards": "game_distance_yards",
    }
    for src, dst in metric_map.items():
        df[dst] = safe_num(df[src], fill=0) if src in df.columns else 0.0

    if "Team" in df.columns:
        df["game_team"] = df["Team"].apply(clean_team)
    elif "team" in df.columns:
        df["game_team"] = df["team"].apply(clean_team)
    else:
        df["game_team"] = ""

    roster_map_team = roster.set_index("player_key")["roster_team"].to_dict() if not roster.empty else {}
    df["game_team"] = df.apply(
        lambda r: r["game_team"] or roster_map_team.get(r["player_key"], ""), axis=1
    )

    game_id_col = "mlbam_game_pk" if "mlbam_game_pk" in df.columns else None
    agg_map = {
        "player_name": mode_or_last,
        "game_team": mode_or_last,
        "game_max_effort_runs": "sum",
        "game_max_effort_distance_yards": "sum",
        "game_distance_yards": "sum",
    }
    if game_id_col:
        agg_map[game_id_col] = "nunique"

    out = df.groupby(["player_key", "date"], as_index=False).agg(agg_map)
    if game_id_col:
        out = out.rename(columns={game_id_col: "game_days"})
    else:
        out["game_days"] = 1
    out["game_observed"] = 1
    return out




def clean_acwr(pp_acwr: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    """Normalize PP_ACWR into one authoritative ACWR observation per player/date."""
    cols = ["player_key", "date", "acwr", "acwr_team", "acwr_observed"]
    if pp_acwr is None or pp_acwr.empty:
        return pd.DataFrame(columns=cols)

    df = pp_acwr.copy()
    df.columns = [str(c).strip().casefold() for c in df.columns]
    if "batter" not in df.columns or "ewma_acwr_7_28" not in df.columns:
        return pd.DataFrame(columns=cols)

    df["player_key"] = df["batter"].fillna("").astype(str).apply(normalize_name)
    df["acwr"] = pd.to_numeric(df["ewma_acwr_7_28"], errors="coerce")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    else:
        df["date"] = pd.NaT

    if "team" in df.columns:
        df["acwr_team"] = df["team"].fillna("").astype(str).apply(clean_team)
    else:
        df["acwr_team"] = ""

    df = df[df["player_key"].ne("") & df["acwr"].notna() & df["date"].notna()].copy()

    if not roster.empty:
        if "is_pitcher" in roster.columns:
            pitcher_keys = set(roster.loc[roster["is_pitcher"], "player_key"].dropna())
        else:
            pitcher_keys = set(
                roster.loc[roster["position"].apply(is_pitcher_position), "player_key"].dropna()
            )
        if pitcher_keys:
            df = df[~df["player_key"].isin(pitcher_keys)].copy()

    if df.empty:
        return pd.DataFrame(columns=cols)

    df["_row_order"] = np.arange(len(df))
    df = (
        df.sort_values(["player_key", "date", "_row_order"])
          .groupby(["player_key", "date"], as_index=False)
          .tail(1)
          .sort_values(["player_key", "date"])
          .reset_index(drop=True)
    )
    df["acwr_observed"] = 1
    return df[cols]


def attach_acwr_values(daily: pd.DataFrame, acwr_daily: pd.DataFrame) -> pd.DataFrame:
    """Attach PP_ACWR values without deriving or substituting ACWR from PP_Sprint."""
    keys = ["player_key", "date"]
    d = daily.copy() if isinstance(daily, pd.DataFrame) else pd.DataFrame()
    a = acwr_daily.copy() if isinstance(acwr_daily, pd.DataFrame) else pd.DataFrame()

    if d.empty and a.empty:
        return pd.DataFrame()
    if d.empty:
        d = pd.DataFrame(columns=keys)
    if a.empty:
        d["acwr"] = np.nan
        d["acwr_observed"] = 0
        return d

    keep = [c for c in ["player_key", "date", "acwr", "acwr_team", "acwr_observed"] if c in a.columns]
    out = d.merge(a[keep], on=keys, how="outer")
    if "acwr" not in out.columns:
        out["acwr"] = np.nan
    out["acwr"] = pd.to_numeric(out["acwr"], errors="coerce")
    if "acwr_observed" not in out.columns:
        out["acwr_observed"] = 0
    out["acwr_observed"] = pd.to_numeric(out["acwr_observed"], errors="coerce").fillna(0)
    return out.sort_values(keys).reset_index(drop=True)


def add_game_flag_context(games_daily: pd.DataFrame) -> pd.DataFrame:
    """Add standalone-report game-load classes.

    Game load is classified against each athlete's OWN expanding prior-game
    baseline. ACWR is NOT sourced from PP_Sprint here; PP_ACWR is authoritative.
    The legacy game_acwr column is retained only for backward compatibility and
    is not used by dashboard flags, charts, snapshots, or reports.
    """
    if games_daily is None or games_daily.empty:
        out = games_daily.copy() if isinstance(games_daily, pd.DataFrame) else pd.DataFrame()
        if "game_load_class" not in out.columns:
            out["game_load_class"] = pd.Series(dtype=object)
        if "game_acwr" not in out.columns:
            out["game_acwr"] = pd.Series(dtype=float)
        return out

    out = games_daily.copy().sort_values(["player_key", "date"]).reset_index(drop=True)
    out["game_load_class"] = "—"
    out["game_acwr"] = np.nan

    for key, idx in out.groupby("player_key", sort=False).groups.items():
        idx = list(idx)
        grp = out.loc[idx].sort_values("date")

        runs = safe_num(grp["game_max_effort_runs"])
        dist = safe_num(grp["game_max_effort_distance_yards"])
        prior_run_mean = runs.shift(1).expanding().mean()
        prior_run_sd = runs.shift(1).expanding().std().replace(0, np.nan)
        prior_dist_mean = dist.shift(1).expanding().mean()
        prior_dist_sd = dist.shift(1).expanding().std().replace(0, np.nan)

        acute = None
        chronic = None
        for prior_n, row_idx in enumerate(grp.index):
            r = runs.loc[row_idx]
            d = dist.loc[row_idx]

            if prior_n >= GAME_MIN_PRIOR:
                high_run = (
                    pd.notna(r) and pd.notna(prior_run_sd.loc[row_idx])
                    and r >= prior_run_mean.loc[row_idx] + prior_run_sd.loc[row_idx]
                )
                high_dist = (
                    pd.notna(d) and pd.notna(prior_dist_sd.loc[row_idx])
                    and d >= prior_dist_mean.loc[row_idx] + prior_dist_sd.loc[row_idx]
                )
                low_run = (
                    pd.isna(r) or pd.isna(prior_run_sd.loc[row_idx])
                    or r <= prior_run_mean.loc[row_idx] - prior_run_sd.loc[row_idx]
                )
                low_dist = (
                    pd.isna(d) or pd.isna(prior_dist_sd.loc[row_idx])
                    or d <= prior_dist_mean.loc[row_idx] - prior_dist_sd.loc[row_idx]
                )
                if high_run or high_dist:
                    out.at[row_idx, "game_load_class"] = "High"
                elif low_run and low_dist:
                    out.at[row_idx, "game_load_class"] = "Low"
                else:
                    out.at[row_idx, "game_load_class"] = "Moderate"

            load = float(d) if pd.notna(d) else 0.0
            if acute is None:
                acute = chronic = load
            else:
                acute = load * LAMBDA_ACUTE + acute * (1 - LAMBDA_ACUTE)
                chronic = load * LAMBDA_CHRONIC + chronic * (1 - LAMBDA_CHRONIC)
            out.at[row_idx, "game_acwr"] = acute / chronic if chronic and chronic > 0 else np.nan

    return out.sort_values(["player_key", "date"]).reset_index(drop=True)


def combine_daily(practice_daily: pd.DataFrame, games_daily: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    keys = ["player_key", "date"]
    p = practice_daily.copy()
    g = games_daily.copy()
    if p.empty and g.empty:
        return pd.DataFrame()
    if p.empty:
        p = pd.DataFrame(columns=keys)
    if g.empty:
        g = pd.DataFrame(columns=keys)
    d = p.merge(g, on=keys, how="outer", suffixes=("_practice", "_game"))

    # Reconcile display name.
    name_cols = [c for c in ["player_name_practice", "player_name_game", "player_name"] if c in d.columns]
    if name_cols:
        d["player_name"] = ""
        for c in name_cols:
            vals = d[c].fillna("").astype(str).str.strip()
            d["player_name"] = d["player_name"].where(d["player_name"].ne(""), vals)
    else:
        d["player_name"] = ""

    roster_name = roster.set_index("player_key")["player_name"].to_dict() if not roster.empty else {}
    roster_team = roster.set_index("player_key")["roster_team"].to_dict() if not roster.empty else {}
    roster_pos = roster.set_index("player_key")["position"].to_dict() if not roster.empty else {}

    d["player_name"] = d.apply(
        lambda r: r["player_name"] or roster_name.get(r["player_key"], r["player_key"]), axis=1
    )
    d["position"] = d.get("position", pd.Series("", index=d.index)).fillna("").astype(str)
    d["position"] = d.apply(
        lambda r: r["position"] or roster_pos.get(r["player_key"], ""), axis=1
    )

    practice_team = d.get("practice_team", pd.Series("", index=d.index)).fillna("").astype(str)
    game_team = d.get("game_team", pd.Series("", index=d.index)).fillna("").astype(str)
    d["team"] = practice_team.where(practice_team.ne(""), game_team)
    d["team"] = d.apply(
        lambda r: r["team"] or roster_team.get(r["player_key"], ""), axis=1
    )
    d["team"] = d["team"].apply(clean_team)

    zero_cols = [
        "practice_sessions", "practice_drills", "n_sprints", "n_accelerations",
        "hsr_distance_m", "total_distance_m", "hmld_m", "sprint_distance_m",
        "mechanical_load", "duration_min", "practice_observed", "game_days",
        "game_max_effort_runs", "game_max_effort_distance_yards", "game_distance_yards",
        "game_observed",
    ]
    for c in zero_cols:
        if c not in d.columns:
            d[c] = 0.0
        d[c] = safe_num(d[c], fill=0)

    for c in ["top_speed_ms", "max_accel_ms2"]:
        if c not in d.columns:
            d[c] = np.nan
        d[c] = safe_num(d[c])

    d["game_distance_m"] = d["game_distance_yards"] * 0.9144
    d["game_max_effort_distance_m"] = d["game_max_effort_distance_yards"] * 0.9144
    d["combined_total_distance_m"] = d["total_distance_m"] + d["game_distance_m"]
    d["combined_high_intensity_m"] = d["hsr_distance_m"] + d["game_max_effort_distance_m"]
    d["activity_observed"] = ((d["practice_observed"] > 0) | (d["game_observed"] > 0)).astype(int)

    # Final defensive exclusion in case a pitcher row arrives through a source
    # path that bypassed the earlier practice/game filters.
    if not roster.empty:
        if "is_pitcher" in roster.columns:
            pitcher_keys = set(roster.loc[roster["is_pitcher"], "player_key"].dropna())
        else:
            pitcher_keys = set(
                roster.loc[roster["position"].apply(is_pitcher_position), "player_key"].dropna()
            )
        if pitcher_keys:
            d = d[~d["player_key"].isin(pitcher_keys)].copy()

    # Flag calculations are intentionally deferred to compute_flag_snapshot(),
    # which reproduces the standalone report's Team + Position grouping,
    # rolling baselines, minimum deltas, and status priority order.
    return d.sort_values(["player_key", "date"]).reset_index(drop=True)


def build_history_calendar(daily: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    """Expand athletes to calendar days for charting while preserving PP_ACWR observations.

    PP_ACWR is the authoritative ACWR source. Calendar rows carry the most recent
    PP_ACWR value forward for snapshot context; ACWR trend charts filter back to
    dates that actually contain a PP_ACWR observation.
    """
    if daily.empty:
        return pd.DataFrame()

    min_date = daily["date"].min().normalize()
    max_date = daily["date"].max().normalize()
    calendar = pd.date_range(min_date, max_date, freq="D")
    pieces = []

    roster_name = roster.set_index("player_key")["player_name"].to_dict() if not roster.empty else {}
    roster_team = roster.set_index("player_key")["roster_team"].to_dict() if not roster.empty else {}
    roster_pos = roster.set_index("player_key")["position"].to_dict() if not roster.empty else {}

    for key, grp in daily.groupby("player_key"):
        base = pd.DataFrame({"date": calendar})
        base["player_key"] = key
        merged = base.merge(grp, on=["player_key", "date"], how="left")

        merged["player_name"] = merged.get("player_name", pd.Series("", index=merged.index)).fillna("").astype(str)
        if merged["player_name"].replace("", np.nan).notna().any():
            display = merged.loc[merged["player_name"].ne(""), "player_name"].iloc[-1]
        else:
            display = roster_name.get(key, key)
        merged["player_name"] = display

        for c, lookup in [("team", roster_team), ("position", roster_pos)]:
            if c not in merged.columns:
                merged[c] = ""
            merged[c] = merged[c].fillna("").astype(str)
            merged[c] = merged[c].replace("", np.nan).ffill().bfill().fillna(lookup.get(key, ""))

        fill_zero = [
            "practice_sessions", "practice_drills", "n_sprints", "n_accelerations",
            "hsr_distance_m", "total_distance_m", "hmld_m", "sprint_distance_m",
            "mechanical_load", "duration_min", "practice_observed", "game_days",
            "game_max_effort_runs", "game_max_effort_distance_yards", "game_distance_yards",
            "game_distance_m", "game_max_effort_distance_m", "combined_total_distance_m",
            "combined_high_intensity_m", "game_observed", "activity_observed",
            "acwr_observed",
        ]
        for c in fill_zero:
            if c not in merged.columns:
                merged[c] = 0.0
            merged[c] = safe_num(merged[c], fill=0)

        for c in ["top_speed_ms", "max_accel_ms2", "game_acwr", "acwr"]:
            if c not in merged.columns:
                merged[c] = np.nan
            merged[c] = safe_num(merged[c])

        if "game_load_class" not in merged.columns:
            merged["game_load_class"] = ""
        merged["game_load_class"] = merged["game_load_class"].fillna("").astype(str)

        # Carry the most recent sheet value for snapshot context only. Trend
        # plotting filters back to acwr_observed rows, so this does not invent
        # additional PP_ACWR observations.
        merged["acwr"] = merged["acwr"].ffill()
        pieces.append(merged)

    out = pd.concat(pieces, ignore_index=True)
    return out.sort_values(["player_key", "date"]).reset_index(drop=True)



def refresh_data():
    """Reload all sources and rebuild derived data. Returns a human-readable status string."""
    try:
        practice_raw, roster_raw, pp_raw, pp_acwr_raw, source = load_source_frames()
        roster = clean_roster(roster_raw)
        raw_practice, practice_daily = clean_practice(practice_raw, roster)
        games_daily = add_game_flag_context(clean_games(pp_raw, roster))
        acwr_daily = clean_acwr(pp_acwr_raw, roster)
        daily = attach_acwr_values(combine_daily(practice_daily, games_daily, roster), acwr_daily)
        history = build_history_calendar(daily, roster)

        loaded_at = datetime.now()
        with _DATA_LOCK:
            DATA.update({
                "raw_practice_source": practice_raw,
                "raw_practice": raw_practice,
                "raw_pp_sprint": pp_raw,
                "raw_pp_acwr": pp_acwr_raw,
                "acwr_daily": acwr_daily,
                "practice_daily": practice_daily,
                "games_daily": games_daily,
                "daily": daily,
                "roster": roster,
                "history_calendar": history,
                "loaded_at": loaded_at,
                "source": source,
                "error": None,
            })
        return f"Loaded {len(daily):,} player-days from {source} · {loaded_at.strftime('%b %d, %Y %I:%M %p')}"
    except Exception as exc:
        with _DATA_LOCK:
            DATA["error"] = str(exc)
        return f"Refresh failed: {exc}"


def snapshot_data():
    with _DATA_LOCK:
        return {k: (v.copy() if isinstance(v, pd.DataFrame) else v) for k, v in DATA.items()}


def available_date_bounds(bundle=None):
    bundle = bundle or snapshot_data()
    d = bundle["daily"]
    if d.empty:
        today = pd.Timestamp.today().normalize()
        return today, today
    return d["date"].min().normalize(), d["date"].max().normalize()


def ordered_teams(values) -> list[str]:
    vals = [clean_team(v) for v in values if clean_team(v)]
    vals = [v for v in vals if v in ALLOWED_TEAMS]
    vals = sorted(set(vals), key=lambda x: TEAM_ORDER.index(x))
    return vals


def is_pitcher_position(value) -> bool:
    """Return True for roster positions labeled P or Pitcher.

    Token-based matching also excludes combined labels such as "P/1B" while
    avoiding accidental matches inside unrelated position names.
    """
    text = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()
    tokens = set(text.split())
    return "P" in tokens or "PITCHER" in tokens


def eligible_player_keys(bundle, start_date, end_date, teams, selected_keys=None):
    daily = bundle["daily"].copy()
    roster = bundle["roster"].copy()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    teams = set(teams or [])

    within = daily[daily["date"].between(start, end)].copy()
    if teams:
        within = within[within["team"].isin(teams)]

    keys = set(within["player_key"].dropna())
    # Include current roster players for selected teams so no-data athletes can still appear.
    if teams and not roster.empty:
        keys |= set(roster.loc[roster["roster_team"].isin(teams), "player_key"].dropna())

    # Position-player dashboard: exclude pitchers using the preserved roster flag.
    # Also inspect the daily Position field as a fallback for unmatched roster rows.
    pitcher_keys = set()
    if not roster.empty:
        if "is_pitcher" in roster.columns:
            pitcher_keys |= set(roster.loc[roster["is_pitcher"], "player_key"].dropna())
        elif "position" in roster.columns:
            pitcher_keys |= set(
                roster.loc[roster["position"].apply(is_pitcher_position), "player_key"].dropna()
            )
    if "position" in within.columns:
        pitcher_keys |= set(
            within.loc[within["position"].apply(is_pitcher_position), "player_key"].dropna()
        )
    keys -= pitcher_keys

    if selected_keys:
        keys &= set(selected_keys)
    return sorted(keys)


def player_display_map(bundle):
    mapping = {}
    r = bundle["roster"]
    d = bundle["daily"]
    if not r.empty:
        mapping.update(r.set_index("player_key")["player_name"].to_dict())
    if not d.empty:
        for key, grp in d.groupby("player_key"):
            vals = grp["player_name"].dropna().astype(str).str.strip()
            vals = vals[vals.ne("")]
            if not vals.empty:
                mapping.setdefault(key, vals.iloc[-1])
    return mapping


# =============================================================================
# STATUS + SUMMARY LOGIC — integrated standalone report flag engine
# =============================================================================

def _last_game_row(history: pd.DataFrame, end: pd.Timestamp):
    if history is None or history.empty:
        return None
    rows = history[(history["date"] <= end) & (safe_num(history.get("game_observed", pd.Series(0, index=history.index)), 0) > 0)].sort_values("date")
    return rows.iloc[-1] if not rows.empty else None


def classify_combined_load(practice_level, game_load_class):
    """Exact combined-load matrix from the standalone GPS workload report."""
    gl = game_load_class if game_load_class in ("High", "Moderate", "Low") else "Low"
    if practice_level == "High" and gl == "High":
        return "Major Load Concern"
    if practice_level == "High":
        return "Practice-Driven Spike"
    if practice_level == "Moderate" and gl == "High":
        return "Game-Driven Load"
    if practice_level == "Moderate":
        return "Normal / Monitor"
    if practice_level == "Low" and gl == "High":
        return "Game-Driven Load"
    if practice_level == "Low" and gl == "Moderate":
        return "Normal / Monitor"
    return "Possible Underload"


def _last_nonblank(series, default=""):
    if series is None:
        return default
    vals = pd.Series(series).dropna().astype(str).str.strip()
    vals = vals[vals.ne("")]
    return vals.iloc[-1] if not vals.empty else default


def compute_flag_snapshot(bundle, end_date, player_keys, criteria=None) -> pd.DataFrame:
    """Reproduce gps_flags.compute_athlete_windows() inside the Streamlit app.

    This keeps the dashboard self-contained for GitHub/Streamlit Cloud while
    preserving the standalone report's actual flag math:
      * Team + Position same-day z-scores for Sprint Dist, HSR, Total Dist.
      * 14-day Team + Position rolling baselines for Sprints/Accelerations.
      * Positive spikes only; rolling minimum absolute deltas still apply.
      * Sprint/HSR exposure combines practice GPS and PP_Sprint max-effort runs.
      * Roster athletes with no GPS on the end date remain visible as Data Check.
    """
    criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
    end = pd.Timestamp(end_date).normalize()
    cut7 = end - pd.Timedelta(days=7)
    cut28 = end - pd.Timedelta(days=28)
    cutrolling = end - pd.Timedelta(days=int(criteria["rolling_window_days"]))

    practice = bundle.get("practice_daily", pd.DataFrame()).copy()
    games = bundle.get("games_daily", pd.DataFrame()).copy()
    acwr_daily = bundle.get("acwr_daily", pd.DataFrame()).copy()
    roster = bundle.get("roster", pd.DataFrame()).copy()
    display = player_display_map(bundle)

    if not practice.empty:
        practice["date"] = pd.to_datetime(practice["date"], errors="coerce").dt.normalize()
    if not games.empty:
        games["date"] = pd.to_datetime(games["date"], errors="coerce").dt.normalize()
    if not acwr_daily.empty:
        acwr_daily["date"] = pd.to_datetime(acwr_daily["date"], errors="coerce").dt.normalize()

    roster_team = roster.set_index("player_key")["roster_team"].to_dict() if not roster.empty else {}
    roster_pos = roster.set_index("player_key")["position"].to_dict() if not roster.empty else {}

    metric_cols = [m[0] for m in FLAG_METRICS]
    records = []

    for key in player_keys:
        p_all = practice[(practice["player_key"] == key) & (practice["date"] <= end)].sort_values("date") if not practice.empty else pd.DataFrame()
        p_today = p_all[p_all["date"] == end] if not p_all.empty else pd.DataFrame()
        p_prior = p_all[p_all["date"] < end] if not p_all.empty else pd.DataFrame()
        p_prior7 = p_prior[p_prior["date"] >= cut7] if not p_prior.empty else pd.DataFrame()
        p_prior28 = p_prior[p_prior["date"] >= cut28] if not p_prior.empty else pd.DataFrame()
        prow = p_today.iloc[-1] if not p_today.empty else None

        g_all = games[(games["player_key"] == key) & (games["date"] <= end)].sort_values("date") if not games.empty else pd.DataFrame()
        grow = g_all.iloc[-1] if not g_all.empty else None
        a_all = acwr_daily[(acwr_daily["player_key"] == key) & (acwr_daily["date"] <= end)].sort_values("date") if not acwr_daily.empty else pd.DataFrame()
        arow = a_all.iloc[-1] if not a_all.empty else None

        team = str(prow.get("practice_team", "") if prow is not None else "").strip()
        if not team and not p_all.empty:
            team = _last_nonblank(p_all.get("practice_team", pd.Series(dtype=str)))
        if not team and grow is not None:
            team = str(grow.get("game_team", "") or "").strip()
        team = clean_team(team or roster_team.get(key, ""))

        pos = str(prow.get("position", "") if prow is not None else "").strip()
        if not pos and not p_all.empty:
            pos = _last_nonblank(p_all.get("position", pd.Series(dtype=str)))
        pos = pos or roster_pos.get(key, "")

        rec = {
            "Player Key": key,
            "Athlete": display.get(key, key),
            "Team": team,
            "Pos": pos,
            "has_gps": prow is not None,
            "flag_count": 0,
        }

        for col, short, unit, *_ in FLAG_METRICS:
            val = pd.to_numeric(prow.get(col), errors="coerce") if prow is not None else np.nan
            avg7 = safe_num(p_prior7[col]).mean() if (not p_prior7.empty and col in p_prior7.columns) else np.nan
            avg28 = safe_num(p_prior28[col]).mean() if (not p_prior28.empty and col in p_prior28.columns) else np.nan
            rec[f"{col}_val"] = round(float(val), 1) if pd.notna(val) else np.nan
            rec[f"{col}_7d"] = round(float(avg7), 1) if pd.notna(avg7) else np.nan
            rec[f"{col}_28d"] = round(float(avg28), 1) if pd.notna(avg28) else np.nan
            rec[f"{col}_flag"] = None
            rec[f"{col}_z"] = np.nan

        # Game context as-of the selected date.
        rec["game_load_class"] = str(grow.get("game_load_class", "—")) if grow is not None else "—"
        rec["acwr"] = pd.to_numeric(arow.get("acwr"), errors="coerce") if arow is not None else np.nan
        rec["last_game_date"] = grow.get("date") if grow is not None else pd.NaT
        rec["last_game_runs"] = float(pd.to_numeric(grow.get("game_max_effort_runs", 0), errors="coerce") or 0) if grow is not None else 0.0
        rec["last_game_dist_yards"] = float(pd.to_numeric(grow.get("game_max_effort_distance_yards", 0), errors="coerce") or 0) if grow is not None else 0.0

        # Practice exposure history.
        sprint_count_thr = float(criteria["meaningful_sprint_threshold"])
        sprint_dist_thr = float(criteria["meaningful_sprint_dist_m"])
        hsr_thr = float(criteria["meaningful_hsr_m"])

        today_sprints = pd.to_numeric(prow.get("n_sprints"), errors="coerce") if prow is not None else np.nan
        today_sdist = pd.to_numeric(prow.get("sprint_distance_m"), errors="coerce") if prow is not None else np.nan
        today_hsr = pd.to_numeric(prow.get("hsr_distance_m"), errors="coerce") if prow is not None else np.nan
        today_has_sprint_gps = (
            (pd.notna(today_sprints) and today_sprints >= sprint_count_thr)
            or (pd.notna(today_sdist) and today_sdist >= sprint_dist_thr)
        )
        today_has_hsr_gps = pd.notna(today_hsr) and today_hsr >= hsr_thr

        g_dates = set()
        g_dist_by_date = {}
        g_hsr_by_date = {}
        if not g_all.empty:
            for gd, ggrp in g_all.groupby("date"):
                gd = pd.Timestamp(gd).normalize()
                runs = float(safe_num(ggrp["game_max_effort_runs"], 0).sum())
                dist_m = float(safe_num(ggrp["game_max_effort_distance_yards"], 0).sum()) * 0.9144
                if runs >= 1:
                    g_dates.add(gd)
                g_dist_by_date[gd] = dist_m
                g_hsr_by_date[gd] = dist_m

        today_game_sprint = end in g_dates
        today_game_dist = g_dist_by_date.get(end, 0.0)
        today_game_hsr = g_hsr_by_date.get(end, 0.0)
        today_has_sprint = today_has_sprint_gps or today_game_sprint
        today_has_hsr = today_has_hsr_gps or today_game_hsr >= hsr_thr

        if today_has_sprint:
            rec["days_since_sprint"] = 0
            rec["last_sprint_date"] = end
        else:
            gps_dates = set()
            if not p_prior.empty:
                pc = safe_num(p_prior.get("n_sprints", pd.Series(0, index=p_prior.index)), 0)
                psd = safe_num(p_prior.get("sprint_distance_m", pd.Series(0, index=p_prior.index)), 0)
                gps_dates = set(p_prior.loc[(pc >= sprint_count_thr) | (psd >= sprint_dist_thr), "date"].dt.normalize())
            all_dates = gps_dates | {d for d in g_dates if d < end}
            if all_dates:
                last_date = max(all_dates)
                rec["last_sprint_date"] = last_date
                # Preserve the standalone report's one-day adjustment.
                rec["days_since_sprint"] = max(0, int((end - last_date).days) - 1)
            else:
                rec["last_sprint_date"] = pd.NaT
                rec["days_since_sprint"] = None

        if today_has_hsr:
            rec["days_since_hsr"] = 0
        else:
            gps_hsr_dates = set()
            if not p_prior.empty:
                ph = safe_num(p_prior.get("hsr_distance_m", pd.Series(0, index=p_prior.index)), 0)
                gps_hsr_dates = set(p_prior.loc[ph >= hsr_thr, "date"].dt.normalize())
            game_hsr_dates = {d for d, dist in g_hsr_by_date.items() if d < end and dist >= hsr_thr}
            all_hsr_dates = gps_hsr_dates | game_hsr_dates
            rec["days_since_hsr"] = int((end - max(all_hsr_dates)).days) if all_hsr_dates else None

        # 7-day exposure totals. Match the standalone same-date guard for PRIOR
        # game exposure so a date represented in practice GPS is not double counted.
        prior7_dates = set(p_prior7["date"].dt.normalize()) if not p_prior7.empty else set()
        p7_sprints = safe_num(p_prior7.get("n_sprints", pd.Series(dtype=float)), 0)
        p7_sdist = safe_num(p_prior7.get("sprint_distance_m", pd.Series(dtype=float)), 0)
        p7_hsr = safe_num(p_prior7.get("hsr_distance_m", pd.Series(dtype=float)), 0)
        game7_sdist = 0.0
        game7_hsr = 0.0
        game7_sprint_days = 0
        for gd, dist_m in g_dist_by_date.items():
            if cut7 <= gd < end and gd not in prior7_dates:
                if gd in g_dates:
                    game7_sprint_days += 1
                game7_sdist += dist_m
                game7_hsr += g_hsr_by_date.get(gd, 0.0)
        gps7_sprint_days = int(((p7_sprints >= sprint_count_thr) | (p7_sdist >= sprint_dist_thr)).sum())

        rec["r7_sprint_days"] = gps7_sprint_days + game7_sprint_days + (1 if today_has_sprint else 0)
        rec["r7_sprint_dist"] = float(p7_sdist.sum()) + game7_sdist + (float(today_sdist) if pd.notna(today_sdist) else 0.0) + today_game_dist
        rec["r7_hsr"] = float(p7_hsr.sum()) + game7_hsr + (float(today_hsr) if pd.notna(today_hsr) else 0.0) + today_game_hsr
        rec["today_has_sprint"] = today_has_sprint
        rec["today_has_hsr"] = today_has_hsr
        records.append(rec)

    result = pd.DataFrame(records)
    if result.empty or not criteria["use_gps_flags"]:
        return result

    # Pass 2: metric flags by Team + Position, matching gps_flags.py.
    for (team, pos), grp_idx in result.groupby(["Team", "Pos"], dropna=False).groups.items():
        grp_idx = list(grp_idx)
        grp = result.loc[grp_idx]
        n = len(grp)

        hist_pos = practice[
            (practice.get("practice_team", pd.Series("", index=practice.index)).apply(clean_team) == team)
            & (practice.get("position", pd.Series("", index=practice.index)).astype(str) == str(pos))
            & (practice["date"] >= cutrolling)
            & (practice["date"] < end)
        ] if not practice.empty else pd.DataFrame()

        rolling_stats = {}
        for col, short, unit, flag_enabled, flag_mode in FLAG_METRICS:
            if flag_mode != "rolling_pct":
                continue
            vals = safe_num(hist_pos[col]).dropna() if (not hist_pos.empty and col in hist_pos.columns) else pd.Series(dtype=float)
            if len(vals) >= int(criteria["rolling_min_sessions"]):
                rolling_stats[col] = (float(vals.mean()), float(vals.std(ddof=1)))
            else:
                rolling_stats[col] = (np.nan, np.nan)

        for col, short, unit, flag_enabled, flag_mode in FLAG_METRICS:
            if not flag_enabled:
                continue
            val_col, flag_col, z_col = f"{col}_val", f"{col}_flag", f"{col}_z"

            if flag_mode == "rolling_pct":
                rmean, rsd = rolling_stats.get(col, (np.nan, np.nan))
                min_delta = (
                    float(criteria["rolling_min_delta_sprints"])
                    if col == "n_sprints" else float(criteria["rolling_min_delta_accels"])
                )
                use_rolling = pd.notna(rmean) and pd.notna(rsd) and rsd > 0
                if use_rolling:
                    for idx in grp_idx:
                        v = result.at[idx, val_col]
                        if pd.isna(v):
                            continue
                        z = (v - rmean) / rsd
                        result.at[idx, z_col] = round(float(z), 2)
                        delta = v - rmean
                        if z >= float(criteria["rolling_review_z"]) and delta >= min_delta:
                            result.at[idx, flag_col] = "review"
                            result.at[idx, "flag_count"] += 1
                        elif z >= float(criteria["rolling_monitor_z"]) and delta >= min_delta:
                            result.at[idx, flag_col] = "monitor"
                            result.at[idx, "flag_count"] += 1
                else:
                    vals = safe_num(grp[val_col]).dropna()
                    if n < MIN_GROUP_SIZE or len(vals) < MIN_GROUP_SIZE:
                        continue
                    mean, sd = vals.mean(), vals.std(ddof=1)
                    if pd.isna(sd) or sd == 0:
                        continue
                    for idx in grp_idx:
                        v = result.at[idx, val_col]
                        if pd.isna(v):
                            continue
                        z = (v - mean) / sd
                        result.at[idx, z_col] = round(float(z), 2)
                        if z >= float(criteria["review_z"]):
                            result.at[idx, flag_col] = "review"
                            result.at[idx, "flag_count"] += 1
                        elif z >= float(criteria["monitor_z"]):
                            result.at[idx, flag_col] = "monitor"
                            result.at[idx, "flag_count"] += 1
            else:
                vals = safe_num(grp[val_col]).dropna()
                if n < MIN_GROUP_SIZE or len(vals) < MIN_GROUP_SIZE:
                    continue
                mean, sd = vals.mean(), vals.std(ddof=1)
                if pd.isna(sd) or sd == 0:
                    continue
                for idx in grp_idx:
                    v = result.at[idx, val_col]
                    if pd.isna(v):
                        continue
                    z = (v - mean) / sd
                    result.at[idx, z_col] = round(float(z), 2)
                    if z >= float(criteria["review_z"]):
                        result.at[idx, flag_col] = "review"
                        result.at[idx, "flag_count"] += 1
                    elif z >= float(criteria["monitor_z"]):
                        result.at[idx, flag_col] = "monitor"
                        result.at[idx, "flag_count"] += 1

    return result


def classify_status_from_snapshot(row, criteria=None):
    """Return status, driver, action, combined load and practice level."""
    criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
    flags = {col: row.get(f"{col}_flag") for col, *_ in FLAG_METRICS}
    has_review = any(v == "review" for v in flags.values())
    has_monitor = any(v == "monitor" for v in flags.values())
    practice_level = "High" if has_review else ("Moderate" if has_monitor else "Low")
    game_class = row.get("game_load_class", "—")
    combined = classify_combined_load(practice_level, game_class)
    acwr = pd.to_numeric(row.get("acwr"), errors="coerce")

    if not bool(row.get("has_gps", False)):
        return "Data Check", "No GPS session", "Confirm if off-day, injury, or device issue", combined, practice_level
    if all(pd.isna(row.get(f"{col}_val")) for col, *_ in FLAG_METRICS):
        return "Data Check", "Missing GPS data", "Confirm device sync and session upload", combined, practice_level

    if criteria["use_acwr"] and pd.notna(acwr) and acwr >= criteria["review_acwr"]:
        return "Review", f"High ACWR ({acwr:.2f})", "Check soreness/readiness; consider modified next-day workload", combined, practice_level
    if criteria["use_combined_load"] and combined == "Major Load Concern":
        return "Review", "High combined practice + game load", "Check soreness/readiness; avoid additional sprint volume tomorrow", combined, practice_level
    if criteria["use_combined_load"] and game_class == "High" and criteria["use_acwr"] and pd.notna(acwr) and acwr >= criteria["monitor_acwr"]:
        return "Review", f"High game load + elevated ACWR ({acwr:.2f})", "Avoid extra sprint volume tomorrow; check next-day readiness", combined, practice_level
    if has_review:
        for col, short, unit, flag_enabled, flag_mode in FLAG_METRICS:
            if flag_enabled and flags.get(col) == "review":
                z = row.get(f"{col}_z", np.nan)
                zs = f" (z={z:.1f})" if pd.notna(z) else ""
                return "Review", f"{short} spike{zs}", "Seek athlete context; consider modified next-day workload", combined, practice_level

    if criteria["use_acwr"] and pd.notna(acwr) and acwr >= criteria["monitor_acwr"]:
        return "Monitor", f"Elevated ACWR ({acwr:.2f})", "Watch next session; avoid extra volume", combined, practice_level
    if criteria["use_combined_load"] and combined == "Practice-Driven Spike":
        return "Monitor", "Practice load spike", "Watch next session; note load context", combined, practice_level
    if has_monitor:
        for col, short, unit, flag_enabled, flag_mode in FLAG_METRICS:
            if flag_enabled and flags.get(col) == "monitor":
                z = row.get(f"{col}_z", np.nan)
                zs = f" (z={z:.1f})" if pd.notna(z) else ""
                return "Monitor", f"{short} elevated{zs}", "Watch next session; note load trend", combined, practice_level

    if criteria["use_exposure_flags"]:
        ds_sprint = row.get("days_since_sprint")
        ds_hsr = row.get("days_since_hsr")
        today_sprint = bool(row.get("today_has_sprint", False))
        today_hsr = bool(row.get("today_has_hsr", False))
        r7_sprint = float(row.get("r7_sprint_dist") or 0)
        r7_hsr = float(row.get("r7_hsr") or 0)

        if pd.notna(ds_sprint) and ds_sprint is not None and ds_sprint > criteria["max_days_without_sprint"]:
            return "Needs Exposure", f"No sprint exposure ({int(ds_sprint)}d)", "Add controlled sprint exposure if healthy", combined, practice_level
        if pd.notna(ds_hsr) and ds_hsr is not None and ds_hsr > criteria["max_days_without_hsr"]:
            return "Needs Exposure", f"No HSR exposure ({int(ds_hsr)}d)", "Add controlled HSR exposure if healthy", combined, practice_level
        if not today_sprint and r7_sprint < criteria["low_7d_sprint_dist_m"]:
            return "Needs Exposure", f"Low 7-day sprint dist ({r7_sprint:.0f}m)", "Add controlled sprint exposure if healthy", combined, practice_level
        if not today_hsr and r7_hsr < criteria["low_7d_hsr_m"]:
            return "Needs Exposure", f"Low 7-day HSR ({r7_hsr:.0f}m)", "Increase high-speed running volume if healthy", combined, practice_level

    return "Prepared", "Normal workload", "Maintain normal plan", combined, practice_level


def build_status_table(bundle, end_date, player_keys, criteria=None) -> pd.DataFrame:
    criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
    if not player_keys:
        return pd.DataFrame()
    end = pd.Timestamp(end_date).normalize()
    snap = compute_flag_snapshot(bundle, end, player_keys, criteria=criteria)
    if snap.empty:
        return pd.DataFrame()

    history = bundle.get("history_calendar", pd.DataFrame())
    rows = []
    for _, r in snap.iterrows():
        status, driver, action, combined, practice_level = classify_status_from_snapshot(r, criteria=criteria)
        key = r["Player Key"]
        grp = history[(history["player_key"] == key) & (history["date"] <= end)].sort_values("date") if not history.empty else pd.DataFrame()

        last_game_class = r.get("game_load_class", "—")
        if pd.isna(r.get("last_game_date")):
            last_game_label = "—"
        else:
            last_game_label = f"{last_game_class} · {int(round(r.get('last_game_runs', 0)))} / {int(round(r.get('last_game_dist_yards', 0)))} yd"

        prev = grp[grp["date"] == end - pd.Timedelta(days=1)] if not grp.empty else pd.DataFrame()
        if prev.empty or int(prev.iloc[-1].get("practice_observed", 0)) == 0:
            prev_label = "—"
        else:
            pr = prev.iloc[-1]
            prev_label = f"{int(round(pr.get('n_accelerations', 0)))} acc / {int(round(pr.get('n_sprints', 0)))} spr / {int(round(pr.get('hsr_distance_m', 0)))} m HSR"

        last_sprint = r.get("last_sprint_date")
        last_sprint_label = "no history" if pd.isna(last_sprint) else pd.Timestamp(last_sprint).strftime("%b %d")
        acwr = pd.to_numeric(r.get("acwr"), errors="coerce")

        rows.append({
            "Player Key": key,
            "Athlete": r.get("Athlete", key),
            "Team": r.get("Team", ""),
            "Pos": r.get("Pos", ""),
            "Status": status,
            "Primary Driver": driver,
            "Recommended Action": action,
            "Combined Load": combined,
            "Practice Level": practice_level,
            "ACWR": round(float(acwr), 2) if pd.notna(acwr) else np.nan,
            "Last Game Load": last_game_label,
            "Practice Load (Prev Day)": prev_label,
            "Last Sprint": last_sprint_label,
            "Days Since Sprint": r.get("days_since_sprint"),
            "Days Since HSR": r.get("days_since_hsr"),
            "7d Sprint Dist (m)": round(float(r.get("r7_sprint_dist", 0)), 1),
            "7d HSR (m)": round(float(r.get("r7_hsr", 0)), 1),
            "Flag Count": int(r.get("flag_count", 0)),
        })

    out = pd.DataFrame(rows)
    out["_severity"] = out["Status"].map(STATUS_ORDER).fillna(99)
    return out.sort_values(["_severity", "Team", "Athlete"]).drop(columns="_severity").reset_index(drop=True)

def build_period_summary(bundle, start_date, end_date, player_keys) -> pd.DataFrame:
    d = bundle["daily"]
    if d.empty or not player_keys:
        return pd.DataFrame()
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    df = d[d["date"].between(start, end) & d["player_key"].isin(player_keys)].copy()
    if df.empty:
        return pd.DataFrame()

    agg = (
        df.groupby("player_key", as_index=False)
          .agg(
              Athlete=("player_name", mode_or_last),
              Team=("team", mode_or_last),
              Pos=("position", mode_or_last),
              Practice_Days=("practice_observed", lambda s: int((safe_num(s, 0) > 0).sum())),
              Game_Days=("game_observed", lambda s: int((safe_num(s, 0) > 0).sum())),
              Practice_Sessions=("practice_sessions", "sum"),
              Total_Distance_m=("combined_total_distance_m", "sum"),
              HSR_m=("hsr_distance_m", "sum"),
              Max_Effort_Game_m=("game_max_effort_distance_m", "sum"),
              Accelerations=("n_accelerations", "sum"),
              Sprints=("n_sprints", "sum"),
              Top_Speed_mps=("top_speed_ms", "max"),
              Duration_min=("duration_min", "sum"),
          )
    )
    for c in ["Practice_Sessions", "Practice_Days", "Game_Days", "Accelerations", "Sprints"]:
        agg[c] = safe_num(agg[c], 0).round(0).astype(int)
    for c in ["Total_Distance_m", "HSR_m", "Max_Effort_Game_m", "Duration_min"]:
        agg[c] = safe_num(agg[c], 0).round(0)
    agg["Top_Speed_mps"] = safe_num(agg["Top_Speed_mps"]).round(2)
    agg["Top_Speed_mph"] = (agg["Top_Speed_mps"] * 2.236936).round(1)
    return agg.sort_values(["Team", "Athlete"]).reset_index(drop=True)


def selected_history(bundle, start_date, end_date, player_keys):
    h = bundle["history_calendar"]
    if h.empty:
        return h
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    return h[h["date"].between(start, end) & h["player_key"].isin(player_keys)].copy()


# =============================================================================
# CHARTS
# =============================================================================

PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Inter, Arial, sans-serif", color=C_TEXT, size=12),
    margin=dict(l=54, r=24, t=58, b=44),
    hoverlabel=dict(bgcolor="white", bordercolor=C_BORDER, font_size=12),
    hovermode="closest",
)

# Restrained comparison palette. Lines carry the information; markers are intentionally
# omitted so multi-player trend charts stay clean even over long date ranges.
COMPARISON_COLORS = [
    C_RED, C_NAVY, C_BLUE, C_GREEN, C_AMBER, C_PURPLE,
    "#0F766E", "#9333EA", "#475569", "#B45309",
]

TREND_METRICS = [
    ("top_speed_ms", "Top Speed", "m/s", "max"),
    ("n_sprints", "Sprints", "#", "sum"),
    ("sprint_distance_m", "Sprint Distance", "m", "sum"),
    ("n_accelerations", "Accelerations", "#", "sum"),
    ("hsr_distance_m", "HSR", "m", "sum"),
    ("combined_total_distance_m", "Total Distance", "m", "sum"),
    ("duration_min", "Duration", "min", "sum"),
    ("acwr", "PP_ACWR (7:28 EWMA)", "ratio", "last"),
]


def empty_figure(title="No data"):
    fig = go.Figure()
    fig.update_layout(**PLOT_LAYOUT, height=330, title=dict(text=title, x=0))
    return fig


def _trend_rows_for_metric(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Keep only true event observations for trends.

    GPS metrics use practice-observed dates. ACWR uses dates actually present
    in the PP_ACWR sheet; carried-forward calendar values are display-only.
    """
    if df is None or df.empty or metric not in df.columns:
        return pd.DataFrame()
    p = df.copy().sort_values("date")
    if metric == "acwr" and "acwr_observed" in p.columns:
        p = p[safe_num(p["acwr_observed"], 0) > 0].copy()
    elif "practice_observed" in p.columns:
        p = p[safe_num(p["practice_observed"], 0) > 0].copy()
    p[metric] = safe_num(p[metric])
    return p[p[metric].notna()].copy()


def comparison_trend_figure(
    bundle,
    start_date,
    end_date,
    player_keys,
    teams,
    metric,
    title,
    unit,
    show_team_average=False,
    show_selected_average=False,
    team_average_exclude_keys=None,
    criteria=None,
    restrict_player_teams=None,
    legend_mode="averages",
):
    """Compare multiple players on a restrained trend chart with optional averages."""
    history = bundle.get("history_calendar", pd.DataFrame())
    if history is None or history.empty:
        return empty_figure(title)

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    display = player_display_map(bundle)
    fig = go.Figure()
    plotted = 0

    player_keys = list(player_keys or [])
    player_count = len(player_keys)

    # Keep the chart calm by default. Individual lines are still identifiable on hover.
    if legend_mode == "all":
        show_player_legend = True
        show_average_legend = True
    elif legend_mode == "off":
        show_player_legend = False
        show_average_legend = False
    else:  # averages
        show_player_legend = False
        show_average_legend = True

    for idx, key in enumerate(player_keys):
        p = history[
            history["date"].between(start, end) & history["player_key"].eq(key)
        ].copy()
        if restrict_player_teams:
            p = p[p["team"].isin(list(restrict_player_teams))].copy()
        p = _trend_rows_for_metric(p, metric)
        if p.empty:
            continue

        color = COMPARISON_COLORS[idx % len(COMPARISON_COLORS)]
        fig.add_trace(go.Scatter(
            x=p["date"],
            y=p[metric],
            mode="lines",
            name=display.get(key, key),
            line=dict(color=color, width=1.35),
            opacity=0.55,
            connectgaps=False,
            showlegend=show_player_legend,
            hovertemplate=(
                f"<b>{display.get(key, key)}</b><br>"
                f"%{{x|%b %d, %Y}}<br>{title}: %{{y:.1f}} {unit}<extra></extra>"
            ),
        ))
        plotted += 1

    if show_team_average:
        excluded_from_team_avg = set(team_average_exclude_keys or [])
        avg_team_values = list(teams or [])
        if not avg_team_values:
            avg_team_values = list(
                history.loc[history["player_key"].isin(player_keys), "team"].dropna().unique()
            ) if player_keys else []

        for team_idx, team in enumerate(ordered_teams(avg_team_values)):
            team_keys = eligible_player_keys(bundle, start, end, [team], selected_keys=None)
            team_keys = [k for k in team_keys if k not in excluded_from_team_avg]
            if not team_keys:
                continue

            th = history[
                history["date"].between(start, end)
                & history["player_key"].isin(team_keys)
                & history["team"].eq(team)
            ].copy()
            th = _trend_rows_for_metric(th, metric)
            if th.empty:
                continue

            team_daily = (
                th.groupby("date", as_index=False)[metric]
                  .mean()
                  .sort_values("date")
            )
            fig.add_trace(go.Scatter(
                x=team_daily["date"],
                y=team_daily[metric],
                mode="lines",
                name=f"{team} average",
                line=dict(color=C_TEXT, width=3.6, dash="dash"),
                opacity=0.98,
                connectgaps=False,
                showlegend=show_average_legend,
                hovertemplate=(
                    f"<b>{team} team average</b><br>"
                    f"%{{x|%b %d, %Y}}<br>{title}: %{{y:.1f}} {unit}<extra></extra>"
                ),
            ))
            plotted += 1

    if show_selected_average and player_keys:
        sh = history[
            history["date"].between(start, end)
            & history["player_key"].isin(player_keys)
        ].copy()
        if restrict_player_teams:
            sh = sh[sh["team"].isin(list(restrict_player_teams))].copy()
        sh = _trend_rows_for_metric(sh, metric)
        if not sh.empty:
            selected_daily = (
                sh.groupby("date", as_index=False)[metric]
                  .mean()
                  .sort_values("date")
            )
            fig.add_trace(go.Scatter(
                x=selected_daily["date"],
                y=selected_daily[metric],
                mode="lines",
                name="Selected players average",
                line=dict(color=C_PURPLE, width=3.2, dash="dot"),
                opacity=0.98,
                connectgaps=False,
                showlegend=show_average_legend,
                hovertemplate=(
                    f"<b>Selected players average</b><br>"
                    f"%{{x|%b %d, %Y}}<br>{title}: %{{y:.1f}} {unit}<extra></extra>"
                ),
            ))
            plotted += 1

    if metric == "acwr":
        criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
        refs = [(criteria["optimal_low_acwr"], C_BLUE)]
        if criteria["use_acwr"]:
            refs.extend([
                (criteria["monitor_acwr"], C_AMBER),
                (criteria["review_acwr"], C_RED),
            ])
        for val, color in refs:
            fig.add_hline(
                y=val,
                line_dash="dot",
                line_color=color,
                line_width=1.0,
                opacity=0.45,
            )

    if plotted == 0:
        return empty_figure(title)

    unit_labels = {
        "m": "Meters",
        "m/s": "m/s",
        "#": "Count",
        "min": "Minutes",
        "ratio": "ACWR",
    }
    y_title = unit_labels.get(unit, unit)

    # For explicit all-player legends, use a right rail when many names are present.
    all_legend_large = legend_mode == "all" and player_count > 8
    if all_legend_large:
        legend_cfg = dict(
            orientation="v",
            yanchor="top",
            y=1.0,
            xanchor="left",
            x=1.015,
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            font=dict(size=10, color=C_MUTED),
            itemsizing="constant",
            title=dict(text=""),
        )
        chart_margin = dict(l=62, r=225, t=18, b=54)
    else:
        # Average-only legend is compact and sits above the plotting area.
        legend_cfg = dict(
            orientation="h",
            yanchor="bottom",
            y=1.015,
            xanchor="right",
            x=1.0,
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            font=dict(size=10, color=C_MUTED),
            itemsizing="constant",
            title=dict(text=""),
        )
        chart_margin = dict(l=62, r=24, t=34, b=54)

    # Important: use closest-hover, not unified-hover. Unified hover was the source
    # of the stray 'undefined' label in some Plotly/Streamlit combinations.
    comparison_layout = {
        **PLOT_LAYOUT,
        "height": 470,
        "margin": chart_margin,
        # Explicit empty title avoids Plotly/Streamlit rendering a JS undefined title.
        "title": dict(text=""),
        "hovermode": "closest",
        "showlegend": (show_player_legend or show_average_legend),
        "legend": legend_cfg,
        "xaxis": dict(
            showgrid=False,
            zeroline=False,
            showline=True,
            linecolor="#D7DEE8",
            linewidth=1,
            tickformat="%b %d",
            tickfont=dict(size=11, color=C_MUTED),
            title=dict(text=""),
            # Plotly 6 can surface the literal word "undefined" from an unset
            # unified-hover title in some Streamlit builds. Set it explicitly.
            unifiedhovertitle=dict(text="%{x|%b %d}"),
            fixedrange=False,
        ),
        "yaxis": dict(
            showgrid=True,
            gridcolor="#E8EDF3",
            gridwidth=1,
            zeroline=False,
            showline=False,
            tickfont=dict(size=11, color=C_MUTED),
            title=dict(text=y_title, font=dict(size=11, color=C_MUTED), standoff=10),
            fixedrange=False,
        ),
    }
    fig.update_layout(**comparison_layout)
    return fig



def team_context_trend_figure(
    bundle,
    start_date,
    end_date,
    reference_team,
    metric,
    title,
    unit,
    focus_player_key=None,
    criteria=None,
    show_middle_50=True,
):
    """Clean trend view: one focus athlete against the team's daily median/IQR.

    The prior dashboard put every athlete on one chart by default. This view uses
    the team as context instead: a restrained middle-50% band + median, with at
    most one athlete emphasized. Multi-athlete overlays remain available as an
    explicit comparison view in the UI.
    """
    history = bundle.get("history_calendar", pd.DataFrame())
    if history is None or history.empty or not reference_team:
        return empty_figure(title)

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    team_keys = eligible_player_keys(bundle, start, end, [reference_team], selected_keys=None)
    if not team_keys:
        return empty_figure(title)

    team_history = history[
        history["date"].between(start, end)
        & history["player_key"].isin(team_keys)
        & history["team"].eq(reference_team)
    ].copy()
    team_history = _trend_rows_for_metric(team_history, metric)
    if team_history.empty:
        return empty_figure(title)

    grouped = team_history.groupby("date")[metric]
    team_daily = pd.DataFrame({
        "date": grouped.median().index,
        "median": grouped.median().values,
        "q25": grouped.quantile(0.25).values,
        "q75": grouped.quantile(0.75).values,
        "n": grouped.count().values,
    }).sort_values("date")

    fig = go.Figure()
    hover_unit = "" if unit == "ratio" else f" {unit}"

    # Middle 50% is intentionally subtle: it gives context without producing a
    # spaghetti chart of every player on the roster.
    if show_middle_50 and not team_daily.empty:
        fig.add_trace(go.Scatter(
            x=team_daily["date"],
            y=team_daily["q25"],
            mode="lines",
            line=dict(width=0, color="rgba(17,34,90,0)"),
            hoverinfo="skip",
            showlegend=False,
            name="25th percentile",
        ))
        fig.add_trace(go.Scatter(
            x=team_daily["date"],
            y=team_daily["q75"],
            mode="lines",
            line=dict(width=0, color="rgba(17,34,90,0)"),
            fill="tonexty",
            fillcolor="rgba(17,34,90,0.10)",
            name="Team middle 50%",
            customdata=team_daily[["q25"]].to_numpy(),
            hovertemplate=(
                f"<b>{reference_team} middle 50%</b><br>"
                f"%{{x|%b %d, %Y}}<br>Range: %{{customdata[0]:.1f}}–%{{y:.1f}}{hover_unit}<extra></extra>"
            ),
        ))

    fig.add_trace(go.Scatter(
        x=team_daily["date"],
        y=team_daily["median"],
        mode="lines",
        name=f"{reference_team} median",
        line=dict(color=C_NAVY, width=2.4, dash="dash"),
        opacity=0.9,
        connectgaps=False,
        hovertemplate=(
            f"<b>{reference_team} median</b><br>"
            f"%{{x|%b %d, %Y}}<br>{title}: %{{y:.1f}}{hover_unit}<extra></extra>"
        ),
    ))

    if focus_player_key:
        display = player_display_map(bundle)
        focus = history[
            history["date"].between(start, end)
            & history["player_key"].eq(focus_player_key)
        ].copy()
        focus = _trend_rows_for_metric(focus, metric)
        if not focus.empty:
            fig.add_trace(go.Scatter(
                x=focus["date"],
                y=focus[metric],
                mode="lines+markers",
                name=display.get(focus_player_key, focus_player_key),
                line=dict(color=C_RED, width=3.1),
                marker=dict(size=5, color=C_RED, line=dict(width=1, color="white")),
                connectgaps=False,
                hovertemplate=(
                    f"<b>{display.get(focus_player_key, focus_player_key)}</b><br>"
                    f"%{{x|%b %d, %Y}}<br>{title}: %{{y:.1f}}{hover_unit}<extra></extra>"
                ),
            ))

    if metric == "acwr":
        criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
        fig.add_hline(
            y=criteria["optimal_low_acwr"], line_dash="dot", line_color=C_BLUE,
            line_width=1.0, opacity=0.45,
        )
        if criteria["use_acwr"]:
            fig.add_hline(
                y=criteria["monitor_acwr"], line_dash="dot", line_color=C_AMBER,
                line_width=1.0, opacity=0.5,
            )
            fig.add_hline(
                y=criteria["review_acwr"], line_dash="dot", line_color=C_RED,
                line_width=1.0, opacity=0.5,
            )

    unit_labels = {"m": "Meters", "m/s": "m/s", "#": "Count", "min": "Minutes", "ratio": "ACWR"}
    y_title = unit_labels.get(unit, unit)
    context_layout = {
        **PLOT_LAYOUT,
        "height": 455,
        "margin": dict(l=62, r=24, t=34, b=50),
        "title": dict(text=""),
        "hovermode": "closest",
        "legend": dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
            bgcolor="rgba(0,0,0,0)", borderwidth=0,
            font=dict(size=10, color=C_MUTED),
        ),
        "xaxis": dict(
            showgrid=False, zeroline=False, showline=True,
            linecolor="#D7DEE8", linewidth=1,
            tickformat="%b %d", tickfont=dict(size=11, color=C_MUTED),
            title=dict(text=""), fixedrange=False,
        ),
        "yaxis": dict(
            showgrid=True, gridcolor="#E8EDF3", gridwidth=1,
            zeroline=False, showline=False,
            tickfont=dict(size=11, color=C_MUTED),
            title=dict(text=y_title, font=dict(size=11, color=C_MUTED), standoff=10),
            fixedrange=False,
        ),
    }
    fig.update_layout(**context_layout)
    return fig


def latest_focus_metric_context(bundle, start_date, end_date, reference_team, focus_player_key, metric):
    """Return latest athlete value plus same-day team context for UI summary cards."""
    history = bundle.get("history_calendar", pd.DataFrame())
    if history is None or history.empty or not focus_player_key:
        return None
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()

    focus = history[
        history["date"].between(start, end)
        & history["player_key"].eq(focus_player_key)
    ].copy()
    focus = _trend_rows_for_metric(focus, metric)
    if focus.empty:
        return None
    focus = focus.sort_values("date")
    latest = focus.iloc[-1]
    latest_date = pd.Timestamp(latest["date"]).normalize()
    value = pd.to_numeric(pd.Series([latest[metric]]), errors="coerce").iloc[0]
    if pd.isna(value):
        return None

    team_keys = eligible_player_keys(bundle, start, end, [reference_team], selected_keys=None)
    same_day = history[
        history["date"].eq(latest_date)
        & history["player_key"].isin(team_keys)
        & history["team"].eq(reference_team)
    ].copy()
    same_day = _trend_rows_for_metric(same_day, metric)
    vals = pd.to_numeric(same_day.get(metric, pd.Series(dtype=float)), errors="coerce").dropna()
    median = float(vals.median()) if not vals.empty else np.nan
    percentile = float((vals <= float(value)).mean() * 100.0) if len(vals) >= 2 else np.nan
    return {
        "date": latest_date,
        "value": float(value),
        "team_median": median,
        "delta_vs_median": float(value - median) if pd.notna(median) else np.nan,
        "percentile": percentile,
        "team_n": int(len(vals)),
    }

def format_trend_value(value, unit, signed=False):
    if pd.isna(value):
        return "—"
    sign = "+" if signed and float(value) > 0 else ""
    if unit == "#":
        return f"{sign}{float(value):.0f}"
    if unit == "ratio":
        return f"{sign}{float(value):.2f}"
    if unit == "m/s":
        return f"{sign}{float(value):.2f} m/s"
    suffix = f" {unit}" if unit else ""
    return f"{sign}{float(value):.1f}{suffix}"


def team_period_figure(bundle, start_date, end_date, player_keys):
    h = selected_history(bundle, start_date, end_date, player_keys)
    if h.empty:
        return empty_figure("Selected-period workload")
    # Use observed + zero days from history calendar so team daily average is comparable.
    daily = (
        h.groupby(["date", "team"], as_index=False)
         .agg(avg_total_distance=("combined_total_distance_m", "mean"))
    )
    fig = go.Figure()
    for team in ordered_teams(daily["team"].dropna().unique()):
        t = daily[daily["team"] == team]
        fig.add_trace(go.Scatter(
            x=t["date"], y=t["avg_total_distance"], mode="lines",
            name=team,
            line=dict(width=2.6),
            hovertemplate=f"<b>{team}</b><br>%{{x|%b %d}}<br>%{{y:.0f}} m/player<extra></extra>",
        ))
    fig.update_layout(
        **PLOT_LAYOUT,
        height=390,
        title=dict(text="Average Combined Distance by Team", x=0, font=dict(size=16, color=C_NAVY)),
        xaxis=dict(showgrid=False, showline=True, linecolor=C_BORDER, linewidth=1),
        yaxis=dict(title="m / player / day", showgrid=True, gridcolor="#E9EEF5", zeroline=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, bgcolor="rgba(0,0,0,0)"),
    )
    return fig


# =============================================================================
# REPORT EXPORT — exact supplied standalone report structure
# =============================================================================

def _report_roster_frame(bundle, team: str) -> pd.DataFrame:
    """Current roster for one affiliate, sourced only from Python Reports -> Master Roster."""
    roster = bundle.get("roster", pd.DataFrame()).copy()
    if roster.empty:
        return pd.DataFrame(columns=["Athlete", "Team", "Position"])
    roster = roster[roster["roster_team"].apply(clean_team) == clean_team(team)].copy()
    if roster.empty:
        return pd.DataFrame(columns=["Athlete", "Team", "Position"])
    out = pd.DataFrame({
        "Athlete": roster["player_name"].fillna("").astype(str).str.strip(),
        "Team": roster["roster_team"].fillna("").astype(str).str.strip(),
        "Position": roster["position"].fillna("").astype(str).str.strip(),
    })
    out = out[out["Athlete"].ne("")].drop_duplicates(subset=["Athlete"], keep="last")
    return out.reset_index(drop=True)


def _report_roster_maps(roster_df: pd.DataFrame):
    team_map, pos_map, name_map = {}, {}, {}
    if roster_df is None or roster_df.empty:
        return team_map, pos_map, name_map
    for _, row in roster_df.iterrows():
        key = normalize_name(row.get("Athlete", ""))
        if not key:
            continue
        name_map[key] = str(row.get("Athlete", "")).strip()
        team_map[key] = str(row.get("Team", "")).strip()
        pos_map[key] = str(row.get("Position", "")).strip()
    return team_map, pos_map, name_map


def _report_gps_frame(bundle, roster_df: pd.DataFrame) -> pd.DataFrame:
    """Adapt the dashboard's raw STATSports rows to gps_report_html.py's exact input schema."""
    raw = bundle.get("raw_practice_source", pd.DataFrame()).copy()
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw.columns = [str(c).strip() for c in raw.columns]
    lower = {str(c).strip().casefold(): c for c in raw.columns}
    rename = {}
    for src, dst in [("player_name", "Athlete"), ("athlete", "Athlete"), ("date", "Date")]:
        if src in lower:
            rename[lower[src]] = dst
    raw = raw.rename(columns=rename)
    if "Athlete" not in raw.columns or "Date" not in raw.columns:
        return pd.DataFrame()

    raw["Athlete"] = raw["Athlete"].fillna("").astype(str).str.strip()
    raw["Date"] = pd.to_datetime(raw["Date"], errors="coerce").dt.normalize()
    raw = raw[(raw["Athlete"].ne("")) & raw["Date"].notna()].copy()
    raw["_player_key"] = raw["Athlete"].apply(normalize_name)

    team_map, pos_map, name_map = _report_roster_maps(roster_df)
    roster_keys = set(name_map)
    raw = raw[raw["_player_key"].isin(roster_keys)].copy()
    if raw.empty:
        return raw.drop(columns=["_player_key"], errors="ignore")

    # Master Roster is authoritative for REPORT MEMBERSHIP and current display name.
    # For the supplied flag engine, preserve the GPS session's own team when it
    # exists (or derive it from session_name); this keeps historical same-team
    # rolling baselines from being rewritten after a promotion/demotion.
    raw["Athlete"] = raw["_player_key"].map(name_map).fillna(raw["Athlete"])
    raw_team = pd.Series("", index=raw.index, dtype=object)
    for candidate in ["Team", "team", "practice_team"]:
        if candidate in raw.columns:
            vals = raw[candidate].fillna("").astype(str).map(clean_team)
            raw_team = raw_team.where(raw_team.ne(""), vals)
    if "session_name" in raw.columns:
        session_team = raw["session_name"].fillna("").astype(str).map(parse_team_from_session)
        raw_team = raw_team.where(raw_team.ne(""), session_team)
    raw["Team"] = raw_team.where(raw_team.ne(""), raw["_player_key"].map(team_map)).fillna("")

    raw_pos = pd.Series("", index=raw.index, dtype=object)
    for candidate in ["Position", "position"]:
        if candidate in raw.columns:
            vals = raw[candidate].fillna("").astype(str).str.strip()
            raw_pos = raw_pos.where(raw_pos.ne(""), vals)
    raw["Position"] = raw_pos.where(raw_pos.ne(""), raw["_player_key"].map(pos_map)).fillna("")

    for col in ["top_speed_ms", "n_sprints", "sprint_distance_m", "n_accelerations",
                "hsr_distance_m", "total_distance_m", "duration_min"]:
        if col not in raw.columns:
            raw[col] = np.nan
        raw[col] = pd.to_numeric(raw[col], errors="coerce")

    for col in ["drill_name", "session_name", "week", "week_start"]:
        if col not in raw.columns:
            raw[col] = ""
        raw[col] = raw[col].fillna("").astype(str).str.strip()

    return raw.drop(columns=["_player_key"], errors="ignore")


def _report_game_frame(bundle, roster_df: pd.DataFrame) -> pd.DataFrame:
    raw = bundle.get("raw_pp_sprint", pd.DataFrame()).copy()
    if raw is None or raw.empty:
        # Fallback to the already-cleaned daily game frame if raw PP_Sprint is unavailable.
        gd = bundle.get("games_daily", pd.DataFrame()).copy()
        if gd is None or gd.empty:
            return pd.DataFrame()
        raw = pd.DataFrame({
            "batter": gd.get("player_name", ""),
            "game_date": gd.get("date"),
            "max_effort_runs": gd.get("game_max_effort_runs", 0),
            "max_effort_distance_covered_yards": gd.get("game_max_effort_distance_yards", 0),
            "distance_covered_yards": gd.get("game_distance_yards", 0),
        })

    raw.columns = [str(c).strip() for c in raw.columns]
    lower = {str(c).strip().casefold(): c for c in raw.columns}
    if "batter" not in lower:
        if "player_name" in lower:
            raw = raw.rename(columns={lower["player_name"]: "batter"})
        else:
            return pd.DataFrame()
    elif lower["batter"] != "batter":
        raw = raw.rename(columns={lower["batter"]: "batter"})
    lower = {str(c).strip().casefold(): c for c in raw.columns}
    if "game_date" not in lower:
        if "date" in lower:
            raw = raw.rename(columns={lower["date"]: "game_date"})
        else:
            return pd.DataFrame()
    elif lower["game_date"] != "game_date":
        raw = raw.rename(columns={lower["game_date"]: "game_date"})

    raw["batter"] = raw["batter"].fillna("").astype(str).str.strip()
    raw["game_date"] = pd.to_datetime(raw["game_date"], errors="coerce").dt.normalize()
    raw = raw[(raw["batter"].ne("")) & raw["game_date"].notna()].copy()
    raw["_player_key"] = raw["batter"].apply(normalize_name)
    _, _, name_map = _report_roster_maps(roster_df)
    raw = raw[raw["_player_key"].isin(set(name_map))].copy()
    raw["batter"] = raw["_player_key"].map(name_map).fillna(raw["batter"])

    for col in ["max_effort_runs", "max_effort_distance_covered_yards", "distance_covered_yards",
                "monthly_p95_sprint_speed"]:
        if col not in raw.columns:
            raw[col] = np.nan
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    return raw.drop(columns=["_player_key"], errors="ignore")


def _report_acwr_frame(bundle, roster_df: pd.DataFrame) -> pd.DataFrame:
    raw = bundle.get("raw_pp_acwr", pd.DataFrame()).copy()
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw.columns = [str(c).strip().casefold() for c in raw.columns]
    if "batter" not in raw.columns or "ewma_acwr_7_28" not in raw.columns:
        return pd.DataFrame()
    raw["_player_key"] = raw["batter"].fillna("").astype(str).apply(normalize_name)
    _, _, name_map = _report_roster_maps(roster_df)
    raw = raw[raw["_player_key"].isin(set(name_map))].copy()
    raw["batter"] = raw["_player_key"].map(name_map).fillna(raw["batter"])
    raw["ewma_acwr_7_28"] = pd.to_numeric(raw["ewma_acwr_7_28"], errors="coerce")
    if "date" in raw.columns:
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce").dt.normalize()
    return raw.drop(columns=["_player_key"], errors="ignore")


def build_exact_team_report(bundle, team: str, report_date, temp_dir: Path) -> dict:
    """Generate one team's report with the supplied gps_report_html.py structure."""
    team = clean_team(team)
    roster_df = _report_roster_frame(bundle, team)
    if roster_df.empty:
        raise RuntimeError(f"No current Master Roster athletes found for {team}.")

    df_gps = _report_gps_frame(bundle, roster_df)
    df_game = _report_game_frame(bundle, roster_df)
    df_acwr = _report_acwr_frame(bundle, roster_df)
    report_ts = pd.Timestamp(report_date).normalize()

    html = generate_report_html(df_gps, df_game, df_acwr, roster_df, team, report_ts)
    safe_team = re.sub(r"[^A-Za-z0-9_-]+", "_", team).strip("_") or "Team"
    stem = f"{safe_team}_GPS_Workload_{report_ts.strftime('%Y-%m-%d')}"
    html_path = temp_dir / f"{stem}.html"
    pdf_path = temp_dir / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")

    pdf_ok = render_html_to_pdf(html_path, pdf_path)
    return {
        "team": team,
        "html_name": html_path.name,
        "html_bytes": html_path.read_bytes(),
        "pdf_name": pdf_path.name if pdf_ok and pdf_path.exists() else None,
        "pdf_bytes": pdf_path.read_bytes() if pdf_ok and pdf_path.exists() else None,
    }


def build_exact_reports(bundle, teams, report_date) -> dict:
    """One standalone-style report per team. Multi-team selections return a ZIP of those reports."""
    report_teams = ordered_teams(teams or [])
    if not report_teams:
        raise RuntimeError("Select at least one team to build reports.")

    results, errors = [], []
    with tempfile.TemporaryDirectory(prefix="gps_reports_") as td:
        temp_dir = Path(td)
        for team in report_teams:
            try:
                results.append(build_exact_team_report(bundle, team, report_date, temp_dir))
            except Exception as exc:
                errors.append(f"{team}: {exc}")

        if not results:
            raise RuntimeError("No reports were generated. " + " | ".join(errors))

        if len(results) == 1:
            result = results[0]
            return {"mode": "single", "result": result, "errors": errors}

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for result in results:
                if result["pdf_bytes"] is not None:
                    zf.writestr(result["pdf_name"], result["pdf_bytes"])
                else:
                    zf.writestr(result["html_name"], result["html_bytes"])
            if errors:
                zf.writestr("report_errors.txt", "\n".join(errors))
        return {
            "mode": "zip",
            "zip_bytes": zip_buffer.getvalue(),
            "zip_name": f"GPS_Workload_Reports_{pd.Timestamp(report_date).strftime('%Y-%m-%d')}.zip",
            "results": results,
            "errors": errors,
        }


# =============================================================================
# STREAMLIT APP
# =============================================================================

st.set_page_config(
    page_title="GPS Workload Dashboard",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    f"""
    <style>
      .block-container {{
          padding-top: 1.05rem;
          padding-bottom: 3rem;
          max-width: 1500px;
      }}
      h1, h2, h3 {{
          color: {C_TEXT};
          letter-spacing: -0.025em;
      }}
      h1 {{font-weight: 650;}}
      h2, h3 {{font-weight: 600;}}
      [data-testid="stMetric"] {{
          background: transparent;
          border: 0;
          border-top: 1px solid {C_BORDER};
          border-bottom: 1px solid {C_BORDER};
          border-radius: 0;
          padding: 12px 4px 10px 4px;
      }}
      [data-testid="stMetricLabel"] {{color: {C_MUTED};}}
      [data-testid="stDataFrame"] {{
          border: 1px solid {C_BORDER};
          border-radius: 4px;
          overflow: hidden;
      }}
      .stTabs [data-baseweb="tab-list"] {{
          gap: 1.4rem;
          border-bottom: 1px solid {C_BORDER};
      }}
      .stTabs [data-baseweb="tab"] {{
          height: 2.8rem;
          padding-left: 0;
          padding-right: 0;
          border-radius: 0;
          background: transparent;
      }}
      .stButton button, .stDownloadButton button {{
          border-radius: 5px !important;
          box-shadow: none !important;
      }}
      [data-testid="stExpander"] {{
          border-color: {C_BORDER};
          border-radius: 5px;
      }}
      .gps-subtle {{color: {C_MUTED}; font-size: 0.9rem; margin-bottom: 0.4rem;}}
      .trend-title-clean {{
          color: {C_TEXT};
          font-size: 1.05rem;
          font-weight: 650;
          letter-spacing: -0.012em;
          margin-top: 0.8rem;
          margin-bottom: 0.05rem;
      }}
      .trend-context-clean {{
          color: {C_MUTED};
          font-size: 0.82rem;
          font-weight: 450;
          margin-bottom: 0.15rem;
      }}
      .gps-badge {{
          display: inline-block;
          padding: 0.18rem 0.48rem;
          border-radius: 4px;
          font-size: 0.76rem;
          font-weight: 700;
          color: white;
      }}
      .status-grid {{
          display: grid;
          grid-template-columns: repeat(5, minmax(0, 1fr));
          gap: 0.75rem;
          margin: 0.55rem 0 1.15rem 0;
      }}
      .status-card {{
          background: #FFFFFF;
          border: 1px solid {C_BORDER};
          border-radius: 10px;
          padding: 0.85rem 0.95rem 0.78rem 0.95rem;
          min-height: 96px;
          box-shadow: 0 1px 2px rgba(15, 23, 42, 0.035);
      }}
      .status-card-label {{
          color: {C_MUTED};
          font-size: 0.75rem;
          font-weight: 700;
          letter-spacing: 0.035em;
          text-transform: uppercase;
      }}
      .status-card-value {{
          font-size: 1.75rem;
          font-weight: 760;
          line-height: 1.05;
          margin-top: 0.28rem;
      }}
      .status-card-sub {{
          color: {C_MUTED};
          font-size: 0.74rem;
          margin-top: 0.3rem;
      }}
      .section-kicker {{
          color: {C_MUTED};
          font-size: 0.74rem;
          font-weight: 700;
          letter-spacing: 0.06em;
          text-transform: uppercase;
          margin-top: 0.2rem;
          margin-bottom: 0.05rem;
      }}
      .focus-panel {{
          background: #FFFFFF;
          border: 1px solid {C_BORDER};
          border-radius: 10px;
          padding: 0.9rem 1rem;
          margin: 0.35rem 0 0.8rem 0;
      }}
      .focus-name {{
          color: {C_TEXT};
          font-size: 1.12rem;
          font-weight: 720;
          margin-bottom: 0.18rem;
      }}
      .focus-meta {{
          color: {C_MUTED};
          font-size: 0.8rem;
      }}
      @media (max-width: 900px) {{
          .status-grid {{grid-template-columns: repeat(2, minmax(0, 1fr));}}
      }}
      hr {{border-color: {C_BORDER};}}
    </style>
    """,
    unsafe_allow_html=True,
)


def _optional_password_gate():
    password = str(_secret_value("APP_PASSWORD", "") or "").strip()
    if not password:
        return
    if st.session_state.get("gps_authenticated", False):
        return

    st.title("GPS Workload Dashboard")
    st.caption("This dashboard is password protected.")
    entered = st.text_input("Password", type="password")
    if st.button("Sign in", type="primary"):
        if hmac.compare_digest(entered, password):
            st.session_state["gps_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


_optional_password_gate()


@st.cache_data(ttl=300, show_spinner=False)
def load_bundle():
    message = refresh_data()
    bundle = snapshot_data()
    if bundle.get("error"):
        raise RuntimeError(bundle["error"])
    return bundle, message


def _perform_api_sync(start_value, end_value):
    """Run the API sync from a Streamlit button and reload Google Sheet data."""
    with st.spinner(
        f"Pulling STATSports API {pd.Timestamp(start_value).strftime('%b %d')}–"
        f"{pd.Timestamp(end_value).strftime('%b %d, %Y')} and updating Raw Sessions…"
    ):
        try:
            message, success = run_api_sync(start_value, end_value)
        except Exception as exc:
            message, success = f"STATSports API sync failed: {exc}", False

    st.session_state["api_sync_notice"] = ("success" if success else "error", message)
    if success:
        st.cache_data.clear()
        st.session_state.pop("exact_report_artifacts", None)
        st.session_state.pop("report_signature", None)
        st.rerun()
    else:
        st.error(message)


def _reset_flag_criteria():
    for key in list(st.session_state.keys()):
        if str(key).startswith("flag_"):
            del st.session_state[key]



# Manual refresh invalidates the 5-minute Streamlit cache.
with st.sidebar:
    st.header("Filters")
    if st.button("Refresh Google Sheets", use_container_width=True):
        st.cache_data.clear()
        st.session_state.pop("exact_report_artifacts", None)
        st.session_state.pop("report_signature", None)
        st.rerun()

try:
    with st.spinner("Loading Google Sheets…"):
        bundle, load_message = load_bundle()
except Exception as exc:
    st.error("The dashboard could not load its data.")
    st.code(str(exc))
    st.info(
        "For Streamlit Cloud, add the two Sheet IDs and the Google service-account "
        "credentials in the app's Secrets settings. See README.md in this repo."
    )
    st.stop()

# Show the result of the most recent API sync after the app reloads.
notice = st.session_state.pop("api_sync_notice", None)
if notice:
    notice_kind, notice_text = notice
    if notice_kind == "success":
        st.success(notice_text)
    else:
        st.error(notice_text)

# Optional API sync controls. These write only missing rows to Raw Sessions and
# then reload the dashboard from Google Sheets so the Sheet remains the source of truth.
latest_sheet_date = latest_practice_date(bundle)
today_date = pd.Timestamp.today().normalize().date()
api_ready = statsports_api_is_configured()

with st.sidebar:
    st.divider()
    st.subheader("STATSports API Sync")
    if latest_sheet_date is None:
        st.caption("Latest Raw Sessions date: no practice data found")
    else:
        st.caption(f"Latest Raw Sessions date: {latest_sheet_date.strftime('%b %d, %Y')}")

    if api_ready:
        if latest_sheet_date is None:
            st.warning("Raw Sessions has no dated rows. Use a custom API pull to seed it.")
        elif latest_sheet_date.date() < today_date:
            gap_days = (today_date - latest_sheet_date.date()).days
            st.warning(
                f"The Sheet ends {gap_days} calendar day(s) before today. "
                "That can mean it is behind, or simply that there were no sessions."
            )
            auto_start = latest_sheet_date.date()
            if st.button("Pull latest Sheet date → today", use_container_width=True, type="primary"):
                _perform_api_sync(auto_start, today_date)
        else:
            st.success("Raw Sessions contains data dated today.")
            if st.button("Recheck today from API", use_container_width=True):
                _perform_api_sync(today_date, today_date)

        with st.expander("Custom API pull", expanded=False):
            default_custom_start = (
                latest_sheet_date.date() if latest_sheet_date is not None else today_date - timedelta(days=13)
            )
            custom_range = st.date_input(
                "API date range",
                value=(default_custom_start, today_date),
                max_value=today_date,
                key="statsports_api_custom_range",
            )
            if isinstance(custom_range, (tuple, list)) and len(custom_range) == 2:
                api_start, api_end = custom_range
            else:
                api_start = api_end = custom_range
            st.caption(
                "The sync is append-only: existing Raw Sessions rows are not rewritten. "
                "Re-pulling a date can add new session/drill rows that were not previously present."
            )
            if st.button("Pull custom range from API", use_container_width=True):
                _perform_api_sync(api_start, api_end)
    else:
        st.info(
            "API sync is not configured yet. Add [statsports_api] credentials to "
            "Streamlit Secrets; they stay out of the public GitHub repo."
        )

min_d, max_d = available_date_bounds(bundle)
# Keep the team selector fixed to the approved organization list, even if a
# particular team has no observations in the currently loaded date range.
all_teams = TEAM_ORDER.copy()

with st.sidebar:
    default_teams = [t for t in all_teams if t != "Washington"] or all_teams
    teams = st.multiselect("Teams", options=all_teams, default=default_teams)

    default_start = max(min_d, max_d - pd.Timedelta(days=13)).date()
    date_value = st.date_input(
        "Date range",
        value=(default_start, max_d.date()),
        min_value=min_d.date(),
        max_value=max_d.date(),
    )
    if isinstance(date_value, (tuple, list)) and len(date_value) == 2:
        start_date, end_date = date_value
    else:
        start_date = end_date = date_value

    # Report downloads are handled in the main Reports section below.

    all_player_keys = eligible_player_keys(bundle, start_date, end_date, teams, selected_keys=None)
    display_map = player_display_map(bundle)
    player_options = sorted(all_player_keys, key=lambda k: display_map.get(k, k))
    selected_players = st.multiselect(
        "Players",
        options=player_options,
        default=[],
        format_func=lambda k: display_map.get(k, k),
        help="Leave blank to include every player on the selected teams.",
    )

    with st.expander("Flagging Criteria", expanded=False):
        st.caption(
            "Defaults match the standalone GPS workload report. Changes apply immediately "
            "to the dashboard and generated PDFs."
        )

        use_acwr = st.checkbox(
            "Use PP_ACWR flags",
            value=DEFAULT_FLAG_CRITERIA["use_acwr"],
            key="flag_use_acwr",
        )
        acwr_monitor, acwr_review = st.slider(
            "ACWR thresholds — Monitor / Review",
            min_value=0.80,
            max_value=2.50,
            value=(DEFAULT_FLAG_CRITERIA["monitor_acwr"], DEFAULT_FLAG_CRITERIA["review_acwr"]),
            step=0.05,
            key="flag_acwr_thresholds",
            disabled=not use_acwr,
        )
        st.caption(
            "ACWR uses the PP_ACWR sheet's ewma_acwr_7_28 value directly. "
            "The app no longer recomputes ACWR from PP_Sprint."
        )

        use_gps_flags = st.checkbox(
            "Use GPS workload outlier flags",
            value=DEFAULT_FLAG_CRITERIA["use_gps_flags"],
            key="flag_use_gps_flags",
        )
        z_monitor, z_review = st.slider(
            "GPS z-score thresholds — Monitor / Review",
            min_value=0.50,
            max_value=4.00,
            value=(DEFAULT_FLAG_CRITERIA["monitor_z"], DEFAULT_FLAG_CRITERIA["review_z"]),
            step=0.10,
            key="flag_z_thresholds",
            disabled=not use_gps_flags,
            help="Sprint Distance, HSR, and Total Distance use same-day Team + Position z-scores. Sprints and Accelerations use rolling baselines when available.",
        )
        rolling_days = st.slider(
            "Rolling baseline window (days)", 7, 28,
            value=DEFAULT_FLAG_CRITERIA["rolling_window_days"], step=1,
            key="flag_rolling_days", disabled=not use_gps_flags,
        )
        rolling_min_sessions = st.slider(
            "Minimum prior sessions for rolling Sprints/Accels", 2, 10,
            value=DEFAULT_FLAG_CRITERIA["rolling_min_sessions"], step=1,
            key="flag_rolling_min_sessions", disabled=not use_gps_flags,
        )
        sprint_delta = st.number_input(
            "Minimum Sprint-count increase to flag", min_value=0.0, max_value=20.0,
            value=float(DEFAULT_FLAG_CRITERIA["rolling_min_delta_sprints"]), step=1.0,
            key="flag_sprint_delta", disabled=not use_gps_flags,
        )
        accel_delta = st.number_input(
            "Minimum Acceleration increase to flag", min_value=0.0, max_value=30.0,
            value=float(DEFAULT_FLAG_CRITERIA["rolling_min_delta_accels"]), step=1.0,
            key="flag_accel_delta", disabled=not use_gps_flags,
        )
        st.caption(
            "Only positive workload spikes flag, matching gps_flags.py. The comparison group is Team + Position; "
            "at least 4 same-day athletes are required for same-day z-scores."
        )

        use_combined_load = st.checkbox(
            "Use combined practice + game load logic",
            value=DEFAULT_FLAG_CRITERIA["use_combined_load"],
            key="flag_use_combined_load",
        )

        use_exposure_flags = st.checkbox(
            "Use sprint / HSR exposure flags",
            value=DEFAULT_FLAG_CRITERIA["use_exposure_flags"],
            key="flag_use_exposure_flags",
        )
        sprint_gap = st.slider(
            "Sprint gap — Needs Exposure after more than (days)", 1, 14,
            value=DEFAULT_FLAG_CRITERIA["max_days_without_sprint"], step=1,
            key="flag_sprint_gap", disabled=not use_exposure_flags,
        )
        hsr_gap = st.slider(
            "HSR gap — Needs Exposure after more than (days)", 1, 14,
            value=DEFAULT_FLAG_CRITERIA["max_days_without_hsr"], step=1,
            key="flag_hsr_gap", disabled=not use_exposure_flags,
        )
        low7_sprint = st.number_input(
            "Minimum 7-day sprint distance (m)", min_value=0.0, max_value=300.0,
            value=float(DEFAULT_FLAG_CRITERIA["low_7d_sprint_dist_m"]), step=5.0,
            key="flag_low7_sprint", disabled=not use_exposure_flags,
        )
        low7_hsr = st.number_input(
            "Minimum 7-day HSR (m)", min_value=0.0, max_value=500.0,
            value=float(DEFAULT_FLAG_CRITERIA["low_7d_hsr_m"]), step=5.0,
            key="flag_low7_hsr", disabled=not use_exposure_flags,
        )

        st.caption("Data Check takes priority when there is no GPS session on the selected end date.")
        st.button("Reset flag criteria", on_click=_reset_flag_criteria, use_container_width=True)

    flag_criteria = {
        **DEFAULT_FLAG_CRITERIA,
        "monitor_acwr": float(acwr_monitor),
        "review_acwr": float(acwr_review),
        "monitor_z": float(z_monitor),
        "review_z": float(z_review),
        "rolling_monitor_z": float(z_monitor),
        "rolling_review_z": float(z_review),
        "rolling_window_days": int(rolling_days),
        "rolling_min_sessions": int(rolling_min_sessions),
        "rolling_min_delta_sprints": float(sprint_delta),
        "rolling_min_delta_accels": float(accel_delta),
        "max_days_without_sprint": int(sprint_gap),
        "max_days_without_hsr": int(hsr_gap),
        "low_7d_sprint_dist_m": float(low7_sprint),
        "low_7d_hsr_m": float(low7_hsr),
        "use_acwr": bool(use_acwr),
        "use_gps_flags": bool(use_gps_flags),
        "use_combined_load": bool(use_combined_load),
        "use_exposure_flags": bool(use_exposure_flags),
    }


    st.divider()
    st.caption(load_message)
    if bundle.get("source"):
        st.caption(f"Source: {bundle['source']}")

keys = eligible_player_keys(
    bundle,
    start_date,
    end_date,
    teams,
    selected_keys=selected_players or None,
)
status = build_status_table(bundle, end_date, keys, criteria=flag_criteria)
period = build_period_summary(bundle, start_date, end_date, keys)

st.title("GPS Workload Dashboard")
st.markdown(
    f'<div class="gps-subtle">{pd.Timestamp(start_date).strftime("%b %d, %Y")} — '
    f'{pd.Timestamp(end_date).strftime("%b %d, %Y")} · '
    f'{len(teams)} team(s) · {len(keys)} athlete(s)</div>',
    unsafe_allow_html=True,
)

# Reports use the supplied standalone structure: one team, one report date,
# full current Master Roster. Dashboard player filters do not alter report rosters.
st.markdown("### Reports")
st.caption(
    "Build the bulk team reports, then use the selector immediately underneath to download one affiliate PDF."
)

# Keep the bulk build tied to the sidebar selection. If nothing is selected,
# fall back to the full organization so report controls remain usable.
bulk_report_teams = ordered_teams(teams or TEAM_ORDER)
report_date_label = pd.Timestamp(end_date).strftime('%b %d, %Y')

st.caption(
    f"Bulk build teams: {', '.join(bulk_report_teams)} · Report date: {report_date_label}"
)

# Primary bulk build action.
if st.button(
    "Build Team Reports",
    type="primary",
    use_container_width=True,
    key="build_team_reports_v6_20260812_1703",
):
    with st.spinner("Building team PDFs…"):
        try:
            built = build_exact_reports(bundle, bulk_report_teams, end_date)
            if built["mode"] == "single":
                results = [built["result"]]
                errors = built.get("errors", [])
            else:
                results = built.get("results", [])
                errors = built.get("errors", [])

            team_results = {
                r["team"]: r for r in results
                if r.get("pdf_bytes") is not None
            }

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for team_name, result in team_results.items():
                    zf.writestr(result["pdf_name"], result["pdf_bytes"])
                if errors:
                    zf.writestr("report_errors.txt", "\n".join(errors))

            st.session_state["team_reports_artifact_v6"] = {
                "teams": tuple(bulk_report_teams),
                "date": str(end_date),
                "team_results": team_results,
                "zip_bytes": zip_buffer.getvalue(),
                "zip_name": f"GPS_Workload_Reports_{pd.Timestamp(end_date).strftime('%Y-%m-%d')}.zip",
                "errors": errors,
            }
            st.success(f"Built {len(team_results)} team PDF report(s).")
        except Exception as exc:
            st.session_state.pop("team_reports_artifact_v6", None)
            st.error(f"Team report build failed: {exc}")

# IMPORTANT: this box is deliberately immediately below Build Team Reports.
# It is always rendered and is independent of the dashboard's player filter.
with st.container(border=True):
    st.markdown("#### TEAM REPORT DOWNLOAD")
    st.caption("Choose the affiliate whose PDF you want to download.")
    report_download_team = st.selectbox(
        "Team",
        options=TEAM_ORDER.copy(),
        index=TEAM_ORDER.index(bulk_report_teams[0]) if bulk_report_teams and bulk_report_teams[0] in TEAM_ORDER else 0,
        key="report_download_team_v6_20260812_1703",
    )

    artifact = st.session_state.get("team_reports_artifact_v6")
    selected_result = None
    if artifact and artifact.get("date") == str(end_date):
        selected_result = artifact.get("team_results", {}).get(report_download_team)

    # If this team was not part of the bulk build, allow building just this one
    # directly from the same selector rather than forcing sidebar changes.
    if selected_result is None:
        if st.button(
            f"Build {report_download_team} PDF",
            use_container_width=True,
            key="build_selected_team_pdf_v6_20260812_1703",
        ):
            with st.spinner(f"Building {report_download_team} PDF…"):
                try:
                    one = build_exact_reports(bundle, [report_download_team], end_date)
                    if one["mode"] == "single":
                        selected_result = one["result"]
                    else:
                        matches = [r for r in one.get("results", []) if r.get("team") == report_download_team]
                        selected_result = matches[0] if matches else None
                    if selected_result and selected_result.get("pdf_bytes") is not None:
                        st.session_state["single_team_report_v6"] = {
                            "team": report_download_team,
                            "date": str(end_date),
                            "result": selected_result,
                        }
                        st.success(f"{report_download_team} PDF is ready below.")
                    else:
                        st.error(f"{report_download_team} PDF could not be created.")
                except Exception as exc:
                    st.error(f"{report_download_team} report build failed: {exc}")

        cached_one = st.session_state.get("single_team_report_v6")
        if (
            cached_one
            and cached_one.get("team") == report_download_team
            and cached_one.get("date") == str(end_date)
        ):
            selected_result = cached_one.get("result")

    if selected_result and selected_result.get("pdf_bytes") is not None:
        st.download_button(
            f"Download {report_download_team} PDF",
            data=selected_result["pdf_bytes"],
            file_name=selected_result["pdf_name"],
            mime="application/pdf",
            use_container_width=True,
            key="download_selected_team_pdf_v6_20260812_1703",
        )
    else:
        st.caption(
            "Either click Build Team Reports above, or click the selected team's Build PDF button here."
        )

artifact = st.session_state.get("team_reports_artifact_v6")
if artifact and artifact.get("date") == str(end_date) and artifact.get("zip_bytes"):
    st.download_button(
        "Download Bulk Team Reports (ZIP)",
        data=artifact["zip_bytes"],
        file_name=artifact["zip_name"],
        mime="application/zip",
        use_container_width=True,
        key="download_all_team_reports_zip_v6_20260812_1703",
    )
    if artifact.get("errors"):
        st.warning("Some team reports failed: " + " | ".join(artifact["errors"]))

st.caption(
    "Reports use the supplied standalone GPS reporting structure and the current Python Reports → Master Roster. "
    "The selected end date is used as the report date."
)

if not keys:
    st.warning("No players match the selected filters.")
    st.stop()

counts = status["Status"].value_counts() if not status.empty else pd.Series(dtype=int)

# A dashboard should answer "who needs attention?" before it asks the user to
# interpret a dense table or a spaghetti chart.
status_specs = [
    ("Review", "Prioritize today", C_RED),
    ("Monitor", "Watch closely", C_AMBER),
    ("Needs Exposure", "Rolling exposure gap", C_BLUE),
    ("Prepared", "Normal workload", C_GREEN),
    ("Data Check", "Missing / no session", C_GRAY),
]
status_cards = []
for name, subtitle, color in status_specs:
    status_cards.append(
        f'<div class="status-card" style="border-top:3px solid {color};">'
        f'<div class="status-card-label">{name}</div>'
        f'<div class="status-card-value" style="color:{color};">{int(counts.get(name, 0))}</div>'
        f'<div class="status-card-sub">{subtitle}</div>'
        '</div>'
    )
st.markdown('<div class="status-grid">' + ''.join(status_cards) + '</div>', unsafe_allow_html=True)

overview_tab, player_tab, summary_tab = st.tabs(
    ["Overview", "Player Detail", "Period Summary"]
)

metric_lookup = {metric: (title, unit, agg) for metric, title, unit, agg in TREND_METRICS}
metric_keys = list(metric_lookup)

with overview_tab:
    st.markdown('<div class="section-kicker">Decision view</div>', unsafe_allow_html=True)
    st.subheader("Athletes Requiring Attention")

    if status.empty:
        st.info("No status rows for this selection.")
    else:
        attention = status[status["Status"].isin(["Review", "Monitor", "Needs Exposure"])].copy()
        attention_cols = [
            "Athlete", "Team", "Pos", "Status", "Primary Driver", "ACWR", "Last Sprint"
        ]
        if attention.empty:
            st.success("No Review, Monitor, or Needs Exposure athletes in the current selection.")
        else:
            attention_show = attention[attention_cols].copy()
            attention_show["ACWR"] = pd.to_numeric(attention_show["ACWR"], errors="coerce").round(2)
            st.dataframe(
                attention_show,
                use_container_width=True,
                hide_index=True,
                height=min(430, 42 + 36 * len(attention_show)),
            )

        with st.expander(f"Full athlete snapshot ({len(status)} athletes)", expanded=False):
            show_cols = [
                "Athlete", "Team", "Pos", "Status", "Primary Driver", "Combined Load",
                "ACWR", "Last Game Load", "Practice Load (Prev Day)", "Last Sprint",
            ]
            show = status[show_cols].copy()
            show["ACWR"] = pd.to_numeric(show["ACWR"], errors="coerce").round(2)
            st.dataframe(show, use_container_width=True, hide_index=True, height=520)

    st.divider()
    st.markdown('<div class="section-kicker">Team context</div>', unsafe_allow_html=True)
    st.subheader("Team Workload Distribution")
    st.caption(
        "The line is the team median. The shaded band is the middle 50% of athletes, "
        "so you can see the normal daily range without drawing every player on top of each other."
    )

    overview_team_options = [
        team for team in ordered_teams(teams)
        if eligible_player_keys(bundle, start_date, end_date, [team], selected_keys=None)
    ]
    if not overview_team_options:
        overview_team_options = [
            team for team in TEAM_ORDER
            if eligible_player_keys(bundle, start_date, end_date, [team], selected_keys=None)
        ]

    if overview_team_options:
        c_team, c_metric = st.columns([1.0, 1.25])
        with c_team:
            overview_team = st.selectbox(
                "Team",
                options=overview_team_options,
                index=0,
                key="overview_context_team_v5",
            )
        with c_metric:
            overview_metric = st.selectbox(
                "Metric",
                options=metric_keys,
                index=metric_keys.index("combined_total_distance_m")
                if "combined_total_distance_m" in metric_keys else 0,
                format_func=lambda m: metric_lookup[m][0],
                key="overview_context_metric_v5",
            )
        overview_title, overview_unit, _ = metric_lookup[overview_metric]
        st.plotly_chart(
            team_context_trend_figure(
                bundle,
                start_date,
                end_date,
                overview_team,
                overview_metric,
                overview_title,
                overview_unit,
                focus_player_key=None,
                criteria=flag_criteria,
                show_middle_50=True,
            ),
            use_container_width=True,
            config={"displaylogo": False, "displayModeBar": False, "responsive": True},
        )
    else:
        st.info("No team trend data are available for the selected period.")

with player_tab:
    st.markdown('<div class="section-kicker">Individual interpretation</div>', unsafe_allow_html=True)
    st.subheader("Player vs Team Context")
    st.caption(
        "Pick one athlete to emphasize. The team stays in the background as a median + middle-50% range, "
        "instead of putting the entire roster on the same chart."
    )

    trend_team_options = [
        team for team in TEAM_ORDER
        if eligible_player_keys(bundle, start_date, end_date, [team], selected_keys=None)
    ]

    if not trend_team_options:
        st.info("No player trend data are available for this period.")
    else:
        preferred_team = next((t for t in teams if t in trend_team_options), trend_team_options[0])
        c_team, c_player, c_metric = st.columns([1.0, 1.45, 1.15])
        with c_team:
            trend_reference_team = st.selectbox(
                "Reference team",
                options=trend_team_options,
                index=trend_team_options.index(preferred_team),
                key="trend_reference_team_v5",
            )

        team_player_options = eligible_player_keys(
            bundle, start_date, end_date, [trend_reference_team], selected_keys=None
        )
        team_player_options = sorted(team_player_options, key=lambda k: display_map.get(k, k))

        with c_player:
            focus_player = st.selectbox(
                "Focus athlete",
                options=team_player_options,
                index=0,
                format_func=lambda k: display_map.get(k, k),
                key=f"trend_focus_player_{re.sub(r'[^a-z0-9]+', '_', trend_reference_team.casefold()).strip('_')}_v5",
            )
        with c_metric:
            metric = st.selectbox(
                "Metric",
                options=metric_keys,
                index=metric_keys.index("combined_total_distance_m")
                if "combined_total_distance_m" in metric_keys else 0,
                format_func=lambda m: metric_lookup[m][0],
                key="trend_focus_metric_v5",
            )

        trend_title, trend_unit, _ = metric_lookup[metric]
        focus_context = latest_focus_metric_context(
            bundle, start_date, end_date, trend_reference_team, focus_player, metric
        )
        focus_status = build_status_table(bundle, end_date, [focus_player], criteria=flag_criteria)
        focus_status_text = "—"
        focus_driver = "No end-date status"
        if not focus_status.empty:
            focus_status_text = str(focus_status.iloc[0].get("Status", "—"))
            focus_driver = str(focus_status.iloc[0].get("Primary Driver", ""))

        status_color = STATUS_COLORS.get(focus_status_text, C_GRAY)
        context_bits = [f"{trend_reference_team} reference"]
        if focus_context:
            context_bits.append(f"latest {focus_context['date'].strftime('%b %d')}")
            if pd.notna(focus_context["team_median"]):
                context_bits.append(f"team n={focus_context['team_n']}")
        st.markdown(
            f'<div class="focus-panel" style="border-left:4px solid {status_color};">'
            f'<div class="focus-name">{display_map.get(focus_player, focus_player)}</div>'
            f'<div class="focus-meta"><b style="color:{status_color};">{focus_status_text}</b> · '
            f'{focus_driver} · {" · ".join(context_bits)}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        if focus_context:
            latest_value = focus_context["value"]
            team_med = focus_context["team_median"]
            delta = focus_context["delta_vs_median"]
            pct = focus_context["percentile"]
            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric(f"Latest {trend_title}", format_trend_value(latest_value, trend_unit))
            with m2:
                st.metric(
                    "Team median same day",
                    format_trend_value(team_med, trend_unit),
                )
            with m3:
                st.metric(
                    "Vs team median",
                    format_trend_value(delta, trend_unit, signed=True),
                )
            with m4:
                st.metric(
                    "Same-day percentile",
                    f"{pct:.0f}th" if pd.notna(pct) else "—",
                )

        st.plotly_chart(
            team_context_trend_figure(
                bundle,
                start_date,
                end_date,
                trend_reference_team,
                metric,
                trend_title,
                trend_unit,
                focus_player_key=focus_player,
                criteria=flag_criteria,
                show_middle_50=True,
            ),
            use_container_width=True,
            config={"displaylogo": False, "displayModeBar": False, "responsive": True},
        )

        with st.expander("Compare multiple athletes", expanded=False):
            st.caption(
                "Use this only when you actually want direct player-to-player comparison. "
                "Keeping the selection to roughly 2–6 athletes is the most readable."
            )
            all_compare_options = eligible_player_keys(
                bundle, start_date, end_date, TEAM_ORDER, selected_keys=None
            )
            all_compare_options = sorted(all_compare_options, key=lambda k: display_map.get(k, k))
            roster = bundle.get("roster", pd.DataFrame())
            roster_team_map = (
                roster.set_index("player_key")["roster_team"].to_dict()
                if roster is not None and not roster.empty and "roster_team" in roster.columns
                else {}
            )
            compare_players = st.multiselect(
                "Athletes to compare",
                options=all_compare_options,
                default=[focus_player],
                format_func=lambda k: (
                    f"{display_map.get(k, k)} · {roster_team_map.get(k, '')}"
                    if roster_team_map.get(k, "") else display_map.get(k, k)
                ),
                key="trend_compare_players_v5",
                placeholder="Add athletes",
            )
            if len(compare_players) > 8:
                st.warning(
                    "This selection will be visually dense. The focus-athlete view above is better for interpretation; "
                    "use this chart when the direct overlay itself is the goal."
                )
            if compare_players:
                show_compare_average = st.toggle(
                    "Show selected-player average",
                    value=False,
                    key="trend_compare_average_v5",
                )
                st.plotly_chart(
                    comparison_trend_figure(
                        bundle,
                        start_date,
                        end_date,
                        compare_players,
                        [],
                        metric,
                        trend_title,
                        trend_unit,
                        show_team_average=False,
                        show_selected_average=show_compare_average,
                        team_average_exclude_keys=[],
                        criteria=flag_criteria,
                        restrict_player_teams=None,
                        legend_mode="all",
                    ),
                    use_container_width=True,
                    config={"displaylogo": False, "displayModeBar": False, "responsive": True},
                )

with summary_tab:
    st.subheader("Selected-Period Player Totals")
    if period.empty:
        st.info("No period summary is available for this selection.")
    else:
        rename = {
            "Practice_Sessions": "Practice Sessions",
            "Practice_Days": "Practice Days",
            "Game_Days": "Game Days",
            "Total_Distance_m": "Total Distance (m)",
            "HSR_m": "HSR (m)",
            "Max_Effort_Game_m": "Max-Effort Game (m)",
            "Top_Speed_mps": "Top Speed (m/s)",
            "Top_Speed_mph": "Top Speed (mph)",
            "Duration_min": "Duration (min)",
        }
        show_period = period.drop(columns=["Player Key"], errors="ignore").rename(columns=rename)
        st.dataframe(show_period, use_container_width=True, hide_index=True, height=580)
        st.download_button(
            "Download period summary CSV",
            data=show_period.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"GPS_Period_Summary_{pd.Timestamp(start_date).strftime('%Y%m%d')}_to_"
                f"{pd.Timestamp(end_date).strftime('%Y%m%d')}.csv"
            ),
            mime="text/csv",
        )

st.divider()
active_criteria_parts = []
if flag_criteria["use_acwr"]:
    active_criteria_parts.append(
        f'PP_ACWR Monitor ≥ {flag_criteria["monitor_acwr"]:.2f}, Review ≥ {flag_criteria["review_acwr"]:.2f}'
    )
if flag_criteria["use_gps_flags"]:
    active_criteria_parts.append(
        f'GPS Team + Position z Monitor ≥ {flag_criteria["monitor_z"]:.1f}, Review ≥ {flag_criteria["review_z"]:.1f}; '
        f'{flag_criteria["rolling_window_days"]}d rolling Sprints/Accels'
    )
if flag_criteria["use_combined_load"]:
    active_criteria_parts.append("combined practice + game load")
if flag_criteria["use_exposure_flags"]:
    active_criteria_parts.append(
        f'sprint gap > {flag_criteria["max_days_without_sprint"]}d, HSR gap > {flag_criteria["max_days_without_hsr"]}d, '
        f'7d sprint < {flag_criteria["low_7d_sprint_dist_m"]:.0f}m, 7d HSR < {flag_criteria["low_7d_hsr_m"]:.0f}m'
    )

st.caption(
    "Active flagging criteria: "
    + (" · ".join(active_criteria_parts) if active_criteria_parts else "No optional flag criteria enabled.")
)
