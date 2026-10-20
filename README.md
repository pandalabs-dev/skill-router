# skill-router

A Claude Code plugin that names the few installed skills that fit each prompt, so the model loads only the skills it needs instead of guessing from a trimmed skill listing.

## Install

```sh
claude plugin install skill-router --marketplace pandalabs-dev/skill-router --scope user
```

Start a new session afterwards. Requires `python3` 3.8+ (macOS system Python works) and uses the standard library only.

## Uninstall

```sh
claude plugin uninstall skill-router@skill-router --scope user
claude plugin marketplace remove skill-router
rm -rf ~/.claude/skill-router   # only exists if you ran the scripts by hand
```

Uninstall deletes the plugin's data directory (index and session state) unless you pass `--keep-data`.

## Token budget

- Silent by default: a hint appears only on a confident match. On the bundled everyday sample it fires on 28% of prompts.
- One line of at most 400 characters and at most 5 names. Extra names must score at least half of the top hit, so most hints carry 1 to 2 names. Only the first two names may carry a short description, and only when the name shares no word with the prompt.
- Each skill is suggested once per session, and never after the transcript shows it was already used.
- The `SessionStart` hook only rebuilds a stale index and prints nothing.

## How matching works

- A BM25 index over every skill description. It covers user skills, enabled plugins, synced plugins and project `.claude/skills`, plus a static list of built-in skills (`builtin_skills.json`) that may drift from what your build offers.
- Name and trigger-phrase hits, bigrams and a small alias table (`k8s`, `tf`, `gh actions`, `pg`, `tdd`, ...) boost a skill. A rare tech word at the start of a skill's name beats generic skills (`postgres-pro` over `migration`).
- Direction words split conversions (`page into Figma` vs `Figma into code`), and "NOT for:" clauses in descriptions count against a skill.
- Platform skills (`vercel:*`, `expo-*`/`eas-*`, `figma:*`, `higgsfield-*`, `shopify-expert`) stay quiet unless the prompt or the project shows that platform. `routes.json` holds the project-file signals.
- Prompts under 4 meaningful words fire only on a rare, specific hit. Continuations ("ok", "go on", "do it") never fire.

## Config

Optional `config.json` in the data directory (`~/.claude/plugins/data/skill-router-skill-router/`):

```json
{ "top_k": 5, "min_score": 9, "rel_cutoff": 0.5, "max_chars": 400, "exclude": [], "builtins": "auto", "debug": false }
```

`builtins` takes `"auto"`, `true` or `false`. With `"auto"`, the anthropic-skills entries are indexed only when you are logged in with a claude.ai account.

## Tests

```sh
python3 -m unittest discover -s tests -v   # unit tests plus the labeled eval
python3 tests/eval_lib.py -v --holdout     # eval report: hit rates, budget, latency
```

The eval runs against the skills installed on your machine and skips itself when most labeled skills are missing.
