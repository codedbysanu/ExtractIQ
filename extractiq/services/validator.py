"""Validation layer: decides which fields need a human look.

There are NO confidence scores. A field is either:
  extracted     - found by a reliable rule and passed format checks
  needs_review  - missing, guessed by a heuristic, ambiguous, or failed a check
The human then moves fields to `verified` in the UI.
"""

from __future__ import annotations

import re

from .resume_extractor import EMAIL_RE

# Methods that are guesses rather than direct matches.
GUESS_METHODS = {
    "heuristic": "Found by a guess rather than an exact rule.",
    "keyword_scan": "Found by keyword scanning, not from a dedicated section.",
    "fallback": "Recovered without a clear section heading.",
    "summary": "Auto-generated; verify it matches the document.",
}


def _is_empty(value) -> bool:
    return value in ("", None) or value == [] or value == {}


def _check_name(value):
    words = str(value).split()
    return [] if 2 <= len(words) <= 4 else ["Name does not look like a first and last name."]


def _check_email(value):
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    bad = [p for p in parts if not EMAIL_RE.fullmatch(p)]
    return [f"Invalid email format: {', '.join(bad)}"] if bad else []


def _check_phone(value):
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    bad = [p for p in parts if not 10 <= len(re.sub(r"\D", "", p)) <= 13]
    return ["Phone number should have 10–13 digits."] if bad else []


def _check_total(value):
    return [] if re.fullmatch(r"\d{1,4}", str(value).strip()) else ["Total must be a whole number of months."]


def _check_education(records):
    issues = []
    for i, r in enumerate(records, 1):
        missing = [k for k in ("institution", "period") if not str(r.get(k, "")).strip()]
        if missing:
            issues.append(f"Education entry {i} is missing: {', '.join(missing)}.")
    return issues


def _check_experience(records):
    issues = []
    for i, r in enumerate(records, 1):
        if r.get("months") is None and "excluded" not in str(r.get("note", "")):
            issues.append(f"Work entry {i} has no usable month count.")
        if not str(r.get("title_company", "")).strip():
            issues.append(f"Work entry {i} has no role/company text.")
    return issues


FIELD_CHECKS = {
    "full_name": _check_name,
    "email": _check_email,
    "phone": _check_phone,
    "total_experience": _check_total,
    "education": _check_education,
    "experience": _check_experience,
}

# Fields where "nothing found" is expected for some documents and deserves a gentler message.
MISSING_MESSAGES = {
    "skills": "No skills were detected. Add them, or confirm there are none.",
    "education": "No education entries were detected. Add them, or confirm there are none.",
    "experience": "No work experience was detected (fresher, or the layout was not recognised).",
    "key_information": "No emails, links, dates, or amounts were detected.",
}


def validate_fields(fields: dict) -> dict:
    """Add `status` and `issues` to every field, in place, and return a summary."""
    for key, field in fields.items():
        issues = list(field.get("notes", []))
        value = field.get("value")

        if _is_empty(value) and field.get("type") != "readonly":
            issues.insert(0, MISSING_MESSAGES.get(key, "Not found in the document. Enter it manually."))
        else:
            method = field.get("method")
            if method in GUESS_METHODS and not any(GUESS_METHODS[method] in i for i in issues):
                if not issues:  # extractor notes already explain the guess
                    issues.append(GUESS_METHODS[method])
            check = FIELD_CHECKS.get(key)
            if check:
                issues.extend(check(value))

        field["issues"] = list(dict.fromkeys(issues))
        field["status"] = "needs_review" if field["issues"] else "extracted"
        field["missing"] = _is_empty(value) and field.get("type") != "readonly"
        field.pop("notes", None)

    counts = {"extracted": 0, "needs_review": 0, "verified": 0}
    for field in fields.values():
        counts[field["status"]] += 1
    return counts
