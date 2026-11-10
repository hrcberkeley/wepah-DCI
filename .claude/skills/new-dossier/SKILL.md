---
name: new-dossier
description: Build a WeHPAH! dossier for a new folder of PDFs, or adapt the pipeline (profile, labels, judge model) to a new language, jurisdiction or subject, and measure the result. Use when the user wants to set up, adapt, evaluate or improve a dossier.
---

Follow docs/WORKFLOW.md in this repository, in order. It is the single source of truth; this skill only
adds how to work through it in a session.

1. Read docs/WORKFLOW.md, then docs/PROFILES.md and the parts of docs/PIPELINE.md the task touches.
2. Ask the person the step-0 questions (may the documents be public; language and jurisdiction;
   which machine builds) before running anything that downloads models or builds.
3. Keep a task list with one item per workflow step. Run long builds in the background and report
   progress from their output.
4. When drafting labels for evaluation/names_gold.json, show your drafts passage by passage and add
   them only after the person has reviewed and corrected them.
5. After every profile or code change: rebuild, run `./wehpah eval names`, and report the printed
   precision, recall and the wrong/missed lists unedited. Keep the change only if the numbers improve.
6. Record each decision with its evidence (profile comments, docs/PIPELINE.md, or the dossier's notes).
7. Never run `./wehpah publish`, push, or delete PDFs without the person's explicit go-ahead for that
   action, and never type or echo access tokens — ask the person to run `./wehpah login`.
