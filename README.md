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

- Silent by default: a hint appears only on a confident match. On the bundled everyday sample it fires on 22% of prompts.
- One line of at most 400 characters and at most 5 names. An extra name must score at least half of the top hit and match a prompt word tied to its own name or triggers that the top hit does not explain. If it belongs to another topic category, that category's words must also be in the prompt. At most one name per skill family and one per artifact group (PowerPoint vs HTML slides vs Figma Slides). Hints average 1.1 names. Only the first two names may carry a short description, and only when the name shares no word with the prompt.
- Each skill is suggested once per session, and never after the transcript shows it was already used.
- The `SessionStart` hook only rebuilds a stale index and prints nothing.

## How matching works

- A BM25 index over every skill description. It covers user skills, enabled plugins, synced plugins and project `.claude/skills`, plus a static list of built-in skills (`builtin_skills.json`) that may drift from what your build offers.
- Name and trigger-phrase hits, bigrams and a small alias table (`k8s`, `tf`, `gh actions`, `pg`, `tdd`, ...) boost a skill. A rare tech word at the start of a skill's name beats generic skills (`postgres-pro` over `migration`). Capitalized product nouns in a description (Blob, Edge Config) count once the platform is named. Word, Excel, PowerPoint and PDF requests go to the matching format skill. SKILL.md headings and CamelCase API names (`NativeTabs`) add a small, capped weight.
- In "migrate X to Y", Y counts more than X, and a skill named after X is not the match.
- Everyday English words in skill names (`data/common_english.txt`: high, safe, lean, full, ...) never count as rare evidence. Intent lexicons map phrasings to topics: brute force, XSS, leaked keys and similar route to security skills, and meta descriptions, deindexing, backlinks and similar route to the most specific `seo-*` skill. Look-alikes such as "force push" and "index on the orders table" are excluded.
- What follows `into`/`to`/`as` sets the direction: a code target (Next.js, React, HTML, SwiftUI, component, page) means design to code, and Figma/FigJam means code to design. A screenshot, mockup or image turned into code goes to `image-to-code`; naming Figma routes to the Figma skills instead. "NOT for:" clauses in descriptions count against a skill.
- Platform skills (`vercel:*`, `expo-*`/`eas-*`, `figma:*`, `higgsfield-*`, `shopify-expert`) and language skills (`python-pro`, `typescript-pro`, `golang-pro`, ...) stay quiet unless the prompt or the project shows that platform or language. If the prompt names a different language from the project, they stay quiet. Style families such as `caveman-*` fire only when the prompt names them. Uppercase `EXPLAIN`, "query plan" and "seq scan" signal query performance; a lowercase "explain this" does not.
- Prompts under 4 meaningful words fire only on a rare, specific hit. Continuations ("ok", "go on", "do it") never fire.

## Config

Optional `config.json` in the data directory (`~/.claude/plugins/data/skill-router-skill-router/`):

```json
{ "top_k": 5, "min_score": 9, "rel_cutoff": 0.5, "max_chars": 400, "exclude": [], "builtins": "auto", "opt_in_families": ["myteam"], "debug": false }
```

`opt_in_families` adds to the default `caveman` family: `"name"` mutes `name*` skills unless the prompt says `name`. A dict form `{"name": {"skills": [...], "words": [...]}}` is also accepted. `builtins` takes `"auto"`, `true` or `false`. With `"auto"`, the anthropic-skills entries are indexed only when you are logged in with a claude.ai account.

Every skill gets a rule-based topic category (`routes.json`). `python3 scripts/build_index.py --list` prints skills grouped by category. It is for humans only and never runs from a hook.

## Tests

```sh
python3 -m unittest discover -s tests -v   # unit tests plus the labeled eval
python3 tests/eval_lib.py -v --holdout     # eval report: hit rates, budget, latency
```

The eval runs against the skills installed on your machine and skips itself when most labeled skills are missing.
