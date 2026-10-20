#!/usr/bin/env python3
"""Build the skill-router index from every skill Claude Code can see.

Usage:
  build_index.py            rebuild unconditionally
  build_index.py --if-stale rebuild only when a source changed (SessionStart)
  build_index.py --stats    rebuild and print a summary
Never prints anything in --if-stale mode (SessionStart must stay silent).
"""
import fnmatch
import hashlib
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import srlib  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE = srlib.claude_dir()
USER_SKILLS = os.path.join(CLAUDE, "skills")
PLUGINS = os.path.join(CLAUDE, "plugins")
INSTALLED = os.path.join(PLUGINS, "installed_plugins.json")
SETTINGS = os.path.join(CLAUDE, "settings.json")


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ discovery

def enabled_plugins():
    """[(plugin_name, install_path)] for enabled plugins, current user-scope install only."""
    settings = load_json(SETTINGS, {}) or {}
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}
    installed = (load_json(INSTALLED, {}) or {}).get("plugins", {})
    out = []
    for pid in sorted(enabled):
        entries = installed.get(pid) or []
        user = [e for e in entries if e.get("scope") == "user"]
        pick = user or [e for e in entries if e.get("scope") in ("project", "local")]
        if not pick:
            continue
        # latest by lastUpdated among the chosen scope
        e = sorted(pick, key=lambda x: x.get("lastUpdated") or "")[-1]
        path = e.get("installPath")
        if path and os.path.isdir(path):
            out.append((pid.split("@")[0], path))
    return out


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def plugin_skill_dirs(name, root):
    manifest = load_json(os.path.join(root, ".claude-plugin", "plugin.json"), {}) or {}
    dirs = []
    declared = _as_list(manifest.get("skills"))
    for d in declared or ["skills"]:
        dirs.append(os.path.normpath(os.path.join(root, d)))
    if declared and os.path.isdir(os.path.join(root, "skills")):
        dirs.append(os.path.join(root, "skills"))  # default dir is additive
    # plugins that sync skills to ~/.claude/plugins/<name>/skills (e.g. mcpmarket-me)
    synced = os.path.join(PLUGINS, name, "skills")
    if os.path.isdir(synced):
        dirs.append(synced)
    cmds = []
    declared_cmds = _as_list(manifest.get("commands"))
    if declared_cmds:
        for c in declared_cmds:
            if isinstance(c, str):
                cmds.append(os.path.normpath(os.path.join(root, c)))
    else:
        cdir = os.path.join(root, "commands")
        if os.path.isdir(cdir):
            cmds += [os.path.join(cdir, f) for f in sorted(os.listdir(cdir)) if f.endswith(".md")]
    seen, uniq = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            uniq.append(d)
    return uniq, cmds


def synced_plugins():
    """Org/app-synced plugins under ~/.claude/plugins/synced/<id>/<name>/skills."""
    base = os.path.join(PLUGINS, "synced")
    out = []
    if not os.path.isdir(base):
        return out
    for acct in sorted(os.listdir(base)):
        man = load_json(os.path.join(base, acct, "manifest.json"), {}) or {}
        for p in man.get("plugins") or []:
            n = p.get("name")
            if n and os.path.isdir(os.path.join(base, acct, n, "skills")):
                out.append((n, os.path.join(base, acct, n, "skills")))
    return out


def skill_dir_entries(skills_dir):
    try:
        names = sorted(os.listdir(skills_dir))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(skills_dir, n, "SKILL.md")
        if os.path.isfile(p):
            out.append((n, p))
    return out


def discover(cwd=None):
    """Yield dicts {name, path, source, plugin}."""
    found = []
    for n, p in skill_dir_entries(USER_SKILLS):
        found.append({"name": n, "path": p, "source": "user", "plugin": None})
    for pname, root in enabled_plugins():
        dirs, cmds = plugin_skill_dirs(pname, root)
        for d in dirs:
            for n, p in skill_dir_entries(d):
                found.append({"name": "%s:%s" % (pname, n), "path": p, "source": "plugin", "plugin": pname})
        for c in cmds:
            if os.path.isfile(c):
                n = os.path.splitext(os.path.basename(c))[0]
                found.append({"name": "%s:%s" % (pname, n), "path": c, "source": "command", "plugin": pname})
    for pname, d in synced_plugins():
        for n, p in skill_dir_entries(d):
            found.append({"name": "%s:%s" % (pname, n), "path": p, "source": "plugin", "plugin": pname})
    if cwd:
        for n, p in skill_dir_entries(os.path.join(cwd, ".claude", "skills")):
            found.append({"name": n, "path": p, "source": "project", "plugin": None})
    # same name from two places: first wins (user > plugin)
    seen, uniq = set(), []
    for f in found:
        if f["name"] not in seen:
            seen.add(f["name"])
            uniq.append(f)
    return uniq


