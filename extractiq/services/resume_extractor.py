"""Rule-based resume extraction.

Most of the parsing ideas here come from the original script: the date / duration /
percentage regexes, `parse_month`, section detection, the "count overlapping months
once" experience logic, internship exclusion, and the habit of keeping a *source line*
for every value so a human can check it.

Everything site-specific (DataSort field IDs, forcing gmail.com, "NA" filling,
nationality/passport defaults, Playwright) was deliberately dropped.

Every field is returned as:
    {label, type, value, source, method, notes[]}
`method` says HOW the value was found (pattern / labeled / section / top_line /
heuristic / keyword_scan / fallback / none). The validator uses it to decide
whether a human needs to look at the field. No confidence scores are invented.
"""

from __future__ import annotations

import re
from datetime import date

# --------------------------------------------------------------------------- #
# Regex toolkit (reused from the original script)
# --------------------------------------------------------------------------- #

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_NAME = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_TOKEN = (
    r"(?:"
    r"\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)\d{2}|"
    r"(?:19|20)\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|"
    rf"{MONTH_NAME}\.?,?\s+\d{{4}}|\d{{1,2}}[./-]\d{{4}}|"
    r"(?:19|20)\d{2})"
)
DATE_RANGE_RE = re.compile(
    rf"(?P<start>{DATE_TOKEN})\s*(?:-|–|—|\bto\b)\s*"
    rf"(?P<end>{DATE_TOKEN}|present|current|till\s+date|to\s+date|now)",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"\b(?:(?P<years>\d+(?:[.,]\d+)?)\s*(?:years?|yrs?)"
    r"(?:[\s,]*(?:and\s*)?(?P<months>\d+(?:[.,]\d+)?)\s*(?:months?|mos?))?"
    r"|(?P<months_only>\d+(?:[.,]\d+)?)\s*(?:months?|mos?))\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
PERCENT_RE = re.compile(
    r"\b\d{1,3}(?:[.,]\d+)?\s*(?:%|percent(?:age)?\b|per\s+cent\b)", re.IGNORECASE
)
PERCENT_LABEL_RE = re.compile(
    r"\b(?:percentage|percent|aggregate\s+percentage)\s*"
    r"(?:obtained\s*)?[:=\-]?\s*(\d{1,3}(?:[.,]\d+)?)\s*%?(?![\d.])",
    re.IGNORECASE,
)
SCORE_RE = re.compile(
    r"\b(?:(?:\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)\s*"
    r"(?:CGPA|SGPA|GPA|SPI|CPI|CGPI)|"
    r"(?:CGPA|SGPA|GPA|SPI|CPI|CGPI)\s*(?:/\s*\d+(?:[.,]\d+)?)?\s*"
    r"[:\-]?\s*\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)(?![\d])",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)

INTERNSHIP_RE = re.compile(r"\b(?:internship|intern|training|trainee|apprentice)\b", re.I)
FRESHER_RE = re.compile(
    r"\b(?:fresher|no\s+(?:prior\s+|professional\s+|work\s+)?experience)\b", re.I
)


def month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def parse_month(token: str):
    """Turn a date token into (year, month); None when it cannot be trusted."""
    token = token.strip().rstrip(".,")
    lowered = token.lower()
    if lowered in {"present", "current", "till date", "to date", "now"}:
        today = date.today()
        return today.year, today.month
    named = re.fullmatch(rf"({MONTH_NAME})\.?,?\s+(\d{{4}})", token, re.I)
    if named:
        name = re.sub(r"\.$", "", named.group(1)).lower()
        return int(named.group(2)), MONTHS[name]
    year_first = re.fullmatch(r"((?:19|20)\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", token)
    if year_first and 1 <= int(year_first.group(2)) <= 12:
        return int(year_first.group(1)), int(year_first.group(2))
    day_first = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})", token)
    if day_first and 1 <= int(day_first.group(2)) <= 12:
        return int(day_first.group(3)), int(day_first.group(2))
    numeric = re.fullmatch(r"(\d{1,2})[./-](\d{4})", token)
    if numeric and 1 <= int(numeric.group(1)) <= 12:
        return int(numeric.group(2)), int(numeric.group(1))
    if re.fullmatch(r"(?:19|20)\d{2}", token):
        return int(token), 1
    return None


