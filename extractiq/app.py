"""ExtractIQ - Flask app.

Flow: upload PDF -> extract text (PyMuPDF) -> rule-based extraction -> validation
-> JSON to the browser, where a human reviews/edits and exports verified data.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from services.generic_analyzer import analyze_generic
from services.pdf_processor import PDFError, process_pdf
from services.resume_extractor import extract_resume
from services.validator import validate_fields

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
SAMPLE_DIR = BASE_DIR / "sample_documents"
# file name -> (button label, extraction mode). All sample documents are fictional.
SAMPLES = {
    "sample_resume_clean.pdf": ("Clean resume", "resume"),
    "sample_resume_messy.pdf": ("Messy resume", "resume"),
    "sample_generic_report.pdf": ("Generic report", "generic"),
    "sample_scanned_no_text.pdf": ("Scanned PDF (error demo)", "resume"),
    "sample_corrupt.pdf": ("Corrupt file (error demo)", "resume"),
}
MAX_UPLOAD_MB = 10
MODES = {"resume", "generic"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.json.sort_keys = False  # keep fields in the order the extractor defines them
UPLOAD_DIR.mkdir(exist_ok=True)


def error_response(code: str, message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


@app.get("/")
def index():
    samples = [
        {"file": name, "label": label, "mode": mode}
        for name, (label, mode) in SAMPLES.items() if (SAMPLE_DIR / name).exists()
    ]
    return render_template("index.html", max_mb=MAX_UPLOAD_MB, samples=samples)


@app.get("/samples/<name>")
def sample(name):
    if name not in SAMPLES:
        abort(404)
    return send_from_directory(SAMPLE_DIR, name, mimetype="application/pdf")


@app.post("/api/extract")
def extract():
    mode = request.form.get("mode", "resume")
    if mode not in MODES:
        return error_response("bad_mode", "Unknown extraction mode.")

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return error_response("no_file", "Choose a PDF file first.")

    filename = os.path.basename(upload.filename)
    if not filename.lower().endswith(".pdf"):
        return error_response("bad_type", "Only PDF files are supported (the file name must end in .pdf).")

    head = upload.stream.read(1024)
    upload.stream.seek(0)
    if b"%PDF-" not in head:
        return error_response("bad_type", "This file is not a valid PDF (it has no PDF header).")

    # Saved only for the duration of processing, then deleted.
    temp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    try:
        upload.save(temp_path)
        pdf = process_pdf(str(temp_path))
    except PDFError as err:
        return error_response(err.code, err.message, 422)
    except Exception:
        app.logger.exception("Unexpected PDF failure")
        return error_response("read_failed", "Something went wrong while reading this PDF.", 500)
    finally:
        temp_path.unlink(missing_ok=True)

    try:
        if mode == "resume":
            fields = extract_resume(pdf["lines"])
        else:
            fields = analyze_generic(filename, pdf["page_count"], pdf["text"])
        counts = validate_fields(fields)
    except Exception:
        app.logger.exception("Extraction failed")
        return error_response(
            "extract_failed",
            "The PDF was read, but structured extraction failed. Try Generic Document mode.",
            500,
        )

    return jsonify({
        "ok": True,
        "mode": mode,
        "document": {
            "filename": filename,
            "pages": pdf["page_count"],
            "characters": pdf["char_count"],
            # Resume mode shows the raw text beside the form so a human can check sources.
            "text": pdf["text"] if mode == "resume" else "",
        },
        "warnings": pdf["warnings"],
        "fields": fields,
        "counts": counts,
    })


@app.errorhandler(413)
def too_large(_err):
    return error_response("too_large", f"That file is larger than {MAX_UPLOAD_MB} MB.", 413)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
