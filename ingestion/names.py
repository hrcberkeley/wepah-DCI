"""names.py — people and organizations, found and checked at build time.

Pipeline (each stage narrows or joins what the one before produced):

  1. Find   every candidate, recall first, from four independent sources:
            a. GLiNER name finder (person / government agency / company / organization / court)
            b. patterns: organization names ("... Commission", "City of ..."), titled people
               ("Commissioner Karen Douglas"), acronym definitions ("Full Name (ABC)")
            c. document structure: From:/To:, Dear ..., signature blocks, /s/, "Submitted by"
            d. defined roles: 'SJC02 LLC ("Applicant")'
  2. Clean  strip titles, possessives and articles; drop roles, junk and lone lowercase words
  3. Judge  a local language model labels each distinct name once: keep or drop, type, and
            the fullest form written in its context (accepted only if it is verbatim there)
  4. Join   within a document: acronyms -> their definition, "Ms. Warshaw" -> the one
            Warshaw in that document; across documents: same name with or without middle
            initials, legal suffixes or accents; near-identical spellings (scanning errors)
            only when the judge confirms they are the same
  5. Write  names.parquet: one row per mention with its entity, role, exact words and sentence

Every mention keeps its exact words and sentence from the page, so nothing the model says
can put a name on screen that is not written in the record.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from entities import ROW_KEYS, passage_lines, sentence_around

GLINER_MODEL = "urchade/gliner_multi-v2.1"
GLINER_LABELS = ["person", "government agency", "company", "organization", "court"]
GLINER_THRESHOLD = 0.4
JUDGE_DEFAULT = "mlx:arthurcollet/Qwen3.6-35B-A3B-mlx-mxfp8"
JUDGE_BATCH = 25
PROMPT_VERSION = 2

# ---------------------------------------------------------------------------
# 1. Find
# ---------------------------------------------------------------------------

TITLE_WORDS = ("Commissioners?|Governor|Lieutenant Governor|President|Secretary|Chief|Deputy|Chair(?:man|woman|person)?|Vice Chair|Presiding Member|Associate Member|Hearing Officer|"
               "Mr\\.|Ms\\.|Mrs\\.|Dr\\.|Hon\\.|Honorable|Senator|Assemblymember|Assembly Member|Councilmember|"
               "Council Member|Mayor|Supervisor|Intervenor|Attorney|Professor|Judge|Justice")
NAME_TOKEN = r"(?:[A-Z][a-z'’\-]+|[A-Z]\.)"
PERSON = rf"({NAME_TOKEN}(?:\s+{NAME_TOKEN}){{1,3}})"
TITLED_RE = re.compile(rf"\b(?:(?:{TITLE_WORDS})\s+)+({NAME_TOKEN}(?:\s+{NAME_TOKEN}){{0,3}})")

ORG_TAIL = ("Commission|Department|District|Agency|Board|Council|Authority|Company|Corporation|Corp\\.|Inc\\.|"
            "Incorporated|LLC|L\\.L\\.C\\.|LP|L\\.P\\.|LLP|Association|Coalition|Society|Institute|University|College|"
            "Court|Office|Bureau|Service|Division|Committee|Foundation|Club|Alliance|Partners|Utilities|Unions?|"
            "Network|Center|Laboratory|Group|Associates|Trust|Conservancy|Federation|Council")
ORG_RE = re.compile(rf"\b((?:U\.S\.\s+)?(?:[A-Z][\w&'’.\-]*,?\s+(?:(?:of the|of|and|for|on|the|&)\s+)?){{1,8}}(?:{ORG_TAIL}))(?![a-z])")
PLACE_GOV_RE = re.compile(r"\b((?:City|County|State|Town|Port) of (?:[A-Z][a-zé]+)(?:\s+[A-Z][a-zé]+){0,2})")
ACRONYM_DEF_RE = re.compile(r"((?:[A-Z][\w&'’.\-]*\s+(?:(?:of the|of|and|for|on|the|&)\s+)?){1,9}[A-Z][\w&'’.\-]*)\s*\(\s*[\"“]?([A-Z][A-Za-z&]{1,9})[\"”]?\s*\)")
ROLE_DEF_RE = re.compile(r"((?:[A-Z][\w&'’.\-]*\s+(?:(?:of the|of|and|for|on|the|&)\s+)?){0,8}[A-Z][\w&'’.\-]*)\s*,?\s*\(\s*(?:the\s+)?[\"“]?(Applicant|Project Owner|Owner|Petitioner|Intervenor|Respondent|Complainant|Commenter)[\"”]?\s*\)")
CITE_AUTHOR_RE = [  # author-date references: "(Jacobs 2019a, p. 3)" and reference lists "Jacobs 2020a - Jacobs (Jacobs). ..."
    re.compile(r"\(([A-Z][A-Za-z&'’\-]+(?:\s+[A-Z][A-Za-z&'’\-]+){0,3})\s+(?:19|20)\d{2}[a-z]?[,;)\s]"),
    re.compile(r"^([A-Z][A-Za-z&'’\-]+(?:\s+[A-Z][A-Za-z&'’\-]+){0,3})\s+(?:19|20)\d{2}[a-z]?\s+[-–—]\s"),
]
STRUCTURE = [  # (pattern with one name group, role)
    (re.compile(rf"^(?:From|FROM)\s*:\s*{PERSON}"), "author"),
    (re.compile(rf"^(?:To|TO)\s*:\s*{PERSON}"), "recipient"),
    (re.compile(rf"^Dear\s+(?:(?:{TITLE_WORDS})\s+)?{PERSON}"), "recipient"),
    (re.compile(rf"(?:/s/|Sincerely,|Respectfully submitted,|Respectfully,|Best regards,|Regards,)\s*(?:/s/\s*)?{PERSON}"), "signer"),
    (re.compile(rf"\b(?:Submitted|Prepared|Filed|Signed) by:?\s+{PERSON}"), "author"),
]


def _acronym_fits(full: str, acr: str) -> bool:
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z&][\w'’.\-]*", full) if w.lower() not in {"of", "and", "for", "on", "the"} or w == "&")
    return acr[0].upper() == initials[:1].upper() and len(acr) <= len(initials) + 2


def find_in_line(line: str) -> list[dict]:
    """Pattern and structure candidates in one line: [{start, end, hint, source, role}]."""
    out = []
    for m in ORG_RE.finditer(line):
        out.append({"start": m.start(1), "end": m.end(1), "hint": "organization", "source": "pattern", "role": ""})
    for m in PLACE_GOV_RE.finditer(line):
        out.append({"start": m.start(1), "end": m.end(1), "hint": "government", "source": "pattern", "role": ""})
    for m in TITLED_RE.finditer(line):
        out.append({"start": m.start(1), "end": m.end(1), "hint": "person", "source": "title", "role": ""})
    for pattern, role in STRUCTURE:
        for m in pattern.finditer(line):
            out.append({"start": m.start(1), "end": m.end(1), "hint": "person", "source": "structure", "role": role})
    for pattern in CITE_AUTHOR_RE:
        for m in pattern.finditer(line):
            out.append({"start": m.start(1), "end": m.end(1), "hint": "", "source": "citation", "role": ""})
    for m in ACRONYM_DEF_RE.finditer(line):
        if _acronym_fits(m.group(1), m.group(2)):
            out.append({"start": m.start(1), "end": m.end(1), "hint": "", "source": "definition", "role": "", "acronym": m.group(2)})
            out.append({"start": m.start(2), "end": m.end(2), "hint": "", "source": "definition", "role": "", "defines": m.group(1)})
    for m in ROLE_DEF_RE.finditer(line):
        out.append({"start": m.start(1), "end": m.end(1), "hint": "", "source": "definition", "role": m.group(2).lower()})
    return out


class GlinerCache:
    """GLiNER results per line, keyed by text hash, in the build cache."""

    def __init__(self, cache_dir: Path, finder: str = GLINER_MODEL):
        import duckdb
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.finder = finder
        self.config = hashlib.sha256(json.dumps([finder, GLINER_LABELS, GLINER_THRESHOLD]).encode()).hexdigest()[:16]
        self.con = duckdb.connect(str(cache_dir / "names.duckdb"))
        self.con.execute("CREATE TABLE IF NOT EXISTS gliner (config VARCHAR, h VARCHAR, result VARCHAR, PRIMARY KEY (config, h))")
        self.con.execute("CREATE TABLE IF NOT EXISTS judge (key VARCHAR PRIMARY KEY, result VARCHAR)")

    def run(self, lines: list[str]) -> dict[str, list]:
        hashes = {hashlib.sha256(t.encode()).hexdigest(): t for t in lines}
        self.con.execute("CREATE OR REPLACE TEMP TABLE want AS SELECT unnest(?::VARCHAR[]) AS h", [list(hashes)])
        done = {h: json.loads(r) for h, r in self.con.execute(
            "SELECT g.h, g.result FROM gliner g JOIN want USING (h) WHERE g.config = ?", [self.config]).fetchall()}
        todo = [h for h in hashes if h not in done]
        print(f"[names] name finder: {len(done)} lines already read · {len(todo)} to read", flush=True)
        if todo:
            import torch
            from gliner import GLiNER
            model = GLiNER.from_pretrained(self.finder)
            model = model.to("mps" if torch.backends.mps.is_available() else "cpu")
            step = 512
            t0 = time.time()
            for i in range(0, len(todo), step):
                chunk = todo[i:i + step]
                results = model.inference([hashes[h] for h in chunk], GLINER_LABELS, threshold=GLINER_THRESHOLD, batch_size=16)
                rows = [(self.config, h, json.dumps([[e["start"], e["end"], e["label"], round(e["score"], 3)] for e in r]))
                        for h, r in zip(chunk, results)]
                self.con.executemany("INSERT OR REPLACE INTO gliner VALUES (?, ?, ?)", rows)
                done.update({h: json.loads(r) for _, h, r in rows})
                n = min(i + step, len(todo))
                print(f"[names] name finder: {n}/{len(todo)} lines · {(time.time() - t0) / n * (len(todo) - n) / 60:.0f} min left", flush=True)
        return {hashes[h]: v for h, v in done.items()}


def split_long(line: str, limit: int = 1100) -> list[tuple[int, str]]:
    """GLiNER reads ~384 words; long lines are read in sentence-aligned pieces."""
    if len(line) <= limit:
        return [(0, line)]
    pieces, start = [], 0
    while start < len(line):
        end = min(len(line), start + limit)
        if end < len(line):
            cut = max(line.rfind(". ", start, end), line.rfind("; ", start, end))
            end = cut + 2 if cut > start + limit // 2 else end
        pieces.append((start, line[start:end]))
        start = end
    return pieces


# ---------------------------------------------------------------------------
# 2. Clean
# ---------------------------------------------------------------------------

ROLE_WORDS = {
    "applicant", "staff", "commission", "committee", "intervenor", "intervenors", "petitioner", "project owner", "owner",
    "hearing officer", "presiding member", "associate member", "the city", "the county", "the state", "city", "county",
    "state", "district", "agency", "department", "board", "court", "the court", "counsel", "respondent", "complainant",
    "commenter", "public", "energy commission", "energy commission staff", "cec staff", "the applicant", "the commission",
    "the committee", "lead agency", "responsible agency", "trustee agency", "the project", "project", "cpm",
    "compliance project manager", "parties", "the parties", "docket unit", "public adviser", "public advisor",
}
TITLE_PREFIX_RE = re.compile(rf"^(?:(?:{TITLE_WORDS})\s+)+")
SMALL = {"of", "and", "for", "on", "the", "to", "in", "at", "by", "&"}


def clean_surface(text: str) -> str:
    t = " ".join(text.replace("’", "'").split()).strip(" ,.;:-–—\"'“”()[]")
    t = re.sub(r"\s*\([^)]*\)?\s*$", "", t)  # "Name (ABC)" or a cut-off "Name (ABC"
    t = re.sub(r"'s$", "", t)
    t = re.sub(r"^(?:the|The|THE)\s+", "", t)
    t = TITLE_PREFIX_RE.sub("", t).strip(" ,.")
    return t


JOB_WORDS = {"senior", "planner", "manager", "director", "engineer", "environmental", "counsel", "attorney", "officer",
             "staff", "specialist", "analyst", "associate", "principal", "project", "presiding", "commissioner",
             "member", "chair", "president", "secretary", "assistant", "supervisor", "advisor", "adviser", "scientist",
             "consultant", "coordinator", "executive", "general", "deputy", "chief", "vice", "esq", "phd", "ph.d", "p.e"}


def person_name(name: str) -> str:
    """Cut job titles and employers off a person's name: "Lisa Worrall, Senior Environmental Planner, ..." -> "Lisa Worrall"."""
    name = name.split(",")[0].strip()
    words = []
    for w in name.split():
        if words and w.lower().strip(".") in JOB_WORDS:
            break
        words.append(w)
    return " ".join(words[:4])


