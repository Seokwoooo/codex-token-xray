"""Parse one Codex rollout log into what the model was sent and what it cost.

Measured numbers come from `token_usage_record` entries (one per model response):
input, cached input, output and reasoning tokens. Everything else is attributed by
size: bytes / 4, the same approximation Codex uses for its skills budget. The two
are kept apart in the output and compared in `calibration`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .budget import approx_tokens

TAG = re.compile(r"^\s*<([a-z_-]+)>")
SKILL_TAG = re.compile(r"^\s*<skill>\s*<name>([^<\n]+)</name>\s*<path>([^<\n]+)</path>", re.S)
AGENTS_HEAD = re.compile(r"^# AGENTS\.md instructions for (.+)")
TRUNCATED = re.compile(r"Warning: truncated output \(original token count: (\d+)\)")
SKILL_PATH = re.compile(r"(?<![\w./\\-])((?:[A-Za-z]:[\\/]|/|~/)[^\s\"'`<>|;)&]*?SKILL\.md)")
FILE_PATH = re.compile(
    r"(?<![\w./\\-])((?:[A-Za-z]:[\\/]|/|~/)[^\s\"'`<>|;)&]+?\.(?:md|py|ts|tsx|js|jsx|mjs|json|toml|yaml|yml|txt|rs|go|swift|kt|java|cs|html|css|scss|sh|ps1|sql|csv|log|xml))\b"
)
PATCH_MARKER = "*** Begin Patch"
CMD_HEAD = re.compile(r"cmd\s*:\s*\"((?:[^\"\\]|\\.){0,160})")
LOCATOR_KINDS = ("file", "executor package", "orchestrator package", "custom resource")
ROOT_LINE = re.compile(r"^- `(r\d+)` = `(.*)`$")
OMISSION_LINE = re.compile(r"^- (\d+) additional skills? omitted")
SKIP_FAST = ('"encrypted_content"',)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for piece in content:
            if isinstance(piece, dict):
                parts.append(str(piece.get("text") or piece.get("input_text") or piece.get("output_text") or ""))
            elif isinstance(piece, str):
                parts.append(piece)
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return "" if content is None else str(content)


def categorize_message(role: str, text: str) -> tuple[str, str | None]:
    head = text[:400]
    if role == "developer":
        tag = TAG.match(head)
        name = tag.group(1) if tag else None
        if name == "skills_instructions":
            return "skills_catalog", None
        if name == "model_switch":
            return "model_switch", None
        return "developer_instructions", name
    if role == "user":
        agents = AGENTS_HEAD.match(head)
        if agents:
            return "agents_md", agents.group(1).strip()
        skill = SKILL_TAG.match(text[:2000])
        if skill:
            return "skill_body", skill.group(1).strip()
        tag = TAG.match(head)
        if tag:
            if tag.group(1) == "environment_context":
                return "environment", None
            return "system_notices", tag.group(1)
        return "user_message", None
    if role == "assistant":
        return "assistant_message", None
    return "other_message", role


def command_head(tool: str, raw: str) -> str:
    match = CMD_HEAD.search(raw)
    if match:
        return match.group(1).replace("\\n", " ")[:160]
    if raw.startswith("{"):
        try:
            args = json.loads(raw)
            for key in ("command", "cmd", "code", "title", "path", "file_path"):
                if key in args:
                    value = args[key]
                    return (" ".join(value) if isinstance(value, list) else str(value))[:160]
        except ValueError:
            pass
    return raw.replace("\n", " ")[:160]


def _existing(raw_path: str) -> str | None:
    """Normalize a path mentioned in a tool call; keep it only if it exists on this machine."""
    candidate = Path(raw_path.replace("\\", "/")).expanduser()
    try:
        if candidate.is_file():
            return str(candidate.resolve())
    except OSError:
        pass
    return None


_FILE_TOKENS: dict[str, int] = {}


def _file_tokens(path: str) -> int:
    """Estimated tokens of a file on disk, cached; caps what one read can be charged."""
    if path not in _FILE_TOKENS:
        try:
            _FILE_TOKENS[path] = (len(Path(path).read_bytes()) + 3) // 4
        except OSError:
            _FILE_TOKENS[path] = 1 << 30
    return _FILE_TOKENS[path]


def _item(category, text_bytes, **extra) -> dict:
    item = {"category": category, "bytes": text_bytes, "tokens": (text_bytes + 3) // 4}
    item.update(extra)
    return item


def parse_skills_body(body: str) -> dict:
    roots, entries, omitted, section = [], [], 0, None
    for line in body.splitlines():
        if line.startswith("### Skill roots"):
            section = "roots"
            continue
        if line.startswith("### Available skills"):
            section = "skills"
            continue
        if line.startswith("#"):
            section = None
            continue
        if section == "roots":
            match = ROOT_LINE.match(line)
            if match:
                roots.append((match.group(1), match.group(2)))
        elif section == "skills" and line.startswith("- "):
            omission = OMISSION_LINE.match(line)
            if omission:
                omitted = int(omission.group(1))
                continue
            entry = parse_entry(line)
            if entry:
                entries.append(entry)
    root_map = dict(roots)
    for entry in entries:
        match = re.match(r"^(r\d+)/(.*)$", entry["locator"])
        if match and match.group(1) in root_map:
            entry["path"] = root_map[match.group(1)].rstrip("/") + "/" + match.group(2)
            entry["alias"] = match.group(1)
        else:
            entry["path"] = entry["locator"]
            entry["alias"] = None
    return {"roots": roots, "entries": entries, "omission_marker": omitted}


def parse_entry(line: str) -> dict | None:
    head = re.match(r"^- (\S+): ", line)
    if not head:
        return None
    rest = line[head.end():]
    best = (-1, None)
    for kind in LOCATOR_KINDS:
        index = rest.rfind(f"({kind}: ")
        if index > best[0]:
            best = (index, kind)
    index, kind = best
    if index < 0 or not rest.endswith(")"):
        return None
    description = rest[:index]
    if description.endswith(" "):
        description = description[:-1]
    return {"name": head.group(1), "description": description, "kind": kind,
            "locator": rest[index + len(kind) + 3:-1], "line": line}


class _Window:
    def __init__(self, number: int):
        self.number = number
        self.items: list[dict] = []
        self.usage: list[dict] = []
        self.items_tokens = 0
        self.reasoning_tokens = 0
        self.carried_over_tokens = 0

    def add(self, item: dict) -> None:
        item["index"] = len(self.items)
        self.items.append(item)
        self.items_tokens += item["tokens"]

    def record(self, usage: dict, response_id, turn_id) -> None:
        entry = {
            "input": int(usage.get("input_tokens") or 0),
            "cached": int(usage.get("cached_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "reasoning": int(usage.get("reasoning_output_tokens") or 0),
            "response_id": response_id, "turn_id": turn_id,
            "estimated_prompt": self.items_tokens + self.reasoning_tokens,
        }
        entry["net_new"] = max(0, entry["input"] - entry["cached"])
        self.usage.append(entry)
        self.reasoning_tokens += entry["reasoning"]

    def summary(self) -> dict:
        categories: dict[str, dict] = {}
        for item in self.items:
            slot = categories.setdefault(item["category"], {"tokens": 0, "bytes": 0, "count": 0, "max_item": 0})
            slot["max_item"] = max(slot["max_item"], item["tokens"])
            if item["category"] in ("developer_instructions", "system_notices") and item.get("detail"):
                tags = slot.setdefault("by_tag", {})
                tags[item["detail"]] = tags.get(item["detail"], 0) + item["tokens"]
            slot["tokens"] += item["tokens"]
            slot["bytes"] += item["bytes"]
            slot["count"] += 1
        if self.reasoning_tokens:
            categories["reasoning"] = {"tokens": self.reasoning_tokens, "bytes": None, "count": len(self.usage), "measured": True}
        inputs = [u["input"] for u in self.usage]
        last = self.usage[-1] if self.usage else None
        first = self.usage[0] if self.usage else None
        growth = None
        if first and last and last is not first and last["input"] > first["input"]:
            growth = round((last["estimated_prompt"] - first["estimated_prompt"]) / (last["input"] - first["input"]), 3)
        return {
            "estimated_prompt_at_first_call": first["estimated_prompt"] if first else None,
            "growth_calibration": growth,
            "number": self.number,
            "calls": len(self.usage),
            "first_input": inputs[0] if inputs else None,
            "peak_input": max(inputs) if inputs else None,
            "last_input": inputs[-1] if inputs else None,
            "input_total": sum(inputs),
            "cached_total": sum(u["cached"] for u in self.usage),
            "net_new_total": sum(u["net_new"] for u in self.usage),
            "output_total": sum(u["output"] for u in self.usage),
            "reasoning_total": self.reasoning_tokens,
            "estimated_prompt_at_last_call": last["estimated_prompt"] if last else None,
            "calibration": (round(last["estimated_prompt"] / last["input"], 3) if last and last["input"] else None),
            "carried_over_tokens": self.carried_over_tokens,
            "categories": categories,
            "items": len(self.items),
        }


def parse(path: Path, keep_items: bool = False) -> dict | None:
    meta = {"path": str(path), "session_id": None, "cwd": None, "cli_version": None, "originator": None,
            "started": None, "forked_from": None, "models": []}
    windows: list[_Window] = [_Window(1)]
    catalog_body = None
    base_instructions_tokens = 0
    calls: dict[str, dict] = {}
    tool_outputs: list[dict] = []
    file_reads: dict[str, int] = {}
    invocations: dict[str, dict] = {}
    truncated = {"count": 0, "original_tokens": 0}
    token_count_fallback: list[dict] = []
    saw_usage_record = False
    reasoning_items = 0

    def current() -> _Window:
        return windows[-1]

    def add_message(role: str, content, from_compaction: bool = False) -> None:
        text = text_of(content)
        category, detail = categorize_message(role, text)
        item = _item(category, len(text.encode("utf-8")), role=role, detail=detail, from_compaction=from_compaction)
        if category == "skill_body":
            skill_path = SKILL_TAG.match(text[:2000])
            record = invocations.setdefault(detail, {"explicit": 0, "implicit": 0, "tokens": 0, "path": None})
            record["explicit"] += 1
            record["tokens"] += item["tokens"]
            record["path"] = skill_path.group(2).strip() if skill_path else record["path"]
        current().add(item)

    def add_call(payload: dict) -> None:
        raw = payload.get("input") if payload.get("type") == "custom_tool_call" else payload.get("arguments")
        raw = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
        tool = payload.get("name") or "unknown"
        skill_paths = sorted({_existing(m) for m in SKILL_PATH.findall(raw)} - {None})
        if PATCH_MARKER not in raw:
            for key in {_existing(m) for m in FILE_PATH.findall(raw)} - {None}:
                file_reads[key] = file_reads.get(key, 0) + 1
        item = _item("tool_input", len(raw.encode("utf-8")), tool=tool, call_id=payload.get("call_id"),
                     head=command_head(tool, raw), skill_paths=skill_paths)
        current().add(item)
        if payload.get("call_id"):
            calls[payload["call_id"]] = item

    def add_output(payload: dict) -> None:
        text = text_of(payload.get("output"))
        call = calls.get(payload.get("call_id") or "", {})
        tool = call.get("tool", "unknown")
        item = _item("tool_output", len(text.encode("utf-8")), tool=tool, call_id=payload.get("call_id"),
                     head=call.get("head"), truncated_original_tokens=None)
        match = TRUNCATED.search(text[:400])
        if match:
            item["truncated_original_tokens"] = int(match.group(1))
            truncated["count"] += 1
            truncated["original_tokens"] += int(match.group(1))
        for skill_path in call.get("skill_paths") or []:
            name = Path(skill_path).parent.name
            if not name:
                continue
            record = invocations.setdefault(name, {"explicit": 0, "implicit": 0, "tokens": 0, "path": None})
            record["implicit"] += 1
            share = item["tokens"] // max(1, len(call["skill_paths"]))
            record["tokens"] += min(share, _file_tokens(skill_path))
            record["path"] = record["path"] or skill_path
        current().add(item)
        tool_outputs.append({"tool": tool, "tokens": item["tokens"], "head": item["head"],
                             "truncated_original_tokens": item["truncated_original_tokens"],
                             "window": current().number, "index": item["index"]})

    def add_response_item(payload: dict, from_compaction: bool = False) -> None:
        nonlocal reasoning_items
        kind = payload.get("type")
        if kind == "message":
            add_message(payload.get("role") or "unknown", payload.get("content"), from_compaction)
        elif kind in ("function_call", "custom_tool_call"):
            add_call(payload)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            add_output(payload)
        elif kind == "reasoning":
            reasoning_items += 1
            summary = text_of(payload.get("summary"))
            current().add(_item("reasoning_summary", len(summary.encode("utf-8"))))
        elif kind == "agent_message":
            current().add(_item("agent_messages", len(text_of(payload.get("content")).encode("utf-8"))))
        elif kind == "compaction":
            current().add(_item("compaction_summary", 0))

    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            if '"type": "reasoning"' in line[:200] and all(marker in line for marker in SKIP_FAST):
                reasoning_items += 1
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "session_meta":
                meta.update(session_id=payload.get("id") or payload.get("session_id"), cwd=payload.get("cwd"),
                            cli_version=payload.get("cli_version"), originator=payload.get("originator"),
                            started=payload.get("timestamp") or record.get("timestamp"),
                            forked_from=payload.get("forked_from_id"))
                base = payload.get("base_instructions")
                text = base.get("text") if isinstance(base, dict) else base
                if isinstance(text, str):
                    base_instructions_tokens = approx_tokens(text)
                    current().add(_item("base_instructions", len(text.encode("utf-8"))))
            elif kind == "turn_context":
                model = payload.get("model")
                if model and model not in meta["models"]:
                    meta["models"].append(model)
                meta["cwd"] = meta["cwd"] or payload.get("cwd")
            elif kind == "world_state":
                body = (((payload.get("state") or {}).get("host_skills") or {}).get("body"))
                if isinstance(body, str) and "### Available skills" in body:
                    catalog_body = body
            elif kind == "response_item":
                if payload.get("type") == "message" and payload.get("role") == "developer":
                    text = text_of(payload.get("content"))
                    if text.lstrip().startswith("<skills_instructions>") and "### Available skills" in text:
                        catalog_body = catalog_body or text
                add_response_item(payload)
            elif kind == "token_usage_record":
                saw_usage_record = True
                current().record(payload.get("usage") or {}, payload.get("response_id"), payload.get("turn_id"))
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if info.get("last_token_usage"):
                    token_count_fallback.append((len(windows), info["last_token_usage"]))
            elif kind == "compacted":
                new = _Window(windows[-1].number + 1)
                if base_instructions_tokens:
                    new.add(_item("base_instructions", base_instructions_tokens * 4))
                windows.append(new)
                for item in payload.get("replacement_history") or []:
                    if isinstance(item, dict):
                        add_response_item(item, from_compaction=True)
                new.carried_over_tokens = new.items_tokens

    if not saw_usage_record:
        for window_index, usage in token_count_fallback:
            windows[min(window_index, len(windows)) - 1].record(usage, None, None)

    summaries = [w.summary() for w in windows]
    totals = {key: sum(w[key] for w in summaries) for key in
              ("calls", "input_total", "cached_total", "net_new_total", "output_total", "reasoning_total")}
    categories: dict[str, dict] = {}
    for w in summaries:
        for name, slot in w["categories"].items():
            target = categories.setdefault(name, {"tokens": 0, "count": 0})
            target["tokens"] += slot["tokens"]
            target["count"] += slot["count"]
    static = {}
    for name in ("base_instructions", "skills_catalog", "agents_md", "environment", "model_switch"):
        static[name] = max(w["categories"].get(name, {}).get("max_item", 0) for w in summaries)
    # Developer instructions are several messages; count each tag once at its largest.
    tags: dict[str, int] = {}
    untagged = 0
    for w in summaries:
        slot = w["categories"].get("developer_instructions", {})
        for tag, tokens in (slot.get("by_tag") or {}).items():
            tags[tag] = max(tags.get(tag, 0), tokens)
        untagged = max(untagged, slot.get("tokens", 0) - sum((slot.get("by_tag") or {}).values()))
    static["developer_instructions"] = sum(tags.values()) + untagged
    static["developer_by_tag"] = tags
    seen_outputs, unique_outputs = set(), []
    for output in sorted(tool_outputs, key=lambda t: -t["tokens"]):
        key = (output["tool"], output["head"], output["tokens"])
        if key in seen_outputs:
            continue
        seen_outputs.add(key)
        unique_outputs.append(output)
    result = {
        **meta,
        "usage_source": "token_usage_record" if saw_usage_record else ("token_count" if token_count_fallback else "none"),
        "windows": summaries,
        "totals": totals,
        "startup_input": summaries[0]["first_input"],
        "startup_estimated": summaries[0]["estimated_prompt_at_first_call"],
        "startup_unattributed": (summaries[0]["first_input"] - summaries[0]["estimated_prompt_at_first_call"]
                                 if summaries[0]["first_input"] is not None and summaries[0]["estimated_prompt_at_first_call"] is not None else None),
        "peak_input": max((w["peak_input"] or 0) for w in summaries),
        "categories": categories,
        "static": static,
        "catalog": parse_skills_body(catalog_body) if catalog_body else None,
        "catalog_tokens": approx_tokens(catalog_body) if catalog_body else 0,
        "skill_invocations": invocations,
        "tool_outputs": unique_outputs[:25],
        "tools": _tool_totals(windows),
        "file_reads": {k: v for k, v in sorted(file_reads.items(), key=lambda kv: -kv[1]) if v >= 2},
        "truncated_outputs": truncated,
        "reasoning_items": reasoning_items,
    }
    if keep_items:
        result["items"] = [w.items for w in windows]
    return result


def _tool_totals(windows: list[_Window]) -> dict:
    totals: dict[str, dict] = {}
    for window in windows:
        for item in window.items:
            if item["category"] in ("tool_input", "tool_output"):
                slot = totals.setdefault(item.get("tool") or "unknown", {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                if item["category"] == "tool_input":
                    slot["calls"] += 1
                    slot["input_tokens"] += item["tokens"]
                else:
                    slot["output_tokens"] += item["tokens"]
    return totals


def recent_rollouts(home: Path, limit: int = 200) -> list[Path]:
    folder = home / "sessions"
    if not folder.is_dir():
        return []
    files = list(folder.rglob("rollout-*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]
