"""evaluate.py — quality tools behind `./wehpah eval …`.

The labeled data belongs to each dossier, not to the tool, so it lives next to dossier.toml:

  evaluation/names_gold.json   passages with their people and organizations marked by hand
  evaluation/judge_cases.json  candidate names with the right keep/type/full-name answer

  ./wehpah eval sample [--n 30]          draw passages to label -> evaluation/names_to_label.json
  ./wehpah eval names                    precision and recall of the built names.parquet
  ./wehpah eval judge <model spec> …     compare judge models with the build's exact prompt

See docs/WORKFLOW.md for how these fit into adapting the pipeline to a new dossier.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import names

EVAL_DIR = "evaluation"


def _gold(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"No labels yet: {path}. Run  ./wehpah eval sample  and label the passages first (docs/WORKFLOW.md).")
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


def _match(gold: str, found: str) -> bool:
    """A found name matches a labeled one (or one of its |-separated alternatives) when either contains the other."""
    f = f" {names.key_of(found)} "
    return any(g in f or f in g for g in (f" {names.key_of(alt)} " for alt in gold.split("|")))


def eval_names(settings) -> tuple[float, float]:
    """Precision and recall of the release's names.parquet against evaluation/names_gold.json."""
    import duckdb
    names.configure(settings.profile)
    gold = _gold(settings.root / EVAL_DIR / "names_gold.json")
    path = settings.output / "names.parquet"
    if not path.is_file():
        raise SystemExit(f"{path} does not exist; build first (`./wehpah build`).")
    found: dict[str, set] = {}
    for pid, entity in duckdb.sql(f"SELECT passage_id, entity FROM read_parquet('{path.as_posix()}')").fetchall():
        if pid in gold:
            found.setdefault(pid, set()).add(entity)
    tp = fp = fn = 0
    for pid, labels in gold.items():
        pred = found.get(pid, set())
        hits = [g for g in labels if any(_match(g, p) for p in pred)]
        wrong = [p for p in pred if not any(_match(g, p) for g in labels)]
        missed = [g for g in labels if g not in hits]
        tp, fp, fn = tp + len(hits), fp + len(wrong), fn + len(missed)
        if wrong or missed:
            print(f"  {pid[:70]}: " + "; ".join(filter(None, [f"wrong {wrong}" if wrong else "",
                                                               f"missed {missed}" if missed else ""])))
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    print(f"precision {p:.0%} ({tp}/{tp + fp}) · recall {r:.0%} ({tp}/{tp + fn}) · {len(gold)} labeled passages")
    return p, r


def eval_judge(settings, specs: list[str]) -> None:
    """Score judge models on evaluation/judge_cases.json with the build's exact prompt."""
    names.configure(settings.profile)
    path = settings.root / EVAL_DIR / "judge_cases.json"
    if not path.is_file():
        raise SystemExit(f"No judge cases yet: {path} (format: docs/WORKFLOW.md).")
    cases = json.loads(path.read_text())
    norm = lambda n: names.clean_surface(re.sub(r"\s*\([^)]*\)", "", n or "")).lower()
    items = "\n".join(f'{i}. "{c["name"]}" — context: "{c["context"]}"' for i, c in enumerate(cases))
    for spec in specs:
        try:
            judge = names.Judge(spec)
            judge._ask("Say OK.", 5)  # load the model before timing
            t = time.time()
            raw = re.sub(r"^.*?</think>", "", judge._ask(names.JUDGE_PROMPT + items, 4000), flags=re.S)
            secs = time.time() - t
            got = {int(a["id"]): a for a in json.loads(raw[raw.find("["):raw.rfind("]") + 1])}
        except Exception as exc:
            print(f"{spec:50} failed: {exc}")
            continue
        keep_ok = type_ok = name_ok = kept = 0
        misses = []
        for i, c in enumerate(cases):
            a = got.get(i, {})
            if bool(a.get("keep")) == c["keep"]:
                keep_ok += 1
            else:
                misses.append(c["name"])
            if c["keep"]:
                kept += 1
                type_ok += a.get("type") in c.get("types", [c["type"]])
                name_ok += norm(a.get("full_name", "")) == norm(c["full_name"])
        print(f"{spec:50} keep {keep_ok}/{len(cases)} · type {type_ok}/{kept} · name {name_ok}/{kept} · {secs:5.1f}s"
              + (f" · wrong: {', '.join(misses)}" if misses else ""))


def sample(settings, n: int = 30, seed: str = "") -> Path:
    """Draw passages that likely name people or organizations, skipping ones already labeled,
    into evaluation/names_to_label.json for a person (or an agent, then a person) to label."""
    import duckdb
    corpus = settings.output / "corpus.parquet"
    if not corpus.is_file():
        raise SystemExit("Build first (`./wehpah build`): sampling reads the built passages.")
    out_dir = settings.root / EVAL_DIR
    done = set(_gold(out_dir / "names_gold.json")) if (out_dir / "names_gold.json").is_file() else set()
    rows = duckdb.execute(f"""
        SELECT passage_id, passage_text FROM read_parquet('{corpus.as_posix()}')
        WHERE content_type = 'text' AND length(passage_text) BETWEEN 120 AND 900
          AND regexp_matches(passage_text, '[A-Z][a-z]+ [A-Z]\\.? ?[A-Z][a-z]+|[A-Z][a-z]+ (Commission|District|Department|Agency|Board|Inc\\.|LLC|Company|Corporation)')
        ORDER BY hash(passage_id || ?) LIMIT ?""", [seed or str(len(done)), n + len(done)]).fetchall()
    todo = {pid: {"text": text, "names": []} for pid, text in rows if pid not in done}
    todo = dict(list(todo.items())[:n])
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "names_to_label.json"
    path.write_text(json.dumps({"_how": "For each passage, list every person and organization it names in 'names'. "
                                        "Use 'A|B' when either name is right (an acronym and its full form). Do not list "
                                        "places, facilities, projects, laws, documents, roles ('the Applicant') or lone first "
                                        "names. Then run: ./wehpah eval add-labels", **todo}, indent=2, ensure_ascii=False))
    print(f"[eval] {len(todo)} passages to label in {path}")
    return path


def add_labels(settings) -> None:
    """Move labeled passages from names_to_label.json into names_gold.json."""
    out_dir = settings.root / EVAL_DIR
    todo_path, gold_path = out_dir / "names_to_label.json", out_dir / "names_gold.json"
    todo = {k: v for k, v in json.loads(todo_path.read_text()).items() if not k.startswith("_")}
    gold = json.loads(gold_path.read_text()) if gold_path.is_file() else {
        "_note": "Hand-labeled people and organizations per passage. 'A|B' accepts either name."}
    for pid, entry in todo.items():
        gold[pid] = entry["names"]
    gold_path.write_text(json.dumps(gold, indent=2, ensure_ascii=False))
    todo_path.unlink()
    print(f"[eval] {len(todo)} passages added · {len(gold) - 1} labeled in {gold_path}")
