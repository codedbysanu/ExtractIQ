"""PDF reading layer: PyMuPDF text extraction with layout-aware line rebuilding.

Refactored from the original script's `visually_ordered_text()` and its
"fewer than 50 characters means scanned PDF" guard. All site/browser specific
code (Playwright capture, HTTP responses) has been removed.
"""

from __future__ import annotations

import pymupdf

MIN_TEXT_CHARS = 50       # below this we refuse: almost certainly scanned/image-only
LOW_TEXT_CHARS = 300      # below this we warn but continue


class PDFError(Exception):
    """A problem with the uploaded PDF that is safe to show to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def visually_ordered_text(document) -> list[tuple[int, list[str]]]:
    """Return [(page_number, [lines])] in reading order.

    Words are grouped by (block, line) using PyMuPDF word boxes, then lines are
    sorted top-to-bottom. If PyMuPDF's plain-text extractor recovers more text
    than the word-box path (some PDFs expose text only that way), we use that.
    """
    pages = []
    for page_number, page in enumerate(document, start=1):
        try:
            words = page.get_text("words")
        except Exception:
            words = []

        grouped: dict[tuple[int, int], list[tuple]] = {}
        for x0, y0, _x1, _y1, text, block_no, line_no, word_no in words:
            grouped.setdefault((block_no, line_no), []).append((x0, y0, word_no, text))

        lines = []
        for group in grouped.values():
            group.sort(key=lambda item: (item[0], item[2]))
            lines.append((
                min(item[1] for item in group),
                min(item[0] for item in group),
                " ".join(item[3] for item in group),
            ))
        lines.sort(key=lambda item: (round(item[0]), item[1]))
        word_lines = [item[2].strip() for item in lines if item[2].strip()]

        try:
            plain_text = page.get_text("text", sort=True)
        except Exception:
            plain_text = ""
        if len(plain_text.strip()) > sum(len(line) for line in word_lines):
            selected = [ln.strip() for ln in plain_text.splitlines() if ln.strip()]
        else:
            selected = word_lines
        pages.append((page_number, selected))
    return pages


def process_pdf(path: str) -> dict:
    """Open a PDF and return text + metadata, or raise PDFError."""
    try:
        document = pymupdf.open(path)
    except Exception:
        raise PDFError(
            "corrupt_pdf",
            "This file could not be opened as a PDF. It may be corrupted or not a real PDF.",
        )

    try:
        if document.needs_pass:
            raise PDFError(
                "encrypted_pdf",
                "This PDF is password-protected. Remove the password and upload it again.",
            )
        if document.page_count == 0:
            raise PDFError("empty_pdf", "This PDF has no pages.")

        try:
            pages = visually_ordered_text(document)
        except Exception:
            raise PDFError(
                "read_failed",
                "The PDF opened but its text could not be read. Try re-saving it as a new PDF.",
            )
        page_count = document.page_count
    finally:
        document.close()

    lines = [line for _n, page_lines in pages for line in page_lines]
    char_count = sum(len(line) for line in lines)

    if char_count < MIN_TEXT_CHARS:
        raise PDFError(
            "no_text",
            "Almost no selectable text was found. This looks like a scanned or image-only PDF, "
            "and ExtractIQ does not do OCR. Try a PDF with selectable text.",
        )

    warnings = []
    if char_count < LOW_TEXT_CHARS:
        warnings.append(
            f"Only {char_count} characters of text were found, so extraction will be limited."
        )

    return {
        "page_count": page_count,
        "lines": lines,
        "text": "\n".join(lines),
        "char_count": char_count,
        "warnings": warnings,
    }
