# Workflow: build a dossier for a new document collection

This is the playbook for turning a new folder of PDFs into a published WeHPAH! dossier, adapting
the pipeline to it, and proving the result is good. It is written so a person can follow it, and so
a coding agent (Claude Code, Codex, or similar) can follow it **with** a person: the steps marked
**Ask** are decisions the agent must put to the person and wait for; it must never answer them itself.

Background: [PIPELINE.md](PIPELINE.md) explains what each stage does and why;
[PROFILES.md](PROFILES.md) is the reference for the rules you will adapt.

## 0. Before anything: three questions

- **Ask: may these documents be made public?** Publishing puts every PDF and all extracted text in a
  public Hugging Face dataset. If the answer is no or unsure, build and preview locally only; never
  run `./wehpah publish`.
- **Ask: what language and jurisdiction are the documents?** This decides the starting profile
  (`en`, `us-california`, or a new one — step 3).
- **Ask: what machine will build it?** The people-and-organizations step runs a local language
  model. On an Apple-silicon Mac with ~48 GB of memory or more the default works. Otherwise plan to
  use LM Studio or Ollama with a smaller model (step 5), or set `[names] enabled = false`.

## 1. Set up

```sh
git clone <this repository> && cd <it>
# put the PDFs in raw_pdfs/ (or point `pdfs = ` in dossier.toml at their folder)
```

Create `dossier.toml` (the Studio, `./wehpah`, does this interactively):

```toml
title = "…"
description = "…"
profile = "en"                 # or the profile chosen in step 0
# publish_to = "owner/name"    # only if step 0 allowed publishing
```

Requirements: [uv](https://docs.astral.sh/uv/). The first build downloads the text-recognition,
search and name-finding models.

## 2. Trial build

```sh
./wehpah build --limit 5       # first five PDFs, quick
./wehpah preview               # open the explorer on the local build
```

Check: pages open with the highlight on the right text; the Timeline has sensible dates; Laws,
People and Organizations look plausible. A failure here is usually text recognition — see
[INGESTION.md](../ingestion/INGESTION.md) (`full_page_ocr` for poor scans).

Then the full build: `./wehpah build`. Later builds reuse all finished work.

## 3. Choose or write the profile

If a shipped profile matches the collection's language and jurisdiction, use it. Otherwise create
one (`extends = "en"`) as described in [PROFILES.md](PROFILES.md), starting with:

- `[dates] numeric_order` — **Ask** the person if unsure; a wrong order silently swaps days and months.
- `[[laws.patterns]]` for the jurisdiction's main citation forms. An agent can draft these from
  examples it finds in the passages (search `corpus.parquet` for "§", "section", "Code"), then show
  the person a sample of what each pattern finds before keeping it.
- `role_words`, `defined_roles` and `titles` for the proceeding type (court case, arbitration,
  permit hearing, legislative record …).

Rebuild after each change (`./wehpah build`; only the changed stages re-run).

## 4. Make a labeled set — the measure of quality

Without labels there is no way to tell whether a change helped. Aim for **50 passages** at first.

```sh
./wehpah eval sample --n 30          # writes evaluation/names_to_label.json
```

Fill each passage's `"names"` with every person and organization it names (rules are in the file).
Then:

```sh
./wehpah eval add-labels             # moves them into evaluation/names_gold.json
```

**An agent may draft the labels, but a person must review them before they are added.** Labels
that come from the same kind of model that is being measured only measure agreement with itself.
The agent should present its drafts passage by passage (text, proposed names) and apply the
person's corrections.

Also create `evaluation/judge_cases.json`: 20–30 names from your documents with the right answer,
including the tricky ones (roles, facilities, acronyms). Format:

```json
[{"name": "SJCE", "context": "San Jose Clean Energy (SJCE) will supply power.", "keep": true,
  "type": "government", "types": ["government", "company"], "full_name": "San Jose Clean Energy"},
 {"name": "Applicant", "context": "The Applicant shall submit the plan.", "keep": false}]
```

## 5. Choose the name judge

```sh
./wehpah eval judge mlx:<repo> lmstudio:<model> ollama:<model>
```

prints, per model, keep/drop, type and full-name accuracy on your judge cases and the time per
batch. Try the smallest models you have first and stop when one is accurate enough; record the
table in your dossier's notes (the comparison for the Microsoft Datacenter dossier is in
[PIPELINE.md](PIPELINE.md)). Set the winner:

```toml
[names]
judge = "lmstudio:qwen/qwen3.5-9b"
```

## 6. Measure, fix, repeat

```sh
./wehpah build
./wehpah eval names
```

prints precision and recall and lists every wrong and missed name. For each error, find its cause
and the narrowest fix:

| What you see | Usual fix |
|---|---|
| Facilities, projects, places kept as organizations | add the kind to `[names.judge] not_names` |
| Roles kept as names ("the Trustee") | add to `role_words` |
| Job titles stuck to people | add the word to `job_words` |
| Titled people missed ("Bailiff Smith") | add to `titles` |
| One organization under two names | `name_aliases`, or check that the acronym is defined somewhere |
| A citation form not found | add a `[[laws.patterns]]` entry |
| An error the profile cannot express | it is a pipeline change: see PIPELINE.md and add a self-check |

Change one thing at a time; keep it only if the numbers improve. Write next to the profile value
which error it fixed.

## 7. Publish

**Ask** for explicit approval, naming the dataset, before publishing — even if step 0 allowed it.

```sh
./wehpah login                       # a Hugging Face token with write access
./wehpah publish
```

Open the published dossier in the explorer with `?repo=owner/name` and check one search, one
Timeline entry and one person end to end.

## 8. Record what you did

Add to your dossier's notes (or to the profile's comments): the profile and why, the judge
comparison table, the final precision and recall with the number of labeled passages, and the
remaining known errors. The next person will start from there.

## Guardrails for agents

- Never publish, push, or delete source PDFs without the person's explicit go-ahead for that action.
- Never enter or echo access tokens; ask the person to run `./wehpah login` themselves.
- Never add model-drafted labels to `names_gold.json` without the person's review.
- Report measurements as printed by `./wehpah eval`, including the wrong and missed lists — do not
  summarize a lower score away.
- Prefer a profile change over a code change; when the code must change, keep the step general and
  put any language- or jurisdiction-specific word list in the profile.
