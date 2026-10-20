#!/usr/bin/env python3
"""UserPromptSubmit hook: surface the few skills relevant to this prompt.

Reads the hook JSON on stdin, prints a UserPromptSubmit JSON object with
hookSpecificOutput.additionalContext, or prints nothing. Never fails a prompt:
every error path exits 0 silently.

Budget design: silent by default, capped output (<= 400 chars, <= 5 names), and each skill is
suggested at most once per session.
"""
import fnmatch
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
    "top_k": 5,          # hard max 5; extra names must earn their place (rel_cutoff)
    "min_score": 9.0,   # confidence threshold for the first pick (BM25-ish units)
    "min_terms": 2,      # specific prompt terms that must match (or an exact name/phrase hit)
    "rel_cutoff": 0.5,   # extra picks must score >= this fraction of the first pick
    "max_chars": 400,    # cap for the whole injected text
    "desc_chars": 60,    # fragment for the first 1-2 names, only when the name is ambiguous
    "exclude": [],
    "builtins": "auto",  # index builtin_skills.json: "auto" | true | false
    # style/mode skill families suggested only when the prompt names the family
    "opt_in_families": {"caveman": {"skills": ["caveman*", "cavecrew*"],
                                    "words": ["caveman", "cavecrew", "terse", "compress", "compressed",
                                              "fewer tokens", "less tokens", "token-efficient"]}},
    "debug": False,
}
HARD_MAX_K = 5
HEADER = "skill-router hint (load via Skill tool if relevant): "
K1, B = 1.2, 0.75
SPECIALISATION_PENALTY = 0.6   # domain-named skill, domain absent from prompt and project
PLATFORM_PENALTY = 0.5         # platform skill (vercel:*, expo-*, figma:*) without platform evidence
NEGATIVE_PENALTY = 0.45        # prompt hits the skill's "NOT for:" clause
CLASS_WEIGHT = 4.0             # weight of a matching conversion class (@code / @design / @visual)
INTENT_CAT_BONUS = 5.0         # intent lexicon names the skill's topic category
INTENT_WEIGHT = 0.9            # terms implied by an intent lexicon (security, seo)
COMMON_IDF_CAP = 2.2           # idf ceiling for everyday English words (data/common_english.txt)
DEST_WEIGHT = 1.5              # 'migrate X to Y': Y terms
SOURCE_WEIGHT = 0.6            # 'migrate X to Y': X terms
BODY_WEIGHT = 0.35             # SKILL.md headings / API names, well below description terms
BODY_CAP = 2.5                 # total body contribution per skill
FORMAT_BONUS = 12.0            # prompt names the file format a skill produces (Word, Excel, PDF, PowerPoint)
WRONG_DIRECTION_PENALTY = 0.6  # e.g. prompt goes code->Figma, skill goes Figma->code
VERY_RARE_IDF = 99.0  # disabled: common English words are rare in descriptions too
IDENTITY_WINDOW = 0.85         # near-tie window in which exact identity hits are preferred
FIELD_RATIO = 0.9              # first pick must be within 90% of the best raw score (else ambiguous)
STATE_TTL = 3 * 24 * 3600

CONTINUATION = re.compile(
    r"^\s*(ok(ay)?|k|yes|yep|yeah|no|nope|sure|go( on| ahead)?|continue|proceed|do it|ship it|"
    r"thanks?( you)?|thx|ty|lgtm|nice|great|cool|perfect|done|next|again|retry|try again|"
    r"sounds good|looks good|go for it|keep going|carry on|resume)\b[\s.,!?]*"
    r"((go ahead|ship it|do it|thanks?|please|continue)\b[\s.,!?]*)*$",
    re.I)
FILLER = set("a an the to of in on at for with and or but is are be it this that these those i me my "
             "you your we our pls please just now then so ok okay yes no".split())
CONVERSION_VERBS = set(srlib.stem(w) for w in (
    "convert turn translate port migrate export push import move rewrite transform implement "
    "recreate rebuild replicate switch upgrade code".split()))
BUILD_VERBS = set("build make implement recreate replicate clone copy code convert turn rebuild match "
                  "port translate create".split())
