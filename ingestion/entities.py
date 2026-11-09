"""entities.py — dated events and cited US authorities, found in passage text at build time.

Every row keeps the exact words matched (`date_text` / `cite_text`) and the sentence
around them, both verbatim substrings of the whitespace-normalized passage text, so
the browser's verification gate accepts them without any model involved.

  events.parquet       one row per date mention, sorted by date
  authorities.parquet  one row per citation of a statute, regulation, act, code or case

Dates: US conventions (numeric dates are month/day/year). Bare years and relative
dates ("two weeks later") are not converted. Authorities: federal (U.S.C., C.F.R.),
California codes and regulations, CEQA Guidelines, named Acts (with their defined
acronyms), municipal codes, and court cases (via eyecite).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_RE = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
YEAR = r"((?:19|20)\d{2})"

# Order matters: longer forms first; a span claimed by one pattern is not reused.
DATE_PATTERNS = [
    ("mdy", re.compile(rf"\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*{YEAR}\b", re.I)),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?{MONTH_RE}\.?,?\s+{YEAR}\b", re.I)),
    ("iso", re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")),
    ("num", re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})(?![\d/])")),
    ("my", re.compile(rf"\b{MONTH_RE}\.?,?\s+{YEAR}\b", re.I)),
]


def _month(name: str) -> int:
    return MONTHS[name.lower()[:3]]


def find_dates(line: str) -> list[tuple[int, int, str, str]]:
    """[(start, end, iso, precision)] — iso is YYYY-MM-DD (day) or YYYY-MM (month)."""
    found, taken = [], []
    for kind, pattern in DATE_PATTERNS:
        for m in pattern.finditer(line):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            g = m.groups()
            try:
                if kind == "mdy":
                    y, mo, d = int(g[2]), _month(g[0]), int(g[1])
                elif kind == "dmy":
                    y, mo, d = int(g[2]), _month(g[1]), int(g[0])
                elif kind == "iso":
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                elif kind == "num":
                    y, mo, d = int(g[2]), int(g[0]), int(g[1])
                else:
                    y, mo, d = int(g[1]), _month(g[0]), 0
                if d:
                    iso, precision = dt.date(y, mo, d).isoformat(), "day"
                else:
                    iso, precision = f"{dt.date(y, mo, 1):%Y-%m}", "month"
            except (ValueError, KeyError):
                continue
            # "May" and "March" are also ordinary words; month-only needs a capital.
            if kind == "my" and not g[0][0].isupper():
                continue
            taken.append(m.span())
            found.append((m.start(), m.end(), iso, precision))
    return sorted(found)


# ---------------------------------------------------------------------------
# Authorities
# ---------------------------------------------------------------------------

PROV = r"(\d+[A-Za-z]?(?:[.\-]\d+[A-Za-z]?)*(?:\([A-Za-z0-9]{1,4}\))*)"
SEC = r"(?:[Ss]ections?|SECTION|[Ss]ec\.|§§?|[Pp]art)"

CA_CODES = {
    "public resources": ["pub. res.", "pub. resources"], "government": ["gov.", "govt."],
    "health and safety": ["health & saf.", "health & safety", "health and saf."], "water": ["wat."],
    "fish and game": ["fish & game", "fish & g."], "civil": ["civ."], "penal": ["pen."], "labor": ["lab."],
    "business and professions": ["bus. & prof."], "vehicle": ["veh."], "evidence": ["evid."],
    "public utilities": ["pub. util.", "pub. utilities"], "revenue and taxation": ["rev. & tax."],
    "food and agricultural": ["food & agric."], "streets and highways": ["sts. & high."],
    "education": ["educ."], "elections": ["elec."], "insurance": ["ins."], "harbors and navigation": ["harb. & nav."],
}
_CA_ALT = {alt: full for full, alts in CA_CODES.items() for alt in [full, *alts]}
_CA_NAMES = "|".join(sorted((re.escape(a).replace(r"\ ", r"\s+") for a in _CA_ALT), key=len, reverse=True))

# (pattern, instrument builder, provision group or None)
LAW_PATTERNS = [
    (re.compile(rf"\b(\d{{1,2}})\s+U\.?\s?S\.?\s?C\.?(?:\s?A\.?)?\s*(?:{SEC}\s*)?{PROV}"),
     lambda g: f"{g[0]} U.S.C.", 1),
    (re.compile(rf"\b(\d{{1,2}})\s+C\.?\s?F\.?\s?R\.?\s*(?:{SEC}\s*)?{PROV}"),
     lambda g: f"{g[0]} C.F.R.", 1),
    (re.compile(rf"\b(\d{{1,2}})\s+(?:Cal\.?\s*Code\s*(?:of\s+)?Regs?\.?|CCR)\s*,?\s*(?:{SEC}\s*)?{PROV}"),
     lambda g: f"Cal. Code Regs., tit. {g[0]}", 1),
    (re.compile(rf"California\s+Code\s+of\s+Regulations,?\s+[Tt]itle\s+(\d{{1,2}}),?\s*(?:{SEC}\s*{PROV})?"),
     lambda g: f"Cal. Code Regs., tit. {g[0]}", 1),
    (re.compile(rf"[Tt]itle\s+(\d{{1,2}})\s+of\s+the\s+California\s+Code\s+of\s+Regulations(?:,?\s*{SEC}\s*{PROV})?"),
     lambda g: f"Cal. Code Regs., tit. {g[0]}", 1),
    (re.compile(rf"(?:(?:State\s+)?CEQA\s+Guidelines|Guidelines\s+for\s+Implementation\s+of\s+the\s+California\s+Environmental\s+Quality\s+Act)(?:,?\s*{SEC}\s*{PROV})?"),
     lambda g: "CEQA Guidelines", 0),
    (re.compile(rf"(?:(?:California|Cal\.)\s+)?({_CA_NAMES})\s+Code(?:,?\s*{SEC}\s*{PROV})?", re.I),
     lambda g: f"California {_CA_ALT[re.sub(r'\s+', ' ', g[0].lower())].title().replace(' And ', ' and ')} Code", 1),
    (re.compile(rf"\b((?:[A-Z][a-zé]+\s+){{1,3}})Municipal\s+Code(?:,?\s*{SEC}\s*{PROV})?"),
     lambda g: f"{g[0].strip()} Municipal Code", 1),
]

ACT_LEAD = {"the", "this", "that", "under", "pursuant", "per", "see", "and", "of", "in", "to", "by", "with",
            "for", "as", "a", "an", "such", "said", "its", "their", "our", "any", "each", "if", "when", "where"}
ACT_RE = re.compile(r"\b((?:[A-Z][A-Za-z'’\-]*\s+(?:(?:of|and|for|on|the|to|in|with)\s+)*){1,8}(?:Act|ACT))\b(?:\s+of\s+((?:19|20)\d{2}))?")
ACRONYM_RE = re.compile(r"\s*\(\s*[\"“]?([A-Z][A-Za-z]{1,9})[\"”]?\s*\)")
LOOKBACK_RE = re.compile(rf"{SEC}\s*{PROV}\s+of\s+(?:the\s+)?$")
FORWARD_RE = re.compile(rf"^,?\s*{SEC}\s*{PROV}")


def _act_name(text: str) -> str:
    words = text.split()
    while words and words[0].lower() in ACT_LEAD:
        words = words[1:]
    if len(words) < 2:
        return ""
    name = " ".join(words)
    if name.isupper():  # headings: CLEAN AIR ACT -> Clean Air Act
        name = " ".join(w.lower() if i and w in {"OF", "AND", "FOR", "ON", "THE", "TO", "IN"} else w.capitalize()
                        for i, w in enumerate(words))
    return name


def act_aliases(lines) -> dict[str, str]:
    """Acronyms a document defines for an Act: "California Environmental Quality Act (CEQA)"."""
    aliases: dict[str, str] = {}
    for line in lines:
        for m in ACT_RE.finditer(line):
            name = _act_name(m.group(1))
            a = ACRONYM_RE.match(line, m.end())
            if name and a and a.group(1).upper() == a.group(1):
                aliases.setdefault(a.group(1), name)
    return aliases


def _with_provision(line: str, start: int, end: int, provision: str) -> tuple[int, int, str]:
    """Pull in "Section 12 of the ..." before, or ", Section 12" after, an instrument."""
    if provision:
        return start, end, provision
    back = LOOKBACK_RE.search(line[max(0, start - 60):start])
    if back:
        return start - (len(line[max(0, start - 60):start]) - back.start()), end, back.group(1)
    fwd = FORWARD_RE.match(line[end:end + 40])
    if fwd:
        return start, end + fwd.end(), fwd.group(1)
    return start, end, ""


def find_authorities(line: str, aliases: dict[str, str]) -> list[tuple[int, int, str, str]]:
    """[(start, end, instrument, provision)]."""
    found, taken = [], []

    def claim(s, e, instrument, provision):
        if any(s < te and ts < e for ts, te in taken):
            return
        s, e, provision = _with_provision(line, s, e, provision)
        taken.append((s, e))
        found.append((s, e, instrument, provision.rstrip(".")))

    for pattern, instrument, pgroup in LAW_PATTERNS:
        for m in pattern.finditer(line):
            g = m.groups()
            claim(m.start(), m.end(), instrument(g), (g[pgroup] if pgroup is not None and pgroup < len(g) else None) or "")
    for m in ACT_RE.finditer(line):
        name = _act_name(m.group(1))
        if name:
            lead = m.group(1).find(name.split()[0]) if not m.group(1).isupper() else len(m.group(1)) - len(m.group(1).lstrip())
            claim(m.start(1) + max(0, lead), m.end(), name + (f" of {m.group(2)}" if m.group(2) else ""), "")
    for acronym, name in aliases.items():
        for m in re.finditer(rf"\b{re.escape(acronym)}\b(?!\s+Guidelines)", line):
            claim(m.start(), m.end(), name, "")
    try:
        from eyecite import get_citations
        from eyecite.models import FullCaseCitation
        for c in get_citations(line):
            if isinstance(c, FullCaseCitation):
                s, e = c.span()
                p, d = getattr(c.metadata, "plaintiff", None), getattr(c.metadata, "defendant", None)
                if p and d:  # a bare "11400 S.E. 8" is usually a street address, not a case
                    claim(s, e, " ".join(f"{p} v. {d} ({c.corrected_citation()})".split()), "")
    except ImportError:
        pass
    return sorted(found)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z(“\"\d])")
MAX_SENTENCE = 320


def sentence_around(line: str, start: int, end: int) -> str:
    """The sentence holding line[start:end], clipped at word boundaries when long.
    Always a verbatim substring of `line`."""
    s0, s1 = 0, len(line)
    for m in SENTENCE_SPLIT.finditer(line):
        if m.end() <= start:
            s0 = m.end()
        elif m.start() >= end:
            s1 = m.start()
            break
    if s1 - s0 > MAX_SENTENCE:
        pad = (MAX_SENTENCE - (end - start)) // 2
        a, b = max(s0, start - pad), min(s1, end + pad)
        if a > s0:
            a = line.find(" ", a) + 1 or a
            a = min(a, start)
        if b < s1:
            sp = line.rfind(" ", end, b)
            b = sp if sp > 0 else b
        s0, s1 = a, b
    return line[s0:s1].strip()


def passage_lines(text: str) -> list[str]:
    return [" ".join(part.split()) for part in str(text or "").split("\n") if part.strip()]


ROW_KEYS = ("passage_id", "doc_id", "doc_title", "page_number", "bbox_x_pct", "bbox_y_pct", "bbox_w_pct", "bbox_h_pct")


def extract(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(events, authorities) for corpus rows; figure placeholders are skipped."""
    aliases = act_aliases(line for r in rows for line in passage_lines(r["passage_text"]))
    events, authorities = [], []
    for r in rows:
        if r["content_type"] == "visual_evidence":
            continue
        base = {k: r[k] for k in ROW_KEYS}
        for line in passage_lines(r["passage_text"]):
            for s, e, iso, precision in find_dates(line):
                events.append({**base, "date": iso, "precision": precision, "date_text": line[s:e],
                               "sentence": sentence_around(line, s, e)})
            for s, e, instrument, provision in find_authorities(line, aliases):
                authorities.append({**base, "instrument": instrument, "provision": provision,
                                    "cite_text": line[s:e], "sentence": sentence_around(line, s, e)})
    return events, canonicalize(authorities)


