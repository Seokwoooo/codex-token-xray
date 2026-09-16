#!/usr/bin/env python3
"""Apply reviewed changes with a backup. No model calls, no network.

  apply.py --plan plan.json          # check everything, show diffs
  apply.py --plan plan.json --apply  # back up, write, verify, seal

Plan:
  {"edits":  [{"path": ".../SKILL.md",  "sha256": "...", "description": "...", "reason": "..."},
              {"path": ".../SKILL.md",  "sha256": "...", "body": "...",        "reason": "..."},
              {"path": ".../AGENTS.md", "sha256": "...", "content": "...",     "reason": "..."}],
   "create": [{"path": ".../references/topic.md", "content": "..."}],
   "remove": ["/abs/path/to/unused-skill/SKILL.md"]}

Only skills you wrote can be edited. Skills made by other people and Codex's own
skills and plugins are refused. `remove` deletes a whole skill folder you no
longer use; the backup keeps every file and restore.py puts it back.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txray import archive, config  # noqa: E402
from txray.budget import approx_tokens  # noqa: E402
from txray.constants import MAX_CATALOG_DESCRIPTION_CHARS  # noqa: E402
from txray.frontmatter import read_skill, replace_body, replace_description  # noqa: E402
from txray.native import classify, is_native_path, native_names  # noqa: E402
from txray.paths import is_within, local_key  # noqa: E402

AGENTS_NAMES = ("AGENTS.md", "AGENTS.override.md")


def prepare(plan: dict, home: Path) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    edits, creates, removes = plan.get("edits") or [], plan.get("create") or [], plan.get("remove") or []
    for key, value in (("edits", edits), ("create", creates), ("remove", removes)):
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
    if not (edits or creates or removes):
        raise ValueError("plan has nothing to do")
    natives = native_names(home)
    items, seen, skill_dirs = [], set(), []

    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("each edit must be an object")
        path = Path(str(edit.get("path", ""))).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"not an existing absolute file: {path}")
        key = local_key(str(path))
        if key in seen:
            raise ValueError(f"duplicate edit for {path}")
        seen.add(key)
        if not isinstance(edit.get("reason"), str) or not edit["reason"].strip():
            raise ValueError(f"missing review reason: {path}")
        before = path.read_bytes()
        if edit.get("sha256") != archive.sha256_bytes(before):
            raise ValueError(f"file changed since review: {path}")
        fields = [k for k in ("description", "body", "content") if k in edit]
        if len(fields) != 1:
            raise ValueError(f"exactly one of description, body or content per edit: {path}")
        field, value = fields[0], edit[fields[0]]
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string: {path}")
        if path.name == "SKILL.md":
            protection = classify(path, home, natives)
            if protection["provenance"] != "local":
                raise ValueError(f"{path}: not yours to edit. {protection['caveat']}")
            info = read_skill(path)
            if info["error"]:
                raise ValueError(f"{path}: {info['error']}")
            if field == "description":
                if len(value) > MAX_CATALOG_DESCRIPTION_CHARS:
                    raise ValueError(f"description exceeds the {MAX_CATALOG_DESCRIPTION_CHARS}-character catalog cap: {path}")
                if info["description"] == value:
                    raise ValueError(f"description is unchanged: {path}")
                after = replace_description(before, value)
                saved = approx_tokens(info["description"]) - approx_tokens(value)
            elif field == "body":
                after = replace_body(before, value)
                saved = approx_tokens(before.decode("utf-8-sig")) - approx_tokens(after.decode("utf-8-sig"))
            else:
                raise ValueError(f"use description or body for SKILL.md: {path}")
            skill_dirs.append(path.parent)
        elif path.name in AGENTS_NAMES:
            if field != "content":
                raise ValueError(f"use content for {path.name}: {path}")
            if is_native_path(str(path), home, natives):
                raise ValueError(f"refusing to edit a native path: {path}")
            after = value.encode("utf-8")
            if after == before:
                raise ValueError(f"content is unchanged: {path}")
            saved = approx_tokens(before.decode("utf-8", errors="replace")) - approx_tokens(value)
        else:
            raise ValueError(f"only SKILL.md and AGENTS.md files can be edited: {path}")
        items.append({"kind": "edit", "path": path, "before": before, "after": after,
                      "reason": edit["reason"], "tokens_saved": saved})

    for item in creates:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ValueError("each create needs path and content")
        path = Path(str(item.get("path", ""))).expanduser()
        if not path.is_absolute():
            raise ValueError(f"create path must be absolute: {path}")
        if path.exists() or path.is_symlink():
            raise ValueError(f"create target already exists: {path}")
        if not any(is_within(str(path), str(d)) for d in skill_dirs):
            raise ValueError(f"create target must sit inside a skill folder edited in the same plan: {path}")
        items.append({"kind": "create", "path": path, "before": None, "after": item["content"].encode("utf-8"),
                      "reason": "reference created by plan", "tokens_saved": 0})

    remove_dirs = []
    for raw in removes:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute() or path.name != "SKILL.md" or not path.is_file():
            raise ValueError(f"remove entries must be existing absolute SKILL.md paths: {path}")
        protection = classify(path, home, natives)
        if protection["kind"] == "native":
            raise ValueError(f"{path}: Codex native skill. Not removed by this tool.")
        folder = path.parent
        if folder in remove_dirs:
            continue
        remove_dirs.append(folder)
        for file in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            items.append({"kind": "remove", "path": file, "before": file.read_bytes(), "after": None,
                          "reason": f"remove unused skill {folder.name}", "tokens_saved": 0})
    lock = Path.home() / ".agents" / ".skill-lock.json"
    lock_after = None
    if remove_dirs and lock.is_file():
        try:
            data = json.loads(lock.read_text(encoding="utf-8-sig"))
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("skills"), dict):
            names = {d.name for d in remove_dirs if is_within(str(d), str(lock.parent / "skills"))}
            kept = {k: v for k, v in data["skills"].items() if k not in names}
            if len(kept) != len(data["skills"]):
                data["skills"] = kept
                lock_after = (json.dumps(data, indent=2) + "\n").encode("utf-8")
                items.append({"kind": "edit", "path": lock, "before": lock.read_bytes(), "after": lock_after,
                              "reason": "drop installer records of removed skills", "tokens_saved": 0})
    return {"items": items, "remove_dirs": remove_dirs, "tokens_saved": sum(i["tokens_saved"] for i in items)}


def write_bytes(path: Path, data: bytes) -> None:
    target = path.resolve() if path.exists() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    fd, temp = tempfile.mkstemp(prefix=".codex-token-xray-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--codex-home")
    args = parser.parse_args(argv)
    home = Path(args.codex_home).expanduser() if args.codex_home else config.codex_home()
    backup, written = None, []
    try:
        plan = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8-sig"))
        prepared = prepare(plan, home)
        items = prepared["items"]
        result = {"ok": True, "applied": False, "tokens_saved_estimate": prepared["tokens_saved"],
                  "removed_skills": [str(d) for d in prepared["remove_dirs"]], "files": []}
        for item in items:
            if item["kind"] == "remove":
                continue
            before_text = item["before"].decode("utf-8-sig", errors="replace").splitlines(True) if item["before"] else []
            after_text = item["after"].decode("utf-8-sig", errors="replace").splitlines(True)
            result["files"].append({"path": str(item["path"]), "kind": item["kind"], "reason": item["reason"],
                                    "tokens_saved_estimate": item["tokens_saved"],
                                    "diff": "".join(difflib.unified_diff(before_text, after_text, fromfile=str(item["path"]), tofile=str(item["path"])))})
        if args.apply:
            changes = [(str(i["path"]), {"edit": "modify", "create": "create", "remove": "delete"}[i["kind"]]) for i in items]
            backup = archive.create(changes, label="token-xray")
            result["backup"] = backup
            for item in items:
                current = item["path"].read_bytes() if item["path"].exists() else None
                if current != item["before"]:
                    raise ValueError(f"file changed during backup: {item['path']}")
            for item in items:
                if item["kind"] == "remove":
                    item["path"].unlink()
                else:
                    write_bytes(item["path"], item["after"])
                written.append(item)
                if item["kind"] != "remove" and item["path"].read_bytes() != item["after"]:
                    raise RuntimeError(f"write verification failed: {item['path']}")
            for folder in prepared["remove_dirs"]:
                shutil.rmtree(folder, ignore_errors=True)
            result["seal"] = archive.seal(Path(backup["backup"]))
            result["applied"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        unresolved = []
        for item in reversed(written):
            try:
                if item["kind"] == "remove":
                    if not item["path"].exists():
                        write_bytes(item["path"], item["before"])
                elif item["path"].read_bytes() == item["after"]:
                    if item["before"] is None:
                        item["path"].unlink()
                    else:
                        write_bytes(item["path"], item["before"])
                else:
                    unresolved.append(str(item["path"]))
            except OSError:
                unresolved.append(str(item["path"]))
        result = {"ok": False, "applied": False, "error": str(exc), "unresolved": unresolved}
        if backup:
            result["backup"] = backup
            try:
                result["seal"] = archive.seal(Path(backup["backup"]))
            except OSError as seal_error:
                result["seal_error"] = str(seal_error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
