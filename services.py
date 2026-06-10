import csv
import io
import json
import os
import random
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

# 知识点问答对所在目录（与生成脚本一致）
IIQE_BASE = Path(os.getenv("IIQE_BASE", r"C:\Users\zhangwenkun12"))

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


# ---------------------------------------------------------------------------
# 知识点 → 题库生成（真实考试风格），与 generate_3000_questions.py 同口径
# ---------------------------------------------------------------------------

GEN_PAPER_NAMES = {
    "P1": "保险原理及实务",
    "P2": "一般保险",
    "P3": "长期保险",
    "P5": "投资相连长期保险",
    "P6": "旅游保险代理人",
}

DIRECT_TEMPLATES = [
    "{q}",
    "{q}",
    "{q}（请选出最正确的答案）",
]
TOPIC_TEMPLATES = [
    "下列关于「{topic}」的描述，何者正确？",
    "就「{topic}」而言，以下哪一项说法正确？",
    "有关「{topic}」，下列何者的陈述最为恰当？",
    "关于「{topic}」，下列哪一项的理解是正确的？",
]
SCENARIO_TEMPLATES = [
    "保险中介人在向客户解释「{topic}」时，下列哪一项说法正确？",
    "在处理相关保险事务时，就「{topic}」的正确理解应为下列何者？",
    "客户就「{topic}」提出查询，作为中介人应给出下列哪一项正确说明？",
]
LETTERS = ["A", "B", "C", "D"]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).strip()


def _chapter_key(chapter: str) -> str:
    if not chapter:
        return ""
    m = re.search(r"\d+", str(chapter))
    return m.group(0) if m else str(chapter).strip()[:2]


def load_qa_pairs() -> list[dict[str, Any]]:
    """按优先级载入知识点问答对：分卷扩展 > 合并扩展 > 种子。"""
    parts = sorted(IIQE_BASE.glob("iiqe_qa_pairs_expanded_P*.jsonl"))
    merged = IIQE_BASE / "iiqe_qa_pairs_expanded.jsonl"
    seed = IIQE_BASE / "iiqe_qa_pairs_seed.jsonl"
    if parts:
        files = parts
    elif merged.exists():
        files = [merged]
    else:
        files = [seed]

    items: list[dict[str, Any]] = []
    for fp in files:
        if not fp.exists():
            continue
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("answer") and obj.get("paper"):
                obj["_ckey"] = _chapter_key(obj.get("chapter", ""))
                items.append(obj)
    return items


def _pick_distractors(qa, pool_same_paper, all_items, rng):
    correct_n = _norm(qa["answer"])
    kp = qa.get("knowledge_point")
    ckey = qa.get("_ckey", "")

    def collect(seq):
        out = []
        for x in seq:
            txt = x["answer"].strip()
            if _norm(txt) != correct_n and x.get("knowledge_point") != kp:
                out.append(txt)
        return out

    near = collect(x for x in pool_same_paper if x.get("_ckey", "") == ckey and ckey)
    same_paper = collect(pool_same_paper)
    glob = collect(all_items)

    seen, ordered = set(), []
    for bucket in (near, same_paper, glob):
        rng.shuffle(bucket)
        for c in bucket:
            n = _norm(c)
            if n and n not in seen:
                seen.add(n)
                ordered.append(c)
    if len(ordered) < 3:
        return None
    return ordered[:8]


def _build_stem(qa, rng):
    topic = qa.get("knowledge_point", "").strip() or "相关考点"
    q = (qa.get("question", "") or "").strip()
    r = rng.random()
    if q and r < 0.45:
        tpl = rng.choice(DIRECT_TEMPLATES)
    elif r < 0.8:
        tpl = rng.choice(TOPIC_TEMPLATES)
    else:
        tpl = rng.choice(SCENARIO_TEMPLATES)
    return tpl.format(topic=topic, q=q)


