---
version: 1
slug: "web-app-index-html"
primary_target: "web_app/index.html"
related_targets: ["web_app/style.css","web_app/src/main.js","web_app/src/pdf_stage.js"]
---

# Surface brief — Dossier Explorer workspace (`web_app/index.html`)

## Scope and visitor mode

Operate. The whole product is one workspace surface: search header + evidence stream +
PDF grounding stage. Related targets: `web_app/style.css`, `web_app/src/main.js`,
`web_app/src/pdf_stage.js`.

## Audience, job, action, proof, constraints

Lawyers working the CEC San Jose docket (19-SPPE-04) must find passages in 283 PDFs and
verify them against the original page. Proof is the highlighted bounding box on the
streamed PDF page. Constraints: three pedagogical search modes, zero AI jargon,
deterministic verification gate, bbox percentages only, body ≥15px, no build step.

## Chosen direction — "The Highlighter Essay" (The Pudding, brief-pinned)

Warm paper editorial data-essay. Cream ground, warm ink, ONE gold highlighter accent.
World contributes type (display grotesque masthead, serif passages, mono data),
palette (paper/ink/gold), density (rule-separated entries, footnote culture), and one
signature move: a single gold highlighter follows the question — it swipes under exact
word hits in the results and lands on the same gold box on the original PDF page.
Controls stay standard web controls; no artifact costume.

Memorable moment: after Search, the tall editorial opening collapses to a slim sticky
strip and nearly the whole window becomes results + page.

## Direction contract

THESIS: Documents and passages ARE the page; chrome exists only at the moment of
action. Refuses the dashboard of boxes — entries are ink on paper separated by
hairlines, and the app opens as an essay front page, not a tool panel.

OWN-WORLD: cream paper `#F7F1E6` ground, warm ink `#211B12`, single California-gold
highlighter (marks, selection, active states, PDF bbox); Bricolage Grotesque display
masthead, Source Serif 4 passages, Spline Sans Mono for numbers/tags/status; hairline
rules and footnote meta everywhere; white plates (summary box, PDF page) sit on the
paper with 1px ink-tinted borders and soft offset shadows.

STORY: the lawyer lands on a calm essay cover that says what this is and asks one
question; they search; the cover folds away; they read passages like an annotated
brief and click one; the original page opens with the gold box exactly on the words;
every claim in Ask-the-Dossier mode carries a gold footnote chip that does the same.

FIRST VIEWPORT (opening state): centered 880px column on cream — masthead "WeHPAH!"
(Bricolage 800, ~64–76px, tight tracking) with the "(we have pinpoint at home)"
tagline set inline in serif italic; one-line docket stat line in mono; the search
input as a huge serif blank with a hairline underline and a chunky ink Search button;
three mode tabs beneath (grotesque labels, numbered sequence, active = ink fill);
helper microcopy as a serif italic footnote with a gold asterisk; status line in mono
at the foot. After first search: header collapses to a 56px sticky strip (wordmark,
compact input, compact tabs, expand chevron) and the workspace takes the full height:
rule-separated entries left (~41%), PDF plate right (~59%) with ghosted mono controls.

FORM: single-workspace Operate screen; position 1 of my ordered structures (essay
cover → two-pane brief desk); the alternatives were full-height desk with permanent
rail, and a scrollytelling dossier page — rejected: the job is lookup + verification,
not narrative.
seed: none (unavailable) — direction pinned by the user's brief (The Pudding style),
confirmed by structured answers 2026-11-06; no roll was dealt.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish
review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.

## Unresolved decisions

None blocking. AGENTS.md §5 must be rewritten to match what ships (user-approved).