def parse_duration_months(text: str):
    match = DURATION_RE.search(text)
    if not match:
        return None
    if match.group("months_only"):
        total = float(match.group("months_only").replace(",", "."))
    else:
        years = float((match.group("years") or "0").replace(",", "."))
        months = float((match.group("months") or "0").replace(",", "."))
        total = years * 12 + months
    rounded = int(round(total))
    return rounded if rounded > 0 else None


# --------------------------------------------------------------------------- #
# Section detection
# --------------------------------------------------------------------------- #

_EDU_H = (
    r"education(?:al)?(?:\s+(?:details?|background|qualifications?|profile))?|"
    r"academics?(?:\s+(?:details?|background|qualifications?))?|"
    r"(?:academic\s+)?qualifications?"
)
_EXP_H = (
    r"(?:(?:professional|work|employment|career|relevant)\s+)?experience"
    r"(?:\s+(?:summary|history|details?))?|(?:work|employment|career)\s+history|"
    r"employment|work\s+profile|career\s+profile"
)
_SKILL_H = (
    r"(?:(?:technical|core|key|professional|primary|it)\s+)?skills?"
    r"(?:\s+(?:summary|details?|(?:&|and)\s+tools))?|core\s+competencies|"
    r"technologies|tech(?:nical)?\s+stack|tools(?:\s+(?:&|and)\s+technologies)?"
)
_OTHER_H = (
    r"(?:professional\s+|career\s+)?(?:summary|profile|objective)|career\s+objective|"
    r"certifications?|courses?|projects?|(?:academic\s+|personal\s+)?projects?|"
    r"achievements?|awards?(?:\s+(?:&|and)\s+honou?rs)?|languages?(?:\s+known)?|"
    r"hobbies|interests|references|declaration|publications?|volunteer(?:ing)?|"
    r"personal\s+(?:details|information|profile)|contact(?:\s+details?)?|"
    r"internships?|extra[- ]?curricular(?:\s+activities)?|activities"
)
HEADING_PATTERNS = [
    ("education", re.compile(_EDU_H, re.I)),
    ("experience", re.compile(_EXP_H, re.I)),
    ("skills", re.compile(_SKILL_H, re.I)),
    ("other", re.compile(_OTHER_H, re.I)),
]
_INLINE_RE = re.compile(r"^(?P<h>[A-Za-z &/]{3,40}?)\s*[:\-–]\s*(?P<rest>\S.*)$")


def heading_kind(line: str):
    """Return (kind, inline_content). kind is None if the line is not a heading."""
    stripped = line.strip().strip("•*|:- \t")
    if not stripped or len(stripped) > 45:
        return None, ""
    for kind, rx in HEADING_PATTERNS:
        if rx.fullmatch(stripped):
            return kind, ""
    inline = _INLINE_RE.match(line.strip())
    if inline:
        head = inline.group("h").strip()
        # "Languages: Python, Java" inside a Skills block must not end that block,
        # so inline labels only open education / experience / skills sections.
        for kind, rx in HEADING_PATTERNS:
            if kind != "other" and rx.fullmatch(head):
                return kind, inline.group("rest").strip()
    return None, ""


def split_sections(lines: list[str]):
    """Return (sections, line_kinds). line_kinds[i] is the section kind of line i."""
    sections: dict[str, list[str]] = {}
    kinds: list[str | None] = []
    current = None
    for line in lines:
        kind, inline = heading_kind(line)
        if kind:
            current = kind
            sections.setdefault(kind, [])
            if inline:
                sections[kind].append(inline)
            kinds.append(kind)
            continue
        if current:
            sections[current].append(line)
        kinds.append(current)
    return sections, kinds


def _field(label, ftype, value, source="", method="none", notes=None):
    return {
        "label": label, "type": ftype, "value": value,
        "source": source, "method": method, "notes": list(notes or []),
    }


# --------------------------------------------------------------------------- #
# Simple fields
# --------------------------------------------------------------------------- #

_NAME_EXCLUDED = re.compile(
    r"resume|curriculum|vitae|profile|contact|experience|education|engineer|developer|"
    r"manager|objective|summary|address|email|phone|skills|scientist|analyst|designer|"
    r"consultant|intern|student|fresher|architect|specialist|administrator|officer|"
    r"technologies|solutions|university|college|institute|project|declaration|"
    r"father|mother|gender|nationality|marital|passport|linkedin|github|portfolio",
    re.I,
)


