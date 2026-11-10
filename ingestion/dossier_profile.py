"""dossier_profile.py — load a dossier profile (ingestion/profiles/*.toml).

A profile holds what is specific to a dossier's language, jurisdiction and domain; the
pipeline steps in entities.py and names.py are general. See docs/PROFILES.md.

    load_profile("us-california")            # a profile shipped in ingestion/profiles/
    load_profile("my-profile.toml", root)    # a file next to dossier.toml

`extends = "<name>"` starts from another profile. Tables merge key by key; any other value a
profile sets replaces the inherited one (lists are not merged).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
DEFAULT_PROFILE = "en"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _path(name: str, root: Path | None) -> Path:
    if name.endswith(".toml"):
        p = Path(name).expanduser()
        return p if p.is_absolute() else (root or Path.cwd()) / p
    return PROFILE_DIR / f"{name}.toml"


def load_profile(name: str = DEFAULT_PROFILE, root: Path | None = None, _seen: tuple = ()) -> dict:
    path = _path(name, root)
    if not path.is_file():
        shipped = ", ".join(sorted(p.stem for p in PROFILE_DIR.glob("*.toml")))
        raise ValueError(f"Profile not found: {name} (shipped profiles: {shipped}; or give a path to a .toml file)")
    if path in _seen:
        raise ValueError(f"Profiles extend each other in a loop: {' -> '.join(map(str, _seen + (path,)))}")
    data = tomllib.loads(path.read_text())
    parent = data.pop("extends", None)
    merged = _merge(load_profile(parent, path.parent, _seen + (path,)), data) if parent else data
    merged["id"] = path.stem
    return merged


def fingerprint(profile: dict) -> str:
    """Changes whenever any rule in the profile changes (used in cache keys)."""
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]


if __name__ == "__main__":
    p = load_profile("us-california")
    assert p["dates"]["numeric_order"] == "mdy" and p["laws"]["eyecite_cases"]
    assert p["names"]["connectors"] and p["names"]["structure"]["sign_offs"]  # inherited from en
    assert "Hearing Officer" in p["names"]["titles"]
    assert load_profile("en")["dates"]["numeric_order"] == "none"
    print("profile self-check OK")
