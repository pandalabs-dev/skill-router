#!/usr/bin/env python3
"""UserPromptSubmit hook: surface the few skills relevant to this prompt.

Reads the hook JSON on stdin, prints a UserPromptSubmit JSON object with
hookSpecificOutput.additionalContext, or prints nothing. Never fails a prompt:
every error path exits 0 silently.

Budget design: silent by default, capped output (~300 chars), and each skill is
suggested at most once per session.
"""
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import srlib  # noqa: E402

DEFAULTS = {
    "top_k": 2,          # hard max 3
    "min_score": 10.0,   # confidence threshold (BM25-ish units)
    "min_terms": 2,      # distinct non-generic prompt terms that must match
    "max_chars": 300,    # cap for the whole injected text
    "desc_chars": 60,    # description fragment, only when the name is ambiguous
    "exclude": [],
    "debug": False,
}
HEADER = "skill-router hint (load via Skill tool only if relevant): "
K1, B = 1.2, 0.75
SPECIALISATION_PENALTY = 0.6
SECOND_RATIO = 0.8   # a 2nd/3rd skill must score >= 80% of the first pick
FIELD_RATIO = 0.75   # candidates below 75% of the best raw score are never picked
STATE_TTL = 3 * 24 * 3600

CONTINUATION = re.compile(
    r"^\s*(ok(ay)?|k|yes|yep|yeah|no|nope|sure|go( on| ahead)?|continue|proceed|do it|ship it|"
    r"thanks?( you)?|thx|ty|lgtm|nice|great|cool|perfect|done|next|again|retry|try again|"
    r"sounds good|looks good|go for it|keep going|carry on|resume)\b[\s.!?]*$",
    re.I)
FILLER = set("a an the to of in on at for with and or but is are be it this that these those i me my "
             "you your we our pls please just now then so ok okay yes no".split())
BUILD_INTENT = set(srlib.stem(w) for w in (
    "add build create implement fix debug setup set configure scaffold migrate wire integrate "
    "screen route endpoint component page deploy upgrade install refactor broken error crash fails "
    "failing bug".split()))


# ------------------------------------------------------------------ io utils

def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config(ddir):
    cfg = dict(DEFAULTS)
    user = load_json(os.path.join(ddir, "config.json"), {}) or {}
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    try:
        cfg["top_k"] = max(1, min(3, int(cfg["top_k"])))
    except (TypeError, ValueError):
        cfg["top_k"] = DEFAULTS["top_k"]
    return cfg


def load_index(ddir):
    path = os.path.join(ddir, "index.json")
    idx = load_json(path)
    if not idx or idx.get("version") != srlib.INDEX_VERSION:
        import build_index  # first run / format change: build once (~100 ms)
        _, idx = build_index.build(ddir)
    return idx


# ------------------------------------------------------------- prompt gates

def should_skip(prompt):
    p = prompt.strip()
    if not p or p.startswith("/") or p.startswith("!") or p.startswith("#"):
        return True
    if "<command-name>" in p[:300] or "<task-notification>" in p[:300] or "<local-command" in p[:300]:
        return True
    if CONTINUATION.match(p):
        return True
    words = [w for w in re.findall(r"[A-Za-z0-9][\w.+#-]*", p.lower()) if w not in FILLER]
    return len(words) < 4


# ----------------------------------------------------------- project signals

def project_signals(cwd, routes):
    """Return {skill_name: weight} from files in the project directory."""
    if not cwd or not os.path.isdir(cwd):
        return {}
    try:
        names = set(os.listdir(cwd))
    except OSError:
        return {}
    deps = set()
    pkgs = [os.path.join(cwd, "package.json")]
    for sub in ("apps", "packages"):
        if sub in names:
            try:
                pkgs += [os.path.join(cwd, sub, d, "package.json") for d in os.listdir(os.path.join(cwd, sub))[:20]]
            except OSError:
                pass
    for pj in pkgs:
        data = load_json(pj)
        if isinstance(data, dict):
            for k in ("dependencies", "devDependencies", "peerDependencies"):
                deps.update((data.get(k) or {}).keys())
    pytext = ""
    for f in ("pyproject.toml", "requirements.txt", "Pipfile"):
        if f in names:
            pytext += srlib.read_head(os.path.join(cwd, f), 20000).lower()
    out = {}
    for rule in routes.get("rules", []):
        hit = False
        for f in rule.get("files", []):
            if any(ch in f for ch in "*?"):
                rx = re.compile("^" + re.escape(f).replace("\\*", ".*").replace("\\?", ".") + "$")
                if any(rx.match(n) for n in names):
                    hit = True
            elif f.rstrip("/") in names:
                hit = True
        for d in rule.get("deps", []):
            if d.endswith("*"):
                if any(x.startswith(d[:-1]) for x in deps):
                    hit = True
            elif d in deps:
                hit = True
        for w in rule.get("python", []):
            if w in pytext:
                hit = True
        if hit:
            for s in rule.get("skills", []):
                out[s] = max(out.get(s, 0), float(rule.get("weight", 1.0)))
    return out