def _looks_like_name(text: str) -> bool:
    if not text or len(text) > 40 or re.search(r"[\d@:/]", text):
        return False
    if _NAME_EXCLUDED.search(text) or heading_kind(text)[0]:
        return False
    words = text.split()
    if not 2 <= len(words) <= 4:
        return False
    return all(re.fullmatch(r"[A-Za-z][A-Za-z'.\-]*", w) for w in words)


def extract_name(lines):
    for line in lines[:40]:
        m = re.match(r"^(?:full\s+name|candidate\s+name|name)\s*[:\-]\s*(.+)$", line.strip(), re.I)
        if m and _looks_like_name(m.group(1).strip()):
            return _field("Full Name", "text", _tidy_name(m.group(1)), line.strip(), "labeled")
    for index, line in enumerate(lines[:12]):
        candidate = re.sub(r"^(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt)\.?\s+", "", line.strip(), flags=re.I)
        if _looks_like_name(candidate):
            method = "top_line" if index <= 2 else "heuristic"
            notes = [] if method == "top_line" else [
                "Name was guessed from a line further down the header, not the first line."
            ]
            return _field("Full Name", "text", _tidy_name(candidate), line.strip(), method, notes)
    return _field("Full Name", "text", "", "", "none")


def _tidy_name(value: str) -> str:
    value = value.strip()
    return value.title() if value.isupper() else value


def extract_email(lines):
    emails, sources = [], []
    for line in lines:
        found = EMAIL_RE.findall(line)
        for e in found:
            if e.lower() not in {x.lower() for x in emails}:
                emails.append(e)
        if found:
            sources.append(line.strip())
    notes = ["More than one email address was found; keep the right one."] if len(emails) > 1 else []
    return _field("Email", "text", ", ".join(emails), " | ".join(sources[:3]),
                  "pattern" if emails else "none", notes)


_PHONE_CAND_RE = re.compile(r"(?<![\d.])\+?\d[\d\s().-]{8,18}\d(?!\d)")


def extract_phone(lines):
    numbers, sources = [], []
    seen = set()
    for line in lines:
        for m in _PHONE_CAND_RE.finditer(line):
            cand = m.group(0).strip()
            groups = re.findall(r"\d+", cand)
            if all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):
                continue  # a run of years like "2019 2020 2021", not a phone
            digits = re.sub(r"\D", "", cand)
            if not 10 <= len(digits) <= 13 or digits in seen:
                continue
            seen.add(digits)
            numbers.append(cand)
            sources.append(line.strip())
    notes = ["More than one phone number was found; keep the right one."] if len(numbers) > 1 else []
    return _field("Phone Number", "text", ", ".join(numbers), " | ".join(dict.fromkeys(sources)),
                  "pattern" if numbers else "none", notes)


# Indian state / city knowledge carried over from the original script's address logic.
STATE_NAMES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa",
    "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "west bengal", "delhi", "jammu and kashmir",
    "ladakh", "puducherry", "chandigarh",
}
KNOWN_CITIES = {
    "mumbai", "pune", "nagpur", "nashik", "aurangabad", "solapur", "beed", "thane",
    "hyderabad", "bengaluru", "bangalore", "mysuru", "chennai", "coimbatore", "madurai",
    "kolkata", "ahmedabad", "surat", "vadodara", "jaipur", "lucknow", "kanpur", "patna",
    "bhopal", "indore", "kochi", "guwahati", "noida", "gurugram", "gurgaon", "new delhi",
    "bhubaneswar", "dehradun", "ranchi", "raipur", "panaji", "amritsar", "ludhiana",
    "london", "new york", "san francisco", "seattle", "austin", "toronto", "dubai",
    "singapore", "berlin", "sydney",
}
_JOB_WORDS = re.compile(
    r"engineer|developer|manager|analyst|designer|scientist|consultant|intern|lead|"
    r"architect|specialist|administrator|officer|full[- ]?stack|backend|frontend",
    re.I,
)


def _strip_pin(text: str) -> str:
    return re.sub(r"\b\d{3}\s?\d{3}\b", "", text).strip(" ,;|-")


