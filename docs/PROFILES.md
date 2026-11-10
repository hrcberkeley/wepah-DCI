# Profiles: adapting the pipeline to a language, jurisdiction and domain

The pipeline's **steps** are general: find candidates from several sources, clean them, let a
local model decide, join variants, keep every result verbatim. What those steps need to *know* —
which numeric date order to trust, which citations are laws, which words are titles or roles,
what a signature block looks like, which things are often mistaken for organizations — is
specific to a dossier's language, jurisdiction and subject. That knowledge lives in a **profile**.

```toml
# dossier.toml
profile = "us-california"        # a profile shipped in ingestion/profiles/
# profile = "profiles/my.toml"   # or your own file, relative to dossier.toml
```

Without a `profile` setting the build uses **`en`** (general English), which is deliberately
cautious: it skips numeric dates like `3/4/2021` (month or day first is unknowable), finds no
statute citations beyond named Acts, and uses only universal titles and roles.

## Shipped profiles

| Profile | For | Built from |
|---|---|---|
| `en` | Any English-language collection; the base every English profile extends | General English legal writing |
| `us-california` | US federal and California administrative proceedings | The Microsoft Datacenter docket (California Energy Commission 19-SPPE-04) |

## How profiles combine

`extends = "en"` starts from another profile. **Tables merge key by key; any other value a profile
sets replaces the inherited one** — lists are never merged. So `titles = [...]` in your profile is
the complete list of titles used, and someone reading your profile sees exactly what it does.
Keys you leave out come from the parent.

Changing a profile changes what the build finds, so the release records the profile's name and a
fingerprint (`dossier.json` → `build.profile`, `build.profile_fingerprint`). The name judge's cache is
keyed by its exact prompt, so a change to `[names.judge]` re-judges every name; nothing stale is reused.

## Every key, and what it is for

Values marked *(regex)* are regular-expression fragments; all others are literal text.

### `[dates]`

| Key | Meaning |
|---|---|
| `numeric_order` | `"mdy"` (US), `"dmy"` (most of the world) or `"none"` (skip `3/4/2021`) |
| `months` | Month names in order; the first three letters also count as abbreviations |
| `month_abbreviations` | Extra abbreviations, e.g. `{ Sept = 9 }` |

Always read regardless of profile: written-out dates in either order ("4 March 2021", "March 4,
2021"), ISO dates, month-and-year. Never read: bare years, two-digit years, relative dates.

### `[laws]`

| Key | Meaning |
|---|---|
| `eyecite_cases` | Read US court case citations with eyecite (US reporters only) |
| `jurisdiction_words` | Words never dropped when shortening a law's name, so state and federal laws with the same short name stay apart (lowercase) |
| `section_marker` *(regex)* | What introduces a section number: "Section", "§", "Part" … |
| `acronym_not_before` | An Act's acronym followed by one of these is a different instrument ("CEQA **Guidelines**") |
| `patterns` | The jurisdiction's citation forms — see below |
| `code_names` | Named codes and their abbreviations, for patterns that use `{CODE_NAMES}` |

Named Acts ("Clean Water Act") and the acronyms documents define for them are found in every
profile; that rule is in the code.

A **law pattern** is a table in `[[laws.patterns]]`:

```toml
[[laws.patterns]]
regex = '\b(\d{1,2})\s+U\.?\s?S\.?\s?C\.?\s*(?:{SEC}\s*)?{PROV}'   # placeholders below
instrument = "{1} U.S.C."      # the law's display name; {1} = regex group 1; {code} = matched code's full name
provision_group = 2            # which group is the section number (0 = none)
ignore_case = false
code_group = 1                 # only with {code}: which group matched {CODE_NAMES}
```

Placeholders: `{SEC}` = `section_marker`; `{PROV}` = one *group* matching a section number such as
`21080(b)` or `15126.6`; `{CODE_NAMES}` = every name and abbreviation in `[laws.code_names]`. Patterns
run in order and earlier ones claim their text first, so put specific forms before general ones.
"Section 12 of the X" and "X, Section 12" are joined to the pattern's match automatically.

### `[names]`

| Key | Meaning |
|---|---|
| `titles` *(regex)* | Titles before a name ("Commissioner", "Dr\."). Stripped from names; a titled name is a strong signal |
| `organization_endings` *(regex)* | Last words of organization names ("Commission", "Inc\.") |
| `place_governments` | "City of X", "County of X" … |
| `legal_suffixes` | Company forms ignored when matching ("inc", "llc"); lowercase, no punctuation |
| `connectors` | Small words inside names ("of", "and") |
| `job_words` | Words that end a person's name in a signature block ("Jane Doe, Senior Planner") |
| `role_words` | Roles that are not names when they stand alone ("staff", "the court"); lowercase |
| `defined_roles` | Roles a document can define for a party: `Acme ("Applicant")` |
| `generic_fragment_prefixes` *(regex)* | With only an organization ending after them, not a name ("State Department") |
| `name_aliases` | `[regex, replacement]` pairs applied when matching names (e.g. `"ca "` → `"california "`) |

`[names.structure]`: `from_labels`, `to_labels`, `salutations`, `sign_offs`, `authored_by` — the
wording of letter headers and signature blocks that marks a person as author, recipient or signer.

`[names.judge]`: the examples the name judge sees. `role_examples` and `generic_group_examples` are
inserted as written; `not_names` is a list of kinds of things that are **not** people or
organizations. This is the most effective lever on precision: list what your documents name that
looks like an organization (in energy siting: power plants, data centers, air basins).

## Writing a profile for a new dossier

1. **Start small.** Create `profiles/<name>.toml` (in the dossier folder, or in
   `ingestion/profiles/` to share it) with `extends = "en"` and only `[dates] numeric_order`.
2. **Build and look.** `./wehpah build`, then open the Timeline, Laws, People and Organizations views
   (`./wehpah preview`). Note what is wrong or missing.
3. **Label** 30–50 passages (`./wehpah eval sample`, see [WORKFLOW.md](WORKFLOW.md)) and measure
   (`./wehpah eval names`). The listed wrong and missed names show which key to change.
4. **Change one thing at a time, measure again.** Add the jurisdiction's citation patterns, the
   proceeding's role words and titles, and `not_names` examples for whatever the judge keeps by
   mistake. Keep a change only if the numbers improve.
5. **Record why.** Add a comment next to each value saying which error it fixed — the next person
   adapting your profile will need to know which values are safe to drop.

To share a profile, put it in `ingestion/profiles/` and add a row to the table above.