KEEP_PREFIX = {"california", "state", "cal"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower())


def canonicalize(authorities: list[dict]) -> list[dict]:
    """One name per law: spelling variants share the most frequent spelling, and stray
    leading words ("Regulatory Requirements California Environmental Quality Act",
    "Federal Clean Air Act") drop to a known shorter name — but never a jurisdiction
    word, so the California and federal Endangered Species Acts stay apart."""
    from collections import Counter
    counts = Counter(_key(a["instrument"]) for a in authorities)
    spelling = {}
    for name, _ in Counter(a["instrument"] for a in authorities).most_common():
        spelling.setdefault(_key(name), name)

    def best(name: str) -> str:
        base = re.sub(r" of (?:19|20)\d{2}$", "", name)
        if not base.endswith("Act"):  # codes and cases are already exact
            return spelling[_key(name)]
        if base != name and counts.get(_key(base), 0) >= 3:  # "...Act of 1970" -> "...Act"
            name = base
        words = name.split()
        for i in range(1, len(words) - 1):
            if words[i - 1].lower().strip(".") in KEEP_PREFIX:
                continue
            k = _key(" ".join(words[i:]))
            if counts.get(k, 0) >= 3:
                return spelling[k]
        return spelling[_key(name)]

    return [{**a, "instrument": best(a["instrument"])} for a in authorities]