def extract_location(lines):
    for line in lines[:80]:
        m = re.match(
            r"^(?:(?:permanent|present|current|residential)\s+)?(?:address|location|city)\s*[:\-]\s*(.+)$",
            line.strip(), re.I,
        )
        if m and not EMAIL_RE.search(m.group(1)):
            value = _strip_pin(m.group(1))
            if value:
                return _field("Location", "text", value, line.strip(), "labeled")

    for line in lines[:15]:
        for part in re.split(r"\s*[|•·]\s*", line):
            part = part.strip()
            if not part or EMAIL_RE.search(part) or re.search(r"https?://|linkedin|github", part, re.I):
                continue
            if len(re.sub(r"\D", "", part)) >= 7 and not re.search(r"[A-Za-z]{3,}", part):
                continue
            lowered = part.lower()
            words = set(re.findall(r"[a-z]+(?: [a-z]+)?", lowered))
            hit = any(s in lowered for s in STATE_NAMES) or any(
                re.search(rf"\b{re.escape(c)}\b", lowered) for c in KNOWN_CITIES
            )
            if hit and len(part) <= 60 and not _JOB_WORDS.search(part):
                return _field("Location", "text", _strip_pin(part), line.strip(), "pattern")
            _ = words

    for line in lines[:12]:
        for part in re.split(r"\s*[|•·]\s*", line):
            part = part.strip()
            if (
                "," in part and len(part) <= 40 and not re.search(r"[\d@/]", part)
                and not _JOB_WORDS.search(part) and not _NAME_EXCLUDED.search(part)
                and 2 <= len(part.replace(",", " ").split()) <= 4
            ):
                return _field(
                    "Location", "text", part, line.strip(), "heuristic",
                    ["Location was guessed from the header (a 'City, Region' shaped line)."],
                )
    return _field("Location", "text", "", "", "none")


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

KNOWN_SKILLS = [
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust", "PHP", "Ruby",
    "Kotlin", "Swift", "SQL", "MySQL", "PostgreSQL", "MongoDB", "SQLite", "Redis",
    "HTML", "CSS", "React", "Angular", "Vue", "Node.js", "Express", "Flask", "Django",
    "FastAPI", "Spring Boot", "Git", "GitHub", "Docker", "Kubernetes", "AWS", "Azure",
    "GCP", "Linux", "REST API", "GraphQL", "Pandas", "NumPy", "Scikit-learn",
    "TensorFlow", "PyTorch", "Keras", "OpenCV", "NLP", "Machine Learning", "Deep Learning",
    "Data Analysis", "Data Visualization", "Power BI", "Tableau", "Excel", "Tailwind CSS",
    "Bootstrap", "Figma", "Jira", "Agile", "CI/CD", "Selenium", "Playwright",
]


def extract_skills(lines, sections):
    section = sections.get("skills")
    if section:
        items, seen = [], set()
        for line in section:
            text = line.strip().strip("•*-| \t")
            text = re.sub(r"^[A-Za-z /&]{2,30}:\s*", "", text)  # "Languages: Python, Java"
            if len(text.split()) > 14 and "," not in text:
                continue  # prose, not a skills list
            for piece in re.split(r"\s*[,;|•·]\s*|\s{3,}", text):
                piece = piece.strip(" .-")
                if 2 <= len(piece) <= 40 and len(piece.split()) <= 5 and piece.lower() not in seen:
                    seen.add(piece.lower())
                    items.append(piece)
        if items:
            return _field("Skills", "list", items, " | ".join(section[:4]), "section")

    text = "\n".join(lines)
    found = []
    for skill in KNOWN_SKILLS:
        pattern = r"(?<![A-Za-z0-9+#.])" + re.escape(skill) + r"(?![A-Za-z0-9+#])"
        if re.search(pattern, text, re.I if len(skill) > 3 else 0):
            found.append(skill)
    if found:
        return _field(
            "Skills", "list", found, "Matched against a built-in keyword list", "keyword_scan",
            ["No Skills section was found. These come from scanning the whole document for "
             "common skill keywords, so the list may be incomplete or include false matches."],
        )
    return _field("Skills", "list", [], "", "none")


# --------------------------------------------------------------------------- #
# Education
# --------------------------------------------------------------------------- #

