# skill-router

A Claude Code plugin that names the one or two skills relevant to a prompt, at
the moment the model decides what to do.

## Why

Claude Code trims the skill listing to fit a size budget. With about 280
installed skills, most of them appear as bare names, so the model often misses
the right one or scans and guesses. skill-router keeps an index of every skill
description. A `UserPromptSubmit` hook matches each prompt against that index
and the current project, and adds a short hint naming the likely skills. The
model then loads only the skills it needs. A hook cannot invoke a skill. It can
only surface the right names.

## Budget design: it must save tokens, never add them

- **Silent by default.** Most prompts get nothing. The hint appears only when a
  skill clears a confidence bar: a score threshold, at least two specific
  matching terms (or an exact skill-name or trigger-phrase hit), and a clear
  lead over the other candidates. Skills named after a specific technology,
  such as `terraform-engineer` or `web3-testing`, are penalised unless that
  technology is in the prompt or the project.
- **Capped size.** The hint is one line of at most 300 characters. By default
  it holds 2 skill names (3 at most) and usually bare names. A description
  fragment of 60 characters or fewer is added only when the name shares no
  word with the prompt.
- **Once per session.** A skill is suggested at most once per session. Skills
  the transcript shows were already invoked (Skill tool or slash command) are
  never suggested. The transcript is scanned incrementally from a stored
  offset.
- **No session-start context.** The `SessionStart` hook only rebuilds the index
  when an input changed. It prints nothing.
- **Skips noise:** slash commands, `!`/`#` prompts, continuations ("ok", "go on",
  "do it", "lgtm", emoji), and anything under 4 meaningful words.

On a 40-prompt sample (the test prompts plus everyday chat, git and
follow-up prompts), the hint fires on 30% of prompts. It averages 26 injected
characters per prompt, and the largest single hint is 132 characters. The
hook takes about 35 ms. Run `python3 -m unittest discover -s tests -v` to see
these numbers.

Example of an injected line:

```
skill-router hint (load via Skill tool only if relevant): expo-router
```

## What gets indexed

- User skills: `~/.claude/skills/*/SKILL.md`. Symlinks are followed and read
  only. Folders without `SKILL.md` are skipped.
- Skills and commands of **enabled** plugins (`enabledPlugins` in
  `~/.claude/settings.json`), current install only (`installed_plugins.json`).
  They are named `plugin:skill`, as the Skill tool names them, for example
  `vercel:env-vars` and `superpowers:systematic-debugging`.
- Synced org plugins under `~/.claude/plugins/synced/`.
- Project skills in `<cwd>/.claude/skills`, read at prompt time.
- Skills with `disable-model-invocation: true` are left out.
- Duplicates are collapsed to one entry. These are skills with the same base
  name or near-identical descriptions, for example the `ui-ux-pro-max:*` plugin
  copies of standalone skills, or `vercel:shadcn` and `shadcn`. The standalone
  copy is preferred.

Skills that exist only server-side, such as `anthropic-skills:*` and built-in
skills like `code-review`, have no file on disk and are not indexed.

## Scoring

The score is BM25 over the description tokens, plus:

- boosted name tokens
- trigger keywords from "Triggers on:", "Use when" and "Invoke for" lists
- quoted trigger phrases
- a bonus for an exact skill-name mention

Tokens are stemmed and stopwords are removed. A small alias table maps
spellings such as `postgresql` to `postgres` and `k8s` to `kubernetes`.

Project signals in `routes.json` nudge skills: package.json deps, `*.csproj`,
`Cargo.toml`, `go.mod`, `wp-config.php`, `components.json`,
`playwright.config.*` and similar. A signal never fires a hint alone. It
multiplies a skill that the prompt already matches, or adds a small bonus when
the prompt asks to build or fix something.

## Install

One command:

```sh
claude plugin install skill-router --marketplace pandalabs-dev/skill-router --scope user
```

Or inside Claude Code, add the marketplace and install in two steps:

```
/plugin marketplace add pandalabs-dev/skill-router
/plugin install skill-router@skill-router
```

From a local clone, which picks up edits live:

```sh
claude plugin marketplace add ~/development/skill-router
claude plugin install skill-router@skill-router --scope user
```

Restart Claude Code, or start a new session.

## Files and data

- `hooks/hooks.json`: the `SessionStart` hook (rebuilds the index if stale,
  silent) and the `UserPromptSubmit` hook (`scripts/route.py`).
- `scripts/build_index.py`: the indexer. `--if-stale` rebuilds only when an
  input changed, `--stats` lists the duplicates.
- `scripts/route.py`: the hook. Any error exits 0 with no output, so it never
  blocks a prompt.
- `scripts/srlib.py`: the tokenizer, stemmer and frontmatter parser.
- `routes.json`: the project-file signals.
- `tests/test_route.py`: the stdlib unittest suite. It runs against a real
  index built into a temporary directory.

The index, the per-session state and the optional config live in
`$CLAUDE_PLUGIN_DATA`. When the plugin runs as a hook this is
`~/.claude/plugins/data/skill-router-skill-router/`. Outside a hook it is
`~/.claude/skill-router/`, and `SKILL_ROUTER_DATA` overrides both. Session
state files are pruned after 3 days.

Optional `config.json` in that directory:

```json
{ "top_k": 2, "min_score": 10, "exclude": ["caveman-review"], "debug": false }
```

`top_k` is capped at 3. `debug: true` prints the top candidates to stderr.

Requirements: Python 3.8+ stdlib only. It works with macOS `/usr/bin/python3`
(3.9).
