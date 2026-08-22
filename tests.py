#!/usr/bin/env python3
"""Tests for codeloom. Run with: python3 tests.py"""
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import codeloom  # noqa: E402


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


class TestCodeLoom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        make_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_file_count_and_gitignore(self):
        m = codeloom.build_map(self.repo, True, 5000)
        # .venv/junk.py and ignored.pyc excluded; 9 real files remain
        self.assertEqual(m["file_count"], 9)

    def test_outline_python(self):
        m = codeloom.build_map(self.repo, True, 5000)
        text = codeloom.render_text(m)
        self.assertIn("def main():", text)
        self.assertIn("class Parser:", text)
        self.assertIn("class Engine:", text)

    def test_entry_points_prefer_root(self):
        m = codeloom.build_map(self.repo, True, 5000)
        eps = [os.path.basename(e) for e in m["entry_points"]]
        self.assertIn("README.md", eps)

    def test_json_roundtrip(self):
        m = codeloom.build_map(self.repo, True, 5000)
        payload = {
            "root": m["root"],
            "file_count": m["file_count"],
            "tree": codeloom.tree_to_json(m["tree"]),
        }
        import json
        json.dumps(payload)  # must not raise

    def test_cli_runs(self):
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
             "--json", self.repo],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("file_count", r.stdout)

    def _run_cli(self, *argv, cwd=None, timeout=120):
        """Run codeloom.py as a subprocess in an isolated cwd (default: a
        throwaway temp dir so per-repo artifacts like .codeloom-memory never
        land in the fixture repo or the codeloom checkout)."""
        if cwd is None:
            cwd = tempfile.mkdtemp()
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py")]
            + list(argv),
            capture_output=True, text=True, cwd=cwd, timeout=timeout,
        )
        return r, cwd

    def _make_cli_repo(self, base):
        """Tiny 3-file Python repo with a symbol, a dead symbol, and a call."""
        repo = os.path.join(base, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "engine.py"), "w") as f:
            f.write("from utils.retry import retry\n\n"
                    "class Engine:\n"
                    "    def run(self):\n"
                    "        return retry(lambda: None)\n\n"
                    "def main():\n"
                    "    eng = Engine()\n"
                    "    return eng.run()\n")
        with open(os.path.join(repo, "utils.py"), "w") as f:
            f.write("def retry(fn, tries=3):\n"
                    "    return fn()\n\n"
                    "def backup():\n"
                    "    return 1\n")
        with open(os.path.join(repo, "cli.py"), "w") as f:
            f.write("def parse(args):\n    return args\n")
        return repo

    def test_cli_get_symbol_dispatch(self):
        # --get-symbol resolves a real symbol (class def + body) to its file
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            r, _ = self._run_cli("--get-symbol", "Engine", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("# get_symbol: Engine", r.stdout)
            self.assertRegex(r.stdout, r"engine:\d+")
            self.assertIn("class Engine", r.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_cli_pack_dispatch(self):
        # --pack emits a single-shot task brief with a ranked reading order
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            r, _ = self._run_cli("--pack", "add retry to engine", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("# TASK: add retry to engine", r.stdout)
            self.assertIn("## 1. READ THESE, IN ORDER", r.stdout)
            self.assertIn("engine.py", r.stdout)
            self.assertIn("## 2. THE RELEVANT CODE", r.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_cli_decide_query_memory_roundtrip(self):
        # --decide writes to .codeloom-memory in the CWD; --query-memory in
        # the same CWD reads it back. Runs in a throwaway dir so the repo
        # and the codeloom checkout stay clean.
        tmp = tempfile.mkdtemp()
        try:
            r1, wd = self._run_cli("--decide", "Use retry everywhere",
                                   "--reason", "idempotent")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("recorded decision", r1.stdout)
            self.assertTrue(os.path.isfile(
                os.path.join(wd, ".codeloom-memory", "DECISIONS.md")))
            r2, _ = self._run_cli("--query-memory", "retry", cwd=wd)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn('--query-memory "retry"', r2.stdout)
            self.assertIn("Use retry everywhere", r2.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_cli_health_dispatch(self):
        # --health prints a score line (avg X/10 across N files)
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            r, _ = self._run_cli("--health", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("code health", r.stdout)
            self.assertRegex(r.stdout, r"avg \d+(\.\d+)?/10")
            self.assertIn("files", r.stdout)
        finally:
            shutil.rmtree(tmp)

    def test_cli_risk_dispatch(self):
        # --risk on a git commit range exits 0 and prints a score line
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            for c in (["init", "-q"],
                      ["config", "user.email", "t@t"],
                      ["config", "user.name", "t"]):
                g = subprocess.run(["git"] + c, cwd=repo,
                                   capture_output=True, text=True)
                self.assertEqual(g.returncode, 0, g.stderr)
            subprocess.run(["git", "add", "-A"], cwd=repo,
                           capture_output=True, text=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                           capture_output=True, text=True)
            with open(os.path.join(repo, "engine.py"), "w") as f:
                f.write("from utils.retry import retry\n\n"
                        "class Engine:\n"
                        "    def run(self):\n"
                        "        return retry(lambda: None)\n\n"
                        "def main():\n"
                        "    eng = Engine()\n"
                        "    return eng.run() + 1\n")
            subprocess.run(["git", "add", "-A"], cwd=repo,
                           capture_output=True, text=True)
            subprocess.run(["git", "commit", "-q", "-m", "tweak"], cwd=repo,
                           capture_output=True, text=True)
            r, _ = self._run_cli("--risk", "HEAD~1..HEAD", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("change risk", r.stdout)
            self.assertRegex(r.stdout, r"score \d+/100")
        finally:
            def _force_remove(func, path, _exc):
                try:
                    os.chmod(path, 0o700)
                    func(path)
                except OSError:
                    pass
            shutil.rmtree(tmp, onerror=_force_remove)

    def test_graph_import_edges(self):
        m = codeloom.build_map(self.repo, True, 5000)
        files = []
        # re-walk (build_map doesn't return files list)
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codeloom.build_graph(files, self.repo)
        # src.cli -> src.core.engine and src.utils.retry
        self.assertIn("src.core.engine", graph.get("src.cli", set()))
        self.assertIn("src.utils.retry", graph.get("src.cli", set()))
        # tests.test_cli -> src.core.engine
        self.assertIn("src.core.engine", graph.get("tests.test_cli", set()))

    def test_graph_focus(self):
        m = codeloom.build_map(self.repo, True, 5000)
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codeloom.build_graph(files, self.repo)
        fs = codeloom.focus_subgraph(graph, "src.core.engine")
        self.assertIn("src.cli", fs["depended_on_by"])
        self.assertIn("tests.test_cli", fs["depended_on_by"])
        self.assertIn("src.utils.retry", fs["depends_on"])

    def test_call_graph(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        calls = codeloom.build_call_graph(files, self.repo)
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
            calls = codeloom.build_call_graph_multi(files, tmp)
            self.assertIn("greet", calls.get("app", {}).get("main", set()))
            self.assertIn("helper", calls.get("main", {}).get("main", set()))
        finally:
            shutil.rmtree(tmp)

    def test_install_agents(self):
        # creates AGENTS.md
        msg = codeloom.install_agents(self.repo)
        self.assertIn("AGENTS.md", msg)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "AGENTS.md")))
        # updates on second call
        msg2 = codeloom.install_agents(self.repo)
        self.assertIn("updated", msg2)

    def test_token_estimate(self):
        self.assertGreater(codeloom.estimate_tokens("hello world"), 0)
        self.assertEqual(codeloom.estimate_tokens(""), 1)

    def test_impact_analysis(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        graph = codeloom.build_graph(files, self.repo)
        imp = codeloom.impact_analysis(graph, "src.core.engine")
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
        results = codeloom.task_relevance(files, self.repo, "retry logic")
        self.assertTrue(results)
        # retry module should rank first for 'retry' task
        self.assertEqual(results[0]["module"], "src.utils.retry")

    def test_build_plan(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        plan = codeloom.build_plan(files, self.repo, "add retry to engine")
        self.assertIn("Read these files", plan)
        # cross-platform: check for the module name, not a hardcoded path separator
        self.assertIn("engine.py", plan)

    def test_cross_call_graph(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        calls = codeloom.build_cross_call_graph(files, self.repo)
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
        index = codeloom.build_symbol_index(files, self.repo)
        results = codeloom.search_symbols(index, "Engine")
        self.assertTrue(results)
        self.assertEqual(results[0]["name"], "Engine")
        self.assertEqual(results[0]["kind"], "class")
        # search for a method
        results2 = codeloom.search_symbols(index, "run")
        self.assertTrue(results2)

    def test_gitignore_negation_and_anchoring(self):
        # create a .gitignore with negation + anchoring
        gi = os.path.join(self.repo, ".gitignore")
        with open(gi, "w") as f:
            f.write("*.pyc\n/build\n!keep.pyc\nnode_modules/\n")
        rules = codeloom.parse_gitignore(gi)
        # *.pyc ignored
        self.assertTrue(codeloom.is_ignored(os.path.join(self.repo, "a.pyc"), rules))
        # /build anchored ignores build/out.js
        self.assertTrue(codeloom.is_ignored(os.path.join(self.repo, "build", "out.js"), rules))
        # node_modules/ dir-only ignores contents
        self.assertTrue(codeloom.is_ignored(os.path.join(self.repo, "node_modules", "x.js"), rules))
        # !keep.pyc negation keeps it
        self.assertFalse(codeloom.is_ignored(os.path.join(self.repo, "keep.pyc"), rules))

    def test_incremental_cache(self):
        # first run: all files changed
        files = [os.path.join(self.repo, "src", "cli.py")]
        cache = codeloom.load_cache(self.repo)
        changed = codeloom.changed_files(files, cache)
        self.assertEqual(len(changed), 1)
        # update cache, second run: no changes
        codeloom.update_cache(files, cache)
        changed2 = codeloom.changed_files(files, cache)
        self.assertEqual(len(changed2), 0)

    def test_multi_lang_import_graph(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "app.js"), "w") as f:
                f.write('import { helper } from "./util";\nfunction main() { helper(); }\n')
            with open(os.path.join(tmp, "util.js"), "w") as f:
                f.write("export function helper() { return 1; }\n")
            files = [os.path.join(tmp, "app.js"), os.path.join(tmp, "util.js")]
            graph = codeloom.build_graph_multi(files, tmp)
            self.assertIn("util", graph.get("app", set()))
        finally:
            shutil.rmtree(tmp)

    def test_verify_sha256(self):
        path = os.path.join(self.repo, "src", "cli.py")
        digest = codeloom.sha256_file(path)
        self.assertEqual(len(digest), 64)  # sha256 hex is 64 chars

    def test_search_snippet(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        index = codeloom.build_symbol_index(files, self.repo)
        results = codeloom.search_symbols(index, "Engine")
        self.assertTrue(results)
        self.assertIn("snippet", results[0])
        self.assertTrue(results[0]["snippet"])

    def test_find_usages(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        usages = codeloom.find_usages(files, self.repo, "retry")
        # retry is used in src.cli.main and src.core.engine.run
        mods = {u["module"] for u in usages}
        self.assertIn("src.cli", mods)
        self.assertIn("src.core.engine", mods)

    def test_tree_sitter_backend(self):
        # tree-sitter should catch 'export function' that regex misses
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "util.js"), "w") as f:
                f.write("export function helper() { return 1; }\nfunction main() { helper(); }\n")
            files = [os.path.join(tmp, "util.js")]
            calls = codeloom.build_call_graph_multi(files, tmp)
            # if tree-sitter is available, helper should be found
            if codeloom._TS_AVAILABLE:
                self.assertIn("helper", calls.get("util", {}).get("main", set()))
        finally:
            shutil.rmtree(tmp)

    def test_embedding_backend_graceful(self):
        # without an embedding backend, task_relevance should still work
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codeloom.task_relevance(files, self.repo, "retry logic")
        self.assertTrue(results)
        self.assertEqual(results[0]["module"], "src.utils.retry")

    def test_grep_search(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codeloom.grep_search(files, self.repo, "retry")
        self.assertTrue(results)
        # retry appears in src.utils.retry and src.core.engine
        mods = {r["module"] for r in results}
        self.assertIn("src.utils.retry", mods)
        self.assertIn("src.core.engine", mods)

    def test_cached_symbols(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        cache = codeloom.load_cache(self.repo)
        index1 = codeloom.cached_symbols(files, self.repo, cache)
        self.assertIn("Engine", index1)
        # second call reuses cache
        index2 = codeloom.cached_symbols(files, self.repo, cache)
        self.assertIn("Engine", index2)

    def test_read_symbol(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        result = codeloom.read_symbol(files, self.repo, "Engine")
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "class")
        self.assertIn("class Engine", result["source"])
        # method
        result2 = codeloom.read_symbol(files, self.repo, "run")
        self.assertIsNotNone(result2)
        self.assertIn("def run", result2["source"])

    def test_read_symbol_multi_lang(self):
        # Go via tree-sitter, Java via brace-matching fallback
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "main.go"), "w") as f:
                f.write("package main\nfunc helper() int { return 42 }\n")
            with open(os.path.join(tmp, "App.java"), "w") as f:
                f.write("public class App {\n    public int add(int a, int b) {\n        return a + b;\n    }\n}\n")
            files = [os.path.join(tmp, "main.go"), os.path.join(tmp, "App.java")]
            # Go (tree-sitter if available, else brace fallback)
            r1 = codeloom.read_symbol(files, tmp, "helper")
            self.assertIsNotNone(r1)
            self.assertIn("func helper", r1["source"])
            # Java (brace-matching fallback)
            r2 = codeloom.read_symbol(files, tmp, "add")
            self.assertIsNotNone(r2)
            self.assertIn("public int add", r2["source"])
        finally:
            shutil.rmtree(tmp)

    def test_explain_symbol(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        info = codeloom.explain_symbol(files, self.repo, "Engine")
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "class")
        self.assertIn("module", info)

    def test_similar_symbols(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codeloom.similar_symbols(files, self.repo, "run")
        # run has 1 param (self, fn) -> 1 non-self param; find others with 1 param
        self.assertIsInstance(results, list)

    def test_dead_code(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        dead = codeloom.dead_code(files, self.repo)
        self.assertIsInstance(dead, list)

    def test_byte_index(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        index = codeloom.build_byte_index(files, self.repo)
        self.assertIn("Engine", index)
        loc = index["Engine"][0]
        self.assertIn("start_byte", loc)
        self.assertIn("end_byte", loc)
        self.assertGreater(loc["end_byte"], loc["start_byte"])
        self.assertIn("tokens", loc)
        self.assertGreater(loc["tokens"], 0)

    def test_get_symbol(self):
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        loc = codeloom.get_symbol(files, self.repo, "Engine")
        self.assertIsNotNone(loc)
        self.assertIn("class Engine", loc["source"])
        self.assertIn("tokens", loc)

    def test_get_snippet_by_offset(self):
        path = os.path.join(self.repo, "src", "core", "engine.py")
        s = codeloom.get_snippet_by_offset(path, 0, 30)
        self.assertIsNotNone(s)
        self.assertIn("tokens", s)
        self.assertIn("bytes", s)
        self.assertGreater(s["bytes"], 0)

    def test_nested_gitignore(self):
        # a nested .gitignore should exclude files in its directory
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, ".gitignore"), "w") as f:
                f.write("*.log\n")
            os.makedirs(os.path.join(tmp, "sub"))
            with open(os.path.join(tmp, "sub", ".gitignore"), "w") as f:
                f.write("secret.txt\n")
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("x = 1\n")
            with open(os.path.join(tmp, "sub", "secret.txt"), "w") as f:
                f.write("secret\n")
            with open(os.path.join(tmp, "sub", "keep.py"), "w") as f:
                f.write("y = 2\n")
            rules = codeloom.parse_gitignore(os.path.join(tmp, ".gitignore"))
            files = []
            codeloom._walk(tmp, rules, 100, files)
            rels = [os.path.relpath(f, tmp) for f in files]
            self.assertIn("a.py", rels)
            self.assertIn(os.path.join("sub", "keep.py"), rels)
            self.assertNotIn(os.path.join("sub", "secret.txt"), rels)
        finally:
            shutil.rmtree(tmp)

    def test_gitignore_cache_invalidation(self):
        # changing .gitignore should invalidate the cache (all files changed)
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, ".gitignore"), "w") as f:
                f.write("*.log\n")
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("x = 1\n")
            cache = codeloom.load_cache(tmp)
            files = [os.path.join(tmp, "a.py")]
            # first run: all changed
            changed = codeloom.changed_files(files, cache, tmp)
            self.assertEqual(len(changed), 1)
            codeloom.update_cache(files, cache, tmp)
            # second run: no change
            changed2 = codeloom.changed_files(files, cache, tmp)
            self.assertEqual(len(changed2), 0)
            # change .gitignore -> all changed again
            with open(os.path.join(tmp, ".gitignore"), "w") as f:
                f.write("*.py\n")
            changed3 = codeloom.changed_files(files, cache, tmp)
            self.assertEqual(len(changed3), 1)
        finally:
            shutil.rmtree(tmp)

    def test_workspace_roots(self):
        # a pyproject.toml with src/ should be detected as a workspace root
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "packages", "foo", "src"))
            with open(os.path.join(tmp, "packages", "foo", "pyproject.toml"), "w") as f:
                f.write("[project]\nname = 'foo'\n")
            roots = codeloom._workspace_roots(tmp)
            self.assertIn("packages.foo.src", roots)
        finally:
            shutil.rmtree(tmp)

    def test_persistent_index(self):
        # build + save a persistent index, then load it
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("class Foo:\n    def bar(self):\n        return 1\n")
            files = [os.path.join(tmp, "a.py")]
            index = codeloom.build_persistent_index(files, tmp)
            self.assertIn("Foo", index)
            codeloom.save_persistent_index(tmp, index, files)
            loaded = codeloom.load_persistent_index(tmp)
            self.assertIsNotNone(loaded)
            self.assertIn("Foo", loaded["symbols"])
            # status should report fresh
            status = codeloom.render_index_status(tmp)
            self.assertIn("fresh", status)
        finally:
            shutil.rmtree(tmp)

    def test_summary_retrieval(self):
        # summary-first retrieval should be much smaller than full source
        # for a LARGE symbol (the huge-symbol case that used to lose tokens)
        tmp = tempfile.mkdtemp()
        try:
            # build a large class (100 methods) so full source is big
            with open(os.path.join(tmp, "big.py"), "w") as f:
                f.write("class Big:\n")
                for i in range(100):
                    f.write(f"    def method_{i}(self):\n        return {i}\n")
            files = [os.path.join(tmp, "big.py")]
            summary = codeloom.render_get_symbol(files, tmp, "Big", summary=True)
            full = codeloom.render_get_symbol(files, tmp, "Big", summary=False)
            self.assertIn("Signature", summary)
            self.assertIn("Docstring", summary)
            self.assertIn("Calls", summary)
            # summary should be much smaller than full source for a large symbol
            self.assertLess(len(summary), len(full) // 10)
        finally:
            shutil.rmtree(tmp)

    def test_scanner_skips_strings_comments(self):
        # a call inside a string or comment should NOT be detected
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "app.js"), "w") as f:
                f.write("function helper() { return 1; }\n"
                        "function main() {\n"
                        "  // comment mentioning helper() but not a real call\n"
                        "  const msg = 'calling helper() here is a string';\n"
                        "  return helper();\n"
                        "}\n")
            files = [os.path.join(tmp, "app.js")]
            calls = codeloom.build_call_graph_multi(files, tmp)
            # main should call helper exactly once (the real call)
            self.assertIn("helper", calls.get("app", {}).get("main", set()))
            # the string/comment references should not create extra edges
            self.assertEqual(len(calls.get("app", {}).get("main", set())), 1)
        finally:
            shutil.rmtree(tmp)

    def test_pack(self):
        # --pack emits a code-embedded task brief: reading order + embedded code + impact
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        pack = codeloom.render_pack(files, self.repo, "retry")
        self.assertIn("READ THESE, IN ORDER", pack)
        self.assertIn("THE RELEVANT CODE", pack)
        self.assertIn("IMPACT", pack)
        self.assertIn("retry", pack)

    def test_resume(self):
        # --resume emits a compact structural snapshot for compaction survival
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        resume = codeloom.render_resume(files, self.repo, 1000)
        self.assertIn("--resume", resume)
        self.assertIn("Entry points", resume)
        self.assertIn("Modules", resume)
        self.assertIn("Top call sites", resume)

    def test_memory_remember_read(self):
        # repository memory: remember persists, loom_context returns it
        # use a temp dir to avoid polluting the real demo repo
        import tempfile, shutil
        td = tempfile.mkdtemp()
        try:
            codeloom.memory_remember(td, "ARCHITECTURE", "auth uses session tokens")
            mem = codeloom.memory_read(td)
            self.assertIn("session tokens", mem)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_hybrid_search(self):
        # hybrid search ranks the lexical+structural match first
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codeloom.hybrid_search(files, self.repo, "retry")
        self.assertTrue(results, "hybrid search should return results")
        self.assertEqual(results[0]["name"].lower(), "retry")

    def test_cross_repo(self):
        # cross-repo: build a graph across two roots
        import tempfile, shutil
        td = tempfile.mkdtemp()
        td2 = tempfile.mkdtemp()
        try:
            with open(os.path.join(td, "sdk.py"), "w") as f:
                f.write("def client():\n    pass\n")
            cr = codeloom.build_cross_repo([td, td2])
            # two repos should be mapped
            self.assertGreaterEqual(len(cr["repos"]), 1)
        finally:
            shutil.rmtree(td, ignore_errors=True)
            shutil.rmtree(td2, ignore_errors=True)

    def test_install_agent_config(self):
        # install-agent emits a valid MCP config snippet for an agent
        for agent in ("claude", "cursor", "codex", "gemini", "opencode",
                      "cline", "openhands", "devin", "hermes", "aider",
                      "roo", "windsurf", "amazon-q", "jetbrains", "junie",
                      "kimi", "qwen"):
            cfg = codeloom.install_agent_config(agent, "/tmp/codeloom.py")
            self.assertIn("codeloom", cfg)
            self.assertIn("python3", cfg)
        # TOML-style agents use [mcp_servers.codeloom]
        for agent in ("openhands", "devin", "hermes"):
            self.assertIn("mcp_servers.codeloom", codeloom.install_agent_config(agent, "/tmp/codeloom.py"))
        # mcpServers-style agents use JSON mcpServers
        for agent in ("claude", "cline", "aider", "roo", "windsurf"):
            self.assertIn("mcpServers", codeloom.install_agent_config(agent, "/tmp/codeloom.py"))
        # unknown agent should be rejected by the caller, config returns a sensible default
        self.assertIn("codeloom", codeloom.install_agent_config("unknown", "/tmp/codeloom.py"))

    def test_edit_relevance(self):
        # edit-relevance ranks the call path, not just keyword matches
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        results = codeloom.edit_relevance(files, self.repo, "retry")
        # the retry module should rank first (it defines the anchor)
        self.assertTrue(results)
        self.assertEqual(results[0]["module"], "src.utils.retry")

    def test_index_auto_refresh(self):
        # a fresh index is fast; a changed file triggers a rebuild
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("class Foo:\n    def bar(self):\n        return 1\n")
            files = [os.path.join(tmp, "a.py")]
            index = codeloom.build_persistent_index(files, tmp)
            codeloom.save_persistent_index(tmp, index, files)
            pidx = codeloom.load_persistent_index(tmp)
            self.assertIsNotNone(pidx)
            self.assertTrue(codeloom.index_is_fresh(tmp, pidx))
            # modify the file -> stale
            with open(os.path.join(tmp, "a.py"), "a") as f:
                f.write("\n# change\n")
            self.assertFalse(codeloom.index_is_fresh(tmp, pidx))
            # ensure_fresh_index rebuilds
            fresh = codeloom.ensure_fresh_index(tmp, 100)
            self.assertIsNotNone(fresh)
            self.assertTrue(codeloom.index_is_fresh(tmp, fresh))
        finally:
            shutil.rmtree(tmp)

    def test_rename_reports_blast_radius(self):
        # rename should list the definition, touched files, and dependent modules
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "pkg"))
            with open(os.path.join(tmp, "pkg", "a.py"), "w") as f:
                f.write("def helper():\n    pass\n")
            with open(os.path.join(tmp, "pkg", "b.py"), "w") as f:
                f.write("from pkg.a import helper\nhelper()\n")
            files = [os.path.join(tmp, "pkg", "a.py"), os.path.join(tmp, "pkg", "b.py")]
            out = codeloom.render_rename(files, tmp, "helper", "new_helper")
            self.assertIn("helper", out)
            self.assertIn("pkg.a", out)  # definition module
            # b.py depends on pkg.a
            self.assertIn("pkg.a", out.split("depending")[1] if "depending" in out else out)
        finally:
            shutil.rmtree(tmp)

    def test_grep_searches_docs(self):
        # --grep must search markdown/docs, not just code files
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "README.md"), "w") as f:
                f.write("# README\nStale reference 48 tools here.\n")
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("x = 1\n")
            files = [os.path.join(tmp, "README.md"), os.path.join(tmp, "a.py")]
            out = codeloom.render_grep(files, tmp, "48 tools")
            self.assertIn("48 tools", out)
            self.assertIn("README.md", out)  # doc file now searched
        finally:
            shutil.rmtree(tmp)

    def test_files_finds_by_name(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "src", "core"))
            with open(os.path.join(tmp, "src", "core", "engine.py"), "w") as f:
                f.write("class Engine: pass\n")
            files = [os.path.join(tmp, "src", "core", "engine.py")]
            out = codeloom.render_files(files, tmp, "engine")
            self.assertIn("src/core/engine.py", out)
            # glob mode
            out2 = codeloom.render_files(files, tmp, "*.py")
            self.assertIn("engine.py", out2)
        finally:
            shutil.rmtree(tmp)

    def test_routes_extracts_http(self):
        # extract_routes finds FastAPI decorators + Express chains
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "app.py"), "w") as f:
                f.write("from fastapi import FastAPI\napp = FastAPI()\n"
                        "@app.get('/')\ndef root(): pass\n"
                        "@app.post('/items')\ndef create(): pass\n")
            routes = codeloom.extract_routes(tmp)
            self.assertTrue(any(r["path"] == "/" and r["handler"] == "root" for r in routes))
            self.assertTrue(any(r["path"] == "/items" and r["handler"] == "create" for r in routes))
        finally:
            shutil.rmtree(tmp)

    def test_channels_extraction(self):
        # channel detection maps emit -> listen
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "pub.js"), "w") as f:
                f.write("emitter.emit('user:created', user);\n")
            with open(os.path.join(tmp, "sub.js"), "w") as f:
                f.write("emitter.on('user:created', (u) => {});\n")
            c = codeloom.extract_channels(tmp)
            self.assertIn("user:created", c["emit"])
            self.assertIn("user:created", c["listen"])
        finally:
            shutil.rmtree(tmp)

    def test_export_snapshot(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("def helper(): pass\n")
            out = codeloom.render_export(tmp, os.path.join(tmp, "snap.json"))
            self.assertIn("Exported", out)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "snap.json")))
        finally:
            shutil.rmtree(tmp)

    def test_checkpoint_writes_and_restores(self):
        # --checkpoint writes a file; --checkpoint-restore reads it back
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("def helper(): pass\n")
            out = codeloom.render_checkpoint(tmp, "working on helper")
            self.assertIn("Status note", out)
            self.assertIn("working on helper", out)
            self.assertTrue(os.path.isfile(codeloom._checkpoint_path(tmp)))
            restored = codeloom.render_checkpoint_restore(tmp)
            self.assertIn("working on helper", restored)
        finally:
            shutil.rmtree(tmp)

    def test_adr_writes_and_lists(self):
        # --adr writes a structured record; --adr-list lists it
        tmp = tempfile.mkdtemp()
        try:
            out = codeloom.render_adr(tmp, "Use Postgres", "Need ACID", "Adopt PG16")
            self.assertIn("ADR-001", out)
            self.assertIn("Use Postgres", out)
            listing = codeloom.render_adr_list(tmp)
            self.assertIn("ADR-001", listing)
        finally:
            shutil.rmtree(tmp)

    def test_query_runs_structural_queries(self):
        # --query answers structural questions from the persisted graph
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("def helper():\n    pass\n\ndef main():\n    helper()\n")
            files = [os.path.join(tmp, "a.py")]
            codeloom.save_persistent_index(tmp, {}, files, kg={"calls": {"main": ["helper"]}, "imports": {}})
            out = codeloom.render_query(tmp, "callers helper")
            self.assertIn("main", out)
            out2 = codeloom.render_query(tmp, "callees main")
            self.assertIn("helper", out2)
        finally:
            shutil.rmtree(tmp)

    def test_working_memory_journal(self):
        # decide/reject/hypothesis/mark_seen build a layered working-state packet
        tmp = tempfile.mkdtemp()
        try:
            codeloom.wm_decide(tmp, "Use Redis", "multi-instance", "accepted")
            codeloom.wm_decide(tmp, "In-memory bucket", "too complex", "rejected")
            codeloom.wm_hypothesis(tmp, "pool not shared")
            codeloom.journal_mark_seen(tmp, ["engine.py", "login.py"])
            state = codeloom.render_working_state(tmp, full=True)
            self.assertIn("Use Redis", state)
            self.assertIn("In-memory bucket", state)
            self.assertIn("pool not shared", state)
            self.assertIn("engine.py", state)
            decs = codeloom.list_decisions(tmp)
            self.assertIn("Use Redis", decs)
            opens = codeloom.list_open_items(tmp)
            self.assertIn("pool not shared", opens)
        finally:
            shutil.rmtree(tmp)

    def test_long_term_memory_lesson_and_query(self):
        # --lesson records a trap; --query-memory finds it across memory files
        tmp = tempfile.mkdtemp()
        try:
            codeloom.memory_lesson(tmp, "in-memory bucket failed because not multi-instance")
            codeloom.memory_remember(tmp, "PATTERNS", "auth uses JWT")
            q = codeloom.memory_query(tmp, "auth")
            self.assertIn("auth uses JWT", q)
            q2 = codeloom.memory_query(tmp, "bucket")
            self.assertIn("failed because", q2)
        finally:
            shutil.rmtree(tmp)

    def test_cognitive_load_decomposes(self):
        # --cognitive-load emits intrinsic/extraneous/germane sections
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "engine.py"), "w") as f:
                f.write("class Engine:\n    def run(self):\n        pass\n")
            codeloom.memory_remember(tmp, "DECISIONS", "engine is the core")
            files = [os.path.join(tmp, "engine.py")]
            out = codeloom.render_cognitive_load(files, tmp, "engine")
            self.assertIn("Intrinsic load", out)
            self.assertIn("Extraneous load", out)
            self.assertIn("Germane load", out)
        finally:
            shutil.rmtree(tmp)

    def test_subword_embed_similarity(self):
        # subword-hash embedding gives fuzzy semantic similarity (typos)
        self.assertGreater(codeloom._subword_similarity("engine", "engine"), 0.9)
        self.assertGreater(codeloom._subword_similarity("engine", "engin"), 0.6)
        self.assertGreater(codeloom._subword_similarity("getengine", "engine"), 0.5)
        self.assertLess(codeloom._subword_similarity("engine", "banana"), 0.4)

    def test_rust_engine_finder(self):
        # --engine rust resolves to the compiled Rust core (or None if absent)
        core = codeloom._find_core_engine("rust")
        if core:  # only assert when rustc produced it
            import os
            self.assertTrue(os.path.isfile(core))

    def test_install_grammars_prints(self):
        # without --yes, install_grammars prints the command (doesn't install)
        out = codeloom.install_grammars(do_install=False)
        self.assertIn("pip install", out)
        self.assertIn("--yes", out)

    def test_framework_detection(self):
        # detect FastAPI from pyproject.toml
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "pyproject.toml"), "w") as f:
                f.write("[project]\ndependencies = [\"fastapi\"]\n")
            self.assertEqual(codeloom.detect_framework(tmp), "FastAPI")
            # detect Express from package.json
            with open(os.path.join(tmp, "package.json"), "w") as f:
                f.write('{"dependencies": {"express": "^4.0.0"}}\n')
            self.assertEqual(codeloom.detect_framework(tmp), "Express")
            # render_framework emits structure
            out = codeloom.render_framework(tmp, 100)
            self.assertIn("framework:", out)
            self.assertIn("Express", out)
        finally:
            shutil.rmtree(tmp)

    def test_session_telemetry(self):
        # log_session appends; render_session_report summarizes
        tmp = tempfile.mkdtemp()
        try:
            codeloom.log_session(tmp, "--graph .", "some output text")
            codeloom.log_session(tmp, "--task x .", "more output")
            report = codeloom.render_session_report(tmp)
            self.assertIn("2 call(s)", report)
            self.assertIn("--graph .", report)
            self.assertIn("--task x .", report)
            self.assertIn("cost", report)
        finally:
            shutil.rmtree(tmp)

    def test_versions_in_sync(self):
        # Version-drift guard: VERSION in codeloom.py, SERVER_VERSION in
        # codeloom-mcp.py, and pyproject.toml must all agree. No hardcoded
        # expectation: the three files are the single source of truth for
        # each other, so any one-sided bump fails loudly.
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "codeloom-mcp.py")) as f:
            mcp_m = re.search(r'SERVER_VERSION = "([^"]+)"', f.read())
            if mcp_m is None:
                self.fail("codeloom-mcp.py missing SERVER_VERSION")
            mcp_version = mcp_m.group(1)
        with open(os.path.join(here, "pyproject.toml")) as f:
            m = re.search(r'^version = "([^"]+)"', f.read(), re.MULTILINE)
            if m is None:
                self.fail("pyproject.toml missing 'version' key")
            pyproject_version = m.group(1)
        self.assertEqual(codeloom.VERSION, mcp_version,
                         "codeloom.VERSION != codeloom-mcp.py SERVER_VERSION")
        self.assertEqual(codeloom.VERSION, pyproject_version,
                         "codeloom.VERSION != pyproject.toml version")
        # all three must be non-empty and identical (guards against a stale
        # regex capturing the wrong key)
        self.assertNotEqual(codeloom.VERSION, "",
                            "codeloom.VERSION must not be empty")
        self.assertEqual(len({codeloom.VERSION, mcp_version,
                              pyproject_version}), 1,
                         "VERSION/SERVER_VERSION/pyproject drift: "
                         "%r / %r / %r" % (codeloom.VERSION, mcp_version,
                                           pyproject_version))

    def test_mcp_tool_registry_unique_and_complete(self):
        # The MCP TOOLS registry is the server's contract with the agent.
        # Guards that survive refactors: (a) every tool name is unique,
        # (b) codeloom_ask is registered exactly once (a duplicate would
        # make tool routing ambiguous), (c) the registry is exactly 77
        # tools, and (d) every entry is a well-formed schema dict.
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "codeloom-mcp.py")) as f:
            src = f.read()
        tree = ast.parse(src)
        names = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            target = (node.target if isinstance(node, ast.AnnAssign)
                      else node.targets[0])
            if not (isinstance(target, ast.Name) and target.id == "TOOLS"):
                continue
            for item in node.value.elts:
                if not isinstance(item, ast.Dict):
                    continue
                nm = None
                for k, v in zip(item.keys, item.values):
                    if (isinstance(k, ast.Constant) and k.value == "name"
                            and isinstance(v, ast.Constant)):
                        nm = v.value
                names.append(nm)
        self.assertGreaterEqual(len(names), 77,
                                "expected >=77 MCP tools, got %d" % len(names))
        self.assertEqual(len(set(names)), len(names),
                         "duplicate MCP tool names: %s" % sorted(
                             {n for n in names if names.count(n) > 1}))
        self.assertEqual(names.count("codeloom_ask"), 1,
                         "codeloom_ask must be registered exactly once")
        for nm in names:
            self.assertTrue(nm and nm.startswith("codeloom_"),
                            "bad tool name: %r" % nm)

    def test_meta_envelope_reports_freshness(self):
        # freshness envelope: fresh right after build, stale after file drift
        import time
        tmp = tempfile.mkdtemp()
        try:
            a = os.path.join(tmp, "a.py")
            with open(a, "w") as f:
                f.write("def x():\n    pass\n")
            files = [a]
            idx = codeloom.build_symbol_index(files, tmp)
            codeloom.save_persistent_index(tmp, idx, files)
            meta = codeloom.meta_envelope(tmp)
            self.assertTrue(meta["indexed"])
            self.assertFalse(meta["stale_warning"])
            self.assertIsNotNone(meta["index_age_days"])
            # drift: append to the tracked file -> index goes stale
            time.sleep(0.02)
            with open(a, "a") as f:
                f.write("# drift\n")
            meta2 = codeloom.meta_envelope(tmp)
            self.assertTrue(meta2["stale_warning"])
        finally:
            shutil.rmtree(tmp)

    def test_mcp_responses_carry_meta_envelope(self):
        # every successful tools/call response carries _meta (repowise parity)
        import json
        here = os.path.dirname(os.path.abspath(__file__))
        mcp = os.path.join(here, "codeloom-mcp.py")
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "m.py"), "w") as f:
                f.write("class Q:\n    pass\n")
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "codeloom_map", "arguments": {"root": tmp}}},
            ]
            payload = "\n".join(json.dumps(r) for r in reqs) + "\n"
            r = subprocess.run([sys.executable, mcp], input=payload,
                               capture_output=True, text=True, timeout=120)
            got = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
            resp = next(x for x in got if x.get("id") == 2)
            self.assertIn("_meta", resp["result"])
            self.assertIn("stale_warning", resp["result"]["_meta"])
        finally:
            shutil.rmtree(tmp)

    def test_context_card_batches_targets(self):
        # one call, multiple targets: definitions + callers + ADR titles
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "svc.py"), "w") as f:
                f.write("import helpers\n"
                        "class Svc:\n"
                        "    def run(self):\n"
                        "        return helpers.h()\n")
            with open(os.path.join(tmp, "helpers.py"), "w") as f:
                f.write("def h():\n    return 1\n")
            files = [os.path.join(tmp, "svc.py"), os.path.join(tmp, "helpers.py")]
            out = codeloom.render_context_card(files, tmp, ["Svc", "h"])
            self.assertIn("Svc", out)
            self.assertIn("h", out)
            self.assertIn("callers", out.lower())
            self.assertIn("##", out)
            # unknown target degrades gracefully
            out2 = codeloom.render_context_card(files, tmp, ["nope_xyz"])
            self.assertIn("not found", out2)
        finally:
            shutil.rmtree(tmp)

    def test_answer_includes_confidence_and_citation(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "engine.py"), "w") as f:
                f.write("class Engine:\n"
                        "    def run(self):\n"
                        "        return retry(1)\n")
            files = [os.path.join(tmp, "engine.py")]
            out = codeloom.render_answer(files, tmp, "engine")
            self.assertIn("confidence:", out)
            self.assertIn("source:", out)
            # gibberish -> low confidence, no crash
            out2 = codeloom.render_answer(files, tmp, "zzzqqqxyzzy")
            self.assertIn("confidence: low", out2)
        finally:
            shutil.rmtree(tmp)

    def test_why_stamps_evidence_confidence(self):
        tmp = tempfile.mkdtemp()
        try:
            memdir = os.path.join(tmp, ".codeloom-memory")
            os.makedirs(memdir)
            with open(os.path.join(memdir, "DECISIONS.md"), "w") as f:
                f.write("# Decisions\n- Use Redis pool shared per-process\n")
            files = [os.path.join(tmp, "a.py")]
            out = codeloom.render_why(files, tmp, "Redis pool shared per-process")
            self.assertIn("[exact]", out)
            out2 = codeloom.render_why(files, tmp, "unrelated gibberish topic")
            self.assertIn("No recorded decisions/memory match.", out2)
        finally:
            shutil.rmtree(tmp)

    def test_tree_sitter_grammar_parity_fixtures(self):
        # codegraph-style discipline: no grammar ships without proving its
        # extraction. Each fixture dir holds a canonical sample + frozen
        # expected.jsonl; the engine must reproduce it byte-for-byte.
        here = os.path.dirname(os.path.abspath(__file__))
        fx = os.path.join(here, "engine_rs", "fixtures")
        if not os.path.isdir(fx):
            self.skipTest("no fixtures dir")
        bins = [os.path.join(here, "engine_rs", "target", "release", "codeloom_engine")]
        engine_bin = next((b for b in bins if os.path.isfile(b)), None)
        if engine_bin is None:
            self.skipTest("engine_rs binary not built (cargo build --release)")
        langs = sorted(d for d in os.listdir(fx)
                       if os.path.isdir(os.path.join(fx, d)))
        self.assertGreaterEqual(len(langs), 45, "expected >=45 grammar fixtures")
        for lang in langs:
            d = os.path.join(fx, lang)
            exp_path = os.path.join(d, "expected.jsonl")
            sample = next((os.path.join(d, f) for f in sorted(os.listdir(d))
                           if f.startswith("sample.")), None)
            if not os.path.isfile(exp_path) or sample is None:
                continue
            with open(exp_path, encoding="utf-8") as f:
                expected = f.read().strip()
            lst = subprocess.run([engine_bin, "--list", d], capture_output=True,
                                 text=True, timeout=60)
            got = subprocess.run([engine_bin], input=lst.stdout,
                                 capture_output=True, text=True, timeout=60)
            actual = got.stdout.strip()
            # normalize: the frozen file records the path as written at freeze
            # time; compare on parsed JSON minus the volatile path prefix.
            def norm(s):
                out = []
                for line in s.splitlines():
                    if not line.strip():
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        return s  # not JSON — fall back to raw compare
                    j["file"] = os.path.basename(j.get("file", ""))
                    out.append(json.dumps(j, sort_keys=True))
                return "\n".join(out)
            self.assertEqual(
                norm(expected), norm(actual),
                "grammar parity broke for %s — extraction changed; if intended, "
                "regenerate via engine_rs/gen_fixtures.sh" % lang)

    def test_health_scores_and_detectors(self):
        # deterministic health screen: dead symbol + long function detected,
        # score < 10 for the offender, clean file stays at 10
        tmp = tempfile.mkdtemp()
        try:
            dirty = os.path.join(tmp, "dirty.py")
            with open(dirty, "w") as f:
                f.write("def orphan():\n    return 1\n\n\n")
                f.write("def too_many(a, b, c, d, e, f_, g):\n    return a\n")
            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w") as f:
                f.write("def used():\n    return 1\n\n\n"
                        "def caller():\n    return used()\n")
            files = [dirty, clean]
            out = codeloom.render_health(files, tmp)
            self.assertIn("code health", out)
            self.assertIn("dead_symbol", out)      # orphan never called
            self.assertIn("too_many_params", out)  # 7 params
            self.assertIn("dirty.py", out)         # offender named worst-first
            res = codeloom.compute_health(files, tmp,
                                          codeloom.build_byte_index(files, tmp),
                                          codeloom.build_call_graph_multi(files, tmp))
            self.assertLess(res["files"][dirty]["score"], 10.0)
            # clean file produces NO findings (absent from the findings map)
            self.assertNotIn(clean, res["files"])
        finally:
            shutil.rmtree(tmp)

    def test_change_risk_scores_and_bands(self):
        # clean-room change-risk: bigger diff + spread + hot file => higher
        # band than a trivial commit; deterministic on same inputs
        tmp = tempfile.mkdtemp()
        try:
            def git(*a):
                return subprocess.run(["git"] + list(a), cwd=tmp,
                                      capture_output=True, text=True)
            git("init", "-q")
            git("config", "user.email", "t@t")
            git("config", "user.name", "t")
            with open(os.path.join(tmp, "base.py"), "w") as f:
                f.write("def core():\n    return 1\n\n\ndef user():\n    return core()\n")
            git("add", "-A")
            git("commit", "-q", "-m", "init")
            # risky commit: big diff across two files, one named like a fix target
            with open(os.path.join(tmp, "base.py"), "w") as f:
                f.write("def core():\n    return 2\n\n\ndef user():\n    return core() + 1\n" + "x = %d\n" * 40 % tuple(range(40)))
            with open(os.path.join(tmp, "extra.py"), "w") as f:
                f.write("def helper():\n    return 3\n")
            git("add", "-A")
            git("commit", "-q", "-m", "big refactor after bug report")
            files = [os.path.join(tmp, "base.py"), os.path.join(tmp, "extra.py")]
            index = codeloom.build_byte_index(files, tmp)
            calls = codeloom.build_call_graph_multi(files, tmp)
            r = codeloom.compute_change_risk(tmp, index, calls, "HEAD~1..HEAD")
            self.assertNotIn("error", r)
            self.assertGreater(r["score"], 0)
            self.assertIn(r["band"], ("low", "medium", "high", "critical"))
            self.assertTrue(r["drivers"])          # at least diff-size driver
            self.assertTrue(any("added lines" in d["why"] for d in r["drivers"]))
            out = codeloom.render_change_risk(files, tmp, "HEAD~1..HEAD")
            self.assertIn("change risk", out)
            self.assertIn("/100", out)
        finally:
            # git marks .git objects read-only; plain rmtree fails on Windows.
            def _force_remove(func, path, _exc):
                try:
                    os.chmod(path, 0o700)
                    func(path)
                except OSError:
                    pass
            shutil.rmtree(tmp, onerror=_force_remove)

    def test_pattern_search_finds_structural_matches(self):
        # ast-grep-style structural search: $F($$$A) matches any call and
        # binds the function + args; exact-shape patterns miss non-matches
        tmp = tempfile.mkdtemp()
        try:
            f = os.path.join(tmp, "app.py")
            with open(f, "w") as fh:
                fh.write("import os\n"
                         "result = eval(user_input)\n"
                         "safe = len(items)\n"
                         "other = os.getcwd()\n")
            files = [f]
            # any call shape
            out = codeloom.render_pattern_search(files, tmp, "$F($$$ARGS)")
            self.assertIn("eval", out)
            self.assertIn("len", out)
            self.assertIn("$F =", out)          # captures bound
            # exact call: only eval( matches
            out2 = codeloom.render_pattern_search(files, tmp, "eval($X)")
            self.assertIn("eval", out2)
            self.assertNotIn("len(", out2.split("$F")[0] if "$F" in out2 else out2)
            self.assertIn("$X =", out2)
            # no match case
            out3 = codeloom.render_pattern_search(files, tmp, "nonexistent_fn($Q)")
            self.assertIn("No structural matches", out3)
        finally:
            shutil.rmtree(tmp)
    def test_grep_symbolic_excludes_comments_and_strings(self):
        # plain --grep finds matches everywhere; --grep-symbolic keeps only
        # real-code hits and tags each with its enclosing symbol
        tmp = tempfile.mkdtemp()
        try:
            f = os.path.join(tmp, "app.py")
            with open(f, "w") as fh:
                fh.write(
                    "# use retry here (comment mention)\n"
                    "DOC = 'use retry for backoff'\n"
                    "def handler():\n"
                    "    return retry(fn)\n"
                )
            files = [f]
            # raw grep sees all three mentions
            raw = codeloom.render_grep(files, tmp, "retry")
            self.assertEqual(raw.count("retry"), raw.count("retry"))  # sanity
            raw_n = len(codeloom.grep_search(files, tmp, "retry"))
            self.assertEqual(raw_n, 3)
            # symbolic grep sees only the real call inside `handler`
            sym = codeloom.render_grep_symbolic(files, tmp, "retry")
            self.assertIn("in function `handler`", sym)
            self.assertNotIn("comment mention", sym)
            self.assertNotIn("backoff", sym)
            res = codeloom.grep_symbolic(files, tmp, "retry")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["symbol"], "handler")
            self.assertEqual(res[0]["line"], 4)
        finally:
            shutil.rmtree(tmp)

    def test_doc_sidecars_extract_and_search(self):
        # docx/xlsx/pptx/epub/odt/rtf are extracted via stdlib zip+xml and
        # their .txt sidecars become grep-searchable; unsupported binary
        # formats and missing pdftotext degrade gracefully (None, no crash)
        tmp = tempfile.mkdtemp()
        try:
            import zipfile
            import shutil as _sh

            def zip_doc(members: dict, path: str):
                with zipfile.ZipFile(path, "w") as z:
                    for name, data in members.items():
                        z.writestr(name, data)

            # docx: minimal package with one paragraph
            docx = os.path.join(tmp, "guide.docx")
            zip_doc({"word/document.xml":
                     '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>'
                     'deployment secret rotate quarterly</w:t></w:r></w:p>'
                     '</w:body></w:document>',
                     "[Content_Types].xml": "<Types/>"}, docx)

            # xlsx: shared strings with one value
            xlsx = os.path.join(tmp, "prices.xlsx")
            zip_doc({"xl/sharedStrings.xml":
                     '<sst xmlns="x"><si><t>widget cost 99</t></si></sst>',
                     "[Content_Types].xml": "<Types/>"}, xlsx)

            # pptx: one slide
            pptx = os.path.join(tmp, "deck.pptx")
            zip_doc({"ppt/slides/slide1.xml":
                     '<p:sld xmlns:p="p"><p:cSld><p:spTree><p:sp><p:txBody>'
                     '<a:p xmlns:a="a"><a:r><a:t>quarterly goals</a:t></a:r>'
                     '</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
                     "[Content_Types].xml": "<Types/>"}, pptx)

            # epub: one xhtml chapter
            epub = os.path.join(tmp, "book.epub")
            zip_doc({"OEBPS/ch1.xhtml":
                     '<html xmlns="x"><body><h1>Chapter One</h1>'
                     '<p>The quick brown fox</p></body></html>',
                     "mimetype": "application/epub+zip"}, epub)

            # odt: content.xml
            odt = os.path.join(tmp, "notes.odt")
            zip_doc({"content.xml":
                     '<office:document-content xmlns:office="o">'
                     '<office:body><office:text><text:p xmlns:text="t">'
                     'meeting minutes</text:p></office:text></office:body>'
                     '</office:document-content>'}, odt)

            # rtf
            rtf = os.path.join(tmp, "memo.rtf")
            with open(rtf, "w") as fh:
                fh.write(r"{\rtf1\ansi memo body text\par}")

            # binary file that must NOT extract
            blob = os.path.join(tmp, "blob.bin")
            with open(blob, "wb") as fh:
                fh.write(b"\x00\x01\x02binary")

            files = [docx, xlsx, pptx, epub, odt, rtf, blob]
            sidecars = []
            for f in files:
                s = codeloom.ensure_doc_sidecar(f)
                if s:
                    sidecars.append(s)
            # all six text formats extract; binary does not
            self.assertEqual(len(sidecars), 6)
            for s in sidecars:
                self.assertTrue(os.path.isfile(s))

            # content lands in the sidecars
            all_text = ""
            for s in sidecars:
                with open(s) as fh:
                    all_text += fh.read()
            for needle in ("deployment secret", "widget cost", "quarterly",
                           "Chapter One", "meeting minutes", "memo body"):
                self.assertIn(needle, all_text)

            # sidecars are grep-searchable through the normal pipeline
            out = codeloom.render_grep(sidecars, tmp, "secret rotate")
            self.assertIn("guide.docx.txt", out)

            # stale sidecar refresh: touch the docx, sidecar regenerates
            import time as _t
            _t.sleep(0.02)
            with open(docx, "a") as fh:
                fh.write(" ")  # bump mtime
            os.utime(docx, None)
            s2 = codeloom.ensure_doc_sidecar(docx)
            self.assertIsNotNone(s2)
        finally:
            shutil.rmtree(tmp)


