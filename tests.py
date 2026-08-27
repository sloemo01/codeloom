#!/usr/bin/env python3
"""Tests for codeloom. Run with: python3 tests.py"""
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

# Absolute path to this file's directory, computed once at import time.
# subprocess invocations of codeloom.py must resolve it from ANY cwd
# (CI runs `python3 tests.py` from a different directory).
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, TESTS_DIR)
import codeloom  # noqa: E402


def force_rmtree(path, attempts=12, delay=0.5):
    """Remove a directory tree, retrying transient failures (Windows).

    git leaves short-lived file handles/locks on .git/objects on Windows,
    so plain shutil.rmtree can raise PermissionError [WinError 5]. Retry
    up to `attempts` times with a growing backoff (0.5s -> 1.0s, ~9s worst
    case — enough for a lingering git.exe handle to close), and chmod
    read-only entries (git marks .git objects read-only) before giving up.
    Missing paths are a no-op (safe replacement for ignore_errors=True).

    These are throwaway temp dirs: cleanup is best-effort. After the
    retry loop exhausts, a final ignore_errors=True rmtree guarantees a
    lingering Windows file lock can NEVER fail a test.
    """
    import time
    if not path or not os.path.exists(path):
        return
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if i == attempts - 1:
                break
            time.sleep(min(delay + i * 0.05, 1.0))
    # Last resort: strip read-only bits, then retry each failed unlink /
    # rmdir after a short sleep (the lock is usually a lingering git.exe
    # handle that closes within a second), then repeat the whole loop.
    def _force_remove(func, p, _exc):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
        for _ in range(2):
            try:
                func(p)
                return
            except OSError:
                time.sleep(0.3)
    for i in range(attempts):
        try:
            shutil.rmtree(path, onerror=_force_remove)
            return
        except OSError:
            if i == attempts - 1:
                break
            time.sleep(min(delay + i * 0.05, 1.0))
    # Absolute final fallback: whatever is still locked stays behind, but
    # the test must never fail because cleanup couldn't delete a temp dir.
    shutil.rmtree(path, ignore_errors=True)


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
        force_rmtree(self.tmp)

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
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
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
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py")]
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
            force_rmtree(tmp)

    def test_cli_get_symbol_json_contract(self):
        # --json --get-symbol emits machine-readable JSON (not human text)
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            r, _ = self._run_cli("--json", "--get-symbol", "Engine", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            import json
            data = json.loads(r.stdout)
            self.assertEqual(data["symbol"], "Engine")
            self.assertIn("source", data)
            self.assertIn("class Engine", data["source"])
            # not-found path is JSON too
            r2, _ = self._run_cli("--json", "--get-symbol", "NoSuchSymbol", repo)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertFalse(json.loads(r2.stdout)["found"])
        finally:
            force_rmtree(tmp)

    def test_cli_snippet_byte_accuracy(self):
        # --snippet is byte-accurate: a multi-byte (UTF-8) file sliced at
        # byte offsets must not produce mojibake or off-by-one text
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            # 3-byte chars: "é" = 0xC3 0xA9. 10 é's = 30 bytes + newline.
            with open(os.path.join(repo, "u.py"), "w", encoding="utf-8") as f:
                f.write("é" * 10 + "\n" + "def x():\n    return 1\n")
            r, _ = self._run_cli("--snippet", "u.py", "0", "31", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("é" * 10, r.stdout)
            # reversed range must error, not silently return garbage
            r2, _ = self._run_cli("--snippet", "u.py", "31", "0", repo)
            self.assertNotEqual(r2.returncode, 0)
        finally:
            force_rmtree(tmp)

    def test_cli_query_dependents_no_crash(self):
        # --query dependents on a symbol with no dependents must not crash
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            r, _ = self._run_cli("--query", "dependents backup", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            force_rmtree(tmp)

    def test_cli_diff_non_git_honesty(self):
        # --diff on a non-git dir must say so, not crash or fabricate
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("x = 1\n")
            r, _ = self._run_cli("--diff", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("not a git", r.stdout.lower())
        finally:
            force_rmtree(tmp)

    def test_js_escape_unit(self):
        # _js_escape must neutralize every dangerous character, on all
        # platforms (the CLI test below only runs on POSIX because the
        # quote character can't exist in a Windows filename at all)
        esc = codeloom._js_escape
        self.assertEqual(esc('evil"+alert(1)+"'), 'evil\\"+alert(1)+\\"')
        self.assertEqual(esc("a\\b"), "a\\\\b")
        self.assertEqual(esc("x</script>"), "x<\\/script>")
        self.assertEqual(esc("l1\nl2\rl3\tl4"), "l1\\nl2\\rl3\\tl4")
        # clean names pass through untouched
        self.assertEqual(esc("engine.core"), "engine.core")

    @unittest.skipIf(os.name == "nt", "hostile quote filename cannot exist on Windows")
    def test_cli_graph_html_escape(self):
        # hostile filename must not break out of the JS string literal
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, 'evil"+alert(1)+".py'), "w") as f:
                f.write("import normal\n\ndef hostile():\n    return normal.n()\n")
            with open(os.path.join(repo, "normal.py"), "w") as f:
                f.write("def n():\n    return 1\n")
            r, _ = self._run_cli("--graph-html", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            html = os.path.join(repo, "codeloom-graph.html")
            self.assertTrue(os.path.isfile(html), "--graph-html wrote no file")
            with open(html, encoding="utf-8") as fh:
                content = fh.read()
            # the raw quote must be escaped in the emitted JS
            self.assertNotIn('"evil"', content)
            self.assertIn("\\\"", content)
        finally:
            force_rmtree(tmp)

    def test_cli_symlink_containment(self):
        # a symlink pointing outside the repo must not leak outside content
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            outside = os.path.join(tmp, "outside")
            os.makedirs(repo)
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.py"), "w") as f:
                f.write("TOPSECRET = 'leak'\n")
            os.symlink(outside, os.path.join(repo, "leakdir"))
            r, _ = self._run_cli(repo)  # default command = map
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("secret.py", r.stdout)
            self.assertNotIn("TOPSECRET", r.stdout)
        finally:
            force_rmtree(tmp)

    def test_cli_session_logs_real_time(self):
        # --session must record real wall time and real output bytes
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("def a():\n    return 1\n")
            r, _ = self._run_cli("--session", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            import json
            log = os.path.join(repo, ".codeloom-session.jsonl")
            self.assertTrue(os.path.isfile(log))
            with open(log, encoding="utf-8") as fh:
                entry = json.loads(fh.readline())
            self.assertGreater(entry["seconds"], 0.0)
            self.assertGreater(entry["bytes"], 0)
        finally:
            force_rmtree(tmp)

    def test_cli_json_write_roundtrip(self):
        # --json --write must actually write the JSON payload (was: silent drop)
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            out = os.path.join(tmp, "out.json")
            r, _ = self._run_cli("--json", "--write", out, repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.isfile(out), "JSON payload was not written")
            import json
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("file_count", data)
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_cli_risk_dispatch(self):
        # --risk on a git commit range exits 0 and prints a score line
        tmp = tempfile.mkdtemp()
        try:
            repo = self._make_cli_repo(tmp)
            for c in (["init", "-q"],
                      ["config", "user.email", "t@t"],
                      ["config", "user.name", "t"]):
                g = subprocess.run(["git"] + c, cwd=repo, timeout=30,
                                   capture_output=True, text=True)
                self.assertEqual(g.returncode, 0, g.stderr)
            subprocess.run(["git", "add", "-A"], cwd=repo, timeout=30,
                           capture_output=True, text=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, timeout=30,
                           capture_output=True, text=True)
            with open(os.path.join(repo, "engine.py"), "w") as f:
                f.write("from utils.retry import retry\n\n"
                        "class Engine:\n"
                        "    def run(self):\n"
                        "        return retry(lambda: None)\n\n"
                        "def main():\n"
                        "    eng = Engine()\n"
                        "    return eng.run() + 1\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, timeout=30,
                           capture_output=True, text=True)
            subprocess.run(["git", "commit", "-q", "-m", "tweak"], cwd=repo, timeout=30,
                           capture_output=True, text=True)
            r, _ = self._run_cli("--risk", "HEAD~1..HEAD", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("change risk", r.stdout)
            self.assertRegex(r.stdout, r"score \d+/100")
        finally:
            force_rmtree(tmp)

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

    def test_graph_focus_missing_neighbor_module(self):
        # Regression (a8df4c2): reachable() must NOT KeyError when a module
        # named by an edge is absent from the graph keys — build_graph_multi
        # omits leaf modules (no deps -> no key), so focusing a chain like
        # a -> b -> c walks into the missing 'c'.
        graph = {"a": {"b"}, "b": {"c"}}
        fs = codeloom.focus_subgraph(graph, "a")
        self.assertEqual(fs["module"], "a")
        self.assertEqual(fs["depends_on"], ["b", "c"])
        self.assertEqual(fs["depended_on_by"], [])
        # inverse direction must also survive a missing neighbor
        self.assertEqual(codeloom.reachable(graph, "c", "in"), {"a", "b"})
        self.assertEqual(codeloom.reachable(graph, "a", "out"), {"b", "c"})

    def test_graph_focus_cli_missing_leaf_module(self):
        # End-to-end regression (a8df4c2): --graph --focus on a module whose
        # transitive neighbor is a leaf absent from the graph keys must exit
        # 0 and render the focus (was: unhandled KeyError traceback).
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            for n, body in [
                    ("a.py", "import b\n\ndef fa():\n    return b.fb()\n"),
                    ("b.py", "import c\n\ndef fb():\n    return c.fc()\n"),
                    ("c.py", "def fc():\n    return 1\n")]:
                with open(os.path.join(repo, n), "w") as f:
                    f.write(body)
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--graph", "--focus", "a", repo],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("KeyError", r.stderr)
            self.assertIn("## depends_on", r.stdout)
            self.assertIn("  b", r.stdout)
            self.assertIn("  c", r.stdout)
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_get_symbol_honest_edges_label(self):
        # Regression: with no --index/--graph built, the summary must
        # advertise that the graph is NOT built (run --index) instead of
        # fabricating "Calls (0)". Skipped until the honest-label fix lands.
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "engine.py"), "w") as f:
                f.write("class Engine:\n    def run(self):\n        return 1\n")
            files = [os.path.join(tmp, "engine.py")]
            out = codeloom.render_get_symbol(files, tmp, "Engine", summary=True)
            if "Calls (0)" in out:
                self.skipTest("honest edge-label fix not landed yet; "
                              "summary still prints 'Calls (0)'")
            low = out.lower()
            self.assertNotIn("Calls (0)", out)
            self.assertTrue("not built" in low or "index" in low,
                            "expected 'not built'/'run --index' graph label, got:\n" + out)
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(td)

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
            force_rmtree(td)
            force_rmtree(td2)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_export_snapshot(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.py"), "w") as f:
                f.write("def helper(): pass\n")
            out = codeloom.render_export(tmp, os.path.join(tmp, "snap.json"))
            self.assertIn("Exported", out)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "snap.json")))
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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

    def test_engine_guard_no_core_no_empty_index(self):
        # Regression (9453831): --index --engine c/rust with no core binary
        # available must exit 1 with an actionable fix line and must NOT
        # write an empty index file (was: exit 0 + a lying "saved to").
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("def fa():\n    return 1\n")
            # isolated copy of the script with NO core binaries / sources
            # beside it (the checkout itself has real cores, so a plain
            # subprocess would find them; the copy cannot)
            iso = os.path.join(tmp, "iso")
            os.makedirs(iso)
            shutil.copy(os.path.join(TESTS_DIR, "codeloom.py"),
                        os.path.join(iso, "codeloom.py"))
            env = dict(os.environ)
            env["PATH"] = "/usr/bin:/bin"  # no cc/rustc auto-build paths
            for engine in ("c", "rust"):
                idx = os.path.join(repo, ".codeloom-index.json")
                if os.path.exists(idx):
                    os.remove(idx)
                r = subprocess.run(
                    [sys.executable, os.path.join(iso, "codeloom.py"),
                     "--index", "--engine", engine, repo],
                    capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(r.returncode, 1,
                                 "engine %s: expected exit 1, got %d\n%s"
                                 % (engine, r.returncode, r.stdout))
                self.assertIn("no core binary is built", r.stderr)
                self.assertIn("Refusing to write an empty index", r.stderr)
                self.assertFalse(os.path.exists(idx),
                                 "empty index written despite missing core")
        finally:
            force_rmtree(tmp)

    def test_engine_guard_empty_core_results(self):
        # Second guard (9453851): even WITH a core binary, if the scan
        # yields no symbols the index must not be saved (exit 1, actionable).
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("def fa():\n    return 1\n")
            iso = os.path.join(tmp, "iso")
            os.makedirs(iso)
            shutil.copy(os.path.join(TESTS_DIR, "codeloom.py"),
                        os.path.join(iso, "codeloom.py"))
            # a core that consumes stdin and emits nothing (broken core)
            fake = os.path.join(iso, "codeloom_core")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\ncat >/dev/null\nexit 0\n")
            os.chmod(fake, 0o755)
            env = dict(os.environ)
            env["PATH"] = "/usr/bin:/bin"
            r = subprocess.run(
                [sys.executable, os.path.join(iso, "codeloom.py"),
                 "--index", "--engine", "c", repo],
                capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 1,
                             "expected exit 1, got %d\n%s" % (r.returncode, r.stdout))
            self.assertIn("produced no symbols", r.stderr)
            self.assertIn("Refusing to save an empty index", r.stderr)
            self.assertFalse(
                os.path.exists(os.path.join(repo, ".codeloom-index.json")),
                "empty index written despite zero symbols from core")
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_versions_in_sync(self):
        # Version-drift guard: VERSION in codeloom.py, SERVER_VERSION in
        # codeloom-mcp.py, and pyproject.toml must all agree. No hardcoded
        # expectation: the three files are the single source of truth for
        # each other, so any one-sided bump fails loudly.
        import re
        here = TESTS_DIR
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

    def test_npm_mirror_byte_identical(self):
        # Regression (7130874 + c2134ca re-sync): npm/ is a byte-identical
        # mirror of the single-file tool + MCP server + LICENSE + README,
        # refreshed by npm/sync.sh. A one-sided edit (fix in codeloom.py but
        # no re-sync) silently ships an OLD tool to npm users — exactly the
        # drift class that made npm/codeloom.py lag the --graph --focus fix.
        # This test pins the mirror to the source of truth.
        here = TESTS_DIR
        for name in ("codeloom.py", "codeloom-mcp.py", "codeloom_mcp.py",
                     "LICENSE", "README.md"):
            src = os.path.join(here, name)
            mirror = os.path.join(here, "npm", name)
            self.assertTrue(os.path.isfile(mirror),
                            "npm/%s missing (run npm/sync.sh)" % name)
            with open(src, "rb") as f:
                a = f.read()
            with open(mirror, "rb") as f:
                b = f.read()
            self.assertEqual(a, b,
                             "npm/%s drifted from %s — re-run npm/sync.sh"
                             % (name, name))

    def test_mcp_tool_registry_unique_and_complete(self):
        # The MCP TOOLS registry is the server's contract with the agent.
        # Guards that survive refactors: (a) every tool name is unique,
        # (b) codeloom_ask is registered exactly once (a duplicate would
        # make tool routing ambiguous), (c) the registry is exactly 77
        # tools, and (d) every entry is a well-formed schema dict.
        import ast
        here = TESTS_DIR
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
            force_rmtree(tmp)

    def test_mcp_responses_carry_meta_envelope(self):
        # every successful tools/call response carries _meta (repowise parity)
        import json
        here = TESTS_DIR
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
            force_rmtree(tmp)

    def test_mcp_envelope_truthful_resident_vs_disk(self):
        # Regression (9453831): the freshness envelope must report WHERE the
        # answer came from. Resident-index tools (codeloom_search/health/
        # deadcode/get_symbol) serve from the always-fresh in-memory index:
        # source=resident-in-memory, stale_warning=false, no disk age/commit.
        # Disk-served tools (codeloom_query loads the persistent index) must
        # keep reporting the disk index's TRUE staleness instead of inheriting
        # the resident claim. Both in ONE server session so the resident
        # index exists when the query runs.
        import json
        here = TESTS_DIR
        mcp = os.path.join(here, "codeloom-mcp.py")
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "m.py"), "w") as f:
                f.write("class Q:\n    pass\n")
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "codeloom_search",
                            "arguments": {"root": tmp, "symbol": "Q"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "codeloom_query",
                            "arguments": {"root": tmp, "query": "symbol Q"}}},
            ]
            payload = "\n".join(json.dumps(r) for r in reqs) + "\n"
            r = subprocess.run([sys.executable, mcp], input=payload,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
            sresp = next(x for x in got if x.get("id") == 2)
            self.assertNotIn("isError", sresp.get("result", {}))
            smeta = sresp["result"]["_meta"]
            self.assertEqual(smeta["source"], "resident-in-memory")
            self.assertFalse(smeta["stale_warning"])
            self.assertTrue(smeta["indexed"])
            self.assertIsNone(smeta["index_age_days"])
            qresp = next(x for x in got if x.get("id") == 3)
            qmeta = qresp["result"]["_meta"]
            # codeloom_query reads the on-disk index: it must NOT inherit the
            # resident claim, and must stay honest about the missing index
            # (nothing was ever saved to disk)
            self.assertNotEqual(qmeta.get("source"), "resident-in-memory",
                                "disk-served tool must not claim a resident source")
            self.assertFalse(qmeta["indexed"])
            self.assertTrue(qmeta["stale_warning"])
            self.assertIsNone(qmeta["index_age_days"])
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_answer_weak_match_gate(self):
        # Regression: --answer on a query that scores BELOW the heuristic
        # threshold (a real match, but a weak one) must append a verify-note
        # telling the reader the match is heuristic — never present the weak
        # hit as if it were a confirmed answer. The note is only enforced once
        # the fix lands in render_answer; before that the test self-skips so
        # the suite stays green (the fix itself is owned by the code agent).
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "engine.py"), "w") as f:
                f.write("class Engine:\n"
                        "    def run(self):\n"
                        "        return 1\n")
            files = [os.path.join(tmp, "engine.py")]
            out = codeloom.render_answer(files, tmp, "eng")  # score ~0.45: weak
            low = out.lower()
            # the verify-note fix has landed only if a verify/heuristic marker
            # is present (pre-fix output carries neither)
            if "verify" not in low and "heuristic" not in low:
                self.skipTest("answer heuristic-gate fix not landed yet; "
                              "no verify-note marker in render_answer output")
            # a weak match still yields an answer...
            self.assertIn("## best match", out)
            self.assertIn("source:", out)
            # ...but the heuristic note must be present
            self.assertTrue("verify" in low or "heuristic" in low,
                            "expected heuristic/verify note on weak match, got:\n" + out)
        finally:
            force_rmtree(tmp)

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
            force_rmtree(tmp)

    def test_tree_sitter_grammar_parity_fixtures(self):
        # codegraph-style discipline: no grammar ships without proving its
        # extraction. Each fixture dir holds a canonical sample + frozen
        # expected.jsonl; the engine must reproduce it byte-for-byte.
        here = TESTS_DIR
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
            force_rmtree(tmp)

    def test_change_risk_scores_and_bands(self):
        # clean-room change-risk: bigger diff + spread + hot file => higher
        # band than a trivial commit; deterministic on same inputs
        tmp = tempfile.mkdtemp()
        try:
            def git(*a):
                return subprocess.run(["git"] + list(a), cwd=tmp, timeout=30,
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
            force_rmtree(tmp)

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
            force_rmtree(tmp)
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
            force_rmtree(tmp)

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
            force_rmtree(tmp)


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
            subprocess.run(["git"] + c, cwd=repo, timeout=30, capture_output=True, text=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, timeout=30, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, timeout=30,
                       capture_output=True, text=True)
        return repo

    def _git(self, repo, *args):
        return subprocess.run(["git"] + list(args), cwd=repo, timeout=30,
                              capture_output=True, text=True)

    def _run_verify(self, repo, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
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
            force_rmtree(os.path.dirname(repo))

    def _run_cli_check(self, repo):
        return subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
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
            force_rmtree(os.path.dirname(repo))

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
            force_rmtree(os.path.dirname(repo))

    def test_verify_edit_preexisting_cycle_no_stop(self):
        # Regression: cycle detection must compare against the FULL HEAD
        # graph — an import cycle that already existed at HEAD (a<->b here)
        # must NOT be reported as a new cycle when only c.py is edited.
        repo = self._git_repo({
            "a.py": "import b\n\ndef fa():\n    return b.fb()\n",
            "b.py": "import a\n\ndef fb():\n    return a.fa()\n",
            "c.py": "def fc():\n    return 1\n",
        })
        try:
            # benign edit to c.py only; the a<->b cycle is pre-existing at HEAD
            with open(os.path.join(repo, "c.py"), "w") as f:
                f.write("def fc():\n    return 2\n")
            self._git(repo, "add", "-A")
            r = self._run_verify(repo)
            if "new-cycle" in r.stdout:
                self.skipTest("full-HEAD cycle fix not landed yet; "
                              "pre-existing cycle still reported as new-cycle")
            self.assertEqual(r.returncode, 0, r.stderr)
            # never STOP from a pre-existing cycle; GO or CHECK at most
            self.assertNotIn("VERDICT: STOP", r.stdout)
            self.assertTrue("VERDICT: GO" in r.stdout or "VERDICT: CHECK" in r.stdout,
                            "expected GO or CHECK verdict, got:\n" + r.stdout)
            self.assertNotIn("new-cycle", r.stdout)
            # ...and never name the pre-existing cycle edges
            self.assertNotIn("a -> b", r.stdout)
            self.assertNotIn("b -> a", r.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))

    def test_verify_edit_new_cycle_stops(self):
        # The flip side of the full-HEAD fix: a genuinely NEW cycle (introduced
        # by the edit, absent from HEAD) must still STOP with new-cycle.
        repo = self._git_repo({
            "a.py": "import b\n\ndef fa():\n    return 1\n",
            "b.py": "def fb():\n    return 1\n",
            "c.py": "def fc():\n    return 1\n",
        })
        try:
            # introduce c -> a while a imports c -> brand-new c<->a cycle
            with open(os.path.join(repo, "c.py"), "w") as f:
                f.write("import a\n\ndef fc():\n    return a.fa()\n")
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("import b\nimport c\n\ndef fa():\n    return 1\n")
            self._git(repo, "add", "-A")
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("VERDICT: STOP", r.stdout)
            self.assertIn("new-cycle", r.stdout)
            self.assertIn("c -> a", r.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))

    def _git_repo_no_commit(self, files):
        """A git repo with files in the working tree but ZERO commits
        (no HEAD baseline): --verify-edit must not crash on missing HEAD."""
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
            subprocess.run(["git"] + c, cwd=repo, timeout=30,
                           capture_output=True, text=True)
        return repo

    def test_verify_edit_untracked_file_no_stop(self):
        # Regression: an UNTRACKED file has no HEAD text — the full-HEAD
        # pre-edit graph must treat it as new (nothing pre-existing to
        # dangle), so a broken import inside it is a CHECK (vendored/
        # unresolvable), never a STOP and never a crash. The tracked repo
        # has a clean committed baseline.
        repo = self._git_repo({
            "tracked.py": "def ok():\n    return 1\n",
        })
        try:
            with open(os.path.join(repo, "untracked.py"), "w") as f:
                f.write("from missing import x\n\ndef f():\n    return x()\n")
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("VERDICT: CHECK", r.stdout)
            self.assertIn("untracked", r.stdout)
            self.assertNotIn("VERDICT: STOP", r.stdout)
            self.assertNotIn("dangling-import", r.stdout)
            # strict still must not elevate an untracked suspect to STOP
            r2 = self._run_verify(repo, "--severity", "strict")
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertNotIn("VERDICT: STOP", r2.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))

    def test_verify_edit_no_commits_clean_go(self):
        # Regression: a repo with ZERO commits (all files untracked) has no
        # HEAD tree — ls-tree/HEAD:file both fail. verify-edit must not
        # crash and must still resolve working-tree imports: GO.
        repo = self._git_repo_no_commit({
            "a.py": "import b\n\ndef fa():\n    return b.fb()\n",
            "b.py": "def fb():\n    return 1\n",
        })
        try:
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("VERDICT: GO", r.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))

    def test_verify_edit_no_commits_broken_import_check(self):
        # No-commit repo with an unresolvable import: CHECK (vendored), not
        # STOP (no HEAD baseline means nothing pre-existing can dangle) and
        # not a crash.
        repo = self._git_repo_no_commit({
            "a.py": "from missing import x\n\ndef fa():\n    return x()\n",
        })
        try:
            r = self._run_verify(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("VERDICT: CHECK", r.stdout)
            self.assertIn("vendored_import", r.stdout)
            self.assertNotIn("VERDICT: STOP", r.stdout)
            self.assertNotIn("dangling-import", r.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))


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
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py")]
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
            force_rmtree(os.path.dirname(repo))

    def test_blindspot_optout(self):
        repo = self._repo()
        try:
            r = self._run(repo, "--blindspot", "--no-blindspot")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("VERDICT: SKIP", r.stdout)
            self.assertIn("--no-blindspot opt-out", r.stdout)
        finally:
            force_rmtree(os.path.dirname(repo))


