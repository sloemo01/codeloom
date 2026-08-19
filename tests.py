#!/usr/bin/env python3
"""Tests for codemap. Run with: python3 tests.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import codemap  # noqa: E402


def make_repo(repo):
    """Create a small fixture repo."""
    def j(*parts):
        return os.path.join(repo, *parts)
    def w(path, content):
        with open(path, "w") as f:
            f.write(content)
    os.makedirs(j("src", "core"))
    os.makedirs(j("tests"))
    w(j("src", "__init__.py"), "")
    w(j("src", "cli.py"),
      "import os\n"
      "def main():\n    pass\n"
      "\n"
      "class Parser:\n    pass\n")
    w(j("src", "core", "engine.py"),
      "class Engine:\n    def run(self):\n        pass\n")
    w(j("tests", "test_cli.py"), "def test_x():\n    pass\n")
    w(j("README.md"), "# demo\n")
    w(j(".gitignore"), "*.pyc\n.venv/\n__pycache__/\n")
    os.makedirs(j(".venv"))
    w(j(".venv", "junk.py"), "ignored")
    w(j("ignored.pyc"), "ignored")


class TestCodemap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        make_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_file_count_and_gitignore(self):
        m = codemap.build_map(self.repo, True, 5000)
        # .venv/junk.py and ignored.pyc excluded -> 6 files
        self.assertEqual(m["file_count"], 6)

    def test_outline_python(self):
        m = codemap.build_map(self.repo, True, 5000)
        text = codemap.render_text(m)
        self.assertIn("def main():", text)
        self.assertIn("class Parser:", text)
        self.assertIn("class Engine:", text)

    def test_entry_points_prefer_root(self):
        m = codemap.build_map(self.repo, True, 5000)
        eps = [os.path.basename(e) for e in m["entry_points"]]
        self.assertIn("README.md", eps)

    def test_json_roundtrip(self):
        m = codemap.build_map(self.repo, True, 5000)
        payload = {
            "root": m["root"],
            "file_count": m["file_count"],
            "tree": codemap.tree_to_json(m["tree"]),
        }
        import json
        json.dumps(payload)  # must not raise

    def test_cli_runs(self):
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codemap.py"),
             "--json", self.repo],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("file_count", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
