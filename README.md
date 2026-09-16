**English** | [한국어](./README.ko.md)

# codex-token-xray

A skill that shows where your Codex sessions spend their context and trims only what can be trimmed.

## Install

```bash
npx skills add Seokwoooo/codex-token-xray
```

Then type this in Codex.

```text
$codex-token-xray
```

Add `-g` to the install command to use it in every project. Needs Python 3.9 or newer.

## What you see

![Where one Codex session spends its context](docs/where-tokens-go-en.svg)

This is what it found in 12 real sessions on one Mac.

- A session spends about 33,000 tokens before you type anything. Codex app instructions take 12,000 of that, base instructions 5,000 and the skill list 6,000. You cannot trim this part.
- While you work, the results that tools send back take more than half. Every file read in full lands in the context as is.
- One skill's description costs 39 tokens per session. Its body was read 19 times and cost 36,000 tokens. Trimming the body is what pays.

## What it does about it

Three moves and nothing else.

- Removes skills that no session read. The whole folder goes into a zip backup first and one command puts it back.
- Trims your own long skills. What every run needs stays in SKILL.md and the rest moves into `references` files. Nothing is deleted.
- Trims the project's AGENTS.md the same way.

It shows the diff before changing anything and asks once. Skills made by other
people and Codex's own skills and plugins are never edited. Their authors keep
tuning them and an update would overwrite the edit. If you never use one, the
report says so and you can remove it.

## Can you trust the numbers

- Token totals are Codex's own usage records. Nothing is estimated there.
- The split by category is estimated by size. Checked against the real growth it matched at 0.85 and every report prints that ratio.
- Savings show up in sessions started after the trim.

<details>
<summary>More detail</summary>

- Session logs are read from `~/.codex/sessions`. No network.
- Add `--project` to look only at sessions started in the current folder.
- Reports and backups live only under `~/.codex-token-xray`.
- [astra-xray](https://github.com/Seokwoooo/astra-xray) cleans up the skill list and instructions for Astra. This skill measures where tokens go for any model.
- Development: `python3 -m unittest discover -s tests`
- License: MIT

</details>