# --------------------------------------------------------------- extraction

_QUOTED = re.compile(r"[\"'‘“]([^\"'’”]{3,60})[\"'’”]")
_LIST_HEADS = re.compile(
    r"(?:triggers?(?: on)?|keywords?|invoke for|use (?:it )?for|topics?|elements?|styles?|actions?|projects?)\s*:\s*([^.\n]{3,600})",
    re.I)
_USE_WHEN = re.compile(r"(?:use (?:this skill )?when|use for|invoke for)\s+([^.\n]{3,300})", re.I)


def extract(entry):
    text = srlib.read_head(entry["path"], 60000)
    fm = srlib.parse_frontmatter(text)
    if str(fm.get("disable-model-invocation", "")).lower() == "true":
        return None
    desc = re.sub(r"\s+", " ", fm.get("description", "")).strip()
    if not desc:
        # commands/skills without frontmatter description: first prose line
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if len(line) > 20:
                desc = line[:300]
                break
    if not desc:
        return None
    s = from_description(entry, desc)
    body = text.split("---", 2)[-1] if text.startswith("---") else text
    s["body"] = sorted(set(srlib.body_terms(body)) - set(s["tf"]) - set(s["name_toks"]))
    return s


def from_description(entry, desc):
    base = entry["name"].split(":", 1)[-1]
    pos, neg = srlib.split_negative(desc)
    phrases = set()
    for m in _QUOTED.finditer(pos):
        ph = srlib.norm_phrase(m.group(1))
        if len(ph.split()) >= 2:
            phrases.add(ph)
    kw = []
    for m in _LIST_HEADS.finditer(pos):
        for item in re.split(r",|;|\bor\b|/", m.group(1)):
            ph = srlib.norm_phrase(item)
            if not ph:
                continue
            if 2 <= len(ph.split()) <= 4:
                phrases.add(ph)
            kw.extend(ph.split())
    for m in _USE_WHEN.finditer(pos):
        kw.extend(srlib.tokens(m.group(1)))
    name_toks = srlib.name_tokens(base.replace("-", " ").replace("_", " "))
    desc_toks = srlib.tokens(pos)
    tf = {}
    for t in desc_toks:
        tf[t] = tf.get(t, 0) + 1
    neg_toks = set(srlib.tokens(neg)) - set(tf) - set(name_toks)
    return {
        "name": entry["name"],
        "base": base,
        "plugin": entry["plugin"],
        "source": entry["source"],
        "path": entry["path"],
        "desc": desc,
        "name_toks": sorted(set(name_toks)),
        "name_phrase": " ".join(name_toks),
        "kw": sorted(set(kw) - set(name_toks)),
        "phrases": sorted(phrases),
        "bigrams": sorted(srlib.bigrams(desc_toks)),
        "dir": srlib.directional(pos),
        "conv": srlib.name_conversion(base),
        "neg": sorted(neg_toks),
        "products": products(pos),
        "body": [],
        "formats": [],
        "tf": tf,
        "dl": len(desc_toks),
        "platform": None,
        "dup_of": None,
    }


_PRODUCT = re.compile(r"(?<=[\w,;(/\u2014-] )([A-Z][A-Za-z0-9]+(?: [A-Z][A-Za-z0-9]+)?)")


def products(desc):
    """Capitalized product nouns mid-sentence (Blob, Edge Config, Queues, NativeTabs)."""
    out = set()
    for m in _PRODUCT.findall(desc):
        ph = srlib.norm_phrase(m)
        if ph and ph not in srlib.WEAK and len(ph) >= 2:
            out.add(ph)
    return sorted(out)


# ------------------------------------------------------------ builtins

