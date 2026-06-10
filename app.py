import os
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from services import (
    PAPER_CHOICES,
    PAPER_NAMES,
    bootstrap_existing_bank,
    get_recent_uploads,
    get_stats,
    init_db,
    process_material_upload,
    process_questions_upload,
    query_questions,
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
        )

    @app.post("/upload-material")
    def upload_material_web():
        material_file = request.files.get("material_file")
        paper_hint = (request.form.get("paper_hint") or "").upper()
        try:
            result = process_material_upload(material_file, paper_hint)
            flash(
                f"Uploaded material and added {result['inserted']} generated questions.",
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
                f"Uploaded questions file and inserted {result['inserted']} questions.",
                "success",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/api/stats")
    def api_stats():
        return jsonify(get_stats())

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
