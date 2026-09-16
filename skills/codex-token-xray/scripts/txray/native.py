"""Which skills codex-token-xray may edit.

Codex native skills are never edited: the bundled `.system` skills, plugin caches,
admin skills, copies of those under user folders, and anything OpenAI holds the
copyright on. Everything else under user or project roots is editable, including
skills installed from GitHub. Those keep a provenance note so the report can say
that a reinstall will overwrite local edits and that a backup exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .frontmatter import read_skill
from .paths import is_within, local_key, path_key

NATIVE_MARKERS = ("/skills/.system/", "/plugins/cache/")
ADMIN_ROOT = "/etc/codex/skills"
OPENAI_COPYRIGHT = re.compile(r"(?im)^.*copyright[^\n]*\bOpenAI\b")


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def native_names(home: Path) -> set[str]:
    """Folder names of every native skill on this machine, from `.system` and plugin caches."""
    names = set()
    for base in (home / "skills" / ".system", home / "plugins" / "cache"):
        if not base.is_dir():
            continue
        depth = 2 if base.name == ".system" else 7
        for path in base.rglob("SKILL.md"):
            if len(path.relative_to(base).parts) <= depth:
                names.add(path.parent.name)
    return names


def lock_sources(path: Path) -> dict[str, str]:
    """Installer records that apply to this SKILL.md, keyed by lock file."""
    found = {}
    for parent in path.parents:
        for lock, roots in (
            (parent / ".skill-lock.json", [parent / "skills"]),
            (parent / "skills-lock.json", [parent / ".agents" / "skills", parent / ".codex" / "skills"]),
        ):
            if not lock.is_file():
                continue
            skills = _json(lock).get("skills", {})
            if not isinstance(skills, dict):
                continue
            for name, entry in skills.items():
                if not isinstance(entry, dict):
                    continue
                if any(is_within(str(path), str(root / name)) for root in roots):
                    source = entry.get("source") or entry.get("sourceUrl")
                    if source:
                        found[str(lock)] = str(source)
    return found


def classify(path: Path, home: Path, natives: set[str] | None = None) -> dict:
    """Return kind (native or editable), provenance, source and a caveat for the report."""
    resolved = path.resolve()
    keys = {path_key(str(path)), path_key(str(resolved))}
    if any(marker in key for key in keys for marker in NATIVE_MARKERS) or any(
        is_within(key, ADMIN_ROOT) for key in keys
    ):
        return {"kind": "native", "editable": False, "provenance": "codex-native",
                "source": None, "caveat": "Codex native skill. Never edited."}
    info = read_skill(path)
    name = info["name"]
    if natives and (name in natives or path.parent.name in natives):
        return {"kind": "native", "editable": False, "provenance": "native-copy",
                "source": None, "caveat": f"Copy of the Codex native skill '{name}'. Never edited."}
    for candidate in (path, *(path.parent / n for n in ("LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt"))):
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")[:16000]
        except OSError:
            continue
        if OPENAI_COPYRIGHT.search(text):
            return {"kind": "native", "editable": False, "provenance": "openai-copyright",
                    "source": None, "caveat": f"OpenAI copyright notice in {candidate.name}. Never edited."}
    locks = lock_sources(path)
    if locks:
        lock, source = next(iter(locks.items()))
        return {"kind": "editable", "editable": True, "provenance": "upstream", "source": source,
                "caveat": f"Installed from {source}. Reinstalling overwrites local edits; the backup keeps the original."}
    for parent in resolved.parents:
        if (parent / ".git").exists():
            if not is_within(str(path), str(parent)):
                return {"kind": "editable", "editable": True, "provenance": "linked-checkout",
                        "source": str(parent), "caveat": f"Linked from the checkout {parent}. Edits land in that checkout."}
            break
    return {"kind": "editable", "editable": True, "provenance": "local", "source": None, "caveat": None}


def is_native_path(path: str, home: Path, natives: set[str] | None = None) -> bool:
    p = Path(path)
    if not p.is_file():
        key = path_key(local_key(path))
        return any(marker in key for marker in NATIVE_MARKERS) or is_within(key, ADMIN_ROOT)
    return classify(p, home, natives)["kind"] == "native"
