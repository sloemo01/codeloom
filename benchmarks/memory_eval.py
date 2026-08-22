#!/usr/bin/env python3
"""Memory OS benchmark: codeloom persistent memory (--remember / --query-memory).

Measures the "never forgets" layer — the memory-retrieval differentiator:

1. BUILD   a small synthetic repo (5 files, real symbols) if --repo is absent,
           or copy the given repo to a scratch dir (never touches the real
           repo's .codeloom-memory).
2. SEED    N=20 typed memory entries via subprocess `--remember` (DECISIONS /
           ARCHITECTURE / PATTERNS / CONVENTIONS) and `--lesson` (bugs) with
           affected symbols drawn from the repo's real definitions.
3. RECALL  for 10 query symbols, `--query-memory <symbol>` must return the
           entry whose affected_symbols contains it. Count hits.
4. LATENCY avg wall time of a `--query-memory` call (10 queries x 3 runs).
5. TOKENS  `--query-memory` output size (bytes/4, tiktoken cl100k_base when
           installed) vs the naive baseline: `grep -rn <symbol> repo` + read
           the top 3 matching files, same 10 queries.

Output: per-query table + the summary line
    memory retrieval: N/10 linked hits, X ms avg, Y tok vs baseline Z tok (Q% fewer)

Honest framing: entries are SCRIPTED (generated deterministically, not real
agent history) — they exercise the storage + retrieval paths, not the quality
of what an agent would choose to remember. Loss rows are printed, never
filtered.

Usage:
    python3 benchmarks/memory_eval.py [--repo /tmp/bench-fastapi] [--seed N] [--runs 3]
"""
import argparse
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

# Types -> write path (--lesson writes LESSONS.md; --remember writes a section)
TYPES = {
    "bug":         {"kind": "lesson",   "section": None},
    "decision":    {"kind": "remember", "section": "DECISIONS"},
    "architecture": {"kind": "remember", "section": "ARCHITECTURE"},
    "api":         {"kind": "remember", "section": "PATTERNS"},
    "constraint":  {"kind": "remember", "section": "CONVENTIONS"},
}
TYPE_ORDER = ["bug", "decision", "architecture", "api", "constraint"]