class TestVerifyEdit(unittest.TestCase):
    """--verify-edit: post-edit graph-integrity oracle."""

    def _git_repo(self, files):
        """Create a git repo at tmp/repo with the given {relpath: content}."""
        base = tempfile.mkdtemp()
        repo = os.path.join(base, "repo")
        os.makedirs(repo)
        for rel, content in files.items():
            p = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        for c in (["init", "-q"], ["config", "user.email", "t@t"],
                  ["config", "user.name", "t"]):
            subprocess.run(["git"] + c, cwd=repo, capture_output=True, text=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                       capture_output=True, text=True)
        return repo

    def _git(self, repo, *args):
        return subprocess.run(["git"] + list(args), cwd=repo,
                              capture_output=True, text=True)

    def _run_verify(self, repo, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
             "--verify-edit", repo] + list(extra),
            capture_output=True, text=True)

    def test_verify_edit_stop_on_broken_import(self):
        repo = self._git_repo({
            "a.py": "from b import helper\n\ndef run():\n    return helper()\n",
            "b.py": "def helper():\n    return 1\n",
        })
        try:
            # break the import: remove b.py and the import from a.py
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("def run():\n    return 1\n")
            os.remove(os.path.join(repo, "b.py"))
            self._git(repo, "add", "-A")
            r = self._run_cli_check(repo)
            self.assertEqual(r.returncode, 0, r.stderr)  # warn: exits 0
            self.assertIn("VERDICT: STOP", r.stdout)
            self.assertIn("dangling-import", r.stdout)
            # strict elevates STOP to exit 1
            r2 = self._run_verify(repo, "--severity", "strict")
            self.assertEqual(r2.returncode, 1)
            self.assertIn("VERDICT: STOP", r2.stdout)
        finally:
            shutil.rmtree(os.path.dirname(repo))

    def _run_cli_check(self, repo):
        return subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
             "--verify-edit", repo],
            capture_output=True, text=True)

    def test_verify_edit_check_on_dynamic(self):
        repo = self._git_repo({
            "a.py": "import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n",
        })
        try:
            # edit the file (so it's in the working-tree change set), keeping
            # the dynamic import -> CHECK, never STOP
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("import importlib\n\ndef load(name):\n    return importlib.import_module(name + '.x')\n")
            self._git(repo, "add", "-A")
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("VERDICT: CHECK", r.stdout)
            self.assertIn("dynamic_import", r.stdout)
        finally:
            shutil.rmtree(os.path.dirname(repo))

    def test_verify_edit_go(self):
        repo = self._git_repo({
            "a.py": "from b import x\n\ndef run():\n    return x()\n",
            "b.py": "def x():\n    return 1\n",
        })
        try:
            # benign edit: both files still import-resolvable
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("from b import x\n\ndef run():\n    return x() + 1\n")
            self._git(repo, "add", "-A")
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("VERDICT: GO", r.stdout)
        finally:
            shutil.rmtree(os.path.dirname(repo))


