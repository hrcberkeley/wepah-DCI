"""entities.py — dated events and cited US authorities, found in passage text at build time.

Every row keeps the exact words matched (`date_text` / `cite_text`) and the sentence
around them, both verbatim substrings of the whitespace-normalized passage text, so
the browser's verification gate accepts them without any model involved.

  events.parquet       one row per date mention, sorted by date
  authorities.parquet  one row per citation of a statute, regulation, act, code or case

General rules live here; everything specific to a language, jurisdiction or domain (numeric
date order, month names, which citations are laws, which words mark a jurisdiction) comes from
the dossier's profile — see dossier_profile.py and docs/PROFILES.md. Call configure(profile)
before using the finders; extract() does it for you.

Always: written-out and ISO dates; named Acts and the acronyms documents define for them;
"Section 12 of the …" pulled in from either side. Never: bare years, relative dates.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

YEAR = r"((?:19|20)\d{2})"
PROV = r"(\d+[A-Za-z]?(?:[.\-]\d+[A-Za-z]?)*(?:\([A-Za-z0-9]{1,4}\))*)"

# Set by configure(profile):
MONTHS: dict[str, int] = {}
DATE_PATTERNS: list = []
NUMERIC_ORDER = "none"
SEC = ""
LAW_PATTERNS: list = []
CODE_ALT: dict[str, str] = {}
KEEP_PREFIX: set[str] = set()
ACRONYM_NOT_BEFORE = ""
EYECITE = False
LOOKBACK_RE = FORWARD_RE = None
PROFILE_ID = ""


def configure(profile: dict) -> None:
    """Build the date and law finders from a profile (see dossier_profile.py)."""
    global MONTHS, DATE_PATTERNS, NUMERIC_ORDER, SEC, LAW_PATTERNS, CODE_ALT, KEEP_PREFIX
    global ACRONYM_NOT_BEFORE, EYECITE, LOOKBACK_RE, FORWARD_RE, PROFILE_ID
    # ponytail: module-level settings, one profile per process; pass a config object if builds ever mix profiles.
    PROFILE_ID = profile.get("id", "")
    d, laws = profile["dates"], profile["laws"]
    names = d["months"]
    MONTHS = {m.lower()[:3]: i for i, m in enumerate(names, 1)}
    MONTHS.update({a.lower(): n for a, n in d.get("month_abbreviations", {}).items()})
    alts = [re.escape(m[:3]) + (f"(?:{re.escape(m[3:])})?" if len(m) > 3 else "") for m in names]
    alts += [re.escape(a) for a in d.get("month_abbreviations", {})]
    month_re = "(" + "|".join(alts) + ")"
    NUMERIC_ORDER = d.get("numeric_order", "none")
    # Order matters: longer forms first; a span claimed by one pattern is not reused.
    DATE_PATTERNS = [
        ("mdy", re.compile(rf"\b{month_re}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*{YEAR}\b", re.I)),
        ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?{month_re}\.?,?\s+{YEAR}\b", re.I)),
        ("iso", re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")),
        ("num", re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})(?![\d/])")),
        ("my", re.compile(rf"\b{month_re}\.?,?\s+{YEAR}\b", re.I)),
    ]
    if NUMERIC_ORDER == "none":
        DATE_PATTERNS = [p for p in DATE_PATTERNS if p[0] != "num"]
    SEC = laws["section_marker"]
    CODE_ALT = {alt: full for full, alts in laws.get("code_names", {}).items() for alt in [full, *alts]}
    code_names = "|".join(sorted((re.escape(a).replace(r"\ ", r"\s+") for a in CODE_ALT), key=len, reverse=True))
    LAW_PATTERNS = []
    for spec in laws.get("patterns", []):
        regex = spec["regex"].replace("{SEC}", SEC).replace("{PROV}", PROV).replace("{CODE_NAMES}", code_names)
        LAW_PATTERNS.append((re.compile(regex, re.I if spec.get("ignore_case") else 0), spec))
    KEEP_PREFIX = set(laws.get("jurisdiction_words", []))
    words = laws.get("acronym_not_before", [])
    ACRONYM_NOT_BEFORE = rf"(?!\s+(?:{'|'.join(map(re.escape, words))}))" if words else ""
    EYECITE = bool(laws.get("eyecite_cases"))
    LOOKBACK_RE = re.compile(rf"{SEC}\s*{PROV}\s+of\s+(?:the\s+)?$")
    FORWARD_RE = re.compile(rf"^,?\s*{SEC}\s*{PROV}")


def _instrument(spec: dict, m: re.Match) -> str:
    """A pattern's instrument template: {1} = regex group 1, {code} = the matched code's full name."""
    name = spec["instrument"]
    if "{code}" in name:
        full = CODE_ALT[re.sub(r"\s+", " ", m.group(spec["code_group"]).lower())]
        name = name.replace("{code}", full.title().replace(" And ", " and "))
    return re.sub(r"\{(\d+)\}", lambda g: (m.group(int(g.group(1))) or "").strip(), name)


def _month(name: str) -> int:
    return MONTHS.get(name.lower()) or MONTHS[name.lower()[:3]]


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
                    y, mo, d = (int(g[2]), int(g[0]), int(g[1])) if NUMERIC_ORDER == "mdy" else (int(g[2]), int(g[1]), int(g[0]))
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

ACT_LEAD = {"the", "this", "that", "under", "pursuant", "per", "see", "and", "of", "in", "to", "by", "with",
            "for", "as", "a", "an", "such", "said", "its", "their", "our", "any", "each", "if", "when", "where"}
ACT_RE = re.compile(r"\b((?:[A-Z][A-Za-z'’\-]*\s+(?:(?:of|and|for|on|the|to|in|with)\s+)*){1,8}(?:Act|ACT))\b(?:\s+of\s+((?:19|20)\d{2}))?")
ACRONYM_RE = re.compile(r"\s*\(\s*[\"“]?([A-Z][A-Za-z]{1,9})[\"”]?\s*\)")


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

    for pattern, spec in LAW_PATTERNS:
        for m in pattern.finditer(line):
            pg = spec.get("provision_group", 0)
            claim(m.start(), m.end(), _instrument(spec, m), (m.group(pg) if pg else None) or "")
    for m in ACT_RE.finditer(line):
        name = _act_name(m.group(1))
        if name:
            lead = m.group(1).find(name.split()[0]) if not m.group(1).isupper() else len(m.group(1)) - len(m.group(1).lstrip())
            claim(m.start(1) + max(0, lead), m.end(), name + (f" of {m.group(2)}" if m.group(2) else ""), "")
    for acronym, name in aliases.items():
        for m in re.finditer(rf"\b{re.escape(acronym)}\b{ACRONYM_NOT_BEFORE}", line):
            claim(m.start(), m.end(), name, "")
    try:
        if not EYECITE:
            raise ImportError
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


def extract(rows: list[dict], profile: dict) -> tuple[list[dict], list[dict]]:
    """(events, authorities) for corpus rows; figure placeholders are skipped."""
    configure(profile)
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
    from dossier_profile import load_profile
    configure(load_profile("en"))
    assert [(iso, p) for _, _, iso, p in find_dates("Filed 4 March 2021, 3/4/2021 and Sept 5, 2020.")] == \
        [("2021-03-04", "day"), ("2020-09-05", "day")]  # the general profile skips ambiguous numeric dates
    configure(load_profile("us-california"))
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
