#!/usr/bin/env python3
"""Preview or apply reviewed changes to skills and AGENTS.md. No model calls, no network.

  apply.py --plan plan.json          # validate everything, print diffs and token estimates
  apply.py --plan plan.json --apply  # verified backup, write, verify bytes, seal

Plan:
  {"edits": [
     {"path": ".../SKILL.md",  "sha256": "...", "description": "...", "reason": "..."},
     {"path": ".../SKILL.md",  "sha256": "...", "body": "...",        "reason": "..."},
     {"path": ".../AGENTS.md", "sha256": "...", "content": "...",     "reason": "..."}],
   "create":  [{"path": ".../references/topic.md", "content": "..."}],
   "fork":    [{"path": ".../SKILL.md", "name": "new-name", "into": "~/.agents/skills",
                "description": "...", "body": "...", "reason": "..."}],
   "disable": ["/abs/path/to/SKILL.md"],
   "allow_upstream": false}

Codex native skills are refused everywhere. Skills maintained elsewhere (installed
from GitHub or copied from openai/skills) are refused for in-place edits unless the
plan sets allow_upstream; fork them under a new name or disable them instead.
The agent writes the replacement text after reading the file; this helper never
invents wording.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txray import archive, config  # noqa: E402
from txray.budget import approx_tokens  # noqa: E402
from txray.constants import MAX_CATALOG_DESCRIPTION_CHARS, MAX_SKILL_NAME_CHARS  # noqa: E402
from txray.frontmatter import read_skill, replace_body, replace_description  # noqa: E402
from txray.native import classify, is_native_path, native_names  # noqa: E402
from txray.paths import is_within, local_key  # noqa: E402

AGENTS_NAMES = ("AGENTS.md", "AGENTS.override.md")
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _skill_protection(path: Path, home: Path, natives: set, allow_upstream: bool, verb: str) -> dict:
    protection = classify(path, home, natives)
    if protection["kind"] == "native":
        raise ValueError(f"{path}: {protection['caveat']}")
    if protection["provenance"] in ("upstream", "linked-checkout") and not allow_upstream:
        raise ValueError(f"{path}: maintained elsewhere ({protection['source'] or protection['provenance']}). "
                         f"Refusing to {verb} it in place. Fork it under a new name, disable it, or set allow_upstream in the plan.")
    return protection


def _rename_frontmatter(data: bytes, name: str) -> bytes:
    text = data.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        raise ValueError("missing YAML frontmatter")
    hits = [i for i in range(1, end) if re.match(r"^name\s*:", lines[i])]
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    if hits:
        lines[hits[0]] = f"name: {name}{newline}"
    else:
        lines.insert(1, f"name: {name}{newline}")
    prefix = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return prefix + "".join(lines).encode("utf-8")


def prepare(plan: dict, home: Path) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    edits, creates = plan.get("edits") or [], plan.get("create") or []
    forks, disables = plan.get("fork") or [], plan.get("disable") or []
    allow_upstream = bool(plan.get("allow_upstream", False))
    for key, value in (("edits", edits), ("create", creates), ("fork", forks), ("disable", disables)):
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
    if not (edits or creates or forks or disables):
        raise ValueError("plan has nothing to do")
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
        field, value = fields[0], edit[fields[0]]
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string: {path}")
        if path.name == "SKILL.md":
            protection = _skill_protection(path, home, natives, allow_upstream, "edit")
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
                saved = approx_tokens(before.decode("utf-8-sig")) - approx_tokens(after.decode("utf-8-sig"))
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

    for fork in forks:
        if not isinstance(fork, dict):
            raise ValueError("each fork must be an object")
        source = Path(str(fork.get("path", ""))).expanduser()
        name = str(fork.get("name", ""))
        if not source.is_absolute() or source.name != "SKILL.md" or not source.is_file():
            raise ValueError(f"fork source must be an existing absolute SKILL.md: {source}")
        if not SKILL_NAME.match(name) or len(name) > MAX_SKILL_NAME_CHARS:
            raise ValueError(f"fork name must be hyphen-case and at most {MAX_SKILL_NAME_CHARS} characters: {name!r}")
        protection = classify(source, home, natives)
        if protection["kind"] == "native":
            raise ValueError(f"{source}: {protection['caveat']} Copy it by hand if you really want a fork.")
        info = read_skill(source)
        if info["error"]:
            raise ValueError(f"{source}: {info['error']}")
        if info["name"] == name:
            raise ValueError(f"fork needs a new name; {name!r} is the source skill's name")
        into = Path(str(fork.get("into") or (Path.home() / ".agents" / "skills"))).expanduser()
        target_dir = into / name
        if target_dir.exists() or target_dir.is_symlink():
            raise ValueError(f"fork target already exists: {target_dir}")
        if is_native_path(str(target_dir / "SKILL.md"), home, natives):
            raise ValueError(f"refusing to fork into a native path: {target_dir}")
        if not isinstance(fork.get("reason"), str) or not fork["reason"].strip():
            raise ValueError(f"missing fork reason: {source}")
        files = sorted(p for p in source.parent.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        total = sum(p.stat().st_size for p in files)
        if total > 50_000_000:
            raise ValueError(f"fork source is larger than 50 MB; copy it by hand: {source.parent}")
        for src in files:
            rel = src.relative_to(source.parent)
            data = src.read_bytes()
            if rel == Path("SKILL.md"):
                data = _rename_frontmatter(data, name)
                if "description" in fork:
                    if not isinstance(fork["description"], str) or len(fork["description"]) > MAX_CATALOG_DESCRIPTION_CHARS:
                        raise ValueError(f"fork description must be a string within the catalog cap: {source}")
                    data = replace_description(data, fork["description"])
                if "body" in fork:
                    if not isinstance(fork["body"], str):
                        raise ValueError(f"fork body must be a string: {source}")
                    data = replace_body(data, fork["body"])
            prepared.append({"kind": "fork", "field": "copy", "path": target_dir / rel, "before": None, "after": data,
                             "reason": fork["reason"], "tokens_saved": 0, "caveat": None,
                             "source": str(src), "mode": src.stat().st_mode & 0o777})
        prepared.append({"kind": "note", "path": target_dir / "SKILL.md", "note": (
            f"Forked from {source}. Disable the original when the fork is confirmed: add it to the plan's disable list.")})

    if disables:
        config_path = home / "config.toml"
        cfg = config.load_config(home)
        existing = {local_key(rule["path"]) for rule in (config.get(cfg, "skills.config", []) or [])
                    if isinstance(rule, dict) and rule.get("path")}
        before = config_path.read_bytes() if config_path.is_file() else b""
        blocks = []
        for raw in disables:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute() or path.name != "SKILL.md" or not path.is_file():
                raise ValueError(f"disable entries must be existing absolute SKILL.md paths: {path}")
            if classify(path, home, natives)["kind"] == "native":
                raise ValueError(f"{path}: Codex native skill. Not disabled by this tool.")
            if local_key(str(path)) in existing:
                raise ValueError(f"already listed in skills.config: {path}")
            blocks.append(f'\n[[skills.config]]\npath = {json.dumps(str(path))}\nenabled = false\n')
        after = before + ("" if before.endswith(b"\n") or not before else "\n").encode() + "".join(blocks).encode("utf-8")
        prepared.append({"kind": "edit", "field": "content", "path": config_path, "before": before if config_path.is_file() else None,
                         "after": after, "reason": f"disable {len(blocks)} skills", "tokens_saved": 0, "caveat": None})

    return {"items": prepared, "tokens_saved": sum(i.get("tokens_saved", 0) for i in prepared),
            "allow_upstream": allow_upstream}


def write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    target = path.resolve() if path.exists() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
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
        items = [i for i in prepared["items"] if i["kind"] != "note"]
        result = {"ok": True, "applied": False, "tokens_saved_estimate": prepared["tokens_saved"],
                  "allow_upstream": prepared["allow_upstream"], "files": [],
                  "notes": [i["note"] for i in prepared["items"] if i["kind"] == "note"]}
        for item in items:
            before_text = item["before"].decode("utf-8-sig", errors="replace").splitlines(True) if item["before"] else []
            after_text = item["after"].decode("utf-8-sig", errors="replace").splitlines(True)
            diff = "" if item["kind"] == "fork" else "".join(difflib.unified_diff(
                before_text, after_text, fromfile=str(item["path"]), tofile=str(item["path"])))
            result["files"].append({"path": str(item["path"]), "kind": item["kind"], "field": item["field"],
                                    "reason": item["reason"], "tokens_saved_estimate": item["tokens_saved"],
                                    "caveat": item["caveat"], "source": item.get("source"), "diff": diff})
        if args.apply:
            changes = [(str(i["path"]), "modify" if i["kind"] == "edit" and i["before"] is not None else "create") for i in items]
            backup = archive.create(changes, label="token-xray")
            result["backup"] = backup
            for item in items:
                current = item["path"].read_bytes() if item["path"].exists() else None
                if current != item["before"]:
                    raise ValueError(f"file changed during backup: {item['path']}")
            for item in items:
                current = item["path"].read_bytes() if item["path"].exists() else None
                if current != item["before"]:
                    raise ValueError(f"file changed during apply: {item['path']}")
                write_bytes(item["path"], item["after"], item.get("mode"))
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
