"""Labeled routing eval (tests/eval_set.json) as unittest assertions.

Runs against the skills installed on this machine. Skips when most labeled
skills are missing (e.g. CI or another user's machine), since the repo is public.
Print the full report with: python3 tests/eval_lib.py -v --holdout"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_lib  # noqa: E402


class EvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = eval_lib.Harness()
        cls.data = eval_lib.load_set()
        if cls.h.coverage(cls.data["dev"]) < 0.8:
            cls.h.close()
            raise unittest.SkipTest("most labeled skills are not installed here")
        cls.m = eval_lib.metrics(cls.h, cls.data)
        sys.stderr.write("\n[eval] dev top1=%.0f%% hit@5=%.0f%% | holdout top1=%.0f%% hit@5=%.0f%% | "
                         "silent false-fire=%.0f%% | everyday fire=%.0f%% avg=%.0f | all avg=%.0f max=%d\n" % (
                             cls.m["dev"]["top1"] * 100, cls.m["dev"]["hit5"] * 100,
                             cls.m["holdout"]["top1"] * 100, cls.m["holdout"]["hit5"] * 100,
                             cls.m["silent"]["false_fire"] * 100, cls.m["everyday"]["fire"] * 100,
                             cls.m["everyday"]["avg_chars"], cls.m["all"]["avg_chars"], cls.m["all"]["max_chars"]))

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_dev_quality(self):
        self.assertGreaterEqual(self.m["dev"]["top1"], 0.85)
        self.assertGreaterEqual(self.m["dev"]["hit5"], 0.9)

    def test_holdout_quality(self):
        # holdout was frozen before tuning; the bar is lower on purpose
        self.assertGreaterEqual(self.m["holdout"]["hit5"], 0.6)

    def test_silent_set(self):
        self.assertLessEqual(self.m["silent"]["false_fire"], 0.1, self.m["silent"]["fires"])

    def test_budget(self):
        self.assertLessEqual(self.m["everyday"]["fire"], 0.35)
        self.assertLessEqual(self.m["all"]["avg_chars"], 80)
        self.assertLessEqual(self.m["all"]["max_chars"], 400)


if __name__ == "__main__":
    unittest.main()
