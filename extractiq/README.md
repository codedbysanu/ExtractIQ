# ExtractIQ

**Intelligent Document Extraction & Human Verification Platform**

Upload a PDF → extract text → structured extraction → validation → **human review** → export verified JSON.

> Automation assists → Validation checks → Human reviews

Rule-based extraction, not a language model. It never invents confidence scores:
fields are `Extracted`, `Needs Review` (missing, guessed or inconsistent) or `Verified` (a human confirmed it).

## Run it

```bash
cd extractiq
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

Use the "fictional sample" buttons on the home page, or the PDFs in `sample_documents/`.

## Modes

- **Resume / Candidate**: name, email, phone, location, skills, education, work experience, total experience (months).
- **Generic Document**: filename, pages, extractive summary, detected emails/links/dates/amounts/percentages, full text.

## Structure

```
app.py                      Flask routes: /, /api/extract, /samples/<name>
services/pdf_processor.py   PyMuPDF text extraction, layout-aware line rebuilding, PDF error handling
services/resume_extractor.py  Regex + section based resume parsing
services/generic_analyzer.py  Extractive summary + pattern detection
services/validator.py       Per-field checks -> Extracted / Needs Review
templates/index.html        Single-page UI (upload view + review view)
static/                     CSS and vanilla JS
sample_documents/           Fictional PDFs, including scanned and corrupt error demos
```

## Known limits (on purpose, not hidden)

- No OCR: scanned PDFs are rejected with a clear message.
- Resume parsing is rules and regexes. Unusual layouts (multi-column, graphics-heavy) will produce
  `Needs Review` fields or misses. That is what the review step is for.
- Work experience gives a combined role/company string rather than separate fields.
- Month counting uses elapsed calendar months (Jan 2020 → Jan 2021 = 12); overlapping jobs count once;
  internships are listed but excluded from the total; year-only dates are flagged as estimates.
- Uploaded PDFs are saved to `uploads/` only during processing and deleted immediately.