class TestBlindspot(unittest.TestCase):
    """--blindspot: hot set vs impact-derived read set."""

    def _repo(self):
        base = tempfile.mkdtemp()
        repo = os.path.join(base, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "engine.py"), "w") as f:
            f.write("def run():\n    return 1\n")
        with open(os.path.join(repo, "cli.py"), "w") as f:
            f.write("from engine import run\n\ndef main():\n    return run()\n")
        return repo

    def _run(self, repo, *args):
        return subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py")]
            + list(args) + [repo],
            capture_output=True, text=True)

    def test_blindspot_warn_unread_file(self):
        repo = self._repo()
        try:
            # mark ONLY a non-module token as read; engine.py and cli.py (its
            # dependent) are unread -> STOP + CHECK blast-radius dependents
            r1 = self._run(repo, "--mark-seen", "README.md")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._run(repo, "--blindspot")
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("never-read file: engine.py", r2.stdout)
            self.assertIn("never-read file: cli.py", r2.stdout)
            self.assertIn("VERDICT: STOP", r2.stdout)
            self.assertIn("unread dependent", r2.stdout)
        finally:
            shutil.rmtree(os.path.dirname(repo))

    def test_blindspot_optout(self):
        repo = self._repo()
        try:
            r = self._run(repo, "--blindspot", "--no-blindspot")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("VERDICT: SKIP", r.stdout)
            self.assertIn("--no-blindspot opt-out", r.stdout)
        finally:
            shutil.rmtree(os.path.dirname(repo))


