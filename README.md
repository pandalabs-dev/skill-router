# skill-router

A Claude Code plugin that names the few installed skills that fit each prompt, so the model loads only what it needs instead of guessing from a trimmed skill list.

## Install

```sh
claude plugin install skill-router --marketplace pandalabs-dev/skill-router --scope user
```

Start a new session afterwards. Needs `python3` 3.8+; standard library only.

## Uninstall

```sh
claude plugin uninstall skill-router@skill-router --scope user
claude plugin marketplace remove skill-router   # uninstall also deletes the index and session state
```

## Token budget

- Silent by default: a hint appears only on a confident match, on about 22% of everyday prompts.
- One line of at most 400 characters and at most 5 names, usually 1.
- Each skill is suggested once per session; session start adds nothing.

## How matching works

- Scores every skill description, name and trigger phrase against the prompt, plus project files (package.json, Cargo.toml, vercel.json and similar).
- Recognizes intent phrased without the keyword: "brute force" means security, "deindexed" means SEO, "this screenshot into React" means image-to-code.
- Platform, language and style skills stay quiet unless the prompt or project shows them; everyday words in skill names don't count as evidence.
- Extra names must add evidence of their own; otherwise only the best match is shown.

## Deep mode

Add "use necessary skills" (or "use relevant skills", "use my skills", "+skills") to a prompt and the router searches wider: about 30 candidates, re-ranked by reading each one's full SKILL.md. It then tells Claude to invoke the skills that fit, up to 5, or says that none match. It runs locally in about 0.1 s, only when asked; "don't use skills" never triggers it. Phrases are configurable as `deep_triggers`.

## Config

Optional `~/.claude/plugins/data/skill-router-skill-router/config.json`, e.g. `{"top_k": 3, "exclude": ["some-skill"]}`. `python3 scripts/build_index.py --list` shows skills by category.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 tests/eval_lib.py -v --holdout
```