def plausible(name: str) -> bool:
    if not (2 <= len(name) <= 90) or not re.search(r"[A-Z]", name) or name[0].islower():
        return False
    if name.lower() in ROLE_WORDS or re.fullmatch(r"[\d\W]+", name):
        return False
    words = name.split()
    if len(words) > 12 or sum(w[0].isupper() or w.lower() in SMALL for w in words) < len(words) * 0.7:
        return False
    return True


def ascii_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


LEGAL_SUFFIX = re.compile(r"\b(?:inc|incorporated|llc|l l c|lp|l p|llp|corp|corporation|co|company|ltd)$")


def key_of(name: str, kind: str = "") -> str:
    k = re.sub(r"[^a-z0-9& ]+", " ", ascii_lower(name))
    k = " ".join(k.replace("&", " and ").split())
    k = re.sub(r"^ca ", "california ", k)
    k = re.sub(r"\b(?:united states|u s)\b", "us", k)
    if kind != "person":
        k = re.sub(r"^the ", "", k)
        k = LEGAL_SUFFIX.sub("", k).strip()
    return k


def titlecase_caps(name: str) -> str:
    if not name.isupper() or len(name) <= 6 and " " not in name:  # keep acronyms
        return name
    words = name.split()
    return " ".join(w.lower() if i and w.lower() in SMALL else w.capitalize() for i, w in enumerate(words))