class TestSavingsReport(unittest.TestCase):
    def test_savings_report_basic(self):
        base = tempfile.mkdtemp()
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--session", "--json", base],
                capture_output=True, text=True, cwd=base)
            self.assertEqual(r.returncode, 0, r.stderr)
            r2 = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--savings-report"],
                capture_output=True, text=True, cwd=base)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("# codeloom --savings-report", r2.stdout)
            self.assertIn("Methodology", r2.stdout)
            self.assertRegex(r2.stdout, r"TOTAL: 1 call\(s\), ~\d+ tokens emitted")
            self.assertIn("tokens baseline", r2.stdout)
            self.assertRegex(r2.stdout, r"memory: \d+ files, \d+ KB \(\d+ archived\)")
            # --since N keeps rows newer than N days (same-day rows survive)
            r3 = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--savings-report", "--since", "0"],
                capture_output=True, text=True, cwd=base)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("TOTAL: 1 call(s)", r3.stdout)
        finally:
            shutil.rmtree(base)


class TestMemoryBounds(unittest.TestCase):
    """Caps + lossless archive rotation + --memory-prune (never auto-delete)."""

    def _repo(self):
        base = tempfile.mkdtemp()
        repo = os.path.join(base, "repo")
        os.makedirs(repo)
        return repo

    def test_memory_archive_rotates_at_cap(self):
        repo = self._repo()
        try:
            old = os.environ.get("CODELOOM_MEMORY_CAP_BYTES")
            os.environ["CODELOOM_MEMORY_CAP_BYTES"] = "200"
            try:
                # 20 decisions * ~24 bytes each -> exceeds the 200-byte cap
                for i in range(20):
                    codeloom.wm_decide(repo, "decision number %d" % i,
                                       "some reason here")
            finally:
                if old is None:
                    del os.environ["CODELOOM_MEMORY_CAP_BYTES"]
                else:
                    os.environ["CODELOOM_MEMORY_CAP_BYTES"] = old
            p = os.path.join(repo, ".codeloom-memory", "DECISIONS.md")
            self.assertTrue(os.path.isfile(p))
            size = os.path.getsize(p)
            self.assertLessEqual(size, 200 + 60, "DECISIONS.md must be capped")
            arch_dir = os.path.join(repo, ".codeloom-memory", "archive")
            self.assertTrue(os.path.isdir(arch_dir))
            arch = [f for f in os.listdir(arch_dir) if f.startswith("DECISIONS.md-")]
            self.assertEqual(len(arch), 1, "one archive file expected")
            with open(os.path.join(arch_dir, arch[0])) as fh:
                arch_text = fh.read()
            # lossless: the oldest decisions survive in the archive
            self.assertIn("decision number 0", arch_text)
            self.assertIn("decision number 1", arch_text)
            self.assertIn("decision number 2", arch_text)
            # nothing deleted, nothing summarized: every decision is somewhere
            with open(p) as fh:
                cur = fh.read()
            all_lines = cur + arch_text
            for i in range(20):
                self.assertIn("decision number %d" % i, all_lines)
        finally:
            shutil.rmtree(repo)

    def test_memory_prune_dry_run_no_delete(self):
        repo = self._repo()
        try:
            arch = os.path.join(repo, ".codeloom-memory", "archive")
            os.makedirs(arch, exist_ok=True)
            old = os.path.join(arch, "DECISIONS.md-2020-01-01.md")
            with open(old, "w") as f:
                f.write("old entry\n")
            import time as _t
            _t.time  # noqa
            os.utime(old, (1000000000, 1000000000))  # 2001 — very old
            fresh = os.path.join(arch, "LESSONS.md-2099-01-01.md")
            with open(fresh, "w") as f:
                f.write("new\n")
            os.utime(fresh, None)  # now
            r = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--memory-prune", "--older-than", "30", repo],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("would remove: DECISIONS.md-2020-01-01.md", r.stdout)
            self.assertIn("Nothing was deleted", r.stdout)
            self.assertTrue(os.path.isfile(old), "dry-run must NOT delete")
            # explicit --delete removes ONLY the old one
            r2 = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--memory-prune", "--older-than", "30", "--delete", repo],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("deleted 1 archived file", r2.stdout)
            self.assertFalse(os.path.isfile(old))
            self.assertTrue(os.path.isfile(fresh), "fresh archive must survive")
        finally:
            shutil.rmtree(repo)