if __name__ == "__main__":
    line = ("On March 14, 2019, staff issued notice under Public Resources Code section 21080(b); "
            "the California Environmental Quality Act (CEQA) applies. See 42 U.S.C. § 1983, "
            "Section 15162 of the CEQA Guidelines, 14 CCR 15183 and San José Municipal Code § 20.100.010, "
            "and Bell Atlantic Corp. v. Twombly, 550 U.S. 544 (2007). Filed 11/7/2019; hearing in June 2020.")
    dates = [(iso, p, line[s:e]) for s, e, iso, p in find_dates(line)]
    assert dates == [("2019-03-14", "day", "March 14, 2019"), ("2019-11-07", "day", "11/7/2019"),
                     ("2020-06", "month", "June 2020")], dates
    assert find_dates("Section 2/30/2019 and May 2019 may 2019") == [(22, 30, "2019-05", "month")]
    aliases = act_aliases([line])
    assert aliases == {"CEQA": "California Environmental Quality Act"}, aliases
    cites = {(i, p) for _, _, i, p in find_authorities(line, aliases)}
    expected = {("California Public Resources Code", "21080(b)"), ("California Environmental Quality Act", ""),
                ("42 U.S.C.", "1983"), ("CEQA Guidelines", "15162"), ("Cal. Code Regs., tit. 14", "15183"),
                ("San José Municipal Code", "20.100.010")}
    assert expected <= cites, expected - cites
    assert any(i.startswith("Bell Atlantic Corp. v. Twombly") for i, _ in cites), cites
    for s, e, *_ in find_authorities(line, aliases) + [(s, e) for s, e, *_ in find_dates(line)]:
        assert line[s:e] in sentence_around(line, s, e) and sentence_around(line, s, e) in line
    assert canonicalize([{"instrument": "José Municipal Code"}] * 3 + [{"instrument": "San José Municipal Code"}])[-1]["instrument"] == "San José Municipal Code"
    assert find_authorities("near 11400 S.E. 8 Street", {}) == []
    variants = [{"instrument": n} for n in ["Warren-Alquist Act"] * 3 + ["Warren Alquist Act", "Federal Clean Air Act"]
                + ["Clean Air Act"] * 3 + ["Clean Air Act of 1970"] + ["California Endangered Species Act"] + ["Endangered Species Act"] * 3]
    assert [a["instrument"] for a in canonicalize(variants)] == (["Warren-Alquist Act"] * 4 + ["Clean Air Act"] * 5
                                                                 + ["California Endangered Species Act"] + ["Endangered Species Act"] * 3)
    print("entities self-check OK")
