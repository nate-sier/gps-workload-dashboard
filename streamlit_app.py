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

from matplotlib.backends.backend_pdf import PdfPages
import matplotlib.pyplot as plt


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
ROSTER_TAB = os.getenv("ROSTER_TAB", str(_secret_value("ROSTER_TAB", "Master Roster")))
PP_SPRINT_TAB = os.getenv("PP_SPRINT_TAB", str(_secret_value("PP_SPRINT_TAB", "PP_Sprint")))

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
ALLOW_LOCAL_EXCEL_FALLBACK = os.getenv("ALLOW_LOCAL_EXCEL_FALLBACK", "1") != "0"


# =============================================================================
# STATUS SETTINGS — standalone GPS report flag engine defaults
# =============================================================================

# ACWR zones. ACWR is an EWMA 7:28 ratio computed from PP_Sprint max-effort
# game distance, stepping once per game appearance rather than once per calendar day.
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


def _read_local_excel(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_excel(path, sheet_name=sheet_name)


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
        return practice, roster, pp, "Google Sheets"
    except Exception as exc:
        google_error = exc

    if ALLOW_LOCAL_EXCEL_FALLBACK:
        try:
            practice = _read_local_excel(LOCAL_STATSPORTS_XLSX, STATSPORTS_TAB)
            roster = _read_local_excel(LOCAL_REPORTS_XLSX, ROSTER_TAB)
            pp = _read_local_excel(LOCAL_REPORTS_XLSX, PP_SPRINT_TAB)
            return practice, roster, pp, f"Local Excel fallback ({google_error})"
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



def add_game_flag_context(games_daily: pd.DataFrame) -> pd.DataFrame:
    """Add standalone-report game-load classes and running EWMA ACWR.

    Game load is classified against each athlete's OWN expanding prior-game
    baseline. ACWR is calculated from max-effort game distance with 7- and
    28-observation EWMA smoothing, stepping on game appearances only.
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
    """Expand athletes to calendar days for charting while preserving event-based ACWR.

    The standalone report's ACWR advances only on game appearances. Calendar
    rows therefore carry the most recent game EWMA forward for snapshot context;
    ACWR trend charts later filter back to actual game-observed dates.
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
        ]
        for c in fill_zero:
            if c not in merged.columns:
                merged[c] = 0.0
            merged[c] = safe_num(merged[c], fill=0)

        for c in ["top_speed_ms", "max_accel_ms2", "game_acwr"]:
            if c not in merged.columns:
                merged[c] = np.nan
            merged[c] = safe_num(merged[c])

        if "game_load_class" not in merged.columns:
            merged["game_load_class"] = ""
        merged["game_load_class"] = merged["game_load_class"].fillna("").astype(str)

        # Carry the event-based value for snapshot context only. Trend plotting
        # filters ACWR to game-observed rows so this does not create a fake daily series.
        merged["acwr"] = merged["game_acwr"].ffill()
        pieces.append(merged)

    out = pd.concat(pieces, ignore_index=True)
    return out.sort_values(["player_key", "date"]).reset_index(drop=True)



