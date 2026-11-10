#!/usr/bin/env python3
"""wehpah — build, check, preview and publish a WeHPAH! dossier.

    ./wehpah                 open the Studio (a local page that does all of the below)
    ./wehpah build           read the PDFs, build the release, check it
    ./wehpah check           check the current release
    ./wehpah preview         open the explorer on the local release
    ./wehpah login           sign in to Hugging Face (token stays in your user folder)
    ./wehpah publish         upload the checked release to Hugging Face
    ./wehpah eval …          measure quality: names | judge <models> | sample | add-labels

Settings live in dossier.toml next to your PDFs (see INGESTION.md). Only `title` is required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "dossier.toml"
REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")
TOP_KEYS = {"title", "description", "pdfs", "output", "cache", "publish_to", "profile"}
SECTIONS = {"filenames": {"title", "date"}, "advanced": {"workers", "full_page_ocr"},
            "names": {"enabled", "judge", "finder"}}

TEMPLATE = """# WeHPAH! dossier settings. Paths are relative to this file. Only `title` is required.
title = {title}
description = {description}
# pdfs = "raw_pdfs"            # folder of source PDFs (default: raw_pdfs/ if present, else this folder)
# output = "dist_data"         # where the finished release is written
# cache = ".wehpah-cache"      # saved work, so rebuilds only read new or changed PDFs
# publish_to = "owner/name"    # Hugging Face dataset used by `wehpah publish`
# profile = "en"               # language/jurisdiction rules: a name in ingestion/profiles/ or a .toml path (docs/PROFILES.md)

# [filenames]                  # optional patterns over the file name; the first group is used
# title = '^TN\\d+_\\d{{8}}T\\d{{6}}_(.+)$'
# date = '^TN\\d+_(\\d{{8}})T'     # must capture YYYYMMDD

# [advanced]
# workers = "auto"             # PDFs read at once; lower it if memory is short
# full_page_ocr = false        # read every page as an image (slower; for poor scans)