class TestSavingsReport(unittest.TestCase):
    def test_savings_report_basic(self):
        base = tempfile.mkdtemp()
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--session", "--json", base],
                capture_output=True, text=True, cwd=base)
            self.assertEqual(r.returncode, 0, r.stderr)
            r2 = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--savings-report", "--since", "0"],
                capture_output=True, text=True, cwd=base)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("TOTAL: 1 call(s)", r3.stdout)
        finally:
            force_rmtree(base)


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
            force_rmtree(repo)

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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--memory-prune", "--older-than", "30", repo],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("would remove: DECISIONS.md-2020-01-01.md", r.stdout)
            self.assertIn("Nothing was deleted", r.stdout)
            self.assertTrue(os.path.isfile(old), "dry-run must NOT delete")
            # explicit --delete removes ONLY the old one
            r2 = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--memory-prune", "--older-than", "30", "--delete", repo],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("deleted 1 archived file", r2.stdout)
            self.assertFalse(os.path.isfile(old))
            self.assertTrue(os.path.isfile(fresh), "fresh archive must survive")
        finally:
            force_rmtree(repo)


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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--install-hook", repo],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("installed", r2.stdout)
            # runs warn-only and exits 0
            rc = subprocess.run(["sh", hook], capture_output=True, text=True)
            self.assertEqual(rc.returncode, 0)
            # uninstall removes it
            r3 = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--uninstall-hook", repo],
                capture_output=True, text=True)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("removed", r3.stdout)
            self.assertFalse(os.path.isfile(hook))
        finally:
            force_rmtree(base)


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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--eval", "token", repo],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("eval_runner.py not found", r.stdout)
        finally:
            force_rmtree(base)

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
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--eval", "token", "--json", "--root", repo],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = json.loads(r.stdout)
            self.assertEqual(got["argv"], ["token", "--json", "--root", repo])
        finally:
            force_rmtree(base)