def refresh_data():
    """Reload all sources and rebuild derived data. Returns a human-readable status string."""
    try:
        practice_raw, roster_raw, pp_raw, source = load_source_frames()
        roster = clean_roster(roster_raw)
        raw_practice, practice_daily = clean_practice(practice_raw, roster)
        games_daily = add_game_flag_context(clean_games(pp_raw, roster))
        daily = combine_daily(practice_daily, games_daily, roster)
        history = build_history_calendar(daily, roster)

        loaded_at = datetime.now()
        with _DATA_LOCK:
            DATA.update({
                "raw_practice_source": practice_raw,
                "raw_practice": raw_practice,
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
    roster = bundle.get("roster", pd.DataFrame()).copy()
    display = player_display_map(bundle)

    if not practice.empty:
        practice["date"] = pd.to_datetime(practice["date"], errors="coerce").dt.normalize()
    if not games.empty:
        games["date"] = pd.to_datetime(games["date"], errors="coerce").dt.normalize()

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
        rec["acwr"] = pd.to_numeric(grow.get("game_acwr"), errors="coerce") if grow is not None else np.nan
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
    ("acwr", "PP_Sprint EWMA ACWR", "ratio", "last"),
]


def empty_figure(title="No data"):
    fig = go.Figure()
    fig.update_layout(**PLOT_LAYOUT, height=330, title=dict(text=title, x=0))
    return fig


def _trend_rows_for_metric(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Keep only true event observations for trends.

    GPS metrics use practice-observed dates. EWMA ACWR uses game-observed dates,
    matching the standalone report's game-appearance (not calendar-day) updates.
    """
    if df is None or df.empty or metric not in df.columns:
        return pd.DataFrame()
    p = df.copy().sort_values("date")
    if metric == "acwr" and "game_observed" in p.columns:
        p = p[safe_num(p["game_observed"], 0) > 0].copy()
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
# PDF EXPORT
# =============================================================================

def _pdf_text(ax, x, y, text, size=10, weight="normal", color="#172033", ha="left", va="top"):
    ax.text(x, y, text, transform=ax.transAxes, fontsize=size, fontweight=weight,
            color=color, ha=ha, va=va)


def _draw_pdf_table(ax, df, cols, max_rows=28):
    ax.axis("off")
    if df is None or df.empty:
        _pdf_text(ax, 0.02, 0.95, "No rows found for the selected filters.", 10, color=C_MUTED)
        return
    show = df[cols].head(max_rows).copy()
    cell_text = []
    for _, row in show.iterrows():
        vals = []
        for c in cols:
            v = row[c]
            if c == "ACWR":
                vals.append("—" if pd.isna(v) else f"{float(v):.2f}")
            else:
                vals.append(str(v))
        cell_text.append(vals)
    table = ax.table(
        cellText=cell_text,
        colLabels=cols,
        loc="upper left",
        cellLoc="left",
        colLoc="left",
        bbox=[0.0, 0.02, 1.0, 0.95],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(7.2)
    for (r, c), cell in table.get_celld().items():
        if r == 0:
            cell.set_facecolor(C_NAVY)
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
        else:
            cell.set_facecolor("#FFFFFF" if r % 2 else "#F7F9FC")
        cell.set_edgecolor(C_BORDER)
        cell.set_linewidth(0.5)


def make_pdf_bytes(bundle, start_date, end_date, teams, player_keys, criteria=None) -> bytes:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    criteria = {**DEFAULT_FLAG_CRITERIA, **(criteria or {})}
    status = build_status_table(bundle, end, player_keys, criteria=criteria)
    period = build_period_summary(bundle, start, end, player_keys)
    history = selected_history(bundle, start, end, player_keys)

    output = io.BytesIO()
    with PdfPages(output) as pdf:
        # ------------------------------------------------------------------
        # Cover / summary
        # ------------------------------------------------------------------
        fig = plt.figure(figsize=(11, 8.5))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0.90), 1, 0.10, transform=ax.transAxes, color=C_NAVY))
        ax.add_patch(plt.Rectangle((0, 0.90), 0.012, 0.10, transform=ax.transAxes, color=C_RED))
        _pdf_text(ax, 0.04, 0.965, "GPS WORKLOAD REPORT", 20, "bold", "white", va="center")
        _pdf_text(ax, 0.04, 0.925, "Washington Nationals — Player Development S&C", 9, color="white", va="center")

        team_text = ", ".join(teams) if teams else "All Teams"
        _pdf_text(ax, 0.04, 0.855, f"Selected period: {start.strftime('%b %d, %Y')} — {end.strftime('%b %d, %Y')}", 12, "bold", C_TEXT)
        _pdf_text(ax, 0.04, 0.825, f"Teams: {team_text}", 9, color=C_MUTED)
        _pdf_text(ax, 0.04, 0.800, f"Athletes: {len(player_keys)}", 9, color=C_MUTED)
        _pdf_text(ax, 0.04, 0.775, "Status snapshot is calculated as of the selected end date.", 8, color=C_MUTED)
        active_rules = []
        if criteria["use_acwr"]:
            active_rules.append(f'Game EWMA ACWR Monitor≥{criteria["monitor_acwr"]:.2f} / Review≥{criteria["review_acwr"]:.2f}')
        if criteria["use_gps_flags"]:
            active_rules.append(
                f'GPS Team+Position z Monitor≥{criteria["monitor_z"]:.1f} / Review≥{criteria["review_z"]:.1f}; '
                f'{criteria["rolling_window_days"]}d rolling Sprints/Accels'
            )
        if criteria["use_combined_load"]:
            active_rules.append('Combined practice+game load')
        if criteria["use_exposure_flags"]:
            active_rules.append(
                f'Sprint gap>{criteria["max_days_without_sprint"]}d / HSR gap>{criteria["max_days_without_hsr"]}d; '
                f'7d sprint<{criteria["low_7d_sprint_dist_m"]:.0f}m / HSR<{criteria["low_7d_hsr_m"]:.0f}m'
            )
        criteria_text = " · ".join(active_rules) if active_rules else "No optional flag rules enabled"
        _pdf_text(ax, 0.04, 0.750, f"Active criteria: {criteria_text}", 7, color=C_MUTED)

        counts = status["Status"].value_counts() if not status.empty else pd.Series(dtype=int)
        card_names = ["Review", "Monitor", "Needs Exposure", "Prepared", "Data Check"]
        x0, y0, w, h, gap = 0.04, 0.66, 0.17, 0.09, 0.018
        for i, name in enumerate(card_names):
            x = x0 + i * (w + gap)
            ax.add_patch(plt.Rectangle((x, y0), w, h, transform=ax.transAxes,
                                       facecolor="#FFFFFF", edgecolor=C_BORDER, linewidth=1))
            _pdf_text(ax, x + 0.012, y0 + 0.068, name.upper(), 7.5, "bold", C_MUTED)
            _pdf_text(ax, x + 0.012, y0 + 0.045, str(int(counts.get(name, 0))), 18, "bold", STATUS_COLORS[name])

        total_distance = period["Total_Distance_m"].sum() if not period.empty else 0
        total_hsr = period["HSR_m"].sum() if not period.empty else 0
        total_acc = period["Accelerations"].sum() if not period.empty else 0
        total_sprints = period["Sprints"].sum() if not period.empty else 0
        _pdf_text(ax, 0.04, 0.60, "SELECTED-PERIOD TOTALS", 8, "bold", C_MUTED)
        metrics = [
            ("Distance", f"{total_distance:,.0f} m"),
            ("HSR", f"{total_hsr:,.0f} m"),
            ("Accelerations", f"{total_acc:,.0f}"),
            ("Sprints", f"{total_sprints:,.0f}"),
        ]
        for i, (label, value) in enumerate(metrics):
            x = 0.04 + i * 0.23
            _pdf_text(ax, x, 0.555, value, 16, "bold", C_NAVY)
            _pdf_text(ax, x, 0.525, label, 8, color=C_MUTED)

        table_ax = fig.add_axes([0.04, 0.06, 0.92, 0.42])
        _draw_pdf_table(
            table_ax,
            status,
            ["Athlete", "Pos", "Status", "Primary Driver", "Combined Load", "ACWR", "Last Game Load", "Last Sprint"],
            max_rows=24,
        )
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # Additional status table pages if needed.
        if len(status) > 24:
            for start_row in range(24, len(status), 30):
                fig = plt.figure(figsize=(11, 8.5))
                ax = fig.add_axes([0.05, 0.06, 0.90, 0.88])
                _pdf_text(ax, 0.0, 1.02, "Status Snapshot — Continued", 15, "bold", C_NAVY)
                _draw_pdf_table(
                    ax,
                    status.iloc[start_row:start_row + 30],
                    ["Athlete", "Pos", "Status", "Primary Driver", "Combined Load", "ACWR", "Last Game Load", "Last Sprint"],
                    max_rows=30,
                )
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

        # ------------------------------------------------------------------
        # One page per athlete
        # ------------------------------------------------------------------
        display = player_display_map(bundle)
        status_by_key = status.set_index("Player Key").to_dict("index") if not status.empty else {}

        for key in player_keys:
            p = history[history["player_key"] == key].sort_values("date").copy()
            row = status_by_key.get(key, {})
            name = row.get("Athlete", display.get(key, key))
            team = row.get("Team", "")
            pos = row.get("Pos", "")
            status_name = row.get("Status", "Data Check")
            driver = row.get("Primary Driver", "")
            acwr = row.get("ACWR", np.nan)

            fig, axes = plt.subplots(4, 2, figsize=(11, 8.5))
            fig.subplots_adjust(top=0.82, left=0.08, right=0.97, hspace=0.62, wspace=0.24)
            fig.text(0.06, 0.965, f"{name.upper()} — GPS TRENDS", fontsize=17, fontweight="bold", color=C_NAVY, va="top")
            fig.text(0.06, 0.932, f"{team} · {pos} · {start.strftime('%b %d')}–{end.strftime('%b %d, %Y')}", fontsize=9, color=C_MUTED, va="top")
            acwr_text = "—" if pd.isna(acwr) else f"{float(acwr):.2f}"
            fig.text(0.06, 0.902, f"ACWR: {acwr_text}   ·   {driver}", fontsize=9, color=C_TEXT, va="top")
            fig.text(0.94, 0.932, status_name.upper(), fontsize=10, fontweight="bold", color=STATUS_COLORS.get(status_name, C_GRAY), ha="right", va="top")

            for ax, (metric, title, unit, _) in zip(axes.flat, TREND_METRICS):
                ax.set_title(title, loc="left", fontsize=9, fontweight="bold", color=C_NAVY)
                if p.empty or metric not in p.columns or safe_num(p[metric]).dropna().empty:
                    ax.text(0.5, 0.5, "No data", ha="center", va="center", color=C_MUTED, transform=ax.transAxes)
                    ax.set_axis_off()
                    continue
                y = safe_num(p[metric])
                ax.plot(p["date"], y, linewidth=1.6, marker="o", markersize=2.8, color=C_NAVY)
                if metric == "acwr":
                    ax.axhline(criteria["optimal_low_acwr"], linestyle="--", linewidth=0.8, color=C_BLUE)
                    if criteria["use_acwr"]:
                        ax.axhline(criteria["monitor_acwr"], linestyle="--", linewidth=0.8, color=C_AMBER)
                        ax.axhline(criteria["review_acwr"], linestyle="--", linewidth=0.8, color=C_RED)
                ax.grid(axis="y", alpha=0.18)
                ax.tick_params(labelsize=6)
                for spine in ["top", "right"]:
                    ax.spines[spine].set_visible(False)
                ax.set_ylabel(unit, fontsize=6, color=C_MUTED)

            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    output.seek(0)
    return output.getvalue()




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
        st.session_state.pop("pdf_bytes", None)
        st.session_state.pop("pdf_signature", None)
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
        st.session_state.pop("pdf_bytes", None)
        st.session_state.pop("pdf_signature", None)
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
            "Use PP_Sprint EWMA ACWR flags",
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
            "ACWR is an EWMA 7:28 ratio from PP_Sprint max-effort game distance. "
            "It advances on game appearances only; practice GPS is not part of ACWR."
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

# Invalidate an old PDF when the selection changes.
pdf_signature = (
    str(start_date),
    str(end_date),
    tuple(sorted(teams or [])),
    tuple(sorted(keys)),
    tuple(sorted(flag_criteria.items())),
)
if st.session_state.get("pdf_signature") != pdf_signature:
    st.session_state.pop("pdf_bytes", None)
    st.session_state["pdf_signature"] = pdf_signature

pdf_col, note_col = st.columns([1, 3])
with pdf_col:
    if st.button("Build PDF for selection", type="primary", use_container_width=True):
        with st.spinner("Building PDF…"):
            st.session_state["pdf_bytes"] = make_pdf_bytes(
                bundle, start_date, end_date, teams or [], keys, criteria=flag_criteria
            )
with note_col:
    st.caption(
        "The PDF uses the selected teams, players, and date range. "
        "It includes the end-of-period status snapshot plus one trend page per athlete."
    )

if st.session_state.get("pdf_bytes"):
    filename = (
        f"GPS_Workload_Report_{pd.Timestamp(start_date).strftime('%Y%m%d')}_to_"
        f"{pd.Timestamp(end_date).strftime('%Y%m%d')}.pdf"
    )
    st.download_button(
        "Download PDF",
        data=st.session_state["pdf_bytes"],
        file_name=filename,
        mime="application/pdf",
        use_container_width=False,
    )

if not keys:
    st.warning("No players match the selected filters.")
    st.stop()

counts = status["Status"].value_counts() if not status.empty else pd.Series(dtype=int)
cols = st.columns(5)
for col, name, subtitle in zip(
    cols,
    ["Review", "Monitor", "Needs Exposure", "Prepared", "Data Check"],
    ["Prioritize", "Watch closely", "Rolling gap", "Normal workload", "Missing end-date data"],
):
    with col:
        st.metric(name, int(counts.get(name, 0)), help=subtitle)

overview_tab, player_tab, summary_tab = st.tabs(
    ["Overview", "Player Trends", "Period Summary"]
)

with overview_tab:
    st.subheader("Athlete Snapshot")
    if status.empty:
        st.info("No status rows for this selection.")
    else:
        show_cols = [
            "Athlete", "Team", "Pos", "Status", "Primary Driver", "Combined Load",
            "ACWR", "Last Game Load", "Practice Load (Prev Day)", "Last Sprint",
        ]
        show = status[show_cols].copy()
        show["ACWR"] = pd.to_numeric(show["ACWR"], errors="coerce").round(2)
        st.dataframe(show, use_container_width=True, hide_index=True, height=520)

    st.subheader("Team Workload Trend")
    st.plotly_chart(
        team_period_figure(bundle, start_date, end_date, keys),
        use_container_width=True,
        config={"displaylogo": False},
    )

with player_tab:
    st.subheader("Player Trends")
    st.caption(
        "Select team(s) to load their players, then remove anyone with bad data or add players "
        "from another team. Individual player lines and average membership are controlled separately."
    )

    # Player Trends is intentionally independent of the sidebar team/player filters.
    trend_team_options = [
        team for team in TEAM_ORDER
        if eligible_player_keys(bundle, start_date, end_date, [team], selected_keys=None)
    ]

    if hasattr(st, "pills"):
        trend_selected_teams = st.pills(
            "Team quick-select",
            options=trend_team_options,
            selection_mode="multi",
            default=[],
            key="trend_team_pills_clean_v4",
            help="Select one or more teams. Their eligible position players are loaded into the player selector below.",
        ) or []
    else:
        trend_selected_teams = st.multiselect(
            "Team quick-select",
            options=trend_team_options,
            default=[],
            key="trend_team_multiselect_clean_v4",
            placeholder="Select team(s)",
        )

    trend_scope_keys = eligible_player_keys(
        bundle, start_date, end_date, TEAM_ORDER, selected_keys=None
    )
    all_player_options = sorted(trend_scope_keys, key=lambda k: display_map.get(k, k))

    seeded_players = []
    for team in trend_selected_teams:
        seeded_players.extend(
            eligible_player_keys(bundle, start_date, end_date, [team], selected_keys=None)
        )
    seeded_players = sorted(set(seeded_players), key=lambda k: display_map.get(k, k))

    player_seed_key = "_".join(
        re.sub(r"[^a-z0-9]+", "_", str(t).casefold()).strip("_")
        for t in trend_selected_teams
    ) or "custom"

    roster = bundle.get("roster", pd.DataFrame())
    roster_team_map = (
        roster.set_index("player_key")["roster_team"].to_dict()
        if roster is not None and not roster.empty and "roster_team" in roster.columns
        else {}
    )

    trend_players = st.multiselect(
        "Players shown",
        options=all_player_options,
        default=seeded_players,
        format_func=lambda k: (
            f"{display_map.get(k, k)} · {roster_team_map.get(k, '')}"
            if roster_team_map.get(k, "") else display_map.get(k, k)
        ),
        key=f"trend_players_clean_{player_seed_key}_v4",
        help="Remove players from a selected team or add players from any other approved team.",
        placeholder="Search position players",
    )

    metric_lookup = {metric: (title, unit, agg) for metric, title, unit, agg in TREND_METRICS}
    metric_keys = list(metric_lookup)

    c_metric, c_average, c_legend = st.columns([1.35, 1.35, 1.0])
    with c_metric:
        metric = st.selectbox(
            "Metric",
            options=metric_keys,
            index=metric_keys.index("combined_total_distance_m")
            if "combined_total_distance_m" in metric_keys else 0,
            format_func=lambda m: metric_lookup[m][0],
            key="trend_metric_clean_v4",
        )
    with c_average:
        avg_default = "Team average" if trend_selected_teams else "Selected player average"
        average_mode = st.selectbox(
            "Average line",
            options=["Team average", "Selected player average", "Both", "None"],
            index=["Team average", "Selected player average", "Both", "None"].index(avg_default),
            key=f"trend_average_mode_clean_{player_seed_key}_v4",
        )
    with c_legend:
        legend_mode_label = st.selectbox(
            "Legend",
            options=["Averages only", "All players", "Off"],
            index=0,
            key="trend_legend_clean_v4",
            help="Averages only is the clean default. Hover any individual line to identify the player.",
        )

    trend_title, trend_unit, _ = metric_lookup[metric]
    legend_mode = {
        "Averages only": "averages",
        "All players": "all",
        "Off": "off",
    }[legend_mode_label]

    show_team_average = average_mode in {"Team average", "Both"} and bool(trend_selected_teams)
    show_selected_average = average_mode in {"Selected player average", "Both"}
    trend_teams = list(trend_selected_teams)
    restrict_player_teams = None

    team_average_exclude = []
    if show_team_average and seeded_players:
        with st.expander("Team average exclusions", expanded=False):
            st.caption(
                "Use this only when a player's GPS is bad. They can stay visible on the chart while being removed from the team-average calculation."
            )
            team_average_exclude = st.multiselect(
                "Exclude from team average",
                options=seeded_players,
                default=[],
                format_func=lambda k: (
                    f"{display_map.get(k, k)} · {roster_team_map.get(k, '')}"
                    if roster_team_map.get(k, "") else display_map.get(k, k)
                ),
                key=f"trend_team_avg_exclude_clean_{player_seed_key}_v4",
                placeholder="Select bad-data players",
            )

    outside_seed_count = len(set(trend_players) - set(seeded_players))
    hidden_seed_count = len(set(seeded_players) - set(trend_players))

    context_parts = []
    if trend_selected_teams:
        context_parts.append(" + ".join(trend_selected_teams))
    else:
        context_parts.append("Custom player comparison")
    if outside_seed_count:
        context_parts.append(f"{outside_seed_count} cross-team added")
    if hidden_seed_count:
        context_parts.append(f"{hidden_seed_count} hidden")
    if team_average_exclude:
        context_parts.append(f"{len(team_average_exclude)} excluded from team avg")

    has_selected_average = bool(show_selected_average and trend_players)
    if not trend_players and not show_team_average and not has_selected_average:
        st.info("Select a team above or choose one or more players to compare.")
    else:
        # Keep chart copy deliberately small. The chart itself should be the visual focus.
        st.markdown(f'<div class="trend-title-clean">{trend_title}</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="trend-context-clean">{" · ".join(context_parts)} · {len(trend_players)} players shown</div>',
            unsafe_allow_html=True,
        )

        st.plotly_chart(
            comparison_trend_figure(
                bundle,
                start_date,
                end_date,
                trend_players,
                trend_teams,
                metric,
                trend_title,
                trend_unit,
                show_team_average=show_team_average,
                show_selected_average=show_selected_average,
                team_average_exclude_keys=team_average_exclude,
                criteria=flag_criteria,
                restrict_player_teams=restrict_player_teams,
                legend_mode=legend_mode,
            ),
            use_container_width=True,
            config={
                "displaylogo": False,
                "displayModeBar": False,
                "responsive": True,
            },
        )

    if trend_players:
        comparison_status = build_status_table(
            bundle, end_date, trend_players, criteria=flag_criteria
        )
        if not comparison_status.empty:
            with st.expander("Selected player status", expanded=False):
                compact_cols = ["Athlete", "Team", "Pos", "Status", "Primary Driver", "ACWR"]
                compact = comparison_status[compact_cols].copy()
                compact["ACWR"] = pd.to_numeric(compact["ACWR"], errors="coerce").round(2)
                st.dataframe(
                    compact,
                    use_container_width=True,
                    hide_index=True,
                    height=min(420, 38 + 36 * len(compact)),
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
        f'Game EWMA ACWR Monitor ≥ {flag_criteria["monitor_acwr"]:.2f}, Review ≥ {flag_criteria["review_acwr"]:.2f}'
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