_DEGREE_LONG = re.compile(
    r"(?:Bachelor(?:'s)?|Master(?:'s)?)\s+(?:of|in)\s+[A-Za-z&]+(?:\s+[A-Za-z&]+){0,4}|"
    r"Diploma(?:\s+in\s+[A-Za-z&]+(?:\s+[A-Za-z&]+){0,3})?|Doctorate|Ph\.?\s?D\.?|"
    r"Associate(?:'s)?\s+(?:Degree|of\s+[A-Za-z]+)|Higher\s+Secondary(?:\s+Certificate)?|"
    r"Senior\s+Secondary|Secondary\s+School(?:\s+Certificate)?|High\s+School|"
    r"Class\s*(?:X|XII|10|12)\b|Standard\s*(?:10|12)|12th|10th|XII|SSLC|HSC|SSC|"
    r"MBA|MCA|BBA|BCA|B\.?\s?Tech|M\.?\s?Tech|B\.?\s?Sc|M\.?\s?Sc|B\.?\s?Com|M\.?\s?Com",
    re.I,
)
# Two-letter abbreviations (BE, BA, MS...) are ambiguous, so they are case-sensitive
# and only count when followed by a typical "in/of/(/-/Capitalised word" continuation.
_DEGREE_AMBIG = re.compile(
    r"\b[BM]\.?\s?[EAS]\b\.?(?=\s*(?:in\b|of\b|\(|[-–:]|[A-Z][a-z]+))"
)
_INSTITUTION_RE = re.compile(
    r"\b(?:university|college|institute|school|polytechnic|academy|board|IIT|NIT|IIIT|"
    r"vidyalaya|vidyapeeth|mahavidyalaya)\b", re.I,
)


def _degree_match(line: str):
    candidates = [m for m in (_DEGREE_LONG.search(line), _DEGREE_AMBIG.search(line)) if m]
    return min(candidates, key=lambda m: m.start()) if candidates else None


def _clean_degree(line: str, match) -> str:
    tail = line[match.start():]
    piece = re.split(r"\s+[-–—|]\s+|\s*[|,(]|\s+\d|\s+(?:from|at)\b", tail, maxsplit=1)[0]
    return piece.strip(" :-–—.")


def _institution_of(lines) -> str:
    best = ""
    for line in lines:
        for part in re.split(r"\s+[-–—|]\s+|\s*\|\s*|,", line):
            part = part.strip()
            if _INSTITUTION_RE.search(part) and not _degree_match(part):
                cleaned = re.sub(DATE_RANGE_RE, "", part)
                cleaned = YEAR_RE.sub("", cleaned).strip(" -–—,:()")
                if cleaned:
                    return cleaned
        if not best and _INSTITUTION_RE.search(line):
            best = YEAR_RE.sub("", line).strip(" -–—,:()")
    return best


def _period_of(text: str) -> str:
    m = DATE_RANGE_RE.search(text)
    if m:
        return f"{m.group('start').strip()} – {m.group('end').strip()}"
    years = YEAR_RE.findall(text)
    return years[-1] if years else ""


def _result_of(text: str) -> str:
    m = PERCENT_RE.search(text) or PERCENT_LABEL_RE.search(text)
    if m:
        return m.group(0).strip() if "%" in m.group(0) or "percent" in m.group(0).lower() \
            else f"{m.group(1)}%"
    labeled = re.search(
        r"\b(CGPA|SGPA|GPA|SPI|CPI|CGPI)\s*[:\-]?\s*(\d{1,2}(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?)",
        text, re.I,
    )
    if labeled:
        return f"{labeled.group(1).upper()} {re.sub(r'\s+', '', labeled.group(2))}"
    trailing = re.search(
        r"(?<![\d.])(?!(?:19|20)\d{2}\b)(\d{1,2}(?:[.,]\d+)?(?:\s*/\s*\d+)?)\s*(CGPA|SGPA|GPA|SPI|CPI|CGPI)\b",
        text, re.I,
    )
    return f"{trailing.group(2).upper()} {re.sub(r'\s+', '', trailing.group(1))}" if trailing else ""


