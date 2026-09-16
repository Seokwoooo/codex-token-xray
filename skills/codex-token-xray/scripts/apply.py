#!/usr/bin/env python3
"""Preview or apply reviewed edits to skills and AGENTS.md. No model calls, no network.

  apply.py --plan plan.json          # validate every edit, print diffs and token estimates
  apply.py --plan plan.json --apply  # verified backup, edit, verify bytes, seal

Plan:
  {"edits": [
     {"path": ".../SKILL.md",  "sha256": "...", "description": "...", "reason": "..."},
     {"path": ".../SKILL.md",  "sha256": "...", "body": "...",        "reason": "..."},
     {"path": ".../AGENTS.md", "sha256": "...", "content": "...",     "reason": "..."}],
   "create": [{"path": ".../references/topic.md", "content": "..."}]}

Codex native skills are refused. The agent writes the replacement text after
reading the file; this helper never invents wording or changes configuration.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
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
    edits, creates = plan.get("edits") or [], plan.get("create") or []
    if not isinstance(edits, list) or not isinstance(creates, list) or not (edits or creates):
        raise ValueError("plan needs a non-empty edits or create list")
    natives = native_names(home)
    prepared, seen, skill_dirs = [], set(), []
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
        field = fields[0]
        value = edit[field]
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string: {path}")
        if path.name == "SKILL.md":
            protection = classify(path, home, natives)
            if not protection["editable"]:
                raise ValueError(f"{path}: {protection['caveat']}")
            info = read_skill(path)
            if info["error"]:
                raise ValueError(f"{path}: {info['error']}")
            if field == "description":
                if len(value) > MAX_CATALOG_DESCRIPTION_CHARS:
                    raise ValueError(f"description exceeds the {MAX_CATALOG_DESCRIPTION_CHARS}-character catalog cap: {path}")
                if info["description"] == value:
                    raise ValueError(f"description is unchanged: {path}; record a keep decision instead")
                after = replace_description(before, value)
                saved = approx_tokens(info["description"]) - approx_tokens(value)
            elif field == "body":
                after = replace_body(before, value)
                old_body = before.decode("utf-8-sig")
                saved = approx_tokens(old_body) - approx_tokens(after.decode("utf-8-sig"))
            else:
                raise ValueError(f"use description or body for SKILL.md: {path}")
            skill_dirs.append(path.parent)
            caveat = protection["caveat"]
        elif path.name in AGENTS_NAMES:
            if field != "content":
                raise ValueError(f"use content for {path.name}: {path}")
            if is_native_path(str(path), home, natives):
                raise ValueError(f"refusing to edit a native path: {path}")
            after = value.encode("utf-8")
            if after == before:
                raise ValueError(f"content is unchanged: {path}")
            saved = approx_tokens(before.decode("utf-8", errors="replace")) - approx_tokens(value)
            caveat = None
        else:
            raise ValueError(f"only SKILL.md and AGENTS.md files can be edited: {path}")
        prepared.append({"kind": "edit", "field": field, "path": path, "before": before, "after": after,
                         "reason": edit["reason"], "tokens_saved": saved, "caveat": caveat})
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
        if is_native_path(str(path), home, natives):
            raise ValueError(f"refusing to create inside a native path: {path}")
        prepared.append({"kind": "create", "field": "content", "path": path, "before": None,
                         "after": item["content"].encode("utf-8"), "reason": "reference created by plan",
                         "tokens_saved": 0, "caveat": None})
    return {"items": prepared, "tokens_saved": sum(i["tokens_saved"] for i in prepared)}


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
        result = {"ok": True, "applied": False, "tokens_saved_estimate": prepared["tokens_saved"], "files": []}
        for item in prepared["items"]:
            before_text = item["before"].decode("utf-8-sig", errors="replace").splitlines(True) if item["before"] else []
            after_text = item["after"].decode("utf-8-sig", errors="replace").splitlines(True)
            result["files"].append({
                "path": str(item["path"]), "kind": item["kind"], "field": item["field"], "reason": item["reason"],
                "tokens_saved_estimate": item["tokens_saved"], "caveat": item["caveat"],
                "diff": "".join(difflib.unified_diff(before_text, after_text, fromfile=str(item["path"]), tofile=str(item["path"]))),
            })
        if args.apply:
            changes = [(str(i["path"]), "modify" if i["kind"] == "edit" else "create") for i in prepared["items"]]
            backup = archive.create(changes, label="token-xray")
            result["backup"] = backup
            for item in prepared["items"]:
                current = item["path"].read_bytes() if item["path"].exists() else None
                if current != item["before"]:
                    raise ValueError(f"file changed during backup: {item['path']}")
            for item in prepared["items"]:
                current = item["path"].read_bytes() if item["path"].exists() else None
                if current != item["before"]:
                    raise ValueError(f"file changed during apply: {item['path']}")
                write_bytes(item["path"], item["after"])
                written.append(item)
                if item["path"].read_bytes() != item["after"]:
                    raise RuntimeError(f"write verification failed: {item['path']}")
            result["seal"] = archive.seal(Path(backup["backup"]))
            result["applied"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        unresolved = []
        for item in reversed(written):
            try:
                if item["path"].read_bytes() == item["after"]:
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
