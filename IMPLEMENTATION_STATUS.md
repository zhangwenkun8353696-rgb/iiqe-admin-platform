# Implementation Status

## What Works

- Flask app with web UI and JSON API is implemented and runnable.
- SQLite persistence is implemented (`questions`, `uploads`, `meta` tables).
- Material upload supports `.pdf` and `.txt`.
- Question upload supports `.jsonl`, `.json`, `.csv`.
- Preview/search/filter page supports paper filter, keyword filter, mismatch-only filter.
- Subject validation is active for each inserted question:
  - declared paper
  - detected paper
  - mismatch flag
  - validation note
- Bootstrap import from local enriched question-bank files runs once on first startup.

## AI Generation and Fallback

- If `LLM_API_KEY` is configured, system attempts external LLM generation first.
- If external call is unavailable/fails, system falls back to deterministic local rule-based generation.
- Fallback keeps material-to-question feature usable without network/API keys.

## How to Run

1. `pip install -r requirements.txt`
2. Optional LLM env vars:
   - `LLM_API_KEY`
   - `LLM_API_URL`
   - `LLM_MODEL`
3. `python app.py`
4. Open `http://127.0.0.1:5050`

## Notes

- The app is intentionally minimal but complete for admin workflow and validation visibility.
- Any malformed records during bootstrap/import are skipped safely.
