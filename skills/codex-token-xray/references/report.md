# Reading the report

Write in the user's language. Lead with the measured totals and the biggest
consumer. Keep raw data in the timestamped JSON.

## Measured versus estimated

`usage.measured` comes from `token_usage_record` entries Codex writes after each
model response: input, cached input, net new input (input minus cached), output
and reasoning tokens. These are the numbers the API billed.

Everything under `usage.categories` is estimated from bytes / 4 of the prompt
parts found in the log. `usage.calibration.growth_median` says how well that
estimate tracked the measured growth of the context: 1.0 would be perfect,
0.8 means the estimate runs about 20 percent low. Quote it when you quote
estimates.

`usage.startup` is the measured context size at the first model call of a fresh
session. Its `unattributed` part is the prompt content that never appears in the
log, mostly tool schemas. It is not user-editable.

## Categories

- `tool_output`: text returned by exec, js and other tools. Usually the largest.
  This is behaviour, not a file. Mention once that reading with line ranges and
  `rg` instead of whole files reduces it. Do not turn this into new rules.
- `reasoning`: measured reasoning tokens carried between calls in one window.
- `developer_instructions`: Codex's own injected instructions. `by_tag` shows the
  parts, for example `app-context` from the desktop app. Not editable.
- `base_instructions` and `model_switch`: Codex system prompt. Not editable.
- `skills_catalog`: the skills list Codex renders every session. Its size is capped
  by the skills budget (2 percent of the context window). Trimming descriptions
  lowers it only while the catalog sits under the cap; over the cap it reduces
  truncation instead.
- `skill_body`: SKILL.md text injected by an explicit `$mention`. Implicit reads
  show up under `tool_output` and are attributed in `usage.skills`.
- `agents_md`: AGENTS.md text loaded for the project.

## What can be changed

`trim.candidates.unused`: skills in the catalog that no scanned session read.
`yours` says whether the user wrote it. `trim.candidates.bodies` and
`trim.candidates.descriptions`: the user's own skills over 1,500 estimated body
tokens or 200 description characters. `trim.candidates.agents_md`: loaded
AGENTS.md files over 2,000 tokens. Thresholds are review heuristics.

`protection.kind` is `native` or `editable`; `provenance` is `local`,
`upstream` or `linked-checkout`. Only `local` skills are ever edited.

## Completion record

After a trim, list each candidate as changed, kept with a reason or skipped.
Give the backup path and the restore command. Say plainly that the saving is an
estimate until sessions started after the edit are scanned again.
