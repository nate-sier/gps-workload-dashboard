# GPS Workload Dashboard — Streamlit

A GitHub/Streamlit-ready GPS workload dashboard for player-development monitoring. It reads STATSports practice data plus roster/game sprint data from Google Sheets, supports adjustable flagging criteria, and can pull missing STATSports data directly from the API when the Google Sheet may be behind.

## Features

- Team multi-select
- Player multi-select (blank = all selected-team players)
- Adjustable date range
- Adjustable Review / Monitor / Needs Exposure flag criteria
- End-of-period athlete status snapshot
- Team workload trends
- Individual athlete trend pages
- Selected-period player summary table
- PDF export using the active teams, players, dates, and flagging criteria
- Five-minute Google Sheets cache plus manual refresh
- STATSports API freshness check / sync controls
- Append-only API sync into the `Raw Sessions` Google Sheet
- Optional app password through Streamlit Secrets

## Repository structure

```text
.
├── streamlit_app.py
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example
```

## 1. Configure Streamlit Secrets

For local development, copy:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Fill in the two Google Sheet IDs and the Google service-account credentials.

The default tabs are:

- STATSports Sheet: `Raw Sessions`
- Reports Sheet: `Master Roster`
- Reports Sheet: `PP_Sprint`

### Google permissions

The service account needs:

- **Editor** access to the STATSports Google Sheet if you want the dashboard to append API data into `Raw Sessions`.
- At least **Viewer** access to the reports Google Sheet if it is only being read.

### STATSports API credentials

Add the API credentials under:

```toml
[statsports_api]
api_key = "YOUR_STATSPORTS_API_KEY"
third_party_api_id = "YOUR_THIRD_PARTY_API_ID"
base_url = "https://statsportsproseries.com"
api_version = "7"
```

These values belong in Streamlit Secrets only. Do not put the real values into `streamlit_app.py`, the README, or any file committed to GitHub.

## 2. Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 3. Push to a public GitHub repository

The source-code repository can be public. The real credentials stay outside the repo in Streamlit Secrets.

```bash
git init
git add .
git commit -m "GPS workload Streamlit dashboard"
git branch -M main
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

Before pushing, verify that `.streamlit/secrets.toml`, service-account JSON files, API credentials, raw data exports, and generated PDFs are not staged.

A quick check is:

```bash
git status
```

## 4. Deploy on Streamlit Community Cloud

1. Create a new Streamlit Community Cloud app.
2. Select the public GitHub repository and branch.
3. Set the entrypoint to `streamlit_app.py`.
4. Open the app's Secrets settings.
5. Paste the contents of your real `.streamlit/secrets.toml`.
6. Deploy.

If the deployed app contains internal player information, set `APP_PASSWORD` in Streamlit Secrets even though the GitHub code repository is public.

## STATSports API sync behavior

The sidebar shows the latest date currently present in the `Raw Sessions` Sheet.

If that date is earlier than today, the dashboard offers a button to pull from the latest Sheet date through today. It also includes a custom API date-range pull and a same-day recheck when today's data already exists.

The API pull uses the same basic STATSports workflow as the existing pull script:

- `getFullSessionsByDateRange`
- drill-level rows
- top speed
- max acceleration
- sprint count
- acceleration count
- HSR distance
- total distance
- HMLD
- sprint distance
- mechanical load
- duration
- `Birch -` drills excluded

### Important: append-only safety

The dashboard intentionally uses append-only synchronization into `Raw Sessions`:

- Existing Sheet rows are not rewritten.
- Newly appearing API rows are appended.
- Re-pulling a date can add newly appearing sessions/drills without duplicating rows already present.
- Manual/extra Sheet columns are preserved.

Because it is append-only, an existing row whose KPI value later changes in STATSports is not overwritten. This matches the safer behavior of the existing append-only pull workflow.

## Flagging logic

Flag settings are adjustable from the dashboard sidebar and are also carried into the generated PDF. Current adjustable rules include:

- High ACWR Monitor / Review thresholds
- Low ACWR Needs Exposure threshold
- HSR 14-day individual z-score thresholds
- Acceleration 14-day individual z-score thresholds
- Sprint-exposure gap

`Data Check` remains the highest-priority state when no GPS/game activity is available on the selected end date.
