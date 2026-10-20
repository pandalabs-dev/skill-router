"""Deep mode: on explicit request ("use necessary skills"), search the installed
skills more widely and tell the model which ones to invoke.

Runs only when a deep trigger phrase is in the prompt; normal prompts never import
this module. Everything is local: the candidate pool comes from the normal scorer
with the silence threshold off, then each candidate's full SKILL.md is read and its
when-to-use / trigger sections, headings and NOT-for clauses re-rank the pool."""
import difflib
import math
import os
import re

import srlib

POOL = 30
MAX_NAMES = 5
MAX_CHARS = 700
LOW_BAR = 8.0          # deep score a skill must reach to be listed at all
REL_BAR = 0.35         # ... and this fraction of the best deep score
PURPOSE_CHARS = 90

_WHEN_HEAD = re.compile(r"when|use (?:this|it|cases?)|usage|trigger|purpose|overview|capabilit|what (?:it|this) does|scope", re.I)
_NOT_HEAD = re.compile(r"\bnot\b|don'?t|avoid|limitation|out of scope|anti", re.I)
_WHEN_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:use (?:this|it|when|for)|triggers?(?: on)?:|invoke (?:for|when)|when (?:the )?user)", re.I)


def fix_typos(task, idf):
    """Replace unknown words with a close index term ('screnshot' -> 'screenshot')."""
    vocab = None
    out = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]*|[^A-Za-z]+", task):
        w = word.lower()
        if len(w) >= 5 and w.isalpha():
            st = srlib.stem(w)
            if st not in idf and w not in srlib.STOPWORDS:
                if vocab is None:
                    vocab = [k for k in idf if k.isalpha() and len(k) >= 4]
                # typos rarely hit the first two letters: compare within that slice only
                m = difflib.get_close_matches(st, [k for k in vocab if k[:2] == st[:2]], n=1, cutoff=0.75)
                if m:
                    out.append(m[0])
                    continue
        out.append(word)
    return "".join(out)


def sections(path, limit=40000):
    """(when_text, heading_text, not_text, body_text) from a SKILL.md body."""
    if not path or path == "builtin" or not os.path.isfile(path):
        return "", "", "", ""
    text = srlib.read_head(path, limit)
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) > 2 else ""
    when, heads, neg = [], [], []
    mode = None
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("#"):
            title = st.lstrip("#").strip()
            heads.append(title)
            mode = "not" if _NOT_HEAD.search(title) else ("when" if _WHEN_HEAD.search(title) else None)
            continue
        if not st:
            continue
        if mode == "when" and sum(len(x) for x in when) < 4000:
            when.append(st)
        elif mode == "not" and sum(len(x) for x in neg) < 2000:
            neg.append(st)
        elif _WHEN_LINE.search(st) and sum(len(x) for x in when) < 4000:
            when.append(st)
    return " ".join(when), " ".join(heads), " ".join(neg), text[:20000]


def purpose(s):
    d = s.get("desc", "")
    d = re.sub(r"^\W*(mandatory prerequisite|critical|important)\W*", "", d, flags=re.I)
    d = re.sub(r"^(Framework \(OSS\)\.\s*|EAS service \(paid\)\.\s*)", "", d).replace("**", "").strip()
    first = re.split(r"(?<=[.!?])\s", d, 1)[0]
    if len(first) > PURPOSE_CHARS:
        first = first[: PURPOSE_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return first


def deep_hint(task, results, idx, used, q=None):
    """results: normal scorer output for the task text (sorted). Returns hint text."""
    idf = idx["idf"]
    common = set(idx.get("common") or ())
    qtoks = set(srlib.tokens(task)) | set(srlib.intent_terms(task)) | set(srlib.expand(srlib.tokens(task), task))
    specific = set(t for t in qtoks if t not in srlib.WEAK)
    rare = specific - common          # words that can carry a deep match on their own
    tbig = srlib.bigrams(srlib.tokens(task))
    pool = []
    seen = set()
    for r in results:
        s = r[1]
        name = s.get("dup_of") or s["name"]
        if name in seen:
            continue
        seen.add(name)
        pool.append(r)
        if len(pool) >= POOL:
            break
    ranked = []
    for r in pool:
        base_score, s = r[0], r[1]
        name = s.get("dup_of") or s["name"]
        when, heads, neg, body = sections(s.get("path"))
        if not when and not body:
            when = s.get("desc", "")   # built-in skills have no SKILL.md: the description is all there is
        wtoks = srlib.tokens(when)
        wtf = {}
        for t in wtoks:
            wtf[t] = wtf.get(t, 0) + 1
        ht = set(srlib.tokens(heads))
        nt = set(srlib.tokens(neg))
        bonus = 0.0
        hits = set()
        for t in specific:
            w = min(idf.get(t, 2.0), 4.5) * (0.5 if t in common else 1.0)
            if wtf.get(t):
                bonus += w * (1.0 + math.log(wtf[t])) * 0.8
                hits.add(t)
            elif t in ht:
                bonus += w * 0.5
                hits.add(t)
            if t in s["name_toks"]:
                bonus += 1.5 * w     # the user asked for skills: a skill named for the task counts
                hits.add(t)
        bphr = srlib.bigrams(srlib.tokens(body)) if body else set()
        bg = tbig & (srlib.bigrams(wtoks) | bphr)
        # a two-word phrase from the request ('table scan', 'hero section') anywhere in the SKILL.md
        bonus += sum(3.0 + 0.8 * sum(min(idf.get(t, 2.0), 4.5) for t in b.split()) for b in bg)
        hits.update(t for b in bg for t in b.split())
        penalty = 0.6 if (specific & nt) - set(r[4]) - hits else 1.0
        score = (0.6 * base_score + bonus) * penalty
        distinct_name = set(t for t in s["name_toks"] if t not in srlib.WEAK)
        name_ev = bool(distinct_name) and distinct_name <= hits     # every distinguishing name word
        evidence = ((set(r[4]) | hits) & rare) | (hits & set(t for b in bg for t in b.split()))
        if name_ev:
            evidence |= distinct_name
        if not evidence and not r[3]:
            continue   # only everyday or generic words: not evidence
        ranked.append((score, name, s))
    ranked.sort(key=lambda x: -x[0])
    if not ranked or ranked[0][0] < LOW_BAR:
        return ("skill-router deep: you asked to use relevant skills, but no installed skill clearly matches "
                "this request. Proceed without loading skills.")
    top = ranked[0]
    cats = q.cats if q is not None else set()
    picks = []
    for x in ranked:
        if x[0] < max(LOW_BAR, REL_BAR * top[0]):
            break
        cat = x[2].get("category")
        if picks and cat != top[2].get("category") and cat not in cats:
            continue   # another topic the request does not mention
        picks.append(x)
        if len(picks) >= MAX_NAMES:
            break
    head = ("skill-router deep: you asked to use relevant skills. Invoke the ones below that genuinely fit "
            "this request via the Skill tool before starting, and skip the rest:")
    lines = []
    for _, name, s in picks:
        mark = " (already used this session)" if name in used or s["name"] in used else ""
        lines.append("- %s%s: %s" % (name, mark, purpose(s)))
    while lines and len(head) + sum(len(x) + 1 for x in lines) > MAX_CHARS:
        lines.pop()
    return head + "\n" + "\n".join(lines)
