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
    os.makedirs(j("src", "utils"))
    os.makedirs(j("tests"))
    w(j("src", "__init__.py"), "")
    w(j("src", "cli.py"),
      "from core.engine import Engine\n"
      "from utils.retry import retry\n"
      "\n"
      "def main():\n    eng = Engine()\n    retry(eng.run)\n"
      "\n"
      "class Parser:\n    pass\n")
    w(j("src", "core", "__init__.py"), "")
    w(j("src", "core", "engine.py"),
      "from utils.retry import retry\n"
      "\n"
      "class Engine:\n    def run(self):\n        retry(lambda: None)\n")
    w(j("src", "utils", "__init__.py"), "")
    w(j("src", "utils", "retry.py"),
      "def retry(fn, tries=3):\n    return fn()\n")
    w(j("tests", "test_cli.py"),
      "from core.engine import Engine\n"
      "def test_x():\n    Engine()\n")
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
        # .venv/junk.py and ignored.pyc excluded; 9 real files remain
        self.assertEqual(m["file_count"], 9)

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

    def test_graph_import_edges(self):
        m = codemap.build_map(self.repo, True, 5000)
        files = []
        # re-walk (build_map doesn't return files list)
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codemap.build_graph(files, self.repo)
        # src.cli -> src.core.engine and src.utils.retry
        self.assertIn("src.core.engine", graph.get("src.cli", set()))
        self.assertIn("src.utils.retry", graph.get("src.cli", set()))
        # tests.test_cli -> src.core.engine
        self.assertIn("src.core.engine", graph.get("tests.test_cli", set()))

    def test_graph_focus(self):
        m = codemap.build_map(self.repo, True, 5000)
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codemap.build_graph(files, self.repo)
        fs = codemap.focus_subgraph(graph, "src.core.engine")
        self.assertIn("src.cli", fs["depended_on_by"])
        self.assertIn("tests.test_cli", fs["depended_on_by"])
        self.assertIn("src.utils.retry", fs["depends_on"])

    def test_call_graph(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        calls = codemap.build_call_graph(files, self.repo)
        # src.cli.main calls retry (function defined in src.utils.retry)
        self.assertIn("retry", calls.get("src.cli", {}).get("main", set()))
        # src.core.engine.run calls retry
        self.assertIn("retry", calls.get("src.core.engine", {}).get("run", set()))
        # builtins like len/str should NOT appear (filtered out)
        self.assertNotIn("len", calls.get("src.cli", {}).get("main", set()))

    def test_multi_lang_call_graph(self):
        # JS + Go fixture
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "app.js"), "w") as f:
                f.write("function greet(n) { return n; }\nfunction main() { greet('x'); }\n")
            with open(os.path.join(tmp, "main.go"), "w") as f:
                f.write("package main\nfunc helper() int { return 1 }\nfunc main() { helper() }\n")
            files = [os.path.join(tmp, "app.js"), os.path.join(tmp, "main.go")]
            calls = codemap.build_call_graph_multi(files, tmp)
            self.assertIn("greet", calls.get("app", {}).get("main", set()))
            self.assertIn("helper", calls.get("main", {}).get("main", set()))
        finally:
            shutil.rmtree(tmp)

    def test_install_agents(self):
        # creates AGENTS.md
        msg = codemap.install_agents(self.repo)
        self.assertIn("AGENTS.md", msg)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "AGENTS.md")))
        # updates on second call
        msg2 = codemap.install_agents(self.repo)
        self.assertIn("updated", msg2)

    def test_token_estimate(self):
        self.assertGreater(codemap.estimate_tokens("hello world"), 0)
        self.assertEqual(codemap.estimate_tokens(""), 1)

    def test_impact_analysis(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codemap.build_graph(files, self.repo)
        imp = codemap.impact_analysis(graph, "src.core.engine")
        # src.cli and tests.test_cli depend on engine
        self.assertIn("src.cli", imp["direct_dependents"])
        self.assertIn("tests.test_cli", imp["direct_dependents"])
        # engine depends on retry
        self.assertIn("src.utils.retry", imp["depends_on"])

    def test_task_relevance(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codemap.task_relevance(files, self.repo, "retry logic")
        self.assertTrue(results)
        # retry module should rank first for 'retry' task
        self.assertEqual(results[0]["module"], "src.utils.retry")

    def test_build_plan(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        plan = codemap.build_plan(files, self.repo, "add retry to engine")
        self.assertIn("Read these files", plan)
        self.assertIn("src/core/engine.py", plan)

    def test_cross_call_graph(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        calls = codemap.build_cross_call_graph(files, self.repo)
        # src.cli.main calls Engine (class in src.core.engine) and retry
        self.assertIn("src.core.engine.Engine", calls.get("src.cli", {}).get("main", set()))
        self.assertIn("src.utils.retry.retry", calls.get("src.cli", {}).get("main", set()))
        # src.core.engine.run calls retry
        self.assertIn("src.utils.retry.retry", calls.get("src.core.engine", {}).get("run", set()))

    def test_symbol_search(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        index = codemap.build_symbol_index(files, self.repo)
        results = codemap.search_symbols(index, "Engine")
        self.assertTrue(results)
        self.assertEqual(results[0]["name"], "Engine")
        self.assertEqual(results[0]["kind"], "class")
        # search for a method
        results2 = codemap.search_symbols(index, "run")
        self.assertTrue(results2)

    def test_gitignore_negation_and_anchoring(self):
        # create a .gitignore with negation + anchoring
        gi = os.path.join(self.repo, ".gitignore")
        with open(gi, "w") as f:
            f.write("*.pyc\n/build\n!keep.pyc\nnode_modules/\n")
        rules = codemap.parse_gitignore(gi)
        # *.pyc ignored
        self.assertTrue(codemap.is_ignored(os.path.join(self.repo, "a.pyc"), rules))
        # /build anchored ignores build/out.js
        self.assertTrue(codemap.is_ignored(os.path.join(self.repo, "build", "out.js"), rules))
        # node_modules/ dir-only ignores contents
        self.assertTrue(codemap.is_ignored(os.path.join(self.repo, "node_modules", "x.js"), rules))
        # !keep.pyc negation keeps it
        self.assertFalse(codemap.is_ignored(os.path.join(self.repo, "keep.pyc"), rules))

    def test_incremental_cache(self):
        # first run: all files changed
        files = [os.path.join(self.repo, "src", "cli.py")]
        cache = codemap.load_cache(self.repo)
        changed = codemap.changed_files(files, cache)
        self.assertEqual(len(changed), 1)
        # update cache, second run: no changes
        codemap.update_cache(files, cache)
        changed2 = codemap.changed_files(files, cache)
        self.assertEqual(len(changed2), 0)

    def test_multi_lang_import_graph(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "app.js"), "w") as f:
                f.write('import { helper } from "./util";\nfunction main() { helper(); }\n')
            with open(os.path.join(tmp, "util.js"), "w") as f:
                f.write("export function helper() { return 1; }\n")
            files = [os.path.join(tmp, "app.js"), os.path.join(tmp, "util.js")]
            graph = codemap.build_graph_multi(files, tmp)
            self.assertIn("util", graph.get("app", set()))
        finally:
            shutil.rmtree(tmp)

    def test_verify_sha256(self):
        path = os.path.join(self.repo, "src", "cli.py")
        digest = codemap.sha256_file(path)
        self.assertEqual(len(digest), 64)  # sha256 hex is 64 chars

    def test_search_snippet(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        index = codemap.build_symbol_index(files, self.repo)
        results = codemap.search_symbols(index, "Engine")
        self.assertTrue(results)
        self.assertIn("snippet", results[0])
        self.assertTrue(results[0]["snippet"])

    def test_find_usages(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        usages = codemap.find_usages(files, self.repo, "retry")
        # retry is used in src.cli.main and src.core.engine.run
        mods = {u["module"] for u in usages}
        self.assertIn("src.cli", mods)
        self.assertIn("src.core.engine", mods)


if __name__ == "__main__":
    unittest.main(verbosity=2)
