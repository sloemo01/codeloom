#!/usr/bin/env python3
"""Tests for codeloom. Run with: python3 tests.py"""
import os
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
        # --pack emits a single-shot context file with reading order + impact + symbols
        files = []
        for root, _, fs in os.walk(self.repo):
            for f in fs:
                if f.endswith(".py") and "__pycache__" not in root and ".venv" not in root:
                    files.append(os.path.join(root, f))
        pack = codeloom.render_pack(files, self.repo, "retry")
        self.assertIn("Reading order", pack)
        self.assertIn("Impact", pack)
        self.assertIn("Symbols", pack)
        self.assertIn("retry", pack)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