def extract_education(lines, sections, line_kinds):
    section = sections.get("education")
    method = "section"
    if not section:
        # Fallback: lines near an education cue anywhere outside experience/skills.
        cue = re.compile(
            r"\b(?:university|college|institute|school|polytechnic|bachelor|master|diploma|"
            r"cgpa|gpa|b\.?\s?tech|m\.?\s?tech|mba|bca|mca)\b", re.I)
        keep = set()
        for i, line in enumerate(lines):
            if line_kinds[i] in ("experience", "skills"):
                continue
            if cue.search(line):
                keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
        section = [lines[i] for i in sorted(keep)]
        method = "fallback" if section else "none"
    if not section:
        return _field("Education", "records", [], "", "none")

    starts = [i for i, line in enumerate(section) if _degree_match(line)]
    if not starts:
        return _field("Education", "records", [], "", method, [
            "An education section exists but no recognisable degree or school level was found in it."
        ]) if method == "section" else _field("Education", "records", [], "", "none")

    # Slice into records; an institution-only line right before the next degree belongs to it.
    chunks = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(section)
        chunks.append(section[start:min(end, start + 5)])
    for n in range(len(chunks) - 1):
        tail = chunks[n][-1]
        if (len(chunks[n]) > 1 and _INSTITUTION_RE.search(tail) and not _degree_match(tail)
                and not YEAR_RE.search(tail)):
            chunks[n + 1].insert(0, chunks[n].pop())

    # Same rule for the very first record: an institution line printed above its degree.
    first = starts[0]
    if first > 0:
        above = section[first - 1]
        if _INSTITUTION_RE.search(above) and not _degree_match(above) and not YEAR_RE.search(above):
            chunks[0].insert(0, above)

    records, seen = [], set()
    for chunk in chunks:
        text = " ".join(chunk)
        line = next(l for l in chunk if _degree_match(l))
        degree = _clean_degree(line, _degree_match(line))
        record = {
            "degree": degree,
            "institution": _institution_of(chunk),
            "period": _period_of(text),
            "result": _result_of(text),
        }
        key = tuple(record.values())
        if key in seen:
            continue
        seen.add(key)
        record["_source"] = " | ".join(chunk)
        records.append(record)

    notes = []
    if method == "fallback":
        notes.append("No Education heading was found; entries were recovered from nearby "
                     "degree/school keywords and may be incomplete.")
    source = " || ".join(r.pop("_source") for r in records)
    return _field("Education", "records", records, source, method, notes)


# --------------------------------------------------------------------------- #
# Work experience (the most carefully reused logic)
# --------------------------------------------------------------------------- #

_EMPLOYMENT_CUE = re.compile(
    r"\b(?:company|employer|worked|working|employed|joined|designation|position|manager|"
    r"engineer|executive|analyst|associate|consultant|officer|developer|technician|"
    r"accountant|supervisor|sales|marketing|present|current|pvt\.?\s*ltd|llp|limited|"
    r"technologies|solutions|services|inc\.?|corp)\b", re.I)
_EDUCATION_CUE = re.compile(
    r"\b(?:education|school|college|university|degree|bachelor|master|graduation|SSC|HSC|"
    r"10th|12th|board|passed|CGPA|GPA)\b", re.I)


def _heading_text(section_lines, idx, match):
    """Pick a human-readable role/company string for a dated line."""
    line = section_lines[idx]
    remainder = (line[:match.start()] + " " + line[match.end():])
    remainder = DURATION_RE.sub(" ", remainder)
    remainder = re.sub(r"[()|•·]", " ", remainder).strip(" ,;:-–—\t")
    if len(re.findall(r"[A-Za-z]", remainder)) >= 3:
        return re.sub(r"\s+", " ", remainder), [line.strip()]

    def usable(i):
        if not 0 <= i < len(section_lines):
            return False
        t = section_lines[i].strip()
        return (bool(t) and len(t) < 70 and not DATE_RANGE_RE.search(t)
                and not re.match(r"^[•*\-–]", t) and not heading_kind(t)[0])

    parts = []
    if usable(idx - 1):
        parts.append(section_lines[idx - 1].strip())
    if usable(idx + 1) and (parts or True):
        nxt = section_lines[idx + 1].strip()
        if len(nxt.split()) <= 10 and not nxt.endswith("."):
            parts.append(nxt)
    heading = " — ".join(dict.fromkeys(parts))
    return heading, parts


def _work_lines(lines, sections, line_kinds):
    if sections.get("experience"):
        return sections["experience"], "section"
    keep = set()
    for i, line in enumerate(lines):
        if line_kinds[i] in ("education", "skills") or not DATE_RANGE_RE.search(line):
            continue
        lo, hi = max(0, i - 2), min(len(lines), i + 3)
        ctx = " ".join(lines[lo:hi])
        if _EDUCATION_CUE.search(ctx) and not INTERNSHIP_RE.search(ctx):
            continue
        if _EMPLOYMENT_CUE.search(ctx):
            keep.update(range(lo, hi))
    if keep:
        return [lines[i] for i in sorted(keep)], "fallback"
    return [], "none"


