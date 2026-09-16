"""Aggregate parsed sessions into a ranked view of where context tokens go."""

from __future__ import annotations

from statistics import median

CATEGORY_LABELS = {
    "tool_output": "tool outputs",
    "tool_input": "tool call inputs",
    "reasoning": "reasoning carried between calls (measured)",
    "developer_instructions": "Codex developer instructions",
    "base_instructions": "Codex base instructions",
    "model_switch": "model switch reinjection",
    "skills_catalog": "skills catalog",
    "skill_body": "skill bodies injected by $mention",
    "agents_md": "AGENTS.md",
    "environment": "environment context",
    "assistant_message": "assistant messages",
    "user_message": "user messages",
    "system_notices": "system notices",
    "reasoning_summary": "reasoning summaries",
    "agent_messages": "sub-agent messages",
    "compaction_summary": "compaction summaries",
    "other_message": "other messages",
}
STATIC = ("base_instructions", "skills_catalog", "agents_md", "environment", "developer_instructions", "model_switch")


def _median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return int(median(values)) if values else None


def aggregate(sessions: list[dict]) -> dict:
    measured = {k: sum(s["totals"][k] for s in sessions) for k in
                ("calls", "input_total", "cached_total", "net_new_total", "output_total", "reasoning_total")}
    windows = [w for s in sessions for w in s["windows"]]

    categories = {}
    for s in sessions:
        for name, slot in s["categories"].items():
            target = categories.setdefault(name, {"tokens": 0, "count": 0, "sessions": 0})
            target["tokens"] += slot["tokens"]
            target["count"] += slot["count"]
            target["sessions"] += 1
    estimated_total = sum(c["tokens"] for c in categories.values())
    ranked = []
    for name, slot in categories.items():
        ranked.append({
            "category": name, "label": CATEGORY_LABELS.get(name, name),
            "tokens": slot["tokens"], "count": slot["count"], "sessions": slot["sessions"],
            "per_session": slot["tokens"] // max(1, len(sessions)),
            "share": round(100 * slot["tokens"] / estimated_total, 1) if estimated_total else 0.0,
            "measured": name == "reasoning",
        })
    ranked.sort(key=lambda r: -r["tokens"])

    fresh = [s for s in sessions if s["startup_input"] and s["static"].get("skills_catalog")]
    startup = {
        "sessions_with_fresh_start": len(fresh),
        "median_startup_input": _median([s["startup_input"] for s in fresh]),
        "median_estimated_visible": _median([s["startup_estimated"] for s in fresh]),
        "median_unattributed": _median([s["startup_unattributed"] for s in fresh]),
        "static_median": {name: _median([s["static"].get(name, 0) for s in fresh]) for name in STATIC},
        "developer_by_tag_median": {
            tag: _median([s["static"].get("developer_by_tag", {}).get(tag, 0) for s in fresh])
            for tag in sorted({t for s in fresh for t in s["static"].get("developer_by_tag", {})})},
        "note": "unattributed covers tool schemas and other prompt parts that never appear in the log",
    }

    skills = {}
    for s in sessions:
        for name, rec in s["skill_invocations"].items():
            slot = skills.setdefault(name, {"name": name, "path": rec.get("path"), "explicit": 0, "implicit": 0,
                                            "tokens": 0, "sessions": 0})
            slot["explicit"] += rec["explicit"]
            slot["implicit"] += rec["implicit"]
            slot["tokens"] += rec["tokens"]
            slot["sessions"] += 1
            slot["path"] = slot["path"] or rec.get("path")
    skill_rows = sorted(skills.values(), key=lambda r: -r["tokens"])

    agents = {}
    for s in sessions:
        tokens = s["static"].get("agents_md", 0)
        if tokens:
            key = s.get("cwd") or "?"
            slot = agents.setdefault(key, {"cwd": key, "tokens_per_session": tokens, "sessions": 0})
            slot["sessions"] += 1
            slot["tokens_per_session"] = max(slot["tokens_per_session"], tokens)
    agents_rows = sorted(agents.values(), key=lambda r: -r["tokens_per_session"] * r["sessions"])

    outputs = []
    for s in sessions:
        for o in s["tool_outputs"][:10]:
            outputs.append({**o, "session": s["path"], "model": (s["models"] or [None])[0]})
    outputs.sort(key=lambda o: -o["tokens"])

    repeated = []
    for s in sessions:
        for path, count in s["file_reads"].items():
            if count >= 3:
                repeated.append({"session": s["path"], "path": path, "calls": count})
    repeated.sort(key=lambda r: -r["calls"])

    tools = {}
    for s in sessions:
        for name, slot in s["tools"].items():
            target = tools.setdefault(name, {"tool": name, "calls": 0, "input_tokens": 0, "output_tokens": 0})
            for key in ("calls", "input_tokens", "output_tokens"):
                target[key] += slot[key]
    tool_rows = sorted(tools.values(), key=lambda r: -(r["output_tokens"] + r["input_tokens"]))

    truncated = {"count": sum(s["truncated_outputs"]["count"] for s in sessions),
                 "original_tokens": sum(s["truncated_outputs"]["original_tokens"] for s in sessions)}
    growth = [w["growth_calibration"] for w in windows if w.get("growth_calibration")]
    calibration = {
        "windows": len(windows),
        "growth_median": round(median(growth), 3) if growth else None,
        "growth_min": min(growth) if growth else None,
        "growth_max": max(growth) if growth else None,
        "meaning": "estimated growth divided by measured growth per context window; 1.0 is a perfect byte estimate",
    }
    return {
        "sessions": len(sessions),
        "models": sorted({m for s in sessions for m in s["models"]}),
        "measured": measured,
        "estimated_total": estimated_total,
        "startup": startup,
        "categories": ranked,
        "skills": skill_rows,
        "agents_md": agents_rows,
        "tool_outputs_top": outputs[:15],
        "repeated_file_references": repeated[:15],
        "tools": tool_rows,
        "truncated_outputs": truncated,
        "peak_input_median": _median([s["peak_input"] for s in sessions]),
        "compactions": sum(max(0, len(s["windows"]) - 1) for s in sessions),
        "calibration": calibration,
    }