def has_claude_ai_login():
    """anthropic-skills:* are served to claude.ai-account logins (key presence only)."""
    for p in (os.path.join(CLAUDE, ".claude.json"), os.path.join(srlib.HOME, ".claude.json")):
        data = load_json(p)
        if isinstance(data, dict):
            return "oauthAccount" in data
    return False


def builtin_entries(mode="auto"):
    """Skills the Skill tool exposes with no file on disk (static list, may drift)."""
    if mode is False or mode == "off":
        return []
    data = load_json(os.path.join(ROOT, "builtin_skills.json"), {}) or {}
    login = mode is True or mode == "on" or has_claude_ai_login()
    out = []
    for group in data.get("groups", []):
        if group.get("requires") == "claude-ai-login" and not login:
            continue
        for item in group.get("skills", []):
            if item.get("name") and item.get("description"):
                out.append((item["name"], item["description"], item.get("formats") or []))
    return out


def tag_categories(skills):
    """Rule-based topic category per skill (routes.json 'categories', first match wins):
    name/family/platform patterns first, then description keywords."""
    routes = load_json(os.path.join(ROOT, "routes.json"), {}) or {}
    cats = routes.get("categories") or []
    for s in skills:
        s["category"] = None
        for c in cats:
            if any(fnmatch.fnmatchcase(s["name"], pat) or fnmatch.fnmatchcase(s["base"], pat)
                   for pat in c.get("skills", [])):
                s["category"] = c["name"]
                break
        if s["category"]:
            continue
        best, best_n = None, 0
        toks = set(s["tf"]) | set(s["name_toks"])
        for c in cats:
            n = len(toks & set(srlib.tokens(" ".join(c.get("intent", [])))))
            if n > best_n:
                best, best_n = c["name"], n
        s["category"] = best or "other"


def category_intents():
    routes = load_json(os.path.join(ROOT, "routes.json"), {}) or {}
    return {c["name"]: sorted(set(srlib.norm_phrase(w) for w in c.get("intent", []) if srlib.norm_phrase(w)))
            for c in routes.get("categories") or []}


def tag_platforms(skills):
    routes = load_json(os.path.join(ROOT, "routes.json"), {}) or {}
    plats = routes.get("platforms") or {}
    for s in skills:
        for pname, spec in plats.items():
            if any(fnmatch.fnmatchcase(s["name"], pat) for pat in spec.get("skills", [])):
                s["platform"] = pname
                break


def mark_duplicates(skills, prefer_user=True):
    """Group same-base-name or near-identical descriptions; keep one canonical."""
    def rank(s):
        return (0 if s["source"] in ("user", "project") else 1, len(s["name"]))
    groups = {}
    for s in skills:
        groups.setdefault(s["base"], []).append(s)
    for base, g in groups.items():
        if len(g) > 1:
            g.sort(key=rank)
            for s in g[1:]:
                s["dup_of"] = g[0]["name"]
    # near-identical descriptions across different names
    sets = [(s, set(s["tf"])) for s in skills if not s["dup_of"]]
    for i in range(len(sets)):
        a, sa = sets[i]
        if a["dup_of"] or len(sa) < 6:
            continue
        for j in range(i + 1, len(sets)):
            b, sb = sets[j]
            if b["dup_of"] or len(sb) < 6:
                continue
            inter = len(sa & sb)
            if inter / float(len(sa | sb)) >= 0.85:
                keep, drop = sorted([a, b], key=rank)
                drop["dup_of"] = keep["name"]


def source_signature():
    """Cheap fingerprint of every input: dir mtimes + SKILL.md mtimes + plugin registry."""
    sig = []
    for p in (INSTALLED, SETTINGS, USER_SKILLS, os.path.join(PLUGINS, "synced"), os.path.abspath(__file__),
              os.path.join(ROOT, "routes.json"), os.path.join(ROOT, "builtin_skills.json"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "srlib.py"),
              os.path.join(srlib.data_dir(), "config.json")):
        try:
            sig.append(int(os.stat(p).st_mtime))
        except OSError:
            sig.append(0)
    try:
        for n in sorted(os.listdir(USER_SKILLS)):
            try:
                sig.append(int(os.stat(os.path.join(USER_SKILLS, n, "SKILL.md")).st_mtime))
            except OSError:
                pass
    except OSError:
        pass
    return hashlib.sha1(repr(sig).encode()).hexdigest()[:16]


