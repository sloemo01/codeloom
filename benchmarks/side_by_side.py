#!/usr/bin/env python3
"""Side-by-side token-efficiency benchmark: codeloom vs jcodemunch-mcp.

Runs BOTH tools on the SAME repo and SAME symbols, measuring the tokens each
returns to retrieve a symbol. This is the apples-to-apples yardstick.

- codeloom:  --get-symbol <sym>  (summary-first by default)
- jcodemunch: get_symbol_source  (full source)

Usage:
    python3 benchmarks/side_by_side.py --repo /path/to/repo --symbols Body,Cookie,Depends
"""
import argparse
import json
import os
import re
import subprocess
import sys

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "codeloom.py")
JC_SERVER = os.environ.get("JCODEMUNCH_MCP", "/tmp/jc-venv/bin/jcodemunch-mcp")


def codeloom_tokens(repo, symbol):
    """Tokens codeloom returns for a symbol (summary-first)."""
    r = subprocess.run([sys.executable, CodeLoom, "--get-symbol", symbol, "."],
                       cwd=repo, capture_output=True, text=True)
    if "not found" in r.stdout.lower():
        return None, "not found"
    m = re.search(r"~(\d+) tokens", r.stdout)
    return (int(m.group(1)) if m else len(r.stdout) // 4), "summary"


class JCDriver:
    """Minimal MCP stdio client for jcodemunch-mcp."""

    def __init__(self, server):
        self.proc = subprocess.Popen(
            [server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
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
        line = self.proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"raw": line}

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
        self.proc.wait()


def jc_tokens(driver, repo, symbol):
    """Tokens jcodemunch returns for a symbol via get_symbol_source."""
    # search for the symbol first to get its symbol_id
    search = driver.tool("search_symbols", {"query": symbol, "limit": 1, "repo": repo})
    if not search or "not found" in search.lower() or '"error"' in search:
        return None, "not found"
    # extract the symbol id from the search result
    sym_id = None
    try:
        data = json.loads(search)
        results = data.get("results", [])
        if results:
            sym_id = results[0].get("id")
    except (json.JSONDecodeError, AttributeError):
        # fallback: try to find an id= in the text
        import re
        m = re.search(r'"id":"([^"]+)"', search)
        if m:
            sym_id = m.group(1)
    if not sym_id:
        return None, "no symbol id"
    # get full source
    src = driver.tool("get_symbol_source", {"symbol_id": sym_id, "repo": repo})
    if not src or "not found" in src.lower() or '"error"' in src:
        return None, "not found"
    return len(src) // 4, "full source"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--symbols", required=True, help="comma-separated symbol names")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    symbols = [s.strip() for s in args.symbols.split(",")]

    # index the repo with jcodemunch first (required before retrieval)
    print("indexing repo with jcodemunch...")
    driver = JCDriver(JC_SERVER)
    idx = driver.tool("index_folder", {"path": repo})
    print("  index:", idx[:120])
    # derive the repo id from the index response (e.g. "fastapi/fastapi")
    repo_id = None
    try:
        repo_id = json.loads(idx).get("repo")
    except (json.JSONDecodeError, AttributeError):
        pass
    if not repo_id:
        repo_id = os.path.basename(repo)  # fallback

    print(f"\n=== Side-by-side token efficiency: codeloom vs jcodemunch ===")
    print(f"repo: {repo} (id: {repo_id})")
    print(f"{'symbol':<14} {'codeloom':<12} {'jcodemunch':<12} {'codeloom wins'}")
    print("-" * 55)
    for sym in symbols:
        cl_tok, cl_mode = codeloom_tokens(repo, sym)
        jc_tok, jc_mode = jc_tokens(driver, repo_id, sym)
        if cl_tok is None or jc_tok is None:
            print(f"{sym:<14} {str(cl_tok):<12} {str(jc_tok):<12} (incomplete)")
            continue
        win = "YES" if cl_tok < jc_tok else "no"
        print(f"{sym:<14} {cl_tok:<12} {jc_tok:<12} {win}  (codeloom={cl_mode}, jc={jc_mode})")
    driver.close()


if __name__ == "__main__":
    main()
