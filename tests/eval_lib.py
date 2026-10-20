"""Routing eval harness shared by tests/test_eval.py and `python3 tests/eval_lib.py`.

Builds a fresh index from this machine's installed skills into a temp dir, runs
every labeled prompt through route.run (no session state), and reports metrics."""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import build_index  # noqa: E402
import route  # noqa: E402

FIXTURES = {
    "plain": {},
    "vercel": {"vercel.json": "{}", "package.json": json.dumps({"dependencies": {"next": "16.0.0"}})},
    "expo": {"app.json": "{}", "package.json": json.dumps({"dependencies": {"expo": "54.0.0", "react-native": "0.81.0"}})},
}


def load_set():
    with open(os.path.join(ROOT, "tests", "eval_set.json"), encoding="utf-8") as f:
        return json.load(f)


def norm(item):
    return item if isinstance(item, dict) else {"p": item}


class Harness(object):
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="skill-router-eval-")
        self.ddir = os.path.join(self.tmp, "data")
        _, self.index = build_index.build(self.ddir)
        self.names = {s["name"] for s in self.index["skills"]}
        self.dirs = {}
        for name, files in FIXTURES.items():
            d = os.path.join(self.tmp, "proj-" + name)
            os.makedirs(d)
            for fn, content in files.items():
                with open(os.path.join(d, fn), "w") as f:
                    f.write(content)
            self.dirs[name] = d

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def suggest(self, item):
        item = norm(item)
        cwd = self.dirs[item.get("fixture", "plain")]
        payload = {"prompt": item["p"], "cwd": cwd, "session_id": "eval"}
        text = route.run(payload, ddir=self.ddir, persist=False)
        return text or "", parse_names(text)

    def coverage(self, items):
        """Fraction of labeled items whose acceptable skills exist in this index."""
        ok = [i for i in items if set(i["accept"]) & self.names]
        return len(ok) / float(len(items) or 1)


def parse_names(text):
    if not text:
        return []
    body = text[len(route.HEADER):]
    return [part.split(" (", 1)[0].strip() for part in body.split("; ") if part.strip()]


def class_skill(cls, s, prompt):
    """Does suggested skill s belong to the miss class a negative look-alike must not fire?"""
    if s is None:
        return False
    if cls == "A":   # matched only through everyday words of its name
        import srlib
        hit = set(s["name_toks"]) & set(srlib.tokens(prompt))
        return bool(hit) and hit <= set(s.get("common_name", ()))
    if cls == "B":
        return s.get("category") == "security" or s["name"] in ("solidity-security", "solana-vulnerability-scanner")
    if cls == "C":
        return s.get("category") == "seo"
    if cls == "D":
        return s["name"] == "image-to-code" or s.get("category") == "media-generation"
    return False


