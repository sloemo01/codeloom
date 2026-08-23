#!/usr/bin/env python3
"""Memory OS benchmark: codeloom typed memory (--memory-add / --memory / --query-memory).

Measures the "never forgets" layer on the LANDED Memory OS surface — typed
memory.jsonl entries + graph-linked retrieval:

1. BUILD   a small synthetic repo (5 files, real symbols + real import/call
           edges) if --repo is absent, or copy the given repo to a scratch
           dir (never touches the real repo's .codeloom-memory).
2. SEED    N=20 typed entries via the typed surface:
             --memory-add --type TYPE --title T --body B --symbols S
           each written with affected_symbols pinned to a real symbol in the
           repo. Plus graph-link entries: one extra entry per query, pinned
           to a symbol in an import-graph NEIGHBOR module of the query
           symbol's module (the "memory recorded for a dependency, retrieved
           via the queried symbol" case).
3. RECALL  PRIMARY ROW (the Memory OS differentiator): for 10 query symbols,
           `--memory <symbol>` graph-linked retrieval must return (a) the
           entry pinned to the symbol, in the "## entries linked to <symbol>"
           section, and (b) the neighbor-pinned entry in the
           "## reachable via graph" section — memory pinned to a graph
           neighbor is surfaced when the symbol itself is queried. The
           neighbor-module list codeloom prints is parsed and compared for
           exact equality against the script's own graph model (independent
           prediction on the synthetic repo; a pre-seeding probe of
           codeloom's own graph on real repos).
4. RECALL  BASELINE ROW: the same 10 symbols via `--query-memory <symbol>`
           (full-text term match over plain-text + typed memory) — the
           pre-Memory-OS surface, kept as the comparison row.
5. LATENCY avg wall time per query over N runs, both rows.
6. TOKENS   retrieval output size (tiktoken cl100k_base; zero-dep fallback
           bytes/4, same convention as the other benches) vs the naive
           baseline: `grep -rn <symbol> repo` + read the top 3 matching
           files, same 10 queries.

Output: per-query table + summary lines, e.g.
    --memory retrieval:   10/10 direct hits, 10/10 graph hits, X ms avg, Y tok vs baseline Z tok (Q% fewer)
    --query-memory row:   10/10 hits, X ms avg, Y tok

Honest framing: entries are SCRIPTED (generated deterministically, not real
agent history) — they exercise the storage + retrieval paths, not the quality
of what an agent would choose to remember. Loss rows are printed, never
filtered. On real repos every invocation pays cold Python startup + a full
tree walk (and --memory additionally builds the import + call graphs) —
measured and reported as wall time.

Usage:
    python3 benchmarks/memory_eval.py [--repo /tmp/bench-fastapi] [--seed N] [--runs 3]
"""
import argparse
import ast
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import tiktoken
    ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(t: str) -> int:
        return len(ENC.encode(t))
except ImportError:  # zero-dep fallback (bytes/4), same convention as other benches
    def count_tokens(t: str) -> int:
        return len(t.encode("utf-8")) // 4

CODELOOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")
PY = sys.executable or "python3"

# Typed write path is the SAME for every type: --memory-add --type TYPE.
TYPE_ORDER = ["bug", "decision", "architecture", "api", "constraint"]

