import csv
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

PAPER_CHOICES = ["P1", "P2", "P3", "P5", "P6"]
PAPER_NAMES = {
    "P1": "Insurance Principles and Practice",
    "P2": "General Insurance",
    "P3": "Long-Term Insurance",
    "P5": "Investment-Linked Long-Term Insurance",
    "P6": "Travel Insurance Agent",
}

PAPER_HINTS = {
    "P1": ["风险", "保险原理", "操守", "管控", "classification", "principle"],
    "P2": ["火险", "责任险", "general insurance", "赔偿", "近因", "索偿"],
    "P3": ["人寿", "可保权益", "冷静期", "medical", "life insurance", "长期"],
    "P5": ["投连", "衍生工具", "CAPM", "P/E", "期货", "期权"],
    "P6": ["旅游", "代理人", "转介", "披露", "旅保", "travel insurance"],
}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT UNIQUE,
            stem TEXT NOT NULL,
            options_json TEXT NOT NULL,
            answer TEXT NOT NULL,
            explanation TEXT DEFAULT '',
            declared_paper TEXT NOT NULL,
            detected_paper TEXT NOT NULL,
            mismatch_flag INTEGER NOT NULL DEFAULT 0,
            validation_note TEXT DEFAULT '',
            source_type TEXT NOT NULL,
            source_file TEXT DEFAULT '',
            source_locator TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_questions_declared_paper ON questions(declared_paper);
        CREATE INDEX IF NOT EXISTS idx_questions_detected_paper ON questions(detected_paper);
        CREATE INDEX IF NOT EXISTS idx_questions_mismatch_flag ON questions(mismatch_flag);

        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            upload_type TEXT NOT NULL,
            declared_paper TEXT DEFAULT '',
            inserted_count INTEGER NOT NULL DEFAULT 0,
            mismatch_count INTEGER NOT NULL DEFAULT 0,
            generation_mode TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def get_meta(key: str) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def normalize_options(raw_options: Any) -> list[dict[str, str]]:
    default = [{"key": k, "text": ""} for k in ["A", "B", "C", "D"]]
    if not raw_options:
        return default

    if isinstance(raw_options, dict):
        return [{"key": k, "text": str(raw_options.get(k, "")).strip()} for k in ["A", "B", "C", "D"]]

    if isinstance(raw_options, list):
        items = default.copy()
        for idx, item in enumerate(raw_options[:4]):
            key = chr(ord("A") + idx)
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("value") or "").strip()
            else:
                text = str(item).strip()
            items[idx] = {"key": key, "text": text}
        return items

    return default


def _paper_score(text: str, paper: str) -> int:
    lower = text.lower()
    return sum(1 for token in PAPER_HINTS.get(paper, []) if token.lower() in lower)