SYNTHETIC_FILES = {
    "auth.py": (
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
        "class Router:\n"
        "    def __init__(self):\n"
        "        self.routes = {}\n"
        "    def add_route(self, path, handler):\n"
        "        self.routes[path] = handler\n"
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
        "    return Engine()\n"
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

# Natural-language note per (type, symbol) — the symbol name appears verbatim so
# `--query-memory <symbol>` full-text match resolves it, exactly as an agent's
# real note would.
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
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=300)
    return r, (time.perf_counter() - t0) * 1000.0


def build_synthetic_repo(root: str) -> list:
    """Write the 5-file synthetic repo. Returns [file, symbol, ...] tuples."""
    syms = []
    for fname, body in SYNTHETIC_FILES.items():
        with open(os.path.join(root, fname), "w", encoding="utf-8") as fh:
            fh.write(body)
        for m in re.finditer(r"^(?:class|def)\s+(\w+)", body, re.M):
            syms.append((fname, m.group(1)))
    return syms


def extract_symbols(repo: str) -> list:
    """Real symbols from the target repo (any .py files)."""
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
            rel = os.path.relpath(p, repo)
            for m in re.finditer(r"^(?:class|def)\s+(\w+)", text, re.M):
                syms.append((rel, m.group(1)))
    return syms


def seed_memory(repo: str, entries: list) -> list:
    """Write entries via the real CLI. Returns [(type, symbol, note, ok, ms)]."""
    rows = []
    for typ, symbol, note in entries:
        t = TYPES[typ]
        if t["kind"] == "lesson":
            cmd = [PY, CODELOOM, "--lesson", note, repo]
        else:
            cmd = [PY, CODELOOM, "--remember", note, "--section", t["section"], repo]
        r, ms = run(cmd, cwd=os.path.dirname(CODELOOM))
        rows.append((typ, symbol, note, r.returncode == 0, ms))
    return rows


def make_entries(syms: list, rng: random.Random, n: int = 20) -> list:
    """n scripted entries, types mixed, each anchored on a unique primary symbol."""
    pool = [s for s in syms if not s[1].startswith("_")]
    assert len(pool) >= n, f"need >= {n} symbols in repo, found {len(pool)}"
    chosen = rng.sample(pool, n)
    entries = []
    for i, (fname, sym) in enumerate(chosen):
        typ = TYPE_ORDER[i % len(TYPE_ORDER)]
        dep = chosen[(i + 1) % n][1]
        exc = EXC[i % len(EXC)]
        note = NOTE_TEMPLATES[typ].format(sym=sym, file=fname, dep=dep, exc=exc)
        entries.append((typ, sym, note))
    return entries


def baseline_tokens(repo: str, query: str) -> int:
    """grep -rn <symbol> repo + read top 3 matching files, whole-file bytes."""
    r = subprocess.run(
        ["grep", "-rn", query, repo], capture_output=True, text=True, timeout=120
    )
    tok = count_tokens(r.stdout)
    paths = []
    for line in r.stdout.splitlines():
        if ":" in line:
            p = line.split(":", 1)[0]
            if os.path.isfile(p) and p not in paths:
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
    ap.add_argument("--entries", type=int, default=20, help="memory entries to seed")
    ap.add_argument("--queries", type=int, default=10, help="recall queries")
    ap.add_argument("--runs", type=int, default=3, help="latency runs per query")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    # Fresh temp workspace: never touch the source repo's .codeloom-memory.
    work = tempfile.mkdtemp(prefix="codeloom-memeval-")
    try:
        if args.repo is None:
            os.makedirs(os.path.join(work, "repo"), exist_ok=True)
            syms = build_synthetic_repo(os.path.join(work, "repo"))
            repo = os.path.join(work, "repo")
            print(f"# Memory OS benchmark — synthetic repo built at {repo} "
                  f"({len(syms)} real symbols)")
        elif not os.path.isdir(args.repo):
            os.makedirs(os.path.join(work, "repo"), exist_ok=True)
            syms = build_synthetic_repo(os.path.join(work, "repo"))
            repo = os.path.join(work, "repo")
            print(f"# Memory OS benchmark — {args.repo} not found; synthetic repo "
                  f"built at {repo} ({len(syms)} real symbols)")
        else:
            repo = os.path.join(work, "repo")
            shutil.copytree(args.repo, repo, ignore=shutil.ignore_patterns(
                ".git", ".codeloom", ".codeloom-*", "__pycache__"))
            syms = extract_symbols(repo)
            print(f"# Memory OS benchmark — copy of {args.repo} ({len(syms)} symbols, "
                  f"memory isolated in {repo})")

        entries = make_entries(syms, rng, args.entries)
        seed_rows = seed_memory(repo, entries)
        add_ms = sum(r[4] for r in seed_rows) / len(seed_rows) if seed_rows else 0.0
        wrote = sum(1 for r in seed_rows if r[3])
        print(f"  seeded {wrote}/{len(seed_rows)} entries via --remember/--lesson "
              f"(avg {add_ms:.0f} ms per add, types: "
              + ", ".join(f"{k}={sum(1 for e in entries if e[0] == k)}"
                          for k in TYPE_ORDER) + ")")

        # ---- recall + latency + tokens -------------------------------------
        queries = [e[1] for e in entries[: args.queries]]
        rows = []  # (symbol, type, hit, loom_tok, loom_ms, base_tok)
        losses = []
        for sym in queries:
            hits = 0
            ms_sum = 0.0
            loom_tok = 0
            out_text = ""
            for _ in range(args.runs):
                r, ms = run([PY, CODELOOM, "--query-memory", sym, repo],
                            cwd=os.path.dirname(CODELOOM))
                ms_sum += ms
                out_text = r.stdout
                loom_tok = count_tokens(r.stdout)
                expected = next(e for e in entries if e[1] == sym)
                if expected[2] in r.stdout:
                    hits += 1
            hit = hits == args.runs
            if not hit:
                losses.append((sym, expected[2]))
            base = baseline_tokens(repo, sym)
            rows.append((sym, hit, loom_tok, ms_sum / args.runs, base))

        # ---- report -----------------------------------------------------------
        print()
        print(f"{'query symbol':<18}{'hit':<6}{'codeloom tok':<14}"
              f"{'mem ms':<9}{'baseline tok'}")
        tot_l, tot_b, tot_m = 0, 0, 0.0
        for sym, hit, lt, ms, bt in rows:
            tot_l += lt; tot_b += bt; tot_m += ms
            print(f"{sym:<18}{'YES' if hit else 'NO ':<6}{lt:<14}{ms:<9.1f}{bt}")
        tot_hits = sum(1 for r in rows if r[1])
        avg_ms = tot_m / len(rows)
        pct = 100.0 * (tot_b - tot_l) / tot_b if tot_b else 0.0
        print()
        print(f"memory retrieval: {tot_hits}/{len(rows)} linked hits, {avg_ms:.0f} ms avg, "
              f"{tot_l} tok vs baseline {tot_b} tok ({pct:.1f}% fewer)")
        print(f"memory write: avg {add_ms:.0f} ms per --remember/--lesson add")
        if losses:
            print("\nLOSS ROWS (expected entry NOT returned):")
            for sym, note in losses:
                print(f"  - {sym}: {note}")
        else:
            print("\nLOSS ROWS: none")
        print("\nNOTE: entries are synthetic (scripted, not real agent history) — "
              "this measures the storage + retrieval paths, not note quality.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
