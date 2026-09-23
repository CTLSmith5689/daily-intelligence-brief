"""Code that only runs on failure or on a schedule still has to parse.

The failure alert was once seventeen lines of Python inside the workflow YAML.
Nothing parsed it until a failure ran it, and it was a SyntaxError, so for a
day the only channel that reports a failure could not report anything. These
tests parse every Python file in the repository, every JSON file a person
edits, and every piece of Python embedded in a workflow, and they drive the
alert function itself with SMTP replaced by a recorder.

Also here: the em dash check, which fails only on occurrences beyond the
checked-in baseline, so the existing ones do not block and new ones cannot land.
"""
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF

try:
    import yaml  # PyYAML is not a dependency; used only when present.
except ImportError:
    yaml = None


def repo_files(suffix):
    files = H.tracked_files()
    if files is None:
        files = [str(p.relative_to(H.REPO)) for p in H.REPO.rglob("*" + suffix)
                 if ".git" not in p.parts]
    return sorted(f for f in files if f.endswith(suffix))


def workflow_files():
    return sorted((H.REPO / ".github" / "workflows").glob("*.yml"))


class EverythingParses(unittest.TestCase):

    def test_every_python_file_compiles(self):
        files = repo_files(".py")
        self.assertIn("lambda_function.py", files)
        self.assertIn("theses/bin/ingest.py", files)
        for rel in files:
            with self.subTest(file=rel):
                src = (H.REPO / rel).read_text(encoding="utf-8")
                compile(src, rel, "exec")

    def test_every_edited_json_file_loads(self):
        for rel in repo_files(".json"):
            if rel.startswith(("data/", "docs/", "state/")):
                continue
            with self.subTest(file=rel):
                json.loads((H.REPO / rel).read_text(encoding="utf-8"))


class Workflows(unittest.TestCase):

    def test_run_reader_agrees_with_pyyaml(self):
        if yaml is None:
            self.skipTest("PyYAML not installed; the fallback reader is used alone")
        for path in workflow_files():
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                doc = yaml.safe_load(text)
                runs = [st["run"] for job in doc["jobs"].values()
                        for st in job.get("steps", []) if "run" in st]
                self.assertEqual(H.workflow_run_blocks(text), runs)

    def test_inline_python_compiles_and_resolves(self):
        seen = 0
        for path in workflow_files():
            for run in H.workflow_run_blocks(path.read_text(encoding="utf-8")):
                for kind, src in H.python_snippets(run):
                    seen += 1
                    with self.subTest(file=path.name, kind=kind, src=src[:60]):
                        compile(src, f"{path.name}:{kind}", "exec")
                        # Names reached through the pipeline module must exist.
                        for alias in re.findall(r"import\s+lambda_function\s+as\s+(\w+)", src):
                            for attr in re.findall(r"\b" + alias + r"\.(\w+)", src):
                                self.assertTrue(hasattr(LF, attr),
                                                f"lambda_function has no {attr}")
        # brief.yml's alert, keepalive's checks and the cache guard heredoc.
        self.assertGreaterEqual(seen, 4, "fewer inline snippets found than exist")

    def test_brief_workflow_runs_the_suite_before_gather(self):
        text = (H.REPO / ".github" / "workflows" / "brief.yml").read_text(encoding="utf-8")
        tests_at = text.find("python -m unittest discover -s tests")
        gather_at = text.find("- name: Gather")
        self.assertGreater(tests_at, 0)
        self.assertLess(tests_at, gather_at)


class FailureAlert(unittest.TestCase):

    def test_alert_builds_and_sends_offline(self):
        self.assertTrue(callable(LF.send_failure_alert))
        smtp = mock.MagicMock()
        with mock.patch.object(LF.smtplib, "SMTP", smtp), \
                mock.patch.dict(os.environ, {"APTERREON_ICLOUD_APP_PASSWORD": "x"}), \
                H.quiet():
            LF.send_failure_alert("record", "failure", "https://example.invalid/run/1")
        server = smtp.return_value.__enter__.return_value
        server.sendmail.assert_called_once()
        message = server.sendmail.call_args[0][2]
        self.assertIn("FAILURE (record)", message)


class EmDash(unittest.TestCase):

    def test_no_new_em_dashes(self):
        files = H.tracked_files()
        if files is None:
            self.skipTest("not a git checkout")
        baseline = json.loads(H.EM_DASH_BASELINE.read_text(encoding="utf-8"))
        counts = H.em_dash_counts(files)
        grown = {f: (baseline.get(f, 0), n) for f, n in counts.items()
                 if n > baseline.get(f, 0)}
        self.assertEqual(grown, {}, "em dashes added (file: (allowed, found)); "
                         "rewrite them, do not raise the baseline")

    def test_suite_itself_has_none(self):
        for path in sorted((H.REPO / "tests").glob("*")):
            if path.is_file():
                with self.subTest(file=path.name):
                    self.assertNotIn(H.EM_DASH, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
