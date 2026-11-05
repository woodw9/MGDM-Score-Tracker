# MGDM 2027 Scoring

Photograph a judge's scoresheet → the marks are read automatically → a tabulator checks them on screen → scores land in an Excel master (and, optionally, a live Google Sheet).

## Day-of workflow

1. **Before the meet:** fill in `config/entries.csv` (one row per team per event; A/B teams get separate rows), then print the sheets (**Print sheets** page, or `python run.py sheets`). Every sheet is pre-labelled with the school, event and judge number, plus a QR code.
2. **Start the app** on the tabulation laptop by double-clicking `start.command`. It prints a link such as `http://192.168.1.20:5050`; open that link on any phone on the same Wi-Fi.
3. **Capture:** feed the sheets through a document scanner set to save into `data/inbox` (PDF or JPG, any order, duplex OK - blank backs are skipped). The server picks new scans up automatically within a few seconds; see the **📠 Scans** page. Phones (**📷 Capture**) still work as a backup.
4. **Review:** each photo appears in **Review** with the marks pre-filled. Marks the reader wasn't sure of are yellow. Check them against the photo, pick the school if it's a blank walk-in sheet, then **Confirm**. Only confirmed sheets count. Re-confirming a sheet replaces the older copy, and everything is logged.
5. **Results:** **Standings** shows event and overall rankings live. The Excel master (`output/MGDM_2027_Master_Scores.xlsx`) is rebuilt after every confirm.

## Changing the rules (no code)

Everything is in `config/events.yaml`:

| Want to… | Edit |
|---|---|
| Average judges instead of adding them | `judge_combine: average` (or `combine:` on one event) |
| Change overall weights or benchmarks | `overall.events` (`benchmark: auto` = computed from the sheets) |
| Drop a weather-cancelled event from the overall | `overall.excluded_events: [color_guard]` |
| Change penalties | the `penalties` sections (`apply_to: all_judges` or `once`) |
| Member minimums / DQ | `members:` on each event |
| Add/remove a movement or criterion | the event's `sections` → **re-print the sheets** afterwards |

Then press **Reload config files** on the home page, and every score is recalculated.

## Setup still to do

- **Claude API key** (turns the photo reader on): create `config/.env` containing
  `ANTHROPIC_API_KEY=sk-ant-...` and restart. Without it, everything works except auto-reading, so tabulators enter marks by hand on the review screen.
  The model is set in `config/settings.yaml` (default `claude-opus-5-5`; about 3–6¢ per page).
- **Google Sheets mirror:** follow the steps in `config/settings.yaml` → `google_sheets`.
- **Passcode** for tabulator devices: `app.passcode` in `config/settings.yaml`.

## Testing before the meet

    .venv/bin/python run.py practice   # filled-in practice "phone photos" in output/practice
    .venv/bin/python run.py selftest   # reads them back and reports accuracy (needs the API key for marks)

Also print a few sheets, fill them in by hand, and photograph them in the actual tabulation room's lighting.

## Secrets (never committed)

`config/.env` and `config/google_service_account.json` are git-ignored. Put these in `config/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
MGDM_PASSCODE=...          # tabulator sign-in passcode
MGDM_ADMIN_PASSWORD=...    # master password for "Clear all scores"
```
