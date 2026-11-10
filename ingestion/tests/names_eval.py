"""Measure people/organization finding against tests/names_gold.json.

  uv run python tests/names_eval.py ../dist_data/names.parquet

Precision: of the names shown for the labeled passages, how many are real.
Recall: of the hand-labeled names, how many were found.
A name matches when its words equal, contain, or are contained in the labeled name or one
of its alternatives ("CAISO|California Independent System Operator")
(so "Jacobs" matches "Jacobs Engineering", "City of San Jose" matches "City of San José").
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from names import key_of  # noqa: E402

GOLD = {k: v for k, v in json.loads((Path(__file__).parent / "names_gold.json").read_text()).items() if not k.startswith("_")}


def match(gold: str, found: str) -> bool:
    """gold may list alternatives: "CAISO|California Independent System Operator"."""
    f = f" {key_of(found)} "
    return any(g in f or f in g for g in (f" {key_of(alt)} " for alt in gold.split("|")))


def main(path: str) -> None:
    import duckdb
    rows = duckdb.sql(f"SELECT passage_id, entity FROM read_parquet('{path}')").fetchall()
    found = {}
    for pid, entity in rows:
        if pid in GOLD:
            found.setdefault(pid, set()).add(entity)
    tp = fp = fn = 0
    for pid, gold in GOLD.items():
        pred = found.get(pid, set())
        hits = [g for g in gold if any(match(g, p) for p in pred)]
        wrong = [p for p in pred if not any(match(g, p) for g in gold)]
        missed = [g for g in gold if g not in hits]
        tp, fp, fn = tp + len(hits), fp + len(wrong), fn + len(missed)
        if wrong or missed:
            print(f"  {pid[:60]}: " + "; ".join(filter(None, [f"wrong {wrong}" if wrong else "", f"missed {missed}" if missed else ""])))
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    print(f"precision {p:.0%} ({tp}/{tp + fp}) · recall {r:.0%} ({tp}/{tp + fn}) · {len(GOLD)} labeled passages")


if __name__ == "__main__":
    main(sys.argv[1])
