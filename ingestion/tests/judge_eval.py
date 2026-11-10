"""Compare local models as the name judge, with the exact prompt the build uses.

  uv run python tests/judge_eval.py mlx:arthurcollet/Qwen3.6-35B-A3B-mlx-mxfp8 lmstudio:qwen/qwen3.5-9b ollama:gemma3:4b

Model specs are the same as `[names] judge` in dossier.toml:
  mlx:<Hugging Face repo>   runs inside the build (Apple-silicon Macs)
  lmstudio:<model>          LM Studio's local server (start it with `lms server start`)
  ollama:<model>            Ollama's local server (`ollama serve`)

For each model it prints: keep/drop accuracy on all cases, type and full-name accuracy on
the real names, and the seconds one batch of 30 takes (one build batch is 25). The cases live in
tests/judge_cases.json; add ones from your own documents to test what matters for them.
See docs/PIPELINE.md for how the default model was chosen.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from names import JUDGE_PROMPT, Judge, clean_surface  # noqa: E402

CASES = json.loads((Path(__file__).parent / "judge_cases.json").read_text())


def norm(name: str) -> str:
    return clean_surface(re.sub(r"\s*\([^)]*\)", "", name or "")).lower()


def score(spec: str) -> None:
    judge = Judge(spec)
    items = "\n".join(f'{i}. "{c["name"]}" — context: "{c["context"]}"' for i, c in enumerate(CASES))
    judge._ask("Say OK.", 5)  # load the model before timing
    t = time.time()
    raw = judge._ask(JUDGE_PROMPT + items, 4000)
    secs = time.time() - t
    raw = re.sub(r"^.*?</think>", "", raw, flags=re.S)
    try:
        got = {int(a["id"]): a for a in json.loads(raw[raw.find("["):raw.rfind("]") + 1])}
    except Exception:
        print(f"{spec:50} unreadable answer: {raw[:120]!r}")
        return
    keep_ok = type_ok = name_ok = kept = 0
    misses = []
    for i, c in enumerate(CASES):
        a = got.get(i, {})
        if bool(a.get("keep")) == c["keep"]:
            keep_ok += 1
        else:
            misses.append(c["name"])
        if c["keep"]:
            kept += 1
            type_ok += a.get("type") in c.get("types", [c["type"]])
            name_ok += norm(a.get("full_name", "")) == norm(c["full_name"])
    print(f"{spec:50} keep {keep_ok}/{len(CASES)} · type {type_ok}/{kept} · name {name_ok}/{kept} · {secs:5.1f}s"
          + (f" · wrong: {', '.join(misses)}" if misses else ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for spec in sys.argv[1:]:
        try:
            score(spec)
        except Exception as exc:
            print(f"{spec:50} failed: {exc}")
