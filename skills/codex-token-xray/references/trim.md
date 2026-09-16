# Preparing a trim

A request to trim the reported scope authorizes the normal steps. Show the diff
and continue when that authorization exists. Ask only when a missing decision
changes scope or risks losing later work.

## Never edit

Codex native skills: `~/.codex/skills/.system`, `~/.codex/plugins/cache`,
`/etc/codex/skills`, copies of those skills under user folders and anything with
an OpenAI copyright notice. `apply.py` refuses them. Do not work around it.

## Maintained elsewhere

Skills installed from GitHub, copied from openai/skills or pointing contributors
to another repository are maintained by someone else. Their authors tune the
text across models and `npx skills update` overwrites local edits, so do not
edit them in place. Three options, in order:

1. Keep it. A body is read only when the skill is used, which is the design.
2. Disable it when the scanned sessions never read it: put the exact SKILL.md
   path in the plan's `disable` list. The helper appends a `[[skills.config]]`
   entry to config.toml with a backup.
3. Fork it when it is long and used every day: put it in the plan's `fork` list
   with a new hyphen-case name and the trimmed body. The copy lands under
   `~/.agents/skills/<name>/`, the original stays untouched, and the note in the
   result says to disable the original once the fork is confirmed.

`allow_upstream: true` in the plan overrides the in-place refusal. Use it only
when the user has said so for that skill and understands the update caveat.

## Descriptions

Keep the task trigger and the useful exclusions. Remove repeated instructions,
exhaustive examples and persuasion aimed at other agents. The catalog cuts every
description at 1,024 characters. There is no mandatory length.

## Skill bodies

Keep the frontmatter. Keep in the body what every invocation needs: purpose,
inputs, the core steps and the boundaries. Move mode-specific detail, long
examples, edge cases and reference tables into `references/<topic>.md` and point
to each file from the body with the condition for reading it. Create those files
in the same plan. Never drop a safety boundary; move it only if the body still
says when it applies.

## AGENTS.md

Keep production, secret, publishing and destructive-action boundaries. Keep
domain facts and package commands. Move background into docs the file points to.
Drop instructions written for an older model only after checking their purpose.

## Plan format

```json
{
  "edits": [
    {"path": "/abs/skill/SKILL.md", "sha256": "<from report>", "description": "...", "reason": "..."},
    {"path": "/abs/skill/SKILL.md", "sha256": "<from report>", "body": "...", "reason": "..."},
    {"path": "/abs/project/AGENTS.md", "sha256": "<from report>", "content": "...", "reason": "..."}
  ],
  "create": [{"path": "/abs/skill/references/topic.md", "content": "..."}],
  "fork": [{"path": "/abs/upstream/SKILL.md", "name": "upstream-lite", "body": "...", "reason": "..."}],
  "disable": ["/abs/unused/SKILL.md"],
  "allow_upstream": false
}
```

One field per edit. The same path cannot appear twice in `edits`; change the
description and the body in two plans. `sha256` comes from `skills.inventory` in the report or from
hashing the file. Created files must sit inside a skill folder edited in the same
plan. Preview with `apply.py --plan <plan>`, then `--apply`. The helper backs up,
writes, verifies the bytes and seals the backup; on any failure it rolls back
what it wrote and keeps concurrent edits.

## After

Rerun `xray.py` and say that measured savings appear only in sessions started
after the edit. Report the estimated tokens saved from `apply.py` as an estimate.
