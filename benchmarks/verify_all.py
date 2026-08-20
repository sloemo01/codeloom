#!/usr/bin/env python3
"""Comprehensive feature verification for codeloom.

Tests every CLI feature and reports PASS/FAIL for each. Run from the repo root:
    python3 benchmarks/verify_all.py
"""
import json
import os
import subprocess
import sys

LOOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo-repo")

PASS = 0
FAIL = 0
FAILURES = []


def run(args, cwd=REPO, timeout=120):
    r = subprocess.run([sys.executable, LOOM] + args, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


print("=" * 60)
print("codeloom comprehensive feature verification")
print("=" * 60)

# --- 1. Core map commands ---
print("\n[1] Core map commands")
rc, out, err = run([])
check("map (default)", rc == 0 and "codeloom" in out.lower() or "entry points" in out.lower(), f"rc={rc}")
rc, out, err = run(["--json"])
check("--json", rc == 0 and out.strip().startswith("{"), "not JSON")
rc, out, err = run(["--no-outline"])
check("--no-outline", rc == 0)
rc, out, err = run(["--max-files", "10"])
check("--max-files", rc == 0)
rc, out, err = run(["--write", "/tmp/codeloom-map-test.md"])
check("--write", rc == 0 and os.path.isfile("/tmp/codeloom-map-test.md"), "file not written")
rc, out, err = run(["--version"])
check("--version", rc == 0 and "codeloom" in out)

# --- 2. Structural ---
print("\n[2] Structural")
rc, out, err = run(["--graph"])
check("--graph", rc == 0 and "depends_on" in out or "graph" in out.lower(), f"rc={rc}")
rc, out, err = run(["--graph", "--focus", "src.core.engine"])
check("--graph --focus", rc == 0)
rc, out, err = run(["--calls"])
check("--calls", rc == 0)
rc, out, err = run(["--calls", "--focus", "src.core.engine"])
check("--calls --focus", rc == 0)
rc, out, err = run(["--cross"])
check("--cross", rc == 0)
rc, out, err = run(["--framework"])
check("--framework", rc == 0 and "framework:" in out, f"rc={rc}")

# --- 3. Search / retrieval ---
print("\n[3] Search / retrieval")
rc, out, err = run(["--search", "Engine"])
check("--search", rc == 0 and "Engine" in out, f"rc={rc}")
rc, out, err = run(["--usages", "retry"])
check("--usages", rc == 0)
rc, out, err = run(["--grep", "retry"])
check("--grep", rc == 0 and "retry" in out, f"rc={rc}")
rc, out, err = run(["--read", "Engine"])
check("--read", rc == 0 and "Engine" in out, f"rc={rc}")
rc, out, err = run(["--get-symbol", "Engine"])
check("--get-symbol", rc == 0 and "Engine" in out, f"rc={rc}")
rc, out, err = run(["--get-symbol", "Engine", "--full"])
check("--get-symbol --full", rc == 0 and "class Engine" in out, f"rc={rc}")
rc, out, err = run(["--snippet", "src/core/engine.py", "0", "60"])
check("--snippet", rc == 0)

# --- 4. Task-orientation ---
print("\n[4] Task-orientation")
rc, out, err = run(["--task", "add retry logic"])
check("--task", rc == 0 and "retry" in out, f"rc={rc}")
rc, out, err = run(["--impact", "src.core.engine"])
check("--impact", rc == 0 and "risk" in out, f"rc={rc}")
rc, out, err = run(["--plan", "add retry logic"])
check("--plan", rc == 0 and "retry" in out, f"rc={rc}")
rc, out, err = run(["--pack", "add retry logic"])
check("--pack", rc == 0 and "TASK" in out and "THE RELEVANT CODE" in out, f"rc={rc}")

# --- 5. Understanding ---
print("\n[5] Understanding")
rc, out, err = run(["--explain", "Engine"])
check("--explain", rc == 0 and "Engine" in out, f"rc={rc}")
rc, out, err = run(["--similar", "run"])
check("--similar", rc == 0)
rc, out, err = run(["--deadcode"])
check("--deadcode", rc == 0 and "dead code" in out, f"rc={rc}")

# --- 6. Change-aware / runtime ---
print("\n[6] Change-aware / runtime")
rc, out, err = run(["--diff"])
check("--diff", rc == 0)
rc, out, err = run(["--incremental"])
check("--incremental", rc == 0)
# trace needs --force and a command; test it doesn't crash
rc, out, err = run(["--trace", "python3 -c print(1)", "--force"])
check("--trace", rc == 0, f"rc={rc}")

# --- 7. Scale / trust ---
print("\n[7] Scale / trust")
rc, out, err = run(["--index"])
check("--index", rc == 0 and "knowledge graph" in out, f"rc={rc}")
rc, out, err = run(["--index-status"])
check("--index-status", rc == 0)
rc, out, err = run(["--verify", LOOM])
check("--verify", rc == 0 and "sha256" in out, f"rc={rc}")
rc, out, err = run(["--install-grammars"])
check("--install-grammars", rc == 0 and "pip install" in out, f"rc={rc}")
rc, out, err = run(["--install-agents"])
check("--install-agents", rc == 0 and "AGENTS.md" in out, f"rc={rc}")
rc, out, err = run(["--session"])
check("--session", rc == 0)
rc, out, err = run(["--session-report"])
check("--session-report", rc == 0 and "session" in out, f"rc={rc}")

# --- 8. MCP server ---
print("\n[8] MCP server")
mcp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom-mcp.py")
init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
tools = '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
r = subprocess.run([sys.executable, mcp], input=init + "\n" + tools + "\n", capture_output=True, text=True, timeout=30)
tool_names = []
for line in r.stdout.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        if d.get("id") == 2:
            tool_names = [t["name"] for t in d["result"]["tools"]]
    except json.JSONDecodeError:
        pass
check("MCP init + tools/list", len(tool_names) >= 20, f"got {len(tool_names)} tools")
check("MCP has codeloom_ask", "codeloom_ask" in tool_names)
check("MCP has codeloom_pack", "codeloom_pack" in tool_names)
check("MCP has codeloom_framework", "codeloom_framework" in tool_names)
check("MCP has codeloom_session_report", "codeloom_session_report" in tool_names)

# --- 9. Benchmarks ---
print("\n[9] Benchmarks")
rc, out, err = run(["--get-symbol", "Engine"], cwd=REPO)
check("benchmark: get-symbol works", rc == 0)

print("\n" + "=" * 60)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("FAILED:", ", ".join(FAILURES))
print("=" * 60)
sys.exit(1 if FAIL else 0)