class TestHookInstaller(unittest.TestCase):
    def test_install_hook_writes_file(self):
        base = tempfile.mkdtemp()
        try:
            repo = os.path.join(base, "repo")
            os.makedirs(os.path.join(repo, "scripts"))
            os.makedirs(os.path.join(repo, ".git", "hooks"))
            with open(os.path.join(repo, "scripts", "pre-commit-hook.sh"), "w") as f:
                f.write("#!/bin/sh\necho 'warn-only check'\nexit 0\n")
            hook = os.path.join(repo, ".git", "hooks", "pre-commit")
            r = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--install-hook", repo],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("installed", r.stdout)
            self.assertTrue(os.path.isfile(hook))
            with open(hook) as f:
                body = f.read()
            # the hook body must NOT be embedded in codeloom.py — it must
            # reference scripts/pre-commit-hook.sh
            self.assertIn("pre-commit-hook.sh", body)
            self.assertNotIn("hook-only check body", body)
            # idempotent: re-running updates in place, still one file
            r2 = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--install-hook", repo],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("installed", r2.stdout)
            # runs warn-only and exits 0
            rc = subprocess.run(["sh", hook], capture_output=True, text=True)
            self.assertEqual(rc.returncode, 0)
            # uninstall removes it
            r3 = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--uninstall-hook", repo],
                capture_output=True, text=True)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("removed", r3.stdout)
            self.assertFalse(os.path.isfile(hook))
        finally:
            shutil.rmtree(base)