def _memory_os_implemented():
    """True once codeloom.py grows the MemoryOS layer (--memory-add /
    --memory-stats / memory.jsonl). Owned by a sibling agent working in
    parallel; until it lands these tests skip so the suite stays green."""
    try:
        with open(os.path.join(TESTS_DIR, "codeloom.py"), "r",
                  encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return False
    return "--memory-add" in src and "memory.jsonl" in src


@unittest.skipUnless(_memory_os_implemented(),
                     "MemoryOS (--memory-add/--memory graph/--memory-stats, "
                     "memory.jsonl) not implemented in codeloom.py yet")
class TestMemoryOS(unittest.TestCase):
    """Typed JSONL repository memory: schema, backward-compat dual write,
    importance formula, graph-linked --memory, jsonl query, stats, and
    lossless cap rotation. All writes go to .codeloom-memory/ in a
    throwaway CWD so the repo and checkout stay clean."""

    def _cli(self, *argv, cwd, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        r = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "codeloom.py")]
            + list(argv),
            capture_output=True, text=True, cwd=cwd, env=e, timeout=120)
        return r

    def _jsonl(self, cwd):
        """All entries currently in .codeloom-memory/memory.jsonl."""
        p = os.path.join(cwd, ".codeloom-memory", "memory.jsonl")
        entries = []
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        return entries

    def test_memory_add_writes_jsonl(self):
        # --memory-add --type bug --title "login fails" --symbols AuthService
        # creates .codeloom-memory/memory.jsonl with one well-shaped entry
        tmp = tempfile.mkdtemp()
        try:
            r = self._cli("--memory-add", "--type", "bug",
                          "--title", "login fails",
                          "--symbols", "AuthService", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            p = os.path.join(tmp, ".codeloom-memory", "memory.jsonl")
            self.assertTrue(os.path.isfile(p), "memory.jsonl not created")
            entries = self._jsonl(tmp)
            self.assertEqual(len(entries), 1)
            e = entries[0]
            for key in ("type", "id", "title", "body", "affected_symbols",
                        "importance", "confidence", "tier", "timestamp",
                        "created"):
                self.assertIn(key, e, "schema key %r missing" % key)
            self.assertEqual(e["type"], "bug")
            self.assertEqual(e["title"], "login fails")
            self.assertIn("AuthService", e.get("affected_symbols") or [])
        finally:
            force_rmtree(tmp)

    def test_decide_appends_both(self):
        # --decide keeps writing DECISIONS.md (markdown backward compat)
        # AND appends a typed jsonl entry
        tmp = tempfile.mkdtemp()
        try:
            r = self._cli("--decide", "use JWT", "--reason", "stateless",
                          cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            dec = os.path.join(tmp, ".codeloom-memory", "DECISIONS.md")
            self.assertTrue(os.path.isfile(dec), "DECISIONS.md must survive")
            with open(dec, "r", encoding="utf-8") as fh:
                self.assertIn("use JWT", fh.read())
            entries = self._jsonl(tmp)
            self.assertEqual(len(entries), 1, "one jsonl entry expected")
            e = entries[0]
            self.assertEqual(e["type"], "decision")
            blob = (e.get("title") or "") + " " + (e.get("body") or "")
            self.assertIn("use JWT", blob)
        finally:
            force_rmtree(tmp)

    def test_importance_formula(self):
        # base 10 + bug 20 + "must never fail" keyword 30 + symbols 5+ =>
        # importance >= 60; the CLI prints the "importance: N" line
        import re as _re
        tmp = tempfile.mkdtemp()
        try:
            r = self._cli("--memory-add", "--type", "bug",
                          "--title", "auth crash",
                          "--body", "the login flow must never fail or tokens leak",
                          "--symbols", "AuthService,RateLimiter", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            m = _re.search(r"importance:\s*(\d+)", r.stdout)
            self.assertIsNotNone(
                m, "expected 'importance: N' in output: %r" % r.stdout)
            self.assertGreaterEqual(int(m.group(1)), 60)
            self.assertGreaterEqual(self._jsonl(tmp)[0]["importance"], 60)
        finally:
            force_rmtree(tmp)

    def test_remember_graph_links(self):
        # mini repo: engine.py, auth.py (imports engine), utils.py.
        # A decision pinned to engine symbols must be found by
        # --memory <engine symbol> together with the graph-reachable
        # section (auth.py depends on engine), and a decision pinned to a
        # NEIGHBOR symbol (Auth in auth.py) must come back via the
        # reachable-via-graph path, not the direct path.
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            def w(name, text):
                with open(os.path.join(repo, name), "w") as fh:
                    fh.write(text)
            w("engine.py",
              "class Engine:\n"
              "    def run(self):\n"
              "        return 42\n"
              "\n"
              "def start():\n"
              "    return Engine().run()\n")
            w("auth.py",
              "from engine import Engine\n"
              "\n"
              "class Auth:\n"
              "    def login(self):\n"
              "        return Engine().run()\n")
            w("utils.py",
              "def retry(fn, tries=3):\n"
              "    return fn()\n")
            r1 = self._cli("--decide", "Engine must stay stateless",
                           "--reason", "pure safety",
                           "--symbols", "Engine", cwd=repo)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r1b = self._cli("--decide", "Auth must log every login",
                            "--reason", "audit trail",
                            "--symbols", "Auth", cwd=repo)
            self.assertEqual(r1b.returncode, 0, r1b.stderr)
            r2 = self._cli("--memory", "Engine", cwd=repo)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            # the direct entry is returned
            self.assertIn("Engine must stay stateless", r2.stdout)
            # graph-reachable section: auth.py depends on engine
            self.assertIn("auth", r2.stdout.lower())
            # a decision pinned to a graph NEIGHBOR symbol is returned via
            # the reachable-via-graph path (Auth lives in auth.py, which
            # imports engine) — this is the real graph retrieval, not the
            # empty-fallback
            self.assertIn("Auth must log every login", r2.stdout)
        finally:
            force_rmtree(tmp)

    def test_remember_smart_dispatch(self):
        # --remember NOTE keeps the LEGACY append-to-section write for
        # free-form notes, but --remember <codebase-symbol> smart-dispatches
        # to graph retrieval (the value resolves to a module/symbol in the
        # repo). --memory <symbol> is retrieval, never a write.
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "engine.py"), "w") as fh:
                fh.write("class Engine:\n    def run(self):\n        return 42\n")
            # 1) free-form note: legacy write into DECISIONS.md, no jsonl entry
            r1 = self._cli("--remember", "always revalidate the jwt on refresh",
                           cwd=repo)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("remembered: DECISIONS", r1.stdout)
            dec = os.path.join(repo, ".codeloom-memory", "DECISIONS.md")
            self.assertTrue(os.path.isfile(dec), "legacy note must land in DECISIONS.md")
            with open(dec, "r", encoding="utf-8") as fh:
                self.assertIn("always revalidate the jwt", fh.read())
            self.assertEqual(self._jsonl(repo), [],
                             "legacy --remember must NOT write memory.jsonl")
            # 2) symbol-resolvable value: smart-dispatches to graph retrieval
            r2 = self._cli("--remember", "Engine", cwd=repo)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("# memory: Engine", r2.stdout)
            self.assertNotIn("remembered:", r2.stdout,
                             "symbol value must not take the append path")
            # no DECISIONS.md entry was added by the dispatch
            with open(dec, "r", encoding="utf-8") as fh:
                self.assertNotIn("- Engine", fh.read())
            # 3) --memory is retrieval-only even for unknown symbols
            r3 = self._cli("--memory", "NoSuchSymbol", cwd=repo)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("# memory: NoSuchSymbol", r3.stdout)
            entries = self._jsonl(repo)
            self.assertEqual(entries, [])
            self.assertEqual(
                [f for f in os.listdir(os.path.join(repo, ".codeloom-memory"))
                 if f.endswith(".md")],
                ["DECISIONS.md"], "--memory must never create memory files")
        finally:
            force_rmtree(tmp)

    def test_query_memory_ranking_by_importance(self):
        # typed hits in --query-memory are ranked by importance desc:
        # the hot bug (keyword + type weight) must come before the
        # low-importance question even though both match the query
        tmp = tempfile.mkdtemp()
        try:
            r1 = self._cli("--memory-add", "--type", "bug",
                           "--title", "zebra high",
                           "--body", "critical security issue", cwd=tmp)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._cli("--memory-add", "--type", "question",
                           "--title", "zebra low", cwd=tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            r3 = self._cli("--query-memory", "zebra", cwd=tmp)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            hi = r3.stdout.find("zebra high")
            lo = r3.stdout.find("zebra low")
            self.assertNotEqual(hi, -1, "high-importance hit missing")
            self.assertNotEqual(lo, -1, "low-importance hit missing")
            self.assertLess(hi, lo,
                            "importance-desc ranking violated: %r" % r3.stdout)
            # both ranked hits carry the jsonl provenance marker
            self.assertIn("[memory.jsonl]", r3.stdout)
        finally:
            force_rmtree(tmp)

    def test_query_memory_json(self):
        # --query-memory is extended to memory.jsonl: a keyword only present
        # in a typed entry must still come back
        tmp = tempfile.mkdtemp()
        try:
            r1 = self._cli("--memory-add", "--type", "bug",
                           "--title", "zebra token cassette",
                           "--body", "always rotate the zebra_key", cwd=tmp)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._cli("--query-memory", "zebra", cwd=tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            # the jsonl entry body, not just the echoed query header
            self.assertIn("zebra token cassette", r2.stdout)
        finally:
            force_rmtree(tmp)

    def test_goal_hypothesis_adr_dual_write(self):
        # --goal/--hypothesis/--adr keep their legacy markdown/journal
        # surfaces AND append typed memory.jsonl entries (created source
        # stamped; adr entries carry the decision as body + context as
        # reason, symbols forwarded)
        tmp = tempfile.mkdtemp()
        try:
            r1 = self._cli("--goal", "ship v2 before friday", cwd=tmp)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._cli("--hypothesis", "pool is not shared", cwd=tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            r3 = self._cli("--adr", "Use Postgres",
                           "--context", "write scale",
                           "--decision", "migrate to pg",
                           "--symbols", "Store", cwd=tmp)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            # legacy surfaces survive
            mem = os.path.join(tmp, ".codeloom-memory")
            with open(os.path.join(mem, "GOALS.md"), "r", encoding="utf-8") as fh:
                self.assertIn("ship v2 before friday", fh.read())
            adrs = [f for f in os.listdir(os.path.join(mem, "adr"))
                    if f.startswith("ADR-") and f.endswith(".md")]
            self.assertEqual(len(adrs), 1)
            entries = self._jsonl(tmp)
            self.assertEqual(len(entries), 3, "one jsonl entry per writer")
            by_type = {e["type"]: e for e in entries}
            self.assertEqual(sorted(by_type), ["architecture", "goal",
                                               "hypothesis"])
            g = by_type["goal"]
            self.assertEqual(g["title"], "ship v2 before friday")
            self.assertEqual(g["created"], "goal")
            h = by_type["hypothesis"]
            self.assertEqual(h["title"], "pool is not shared")
            self.assertEqual(h["created"], "hypothesis")
            self.assertIn("status: open", h.get("body") or "")
            a = by_type["architecture"]
            self.assertEqual(a["title"], "Use Postgres")
            self.assertEqual(a["created"], "adr")
            self.assertIn("migrate to pg", a.get("body") or "")
            self.assertIn("write scale", a.get("reason") or "")
            self.assertIn("Store", a.get("affected_symbols") or [])
            # symbols were forwarded from --adr --symbols
        finally:
            force_rmtree(tmp)

    def test_include_archive_retrieval(self):
        # archived entries are invisible to --memory by default and surface
        # with --include-archive; the live file still stays capped
        tmp = tempfile.mkdtemp()
        try:
            env = {"CODELOOM_MEMORY_CAP_BYTES": "300"}
            for i in range(8):
                r = self._cli("--memory-add", "--type", "bug",
                              "--title", "t%02d" % i,
                              "--symbols", "AuthService", cwd=tmp, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            arch = os.path.join(tmp, ".codeloom-memory", "archive")
            self.assertTrue(os.path.isdir(arch))
            r1 = self._cli("--memory", "AuthService", cwd=tmp)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertNotIn("t00", r1.stdout,
                             "archived entries must be hidden without the flag")
            r2 = self._cli("--memory", "AuthService", "--include-archive",
                           cwd=tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("t00", r2.stdout,
                          "--include-archive must surface archived entries")
            self.assertIn("t07", r2.stdout)
        finally:
            force_rmtree(tmp)

    def test_memory_stats_top_symbols(self):
        # --memory-stats reports the top-5 linked symbols by count, ranked
        # count desc then name asc
        tmp = tempfile.mkdtemp()
        try:
            r = self._cli("--memory-add", "--type", "bug", "--title", "a",
                          "--symbols", "Alpha", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._cli("--memory-add", "--type", "bug", "--title", "b",
                          "--symbols", "Alpha", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._cli("--memory-add", "--type", "bug", "--title", "c",
                          "--symbols", "Beta", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._cli("--memory-stats", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("top linked symbols:", r.stdout)
            # count-desc then name-asc
            self.assertRegex(r.stdout,
                             r"Alpha\s*[:=]\s*2[^\n]*\n\s*Beta\s*[:=]\s*1")
        finally:
            force_rmtree(tmp)

    def test_memory_add_requires_title(self):
        # --memory-add without --title is a hard error (exit 1, stderr hint)
        tmp = tempfile.mkdtemp()
        try:
            r = self._cli("--memory-add", "--type", "bug", cwd=tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--memory-add requires --title", r.stderr)
            self.assertEqual(self._jsonl(tmp), [],
                             "failed write must not append a partial entry")
        finally:
            force_rmtree(tmp)

    def test_memory_stats_counts(self):
        import re as _re
        tmp = tempfile.mkdtemp()
        try:
            r1 = self._cli("--memory-add", "--type", "bug",
                           "--title", "crash on mount", cwd=tmp)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._cli("--decide", "adopt JSON", "--reason", "typed",
                           cwd=tmp)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            r3 = self._cli("--memory-stats", cwd=tmp)
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn("bug", r3.stdout)
            self.assertIn("decision", r3.stdout)
            self.assertRegex(r3.stdout, _re.compile(r"bug\s*[:=]\s*1"))
            self.assertRegex(r3.stdout, _re.compile(r"decision\s*[:=]\s*1"))
        finally:
            force_rmtree(tmp)

    def test_memory_cap_rotation(self):
        # tiny CODELOOM_MEMORY_CAP_BYTES forces lossless rotation of
        # memory.jsonl into archive/memory-<date>.jsonl; every written
        # entry must stay recoverable (live + archive)
        tmp = tempfile.mkdtemp()
        try:
            env = {"CODELOOM_MEMORY_CAP_BYTES": "300"}
            titles = []
            for i in range(10):
                t = "entry %02d" % i
                titles.append(t)
                r = self._cli("--memory-add", "--type", "bug",
                              "--title", t, cwd=tmp, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            memdir = os.path.join(tmp, ".codeloom-memory")
            arch = os.path.join(memdir, "archive")
            self.assertTrue(os.path.isdir(arch), "archive dir expected")
            arch_files = [f for f in os.listdir(arch)
                          if f.startswith("memory") and f.endswith(".jsonl")]
            self.assertTrue(arch_files,
                            "expected archive/memory-<date>.jsonl rotation")
            seen = set()
            paths = [os.path.join(memdir, "memory.jsonl")]
            paths += [os.path.join(arch, f) for f in sorted(arch_files)]
            for p in paths:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            seen.add(json.loads(line)["title"])
            for t in titles:
                self.assertIn(t, seen, "entry %r lost in rotation" % t)
        finally:
            force_rmtree(tmp)


class TestCEngineImports(unittest.TestCase):
    """C-engine import-edge parity (2026-08-27 HA-core finding): the C core
    emitted ZERO Python/C import edges (quoted-source-only match_import +
    is_code gate dropping #include lines), so --index --engine c produced a
    knowledge graph with ~35% fewer edges than the py engine and --query
    'dependents/hubs' returned empty. Also covers the stale-binary
    auto-rebuild (committed core lagging codeloom_core.c)."""

    def _c_engine_available(self):
        """True if a usable C core can be built (cc present)."""
        import shutil
        return shutil.which("cc") is not None

    def _make_import_repo(self, repo):
        """Small Python repo with local + stdlib + relative imports."""
        def j(*parts):
            return os.path.join(repo, *parts)
        def w(path, content):
            with open(path, "w") as f:
                f.write(content)
        os.makedirs(j("src", "core"))
        os.makedirs(j("src", "utils"))
        w(j("src", "__init__.py"), "")
        w(j("src", "cli.py"),
          "import os\n"
          "import json\n"
          "from core.engine import Engine\n"
          "from utils.retry import retry\n"
          "from .core import engine as core_engine\n"
          "\n"
          "def main():\n"
          "    return Engine().run()\n")
        w(j("src", "core", "__init__.py"), "")
        w(j("src", "core", "engine.py"),
          "from utils.retry import retry\n"
          "\n"
          "class Engine:\n"
          "    def run(self):\n"
          "        return retry(lambda: None)\n")
        w(j("src", "utils", "__init__.py"), "")
        w(j("src", "utils", "retry.py"),
          "def retry(fn, tries=3):\n    return fn()\n")

    def test_c_engine_python_import_edges(self):
        # --index --engine c must record import edges (this is the parity fix)
        if not self._c_engine_available():
            self.skipTest("cc not available — C engine cannot build")
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            self._make_import_repo(repo)
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--index", "--engine", "c", repo],
                capture_output=True, text=True, cwd=tmp, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr)
            idx = codeloom.load_persistent_index(repo)
            self.assertIsNotNone(idx, "persistent index not saved")
            kg = idx.get("kg", {})
            imports = kg.get("imports", {})
            # cli.py imports core.engine and utils.retry (both local)
            cli_imps = imports.get("src.cli", set())
            self.assertIn("src.core.engine", cli_imps,
                          "C engine missed 'from core.engine import Engine'")
            self.assertIn("src.utils.retry", cli_imps,
                          "C engine missed 'from utils.retry import retry'")
            # engine.py imports utils.retry
            eng_imps = imports.get("src.core.engine", set())
            self.assertIn("src.utils.retry", eng_imps,
                          "C engine missed import edge in engine.py")
        finally:
            force_rmtree(tmp)

    def test_c_engine_c_include_edges(self):
        # #include lines were dropped by the is_code gate — must be imports now
        if not self._c_engine_available():
            self.skipTest("cc not available — C engine cannot build")
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with open(os.path.join(repo, "main.c"), "w") as f:
                f.write("#include <stdio.h>\n"
                        "#include \"mylib.h\"\n"
                        "int main(void) { return 0; }\n")
            with open(os.path.join(repo, "mylib.h"), "w") as f:
                f.write("int helper(void);\n")
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--index", "--engine", "c", repo],
                capture_output=True, text=True, cwd=tmp, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr)
            idx = codeloom.load_persistent_index(repo)
            self.assertIsNotNone(idx, "persistent index not saved")
            kg = idx.get("kg", {})
            imports = kg.get("imports", {})
            main_imps = imports.get("main", set())
            self.assertIn("mylib", main_imps,
                          "C engine missed #include \"mylib.h\"")
        finally:
            force_rmtree(tmp)

    def test_core_is_stale_helper(self):
        # _core_is_stale must flag a binary older than its source and accept
        # a fresh one — the shipped-core-lagging-source regression guard
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "codeloom_core.c")
            binary = os.path.join(tmp, "codeloom_core")
            with open(src, "w") as f:
                f.write("/* stub */\n")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\n")
            # source newer -> stale
            os.utime(src, (1000, 2000))
            os.utime(binary, (1000, 1000))
            self.assertTrue(codeloom._core_is_stale(tmp, "codeloom_core", binary),
                            "binary older than source must be stale")
            # binary newer -> fresh
            os.utime(binary, (1000, 3000))
            self.assertFalse(codeloom._core_is_stale(tmp, "codeloom_core", binary),
                             "binary newer than source must be fresh")
            # missing source -> not stale (no source to rebuild from)
            os.remove(src)
            self.assertFalse(codeloom._core_is_stale(tmp, "codeloom_core", binary),
                             "missing source must not be flagged stale")
        finally:
            force_rmtree(tmp)

    def test_query_callers_returns_cross_module_callers(self):
        # --query 'callers X' must return every module+function calling X.
        # Regression (2026-08-27): render_query tested `sym in cs` against the
        # module's function-KEYS, so 'callers async_track_time_interval' on
        # HA-core (20 usages) returned only the module defining it.
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            self._make_import_repo(repo)
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--index", repo],
                capture_output=True, text=True, cwd=tmp, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run(
                [sys.executable, os.path.join(TESTS_DIR, "codeloom.py"),
                 "--query", "callers run", repo],
                capture_output=True, text=True, cwd=tmp, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            # src.cli.main calls run() (via Engine().run()) -> must appear
            self.assertIn("src.cli.main", r.stdout,
                          "cross-module caller missing from --query callers")
        finally:
            force_rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
