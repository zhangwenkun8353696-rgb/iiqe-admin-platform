import os
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from services import (
    PAPER_CHOICES,
    PAPER_NAMES,
    bootstrap_existing_bank,
    generate_and_store,
    get_recent_uploads,
    get_stats,
    init_db,
    process_material_upload,
    process_questions_upload,
    query_questions,
    sample_questions,
)

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "iiqe-admin-dev-secret")

    init_db()
    bootstrap_existing_bank()

    @app.get("/")
    def index():
        paper = (request.args.get("paper") or "").upper().strip()
        keyword = (request.args.get("keyword") or "").strip()
        mismatch_only = request.args.get("mismatch") == "1"
        limit = min(int(request.args.get("limit", 200) or 200), 500)

        questions = query_questions(
            paper=paper,
            keyword=keyword,
            mismatch_only=mismatch_only,
            limit=limit,
        )
        stats = get_stats()
        uploads = get_recent_uploads(limit=20)

        sample_n = min(int(request.args.get("sample_n", 0) or 0), 50)
        sample_paper = (request.args.get("sample_paper") or "").upper().strip()
        samples = sample_questions(n=sample_n, paper=sample_paper) if sample_n else []

        return render_template(
            "index.html",
            questions=questions,
            stats=stats,
            uploads=uploads,
            papers=PAPER_CHOICES,
            paper_names=PAPER_NAMES,
            selected_paper=paper,
            keyword=keyword,
            mismatch_only=mismatch_only,
            limit=limit,
            samples=samples,
            sample_n=sample_n,
            sample_paper=sample_paper,
        )

    @app.post("/upload-material")
    def upload_material_web():
        material_file = request.files.get("material_file")
        paper_hint = (request.form.get("paper_hint") or "").upper()
        try:
            result = process_material_upload(material_file, paper_hint)
            flash(
                f"资料已上传，并生成新增 {result['inserted']} 道题目。",
                "success",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/upload-questions")
    def upload_questions_web():
        question_file = request.files.get("question_file")
        declared_paper = (request.form.get("declared_paper") or "").upper().strip() or None
        try:
            result = process_questions_upload(question_file, declared_paper=declared_paper)
            flash(
                f"题目文件已上传，成功导入 {result['inserted']} 道题目。",
                "success",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/generate")
    def generate_web():
        try:
            target = int(request.form.get("target_total", 3000) or 3000)
            target = max(50, min(target, 6000))
            seed_raw = (request.form.get("seed") or "").strip()
            seed = int(seed_raw) if seed_raw.isdigit() else None
            result = generate_and_store(target_total=target, seed=seed)
            flash(
                f"已从知识点生成 {result['produced']} 道题，新增入库 {result['inserted']} 道"
                f"（疑似科目不符 {result['mismatch_count']} 道）。",
                "success",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/api/stats")
    def api_stats():
        return jsonify(get_stats())

    @app.get("/api/sample")
    def api_sample():
        n = min(int(request.args.get("n", 20) or 20), 50)
        paper = (request.args.get("paper") or "").upper().strip()
        items = sample_questions(n=n, paper=paper)
        return jsonify({"count": len(items), "items": items})

    @app.post("/api/generate")
    def api_generate():
        target = int(request.form.get("target_total", 3000) or 3000)
        target = max(50, min(target, 6000))
        seed_raw = (request.form.get("seed") or "").strip()
        seed = int(seed_raw) if seed_raw.isdigit() else None
        return jsonify(generate_and_store(target_total=target, seed=seed)), 201

    @app.get("/api/questions")
    def api_questions():
        paper = (request.args.get("paper") or "").upper().strip()
        keyword = (request.args.get("keyword") or "").strip()
        mismatch_only = request.args.get("mismatch") == "1"
        limit = min(int(request.args.get("limit", 200) or 200), 1000)
        rows = query_questions(paper=paper, keyword=keyword, mismatch_only=mismatch_only, limit=limit)
        return jsonify({"count": len(rows), "items": rows})

    @app.post("/api/upload/material")
    def api_upload_material():
        material_file = request.files.get("material_file")
        paper_hint = (request.form.get("paper_hint") or "").upper().strip()
        result = process_material_upload(material_file, paper_hint)
        return jsonify(result), 201

    @app.post("/api/upload/questions")
    def api_upload_questions():
        question_file = request.files.get("question_file")
        declared_paper = (request.form.get("declared_paper") or "").upper().strip() or None
        result = process_questions_upload(question_file, declared_paper=declared_paper)
        return jsonify(result), 201

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5050")), debug=True)
