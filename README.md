# garmin-training-automation

Weekly, AI-assisted training check-in for a triathlon build. A scheduled GitHub Action:

1. Reads the athlete profile, macro plan and training log (`data/`).
2. Pulls the last weeks of activities and wellness data from [intervals.icu](https://intervals.icu).
3. Asks Claude (Anthropic API) for a **structured** analysis (Pydantic schema, validated output).
4. Appends the analysis to the log and updates the macro plan only if justified.
5. Generates a weekly PDF plan (ReportLab) and emails it via [Resend](https://resend.com).
6. Pushes next week's workouts to intervals.icu, which syncs them to a Garmin watch.

## Layout

| Path | Purpose |
|---|---|
| `scripts/weekly_checkin.py` | Orchestration: data fetch, Claude call, file updates, email |
| `scripts/intervals_push.py` | Uploads structured workouts to the intervals.icu calendar (idempotent: only replaces events it created) |
| `scripts/pdf_semana.py` | Weekly PDF with a fixed row per discipline and one column per day |
| `.github/workflows/weekly-checkin.yml` | Sunday evening schedule + manual trigger |
| `data/` | **Example** athlete data — replace with your own |

## Notable details

- The model output is typed with `Literal` enums, so a malformed day name cannot reach the PDF generator; the run aborts before writing anything if the week is incomplete.
- intervals.icu workout syntax has quirks (the `Pace` suffix is required, `m` means minutes, `kph` is not parsed). They are documented in `scripts/intervals_push.py`.
- Garmin upload failures do not abort the check-in; they are reported in the email.

## Setup

1. Fork/clone and put your own files in `data/` (the ones here are fictional examples).
2. Add these repository secrets: `ANTHROPIC_API_KEY`, `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`, `RESEND_API_KEY`, `RECIPIENT_EMAIL`.
3. In intervals.icu, enable the Garmin workout upload.
4. Run the workflow manually from the Actions tab to test.

> Your `data/` folder will contain personal training and health data. Keep your own copy of this repo **private**.

Local run: `pip install -r requirements.txt`, export the variables from `.env.example`, then `python scripts/weekly_checkin.py`.
