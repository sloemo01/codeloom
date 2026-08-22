"""Importable entry point for the codeloom MCP server (pip packaging).

The main server lives in `codeloom-mcp.py` (hyphenated, not importable as a
module). This shim loads it so `pip install codeloom` exposes the
`codeloom-mcp` console command.
"""
import importlib.util
import os
import sys


def _load_server():
    here = os.path.dirname(os.path.abspath(__file__))
    # candidate locations: next to shim (source), data-files dir, site-packages
    candidates = [
        os.path.join(here, "codeloom-mcp.py"),
        os.path.join(here, "codeloom_mcp_server.py"),
        os.path.join(here, "..", "codeloom", "codeloom-mcp.py"),
        os.path.join(here, "..", "..", "codeloom", "codeloom-mcp.py"),
        os.path.join(here, "..", "..", "..", "codeloom", "codeloom-mcp.py"),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        raise FileNotFoundError("codeloom-mcp.py not found in package")
    spec = importlib.util.spec_from_file_location("codeloom_mcp_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = _load_server()
    sys.exit(mod.serve())


if __name__ == "__main__":
    main()
