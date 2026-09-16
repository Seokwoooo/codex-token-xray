"""Regression tests with disposable Codex homes, sessions and skills."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-token-xray" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import apply  # noqa: E402
import restore  # noqa: E402
import xray  # noqa: E402
from txray import archive, budget, config, native, rollout, usage  # noqa: E402
from txray.frontmatter import read_skill, replace_body  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.user = self.root / "user"
        self.home = self.user / ".codex"
        (self.home / "sessions" / "2026" / "09" / "16").mkdir(parents=True)
        self.work = self.root / "work"
        (self.work / ".git").mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.user).start()
        patch.object(config, "codex_cli_version", return_value="0.154.0").start()
        patch.dict(os.environ, {"CODEX_TOKEN_XRAY_HOME": str(self.root / "state")}).start()

    def skill(self, name="personal", parent=None, desc=None, body=None):
        path = (parent or self.user / ".agents" / "skills") / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: " + name + "\ndescription: " + (desc or ("Personal workflow. " * 15)) +
                        "\n---\n" + (body or ("Body line.\n" * 700)), encoding="utf-8")
        return path

    def rollout(self, catalog_paths, agents_text=None, explicit=None, reads=(), name="rollout-2026-09-16T10-00-00-abc.jsonl",
                cwd=None, usage_records=3):
        cwd = cwd or str(self.work)
        lines = [f"- {read_skill(p)['name']}: {read_skill(p)['description']} (file: {p.as_posix()})" for p in catalog_paths]
        catalog = "<skills_instructions>\n## Skills\n### Available skills\n" + "\n".join(lines) + "\n</skills_instructions>"
        records = [
            {"type": "session_meta", "timestamp": "2026-09-16T01:00:00Z", "payload": {
                "id": "s1", "cwd": cwd, "cli_version": "0.154.0", "originator": "Codex CLI",
                "timestamp": "2026-09-16T01:00:00Z", "base_instructions": {"text": "x" * 4000}}},
            {"type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": cwd}},
            {"type": "world_state", "payload": {"full": True, "state": {"host_skills": {"body": catalog}}}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<app-context>\n" + "a" * 2000}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": catalog}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>x</cwd>"}]}},
        ]
        if agents_text:
            records.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": f"# AGENTS.md instructions for {cwd}\n\n<INSTRUCTIONS>\n{agents_text}\n</INSTRUCTIONS>"}]}})
        if explicit:
            records.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": f"<skill>\n<name>{explicit.parent.name}</name>\n<path>{explicit.as_posix()}</path>\n{explicit.read_text()}\n</skill>"}]}})
        records.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do the thing"}]}})
        records.append({"type": "token_usage_record", "payload": {"response_id": "r1", "turn_id": "t1", "usage": {
            "input_tokens": 9000, "cached_input_tokens": 0, "output_tokens": 50, "reasoning_output_tokens": 20}}})
        for index, path in enumerate(reads):
            records.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": f"c{index}",
                            "input": f'await tools.exec_command({{cmd:"cat {path.as_posix()}\\ncat {path.as_posix()}"}})'}})
            text = "Script completed\nOutput:\n" + path.read_text()
            if index == 0:
                text = "Warning: truncated output (original token count: 5000)\n" + text
            records.append({"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": f"c{index}",
                            "output": [{"type": "input_text", "text": text}]}})
            records.append({"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}], "encrypted_content": "zzz"}})
            records.append({"type": "token_usage_record", "payload": {"response_id": f"r{index + 2}", "turn_id": "t1", "usage": {
                "input_tokens": 9000 + 3000 * (index + 1), "cached_input_tokens": 9000 + 3000 * index, "output_tokens": 40, "reasoning_output_tokens": 10}}})
        if usage_records > 3:
            records.append({"type": "compacted", "payload": {"window_number": 1, "replacement_history": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary " * 100}]}]}})
            records.append({"type": "token_usage_record", "payload": {"response_id": "r9", "turn_id": "t2", "usage": {
                "input_tokens": 2500, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0}}})
        records.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}})
        log = self.home / "sessions" / "2026" / "09" / "16" / name
        log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return log

    def args(self, **overrides):
        base = {"cwd": str(self.work), "codex_home": str(self.home), "sessions": 12, "days": None,
                "project": False, "session": None}
        base.update(overrides)
        return SimpleNamespace(**base)

    def invoke(self, module, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(list(args))
        return code, json.loads(out.getvalue())

    def plan(self, edits, create=()):
        path = self.root / "plan.json"
        path.write_text(json.dumps({"edits": edits, "create": list(create)}), encoding="utf-8")
        return path


class Budget(unittest.TestCase):
    def test_budget_follows_codex_rules(self):
        self.assertEqual(budget.metadata_budget(272_000, None).to_dict(), {"unit": "tokens", "limit": 5440})
        self.assertEqual(budget.metadata_budget(None, None).to_dict(), {"unit": "chars", "limit": 8000})
        self.assertEqual(budget.metadata_budget(272_000, 20_000).limit, 10_000)
        long = "x" * 1025
        cut = budget.truncate_description(long)
        self.assertEqual(len(cut), 1024)
        self.assertTrue(cut.endswith("..."))
        entries = [{"name": f"s{i}", "description": "short", "path": f"/r/s{i}/SKILL.md"} for i in range(3)]
        result = budget.render_host_catalog(entries, budget.Budget("tokens", 5440))
        self.assertEqual(result["report"]["included"], 3)
        self.assertEqual(result["report"]["truncated_count"], 0)


class Parsing(Fixture):
    def test_rollout_attribution_and_measurement(self):
        personal = self.skill()
        native_dir = self.home / "skills" / ".system"
        official = self.skill("imagegen", parent=native_dir)
        log = self.rollout([personal, official], agents_text="Never push to production.\n" * 20,
                           explicit=personal, reads=[personal, official], usage_records=4)
        parsed = rollout.parse(log)
        self.assertEqual(parsed["usage_source"], "token_usage_record")
        self.assertEqual(len(parsed["windows"]), 2)
        self.assertEqual(parsed["startup_input"], 9000)
        self.assertEqual(parsed["totals"]["calls"], 4)
        self.assertEqual(parsed["totals"]["net_new_total"], 9000 + 3000 + 3000 + 2500)
        self.assertEqual(parsed["totals"]["reasoning_total"], 40)
        cats = parsed["categories"]
        for name in ("base_instructions", "developer_instructions", "skills_catalog", "environment", "agents_md",
                     "skill_body", "user_message", "tool_input", "tool_output", "assistant_message"):
            self.assertIn(name, cats, name)
        self.assertEqual(cats["developer_instructions"]["count"], 1)
        self.assertGreater(parsed["static"]["skills_catalog"], 0)
        self.assertGreater(parsed["static"]["agents_md"], 0)
        self.assertEqual(parsed["skill_invocations"]["personal"]["explicit"], 1)
        self.assertEqual(parsed["skill_invocations"]["personal"]["implicit"], 1)
        self.assertEqual(parsed["skill_invocations"]["imagegen"]["implicit"], 1)
        body_cap = (len(personal.read_bytes()) + 3) // 4
        # explicit = whole <skill> message (body plus a small wrapper); implicit is capped at the body size
        self.assertLessEqual(parsed["skill_invocations"]["personal"]["tokens"], body_cap * 2 + 64)
        self.assertGreater(parsed["skill_invocations"]["personal"]["tokens"], body_cap)
        self.assertEqual(parsed["truncated_outputs"], {"count": 1, "original_tokens": 5000})
        self.assertNotIn(str(personal.resolve()), parsed["file_reads"])  # one read is not repeated
        self.assertEqual(len(parsed["catalog"]["entries"]), 2)
        self.assertEqual(parsed["windows"][0]["estimated_prompt_at_first_call"], parsed["startup_estimated"])
        self.assertIsNotNone(parsed["windows"][0]["growth_calibration"])
        self.assertGreater(parsed["windows"][1]["carried_over_tokens"], 0)

    def test_paths_counted_once_per_call_and_only_when_present(self):
        personal = self.skill()
        log = self.rollout([personal], reads=[personal, personal, personal])
        parsed = rollout.parse(log)
        self.assertEqual(parsed["file_reads"][str(personal.resolve())], 3)
        self.assertEqual(parsed["skill_invocations"]["personal"]["implicit"], 3)
        call = {"type": "custom_tool_call", "name": "exec", "call_id": "z",
                "input": 'cat /absolute/path/to/nowhere/SKILL.md; echo "*** Begin Patch"'}
        self.assertEqual(rollout.SKILL_PATH.findall(call["input"]), ["/absolute/path/to/nowhere/SKILL.md"])
        self.assertEqual(rollout.FILE_PATH.findall("see skills/astra-xray/scripts/scan.py and /tmp/a.py"), ["/tmp/a.py"])

    def test_token_count_fallback_for_old_logs(self):
        log = self.home / "sessions" / "2026" / "09" / "16" / "rollout-old.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": "old", "cwd": str(self.work), "cli_version": "0.140.0", "timestamp": "2026-09-01T00:00:00Z"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 5}}}},
        ]
        log.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        parsed = rollout.parse(log)
        self.assertEqual(parsed["usage_source"], "token_count")
        self.assertEqual(parsed["totals"]["net_new_total"], 1000)

    def test_aggregate_ranks_and_calibrates(self):
        personal = self.skill()
        logs = [self.rollout([personal], reads=[personal], name=f"rollout-2026-09-16T10-00-0{i}-x.jsonl") for i in range(2)]
        agg = usage.aggregate([rollout.parse(p) for p in logs])
        self.assertEqual(agg["sessions"], 2)
        self.assertEqual(agg["startup"]["sessions_with_fresh_start"], 2)
        self.assertEqual(agg["startup"]["median_startup_input"], 9000)
        self.assertEqual(agg["categories"][0]["tokens"], max(c["tokens"] for c in agg["categories"]))
        self.assertEqual(agg["skills"][0]["name"], "personal")
        self.assertIsNotNone(agg["calibration"]["growth_median"])
        self.assertEqual(usage.aggregate([])["sessions"], 0)


class Protection(Fixture):
    def test_native_paths_and_copies_are_never_editable(self):
        natives = self.home / "skills" / ".system"
        official = self.skill("computer-use", parent=natives)
        plugin = self.skill("sites-building", parent=self.home / "plugins" / "cache" / "openai-curated-remote" / "sites" / "1.0" / "skills")
        names = native.native_names(self.home)
        self.assertEqual(names, {"computer-use", "sites-building"})
        for path in (official, plugin):
            self.assertEqual(native.classify(path, self.home, names)["kind"], "native")
        copy = self.skill("computer-use")
        self.assertEqual(native.classify(copy, self.home, names)["provenance"], "native-copy")
        licensed = self.skill("pdf")
        (licensed.parent / "LICENSE.txt").write_text("Copyright (c) 2026 OpenAI. All rights reserved.")
        self.assertEqual(native.classify(licensed, self.home, names)["provenance"], "openai-copyright")

    def test_upstream_and_popular_skills_are_editable_with_caveat(self):
        playwright = self.skill("playwright")
        (playwright.parent / "NOTICE.txt").write_text("Copyright (c) Microsoft Corporation.")
        lock = self.user / ".agents" / ".skill-lock.json"
        lock.write_text(json.dumps({"skills": {"playwright": {"source": "microsoft/playwright-cli"}}}))
        result = native.classify(playwright, self.home, native.native_names(self.home))
        self.assertTrue(result["editable"])
        self.assertEqual(result["provenance"], "upstream")
        self.assertIn("microsoft/playwright-cli", result["caveat"])
        local = self.skill("mine")
        self.assertEqual(native.classify(local, self.home, set())["provenance"], "local")


class Trim(Fixture):
    def test_scan_report_lists_candidates_and_protection(self):
        personal = self.skill(desc="Personal workflow. " * 15)
        official = self.skill("imagegen", parent=self.home / "skills" / ".system")
        (self.work / "AGENTS.md").write_text("Keep secrets out of logs.\n" * 400)
        self.rollout([personal, official], agents_text="x", explicit=personal, reads=[personal, official])
        report = xray.run(self.args())
        self.assertEqual(report["environment"]["sessions_parsed"], 1)
        cat = report["catalog"]
        self.assertEqual(cat["entries"], 2)
        self.assertEqual(cat["editable"]["count"], 1)
        self.assertEqual(cat["native"]["count"], 1)
        self.assertEqual(cat["budget"], {"unit": "tokens", "limit": 5440})
        names = {c["name"] for c in report["trim"]["candidates"]["descriptions"]}
        self.assertEqual(names, {"personal"})
        bodies = {c["name"] for c in report["trim"]["candidates"]["bodies"]}
        self.assertEqual(bodies, {"personal"})
        self.assertEqual(report["skills"]["invoked"]["imagegen"]["protection"]["kind"], "native")
        self.assertEqual([a["path"] for a in report["trim"]["candidates"]["agents_md"]], [str(self.work / "AGENTS.md")])
        text = xray.summary_text(report, Path("/tmp/x.json"))
        self.assertIn("Measured", text)
        self.assertIn("never edited", text)

    def test_scan_without_sessions_still_reports(self):
        report = xray.run(self.args())
        self.assertEqual(report["environment"]["sessions_parsed"], 0)
        self.assertIsNone(report["catalog"])
        self.assertIn("no rollout logs", xray.summary_text(report, Path("/tmp/x.json")))

    def test_apply_description_body_reference_and_agents_then_restore(self):
        personal = self.skill()
        agents = self.work / "AGENTS.md"
        agents.write_text("Long intro.\n" * 50 + "Never push to production.\n")
        before = {p: p.read_bytes() for p in (personal, agents)}
        edits = [
            {"path": str(personal), "sha256": archive.sha256_path(personal), "body": "Core steps only.\nSee references/details.md for edge cases.\nNever modify source media.",
             "reason": "moved edge cases to references"},
            {"path": str(agents), "sha256": archive.sha256_path(agents), "content": "Never push to production.\n", "reason": "dropped intro"},
        ]
        create = [{"path": str(personal.parent / "references" / "details.md"), "content": "# Edge cases\n"}]
        code, preview = self.invoke(apply, "--plan", str(self.plan(edits, create)), "--codex-home", str(self.home))
        self.assertEqual(code, 0, preview)
        self.assertFalse(preview["applied"])
        self.assertEqual(personal.read_bytes(), before[personal])
        self.assertGreater(preview["tokens_saved_estimate"], 0)
        code, result = self.invoke(apply, "--plan", str(self.plan(edits, create)), "--apply", "--codex-home", str(self.home))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["seal"]["sealed"])
        self.assertTrue((personal.parent / "references" / "details.md").is_file())
        self.assertEqual(read_skill(personal)["name"], "personal")
        self.assertIn("Core steps only.", personal.read_text())
        self.assertEqual(agents.read_text(), "Never push to production.\n")
        edits2 = [{"path": str(personal), "sha256": archive.sha256_path(personal),
                   "description": "Personal workflow for clips. Not for longform.", "reason": "trigger kept"}]
        code, result2 = self.invoke(apply, "--plan", str(self.plan(edits2)), "--apply", "--codex-home", str(self.home))
        self.assertEqual(code, 0, result2)
        self.assertEqual(read_skill(personal)["description"], "Personal workflow for clips. Not for longform.")
        code, restored = self.invoke(restore, result2["backup"]["backup"], "--yes")
        self.assertEqual(code, 0, restored)
        code, restored = self.invoke(restore, result["backup"]["backup"], "--yes")
        self.assertEqual(code, 0, restored)
        self.assertEqual(personal.read_bytes(), before[personal])
        self.assertEqual(agents.read_bytes(), before[agents])
        self.assertFalse((personal.parent / "references" / "details.md").exists())

    def test_native_in_batch_rejects_everything(self):
        personal = self.skill()
        official = self.skill("imagegen", parent=self.home / "skills" / ".system")
        original = personal.read_bytes()
        edits = [{"path": str(p), "sha256": archive.sha256_path(p), "description": "short", "reason": "r"} for p in (personal, official)]
        code, result = self.invoke(apply, "--plan", str(self.plan(edits)), "--apply", "--codex-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("native", result["error"].lower())
        self.assertEqual(personal.read_bytes(), original)
        self.assertFalse((self.root / "state" / "backups").exists())

    def test_stale_hash_and_stray_create_are_rejected(self):
        personal = self.skill()
        edits = [{"path": str(personal), "sha256": "0" * 64, "description": "short", "reason": "r"}]
        code, result = self.invoke(apply, "--plan", str(self.plan(edits)), "--codex-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("changed since review", result["error"])
        edits = [{"path": str(personal), "sha256": archive.sha256_path(personal), "description": "short", "reason": "r"}]
        create = [{"path": str(self.work / "elsewhere.md"), "content": "x"}]
        code, result = self.invoke(apply, "--plan", str(self.plan(edits, create)), "--codex-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("inside a skill folder", result["error"])

    def test_body_replacement_preserves_frontmatter_bom_and_crlf(self):
        data = b"\xef\xbb\xbf---\r\nname: a\r\ndescription: d\r\n---\r\n\r\nold\r\n"
        out = replace_body(data, "new\nsecond")
        self.assertEqual(out, b"\xef\xbb\xbf---\r\nname: a\r\ndescription: d\r\n---\r\n\r\nnew\r\nsecond\r\n")

    def test_write_failure_rolls_back_written_files(self):
        first, second = self.skill(), self.skill("second")
        original = first.read_bytes()
        real = apply.write_bytes

        def fail_second(path, data):
            if path == second:
                raise OSError("disk full")
            real(path, data)

        edits = [{"path": str(p), "sha256": archive.sha256_path(p), "description": "short", "reason": "r"} for p in (first, second)]
        with patch.object(apply, "write_bytes", side_effect=fail_second):
            code, result = self.invoke(apply, "--plan", str(self.plan(edits)), "--apply", "--codex-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(first.read_bytes(), original)
        self.assertTrue(result["seal"]["sealed"])


class Distribution(unittest.TestCase):
    def test_repo_ships_exactly_one_skill_with_valid_frontmatter(self):
        repo = Path(__file__).resolve().parents[1]
        found = sorted(repo.glob("skills/*/SKILL.md"))
        self.assertEqual([p.parent.name for p in found], ["codex-token-xray"])
        info = read_skill(found[0])
        self.assertIsNone(info["error"])
        self.assertLessEqual(len(info["description"]), 1024)
        for readme in ("README.md", "README.ko.md"):
            text = (repo / readme).read_text(encoding="utf-8")
            self.assertIn("npx skills add Seokwoooo/codex-token-xray", text)
            self.assertNotIn("·", text)


if __name__ == "__main__":
    unittest.main()
