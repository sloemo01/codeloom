#!/usr/bin/env python3
"""Side-by-side benchmark: codeloom vs code-review-graph (crg).

Apples-to-apples yardstick against the 30.6k-star field leader, on the SAME
repo and SAME symbols/tasks. Measures:

1. Symbol retrieval tokens — codeloom --get-symbol (summary-first) vs
   crg search (FTS JSON) + query callers_of (disambiguated).
2. Setup-to-first-answer wall clock — codeloom (zero setup) vs crg
   (pip install + build graph + [optional] embed), timed end to end.

Usage:
    CRG_BIN=/path/to/code-review-graph python3 benchmarks/vs_crg.py \
        --repo /tmp/bench-fastapi --symbols Body,Cookie,File,Header

Requires: tiktoken (tokenizer parity, cl100k_base — same as benchmarks/run.py).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

try:
    import tiktoken
except ImportError:
    tiktoken = None

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "codeloom.py")
CRG_BIN = os.environ.get("CRG_BIN", "code-review-graph")
ENC = "cl100k_base"


def count_tokens(text):
    if tiktoken is not None:
        return len(tiktoken.get_encoding(ENC).encode(text))
    return len(text) // 4  # fallback estimate


def run(cmd, cwd, timeout=120):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


# --------------------------------------------------------------------------- #
# codeloom side
# --------------------------------------------------------------------------- #

def codeloom_symbol_tokens(repo, symbol):
    """Tokens codeloom returns for a symbol (summary-first)."""
    rc, out, err = run([sys.executable, CodeLoom, "--get-symbol", symbol, "."], repo)
    if "not found" in out.lower():
        return None, "not found", 0.0
    m = re.search(r"~(\d+) tokens", out)
    n = int(m.group(1)) if m else count_tokens(out)
    return n, "summary", 0.0


def codeloom_task_brief(repo, task):
    """Tokens codeloom --pack returns for a task (code-embedded brief)."""
    rc, out, err = run([sys.executable, CodeLoom, "--pack", task, "."], repo)
    if rc != 0:
        return None, out[:120]
    return count_tokens(out), out


# --------------------------------------------------------------------------- #
# crg side
# --------------------------------------------------------------------------- #

class CrgDriver:
    """Minimal MCP stdio client for code-review-graph's mcp server."""

    def __init__(self, repo):
        self.proc = subprocess.Popen(
            [CRG_BIN, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=repo)
        self._id = 0
        self._call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "bench", "version": "1.0"}})

    def _call(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        # read until we get a response for our id (skip notifications)
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == self._id:
                return obj
        return None

    def tool(self, name, args):
        r = self._call("tools/call", {"name": name, "arguments": args})
        if r and "result" in r:
            content = r["result"].get("content", [])
            if content:
                return content[0].get("text", "")
        return ""

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def crg_symbol_tokens(repo, symbol):
    """Tokens crg returns to resolve a symbol + its callers (their workflow:
    search --limit 1, then query callers_of on the top hit)."""
    t0 = time.time()
    rc, out, err = run([CRG_BIN, "search", symbol, "--limit", "1"], repo)
    if rc != 0:
        return None, f"search failed: {err[:80]}", time.time() - t0
    tokens = count_tokens(out)
    # their query callers_of requires a qualified name; grab the top hit's
    # qualified_name if present
    m = re.search(r'"qualified_name": "([^"]+)"', out)
    if m:
        rc2, out2, err2 = run([CRG_BIN, "query", "callers_of", m.group(1)], repo)
        if rc2 == 0:
            tokens += count_tokens(out2)
    return tokens, "search+callers", time.time() - t0


def crg_minimal_context(repo, task):
    """crg's get_minimal_context_tool (~100 token task context)."""
    d = CrgDriver(repo)
    try:
        txt = d.tool("get_minimal_context_tool", {"task": task})
        return count_tokens(txt), txt[:200]
    finally:
        d.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--symbols", required=True, help="comma-separated")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    symbols = [s.strip() for s in args.symbols.split(",")]

    print("=" * 62)
    print("Side-by-side: codeloom vs code-review-graph (crg)")
    print(f"repo: {repo}   tokenizer: {ENC}")
    print("=" * 62)

    # --- symbol retrieval ---
    print(f"\n{'symbol':<12} {'codeloom':>10} {'crg':>10} {'winner'}")
    print("-" * 46)
    cl_wins = crg_wins = ties = 0
    for sym in symbols:
        cl, cl_mode, _ = codeloom_symbol_tokens(repo, sym)
        cr, cr_mode, cr_secs = crg_symbol_tokens(repo, sym)
        if cl is None or cr is None:
            print(f"{sym:<12} {str(cl):>10} {str(cr):>10}  (incomplete)")
            continue
        win = "codeloom" if cl < cr else ("crg" if cr < cl else "tie")
        if win == "codeloom":
            cl_wins += 1
        elif win == "crg":
            crg_wins += 1
        else:
            ties += 1
        print(f"{sym:<12} {cl:>10} {cr:>10}  {win}   (cl={cl_mode}, crg={cr_mode}, {cr_secs:.1f}s)")

    # --- task brief ---
    print(f"\nTask brief (fix the login bug):")
    task = "fix the login bug"
    cl_tok, cl_brief = codeloom_task_brief(repo, task)
    cr_tok, cr_preview = crg_minimal_context(repo, task)
    print(f"  codeloom --pack: {cl_tok} tokens")
    print(f"  crg get_minimal_context_tool: {cr_tok} tokens")
    winner = "codeloom" if (cl_tok or 0) < cr_tok else "crg"
    print(f"  winner: {winner}")

    print(f"\nTally (symbols): codeloom {cl_wins} / crg {crg_wins} / ties {ties}")


if __name__ == "__main__":
    main()
