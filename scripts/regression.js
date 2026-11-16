"use strict";
const path = require("path");
const { findPython, run } = require("../bin/util");
const found = findPython();
if (!found || !found.exe) {
  console.error("Python 3.9+ required; set CLAUDELINK_PYTHON");
  process.exit(1);
}
const tests = ["test_tui_workspace.py", "test_tui_auth.py", "test_upgrade.py", "test_scheduler.py", "test_queue.py", "test_tasks.py", "test_store_idempotency.py", "test_permission_modes.py", "test_proc_tree.py"];
const detection = run(process.execPath, ["--test", path.join(__dirname, "../test/runtime-detect.test.js"), path.join(__dirname, "../test/detect-setup.test.js")], { timeout: 60000 });
process.stdout.write(detection.out || "");
if (!detection.ok) process.exit(1);
for (const file of tests) {
  const result = run(found.exe, [...(found.cmd || []).slice(1), path.join(__dirname, file)], {
    timeout: 120000, cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  process.stdout.write(result.out || "");
  if (!result.ok) process.exit(1);
}
