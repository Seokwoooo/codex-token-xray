# Applying changes

## Never touched

Codex native skills: `~/.codex/skills/.system`, `~/.codex/plugins/cache`,
`/etc/codex/skills`, copies of them under user folders and anything with an
OpenAI copyright. Skills made by other people: installed from GitHub, copied
from openai/skills or anthropics/skills, or pointing contributors to another
repository. Their authors keep tuning them and an update would overwrite an
edit. `apply.py` refuses all of these.

## Remove

Unused means no session in this machine's last 90 days of logs read the skill.
It may be used elsewhere or rarely, so show the list and let the user pick. Put
the SKILL.md paths they chose under `remove`. Every file in the folder goes into
the backup, the folder is deleted, and the installer's lock record is dropped.
`restore.py` brings the whole folder back. Skills the user wrote are refused;
those are deleted by hand if ever.

## Trim

Only skills the user wrote. Keep in SKILL.md what every run needs: purpose,
inputs, the core steps and the boundaries. Move mode-specific detail, long
examples, edge cases and reference tables into `references/<topic>.md` and
point to each file from the body with the condition for reading it. Create the
files in the same plan. Never drop a safety boundary. For descriptions keep the
task trigger and the useful exclusions; the catalog cuts at 1,024 characters.

For AGENTS.md keep production, secret, publishing and destructive-action rules
and package commands. Move background into docs the file points to.

## Plan

```json
{
  "edits": [
    {"path": "/abs/skill/SKILL.md", "sha256": "<from report>", "body": "...", "reason": "..."},
    {"path": "/abs/skill/SKILL.md", "sha256": "<from report>", "description": "...", "reason": "..."},
    {"path": "/abs/project/AGENTS.md", "sha256": "<from report>", "content": "...", "reason": "..."}
  ],
  "create": [{"path": "/abs/skill/references/topic.md", "content": "..."}],
  "remove": ["/abs/unused-skill/SKILL.md"]
}
```

One field per edit and one entry per path; body and description of the same
skill go in two plans. `sha256` comes from `skills.inventory` in the report.
Preview first, then `--apply`. The helper backs up, writes, verifies and seals;
on failure it puts back what it wrote.

## After

Rerun `xray.py`. Say that measured savings appear only in sessions started
after the change.
