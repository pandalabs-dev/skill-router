"""Shared helpers for skill-router (stdlib only, Python 3.8+)."""
import os
import re

INDEX_VERSION = 3

HOME = os.path.expanduser("~")

# Words that carry no routing signal: English stopwords plus the boilerplate
# vocabulary every SKILL.md description uses ("use when the user wants ...").
STOPWORDS = set("""
a about above after again against all also am an and any are aren as at be because been before being
below between both but by can cannot could couldn did didn do does doesn doing don down during each
else etc even ever every few for from further get gets getting go goes going got had has have having he her
here hers herself him himself his how however i if in into is isn it its itself just let lets like ll
may me might more most much must my myself need needs no nor not now of off on once one only or other
our ours ourselves out over own per please re really s same shall she should so some such t than that
the their theirs them themselves then there these they this those through thus to too under until up
upon us ve very via want wants was we were what whatever when whenever where whether which while who
whom whose why will with within without would you your yours yourself yourselves
use used uses using usage user users skill skills claude agent agents ask asks asked asking invoke
invoked invokes trigger triggers triggered triggering e g eg ie i.e e.g including include includes
covers cover covering provides provide providing help helps helping helpful based across new
something anything thing things way ways make makes making want wanted task tasks request requests
mention mentions mentioned mentioning work working works related relevant specific specifically
via within whenever always never must mandatory prerequisite before after first instead
""".split())

# Generic action words: kept for matching but they never count toward the
# "distinct meaningful terms" confidence test on their own.
WEAK = set("""
add added adding build building built create creating created fix fixing fixed run running write
writing written update updating change changing check checking set setup show find look implement
implementing implementation improve improving code project file files page app application
tool tools review reviewing test testing support supports handle handling manage managing
design designing generate generating good best guide guidance pattern patterns system systems
feature features component components data
explain explaining tell know think understand describe summarize summary list try see keep start stop
move remove delete call return take put open close look looking rename steps step thing want
help question answer quick simple new old right wrong work works working done
""".split())

# Role words in skill names that say nothing about the domain.
NAME_GENERIC = set("""
pro expert engineer developer specialist architect wizard guardian master max forge advanced best
practices practice skill skills helper assistant manager guide guidelines core
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#._-]*[a-z0-9+#]|[a-z0-9]")


# Canonical spellings for common tech aliases (applied before stemming).
ALIASES = {
    "postgresql": "postgres", "psql": "postgres", "pg": "postgres",
    "k8s": "kubernetes", "golang": "go", "js": "javascript", "ts": "typescript",
    "envs": "env",
    "repo": "repository", "repos": "repository", "db": "database", "dbs": "database",
    "rn": "react-native", "e2e": "end-to-end", "ui": "ui", "ux": "ux",
    "wp": "wordpress", "woo": "woocommerce", "py": "python",
}


def stem(w):
    """Light, deterministic suffix stripper (Porter-lite). Only consistency matters."""
    if len(w) <= 3 or not w.isalpha():
        return w
    # plurals
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith(("xes", "ches", "shes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    # derivational / inflectional (one pass)
    for suf, rep, minlen in (("ational", "ate", 8), ("ation", "ate", 7), ("ment", "", 7),
                             ("ing", "", 5), ("ed", "", 5), ("ly", "", 6), ("ure", "", 7),
                             ("er", "", 7), ("ity", "", 6)):
        if w.endswith(suf) and len(w) >= minlen:
            w = w[: len(w) - len(suf)] + rep
            if suf in ("ing", "ed") and len(w) >= 3 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


WEAK = set(stem(w) for w in WEAK) | WEAK
NAME_GENERIC = set(stem(w) for w in NAME_GENERIC) | NAME_GENERIC


def raw_tokens(text):
    """Lowercase word tokens; splits compound tokens (next.js, env-vars) into parts too."""
    out = []
    for m in _TOKEN_RE.findall(text.lower()):
        parts = [p for p in re.split(r"[._/-]+", m) if p]
        if len(parts) > 1:
            out.extend(parts)
            joined = "".join(parts)
            if len(joined) <= 12:
                out.append(joined)  # nextjs, shadcnui ...
        else:
            out.append(m)
    return out


def tokens(text):
    """Meaningful, stemmed tokens (stopwords removed)."""
    res = []
    for t in raw_tokens(text):
        if t in STOPWORDS or (len(t) < 2 and not t.isdigit()):
            continue
        res.append(stem(ALIASES.get(t, t)))
    return res


NAME_STOP = set("a an the to of and for with on in by".split())


def name_tokens(text):
    """Tokens of a skill name: keeps words like 'agents' that are stopwords in prose."""
    return [stem(ALIASES.get(t, t)) for t in raw_tokens(text) if t not in NAME_STOP and len(t) > 1]


def norm_phrase(text):
    return " ".join(stem(ALIASES.get(t, t)) for t in raw_tokens(text) if t not in STOPWORDS)


def data_dir():
    d = os.environ.get("SKILL_ROUTER_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        d = os.path.join(HOME, ".claude", "skill-router")
    return d


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


# ---------------------------------------------------------------- frontmatter

def parse_frontmatter(text):
    """Tiny YAML-subset parser for SKILL.md frontmatter (top-level scalars only).

    Handles plain, single/double-quoted, folded (>) and literal (|) scalars, plus
    plain scalars that continue on indented lines."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i in range(1, min(len(lines), 400)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    fm = {}
    i = 1
    key_re = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
    while i < end:
        line = lines[i]
        m = key_re.match(line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        i += 1
        cont = []
        while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            cont.append(lines[i])
            i += 1
        if rest[:1] in (">", "|"):
            body = [c.strip() for c in cont]
            if rest[0] == ">":
                val = " ".join(b for b in body if b)
            else:
                val = "\n".join(body).strip()
        elif rest[:1] in ('"', "'"):
            q = rest[0]
            full = " ".join([rest] + [c.strip() for c in cont if c.strip()])
            if q == '"':
                mm = re.match(r'"((?:[^"\\]|\\.)*)"', full)
                val = mm.group(1) if mm else full.strip('"')
                val = val.replace('\\"', '"').replace("\\n", " ").replace("\\\\", "\\")
            else:
                mm = re.match(r"'((?:[^']|'')*)'", full)
                val = (mm.group(1) if mm else full.strip("'")).replace("''", "'")
        else:
            if cont and all(not c.strip() or not key_re.match(c.strip()) for c in cont) and rest:
                val = " ".join([rest] + [c.strip() for c in cont if c.strip()])
            else:
                val = rest
        fm[key.lower()] = val.strip()
    return fm


def read_head(path, limit=24000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""