# ---------------------------------------------------------------------------
# 3. Judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You label candidate names found in legal documents. For each numbered candidate decide:
- keep: true only if it is the proper name of a specific person or a specific organization (agency, company, court, group). Roles ("the Applicant", "staff", "Hearing Officer"), generic groups ("City Council" without a city), places, regions, air basins, facilities, power plants, data centers, projects, sites, buildings, plans, programs, studies, assessments, documents, tables, species and laws are false.
- type: "person", "government", "company", "court" or "organization".
- full_name: the fullest form of the same name that appears word-for-word in its context (else the candidate itself), without titles. Never invent words.
Answer ONLY a JSON array, one object per candidate: [{"id": 0, "keep": true, "type": "...", "full_name": "..."}, ...]

"""

MERGE_PROMPT = """Each numbered pair holds two names found in the same set of legal documents, with a sentence for each. Decide whether both names refer to the same person or organization (for example a spelling or scanning error), not merely similar ones.
Answer ONLY a JSON array: [{"id": 0, "same": true}, ...]

"""


SERVERS = {  # local model servers that speak the OpenAI chat format
    "lmstudio": "http://127.0.0.1:1234/v1/chat/completions",
    "ollama": "http://127.0.0.1:11434/v1/chat/completions",
}


class Judge:
    """A local language model behind one call: label(items) / same(pairs). Answers are cached."""

    def __init__(self, spec: str, cache: GlinerCache | None = None):
        self.spec = spec
        self.backend, self.model = spec.split(":", 1)
        self.cache = cache
        self._mlx = None

    def _ask(self, text: str, max_tokens: int) -> str:
        if self.backend == "mlx":
            from mlx_lm import generate, load
            if self._mlx is None:
                self._mlx = load(self.model)
            model, tok = self._mlx
            prompt = tok.apply_chat_template([{"role": "user", "content": text}], add_generation_prompt=True,
                                             tokenize=False, enable_thinking=False)
            return generate(model, tok, prompt=prompt, max_tokens=max_tokens)
        import urllib.request
        url = SERVERS[self.backend]
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": text + "\n/no_think"}]}
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]

    def _batched(self, prompt: str, items: list[str], parse, per_item_tokens: int) -> list:
        keys = [hashlib.sha256(json.dumps([PROMPT_VERSION, self.spec, prompt[:40], it]).encode()).hexdigest() for it in items]
        cached = dict(self.cache.con.execute("SELECT key, result FROM judge WHERE key IN (SELECT unnest(?::VARCHAR[]))", [keys]).fetchall())
        todo = [i for i, k in enumerate(keys) if k not in cached]
        print(f"[names] judge ({self.model}): {len(items) - len(todo)} already decided · {len(todo)} to decide", flush=True)
        t0 = time.time()
        for n, start in enumerate(range(0, len(todo), JUDGE_BATCH), 1):
            batch = todo[start:start + JUDGE_BATCH]
            text = prompt + "\n".join(f"{j}. {items[i]}" for j, i in enumerate(batch))
            answers = {}
            for attempt in range(2):
                raw = re.sub(r"^.*?</think>", "", self._ask(text, per_item_tokens * len(batch) + 200), flags=re.S)
                try:
                    answers = {int(a["id"]): a for a in json.loads(raw[raw.find("["):raw.rfind("]") + 1]) if isinstance(a, dict)}
                    break
                except (ValueError, KeyError, TypeError):
                    continue
            rows = [(keys[i], json.dumps(answers.get(j))) for j, i in enumerate(batch) if j in answers]
            if rows:
                self.cache.con.executemany("INSERT OR REPLACE INTO judge VALUES (?, ?)", rows)
                cached.update(rows)
            done = start + len(batch)
            if n % 10 == 0 or done == len(todo):
                print(f"[names] judge: {done}/{len(todo)} · {(time.time() - t0) / done * (len(todo) - done) / 60:.0f} min left", flush=True)
        return [parse(json.loads(cached[k]) if k in cached and cached[k] != "null" else None) for k in keys]

    def label(self, items: list[tuple[str, list[str]]]) -> list[dict | None]:
        text = [f'"{name}" — context: ' + " | ".join(f'"{c}"' for c in ctx) for name, ctx in items]
        return self._batched(JUDGE_PROMPT, text, lambda a: a, 30)

    def same(self, pairs: list[tuple[str, str, str, str]]) -> list[bool]:
        text = [f'"{a}" ("{ca}")  vs  "{b}" ("{cb}")' for a, ca, b, cb in pairs]
        return self._batched(MERGE_PROMPT, text, lambda a: bool(a and a.get("same")), 12)


# ---------------------------------------------------------------------------
# 4-5. Join and write
# ---------------------------------------------------------------------------

# "California Department", "the State Board": not a name on its own
GENERIC_FRAGMENT = re.compile(rf"(?:(?:California|State|U\.S\.|US|Federal|Regional|County|City|Air|Water)\s+)?(?:{ORG_TAIL})")

KINDS = {"person": "person", "government": "government", "company": "company", "court": "court", "organization": "organization",
         "government agency": "government"}


def extract(rows: list[dict], cache_dir: Path, judge_spec: str = JUDGE_DEFAULT, finder: str = GLINER_MODEL) -> list[dict]:
    rows = [r for r in rows if r["content_type"] != "visual_evidence"]
    cache = GlinerCache(cache_dir, finder)
    try:
        return _extract(rows, cache, Judge(judge_spec, cache))
    finally:
        cache.con.close()


def _extract(rows: list[dict], cache: GlinerCache, judge: Judge) -> list[dict]:
    # ---- 1. find ----
    passage_line_list = [(r, passage_lines(r["passage_text"])) for r in rows]
    pieces = {p for _, lines in passage_line_list for line in lines for _, p in split_long(line)}
    gl = cache.run(sorted(pieces))
    mentions = []
    for r, lines in passage_line_list:
        for li, line in enumerate(lines):
            found = find_in_line(line)
            for off, piece in split_long(line):
                for s, e, label, score in gl.get(piece, []):
                    found.append({"start": off + s, "end": off + e, "hint": KINDS[label], "source": "finder",
                                  "role": "", "score": score})
            for f in found:
                surface = line[f["start"]:f["end"]]
                name = clean_surface(surface)
                if not plausible(name) and not f.get("defines"):
                    continue
                mentions.append({**f, "row": r, "line": line, "surface": surface, "name": name})
    print(f"[names] {len(mentions)} candidate mentions from {len(rows)} passages", flush=True)

    # acronyms: per document and dossier-wide (most common definition wins)
    doc_acr, all_acr = defaultdict(dict), defaultdict(Counter)
    role_defs = defaultdict(set)  # name key -> roles defined for it
    for m in mentions:
        if m.get("acronym"):
            doc_acr[m["row"]["doc_id"]].setdefault(m["acronym"], m["name"])
            all_acr[m["acronym"]][m["name"]] += 1
        if m["source"] == "definition" and m["role"]:
            role_defs[key_of(m["name"])].add(m["role"])
    acr_global = {a: c.most_common(1)[0][0] for a, c in all_acr.items()}
    # every later use of a defined acronym is a candidate too ("CEC staff", "per BAAQMD")
    if acr_global:
        acr_re = re.compile(r"\b(" + "|".join(sorted(map(re.escape, acr_global), key=len, reverse=True)) + r")\b(?![a-z'])")
        for r, lines in passage_line_list:
            for line in lines:
                for m in acr_re.finditer(line):
                    mentions.append({"start": m.start(1), "end": m.end(1), "hint": "", "source": "acronym", "role": "",
                                     "row": r, "line": line, "surface": m.group(1), "name": m.group(1)})

    # ---- 3. judge each distinct name once ----
    by_key = defaultdict(list)
    for m in mentions:
        if m.get("defines"):
            continue
        by_key[key_of(m["name"], m["hint"])].append(m)
    keys = sorted(by_key)
    items = []
    for k in keys:
        ms = by_key[k]
        name = Counter(m["name"] for m in ms).most_common(1)[0][0]
        ctx, seen_docs = [], set()
        for m in sorted(ms, key=lambda m: (m["source"] == "finder", m["row"]["passage_id"])):
            if m["row"]["doc_id"] in seen_docs:
                continue
            seen_docs.add(m["row"]["doc_id"])
            ctx.append(sentence_around(m["line"], m["start"], m["end"])[:300])
            if len(ctx) == 2:
                break
        items.append((name, ctx))
    labels = judge.label(items)
    decided = {}
    for k, (name, ctx), lab in zip(keys, items, labels):
        if not lab or not lab.get("keep") or lab.get("type") not in KINDS:
            continue
        full = clean_surface(str(lab.get("full_name") or ""))
        full = re.sub(r"\s*\([^)]*\)\s*$", "", full).strip()
        # the fuller form must be written in one of the contexts, and must contain the candidate's last word
        if not full or not any(full.lower() in c.lower() for c in ctx) or not plausible(full) \
                or name.split()[-1].lower().strip(".") not in full.lower():
            full = name
        kind = KINDS[lab["type"]]
        full = titlecase_caps(full)
        if kind == "person":
            full = person_name(full)
            if not plausible(full):
                continue
        decided[k] = {"kind": kind, "name": full, "short": titlecase_caps(person_name(name) if kind == "person" else name)}
    print(f"[names] judge kept {len(decided)} of {len(keys)} distinct names", flush=True)

    # ---- 4. join ----
    # The fuller form is used only where it is written in the mention's own line: a fragment such as
    # "California Department" must not take the full name it had in some other document.
    final = []
    for k, ms in by_key.items():
        d = decided.get(k)
        if not d:
            continue
        for m in ms:
            if d["name"].lower() in m["line"].lower():
                final.append({**m, "kind": d["kind"], "entity": d["name"]})
            elif not GENERIC_FRAGMENT.fullmatch(d["short"]):
                final.append({**m, "kind": d["kind"], "entity": d["short"]})
    # acronyms -> definition (same document first, then the dossier's usual one)
    for m in final:
        a = m["entity"]
        if re.fullmatch(r"[A-Z][A-Z&]{1,9}", a):
            full = doc_acr[m["row"]["doc_id"]].get(a) or acr_global.get(a)
            if full and decided.get(key_of(full, m["kind"])):
                m["entity"] = decided[key_of(full, m["kind"])]["name"]
    # people: surname-only or initial forms -> the one full name in the same document
    persons_by_doc = defaultdict(set)
    for m in final:
        if m["kind"] == "person" and len(m["entity"].split()) >= 2 and not re.search(r"\b[A-Z]\.$", m["entity"].split()[0]):
            persons_by_doc[m["row"]["doc_id"]].add(m["entity"])
    for m in final:
        if m["kind"] != "person":
            continue
        parts = m["entity"].replace(".", ". ").split()
        if len(parts) == 1 or (len(parts) == 2 and len(parts[0].rstrip(".")) == 1):
            last, first = parts[-1].lower(), parts[0][0].lower() if len(parts) == 2 else ""
            match = [p for p in persons_by_doc[m["row"]["doc_id"]] if p.split()[-1].lower() == last
                     and (not first or p[0].lower() == first)]
            if len(match) == 1:
                m["entity"] = match[0]
            elif len(parts) == 1:
                m["drop"] = True  # a lone surname we cannot pin to one person
    final = [m for m in final if not m.get("drop")]

    # across documents: people with and without middle names/initials
    def person_core(n):
        p = [w for w in n.replace(".", ". ").split() if len(w.rstrip(".")) > 1]
        return (ascii_lower(p[0]), ascii_lower(p[-1])) if len(p) >= 2 else None
    cores = defaultdict(Counter)
    for m in final:
        if m["kind"] == "person" and person_core(m["entity"]):
            cores[person_core(m["entity"])][m["entity"]] += 1
    for m in final:
        c = person_core(m["entity"]) if m["kind"] == "person" else None
        if c and c in cores:
            m["entity"] = max(cores[c], key=lambda n: (cores[c][n] + len(n) / 100, n))

    # one display name per entity: same key -> most common, readable spelling
    spell = defaultdict(Counter)
    for m in final:
        spell[(m["kind"] == "person", key_of(m["entity"], m["kind"]))][m["entity"]] += 1
    canon = {k: max(c, key=lambda n: (not n.isupper(), c[n], len(n))) for k, c in spell.items()}
    for m in final:
        m["entity"] = canon[(m["kind"] == "person", key_of(m["entity"], m["kind"]))]

    # near-identical spellings (scanning errors): the judge decides
    counts = Counter((m["kind"], m["entity"]) for m in final)
    ents = sorted(counts)
    pairs = []
    for i, (kind, a) in enumerate(ents):
        ka = key_of(a, kind)
        for kind2, b in ents[i + 1:i + 40]:
            if kind2 != kind or b == a:
                continue
            kb = key_of(b, kind)
            if len(ka) >= 8 and ka[0] == kb[0] and abs(len(ka) - len(kb)) <= 3 \
                    and difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.9:
                pairs.append((kind, a, b))
    sample = {}
    for m in final:
        sample.setdefault(m["entity"], sentence_around(m["line"], m["start"], m["end"])[:200])
    verdicts = judge.same([(a, sample[a], b, sample[b]) for _, a, b in pairs]) if pairs else []
    merge = {}
    for (kind, a, b), same in zip(pairs, verdicts):
        if same:
            keep, drop = (a, b) if counts[(kind, a)] >= counts[(kind, b)] else (b, a)
            merge[drop] = merge.get(keep, keep)
    for m in final:
        while m["entity"] in merge:
            m["entity"] = merge[m["entity"]]
    print(f"[names] {len(pairs)} near-identical spellings checked · {len(merge)} joined", flush=True)

    # ---- 5. rows ----
    sources = defaultdict(set)
    for m in final:
        sources[(m["row"]["passage_id"], m["entity"])].add(m["source"])
    out, seen = [], set()
    for m in sorted(final, key=lambda m: (m["source"] == "finder", -len(m["role"]))):
        mk = (m["row"]["passage_id"], m["entity"], m["start"])
        if mk in seen:
            continue
        # overlapping finds of the same entity in a line collapse to one mention
        if any((m["row"]["passage_id"], m["entity"], s) in seen for s in range(m["start"] - 60, m["end"] + 1)):
            continue
        seen.add(mk)
        src = sources[(m["row"]["passage_id"], m["entity"])]
        role = m["role"] if m["role"] in {"author", "recipient", "signer"} else ""
        out.append({**{k: m["row"][k] for k in ROW_KEYS},
                    "entity": m["entity"], "kind": m["kind"], "role": role,
                    "defined_as": ",".join(sorted(role_defs.get(key_of(m["entity"]), set()))),
                    "mention_text": m["line"][m["start"]:m["end"]],
                    "sentence": sentence_around(m["line"], m["start"], m["end"]),
                    "confidence": "high" if len(src) > 1 or src & {"pattern", "title", "structure", "definition", "acronym"} else "medium"})
    print(f"[names] {len(out)} mentions of {len({(o['kind'], o['entity']) for o in out})} people and organizations", flush=True)
    return out


if __name__ == "__main__":
    line = 'SJC02 LLC ("Applicant") filed comments. Sincerely, Scott A. Galati'
    found = {(line[f["start"]:f["end"]], f["source"], f["role"]) for f in find_in_line(line)}
    assert ("SJC02 LLC", "pattern", "") in found and ("SJC02 LLC", "definition", "applicant") in found, found
    assert ("Scott A. Galati", "structure", "signer") in found, found
    line = "Presiding Member Commissioner Karen Douglas and the Bay Area Air Quality Management District (BAAQMD) met the City of San Jose."
    found = {(line[f["start"]:f["end"]], f["source"]) for f in find_in_line(line)}
    assert {("Karen Douglas", "title"), ("Bay Area Air Quality Management District", "pattern"),
            ("BAAQMD", "definition"), ("City of San Jose", "pattern")} <= found, found
    assert clean_surface("the Commission's") == "Commission" and not plausible("Commission")
    assert clean_surface("Commissioner Karen Douglas") == "Karen Douglas"
    assert key_of("Kimley-Horn and Associates, Inc.") == key_of("Kimley Horn & Associates")
    assert key_of("San José") == key_of("San Jose")
    assert clean_surface("California Unions for Reliable Energy (CURE") == "California Unions for Reliable Energy"
    assert person_name("Lisa Worrall Senior Environmental Planner California Energy Commission") == "Lisa Worrall"
    assert person_name("Karen Douglas, Presiding Commissioner, California Energy Commission") == "Karen Douglas"
    assert key_of("CA Energy Commission") == key_of("California Energy Commission")
    assert key_of("United States Fish and Wildlife Service") == key_of("U.S. Fish and Wildlife Service")
    line = "noise limits (Jacobs 2019a - section 3.13.5) and"
    assert ("Jacobs", "citation") in {(line[f["start"]:f["end"]], f["source"]) for f in find_in_line(line)}
    assert GENERIC_FRAGMENT.fullmatch("California Department") and not GENERIC_FRAGMENT.fullmatch("Department of Water Resources")
    print("names self-check OK")
