"""Editable skill inventory: user and project roots only.

Plugin caches and `.system` are read for their names, never counted as inventory.
The catalog Codex actually loaded comes from the session log, not from this scan.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import constants as C
from .agents_md import find_project_root
from .budget import approx_tokens
from .config import get
from .frontmatter import read_skill
from .native import classify, native_names
from .paths import local_key


def editable_roots(cwd: Path, home: Path, cfg: dict) -> list[dict]:
    markers = get(cfg, "project_root_markers", C.DEFAULT_PROJECT_ROOT_MARKERS)
    root = find_project_root(cwd, markers if isinstance(markers, list) else C.DEFAULT_PROJECT_ROOT_MARKERS)
    between = [cwd]
    if root is not None:
        cursor = cwd
        while cursor != root and cursor.parent != cursor:
            cursor = cursor.parent
            between.append(cursor)
    roots = [{"path": str(d / ".codex" / "skills"), "kind": "repo-codex"} for d in between]
    roots.append({"path": str(home / "skills"), "kind": "user-deprecated"})
    roots.append({"path": str(Path.home() / ".agents" / "skills"), "kind": "user"})
    roots += [{"path": str(d / ".agents" / "skills"), "kind": "repo"} for d in reversed(between)]
    seen, unique = set(), []
    for r in roots:
        if r["path"] in seen or not Path(r["path"]).is_dir():
            continue
        seen.add(r["path"])
        unique.append(r)
    return unique


def discover(root: Path) -> list[Path]:
    found, visited, stack = [], set(), [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            real = directory.resolve()
        except OSError:
            continue
        if real in visited or len(visited) > C.MAX_SKILL_DIRS_PER_ROOT:
            continue
        visited.add(real)
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name == "SKILL.md" and entry.is_file():
                found.append(Path(entry.path))
            elif entry.is_dir(follow_symlinks=True) and not entry.name.startswith(".") and depth < C.MAX_SCAN_DEPTH:
                stack.append((Path(entry.path), depth + 1))
    return sorted(found)


def inventory(cwd: Path, home: Path, cfg: dict) -> dict:
    natives = native_names(home)
    roots = editable_roots(cwd, home, cfg)
    skills, seen = [], set()
    for root in roots:
        for path in discover(Path(root["path"])):
            key = local_key(str(path))
            if key in seen:
                continue
            seen.add(key)
            info = read_skill(path)
            body = info.pop("body", "")
            info.update(
                root=root["path"], root_kind=root["kind"],
                description_chars=len(info["description"]),
                description_tokens=approx_tokens(info["description"]),
                body_bytes=len(body.encode("utf-8")), body_tokens=approx_tokens(body),
                body_lines=body.count("\n") + 1 if body else 0,
                protection=classify(path, home, natives),
            )
            skills.append(info)
    return {"roots": roots, "native_names": sorted(natives), "skills": skills}