# Synthetic 5-file repo. Imports are real (engine -> router/cache, auth ->
# utils, router -> utils, cache -> utils) so `--memory <symbol>` has a real
# graph to expand.
SYNTHETIC_FILES = {
    "auth.py": (
        "from utils import normalize\n"
        "\n"
        "class TokenStore:\n"
        "    def __init__(self):\n"
        "        self.tokens = {}\n"
        "    def issue_token(self, user):\n"
        "        self.tokens[user] = 't' + user\n"
        "        return self.tokens[user]\n"
        "\n"
        "class AuthError(Exception):\n"
        "    pass\n"
        "\n"
        "def validate_token(token):\n"
        "    if not token:\n"
        "        raise AuthError('empty token')\n"
        "    return token in TokenStore().tokens\n"
        "\n"
        "def authenticate(user, password):\n"
        "    t = TokenStore().issue_token(user)\n"
        "    return validate_token(t) and password == 'x'\n"
    ),
    "router.py": (
        "from utils import normalize\n"
        "\n"
        "class Router:\n"
        "    def __init__(self):\n"
        "        self.routes = {}\n"
        "    def add_route(self, path, handler):\n"
        "        self.routes[normalize(path)] = handler\n"
        "        return handler\n"
        "\n"
        "class RouteMatch:\n"
        "    pass\n"
        "\n"
        "def handle_request(request):\n"
        "    return request.method, request.path\n"
        "\n"
        "def resolve(path):\n"
        "    return Router().routes.get(path)\n"
    ),
    "cache.py": (
        "from utils import hash_key\n"
        "\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self.capacity = capacity\n"
        "        self.order = []\n"
        "    def get(self, key):\n"
        "        return None\n"
        "    def put(self, key, value):\n"
        "        self.order.append(key)\n"
        "\n"
        "class CacheEntry:\n"
        "    def __init__(self, key, value):\n"
        "        self.key, self.value = key, value\n"
        "\n"
        "def evict(cache):\n"
        "    return cache.order.pop(0)\n"
        "\n"
        "def ttl_of(entry):\n"
        "    return 60\n"
    ),
    "engine.py": (
        "from router import Router, handle_request\n"
        "from cache import LRUCache\n"
        "from config import load_config\n"
        "\n"
        "class Engine:\n"
        "    def __init__(self):\n"
        "        self.cache = LRUCache(10)\n"
        "        self.router = Router()\n"
        "    def run(self, request):\n"
        "        return handle_request(request)\n"
        "\n"
        "class Pool:\n"
        "    def __init__(self, size):\n"
        "        self.size = size\n"
        "    def acquire(self):\n"
        "        return None\n"
        "    def release(self, conn):\n"
        "        pass\n"
        "\n"
        "def init_engine(config):\n"
        "    e = Engine()\n"
        "    e.settings = load_config(config)\n"
        "    return e\n"
    ),
    "utils.py": (
        "def retry(fn, times=3):\n"
        "    for _ in range(times):\n"
        "        try:\n"
        "            return fn()\n"
        "        except Exception:\n"
        "            pass\n"
        "    return None\n"
        "\n"
        "def clamp(value, lo, hi):\n"
        "    return max(lo, min(hi, value))\n"
        "\n"
        "def normalize(text):\n"
        "    return text.strip().lower()\n"
        "\n"
        "def hash_key(key):\n"
        "    return abs(hash(key)) % 997\n"
    ),
    "config.py": (
        "class Settings:\n"
        "    def __init__(self):\n"
        "        self.debug = False\n"
        "        self.host = '127.0.0.1'\n"
        "\n"
        "class ConfigDict(dict):\n"
        "    pass\n"
        "\n"
        "def load_config(path):\n"
        "    return ConfigDict()\n"
        "\n"
        "def reload_config():  # pragma: no cover\n"
        "    return load_config('config.json')\n"
    ),
}

# Natural-language note per (type, symbol). Title + body name the symbol
# verbatim (full-text matchable by --query-memory); affected_symbols come from
# --symbols (the --memory <symbol> graph link, independent of the text).
NOTE_TEMPLATES = {
    "bug": "BUG {sym}: {sym} raised {exc} on empty input in {file}; fixed by guarding before calling {sym}",
    "decision": "DECISION: keep {sym} stateless; never persist {sym} state across runs",
    "architecture": "ARCHITECTURE: {sym} is the hub; {file} routes through {sym} before {dep}",
    "api": "API: {sym}() returns str; callers must validate with {dep} before use",
    "constraint": "CONSTRAINT: {sym} must stay thread-safe; no module-level globals in {file}",
}
EXC = ["AuthError", "RouteMatch", "ValueError", "TypeError", "KeyError"]