def metrics(h, data=None):
    data = data or load_set()
    out = {}
    for split in ("dev", "holdout_v3", "holdout_v2", "holdout_v1"):
        items = [i for i in data.get(split, []) if set(i["accept"]) & h.names]
        top1 = hit = fired = 0
        misses = []
        for it in items:
            _, names = h.suggest(it)
            acc = set(it["accept"])
            if names:
                fired += 1
            if names and names[0] in acc:
                top1 += 1
            if acc & set(names[:5]):
                hit += 1
            else:
                misses.append((it["p"], names))
        n = float(len(items) or 1)
        out[split] = {"n": len(items), "top1": top1 / n, "hit5": hit / n, "fired": fired / n, "misses": misses}
    silent = data["silent"]
    false_fires = [(norm(s)["p"], h.suggest(s)[1]) for s in silent if h.suggest(s)[1]]
    out["silent"] = {"n": len(silent), "false_fire": len(false_fires) / float(len(silent)), "fires": false_fires}
    ev = [h.suggest(p) for p in data["everyday"]]
    sizes = [len(t) for t, _ in ev]
    out["everyday"] = {"n": len(ev), "fire": sum(1 for s in sizes if s) / float(len(ev)),
                       "avg_chars": sum(sizes) / float(len(ev)), "max_chars": max(sizes),
                       "fired": [(p, n) for p, (t, n) in zip(data["everyday"], ev) if n]}
    for split in ("class_dev", "class_holdout_v4"):
        per = {}
        for it in data.get(split, []):
            if it["accept"] and not (set(it["accept"]) & h.names) and not any(":" not in a for a in it["accept"]):
                continue
            _, names = h.suggest(it)
            ok = (not it["accept"] or bool(set(it["accept"]) & set(names))) \
                and not (set(it.get("forbid", [])) & set(names)) \
                and len(names) <= it.get("max_names", 5)
            d = per.setdefault(it["cls"], {"n": 0, "pass": 0, "fails": [], "names": []})
            d["n"] += 1
            d["pass"] += ok
            if names:
                d["names"].append(len(names))
            if not ok:
                d["fails"].append((it["p"], names))
        out[split] = per
    negs = {}
    by = {s["name"]: s for s in h.index["skills"]}
    for split, items in (("dev", data.get("class_neg_dev", [])),
                         ("holdout_v4", data.get("class_neg_holdout_v4", []))):
        for it in items:
            names = h.suggest(it)[1]
            hits = [n for n in names if class_skill(it["cls"], by.get(n), it["p"])]
            d = negs.setdefault((split, it["cls"]), {"n": 0, "fired": 0, "fires": [], "any": 0})
            d["n"] += 1
            d["any"] += bool(names)
            if hits:
                d["fired"] += 1
                d["fires"].append((it["p"], names))
    out["class_neg"] = negs
    fired_names = [len(h.suggest(i)[1]) for i in data["dev"] + data["holdout_v3"] + [norm(p) for p in data["everyday"]]]
    fired_names = [n for n in fired_names if n]
    out["names_per_hint"] = sum(fired_names) / float(len(fired_names) or 1)
    allp = [norm(i) for i in data["dev"] + data["holdout_v3"] + data["class_dev"]] + [norm(s) for s in silent] + [norm(p) for p in data["everyday"]]
    allsizes = [len(h.suggest(i)[0]) for i in allp]
    out["all"] = {"n": len(allp), "avg_chars": sum(allsizes) / float(len(allp)), "max_chars": max(allsizes)}
    return out


def deep_metrics(h, data=None, split="deep"):
    """Deep mode vs normal mode on the same task text."""
    data = data or load_set()
    out = {"n": 0, "deep_hit": 0, "normal_hit": 0, "nomatch_ok": 0, "nomatch_n": 0, "chars": [], "misses": []}
    for it in data.get(split, []):
        prompt = "%s, %s" % (it["task"], it["trigger"]) if not it["trigger"].startswith("+") else "%s %s" % (it["trigger"], it["task"])
        text = route.run({"prompt": prompt, "cwd": h.dirs["plain"], "session_id": "deep"}, ddir=h.ddir, persist=False) or ""
        out["chars"].append(len(text))
        names = [l[2:].split(": ", 1)[0].split(" (", 1)[0] for l in text.splitlines() if l.startswith("- ")]
        if not it["accept"]:
            out["nomatch_n"] += 1
            ok = "no installed skill clearly matches" in text
            out["nomatch_ok"] += ok
            if not ok:
                out["misses"].append((prompt, names or ["(expected no-match)"]))
            continue
        if not (set(it["accept"]) & h.names):
            continue
        out["n"] += 1
        normal = h.suggest({"p": it["task"]})[1]
        out["normal_hit"] += bool(set(it["accept"]) & set(normal))
        hit = bool(set(it["accept"]) & set(names[:5]))
        out["deep_hit"] += hit
        if not hit:
            out["misses"].append((prompt, names))
    return out


