# How the dossier pipeline works, and why

This is the decision record for the WeHPAH! builder (`./wehpah build`). It explains each stage that runs
after text extraction, the choices made, the evidence behind them, and how to check or change them for
your own dossier. The step-by-step build guide is [ingestion/INGESTION.md](../ingestion/INGESTION.md);
adapting the pipeline to a new collection is [WORKFLOW.md](WORKFLOW.md); the rules that change per
language, jurisdiction or subject are in a **profile** ([PROFILES.md](PROFILES.md)).

## General steps, profile rules

Every decision below is marked as one of:

- **General** — part of the pipeline's logic, in the code, the same for every dossier.
- **Profile** — a word list, pattern or example that depends on language, jurisdiction or subject.
  It lives in `ingestion/profiles/<name>.toml`; the Microsoft Datacenter dossier uses
  `us-california`, which extends the general English profile `en`.

When adapting the pipeline, change profile rules; change general steps only when an error cannot
be fixed by a profile, and then keep the new step general.

## The rule every stage follows

**Nothing reaches the screen unless it is written on the page.** Every date, citation and name the
explorer shows keeps the exact words it was found in (`date_text`, `cite_text`, `mention_text`) and the
sentence around them (`sentence`), both copied verbatim from the passage text, plus the passage's
highlight box. Language models are allowed to *decide* (keep or drop, which type, are these two the same),
never to *write* what is displayed. This is why lawyers can trust a click on any result: it lands on the
words it claims.

## Stages and the files they produce

| Stage | Code | Output in the release | Needs |
|---|---|---|---|
| Text, tables and figures, with page boxes | `build_corpus.py` (Docling + OCR) | `corpus.parquet`, `clusters/` | any computer |
| Search index and subject areas | `build_corpus.py` | `centroids.json` | any computer |
| Dates | `entities.py` | `events.parquet` | any computer |
| Laws, regulations and cases | `entities.py` | `authorities.parquet` | any computer |
| People and organizations | `names.py` | `names.parquet` | Apple-silicon Mac, or a local model server |

All three entity files have one row per mention with the passage columns (`passage_id`, `doc_id`,
`doc_title`, `page_number`, `bbox_*`) and `sentence`. `dossier.json` lists them (`events_path`,
`authorities_path`, `names_path`) with counts; the explorer shows a view only when its file is listed, so
older releases keep working. `./wehpah check` refuses a release whose entity rows point at passages that
are not in the corpus.

## Dates (`entities.py`)

**What it finds:** "March 14, 2019", "14 March 2019", "Mar. 14, 2019", "2019-03-14", month-only
"June 2020" *(general)*; numeric dates like "3/14/2019" in the order the profile sets — `mdy` in
`us-california`, skipped in `en` because the order cannot be known *(profile: `[dates]`)*.

**Decisions (all general):**
- *Bare years and relative dates ("two weeks later") are not converted.* Bare years are mostly statute
  names, citations and plan horizons; relative dates need a reference date the text rarely states.
  Converting either would put invented dates on a timeline.
- *Two-digit years are skipped.* "3/4/21" cannot be dated safely across a century boundary.
- *Month-only dates need a capital letter* so the words "may" and "march" are not read as months.
- *Dates are found by patterns, not a model.* Formats are regular; patterns are exact, instant and
  free. The language model in Ask the Dossier is never asked for a date.

**Results on the Microsoft Datacenter dossier:** 7,911 date mentions, 3,801 distinct (date, document)
entries on the timeline. About a third are bare dates from page headers or footers; they stay, because the
document title gives them context and dropping them would hide filing dates.

**For another dossier:** set `[dates] numeric_order` and, for another language, `months` in the profile.
Run `uv run --project ingestion python ingestion/entities.py` after any change; it is a self-check.

## Laws, regulations and cases (`entities.py`)

