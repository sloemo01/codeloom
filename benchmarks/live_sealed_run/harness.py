#!/usr/bin/env python3
"""Sealed live-model benchmark: bare retrieval vs codeloom retrieval.

The protocol (honest scope):
  - ONE pinned local model (Qwen3-4B-Q4_K_M via llama-server) — reproducible,
    offline, no API drift. The SAME model sees BOTH toolchains.
  - 10 fixed tasks on the same repo (fastapi clone), graded against
    ground-truth file paths.
  - Retrieval is scripted policy (identical to bench/RESULTS.md's stance):
    bare = the realistic grep-and-read chain; codeloom = `--ask`/`--read`.
    The MODEL then answers the task from the retrieved context. This is a
    live-model end-to-end measurement (retrieval tokens + real answer
    quality), not a claim about autonomous agent loop behavior.
  - Token counts come from llama-server usage stats (prompt + completion).

Usage:
    python3 benchmarks/live_sealed_run/harness.py --mode bare   --repo /tmp/bench-fastapi
    python3 benchmarks/live_sealed_run/harness.py --mode codeloom --repo /tmp/bench-fastapi
    python3 benchmarks/live_sealed_run/harness.py --mode all    --repo /tmp/bench-fastapi

Requires: llama-server running with the pinned model, or pass --spawn.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

MODEL = "qwen3-4b-q4_k_m"
SERVER_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080/v1")
BASE = "qwen3"  # llama-server model name for OpenAI-compat endpoint
MODEL_GGUF = os.path.expanduser("~/.hermes/models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf")

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "..", "codeloom.py")

# 10 fixed tasks with ground-truth answers (file paths in fastapi)
TASKS = [
    ("Where is the File parameter class defined?", "fastapi/params.py"),
    ("Where is the HTTPException class defined?", "fastapi/exceptions.py"),
    ("Where is the FastAPI application class defined?", "fastapi/applications.py"),
    ("Which module defines the APIRouter class?", "fastapi/routing.py"),
    ("Where is the Depends function defined?", "fastapi/params.py"),
    ("Where are the HTTP status codes defined?", "fastapi/status.py"),
    ("What is the entry point of the fastapi command-line tool?", "fastapi/cli.py"),
    ("Where is the UploadFile class defined?", "fastapi/datastructures.py"),
    ("Where is the Request class defined?", "fastapi/requests.py"),
    ("Where is the JSONResponse class defined?", "fastapi/responses.py"),
]

SYSTEM = ("You are a coding agent working in a git repository. Answer the "
          "question with the exact file path where the answer lives. Reply "
          "with ONLY the file path, nothing else.")


def chat(prompt, sys=SYSTEM):
    """One chat completion against llama-server. Returns (text, usage)."""
    body = json.dumps({
        "model": BASE,
        "messages": [{"role": "system", "content": sys},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 96,
    }).encode()
    req = urllib.request.Request(SERVER_URL + "/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"], data.get("usage", {})


def bare_retrieve(repo, question):
    """Realistic grep-and-read chain; returns context text + bytes."""
    # anchor: first noun-ish token that looks like an identifier
    m = re.search(r"\b([A-Z][A-Za-z0-9_]+)\b", question)
    anchor = m.group(1) if m else "class"
    rc = subprocess.run(["rg", "-l", anchor, "--type", "py", "."],
                        cwd=repo, capture_output=True, text=True)
    files = [f for f in rc.stdout.splitlines() if f][:2]
    ctx = "\n".join(files)
    if files:
        try:
            with open(os.path.join(repo, files[0]), "r", encoding="utf-8",
                      errors="replace") as fh:
                ctx += "\n---\n" + fh.read()[:4000]
        except OSError:
            pass
    return ctx, len(ctx.encode())


def codeloom_retrieve(repo, question):
    """codeloom --ask returns a cited answer; also give the top candidate file."""
    rc = subprocess.run([sys.executable, CodeLoom, "--ask", question, "."],
                        cwd=repo, capture_output=True, text=True)
    out = rc.stdout[:6000]
    return out, len(out.encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bare", "codeloom", "all"], required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--jsonl", default=os.path.join(os.path.dirname(__file__), "run.jsonl"))
    args = ap.parse_args()

    modes = ["bare", "codeloom"] if args.mode == "all" else [args.mode]
    results = []
    for mode in modes:
        print(f"\n=== MODE: {mode} ===")
        for q, truth in TASKS:
            t0 = time.time()
            if mode == "bare":
                ctx, nb = bare_retrieve(args.repo, q)
            else:
                ctx, nb = codeloom_retrieve(args.repo, q)
            ans, usage = chat(f"{q}\n\nRepo context:\n{ctx[:4000]}")
            ok = truth in (ans or "")
            row = {"mode": mode, "question": q, "truth": truth,
                   "answer": (ans or "")[:200], "correct": ok,
                   "retrieval_bytes": nb, "tokens": usage,
                   "latency_s": round(time.time() - t0, 2)}
            results.append(row)
            print(f"  {'PASS' if ok else 'FAIL'} {q[:50]:<52} {ans[:60]!r}")

    with open(args.jsonl, "w") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")

    print(f"\n===== SUMMARY =====")
    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        ok = sum(1 for r in rows if r["correct"])
        tok = sum((r["tokens"] or {}).get("total_tokens", 0) for r in rows)
        print(f"{mode}: {ok}/{len(rows)} correct, {tok} total tokens, "
              f"{round(sum(r['latency_s'] for r in rows),1)}s")


if __name__ == "__main__":
    main()
