# IIQE Question-Bank Admin Platform

Minimal runnable admin platform for IIQE question-bank operations:

- Upload study material (`.pdf` / `.txt`)
- Generate and append questions via:
  - External LLM (if configured)
  - Deterministic local fallback (always available)
- Upload question files (`.jsonl` / `.json` / `.csv`)
- Preview/search questions and inspect mismatch validation
- Persist all data in SQLite
- Bootstrap from existing enriched bank files if present

## Tech Stack

- Python 3.10+
- Flask
- SQLite
- `pypdf` for PDF text extraction
- `requests` for optional external LLM API call

## Project Files

- `app.py`: Flask app routes (web + API)
- `services.py`: DB schema, parsing, upload pipelines, AI/fallback generation, validation
- `templates/base.html`, `templates/index.html`: web UI
- `static/styles.css`: basic styling
- `data.db`: SQLite database (created at runtime)
- `uploads/`: uploaded source files (created at runtime)

## Setup and Run

1. Open terminal at:
   - `C:\Users\zhangwenkun12\iiqe-admin-platform`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. (Optional) Configure external LLM:
   - `LLM_API_KEY=...`
   - `LLM_API_URL=https://api.openai.com/v1/chat/completions` (or OpenAI-compatible endpoint)
   - `LLM_MODEL=gpt-4o-mini` (or available model)
4. Run:
   - `python app.py`
5. Open:
   - `http://127.0.0.1:5050`

## Bootstrap Behavior

On first run, the app attempts to import from:

- `C:\Users\zhangwenkun12\iiqe_question_bank_enriched_10x.jsonl`
- `C:\Users\zhangwenkun12\iiqe_question_bank_enriched.jsonl`

Import runs once and is tracked in `meta` table.

## Input Format Notes

### JSONL question file

Each line should include:

```json
{
  "question_id": "QB001",
  "paper": "P1",
  "stem": "Question text",
  "options": {"A":"...","B":"...","C":"...","D":"..."},
  "answer": "A",
  "explanation": "optional"
}
```

### CSV question file columns

Required:

- `paper`, `stem`, `option_a`, `option_b`, `option_c`, `option_d`, `answer`

Optional:

- `question_id`, `explanation`, `source_locator`

## API Routes

- `GET /api/stats`
- `GET /api/questions?paper=P1&keyword=risk&mismatch=1&limit=200`
- `POST /api/upload/material` (`multipart/form-data`: `material_file`, `paper_hint`)
- `POST /api/upload/questions` (`multipart/form-data`: `question_file`, optional `declared_paper`)

## Validation Logic

For every inserted question:

- `declared_paper`: provided by file or material upload form
- `detected_paper`: rule-based keyword detection from stem + options text
- `mismatch_flag`: `1` when detected differs from declared
- `validation_note`: score breakdown for auditing

## Notes

- If external LLM call fails, system automatically falls back to deterministic local generation.
- Duplicate `external_id` is ignored (`INSERT OR IGNORE`) to prevent duplicate imports.