def latency_ms(h, prompts, runs=3):
    import subprocess
    env = dict(os.environ, SKILL_ROUTER_DATA=h.ddir)
    ts = []
    for p in prompts:
        for _ in range(runs):
            t0 = time.time()
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "route.py")],
                           input=json.dumps({"prompt": p, "cwd": h.dirs["plain"], "session_id": "lat"}),
                           capture_output=True, text=True, env=env)
            ts.append((time.time() - t0) * 1000)
    ts.sort()
    return ts[len(ts) // 2], ts[-1]


def main():
    h = Harness()
    try:
        m = metrics(h)
        splits = ("dev", "holdout_v3", "holdout_v2", "holdout_v1")
        for split in splits:
            d = m[split]
            print("%-8s n=%d top1=%.0f%% hit@5=%.0f%% fired=%.0f%%" % (split, d["n"], d["top1"] * 100, d["hit5"] * 100, d["fired"] * 100))
        csplits = ("class_dev", "class_holdout_v4") if "--holdout" in sys.argv else ("class_dev",)
        for cs in csplits:
            for c in sorted(m[cs]):
                d = m[cs][c]
                nh = sum(d["names"]) / float(len(d["names"]) or 1)
                print("%-17s %s n=%d pass=%.0f%% names/hint=%.2f" % (cs, c, d["n"], 100.0 * d["pass"] / d["n"], nh))
        for (sp, c), d in sorted(m["class_neg"].items()):
            if sp == "holdout_v4" and "--holdout" not in sys.argv:
                continue
            print("neg %-10s %s n=%d class_false_fire=%.0f%% any_hint=%.0f%%" % (
                sp, c, d["n"], 100.0 * d["fired"] / d["n"], 100.0 * d["any"] / d["n"]))
        print("silent   n=%d false_fire=%.0f%% %s" % (m["silent"]["n"], m["silent"]["false_fire"] * 100, m["silent"]["fires"]))
        e = m["everyday"]
        print("everyday n=%d fire=%.0f%% avg_chars=%.1f max_chars=%d" % (e["n"], e["fire"] * 100, e["avg_chars"], e["max_chars"]))
        print("all      n=%d avg_chars=%.1f max_chars=%d names_per_hint=%.2f" % (
            m["all"]["n"], m["all"]["avg_chars"], m["all"]["max_chars"], m["names_per_hint"]))
        for split in (("deep", "deep_holdout") if "--holdout" in sys.argv else ("deep",)):
          dm = deep_metrics(h, split=split)
          print(split + " n=%d hit@5 deep=%.0f%% normal(same task)=%.0f%% no-match ok=%d/%d avg_chars=%.0f max_chars=%d" % (
            dm["n"], 100.0 * dm["deep_hit"] / (dm["n"] or 1), 100.0 * dm["normal_hit"] / (dm["n"] or 1),
            dm["nomatch_ok"], dm["nomatch_n"], sum(dm["chars"]) / float(len(dm["chars"]) or 1), max(dm["chars"] or [0])))
          if "-v" in sys.argv:
            for p, n in dm["misses"]:
                print("DEEPMISS %s %s -> %s" % (split, p, n))
        dmed, dmx = latency_ms(h, ["make the hero section feel minimal and calm, use necessary skills",
                                   "full table scan is slow, use relevant skills"])
        print("deep latency median=%.0f ms max=%.0f ms" % (dmed, dmx))
        med, mx = latency_ms(h, ["write a postgres migration adding an index", "the CI is red again"])
        print("latency  median=%.0f ms max=%.0f ms (%s)" % (med, mx, sys.executable))
        if "-v" in sys.argv:
            for split in splits:
                for p, n in m[split]["misses"]:
                    print("MISS %-7s %s -> %s" % (split, p, n))
            for cs in csplits:
                for c in sorted(m[cs]):
                    for p, n in m[cs][c]["fails"]:
                        print("FAIL %s %s: %s -> %s" % (cs, c, p, n))
            for (sp, c), d in sorted(m["class_neg"].items()):
                if sp == "dev" or "--holdout" in sys.argv:
                    for p, n in d["fires"]:
                        print("NEGFIRE %s %s: %s -> %s" % (sp, c, p, n))
            for p, n in m["everyday"]["fired"]:
                print("EVERYDAY fired: %s -> %s" % (p, n))
    finally:
        h.close()


if __name__ == "__main__":
    main()