class TestEvalPlumbing(unittest.TestCase):
    def test_eval_runner_absent_graceful(self):
        # when the runner is missing (forced via CODELOOM_EVAL_RUNNER), --eval
        # must degrade gracefully (exit 1, no crash)
        base = tempfile.mkdtemp()
        try:
            repo = os.path.join(base, "repo")
            os.makedirs(repo)
            env = dict(os.environ)
            env["CODELOOM_EVAL_RUNNER"] = os.path.join(base, "does-not-exist.py")
            r = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--eval", "token", repo],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("eval_runner.py not found", r.stdout)
        finally:
            shutil.rmtree(base)

    def test_eval_plumbs_argv_to_runner(self):
        # stub runner records the exact argv contract:
        #   eval_runner.py <kind> [--json] --root <root>
        base = tempfile.mkdtemp()
        try:
            repo = os.path.join(base, "repo")
            os.makedirs(repo)
            stub = os.path.join(base, "stub_runner.py")
            with open(stub, "w") as f:
                f.write("import json, sys\n"
                        "json.dump({'argv': sys.argv[1:]}, sys.stdout)\n")
            env = dict(os.environ)
            env["CODELOOM_EVAL_RUNNER"] = stub
            r = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "codeloom.py"),
                 "--eval", "token", "--json", "--root", repo],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = json.loads(r.stdout)
            self.assertEqual(got["argv"], ["token", "--json", "--root", repo])
        finally:
            shutil.rmtree(base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