def extract_experience(lines, sections, line_kinds):
    work, method = _work_lines(lines, sections, line_kinds)
    notes: list[str] = []
    entries = []
    covered: set[int] = set()
    ambiguous = False
    year_only_used = False
    overlaps = 0
    current = month_index(date.today().year, date.today().month)

    for idx, line in enumerate(work):
        for match in DATE_RANGE_RE.finditer(line):
            first = parse_month(match.group("start"))
            last = parse_month(match.group("end"))
            heading, src = _heading_text(work, idx, match)
            context = " ".join(work[max(0, idx - 1):idx + 2])
            is_intern = bool(INTERNSHIP_RE.search(context))
            entry = {
                "title_company": heading,
                "period": f"{match.group('start').strip()} – {match.group('end').strip()}",
                "months": None, "note": "",
                "_source": " | ".join(dict.fromkeys(src + [line.strip()])),
            }
            if not first or not last:
                entry["note"] = "Dates could not be parsed."
                ambiguous = True
                entries.append(entry)
                continue
            fi, li = month_index(*first), month_index(*last)
            if li < fi or li > current:
                entry["note"] = "End date is before the start date or in the future."
                ambiguous = True
                entries.append(entry)
                continue

            span = max(1, li - fi)  # elapsed calendar months (original script's convention)
            year_only = bool(re.fullmatch(r"(?:19|20)\d{2}", match.group("start").strip())
                             or re.fullmatch(r"(?:19|20)\d{2}", match.group("end").strip()))
            stated = parse_duration_months(" ".join(work[idx:idx + 2]))
            effective = span
            if year_only:
                year_only_used = True
                entry["note"] = "Year-only dates, so the month count is an estimate."
                if stated:
                    effective = stated
                    entry["note"] = "Year-only dates; used the duration stated in the resume."
            elif stated and abs(stated - span) > 1:
                entry["note"] = (f"Stated duration ({stated} mo) conflicts with the dates "
                                 f"({span} mo); not counted.")
                ambiguous = True
                entry["months"] = None
                entries.append(entry)
                continue

            entry["months"] = effective
            if is_intern:
                entry["note"] = "Internship/training: excluded from the total."
                entries.append(entry)
                continue
            months_span = set(range(fi, fi + effective))
            if covered & months_span:
                overlaps += 1
            covered |= months_span
            entries.append(entry)

    # De-duplicate records that were seen twice.
    unique, seen = [], set()
    for e in entries:
        key = (e["title_company"], e["period"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    entries = unique

    counted = [e for e in entries if e["months"] and "excluded" not in e["note"]]
    if entries and not ambiguous:
        total = len(covered)
    else:
        total = None

    if not entries and FRESHER_RE.search("\n".join(lines)):
        total = 0
        notes.append("The resume says fresher / no experience, so the total was set to 0.")
    if method == "fallback" and entries:
        notes.append("No Experience heading was found; entries were recovered from dated lines "
                     "that look like employment and may be incomplete.")
    if overlaps:
        notes.append(f"{overlaps} date range(s) overlap; overlapping months were counted once.")
    if ambiguous:
        notes.append("At least one entry has unreliable dates, so the total is not calculated.")
    if year_only_used:
        notes.append("Some entries use year-only dates, so the total is an estimate.")

    source = " || ".join(e.pop("_source") for e in entries)
    entry_notes = [n for n in notes if n.startswith(("No Experience heading", "At least one entry"))]
    total_notes = [n for n in notes if n not in entry_notes]
    if total is None and entries:
        total_notes.append("Total could not be calculated safely; enter it manually.")

    exp_field = _field("Work Experience", "records", entries,
                       source, method if entries else "none", entry_notes)
    total_field = _field(
        "Total Experience (months)", "text", "" if total is None else str(total),
        "Calculated from the dated entries above (internships excluded, overlaps counted once)"
        if counted else source,
        "pattern" if total is not None and entries else ("section" if total == 0 else "none"),
        total_notes,
    )
    return exp_field, total_field


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def extract_resume(lines: list[str]) -> dict:
    sections, kinds = split_sections(lines)
    experience, total = extract_experience(lines, sections, kinds)
    return {
        "full_name": extract_name(lines),
        "email": extract_email(lines),
        "phone": extract_phone(lines),
        "location": extract_location(lines),
        "skills": extract_skills(lines, sections),
        "education": extract_education(lines, sections, kinds),
        "experience": experience,
        "total_experience": total,
    }
