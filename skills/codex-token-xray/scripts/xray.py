#!/usr/bin/env python3
"""Read-only scan of where Codex context tokens go, from local session logs.

Measured: token_usage_record entries in ~/.codex/sessions (input, cached, output,
reasoning). Estimated: bytes / 4 of each prompt part found in the same logs, the
approximation Codex uses for its skills budget. The two are never mixed; the report
shows how well the estimate tracks the measurement.

Writes the full JSON report under ~/.codex-token-xray/scans/ and prints a summary.
No network calls. Changes nothing outside ~/.codex-token-xray.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txray import __version__, agents_md, budget, config, rollout, usage  # noqa: E402
from txray import constants as C  # noqa: E402
from txray import skills as sk  # noqa: E402
from txray.archive import private_write, state_dir  # noqa: E402
from txray.budget import approx_tokens  # noqa: E402
from txray.frontmatter import read_skill  # noqa: E402
from txray.native import classify, native_names  # noqa: E402
from txray.paths import local_key  # noqa: E402

DESCRIPTION_REVIEW_CHARS = 200
BODY_REVIEW_TOKENS = 1_500
AGENTS_REVIEW_TOKENS = 2_000


def select_sessions(home: Path, args, cwd: Path) -> list[Path]:
    if args.session:
        return [Path(p).expanduser() for p in args.session]
    cutoff = None
    if args.days:
        cutoff = (datetime.now() - timedelta(days=args.days)).timestamp()
    chosen = []
    for path in rollout.recent_rollouts(home, limit=400):
        if cutoff and path.stat().st_mtime < cutoff:
            break
        if args.project:
            head = _session_cwd(path)
            if not head or local_key(head) != local_key(str(cwd)):
                continue
        chosen.append(path)
        if len(chosen) >= args.sessions:
            break
    return chosen


def _session_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"session_meta"' in line:
                    record = json.loads(line)
                    return (record.get("payload") or {}).get("cwd")
                if '"turn_context"' in line:
                    record = json.loads(line)
                    return (record.get("payload") or {}).get("cwd")
    except (OSError, ValueError):
        return None
    return None


def compact_session(s: dict) -> dict:
    top = sorted(s["categories"].items(), key=lambda kv: -kv[1]["tokens"])[:6]
    return {
        "path": s["path"], "session_id": s["session_id"], "cwd": s["cwd"], "models": s["models"],
        "cli_version": s["cli_version"], "originator": s["originator"], "started": s["started"],
        "forked_from": s["forked_from"], "fresh_start": bool(s["static"].get("skills_catalog")),
        "usage_source": s["usage_source"], "windows": len(s["windows"]), "totals": s["totals"],
        "startup_input": s["startup_input"], "startup_estimated": s["startup_estimated"],
        "startup_unattributed": s["startup_unattributed"], "peak_input": s["peak_input"],
        "static": s["static"], "catalog_tokens": s["catalog_tokens"],
        "top_categories": [{"category": k, "tokens": v["tokens"]} for k, v in top],
        "skill_invocations": s["skill_invocations"], "truncated_outputs": s["truncated_outputs"],
        "growth_calibration": [w["growth_calibration"] for w in s["windows"]],
    }


def catalog_view(session: dict, home: Path, cfg: dict, natives: set[str]) -> dict | None:
    parsed = session.get("catalog")
    if not parsed or not parsed["entries"]:
        return None
    model = (session["models"] or [cfg.get("model")])[0]
    window, window_source = config.model_context_window(cfg, home, model)
    max_tokens = config.get(cfg, "skills.max_context_tokens")
    budget_obj = budget.metadata_budget(window, max_tokens if isinstance(max_tokens, int) else None)
    entries, rows = [], []
    for e in parsed["entries"]:
        path = Path(e["path"]).expanduser()
        disk = read_skill(path) if path.is_file() else None
        description = disk["description"] if disk and disk["description"] else e["description"]
        protection = classify(path, home, natives) if path.is_file() else {
            "kind": "native" if any(m in local_key(e["path"]) for m in ("/skills/.system/", "/plugins/cache/")) else "unknown",
            "editable": False, "provenance": "missing-file", "source": None, "caveat": "file not found on this machine"}
        entries.append({"name": e["name"], "description": description, "path": e["path"],
                        "alias_root": dict(parsed["roots"]).get(e["alias"]) if e["alias"] else None,
                        "alias_root_order": int(e["alias"][1:]) if e["alias"] else None})
        rows.append({"name": e["name"], "path": e["path"], "description_chars": len(description),
                     "tokens": approx_tokens(description), "protection": protection})
    plan = budget.AliasPlan(parsed["roots"]) if parsed["roots"] else None
    sim = budget.render_host_catalog(entries, budget_obj, plan=plan)
    for row, per in zip(rows, sim["per_skill"]):
        row["status"] = per["status"]
        row["shown_chars"] = per.get("shown_chars")
    editable = [r for r in rows if r["protection"]["editable"]]
    native = [r for r in rows if r["protection"]["kind"] == "native"]
    return {
        "source_session": session["path"], "model": model, "context_window": window,
        "context_window_source": window_source, "budget": budget_obj.to_dict(),
        "entries": len(rows), "omitted_marker": parsed["omission_marker"],
        "used_units": sim["cost"], "used_percent": round(100 * sim["cost"] / max(1, budget_obj.limit)),
        "report": sim["report"], "warning": sim["warning"],
        "catalog_tokens_in_prompt": session["catalog_tokens"],
        "editable": {"count": len(editable), "description_chars": sum(r["description_chars"] for r in editable),
                     "tokens": sum(r["tokens"] for r in editable)},
        "native": {"count": len(native), "description_chars": sum(r["description_chars"] for r in native),
                   "tokens": sum(r["tokens"] for r in native)},
        "skills": sorted(rows, key=lambda r: -r["tokens"]),
    }


def run(args) -> dict:
    home = Path(args.codex_home).expanduser() if args.codex_home else config.codex_home()
    cwd = Path(args.cwd).expanduser().resolve()
    cfg = config.load_config(home)
    natives = native_names(home)

    paths = select_sessions(home, args, cwd)
    parsed = [s for s in (rollout.parse(p) for p in paths) if s]
    agg = usage.aggregate(parsed)

    catalog_session = next((s for s in parsed if s.get("catalog")), None)
    catalog = catalog_view(catalog_session, home, cfg, natives) if catalog_session else None

    inv = sk.inventory(cwd, home, cfg)
    by_name = {s["name"]: s for s in inv["skills"]}
    by_path = {local_key(s["path"]): s for s in inv["skills"]}
    invoked = {}
    for row in agg["skills"]:
        disk = by_path.get(local_key(row["path"])) if row.get("path") else None
        disk = disk or by_name.get(row["name"])
        path = Path(row["path"]).expanduser() if row.get("path") else None
        protection = disk["protection"] if disk else (classify(path, home, natives) if path and path.is_file() else None)
        invoked[row["name"]] = {**row, "on_disk": bool(disk or (path and path.is_file())),
                                "protection": protection,
                                "body_tokens": disk["body_tokens"] if disk else None,
                                "body_lines": disk["body_lines"] if disk else None}

    description_candidates = []
    if catalog:
        for row in catalog["skills"]:
            if row["protection"]["editable"] and row["description_chars"] > DESCRIPTION_REVIEW_CHARS \
                    and row["protection"]["provenance"] == "local":
                description_candidates.append({k: row[k] for k in ("name", "path", "description_chars", "tokens", "status")}
                                              | {"caveat": row["protection"]["caveat"], "provenance": row["protection"]["provenance"]})
    body_candidates = []
    for name, row in invoked.items():
        if row["on_disk"] and row["protection"] and row["protection"]["provenance"] == "local" \
                and (row["body_tokens"] or 0) > BODY_REVIEW_TOKENS:
            body_candidates.append({"name": name, "path": row["path"], "body_tokens": row["body_tokens"],
                                    "body_lines": row["body_lines"], "invocations": row["explicit"] + row["implicit"],
                                    "tokens_spent": row["tokens"], "sessions": row["sessions"]})
    body_candidates.sort(key=lambda r: -r["tokens_spent"])

    # Skills in the catalog that no scanned session ever read. Yours are listed for you to decide.
    unused = []
    if catalog:
        for row in catalog["skills"]:
            prot = row["protection"]
            if prot["kind"] == "native" or row["name"] in invoked:
                continue
            unused.append({"name": row["name"], "path": row["path"], "source": prot.get("source"),
                           "yours": prot.get("provenance") == "local", "description_tokens": row["tokens"]})
        unused.sort(key=lambda r: (r["yours"], -r["description_tokens"]))

    global_info = agents_md.global_instructions(home)
    chain = agents_md.project_chain(cwd, cfg)
    agents_loaded = [{"path": f["path"], "loaded_bytes": f.get("loaded_bytes", 0),
                      "tokens": (f.get("loaded_bytes", 0) + 3) // 4, "status": f["status"]}
                     for f in chain["files"] if f["status"] in ("loaded", "truncated")]
    if global_info["loaded"]:
        agents_loaded.insert(0, {"path": global_info["loaded"], "loaded_bytes": global_info["bytes"],
                                 "tokens": (global_info["bytes"] + 3) // 4, "status": "loaded"})
    agents_candidates = [a for a in agents_loaded if a["tokens"] > AGENTS_REVIEW_TOKENS]

    trim = {
        "catalog_editable_tokens_per_session": catalog["editable"]["tokens"] if catalog else None,
        "catalog_native_tokens_per_session": catalog["native"]["tokens"] if catalog else None,
        "invoked_editable_body_tokens_spent": sum(r["tokens_spent"] for r in body_candidates),
        "agents_md_tokens_per_session": sum(a["tokens"] for a in agents_loaded),
        "thresholds": {"description_chars": DESCRIPTION_REVIEW_CHARS, "body_tokens": BODY_REVIEW_TOKENS,
                       "agents_md_tokens": AGENTS_REVIEW_TOKENS,
                       "meaning": "review heuristics, not limits; the report never claims a saving before new sessions confirm it"},
        "candidates": {"descriptions": description_candidates, "bodies": body_candidates,
                       "unused": unused, "agents_md": agents_candidates},
        "policy": "only skills you wrote are trimmed; unused skills can be removed with a backup; other people's skills and plugins are never edited",
    }
    surface = [c["path"] for c in description_candidates] + [c["path"] for c in body_candidates] + [a["path"] for a in agents_candidates]

    return {
        "tool": "codex-token-xray",
        "version": __version__,
        "codex_rules_pinned_to": C.CODEX_PINNED_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {
            "codex_home": str(home), "cwd": str(cwd), "shell_codex_cli": config.codex_cli_version(),
            "configured_model": cfg.get("model"), "sessions_requested": args.sessions, "days": args.days,
            "project_only": bool(args.project), "sessions_found": len(paths), "sessions_parsed": len(parsed),
        },
        "usage": agg,
        "sessions": [compact_session(s) for s in parsed],
        "catalog": catalog,
        "skills": {"roots": inv["roots"], "native_names": inv["native_names"],
                   "editable_on_disk": sum(1 for s in inv["skills"] if s["protection"]["editable"]),
                   "native_on_disk": sum(1 for s in inv["skills"] if s["protection"]["kind"] == "native"),
                   "inventory": [{k: s[k] for k in ("name", "path", "root_kind", "description_chars", "description_tokens",
                                                     "body_tokens", "body_lines", "sha256", "protection", "error")}
                                 for s in inv["skills"]],
                   "invoked": invoked},
        "agents_md": {"global": global_info, "project": chain, "loaded": agents_loaded},
        "trim": trim,
        "surface_files": sorted(dict.fromkeys(p for p in surface if Path(p).exists())),
    }


def _fmt(n) -> str:
    return "?" if n is None else f"{n:,}"


def summary_text(report: dict, out_path: Path) -> str:
    env, agg = report["environment"], report["usage"]
    lines = [f"codex-token-xray {report['version']} | Codex rules pinned to {C.CODEX_PINNED_VERSION}",
             f"Sessions    {env['sessions_parsed']} parsed from {env['codex_home']} | models {', '.join(agg['models']) or '?'}"]
    if not env["sessions_parsed"]:
        lines.append("            no rollout logs found; run a few Codex tasks first")
        lines.append(f"Report      {out_path}")
        return "\n".join(lines)
    m = agg["measured"]
    lines.append(f"Measured    sum over {m['calls']:,} model calls: input {_fmt(m['input_total'])} | cached {_fmt(m['cached_total'])} | net new {_fmt(m['net_new_total'])} | output {_fmt(m['output_total'])} (reasoning {_fmt(m['reasoning_total'])})")
    lines.append(f"            peak context median {_fmt(agg['peak_input_median'])} tokens | {agg['compactions']} compactions")
    st = agg["startup"]
    if st["sessions_with_fresh_start"]:
        s = st["static_median"]
        lines.append(f"Startup     {_fmt(st['median_startup_input'])} tokens before any work (median of {st['sessions_with_fresh_start']} fresh sessions)")
        tags = sorted(st.get("developer_by_tag_median", {}).items(), key=lambda kv: -(kv[1] or 0))[:2]
        tag_text = ", ".join(f"{k} {_fmt(v)}" for k, v in tags if v)
        lines.append(f"            base {_fmt(s['base_instructions'])} | developer {_fmt(s['developer_instructions'])}{' (' + tag_text + ')' if tag_text else ''} | skills catalog {_fmt(s['skills_catalog'])} | AGENTS.md {_fmt(s['agents_md'])} | unattributed {_fmt(st['median_unattributed'])}")
    lines.append("Where it goes (estimated by size, per session average)")
    for row in agg["categories"][:7]:
        lines.append(f"            {row['per_session']:>9,}  {row['share']:>5}%  {row['label']}")
    cal = agg["calibration"]
    if cal["growth_median"] is not None:
        lines.append(f"            estimate tracks measured growth at {cal['growth_median']} (range {cal['growth_min']}..{cal['growth_max']}) over {cal['windows']} windows")
    cat = report["catalog"]
    if cat:
        lines.append(f"Catalog     {cat['entries']} skills | {cat['catalog_tokens_in_prompt']:,} tokens in prompt | budget {cat['budget']['limit']:,} {cat['budget']['unit']} ({cat['used_percent']}% used, {cat['report']['truncated_count']} cut)")
        lines.append(f"            editable descriptions {cat['editable']['count']} = {cat['editable']['tokens']:,} tokens | native {cat['native']['count']} = {cat['native']['tokens']:,} tokens (never edited)")
    if agg["skills"]:
        lines.append("Skill bodies read in these sessions")
        for row in agg["skills"][:5]:
            inv = report["skills"]["invoked"].get(row["name"], {})
            prot = (inv.get("protection") or {}).get("kind", "?")
            lines.append(f"            {row['tokens']:>9,}  {row['name']} | {row['explicit'] + row['implicit']}x | {prot}")
    if agg["tool_outputs_top"]:
        top = agg["tool_outputs_top"][0]
        lines.append(f"Largest tool output {top['tokens']:,} tokens | {top['tool']} | {(top['head'] or '')[:70]}")
    if agg["truncated_outputs"]["count"]:
        lines.append(f"            Codex already truncated {agg['truncated_outputs']['count']} outputs (original {_fmt(agg['truncated_outputs']['original_tokens'])} tokens)")
    if agg["repeated_file_references"]:
        r = agg["repeated_file_references"][0]
        lines.append(f"Repeated    {r['path']} referenced in {r['calls']} calls of one session")
    t = report["trim"]["candidates"]
    lines.append(f"Trim        {len(t['descriptions'])} descriptions | {len(t['bodies'])} long bodies, all yours | {len(t['agents_md'])} AGENTS.md files")
    if t["unused"]:
        theirs = sum(1 for u in t["unused"] if not u["yours"])
        lines.append(f"Unused      {len(t['unused'])} skills never read in these sessions ({theirs} installed from elsewhere, {len(t['unused']) - theirs} yours)")
    lines.append(f"Report      {out_path}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cwd", default=os.getcwd(), help="project directory (default: current)")
    parser.add_argument("--codex-home", help="Codex home (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument("--sessions", type=int, default=400, help="how many recent sessions to read (default 400)")
    parser.add_argument("--days", type=int, default=90, help="ignore sessions older than this (default 90)")
    parser.add_argument("--project", action="store_true", help="only sessions started in --cwd")
    parser.add_argument("--session", action="append", help="read this rollout file instead; repeatable")
    parser.add_argument("--out", help="where to write the JSON report")
    parser.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = state_dir() / "scans" / f"xray-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    private_write(out_path, payload.encode("utf-8"))
    if not args.out:
        private_write(out_path.parent / "latest.json", payload.encode("utf-8"))
    print(payload if args.json else summary_text(report, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