def validate_paper(text: str, declared_paper: str) -> tuple[str, bool, str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return declared_paper, False, "empty text"

    scores = {paper: _paper_score(cleaned, paper) for paper in PAPER_CHOICES}
    detected = max(scores, key=scores.get)
    tie = list(scores.values()).count(scores[detected]) > 1
    if scores[detected] == 0 or tie:
        detected = declared_paper if declared_paper in PAPER_CHOICES else detected
    mismatch = detected != declared_paper
    note = f"declared={declared_paper}, detected={detected}, scores={scores}"
    return detected, mismatch, note


def insert_question(raw: dict[str, Any], source_type: str, source_file: str = "") -> bool:
    stem = str(raw.get("stem") or "").strip()
    answer = str(raw.get("answer") or "").strip().upper()
    declared_paper = str(raw.get("paper") or "").strip().upper()
    if not stem or declared_paper not in PAPER_CHOICES or answer not in {"A", "B", "C", "D"}:
        return False

    options = normalize_options(raw.get("options"))
    merged_text = stem + " " + " ".join(o["text"] for o in options)
    detected_paper, mismatch, note = validate_paper(merged_text, declared_paper)

    payload = (
        str(raw.get("question_id") or uuid.uuid4()),
        stem,
        json.dumps(options, ensure_ascii=False),
        answer,
        str(raw.get("explanation") or "").strip(),
        declared_paper,
        detected_paper,
        1 if mismatch else 0,
        note,
        source_type,
        source_file,
        str(raw.get("source_locator") or "").strip(),
        utc_now(),
    )
    conn = get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO questions(
            external_id, stem, options_json, answer, explanation,
            declared_paper, detected_paper, mismatch_flag, validation_note,
            source_type, source_file, source_locator, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    inserted = conn.total_changes > 0
    conn.commit()
    conn.close()
    return inserted


def _log_upload(
    filename: str,
    upload_type: str,
    declared_paper: str,
    inserted_count: int,
    mismatch_count: int,
    generation_mode: str = "",
    note: str = "",
) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO uploads(filename, upload_type, declared_paper, inserted_count, mismatch_count, generation_mode, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (filename, upload_type, declared_paper, inserted_count, mismatch_count, generation_mode, note, utc_now()),
    )
    conn.commit()
    conn.close()


def parse_jsonl(content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception as exc:
            raise ValueError(f"Invalid JSONL at line {idx}: {exc}") from exc
    return rows


def parse_csv_text(content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        rows.append(
            {
                "question_id": row.get("question_id"),
                "paper": row.get("paper") or row.get("declared_paper"),
                "stem": row.get("stem"),
                "options": {
                    "A": row.get("option_a", ""),
                    "B": row.get("option_b", ""),
                    "C": row.get("option_c", ""),
                    "D": row.get("option_d", ""),
                },
                "answer": row.get("answer"),
                "explanation": row.get("explanation", ""),
                "source_locator": row.get("source_locator", ""),
            }
        )
    return rows


def extract_material_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def _truncate_text(text: str, max_chars: int = 7000) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:max_chars]


def _extract_sentences(text: str) -> list[str]:
    chunks = re.split(r"[。！？.!?\n]+", text)
    return [c.strip() for c in chunks if len(c.strip()) > 18]


def _build_fallback_questions(material_text: str, paper_hint: str, limit: int = 8) -> list[dict[str, Any]]:
    snippets = _extract_sentences(material_text)
    if not snippets:
        snippets = [_truncate_text(material_text, 220) or f"Core topic for {paper_hint} exam."]

    questions: list[dict[str, Any]] = []
    for idx, snippet in enumerate(snippets[:limit], start=1):
        core = _truncate_text(snippet, 120)
        wrong_1 = _truncate_text(snippet[::-1], 120)
        wrong_2 = f"This sentence is unrelated to {paper_hint}."
        wrong_3 = "None of the above."
        questions.append(
            {
                "question_id": f"FB-{paper_hint}-{uuid.uuid4().hex[:10]}",
                "paper": paper_hint,
                "stem": f"Which option best matches this study note? [{core}]",
                "options": {"A": core, "B": wrong_1, "C": wrong_2, "D": wrong_3},
                "answer": "A",
                "explanation": "Fallback generator selects the closest original statement.",
                "source_locator": f"snippet_{idx}",
            }
        )
    return questions


def _generate_with_llm(material_text: str, paper_hint: str) -> list[dict[str, Any]]:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        return []

    api_url = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    prompt = f"""
Generate 8 multiple choice IIQE questions from material for paper {paper_hint}.
Return JSON only in this exact schema:
[
  {{
    "question_id": "...",
    "paper": "{paper_hint}",
    "stem": "...",
    "options": {{"A":"...","B":"...","C":"...","D":"..."}},
    "answer": "A|B|C|D",
    "explanation": "...",
    "source_locator": "..."
  }}
]
Material:
{_truncate_text(material_text)}
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You generate concise high-quality exam questions."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(api_url, json=payload, headers=headers, timeout=45)
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    cleaned = re.sub(r"^```json|```$", "", content.strip(), flags=re.MULTILINE).strip()
    parsed = json.loads(cleaned)
    if isinstance(parsed, dict):
        parsed = parsed.get("questions", [])
    if not isinstance(parsed, list):
        raise ValueError("LLM returned invalid question payload.")
    return [x for x in parsed if isinstance(x, dict)]


def ai_generate_questions_from_material(material_text: str, paper_hint: str) -> tuple[list[dict[str, Any]], str]:
    try:
        rows = _generate_with_llm(material_text, paper_hint)
        if rows:
            return rows, "external_llm"
    except Exception:
        pass
    return _build_fallback_questions(material_text, paper_hint), "deterministic_fallback"


def process_material_upload(material_file: Any, paper_hint: str) -> dict[str, Any]:
    if not material_file or not getattr(material_file, "filename", ""):
        raise ValueError("material_file is required.")
    if paper_hint not in PAPER_CHOICES:
        raise ValueError(f"paper_hint must be one of {PAPER_CHOICES}.")

    suffix = Path(material_file.filename).suffix.lower()
    if suffix not in {".pdf", ".txt"}:
        raise ValueError("Only PDF/TXT study material is supported.")

    saved_name = f"{uuid.uuid4().hex}_{material_file.filename}"
    saved_path = UPLOAD_DIR / saved_name
    material_file.save(saved_path)

    material_text = extract_material_text(saved_path)
    generated_rows, mode = ai_generate_questions_from_material(material_text, paper_hint)

    inserted = 0
    mismatches = 0
    for row in generated_rows:
        row["paper"] = paper_hint
        if insert_question(row, source_type="ai_material", source_file=material_file.filename):
            inserted += 1
            _, mismatch, _ = validate_paper((row.get("stem") or "") + " " + str(row.get("options")), paper_hint)
            mismatches += 1 if mismatch else 0

    _log_upload(
        filename=material_file.filename,
        upload_type="material",
        declared_paper=paper_hint,
        inserted_count=inserted,
        mismatch_count=mismatches,
        generation_mode=mode,
    )
    return {
        "filename": material_file.filename,
        "mode": mode,
        "inserted": inserted,
        "mismatch_count": mismatches,
    }


def process_questions_upload(question_file: Any, declared_paper: str | None = None) -> dict[str, Any]:
    if not question_file or not getattr(question_file, "filename", ""):
        raise ValueError("question_file is required.")

    suffix = Path(question_file.filename).suffix.lower()
    if suffix not in {".jsonl", ".csv", ".json"}:
        raise ValueError("Only .jsonl/.json/.csv question files are supported.")

    content = question_file.read().decode("utf-8", errors="ignore")
    if suffix == ".csv":
        rows = parse_csv_text(content)
    elif suffix == ".json":
        payload = json.loads(content)
        rows = payload if isinstance(payload, list) else payload.get("items", [])
    else:
        rows = parse_jsonl(content)

    inserted = 0
    mismatches = 0
    for row in rows:
        if declared_paper and row.get("paper") in (None, ""):
            row["paper"] = declared_paper
        ok = insert_question(row, source_type="question_file", source_file=question_file.filename)
        if ok:
            inserted += 1
            declared = str(row.get("paper") or "").upper()
            merged = str(row.get("stem") or "") + " " + str(row.get("options") or "")
            _, mismatch, _ = validate_paper(merged, declared)
            mismatches += 1 if mismatch else 0

    _log_upload(
        filename=question_file.filename,
        upload_type="questions",
        declared_paper=declared_paper or "",
        inserted_count=inserted,
        mismatch_count=mismatches,
    )
    return {"filename": question_file.filename, "inserted": inserted, "mismatch_count": mismatches}


def query_questions(
    paper: str = "",
    keyword: str = "",
    mismatch_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if paper in PAPER_CHOICES:
        where.append("declared_paper = ?")
        params.append(paper)
    if keyword:
        where.append("(stem LIKE ? OR explanation LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if mismatch_only:
        where.append("mismatch_flag = 1")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT id, external_id, stem, options_json, answer, explanation,
               declared_paper, detected_paper, mismatch_flag, validation_note,
               source_type, source_file, source_locator, created_at
        FROM questions
        {clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    conn.close()

    out = []
    for row in rows:
        item = dict(row)
        item["options"] = json.loads(item.pop("options_json", "[]"))
        out.append(item)
    return out


def get_recent_uploads(limit: int = 20) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, filename, upload_type, declared_paper, inserted_count, mismatch_count,
               generation_mode, note, created_at
        FROM uploads
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stats() -> dict[str, Any]:
    conn = get_conn()
    total_questions = conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()["c"]
    mismatch_count = conn.execute("SELECT COUNT(*) AS c FROM questions WHERE mismatch_flag = 1").fetchone()["c"]
    paper_rows = conn.execute(
        "SELECT declared_paper AS paper, COUNT(*) AS count FROM questions GROUP BY declared_paper ORDER BY declared_paper"
    ).fetchall()
    conn.close()

    paper_counts = {row["paper"]: row["count"] for row in paper_rows}
    return {
        "total_questions": total_questions,
        "mismatch_count": mismatch_count,
        "paper_counts": paper_counts,
        "bootstrap_done": get_meta("bootstrap_done") == "1",
        "bootstrap_count": int(get_meta("bootstrap_count") or "0"),
    }


def _iter_jsonl_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    yield item
            except Exception:
                continue


def bootstrap_existing_bank() -> None:
    if get_meta("bootstrap_done") == "1":
        return

    candidates = [
        Path(r"C:\Users\zhangwenkun12\iiqe_question_bank_enriched_10x.jsonl"),
        Path(r"C:\Users\zhangwenkun12\iiqe_question_bank_enriched.jsonl"),
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        set_meta("bootstrap_done", "1")
        set_meta("bootstrap_count", "0")
        return

    inserted = 0
    for source in existing:
        for row in _iter_jsonl_lines(source):
            if insert_question(row, source_type="bootstrap", source_file=source.name):
                inserted += 1

    set_meta("bootstrap_done", "1")
    set_meta("bootstrap_count", str(inserted))
    set_meta("bootstrap_sources", ",".join(p.name for p in existing))