# [names]                      # people and organizations (see docs/PIPELINE.md)
# enabled = true               # needs an Apple-silicon Mac, or a local model server for the judge
# judge = "mlx:arthurcollet/Qwen3.6-35B-A3B-mlx-mxfp8"   # or "lmstudio:<model>" / "ollama:<model>"
# finder = "urchade/gliner_multi-v2.1"                   # GLiNER name-finding model
"""


@dataclass
class Settings:
    root: Path
    title: str
    description: str
    pdfs: Path
    output: Path
    cache: Path
    publish_to: str
    title_re: re.Pattern | None
    date_re: re.Pattern | None
    workers: int
    full_page_ocr: bool
    names_enabled: bool = True
    names_judge: str = ""
    names_finder: str = ""
    profile: dict | None = None


def _pattern(value, name):
    if value is None:
        return None
    try:
        pattern = re.compile(value, re.IGNORECASE)
    except (re.error, TypeError) as exc:
        raise ValueError(f"[filenames] {name} is not a valid pattern: {exc}") from exc
    if pattern.groups < 1:
        raise ValueError(f"[filenames] {name} needs one (group) around the part to use")
    return pattern


def load_settings(root: Path) -> Settings:
    path = root / CONFIG_NAME
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    for key, value in data.items():
        if key in SECTIONS:
            if not isinstance(value, dict) or set(value) - SECTIONS[key]:
                raise ValueError(f"Unknown setting in [{key}]: {', '.join(sorted(set(value) - SECTIONS[key]))}")
        elif key not in TOP_KEYS:
            raise ValueError(f"Unknown setting in {CONFIG_NAME}: {key}")
    title = str(data.get("title") or "").strip()
    publish_to = str(data.get("publish_to") or "").strip()
    if publish_to and not REPO_RE.match(publish_to):
        raise ValueError("publish_to must look like owner/name")
    names, advanced = data.get("filenames", {}), data.get("advanced", {})
    workers = advanced.get("workers", "auto")
    if workers == "auto":
        workers = max(2, min(6, (os.cpu_count() or 4) - 2))
    if not isinstance(workers, int) or workers < 1:
        raise ValueError('[advanced] workers must be a positive number or "auto"')
    full_page_ocr = advanced.get("full_page_ocr", False)
    if not isinstance(full_page_ocr, bool):
        raise ValueError("[advanced] full_page_ocr must be true or false")
    names_cfg = data.get("names", {})
    if not isinstance(names_cfg.get("enabled", True), bool):
        raise ValueError("[names] enabled must be true or false")
    judge = str(names_cfg.get("judge", ""))
    if judge and not re.match(r"^(mlx|lmstudio|ollama):\S+$", judge):
        raise ValueError('[names] judge must look like "mlx:<model>", "lmstudio:<model>" or "ollama:<model>"')
    from dossier_profile import DEFAULT_PROFILE, load_profile
    profile = load_profile(str(data.get("profile") or DEFAULT_PROFILE), root)
    default_pdfs = "raw_pdfs" if (root / "raw_pdfs").is_dir() else "."
    resolve = lambda key, default: (root / Path(str(data.get(key) or default)).expanduser()).resolve()
    pdfs, output = resolve("pdfs", default_pdfs), resolve("output", "dist_data")
    # Each build replaces the whole output folder, so the PDFs must never live inside it.
    if pdfs == output or output in pdfs.parents:
        raise ValueError("The PDF folder cannot be the output folder or inside it")
    return Settings(root=root, title=title, description=str(data.get("description") or ""),
                    pdfs=pdfs, output=output,
                    cache=resolve("cache", ".wehpah-cache"), publish_to=publish_to,
                    title_re=_pattern(names.get("title"), "title"), date_re=_pattern(names.get("date"), "date"),
                    workers=workers, full_page_ocr=full_page_ocr,
                    names_enabled=names_cfg.get("enabled", True), names_judge=judge,
                    names_finder=str(names_cfg.get("finder", "")), profile=profile)


def save_settings(root: Path, **values: str) -> None:
    """Create dossier.toml, or update its top-level keys in place (comments are kept)."""
    path = root / CONFIG_NAME
    if not path.exists():
        path.write_text(TEMPLATE.format(title=json.dumps(values.pop("title", "") or "Untitled dossier", ensure_ascii=False),
                                        description=json.dumps(values.pop("description", ""), ensure_ascii=False)))
    lines = path.read_text().splitlines()
    first_section = next((i for i, l in enumerate(lines) if re.match(r"\s*\[", l)), len(lines))
    for key, value in values.items():
        if key not in TOP_KEYS:
            raise ValueError(f"Unknown setting: {key}")
        line = f"{key} = {json.dumps(str(value), ensure_ascii=False)}"  # JSON strings are valid TOML strings
        at = next((i for i in range(first_section) if re.match(rf"{key}\s*=", lines[i])), None)
        if at is not None:
            lines[at] = line
        else:
            last = max((i for i in range(first_section) if re.match(r"\w+\s*=", lines[i])), default=-1)
            lines.insert(last + 1, line)
            first_section += 1
    text = "\n".join(lines) + "\n"
    publish_to = tomllib.loads(text).get("publish_to")  # never write a file that no longer parses
    if publish_to and not REPO_RE.match(str(publish_to)):
        raise ValueError("The Hugging Face dataset must look like owner/name")
    path.write_text(text)


def ensure_title(settings: Settings) -> Settings:
    if settings.title:
        return settings
    if not sys.stdin.isatty():
        raise SystemExit(f"Give the dossier a title first: add  title = \"…\"  to {settings.root / CONFIG_NAME}")
    title = input("Title for this dossier (shown in WeHPAH!): ").strip() or "Untitled dossier"
    save_settings(settings.root, title=title)
    print(f"Saved {settings.root / CONFIG_NAME}")
    return load_settings(settings.root)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def publish(settings: Settings, repo: str = "", assume_yes: bool = False) -> str:
    from huggingface_hub import HfApi, get_token
    from huggingface_hub.errors import RepositoryNotFoundError
    import build_corpus

    repo = repo or settings.publish_to
    if not repo or not REPO_RE.match(repo):
        raise SystemExit(f"Set where to publish: add  publish_to = \"owner/name\"  to {settings.root / CONFIG_NAME}, or pass --to owner/name")
    manifest = build_corpus.verify_corpus(settings.output)
    if (manifest.get("build") or {}).get("trial_limit"):
        raise SystemExit("This release is a trial build of only some PDFs. Build again without --limit, then publish.")
    if not get_token():
        raise SystemExit("Sign in to Hugging Face first:  ./wehpah login")
    api = HfApi()
    user = api.whoami()["name"]
    try:
        if api.dataset_info(repo).private:
            raise SystemExit(f"{repo} is private; WeHPAH! can only open public datasets. Make it public on Hugging Face first.")
        exists = True
    except RepositoryNotFoundError:
        exists = False
    print(f"Signed in as {user}.")
    print(f"This makes {manifest['document_count']} PDFs and all their extracted text PUBLIC at "
          f"https://huggingface.co/datasets/{repo}{'' if exists else ' (new dataset)'}.")
    if not assume_yes:
        if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
            raise SystemExit("Nothing was uploaded.")
    if not exists:
        api.create_repo(repo, repo_type="dataset", private=False)
    print("[publish] uploading (files already on Hugging Face are skipped)…", flush=True)
    api.upload_folder(
        repo_id=repo, repo_type="dataset", folder_path=str(settings.output),
        commit_message=f"Publish {manifest['title']} ({manifest['document_count']} documents)",
        ignore_patterns=[".*", "**/.*"],
        # Remove release files that are no longer part of the dossier (e.g. deleted PDFs);
        # README.md and .gitattributes on the Hub are left alone.
        delete_patterns=["*.parquet", "*.json", "pdfs/*", "clusters/*"],
    )
    if settings.publish_to != repo:
        save_settings(settings.root, publish_to=repo)
    print(f"[done] published https://huggingface.co/datasets/{repo}", flush=True)
    print(f"[done] open it in WeHPAH! with  ?repo={repo}  or the dossier switcher", flush=True)
    return repo


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="wehpah", description="Build, check, preview and publish a WeHPAH! dossier.")
    ap.add_argument("--dir", default=".", help=f"folder holding {CONFIG_NAME} (default: current folder)")
    sub = ap.add_subparsers(dest="command")
    b = sub.add_parser("build", help="read the PDFs and build a checked release")
    b.add_argument("--limit", type=int, default=0, help="trial build with only the first N PDFs")
    b.add_argument("--force", action="store_true", help="read every PDF again, ignoring saved work")
    b.add_argument("--workers", type=int, help="PDFs read at once (overrides dossier.toml)")
    sub.add_parser("check", help="check the current release")
    for name, text in (("preview", "open the explorer on the local release"), ("studio", "open the Studio (default)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--port", type=int, default=8410)
        p.add_argument("--no-browser", action="store_true")
    sub.add_parser("login", help="sign in to Hugging Face")
    p = sub.add_parser("publish", help="upload the release to Hugging Face")
    p.add_argument("--to", default="", help="owner/name (default: publish_to in dossier.toml)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    e = sub.add_parser("eval", help="measure quality (see docs/WORKFLOW.md)")
    e.add_argument("what", choices=["names", "judge", "sample", "add-labels"],
                   help="names: precision/recall · judge: compare models · sample: passages to label · add-labels: keep them")
    e.add_argument("models", nargs="*", help="for `judge`: model specs such as mlx:<repo> lmstudio:<model> ollama:<model>")
    e.add_argument("--n", type=int, default=30, help="for `sample`: how many passages")
    args = ap.parse_args(argv)

    root = Path(args.dir).resolve()
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        settings = load_settings(root)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{root / CONFIG_NAME}: {exc}")
    command = args.command or "studio"
    import build_corpus

    try:
        if command == "build":
            settings = ensure_title(settings)
            build_corpus.build(settings, limit=args.limit, force=args.force, workers=args.workers)
            print("Next:  ./wehpah preview   then   ./wehpah publish")
        elif command == "check":
            build_corpus.verify_corpus(settings.output)
        elif command == "login":
            # Always ask: a token saved earlier may be read-only, and publishing needs a write token.
            from huggingface_hub import interpreter_login
            interpreter_login(skip_if_logged_in=False)
        elif command == "eval":
            import evaluate
            if args.what == "names":
                evaluate.eval_names(settings)
            elif args.what == "judge":
                import names
                evaluate.eval_judge(settings, args.models or [settings.names_judge or names.JUDGE_DEFAULT])
            elif args.what == "sample":
                evaluate.sample(settings, args.n)
            else:
                evaluate.add_labels(settings)
        elif command == "publish":
            publish(settings, args.to, args.yes)
        else:
            import devserver
            devserver.serve(root, args.port, "/?local" if command == "preview" else "/studio", not args.no_browser)
    except (RuntimeError, ValueError, FileNotFoundError, AssertionError) as exc:
        raise SystemExit(f"Stopped: {exc}")


if __name__ == "__main__":
    main()
