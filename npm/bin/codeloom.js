#!/usr/bin/env node
// codeloom CLI launcher — runs the bundled single-file Python tool.
// Requires python3 on PATH. Zero npm dependencies.
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");

const py = process.env.CODELOOM_PYTHON || "python3";
const script = path.join(__dirname, "..", "codeloom.py");

const r = spawnSync(py, [script, ...process.argv.slice(2)], {
  stdio: "inherit",
  env: process.env,
});

if (r.error) {
  console.error(
    `codeloom: failed to run ${py}: ${r.error.message}\n` +
      `codeloom requires python3 on PATH (https://www.python.org/downloads/).`
  );
  process.exit(1);
}
process.exit(r.status === null ? 1 : r.status);
