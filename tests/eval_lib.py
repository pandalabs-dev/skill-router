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


def metrics(h, data=None):
    data = data or load_set()
    out = {}
    for split in ("dev", "holdout", "holdout_v1"):
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
    allp = [norm(i) for i in data["dev"] + data["holdout"]] + [norm(s) for s in silent] + [norm(p) for p in data["everyday"]]
    allsizes = [len(h.suggest(i)[0]) for i in allp]
    out["all"] = {"n": len(allp), "avg_chars": sum(allsizes) / float(len(allp)), "max_chars": max(allsizes)}
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
        splits = ("dev", "holdout", "holdout_v1") if "--holdout" in sys.argv else ("dev", "holdout_v1")
        for split in splits:
            d = m[split]
            print("%-8s n=%d top1=%.0f%% hit@5=%.0f%% fired=%.0f%%" % (split, d["n"], d["top1"] * 100, d["hit5"] * 100, d["fired"] * 100))
        print("silent   n=%d false_fire=%.0f%% %s" % (m["silent"]["n"], m["silent"]["false_fire"] * 100, m["silent"]["fires"]))
        e = m["everyday"]
        print("everyday n=%d fire=%.0f%% avg_chars=%.1f max_chars=%d" % (e["n"], e["fire"] * 100, e["avg_chars"], e["max_chars"]))
        print("all      n=%d avg_chars=%.1f max_chars=%d" % (m["all"]["n"], m["all"]["avg_chars"], m["all"]["max_chars"]))
        med, mx = latency_ms(h, ["write a postgres migration adding an index", "the CI is red again"])
        print("latency  median=%.0f ms max=%.0f ms (%s)" % (med, mx, sys.executable))
        if "-v" in sys.argv:
            for split in splits:
                for p, n in m[split]["misses"]:
                    print("MISS %-7s %s -> %s" % (split, p, n))
            for p, n in m["everyday"]["fired"]:
                print("EVERYDAY fired: %s -> %s" % (p, n))
    finally:
        h.close()


if __name__ == "__main__":
    main()