BUILD_INTENT = set(srlib.stem(w) for w in (
    "add build create implement fix debug setup set configure scaffold migrate wire integrate "
    "screen route endpoint component page deploy upgrade install refactor broken error crash fails "
    "failing bug submit upload".split()))


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
        cfg["top_k"] = max(1, min(HARD_MAX_K, int(cfg["top_k"])))
    except (TypeError, ValueError):
        cfg["top_k"] = DEFAULTS["top_k"]
    fams = dict(DEFAULTS["opt_in_families"])
    extra = user.get("opt_in_families") if isinstance(user, dict) else None
    if isinstance(extra, list):
        for f in extra:
            if isinstance(f, str) and f not in fams:
                fams[f] = {"skills": [f + "*"], "words": [f]}
    elif isinstance(extra, dict):
        fams.update({k: v for k, v in extra.items() if isinstance(v, dict)})
    cfg["opt_in_families"] = fams
    try:
        cfg["max_chars"] = max(120, min(600, int(cfg["max_chars"])))
    except (TypeError, ValueError):
        cfg["max_chars"] = DEFAULTS["max_chars"]
    return cfg


def load_index(ddir):
    path = os.path.join(ddir, "index.json")
    idx = load_json(path)
    if not idx or idx.get("version") != srlib.INDEX_VERSION:
        import build_index  # first run / format change: build once (~100 ms)
        _, idx = build_index.build(ddir)
    return idx


# ------------------------------------------------------------- prompt gates

def prompt_size(prompt):
    """Number of meaningful words (filler removed)."""
    return len([w for w in re.findall(r"[A-Za-z0-9][\w.+#-]*", prompt.lower()) if w not in FILLER])


def should_skip(prompt):
    """Hard skips: commands, continuations, one-word prompts. Short prompts are
    not skipped here; they must clear a stricter bar in pick()."""
    p = prompt.strip()
    if not p or p.startswith("/") or p.startswith("!") or p.startswith("#"):
        return True
    if "<command-name>" in p[:300] or "<task-notification>" in p[:300] or "<local-command" in p[:300]:
        return True
    if CONTINUATION.match(p):
        return True
    return prompt_size(p) < 2


# ----------------------------------------------------------- project signals

def _names_match(names, pattern):
    if any(ch in pattern for ch in "*?"):
        rx = re.compile("^" + re.escape(pattern).replace("\\*", ".*").replace("\\?", ".") + "$")
        return any(rx.match(n) for n in names)
    return pattern.rstrip("/") in names


def project_signals(cwd, routes):
    """Return ({skill_name: weight}, {platform_name}) from files in the project directory."""
    if not cwd or not os.path.isdir(cwd):
        return {}, set()
    try:
        names = set(os.listdir(cwd))
    except OSError:
        return {}, set()
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

    def hit(rule):
        if any(_names_match(names, f) for f in rule.get("files", [])):
            return True
        for d in rule.get("deps", []):
            if (d.endswith("*") and any(x.startswith(d[:-1]) for x in deps)) or d in deps:
                return True
        return any(w in pytext for w in rule.get("python", []))

    out = {}
    for rule in routes.get("rules", []):
        if hit(rule):
            for s in rule.get("skills", []):
                out[s] = max(out.get(s, 0), float(rule.get("weight", 1.0)))
    plats = set(p for p, spec in (routes.get("platforms") or {}).items() if hit(spec))
    return out, plats


def prompt_platforms(qraw_tokens, qtext, routes):
    plats = set()
    for p, spec in (routes.get("platforms") or {}).items():
        for w in spec.get("prompt", []):
            if ((" " in w or "." in w) and w in qtext) or w in qraw_tokens:
                plats.add(p)
                break
    return plats


def resolve_platforms(project, prompt_side, routes):
    """Platforms in force. A language counts from the project only when the
    prompt names no other language (contradictory evidence -> stay silent)."""
    specs = routes.get("platforms") or {}
    langs = set(p for p, sp in specs.items() if sp.get("kind") == "language")
    if prompt_side & langs:
        return (project - langs) | prompt_side
    return project | prompt_side