**What it finds:** *(general)* named Acts and the acronyms documents define for them, with section
numbers pulled in from either side; *(profile: `[laws]` in `us-california`)* U.S.C. and C.F.R. sections; California codes ("Public Resources Code section 21080(b)",
"Cal. Pub. Res. Code § 21080"); California Code of Regulations titles; CEQA Guidelines; named Acts
("Clean Water Act", "Warren-Alquist Act") and the acronyms a document defines for them ("(CEQA)");
municipal codes; court cases (through the [eyecite](https://github.com/freelawproject/eyecite) library
from the Free Law Project). "Section 15162 of the CEQA Guidelines" and "the Act, Section 404" pull the
section number in from either side.

**Decisions:**
- *(profile)* *Patterns for statutes and regulations, eyecite only for cases.* eyecite handles formal citation styles
  well but misses the plain-English forms filings actually use ("Public Resources Code section 21166");
  case citations are the reverse, so each tool does what it does best.
- *(general)* *A case needs both party names.* Without that rule eyecite reads street addresses ("11400 S.E. 8") and
  page references as cases.
- *(general; jurisdiction words from the profile)* *One name per law.* Spelling variants share the most common spelling ("Warren-Alquist" and "Warren
  Alquist"); stray leading words fall away when the shorter name is already known ("Regulatory
  Requirements California Environmental Quality Act" → "California Environmental Quality Act"; "Federal
  Clean Air Act" → "Clean Air Act"); "... Act of 1970" joins "... Act". **A jurisdiction word is never
  dropped**, so the California and federal Endangered Species Acts stay separate.

**Results:** 5,760 citations of 155 laws and cases. Known leftovers: a handful of scanning-error names
("Warren-Alqueist Act") stay separate entries, and the "people and organizations" judge below is the
planned way to clean those too.

**For another jurisdiction:** write `[[laws.patterns]]` and `code_names` in a profile
([PROFILES.md](PROFILES.md) explains the format); `us-california.toml` is a complete example.

## People and organizations (`names.py`)

This stage runs as a five-step pipeline. Each step either finds more (early) or removes and joins (late),
so each can be tuned on its own. **All five steps are general.** The word lists and patterns they use
— titles, organization endings, role words, signature-block wording, job words, the judge's
examples, name spelling rules — come from the profile's `[names]` section.

1. **Find, recall first.** Four independent sources, so a name missed by one is caught by another:
   - **GLiNER**, a name-finding model (`urchade/gliner_multi-v2.1`), asked for *person*, *government
     agency*, *company*, *organization* and *court*;
   - **patterns** *(profile word lists)*: organization names ending in Commission, District, Department, Inc., LLC …;
     "City of …"; titled people ("Commissioner Karen Douglas"); acronym definitions ("Bay Area Air
     Quality Management District (BAAQMD)");
   - **document structure** *(profile: `[names.structure]`)*: `From:` / `To:` lines, "Dear …", signature blocks, `/s/`, "Submitted by".
     These also give a **role** (wrote, addressed to, signed), which is usually what a lawyer wants;
   - **defined roles** *(profile: `defined_roles`)*: `SJC02 LLC ("Applicant")` records that SJC02 LLC is the Applicant;
   - **later uses of defined acronyms**: once a document defines "California Energy Commission (CEC)",
     every "CEC" in the dossier is a candidate (added after the first measurement missed "CEC staff");
   - **author-date citations**: "(Jacobs 2019a, p. 3)" and reference lists "Jacobs 2020a - …" (added
     after the first measurement missed consultants cited only this way).
2. **Clean.** Remove titles, possessives, leading "the" and a trailing "(ABC)" (also a cut-off "(ABC");
   drop roles that are not names ("staff", "the Commission", "Hearing Officer") and anything that is
   mostly lowercase or digits. People's names lose job titles and employers that follow them in
   signature blocks ("Lisa Worrall, Senior Environmental Planner, California Energy Commission" →
   "Lisa Worrall").
3. **Judge.** A local language model looks at each *distinct* name once, with up to two sentences from
   different documents, and returns keep/drop, a type, and the fullest written form of the name. The
   fuller form is accepted **only if it appears word for word in one of those sentences** and contains the
   candidate's last word; otherwise the candidate stands. Answers are cached, so a rebuild only judges
   new names. The prompt names what is *not* an organization because the first version let those
   through *(profile: `[names.judge] not_names` — for this dossier facilities, power plants, data
   centers, projects, air basins, studies …)*. The cache is keyed by the exact prompt, so changing
   the examples re-judges every name.
4. **Join.**
   - The fuller form is applied **only where it is written in that mention's own line.** The name
     finder sometimes returns a fragment ("California Department" from "California Department of Water
     Resources"); without this rule the judge's expansion from one document was applied to the fragment
     everywhere, labeling Department of Water Resources citations as Fish and Wildlife. A fragment that
     is only generic words ("California Department", "State Board") and cannot be expanded is dropped.
   - Within a document: an acronym becomes the name it was defined as; a lone surname or an initial
     ("Ms. Warshaw", "K. Douglas") becomes the one person in that document with that surname. A lone
     surname that matches no one, or several people, is dropped rather than guessed.
   - Across documents: people with and without middle names or initials join ("Scott Galati" / "Scott A.
     Galati"); names that differ only in accents, punctuation, "&"/"and" or a legal suffix join
     ("Kimley-Horn and Associates, Inc." / "Kimley Horn & Associates").
   - Near-identical spellings (likely scanning errors) are **sent to the judge in pairs** and joined only
     when it says they are the same.
5. **Write** one row per mention: `entity`, `kind` (person, government, company, court, organization),
   `role`, `defined_as` (e.g. "applicant"), `mention_text`, `sentence`, and `confidence` — *high* when two
   sources agree or a reliable one (pattern, title, structure, definition) found it, else *medium*.

### Choosing the judge model

The judge was chosen by scoring every complete local model on 30 real cases with known answers
(`evaluation/judge_cases.json`), smallest first, with the exact prompt the build uses
(`./wehpah eval judge <models>`). Results on an Apple M4 Max, 128 GB, November 2026:

| Model | Keep/drop right | Type right | Full name right | Seconds for 30 |
|---|---|---|---|---|
| Qwen3.5 0.8B (LM Studio) | 21/30 | 12/18 | 10/18 | 10 |
| Qwen3.5 9B (LM Studio) | 28/30 | 16/18 | 15/18 | 25 |
| Bonsai 27B (LM Studio) | 30/30 | 16/18 | 17/18 | 26 |
| Qwen3.8 27B (LM Studio) | no usable answer | | | |
| **Qwen3.6-35B-A3B, 8-bit MLX** | **30/30** | **17/18** | **17/18** | **13** |

The largest model was both the most accurate and the fastest: it is a *mixture-of-experts* model, so only
about 3B of its 35B parameters run for each word. It needs about 35 GB of memory. On a smaller machine,
re-run the comparison with what you have and set `[names] judge`; Qwen3.5 9B was the best small option.

**Why the judge model runs at build time and not in the browser:** a 35B model is far too large to
download into a lawyer's browser, and judging once at build time means every reader gets the same,
reviewed result.

### Measuring quality

`evaluation/names_gold.json` holds passages whose people and organizations were marked by hand
(`./wehpah eval sample` draws new ones to label, `./wehpah eval add-labels` keeps them). After a build,
`./wehpah eval names` prints precision (of the names shown, how many are real) and recall (of the real names, how many were
found), and lists each wrong and missed name. Add passages from your own dossier to the file — the more
labeled passages, the more trustworthy the numbers — and re-run after any change to patterns, thresholds
or models.

### Results on the Microsoft Datacenter dossier

Measured on 50 hand-labeled passages (57 names):

| Version | Precision | Recall | What changed |
|---|---|---|---|
| First run | 83% | 86% | — |
| Current | **91%** | **91%** | the fixes described above: own-line rule for full names, job titles cut, acronym reuse, citation authors, stricter judge prompt |

The release holds 43,424 mentions of 4,343 people and organizations (11,676 distinct candidate names
judged, 5,672 kept, 127 scanning-error spellings joined after a pairwise check). Remaining errors are
mostly facilities or named rooms read as organizations or people ("Regional Wastewater Treatment Plant",
"Charles Imbrecht Hearing Room") and project acronyms ("SJDC"); `./wehpah eval names` lists them.

Moving the California rules into the `us-california` profile (November 2026) was checked by rebuilding
dates, laws and names from the same inputs: all 7,680 date, 5,615 law and 42,175 name rows were
identical to the release built before the change.

### Cost

On the M4 Max: the name finder reads the Microsoft Datacenter dossier (61,000 lines) in about 35 minutes
the first time; the judge then decides each distinct name in batches of 25 (about 75 minutes for 11,700 names). Both results are cached in
`names.duckdb` in the cache folder, so later builds only process new text and new names.

## Settings

```toml
[names]
enabled = true                                         # false skips people and organizations
judge = "mlx:arthurcollet/Qwen3.6-35B-A3B-mlx-mxfp8"   # or "lmstudio:<model>" / "ollama:<model>"
finder = "urchade/gliner_multi-v2.1"                   # GLiNER model
```

`mlx:` runs the model inside the build (Apple silicon only). `lmstudio:` and `ollama:` call those apps'
local servers (`lms server start`, `ollama serve`), which also work on Windows and Linux. Changing the
judge or the finder re-runs that step for every name; the old answers stay in the cache.

## Checks to run after changing anything

```sh
uv run --project ingestion python ingestion/entities.py         # dates and laws self-check
uv run --project ingestion python ingestion/names.py            # people/organizations self-check
uv run --project ingestion python ingestion/dossier_profile.py  # profile loading self-check
./wehpah eval judge <model spec> …                               # compare judge models
./wehpah eval names                                              # precision and recall
./wehpah check                                                   # the whole release
```