def build(out_dir=None):
    out_dir = out_dir or srlib.data_dir()
    skills = [s for s in (extract(e) for e in discover()) if s]
    cfg = load_json(os.path.join(out_dir, "config.json"), {}) or {}
    have = {s["name"] for s in skills} | {s["base"] for s in skills}
    for name, desc, fmts in builtin_entries(cfg.get("builtins", "auto") if isinstance(cfg, dict) else "auto"):
        if name not in have and name.split(":", 1)[-1] not in have:
            b = from_description({"name": name, "path": "builtin", "source": "builtin",
                                  "plugin": name.split(":", 1)[0] if ":" in name else None}, desc)
            b["formats"] = list(fmts)
            skills.append(b)
    for s in skills:
        if not s["formats"] and s["base"] in ("pdf", "docx", "xlsx", "pptx"):
            s["formats"] = [s["base"]]
    mark_duplicates(skills)
    tag_platforms(skills)
    tag_categories(skills)
    # family prefix: first name token shared by >= 3 skills (seo-*, caveman-*, expo-* ...)
    firsts = {}
    for s in skills:
        if s["name_toks"]:
            f = srlib.name_tokens(s["base"].replace("-", " "))[0]
            firsts[f] = firsts.get(f, 0) + 1
    for s in skills:
        ft = srlib.name_tokens(s["base"].replace("-", " "))
        s["family"] = ft[0] if ft and firsts.get(ft[0], 0) >= 3 and ft[0] not in srlib.WEAK else None
        s["first"] = ft[0] if ft else None
    df = {}
    for s in skills:
        for t in set(s["tf"]) | set(s["name_toks"]) | set(s["kw"]):
            df[t] = df.get(t, 0) + 1
        for d in s["dir"]:
            df[d] = df.get(d, 0) + 1
    bdf = {}
    for s in skills:
        for t in s.get("body", ()):
            bdf[t] = bdf.get(t, 0) + 1
    n = len(skills)
    avgdl = sum(s["dl"] for s in skills) / float(n or 1)
    idx = {
        "version": srlib.INDEX_VERSION,
        "built_at": time.time(),
        "signature": source_signature(),
        "N": n,
        "avgdl": avgdl,
        "idf": {t: round(math.log(1 + (n - c + 0.5) / (c + 0.5)), 4) for t, c in df.items()},
        "bidf": {t: round(math.log(1 + (n - c + 0.5) / (c + 0.5)), 4) for t, c in bdf.items()},
        "cat_intents": category_intents(),
        "skills": skills,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.json")
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path, idx


def is_stale(out_dir=None):
    path = os.path.join(out_dir or srlib.data_dir(), "index.json")
    idx = load_json(path)
    if not idx or idx.get("version") != srlib.INDEX_VERSION:
        return True
    return idx.get("signature") != source_signature()


def main(argv):
    try:
        if "--list" in argv:
            path = os.path.join(srlib.data_dir(), "index.json")
            idx = load_json(path) if not is_stale() else None
            if not idx:
                _, idx = build()
            groups = {}
            for sk in idx["skills"]:
                if not sk.get("dup_of"):
                    groups.setdefault(sk.get("category") or "other", []).append(sk["name"])
            for cat in sorted(groups, key=lambda c: (-len(groups[c]), c)):
                print("%s (%d)" % (cat, len(groups[cat])))
                print("  " + ", ".join(sorted(groups[cat])))
            return 0
        if "--if-stale" in argv:
            if is_stale():
                build()
            return 0
        t0 = time.time()
        path, idx = build()
        if True:
            dups = [s for s in idx["skills"] if s["dup_of"]]
            print("indexed %d skills (%d marked duplicate) in %.0f ms -> %s" % (
                idx["N"], len(dups), (time.time() - t0) * 1000, path))
            if "--stats" in argv:
                for s in dups:
                    print("  dup: %s -> %s" % (s["name"], s["dup_of"]))
        return 0
    except Exception as e:  # never fail a session start
        if "--if-stale" not in argv:
            print("build failed: %r" % (e,), file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