def muted_names(skills, prompt_side, routes, cfg, qtext, qraw):
    """Skill names muted for this prompt: opt-in families whose word is absent,
    and skills a named platform supersedes (Figma named -> image-to-code muted)."""
    pats = []
    for fam, spec in (cfg.get("opt_in_families") or {}).items():
        words = spec.get("words") or [fam]
        if not any(((" " in w) and w in qtext) or w in qraw for w in words):
            pats += spec.get("skills") or [fam + "*"]
    for p in prompt_side:
        pats += (routes.get("platforms") or {}).get(p, {}).get("suppresses", [])
    if not pats:
        return set()
    return set(s["name"] for s in skills
               if any(fnmatch.fnmatchcase(s["name"], pt) or fnmatch.fnmatchcase(s["base"], pt) for pt in pats))


# ------------------------------------------------------------------ scoring

class Query(object):
    def __init__(self, prompt, idx):
        prompt = srlib.rewrite(prompt)
        self.text = prompt.lower()
        self.toks = srlib.tokens(prompt)
        self.set = set(self.toks)
        self.exp = set(srlib.expand(self.toks, prompt)) - self.set
        self.iexp = set(srlib.intent_terms(prompt)) - self.set   # topic implied by phrasing
        self.icats = srlib.intent_cats(prompt)
        self.all = self.set | self.exp | self.iexp
        # parts of a compound ('react', 'native' of 'react-native') count at reduced weight
        self.parts = srlib.compound_parts(prompt) - set(srlib.tokens(re.sub(r"\S*[-./]\S*", " ", prompt)))
        self.raw = set(srlib.raw_tokens(prompt))
        self.phrase = " " + " ".join(self.toks) + " "
        self.bigrams = srlib.bigrams(self.toks)
        self.dir = srlib.directional(prompt)
        self.compounds = set(m.lower() for m in re.findall(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b", prompt))
        self.conversion = bool(CONVERSION_VERBS & set(self.toks))
        raw = set(srlib.raw_tokens(prompt))
        if raw & srlib.VISUAL_SOURCES and raw & srlib.CODE_TARGETS and raw & BUILD_VERBS:
            # 'build the page from this screenshot', 'make the site look like this picture'
            self.conversion = True
            self.dir["from:@visual"] = self.dir.get("from:@visual", 0) + 1
            self.dir["to:@code"] = self.dir.get("to:@code", 0) + 1
        if not self.conversion:
            # 'add a toggle to the settings page' is not a conversion: keep plain
            # direction features, drop conversion classes and source/target weighting
            self.dir = dict((k, v) for k, v in self.dir.items() if "@" not in k)
        if "to:@code" in self.dir and "code" not in self.set:
            self.exp.add("code")
        self.formats = srlib.format_intents(prompt)
        # picture -> code: the target stack (React, Next.js) is a parameter of the conversion
        self.visual_to_code = "from:@visual" in self.dir and "to:@code" in self.dir
        # migration/conversion: what you move TO matters more than what you move FROM
        if self.conversion:
            self.to_toks = set(d[3:] for d in self.dir if d.startswith("to:") and "@" not in d)
            self.from_toks = set(d[5:] for d in self.dir if d.startswith("from:") and "@" not in d) - self.to_toks
            # parts of a source compound ('react', 'native' of 'react-native') are source too
            self.from_toks |= set(t for t in self.parts if any(t in f and t != f for f in self.from_toks))
            self.from_toks -= self.to_toks
        else:
            self.to_toks = self.from_toks = set()
        xphrase = self.phrase + " ".join(sorted(self.exp | self.iexp)) + " "
        self.cats = set(c for c, words in (idx.get("cat_intents") or {}).items()
                        if any((" %s " % w) in xphrase for w in words))
        idf = idx["idf"]
        # rare = specific vocabulary (few skills mention it)
        self.common = set(idx.get("common") or ())
        self.rare = set(t for t in self.set if t not in srlib.WEAK and idf.get(t, 0) >= 3.2 and t not in self.common)


def score_skill(q, s, idx, signals, platforms, build_intent):
    # fast exit: no lexical contact and no topic/format/conversion route to this skill
    tf = s["tf"]
    if (not any(t in tf for t in q.all) and not q.all.intersection(s["name_toks"])
            and not q.all.intersection(s.get("kw", ())) and not any((" %s " % t) in s.get("body", "") for t in q.all)
            and s.get("category") not in q.icats and not (q.formats and s.get("formats"))
            and not s.get("conv") and not q.dir):
        return 0.0, 0, False, False, [], (False, False, False)
    idf = idx["idf"]
    avgdl = idx["avgdl"] or 1.0
    score = 0.0
    matched = set()
    tf, dl = s["tf"], s["dl"] or 1
    norm = K1 * (1 - B + B * dl / avgdl)
    name_toks = s["name_toks"]
    common = q.common
    proper = set(t for ph in s.get("products", ()) for t in ph.split())
    cname = set(s.get("common_name", ()))
    rare_name = False
    body = s.get("body") or ""
    bidf = idx.get("bidf") or {}
    body_hit = set()
    for t, weight in ([(t, 0.4 if t in q.parts else 1.0) for t in q.set] + [(t, 0.5) for t in q.exp - q.iexp]
                      + [(t, INTENT_WEIGHT) for t in q.iexp]):
        if t in q.to_toks:
            weight *= 1.0 if q.visual_to_code else DEST_WEIGHT
        elif t in q.from_toks:
            weight *= SOURCE_WEIGHT
        elif q.conversion and t in CONVERSION_VERBS:
            weight *= 0.4   # 'migrate'/'convert' says a conversion happens, not what it is about
        w = idf.get(t, 2.0)
        if t in common and t not in proper:
            w = min(w, COMMON_IDF_CAP)   # everyday words are not specific, however rare in descriptions
        w *= weight
        if w >= VERY_RARE_IDF:
            w *= 1.5   # a term only one or two skills mention is a strong discriminator
        f = tf.get(t)
        hitf = False
        if f:
            score += w * f * (K1 + 1) / (f + norm)
            hitf = True
        if t in name_toks:
            score += 2.2 * w
            if (t in q.rare and t == s.get("first") and t not in cname and t not in q.from_toks
                    and not (q.visual_to_code and t in q.to_toks)):
                # specific beats generic: a rare tech word that leads the skill's name
                score += 1.5 * w
                rare_name = True
            hitf = True
        elif t in s["kw"]:
            score += 0.6 * w
            if t in q.rare and len(s["kw"]) <= 30:
                score += 0.8 * w   # rare word from a short, explicit trigger list
            hitf = True
        if not hitf and t not in srlib.WEAK and (" %s " % t) in body:
            body_hit.add(t)
        if hitf:
            matched.add(t)
    if body_hit:
        # SKILL.md headings / API names: low weight, capped, never dominant
        score += min(BODY_CAP, sum(BODY_WEIGHT * bidf.get(t, 2.0) for t in body_hit))
    name_hit = multi_hit = False
    np_ = s.get("name_phrase") or ""
    if np_ and (" %s " % np_) in q.phrase:
        if " " in np_:
            score += 6.0
            name_hit = multi_hit = True
        elif (len(np_) >= 4 and np_ not in srlib.WEAK and np_ not in cname
              and not (q.visual_to_code and np_ in q.to_toks)):
            score += min(6.0, 1.2 * idf.get(np_, 2.0))  # one-word names: scaled by specificity
            name_hit = True
    elif len(s["base"]) >= 5 and s["base"] in q.text and re.search(
            r"(?<![\w-])%s(?![\w-])" % re.escape(s["base"]), q.text):
        score += 6.0
        name_hit = multi_hit = "-" in s["base"]
        name_hit = True
    if not name_hit and q.compounds and any((s["base"].startswith(c + "-") or s["base"] == c)
                                            and not set(srlib.tokens(c)) <= q.from_toks for c in q.compounds):
        score += 6.0   # the prompt's hyphenated compound leads the skill's name ('high-end' -> high-end-visual-design)
        name_hit = multi_hit = True
    if not name_hit and len(name_toks) >= 2 and all(t in q.set for t in name_toks):
        score += 6.0   # every word of the skill's name is in the prompt, just not adjacent
        name_hit = True
    ph_hits = sum(1 for ph in s["phrases"] if (" %s " % ph) in q.phrase
                  and not (q.from_toks and set(ph.split()) <= q.from_toks))   # source-side phrases don't count
    score += min(ph_hits, 3) * 3.0
    plat = s.get("platform")
    plat_ok = plat is None or plat in platforms
    if plat_ok:
        # product nouns from the description (Blob, Edge Config, Queues) once the platform is named
        generic = srlib.WEAK | set(srlib.tokens(" ".join(filter(None, (s.get("plugin"), plat, s.get("family"))))))
        for ph in s.get("products", ()):
            if set(ph.split()) <= (generic | common):
                continue   # 'Figma Design' is the platform plus a generic word, not a product
            if q.from_toks and set(ph.split()) <= q.from_toks:
                continue   # the thing being migrated away from
            if (" %s " % ph) in q.phrase and (" " in ph or idf.get(ph, 0) >= 3.0):
                matched.update(ph.split())
                if plat:
                    score += 2.2 * idf.get(ph.split()[0], 3.0)
                    rare_name = True   # platform product named once the platform is in force (Vercel Blob)
                else:
                    score += 1.0 * idf.get(ph.split()[0], 3.0)   # a product the skill mentions in passing
                break
    if s.get("category") in q.icats or (q.icats & {"security"} and s.get("category") == "web3"
                                        and "secur" in s["name_toks"]):
        score += INTENT_CAT_BONUS   # the prompt's phrasing names this topic (brute force -> security)
    if q.formats and q.formats.intersection(s.get("formats", ())):
        score += FORMAT_BONUS   # the prompt asks for exactly this file format
        rare_name = True
    sbg = s.get("bigrams") or ""
    bg = sum(1 for b in q.bigrams if ("|%s|" % b) in sbg)
    score += min(bg, 2) * 2.0
    # direction: 'page into Figma' vs 'Figma into code'
    sdir = s.get("dir") or {}
    wrong_dir = False
    dir_ev = False
    for d in q.dir:
        kind, x = d.split(":", 1)
        cls = x.startswith("@")
        if not cls and (x in srlib.WEAK or idf.get(x, 0) < 2.0):
            continue
        opp = ("from:" if kind == "to" else "to:") + x
        a, b = sdir.get(d, 0), sdir.get(opp, 0)
        if a or b:
            aff = (a - b) / float(a + b + 1)
            score += 2.5 * aff * (CLASS_WEIGHT if cls else idf.get(x, 2.0)) / 2.0
            if aff < -0.3:
                wrong_dir = True
            elif cls and aff >= 0.3:
                dir_ev = True
    conv = s.get("conv")
    if conv and any("from:" + c in q.dir for c in conv[0]) and any("to:" + c in q.dir for c in conv[1]):
        score += 3 * CLASS_WEIGHT   # the skill's name is this exact conversion (image-to-code)
        rare_name = True
        dir_ev = True
    if wrong_dir:
        score *= WRONG_DIRECTION_PENALTY
    if q.set & set(s.get("neg", ())) and not name_hit:
        score *= NEGATIVE_PENALTY
    strong = [t for t in matched if t not in srlib.WEAK and idf.get(t, 2.0) >= 1.5]
    evidence = len(strong) + len([t for t in matched if t in srlib.WEAK and t in name_toks])
    if bg:
        evidence += 1
    if dir_ev:
        evidence += 1
    sig = signals.get(s["name"]) or signals.get(s["base"])
    distinct = [t for t in name_toks if t not in srlib.WEAK and t not in srlib.NAME_GENERIC]
    qall = q.set | q.exp | q.iexp
    fam = s.get("family")
    if not name_hit and not sig:
        # the word that sets a skill apart from its siblings (figma-implement-MOTION,
        # seo-HREFLANG) must show up; the shared family/plugin word does not count
        shared = {fam, s.get("plugin"), plat}
        core = [t for t in distinct if t not in shared]
        # in 'migrate X to Y' a skill named after X (the thing being left) is not the match
        qeff = qall - q.from_toks
        specific = [t for t in core if t not in cname]
        if specific:
            core_ok = bool(set(specific) & qeff)
        else:   # every distinguishing word is everyday English: all of them must be there
            core_ok = bool(core) and set(core) <= qeff
        if core and not core_ok:
            score *= SPECIALISATION_PENALTY
        elif fam and fam not in qall and not (plat and plat_ok):
            score *= SPECIALISATION_PENALTY   # seo-*, caveman-* ... family word absent
    gated = not plat_ok and not multi_hit
    if gated:
        score *= PLATFORM_PENALTY
    if sig and plat_ok:
        if strong and build_intent:
            score *= 1.0 + 0.5 * sig
            score += 3.0 * sig
            evidence += 1      # building in this stack: the project itself is evidence
        elif strong:
            score *= 1.0 + 0.35 * sig
            score += 2.0 * sig
        elif build_intent and matched:
            score += 2.5 * sig
    rare_hit = bool(q.rare & matched) or name_hit
    # an identity hit outside the topic the prompt's phrasing names (SQL injection -> security,
    # not sql-pro) does not jump the queue
    ident = (name_hit or rare_name) and not (q.icats and s.get("category") not in q.icats
                                             and not (s.get("category") == "web3" and "security" in q.icats))
    return score, evidence, name_hit or ph_hits > 0 or rare_name, rare_hit, sorted(matched), (gated, wrong_dir, ident)


def score_all(prompt, idx, signals, extra_skills=(), platforms=frozenset(), muted=frozenset(), q=None):
    q = q or Query(prompt, idx)
    build_intent = bool(q.set & BUILD_INTENT)
    results = []
    for s in list(idx["skills"]) + list(extra_skills):
        if s["name"] in muted:
            continue
        score, evidence, exact, rare_hit, matched, flags = score_skill(q, s, idx, signals, platforms, build_intent)
        if score > 0:
            results.append((score, s, evidence, exact, matched, rare_hit, flags))
    results.sort(key=lambda r: -r[0])
    return results


def pick(results, cfg, skip_names, short=False, q=None):
    """Confident picks only; silence is the default outcome.

    First pick: score >= min_score, enough specific evidence, and within
    FIELD_RATIO of the best raw candidate. Extra picks (up to top_k): score >=
    rel_cutoff x first pick and at least one specific term. Short prompts
    (< 4 meaningful words) need a rare, specific hit and a higher score."""
    chosen = []
    first_matched = set()
    blockers = []   # higher-scoring candidates that were not confident
    # identity beats description: among near-tied leaders, an exact name/phrase/format hit goes first
    if results:
        top_score = results[0][0]
        lead = [r for r in results if r[0] >= IDENTITY_WINDOW * top_score]
        rest = results[len(lead):]
        results = sorted(lead, key=lambda r: (not r[6][2], -r[0])) + rest
    min_score = cfg["min_score"] * (1.3 if short else 1.0)
    for score, s, evidence, exact, matched, rare_hit, (gated, wrong_dir, _ident) in results:
        name = s["dup_of"] or s["name"]
        if name in skip_names or s["name"] in skip_names or name in cfg["exclude"] or s["name"] in cfg["exclude"]:
            continue
        if any(c[1] == name for c in chosen):
            continue
        if gated:
            continue   # platform skill, but neither prompt nor project shows the platform
        if not chosen:
            ok = score >= min_score and (evidence >= cfg["min_terms"] or exact)
            if short:
                ok = ok and rare_hit
            # ambiguity: an unrelated, higher-scoring but unconfident candidate means silence
            if ok and not exact and any(b[0] * FIELD_RATIO > score and group(b[1]) != group(s) for b in blockers):
                ok = False
            if ok:
                chosen.append((score, name, s))
                first_matched = set(matched)
            else:
                blockers.append((score, s))
                if len(blockers) > 4:
                    break
            continue
        if score < cfg["rel_cutoff"] * chosen[0][0]:
            break
        # an extra name must be confident, point the same way, and match something
        # the first pick did not (siblings matching the same words add nothing)
        if ((evidence >= 2 or exact) and score >= 0.6 * cfg["min_score"] and not wrong_dir
                and not set(matched) <= first_matched and extra_allowed(s, matched, chosen, first_matched, q)):
            chosen.append((score, name, s))
        if len(chosen) >= cfg["top_k"]:
            break
    return chosen


ALTERNATIVES = []


def extra_allowed(s, matched, chosen, first_matched, q):
    """Gates for a 2nd..5th name.
    - category intent: an extra from another topic category than the top hit needs that
      category's own words in the prompt (seo-images needs seo/ranking/meta/sitemap/alt text);
    - one name per family: a sibling of a chosen skill needs evidence of its own, i.e. a
      specific word tied to its identity (name, trigger keywords) that the top hit lacks;
    - a named file format admits only skills that produce that format."""
    top = chosen[0][2]
    cats = q.cats if q is not None else set()
    src = q.from_toks if q is not None else set()
    names = set(c[1] for c in chosen) | set(c[2]["name"] for c in chosen)
    for alt in ALTERNATIVES:
        if (s["name"] in alt or (s.get("dup_of") or "") in alt) and names & alt:
            return False   # same artifact, the better-scoring alternative is already in
    if set(matched) - src <= first_matched:
        return False
    if s.get("category") and top.get("category") and s["category"] != top["category"] and s["category"] not in cats:
        return False
    # every extra (same family, same category or not) needs its own specific evidence:
    # a prompt word the top hit does not explain, tied to the extra's name or triggers,
    # that is not just the shared topic word an intent lexicon or expansion added
    shared = src | (q.exp | q.iexp if q is not None else set())
    if q is not None:
        shared |= q.to_toks if top.get("conv") else set()
    own = (set(matched) - first_matched - shared) & (set(s["name_toks"]) | set(s.get("kw", ())))
    own = [t for t in own if t not in srlib.WEAK and t not in set(s.get("common_name", ()))]
    if not own:
        return False
    if q is not None and q.formats and s.get("category") == "office-docs" and not q.formats.intersection(s.get("formats", ())):
        return False
    return True


def group(s):
    return s.get("platform") or s.get("family") or s.get("plugin") or s["name"]


def name_is_ambiguous(s, prompt_toks):
    base_toks = set(s["name_toks"])
    return not (base_toks & prompt_toks)


def render(chosen, prompt, cfg, by_name):
    ptoks = set(srlib.tokens(prompt))
    parts = []
    for i, (_, name, s) in enumerate(chosen):
        canon = by_name.get(name, s)
        item = name
        if i < 2 and name_is_ambiguous(canon, ptoks):
            d = canon["desc"]
            d = re.sub(r"^\W*(mandatory prerequisite|critical|important)\W*", "", d, flags=re.I)
            d = re.sub(r"^(Framework \(OSS\)\.\s*|EAS service \(paid\)\.\s*)", "", d)
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
    try:
        if os.path.realpath(pdir) == os.path.realpath(os.path.join(srlib.claude_dir(), "skills")):
            return []   # cwd is the home dir: these are the user skills, already indexed
    except OSError:
        return []
    import build_index
    out = []
    for n, p in build_index.skill_dir_entries(pdir)[:60]:
        s = build_index.extract({"name": n, "path": p, "source": "project", "plugin": None})
        if s:
            s.pop("_low", None)
            out.append(build_index.compact(s))
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
    ALTERNATIVES[:] = [set(a) for a in routes.get("alternatives", [])]
    signals, project_plats = project_signals(cwd, routes)
    rprompt = srlib.rewrite(prompt)
    qraw = set(srlib.raw_tokens(rprompt))
    prompt_plats = prompt_platforms(qraw, rprompt.lower(), routes)
    platforms = resolve_platforms(project_plats, prompt_plats, routes)
    muted = muted_names(idx["skills"], prompt_plats, routes, cfg, rprompt.lower(), qraw)
    extra = project_skills(cwd)
    known = {s["name"] for s in idx["skills"]}
    extra = [s for s in extra if s["name"] not in known]
    sp = state_path(ddir, payload.get("session_id"))
    state = (load_json(sp, {}) or {}) if persist else {}
    if persist:
        scan_transcript(payload.get("transcript_path"), state)
    skip = set(state.get("suggested", [])) | set(state.get("used", []))
    q = Query(prompt, idx)
    results = score_all(prompt, idx, signals, extra, platforms, muted, q)
    chosen = pick(results, cfg, skip, short=prompt_size(prompt) < 4, q=q)
    if cfg.get("debug"):
        sys.stderr.write("skill-router top: %s\n" % [(round(r[0], 1), r[1]["name"], r[2], r[4]) for r in results[:6]])
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