def _make_question(qid, qa, pool_same_paper, all_items, rng):
    kp = qa.get("knowledge_point", "核心考点")
    paper = qa["paper"]
    paper_name = GEN_PAPER_NAMES.get(paper, paper)
    correct = qa["answer"].strip()

    pool = _pick_distractors(qa, pool_same_paper, all_items, rng)
    if not pool or len(pool) < 3:
        return None
    distractors = rng.sample(pool, 3)
    options_texts = distractors + [correct]
    rng.shuffle(options_texts)
    answer_letter = LETTERS[options_texts.index(correct)]
    options = {LETTERS[i]: options_texts[i] for i in range(4)}

    locator = qa.get("source_locator", "")
    explanation = (
        f"正确答案：{correct}"
        f"（{paper_name}，知识点「{kp}」"
        f"{'，' + locator if locator else ''}）。"
        f"其余选项为相关主题下的不同概念或表述有误，故不适用。"
    )
    return {
        "question_id": qid,
        "paper": paper,
        "stem": _build_stem(qa, rng),
        "options": options,
        "answer": answer_letter,
        "explanation": explanation,
        "source_locator": locator,
    }


def generate_questions(target_total: int = 3000, seed: int | None = None) -> list[dict[str, Any]]:
    """从知识点问答对生成贴近考试风格的单选题（各卷均衡）。"""
    rng = random.Random(seed if seed is not None else 20260610)
    qa_items = load_qa_pairs()
    if not qa_items:
        return []

    pools: dict[str, list] = {}
    for it in qa_items:
        pools.setdefault(it["paper"], []).append(it)

    papers = sorted(pools.keys())
    per_paper = max(1, target_total // len(papers))

    results, dedup, counter = [], set(), 1
    for paper in papers:
        paper_qa = pools[paper]
        produced, attempts = 0, 0
        max_attempts = per_paper * 60
        while produced < per_paper and attempts < max_attempts:
            attempts += 1
            qa = rng.choice(paper_qa)
            qid = f"GEN-{paper}-{counter:05d}"
            q = _make_question(qid, qa, paper_qa, qa_items, rng)
            if not q:
                continue
            key = _norm(q["stem"]) + "||" + _norm(q["options"][q["answer"]])
            if key in dedup:
                continue
            dedup.add(key)
            results.append(q)
            produced += 1
            counter += 1
    return results


def generate_and_store(target_total: int = 3000, seed: int | None = None) -> dict[str, Any]:
    """生成题目并写入数据库；同时落地一份 JSONL 备份到 IIQE_BASE。"""
    rows = generate_questions(target_total, seed=seed)
    if not rows:
        raise ValueError(
            "未找到知识点问答对，无法生成。请确认 "
            f"{IIQE_BASE} 下存在 iiqe_qa_pairs_expanded_P*.jsonl 或 seed 文件。"
        )

    inserted, mismatches = 0, 0
    for row in rows:
        if insert_question(row, source_type="ai_generated", source_file="knowledge-derived"):
            inserted += 1
            merged = (row.get("stem") or "") + " " + json.dumps(row.get("options"), ensure_ascii=False)
            _, mismatch, _ = validate_paper(merged, str(row.get("paper") or "").upper())
            mismatches += 1 if mismatch else 0

    try:
        out = IIQE_BASE / "iiqe_question_bank_3000.jsonl"
        with out.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        backup = str(out)
    except Exception:
        backup = ""

    by_paper: dict[str, int] = {}
    for r in rows:
        by_paper[r["paper"]] = by_paper.get(r["paper"], 0) + 1

    _log_upload(
        filename="knowledge-derived",
        upload_type="generate",
        declared_paper="",
        inserted_count=inserted,
        mismatch_count=mismatches,
        generation_mode="knowledge-derived",
        note=f"target={target_total}; produced={len(rows)}; backup={backup}",
    )
    return {
        "produced": len(rows),
        "inserted": inserted,
        "mismatch_count": mismatches,
        "by_paper": by_paper,
        "backup": backup,
    }


def sample_questions(n: int = 20, paper: str = "") -> list[dict[str, Any]]:
    """从题库随机抽样，按试卷格式返回（含简单结构质检）。"""
    rows = query_questions(paper=paper, limit=1000)
    if not rows:
        return []
    n = min(n, len(rows))
    picked = random.sample(rows, n)
    out = []
    for q in picked:
        opts = q.get("options") or []
        texts = [o.get("text", "").strip() for o in opts]
        issues = []
        if len(opts) != 4:
            issues.append(f"选项数={len(opts)}")
        if len(set(texts)) != len(texts):
            issues.append("存在重复选项")
        if q.get("answer") not in [o.get("key") for o in opts]:
            issues.append("答案不在选项内")
        if any(not t for t in texts):
            issues.append("存在空选项")
        q = dict(q)
        q["issues"] = issues
        out.append(q)
    return out
