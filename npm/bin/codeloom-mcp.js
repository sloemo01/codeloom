#!/usr/bin/env node
// codeloom MCP server launcher — runs codeloom-mcp.py as a stdio MCP server.
// Works with Claude Code, Cursor, Codex, and any MCP client:
//   npx -y codeloom-mcp
// Requires python3 on PATH. Zero npm dependencies.
"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");

const py = process.env.CODELOOM_PYTHON || "python3";
const script = path.join(__dirname, "..", "codeloom-mcp.py");

const child = spawn(py, [script], { stdio: "inherit", env: process.env });

child.on("error", (err) => {
  console.error(
    `codeloom-mcp: failed to run ${py}: ${err.message}\n` +
      `codeloom requires python3 on PATH (https://www.python.org/downloads/).`
  );
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code === null ? 1 : code);
});
