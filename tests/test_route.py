"""skill-router tests (stdlib unittest). Run: python3 -m unittest discover -s tests -v

The routing tests run against a real index built from this machine's installed
skills, written to a temporary data dir (the live index is never touched)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
import srlib  # noqa: E402
import build_index  # noqa: E402
import route  # noqa: E402

TMP = tempfile.mkdtemp(prefix="skill-router-test-")
CWD = os.path.expanduser("~")  # neutral directory: no project signals
_, INDEX = build_index.build(TMP)
NAMES = {s["name"] for s in INDEX["skills"]}

# prompt -> acceptable skills (any one of them must be among the suggestions)
EXPECTED = [
    ("add a new tab screen with expo router", {"expo-router"}),
    ("run an SEO audit on example.com", {"seo", "seo-audit"}),
    ("add the STRIPE_KEY env var to the vercel project", {"vercel:env-vars", "vercel:env"}),
    ("turn this page into a Figma design", {"figma:figma-generate-design"}),
    ("review this diff for bugs before I merge", {"code-reviewer", "code-review"}),
    ("this test is flaky and fails randomly, find out why", {"superpowers:systematic-debugging", "test-master"}),
    ("write a postgres migration adding an index", {"postgres-pro", "sql-pro"}),
]
SILENT = ["hi", "yes", "/help", "ok go on", "continue", "do it", "✨", "thanks, that looks good"]

# Everyday prompts used for the token-budget measurement (fire rate / size).
EVERYDAY = [
    "commit this and push to main",
    "what does this function do?",
    "can you explain why the build is slow on CI?",
    "rename the variable userId to accountId in this file",
    "git rebase onto main and resolve the conflicts",
    "what's the status of the deploy?",
    "summarize what we changed today",
    "how many rows are in the orders table right now?",
    "check the logs for the payment webhook errors from last night",
    "make the button on the pricing page bigger and blue",
    "why is this returning undefined?",
    "update the README with the new install steps",
    "the login page crashes on safari, can you look",
    "let's pause here, write down where we are",
    "send an email to Alex about the invoice",
    "add a dark mode toggle to the settings page",
    "create a dockerfile for this fastapi service",
    "the useEffect runs twice on mount, is that expected?",
    "bump next to the latest version and fix breaking changes",
    "write unit tests for the payout fee calculator",
    "upload the build to testflight",
    "set up a github actions pipeline that runs tests on every PR",
    "what is the difference between these two approaches",
    "optimize this slow SQL query, it takes 4 seconds",
    "translate the landing page copy to German",
]


def suggest(prompt, session=None, cwd=CWD, persist=False):
    payload = {"prompt": prompt, "cwd": cwd, "session_id": session or "t-%f" % time.time()}
    return route.run(payload, ddir=TMP, persist=persist)


def names_in(text):
    if not text:
        return []
    body = text[len(route.HEADER):]
    out = []
    for part in body.split("; "):
        out.append(part.split(" (", 1)[0].strip())
    return out


class RoutingTests(unittest.TestCase):
    def test_expected_top_hits(self):
        for prompt, accept in EXPECTED:
            if not (accept & NAMES) and not any(a == "code-review" for a in accept):
                self.skipTest("skills not installed: %s" % accept)
            with self.subTest(prompt=prompt):
                got = names_in(suggest(prompt))
                self.assertTrue(set(got) & accept, "%r -> %r, wanted one of %r" % (prompt, got, accept))

    def test_silent_prompts(self):
        for p in SILENT:
            with self.subTest(prompt=p):
                self.assertIsNone(suggest(p))

    def test_output_cap_and_k(self):
        for prompt, _ in EXPECTED:
            text = suggest(prompt)
            if text:
                self.assertLessEqual(len(text), route.DEFAULTS["max_chars"])
                self.assertLessEqual(len(names_in(text)), route.HARD_MAX_K)

    def test_token_budget(self):
        prompts = [p for p, _ in EXPECTED] + SILENT + EVERYDAY
        self.assertGreaterEqual(len(prompts), 30)
        sizes = [len(suggest(p) or "") for p in prompts]
        fired = [s for s in sizes if s]
        fire_rate = len(fired) / float(len(prompts))
        avg = sum(sizes) / float(len(prompts))
        sys.stderr.write("\n[budget] prompts=%d fire_rate=%.0f%% avg_chars=%.1f max_chars=%d\n" % (
            len(prompts), fire_rate * 100, avg, max(sizes)))
        self.assertLessEqual(fire_rate, 0.40)
        self.assertLessEqual(avg, 150)

    def test_once_per_session(self):
        sid = "once-%d" % os.getpid()
        first = suggest("add a new tab screen with expo router", session=sid, persist=True)
        self.assertIn("expo-router", names_in(first))
        for _ in range(4):
            again = suggest("add another tab screen with expo router please", session=sid, persist=True)
            self.assertNotIn("expo-router", names_in(again))

    def test_skips_skill_already_invoked_in_transcript(self):
        sid = "inv-%d" % os.getpid()
        tp = os.path.join(TMP, "transcript.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill",
                                                         "input": {"skill": "expo-router"}}]}},
                               separators=(",", ":")) + "\n")
        payload = {"prompt": "add a new tab screen with expo router", "cwd": CWD,
                   "session_id": sid, "transcript_path": tp}
        self.assertNotIn("expo-router", names_in(route.run(payload, ddir=TMP, persist=True)))

    def test_duplicates_collapse(self):
        dups = {s["name"]: s["dup_of"] for s in INDEX["skills"] if s["dup_of"]}
        if "ui-ux-pro-max:ui-ux-pro-max" in NAMES:
            self.assertEqual(dups.get("ui-ux-pro-max:ui-ux-pro-max"), "ui-ux-pro-max")


class HookContractTests(unittest.TestCase):
    def run_hook(self, payload):
        env = dict(os.environ, SKILL_ROUTER_DATA=TMP)
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "route.py")], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=10)
        return p, (time.time() - t0) * 1000

    def test_json_shape(self):
        p, _ = self.run_hook({"prompt": "add a new tab screen with expo router", "cwd": CWD,
                              "session_id": "shape-%f" % time.time(), "hook_event_name": "UserPromptSubmit"})
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("expo-router", out["hookSpecificOutput"]["additionalContext"])

    def test_garbage_input_is_silent(self):
        for raw in ["", "not json", "[]", json.dumps({"prompt": 42})]:
            env = dict(os.environ, SKILL_ROUTER_DATA=TMP)
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "route.py")], input=raw,
                               capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout, "")

    def test_latency(self):
        times = []
        for i in range(5):
            _, ms = self.run_hook({"prompt": "write a postgres migration adding an index", "cwd": CWD,
                                   "session_id": "lat-%d-%f" % (i, time.time())})
            times.append(ms)
        sys.stderr.write("\n[latency] hook wall ms: %s\n" % ", ".join("%.0f" % t for t in times))
        self.assertLess(sorted(times)[len(times) // 2], 300)


class MatchingFeatureTests(unittest.TestCase):
    def test_direction_features(self):
        d = srlib.directional("push this page into Figma")
        self.assertIn("to:figma", d)
        d = srlib.directional("turn this Figma frame into code")
        self.assertIn("from:figma", d)
        self.assertIn("to:code", d)

    def test_negative_clause_split(self):
        pos, neg = srlib.split_negative("Generate images and video. NOT for: TTS or games. Use when asked.")
        self.assertNotIn("TTS", pos)
        self.assertIn("TTS", neg)
        self.assertIn("Use when asked", pos)
        _, neg = srlib.split_negative("Design guidance. Do NOT call it for dashboards.")
        self.assertIn("dashboards", neg)

    def test_aliases(self):
        self.assertIn("kubernet", srlib.tokens("add a k8s ingress"))
        self.assertIn("terraform", srlib.tokens("tf plan"))
        self.assertIn("github", srlib.tokens("gh actions"))
        self.assertIn("postgr", srlib.tokens("pg index"))

    def test_platform_gate(self):
        if "vercel:status" not in NAMES:
            self.skipTest("vercel plugin not installed")
        plain = tempfile.mkdtemp(dir=TMP)
        self.assertNotIn("vercel:status", names_in(suggest("what's the status of the deploy?", cwd=plain)))
        proj = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(proj, "vercel.json"), "w") as f:
            f.write("{}")
        self.assertIn("vercel:status", names_in(suggest("what's the status of the deploy?", cwd=proj)))

    def test_short_prompt_needs_rare_hit(self):
        self.assertIsNone(suggest("fix the bug"))
        if "eas-app-stores" in NAMES:
            self.assertIn("eas-app-stores", names_in(suggest("upload the build to testflight")))


class FixRuleTests(unittest.TestCase):
    """0.2.1 rules: conversion targets, query-performance signals, opt-in families, language evidence."""

    def proj(self, files):
        d = tempfile.mkdtemp(dir=TMP)
        for fn, content in files.items():
            with open(os.path.join(d, fn), "w") as f:
                f.write(content)
        return d

    def test_conversion_classes(self):
        d = srlib.directional("translate this Figma mockup into a Next.js page")
        self.assertIn("to:@code", d)
        self.assertIn("from:@design", d)
        self.assertIn("to:@design", srlib.directional("push my React login screen into Figma"))
        self.assertIn("from:@visual", srlib.directional("convert this screenshot into HTML and CSS"))
        self.assertEqual(srlib.name_conversion("image-to-code"), [["@visual"], ["@code"]])

    def test_query_perf_only_for_sql_explain(self):
        perf = srlib.expand(srlib.tokens("look at the EXPLAIN output"), "look at the EXPLAIN output")
        self.assertIn("query", perf)
        self.assertEqual(srlib.expand(srlib.tokens("explain this to me"), "explain this to me"), [])
        self.assertIsNone(suggest("explain this to me"))
        self.assertIsNone(suggest("can you explain how this function works step by step"))

    def test_opt_in_family(self):
        if "caveman-commit" not in NAMES:
            self.skipTest("caveman skills not installed")
        self.assertNotIn("caveman-commit", names_in(suggest("write a commit message for this")))
        self.assertIn("caveman-commit", names_in(suggest("write a terse caveman commit message")))

    def test_language_evidence(self):
        if "python-pro" not in NAMES:
            self.skipTest("python-pro not installed")
        self.assertNotIn("python-pro", names_in(suggest("add error handling to the fetch call")))
        py = self.proj({"pyproject.toml": "[project]\nname='x'\n"})
        plats = route.resolve_platforms({"lang-python"}, set(), json.load(open(os.path.join(ROOT, "routes.json"))))
        self.assertIn("lang-python", plats)
        # contradictory: project says python, prompt says typescript -> python not in force
        routes = json.load(open(os.path.join(ROOT, "routes.json")))
        self.assertNotIn("lang-python", route.resolve_platforms({"lang-python"}, {"lang-typescript"}, routes))
        self.assertTrue(os.path.isdir(py))


class ParserTests(unittest.TestCase):
    def test_frontmatter_variants(self):
        cases = {
            "---\nname: a\ndescription: plain text here\n---\n": "plain text here",
            '---\ndescription: "quoted: with colon \\"x\\""\n---\n': 'quoted: with colon "x"',
            "---\ndescription: 'single ''q'''\n---\n": "single 'q'",
            "---\ndescription: >\n  folded line one\n  line two\nother: x\n---\n": "folded line one line two",
            "---\ndescription: |\n  literal one\n  literal two\n---\n": "literal one\nliteral two",
            "---\ndescription: starts here\n  and continues\n---\n": "starts here and continues",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(srlib.parse_frontmatter(text).get("description"), want)

    def test_skip_gates(self):
        for p in ["/compact", "ok", "go on", "do it", "✨", "yes please", "lgtm", "ok, go ahead", "thanks!"]:
            self.assertTrue(route.should_skip(p), p)
        self.assertFalse(route.should_skip("add a new tab screen with expo router"))


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
