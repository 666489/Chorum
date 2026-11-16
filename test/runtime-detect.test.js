"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { detect } = require("../bin/detect");
const { patchConfig } = require("../bin/setup");

test("detection rejects a broken launcher and tries the next candidate", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clh-probe-"));
  const bad = path.join(dir, "bad.js"), good = path.join(dir, "good.js");
  fs.writeFileSync(bad, 'console.log("error 9.9.9"); process.exit(2);');
  fs.writeFileSync(good, 'console.log("Agent 1.2.3");');
  const agent = { id: "probe", label: "Probe", bin: [], paths: [bad, good], support: "builtin" };
  const found = detect([agent], { npmRoot: "", searchRoots: [], everything: false });
  assert.equal(found[0].path, good);
  assert.equal(found[0].usable, true);
  assert.equal(found[0].probes[0].ok, false);
  const broken = detect([{ ...agent, paths: [bad] }], { npmRoot: "", searchRoots: [], everything: false });
  assert.equal(broken[0].usable, false);
  const cfg = { agents: { probe: { enabled: true } } };
  patchConfig(cfg, broken, { registry: [agent] });
  assert.equal(cfg.agents.probe.enabled, false);
});

test("npm package bin metadata works without a global shim", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clh-npm-bin-"));
  const pkg = path.join(dir, "@example", "agent");
  fs.mkdirSync(pkg, { recursive: true });
  fs.writeFileSync(path.join(pkg, "package.json"), JSON.stringify({ bin: { "test-fixture-agent": "cli.js" } }));
  fs.writeFileSync(path.join(pkg, "cli.js"), 'console.log("Fixture 2.3.4");');
  const agent = { id: "fixture", label: "Fixture", bin: ["test-fixture-agent"], packages: ["@example/agent"], support: "builtin" };
  const found = detect([agent], { npmRoot: dir, searchRoots: [], everything: false });
  assert.equal(found[0].source, "npm-package");
  assert.equal(found[0].usable, true);
});

test("setup preserves an intentionally disabled member", () => {
  const registry=[{id:"codex",label:"Codex",support:"builtin"}];
  const config={agents:{codex:{enabled:false,model:"keep-model"}}};
  patchConfig(config,[{id:"codex",path:"codex",usable:true}],{registry});
  assert.equal(config.agents.codex.enabled,false);
  assert.equal(config.agents.codex.model,"keep-model");
});