# ------------------------------------------------------------------ scoring

def score_all(prompt, idx, signals, extra_skills=()):
    qtoks = srlib.tokens(prompt)
    qset = set(qtoks)
    qphrase = " " + " ".join(qtoks) + " "
    qraw = prompt.lower()
    idf = idx["idf"]
    avgdl = idx["avgdl"] or 1.0
    build_intent = bool(qset & BUILD_INTENT)
    results = []
    for s in list(idx["skills"]) + list(extra_skills):
        score = 0.0
        matched = set()
        tf, dl = s["tf"], s["dl"] or 1
        norm = K1 * (1 - B + B * dl / avgdl)
        for t in qset:
            w = idf.get(t, 2.0)
            f = tf.get(t)
            if f:
                score += w * f * (K1 + 1) / (f + norm)
                matched.add(t)
            if t in s["name_toks"]:
                score += 2.2 * w
                matched.add(t)
            elif t in s["kw"]:
                score += 0.6 * w
                matched.add(t)
        name_hit = False
        np_ = s.get("name_phrase") or ""
        if np_ and (" %s " % np_) in qphrase:
            # multi-word names always count; a one-word name only if it is specific
            if " " in np_ or (len(np_) >= 4 and np_ not in srlib.WEAK):
                score += 6.0
                name_hit = True
        elif len(s["base"]) >= 5 and s["base"] in qraw and re.search(
                r"(?<![\w-])%s(?![\w-])" % re.escape(s["base"]), qraw):
            score += 6.0
            name_hit = True
        ph_hits = 0
        for ph in s["phrases"]:
            if (" %s " % ph) in qphrase:
                ph_hits += 1
        score += min(ph_hits, 3) * 3.0
        strong = [t for t in matched if t not in srlib.WEAK and idf.get(t, 2.0) >= 1.5]
        # evidence = specific terms + generic words that are part of the skill's own name
        evidence = len(strong) + len([t for t in matched if t in srlib.WEAK and t in s["name_toks"]])
        sig = signals.get(s["name"]) or signals.get(s["base"])
        # Specialisation check: a skill named after a specific technology/domain
        # ("web3-testing", "terraform-engineer") is only a confident match when
        # that domain word is in the prompt or the project signals it.
        distinct = [t for t in s["name_toks"] if t not in srlib.WEAK and t not in srlib.NAME_GENERIC]
        if distinct and not name_hit and not sig and not (set(distinct) & qset):
            score *= SPECIALISATION_PENALTY
        if sig:
            if strong:
                score *= 1.0 + 0.35 * sig
                score += 2.0 * sig
            elif build_intent and matched:
                score += 2.5 * sig
        if score <= 0:
            continue
        results.append((score, s, evidence, name_hit or ph_hits > 0, sorted(matched)))
    results.sort(key=lambda r: -r[0])
    return results


def pick(results, cfg, skip_names):
    """Confident, unambiguous picks only. Silence is the default outcome."""
    chosen = []
    best = None
    for score, s, evidence, exact, _m in results:
        name = s["dup_of"] or s["name"]
        if name in skip_names or s["name"] in skip_names or name in cfg["exclude"] or s["name"] in cfg["exclude"]:
            continue
        if any(c[1] == name for c in chosen):
            continue
        if best is None:
            best = score
        if score < FIELD_RATIO * best:
            break  # everything below is clearly weaker than the best candidate
        if chosen and score < SECOND_RATIO * chosen[0][0]:
            break
        confident = score >= cfg["min_score"] and (evidence >= cfg["min_terms"] or exact)
        if not confident:
            continue
        chosen.append((score, name, s))
        if len(chosen) >= cfg["top_k"]:
            break
    return chosen


def name_is_ambiguous(s, prompt_toks):
    base_toks = set(srlib.tokens(s["base"].replace("-", " ")))
    return not (base_toks & prompt_toks)


