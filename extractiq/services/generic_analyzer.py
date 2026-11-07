"""Generic document analysis: simple, honest, rule-based.

No machine-learning model is used. The summary is *extractive*: it picks the most
representative existing sentences using word frequency. It can miss the point of a
document, which is why the validator always marks it for human review.
"""

from __future__ import annotations

import re
from collections import Counter

from .resume_extractor import DATE_TOKEN, EMAIL_RE, MONTH_NAME

STOPWORDS = set("""
a an and are as at be been but by can for from has have had he her his i if in into is it
its of on or our she so such than that the their them then there these they this to was
we were what when which who will with you your not no yes do does did also more most other
some any all each per via may must should would could about over under between within
""".split())

URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"]+", re.I)
MONEY_RE = re.compile(
    r"(?:(?:₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)\s?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?"
    r"(?:\s?(?:k|m|million|billion|lakh|crore)\b)?)",
    re.I,
)
PERCENT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
PHONE_RE = re.compile(r"(?<![\d.])\+?\d[\d\s().-]{8,18}\d(?!\d)")
_DAY_MONTH = rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH_NAME}\.?,?\s+\d{{4}}"
_MONTH_DAY = rf"{MONTH_NAME}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
DATE_RE = re.compile(rf"(?:{_DAY_MONTH}|{_MONTH_DAY}|{DATE_TOKEN})(?!\d)", re.I)


def _join_lines(text: str) -> str:
    """Join wrapped lines, but keep short unpunctuated lines (titles, 'Contact: ...')
    as their own sentence so they don't get glued onto the next paragraph."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        is_break = (len(line) < 60 and not re.search(r"[.!?:;,]$", line)
                    and nxt[:1].isupper())
        out.append(line + (". " if is_break else " "))
    return "".join(out)


def split_sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", _join_lines(text))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", flat)
    return [p.strip() for p in parts if 40 <= len(p.strip()) <= 400]


def summarize(text: str, max_sentences: int = 3) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return ""
    words = [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOPWORDS]
    freq = Counter(words)
    if not freq:
        return ""
    top = freq.most_common(1)[0][1]
    scored = []
    for index, sentence in enumerate(sentences):
        tokens = [w for w in re.findall(r"[a-z]{3,}", sentence.lower()) if w not in STOPWORDS]
        if len(tokens) < 5 or EMAIL_RE.search(sentence) or URL_RE.search(sentence):
            continue  # too short, or a contact/link line rather than content
        score = sum(freq[t] / top for t in tokens) / (len(tokens) ** 0.6)
        scored.append((score, index, sentence))
    if not scored:
        return ""
    best = sorted(scored, key=lambda s: s[0], reverse=True)[:max_sentences]
    return " ".join(s for _score, _i, s in sorted(best, key=lambda s: s[1]))


def _unique(items, limit=12):
    out, seen = [], set()
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def detect_key_information(text: str) -> list[dict]:
    """Pattern-based detections. Each item: {type, value}."""
    found: list[dict] = []

    def add(kind, values):
        for v in _unique(values):
            found.append({"type": kind, "value": v})

    add("Email", EMAIL_RE.findall(text))
    add("URL", [u.rstrip(".,);") for u in URL_RE.findall(text)])
    phones = []
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        groups = re.findall(r"\d+", m.group(0))
        if 10 <= len(digits) <= 13 and not all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):
            phones.append(m.group(0).strip())
    add("Phone", phones)
    add("Date", [m.group(0) for m in DATE_RE.finditer(text) if len(m.group(0)) > 4])
    add("Amount", MONEY_RE.findall(text))
    add("Percentage", PERCENT_RE.findall(text))

    words = [w for w in re.findall(r"[A-Za-z]{4,}", text.lower()) if w not in STOPWORDS]
    add("Frequent term", [w for w, _c in Counter(words).most_common(8)])
    return found


def analyze_generic(filename: str, page_count: int, text: str) -> dict:
    summary = summarize(text)
    key_info = detect_key_information(text)
    word_count = len(re.findall(r"\w+", text))
    return {
        "filename": {"label": "Document Filename", "type": "readonly", "value": filename,
                     "source": "Uploaded file name", "method": "pattern", "notes": []},
        "page_count": {"label": "Pages", "type": "readonly", "value": str(page_count),
                       "source": "PDF metadata", "method": "pattern", "notes": []},
        "summary": {
            "label": "Summary", "type": "textarea", "value": summary,
            "source": "Extractive: highest-scoring existing sentences by word frequency",
            "method": "summary",
            "notes": ["Automatically generated by sentence scoring (not a language model). "
                      "Check it reflects the document."] if summary else [],
        },
        "key_information": {
            "label": "Key Detected Information", "type": "keyinfo", "value": key_info,
            "source": "Regex pattern matching over the extracted text",
            "method": "pattern" if key_info else "none", "notes": [],
        },
        "extracted_text": {
            "label": f"Extracted Text ({word_count:,} words)", "type": "longtext", "value": text,
            "source": "PyMuPDF text extraction", "method": "pattern", "notes": [],
        },
    }