def run(cmd, cwd=None):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=600)
    return r, (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------- #
# repo helpers (shared by synthetic and real repos)
# --------------------------------------------------------------------------- #
def module_name_of(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    if rel.endswith(".py"):
        rel = rel[: -len(".py")]
    if rel.endswith("__init__"):
        rel = rel[: -len("__init__")].rstrip(".")  # mirror codeloom: __init__.py -> package
    return rel.replace(os.sep, ".")


def build_synthetic_repo(root: str) -> list:
    """Write the 5-file synthetic repo. Returns [(module, symbol)] tuples."""
    syms = []
    for fname, body in SYNTHETIC_FILES.items():
        with open(os.path.join(root, fname), "w", encoding="utf-8") as fh:
            fh.write(body)
        for m in re.finditer(r"^(?:class|def)\s+(\w+)", body, re.M):
            syms.append((fname[:-3], m.group(1)))
    return sorted(syms)


def extract_symbols(repo: str) -> list:
    """Real symbols from the target repo (any .py files). [(module, symbol)]."""
    syms = []
    for dirpath, _dirs, files in os.walk(repo):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in re.finditer(r"^(?:class|def)\s+(\w+)", text, re.M):
                syms.append((module_name_of(p, repo), m.group(1)))
    return sorted(syms)


def file_imports(path: str) -> set:
    """Top-level module names imported by a .py file (ast)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (SyntaxError, OSError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def module_call_callees(path: str, all_defined: set) -> set:
    """Callee symbols called by functions in a .py file (regex mirror of
    codeloom's Python call-graph pass; safe for the simple generated files)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return set()
    funcs = {}
    current = None
    for line in lines:
        dm = re.match(r"^(?:class|def)\s+(\w+)", line)
        if dm:
            current = dm.group(1)
            funcs.setdefault(current, set())
            continue
        if current is not None and line.startswith((" ", "\t")):
            for cm in re.finditer(r"\b(\w+)\s*\(", line):
                c = cm.group(1)
                if c in all_defined and c != current:
                    funcs[current].add(c)
    out = set()
    for cs in funcs.values():
        out |= cs
    return out


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
def make_primary_entries(syms: list, rng: random.Random, n: int = 20) -> list:
    """n scripted typed entries, types mixed, each pinned (affected_symbols)
    to a unique real symbol. Returns [(symbol, type, title, body)]."""
    pool = [s for s in syms if not s[1].startswith("_")]
    assert len(pool) >= n, f"need >= {n} symbols in repo, found {len(pool)}"
    chosen = rng.sample(pool, n)
    entries = []
    for i, (mod, sym) in enumerate(chosen):
        typ = TYPE_ORDER[i % len(TYPE_ORDER)]
        dep = chosen[(i + 1) % n][1]
        exc = EXC[i % len(EXC)]
        note = NOTE_TEMPLATES[typ].format(sym=sym, file=mod + ".py", dep=dep, exc=exc)
        # title = first sentence-ish chunk (keeps titles short + unique);
        # body = the full note so the text also contains the symbol.
        title = note.split(";")[0].strip()
        entries.append((sym, typ, title, note))
    return entries


def seed_entry(repo: str, typ: str, title: str, body: str, symbols: list) -> tuple:
    """One typed write via the real CLI: --memory-add --type/--title/--body/--symbols.
    Returns (ok, ms)."""
    cmd = [PY, CODELOOM, "--memory-add", "--type", typ, "--title", title,
           "--body", body, "--symbols", ",".join(symbols), repo]
    r, ms = run(cmd, cwd=os.path.dirname(CODELOOM))
    return (r.returncode == 0, ms)


# --------------------------------------------------------------------------- #
# recall
# --------------------------------------------------------------------------- #
def parse_graph_neighbors(out: str):
    """Authoritative neighbor module set printed by `--memory <query>`.
    Returns None when the graph section is absent (symbol not in codebase);
    an empty set when codeloom printed 'no graph neighbors'. Either way the
    caller decides how to grade it."""
    m = re.search(r"graph neighbors of .*?\((?:import|import/call) graph\): ([^\n]*)", out)
    if not m:
        if "no graph neighbors of" in out:
            return set()
        return None
    txt = m.group(1).strip()
    if not txt:
        return None
    return set(x.strip() for x in txt.split(",") if x.strip())


def baseline_tokens(repo: str, query: str) -> int:
    """grep -rn <symbol> repo + read top 3 matching files, whole-file bytes.

    Excludes the memory dir: a wiped agent greps the CODEBASE, not its own
    (erased) .codeloom-memory — seeding must not inflate the baseline.
    Deterministic: the scratch repo's absolute path (random per run) is
    replaced with a fixed token in the COUNTED text — grep prints matching
    paths, so raw output would make the baseline drift run-to-run."""
    mem_dir = os.path.join(repo, ".codeloom-memory")

    def keep(p: str) -> bool:
        return not (p == mem_dir or p.startswith(mem_dir + os.sep))

    r = subprocess.run(
        ["grep", "-rn", "--exclude-dir=.codeloom-memory", query, repo],
        capture_output=True, text=True, timeout=120
    )
    tok = count_tokens(r.stdout.replace(repo, "<REPO>"))
    paths = []
    for line in r.stdout.splitlines():
        if ":" in line:
            p = line.split(":", 1)[0]
            if os.path.isfile(p) and keep(p) and p not in paths:
                paths.append(p)
    for p in paths[:3]:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                tok += count_tokens(fh.read())
        except OSError:
            pass
    return tok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=None,
                    help="repo to benchmark (default: build a synthetic 5-file repo)")
    ap.add_argument("--seed", type=int, default=7, help="rng seed (deterministic)")
    ap.add_argument("--entries", type=int, default=20, help="primary memory entries to seed")
    ap.add_argument("--queries", type=int, default=10, help="recall queries")
    ap.add_argument("--runs", type=int, default=3, help="latency runs per query")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    # Fresh temp workspace: never touch the source repo's .codeloom-memory.
    work = tempfile.mkdtemp(prefix="codeloom-memeval-")
    try:
        synthetic = args.repo is None or not os.path.isdir(args.repo)
        if args.repo is None:
            os.makedirs(os.path.join(work, "repo"), exist_ok=True)
            repo = os.path.join(work, "repo")
            syms = build_synthetic_repo(repo)
            print(f"# Memory OS benchmark (typed surface) — synthetic repo at {repo} "
                  f"({len(syms)} real symbols)")
        elif not os.path.isdir(args.repo):
            os.makedirs(os.path.join(work, "repo"), exist_ok=True)
            repo = os.path.join(work, "repo")
            syms = build_synthetic_repo(repo)
            print(f"# Memory OS benchmark — {args.repo} not found; synthetic repo built "
                  f"at {repo} ({len(syms)} real symbols)")
        else:
            repo = os.path.join(work, "repo")
            shutil.copytree(args.repo, repo, ignore=shutil.ignore_patterns(
                ".git", ".codeloom", ".codeloom-*", "__pycache__"))
            syms = extract_symbols(repo)
            print(f"# Memory OS benchmark — copy of {args.repo} ({len(syms)} symbols, "
                  f"memory isolated in {repo})")

        # --- local import map (the script's prediction of the graph) ---
        imports = {}
        call_callees = {}
        if synthetic:
            # codeloom's call graph counts only FUNCTION calls (ast.Name /
            # ast.Attribute where the name is a FunctionDef anywhere in the
            # repo) — mirror that exactly for the prediction.
            func_names = set()
            for fname in SYNTHETIC_FILES:
                try:
                    tree = ast.parse(SYNTHETIC_FILES[fname])
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        func_names.add(node.name)
            for fname in SYNTHETIC_FILES:
                mod = fname[:-3]
                imports[mod] = {p for p in file_imports(os.path.join(repo, fname))
                                if p in {m for m, _ in syms}}
                call_callees[mod] = module_call_callees(
                    os.path.join(repo, fname), func_names)
        else:
            # real repo: full import map, filtered to local modules
            local_modules = {m for m, _ in syms}
            imports = {}
            for dirpath, _dirs, files in os.walk(repo):
                for fn in sorted(files):
                    if not fn.endswith(".py"):
                        continue
                    p = os.path.join(dirpath, fn)
                    mod = module_name_of(p, repo)
                    deps = set()
                    for imp in file_imports(p):
                        if imp in local_modules:
                            deps.add(imp)
                        else:
                            # longest local prefix (codeloom's resolution)
                            cand = [m for m in local_modules
                                    if m == imp or m.startswith(imp + ".")]
                            if cand:
                                deps.add(sorted(cand, key=len)[-1])
                    imports[mod] = deps
            call_callees = {}

        def predict_neighbors(mod: str) -> set:
            """Import-side neighbors: modules `mod` imports + modules importing it."""
            nb = set(imports.get(mod, ()))
            for m, deps in imports.items():
                if m != mod and mod in deps:
                    nb.add(m)
            return nb

        def predict_call_neighbors(sym: str, mod: str) -> set:
            """Modules (other than mod) whose functions call `sym`."""
            out = set()
            for m, cs in call_callees.items():
                if m != mod and sym in cs:
                    out.add(m)
            return out

        # ---- seed primary entries -----------------------------------------
        entries = make_primary_entries(syms, rng, args.entries)
        seed_rows = [seed_entry(repo, typ, title, body, [sym])
                     for sym, typ, title, body in entries]
        add_ms = sum(ms for _, ms in seed_rows) / len(seed_rows) if seed_rows else 0.0
        wrote = sum(1 for ok, _ in seed_rows if ok)
        print(f"  seeded {wrote}/{len(seed_rows)} typed entries via --memory-add "
              f"(avg {add_ms:.0f} ms per add, types: "
              + ", ".join(f"{k}={sum(1 for e in entries if e[1] == k)}"
                          for k in TYPE_ORDER) + ")")

        # ---- graph-link entries (one per query) ---------------------------
        queries = [e[0] for e in entries[: args.queries]]
        sym2mod = {}
        for m, s in syms:
            sym2mod.setdefault(s, m)
        # On real repos the script's import model is NOT the graph codeloom
        # builds (dotted-module resolution, __init__ packages, relative
        # imports), so the authoritative neighbor list is probed with a
        # `--memory <q>` call before seeding (its printed 'graph neighbors'
        # line; seeding cannot change the graph). Synthetic: exact
        # independent prediction (the script's model IS the repo, verified
        # per query by the equality check below).
        probe_neighbors = {}
        if not synthetic:
            print("  probing real-repo graph neighbors for seed placement ...")
            for q in queries:
                r, _ms = run([PY, CODELOOM, "--memory", q, repo],
                             cwd=os.path.dirname(CODELOOM))
                parsed = parse_graph_neighbors(r.stdout)
                probe_neighbors[q] = parsed if parsed is not None else set()
        graph_seeds = {}  # query sym -> (neighbor_sym, title, ok) or None
        graph_attempts = 0
        used_nsym = set()
        for i, q in enumerate(queries):
            mod = sym2mod.get(q)
            if mod is None:
                graph_seeds[q] = None
                continue
            nb = predict_neighbors(mod) | predict_call_neighbors(q, mod)
            if not synthetic:
                nb = probe_neighbors.get(q, set())
            # a neighbor module that has a symbol we can pin (not q itself).
            # Prefer a DISTINCT symbol per query so one entry can't satisfy
            # two queries' graph-hit counts. Scan ALL neighbor modules for a
            # free symbol first; only reuse an already-used symbol when the
            # whole neighbor pool is exhausted (tiny repos).
            target = None
            for cand in sorted(nb):
                cand_syms = [s for m, s in syms if m == cand and s != q]
                for nsym in sorted(cand_syms):
                    if nsym not in used_nsym:
                        target = (cand, nsym)
                        break
                if target is not None:
                    break
            if target is None:
                for cand in sorted(nb):
                    cand_syms = sorted(s for m, s in syms
                                       if m == cand and s != q)
                    if cand_syms:
                        target = (cand, cand_syms[i % len(cand_syms)])
                        break
            if target is None:
                graph_seeds[q] = None
                print(f"  graph seed skipped for {q}: no import/call neighbor module "
                      f"with symbols (module {mod})")
                continue
            graph_attempts += 1
            nmod, nsym = target
            used_nsym.add(nsym)
            title = f"graph-neighbor note for {nsym} (queried via {q})"
            body = (f"{nsym} in {nmod} must stay compatible with {mod} callers; "
                    f"do not change its signature")
            ok, ms = seed_entry(repo, "constraint", title, body, [nsym])
            graph_seeds[q] = (nsym, title, ok)
            if not ok:
                print(f"  WARN graph seed failed for {q} (rc={ms:.0f}ms)")
        if graph_attempts < args.queries:
            print(f"  note: graph-link seeds placed for {graph_attempts}/{args.queries} "
                  f"queries ({args.queries - graph_attempts} queries have no "
                  f"reachable neighbor symbol)")

        # ---- recall: --memory <symbol> (primary) ---------------------------
        rows = []  # (sym, direct_hit, graph_hit, gmatch, loom_tok, loom_ms,
        #           qm_hit, qm_tok, qm_ms, base_tok)
        losses = []
        for q in queries:
            direct = 0
            graph = 0
            gmatch = False
            ms_sum = 0.0
            loom_tok = 0
            for _ in range(args.runs):
                r, ms = run([PY, CODELOOM, "--memory", q, repo],
                            cwd=os.path.dirname(CODELOOM))
                ms_sum += ms
                loom_tok = count_tokens(r.stdout)
                exp = next(e for e in entries if e[0] == q)
                if exp[2] in r.stdout:
                    direct += 1
                gse = graph_seeds.get(q)
                if gse is not None and gse[1] in r.stdout:
                    graph += 1
                # structural check: the printed neighbor list vs the script's
                # own model. Synthetic: exact equality against the
                # independent prediction (the script's model IS the repo).
                # Real repos: equality against the probe taken before
                # seeding (codeloom's own graph; seeding cannot change it).
                parsed = parse_graph_neighbors(r.stdout)
                if parsed is not None:
                    if synthetic:
                        pred = predict_neighbors(sym2mod.get(q, ""))
                        pred |= predict_call_neighbors(q, sym2mod.get(q, ""))
                    else:
                        pred = probe_neighbors.get(q, None)
                    if pred is not None and parsed == pred:
                        gmatch = True
            direct_hit = direct == args.runs
            graph_hit = (graph == args.runs) if graph_seeds.get(q) is not None else None
            qm_hit, qm_tok, qm_ms = recall_query_memory(repo, q, entries, args.runs)
            base = baseline_tokens(repo, q)
            rows.append((q, direct_hit, graph_hit, gmatch, loom_tok,
                         ms_sum / args.runs, qm_hit, qm_tok, qm_ms, base))
            if not direct_hit or graph_hit is False or not gmatch or not qm_hit:
                losses.append((q, direct_hit, graph_hit, gmatch, qm_hit,
                               next(e for e in entries if e[0] == q)[2]))

        # ---- report -----------------------------------------------------------
        print()
        print(f"{'query symbol':<18}{'dir':<5}{'gr':<5}{'gmatch':<8}"
              f"{'--memory tok':<14}{'mem ms':<9}{'qm hit':<7}{'qm tok':<11}"
              f"{'qm ms':<8}{'base tok'}")
        tot_lt, tot_qm, tot_b = 0, 0, 0
        tot_mm, tot_qmm = 0.0, 0.0
        for (q, dh, gh, gm, lt, mm, qh, qt, qmm, bt) in rows:
            tot_lt += lt; tot_qm += qt; tot_b += bt
            tot_mm += mm; tot_qmm += qmm
            gcell = "YES" if gh is True else ("NO " if gh is False else "n/a")
            print(f"{q:<18}{'YES' if dh else 'NO ':<5}{gcell:<5}"
                  f"{'OK' if gm else 'BAD':<6}{lt:<14}{mm:<9.1f}"
                  f"{'YES' if qh else 'NO ':<7}{qt:<11}{qmm:<8.1f}{bt}")
        n = len(rows)
        dir_hits = sum(1 for r in rows if r[1])
        graph_hits = sum(1 for r in rows if r[2] is True)
        graph_seeded = sum(1 for r in rows if r[2] is not None)
        struct_ok = sum(1 for r in rows if r[3])
        qm_hits = sum(1 for r in rows if r[6])

        def cmp_tok(loom: int, base: int) -> str:
            if base <= 0:
                return f"{loom} tok (no baseline)"
            if loom <= base:
                return f"{loom} tok vs baseline {base} tok ({(1 - loom / base) * 100:.1f}% fewer)"
            return (f"{loom} tok vs baseline {base} tok "
                    f"({(loom / base - 1) * 100:.1f}% MORE — broader graph-linked "
                    f"context)")

        print()
        print(f"--memory retrieval:    {dir_hits}/{n} direct hits, "
              f"{graph_hits}/{graph_seeded} graph-neighbor hits, "
              f"{struct_ok}/{n} neighbor-set OK, "
              f"{tot_mm / n:.0f} ms avg, {cmp_tok(tot_lt, tot_b)}")
        print(f"--query-memory row:    {qm_hits}/{n} hits, {tot_qmm / n:.0f} ms avg, "
              f"{cmp_tok(tot_qm, tot_b)}")
        print(f"memory write: avg {add_ms:.0f} ms per --memory-add")
        if losses:
            print("\nLOSS ROWS (expected entry NOT returned):")
            for q, dh, gr, gm, qh, note in losses:
                print(f"  - {q}: direct={dh} graph={gr} structure={gm} "
                      f"query-memory={qh} | {note}")
        else:
            print("\nLOSS ROWS: none")
        print("\nNOTE: entries are synthetic (scripted, not real agent history) — "
              "this measures the storage + retrieval paths, not note quality. "
              "On real repos every call pays cold Python startup + a full tree "
              "walk (--memory also builds import+call graphs); the synthetic "
              "default measures the pure memory path.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def recall_query_memory(repo, q, entries, runs):
    """--query-memory <q> hit + latency + tokens (the pre-Memory-OS row)."""
    exp = next(e for e in entries if e[0] == q)
    hits = 0
    ms_sum = 0.0
    tok = 0
    for _ in range(runs):
        r, ms = run([PY, CODELOOM, "--query-memory", q, repo],
                    cwd=os.path.dirname(CODELOOM))
        ms_sum += ms
        tok = count_tokens(r.stdout)
        if exp[2] in r.stdout:
            hits += 1
    return (hits == runs, tok, ms_sum / runs)


if __name__ == "__main__":
    sys.exit(main())