def render(chosen, prompt, cfg, by_name):
    ptoks = set(srlib.tokens(prompt))
    parts = []
    for _, name, s in chosen:
        canon = by_name.get(name, s)
        item = name
        if name_is_ambiguous(canon, ptoks):
            d = canon["desc"]
            d = re.sub(r"^\W*(mandatory prerequisite|critical|important)\W*", "", d, flags=re.I)
            d = re.sub(r"^(Framework \(OSS\)\.\s*)", "", d)
            d = d.replace("**", "").strip()
            if len(d) > cfg["desc_chars"]:
                d = d[: cfg["desc_chars"] - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
            item = "%s (%s)" % (name, d)
        parts.append(item)
    text = HEADER + "; ".join(parts)
    while len(text) > cfg["max_chars"] and len(parts) > 1:
        parts.pop()
        text = HEADER + "; ".join(parts)
    if len(text) > cfg["max_chars"]:
        text = text[: cfg["max_chars"] - 1] + "…"
    return text


# -------------------------------------------------------------------- state

def state_path(ddir, session_id):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:80] or "nosession"
    return os.path.join(ddir, "sessions", sid + ".json")


def prune_states(ddir):
    d = os.path.join(ddir, "sessions")
    try:
        now = time.time()
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if now - os.stat(p).st_mtime > STATE_TTL:
                os.remove(p)
    except OSError:
        pass


_SKILL_CALL = re.compile(r'"name"\s*:\s*"Skill"\s*,\s*"input"\s*:\s*\{\s*"skill"\s*:\s*"([^"]+)"')
_SLASH_CMD = re.compile(r"<command-name>/?([A-Za-z0-9:_-]+)</command-name>")


def scan_transcript(path, state, max_bytes=2000000):
    """Incrementally scan the transcript for skills already invoked (Skill tool or slash)."""
    if not path:
        return
    try:
        size = os.path.getsize(path)
        off = state.get("t_off", 0)
        if off > size:
            off = 0
        if size - off > max_bytes:
            off = size - max_bytes
        if size == off:
            return
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(size - off).decode("utf-8", "replace")
        used = set(state.get("used", []))
        used.update(_SKILL_CALL.findall(chunk))
        used.update(_SLASH_CMD.findall(chunk))
        state["used"] = sorted(used)
        state["t_off"] = size
    except OSError:
        pass


# --------------------------------------------------------------------- main

def project_skills(cwd):
    pdir = os.path.join(cwd or "", ".claude", "skills")
    if not cwd or not os.path.isdir(pdir):
        return []
    import build_index
    out = []
    for n, p in build_index.skill_dir_entries(pdir)[:60]:
        s = build_index.extract({"name": n, "path": p, "source": "project", "plugin": None})
        if s:
            out.append(s)
    return out


def run(payload, ddir=None, persist=True):
    """Return the additionalContext string or None."""
    ddir = ddir or srlib.data_dir()
    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or should_skip(prompt):
        return None
    cfg = load_config(ddir)
    idx = load_index(ddir)
    cwd = payload.get("cwd") or ""
    routes = load_json(os.path.join(os.path.dirname(HERE), "routes.json"), {}) or {}
    signals = project_signals(cwd, routes)
    extra = project_skills(cwd)
    known = {s["name"] for s in idx["skills"]}
    extra = [s for s in extra if s["name"] not in known]
    sp = state_path(ddir, payload.get("session_id"))
    state = (load_json(sp, {}) or {}) if persist else {}
    if persist:
        scan_transcript(payload.get("transcript_path"), state)
    skip = set(state.get("suggested", [])) | set(state.get("used", []))
    results = score_all(prompt, idx, signals, extra)
    chosen = pick(results, cfg, skip)
    if cfg.get("debug"):
        sys.stderr.write("skill-router top: %s\n" % [(round(r[0], 1), r[1]["name"], r[2]) for r in results[:5]])
    text = None
    if chosen:
        by_name = {s["name"]: s for s in idx["skills"]}
        text = render(chosen, prompt, cfg, by_name)
        state["suggested"] = sorted(set(state.get("suggested", [])) | {c[1] for c in chosen})
    if persist and (chosen or "t_off" in state):
        try:
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            tmp = sp + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, sp)
            if chosen and int(time.time()) % 20 == 0:
                prune_states(ddir)
        except OSError:
            pass
    return text


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        text = run(payload)
        if text:
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            }))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
