"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const CLI = path.join(ROOT, "bin", "cli.js");
const { detect } = require(path.join(ROOT, "bin", "detect"));
const { loadAgents } = require(path.join(ROOT, "bin", "registry"));
const { loadConfig, patchConfig, backup, writeConfig } = require(path.join(ROOT, "bin", "setup"));

function tmpdir(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), "clh-" + name + "-"));
}

function fixture(count = 2) {
  const dir = tmpdir("agents");
  const agents = ["claude", "codex"].slice(0, count).map((id) => {
    const file = path.join(dir, id + ".js");
    fs.writeFileSync(file, 'console.log("Fixture 1.2.3");');
    return { id, label: id, bin: [], paths: [file], support: "builtin", defaults: { label: id } };
  });
  const reg = path.join(dir, "registry.js");
  fs.writeFileSync(reg, "module.exports={AGENTS:" + JSON.stringify(agents) + "};");
  return { dir, agents, reg, env: { ...process.env, CLAUDELINK_REGISTRY:reg,
    CLAUDELINK_HOME:dir, CLAUDELINK_AGENTS:path.join(dir,"cache.json") } };
}

test("detect: 在隔离 fixture 中识别两个可运行 CLI", () => {
  const f = fixture();
  const found = detect(f.agents, { npmRoot:"", searchRoots:[], everything:false });
  const usable = found.filter((a) => a.usable);
  assert.ok(usable.length >= 2, "usable=" + usable.length + " found=" + found.map((a) => a.id));
  for (const a of found) {
    assert.ok(fs.existsSync(a.path), "路径不存在: " + a.path);
    assert.ok(a.id && a.label && a.version, JSON.stringify(a));
  }
});

test("setup: 装配临时配置（启用/保留/备份/顺序）", () => {
  const dir = tmpdir("setup");
  const file = path.join(dir, "config.json");
  const base = loadConfig(path.join(dir, "not-found.json"), path.join(ROOT, "config.example.json"));
  base.agents = base.agents || {};
  base.agents.claude = Object.assign({}, base.agents.claude, { settings_profile: "keep-me", model: "m1" });
  writeConfig(file, base);

  const f = fixture();
  const found = detect(f.agents, { npmRoot:"", searchRoots:[], everything:false });
  const cfg = loadConfig(file);
  const res = patchConfig(cfg, found, {});
  for (const id of res.enabled) assert.equal(res.config.agents[id].enabled, true);
  assert.ok(res.config.agents.claude.settings_profile === "keep-me", "自定义配置被覆盖");
  assert.ok(res.config.dispatch.order.length >= res.enabled.length);

  const bak = backup(file);
  assert.ok(bak && fs.existsSync(bak), "没有生成备份");
  writeConfig(file, res.config);
  const reread = loadConfig(file);
  assert.equal(reread.agents.claude.enabled, true);
  assert.equal(reread.agents.claude.settings_profile, "keep-me");
  assert.ok(reread.agents.claude.exe, "exe 未写绝对路径");
});

test("cli: 一个可用 Agent 即可通过检测", () => {
  const f = fixture(1);
  const r = cp.spawnSync(process.execPath, [CLI, "detect"], {
    encoding: "utf8", env: f.env,
  });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.doesNotMatch(r.stdout, /没有识别到AIAgent/);
});

test("cli: setup 在临时配置上落盘并备份", () => {
  const dir = tmpdir("cli-setup");
  const file = path.join(dir, "config.json");
  fs.writeFileSync(file, JSON.stringify({
    server: { host: "127.0.0.1", port: 8787 }, agents: {},
  }, null, 2), "utf8");
  const f = fixture();
  const r = cp.spawnSync(process.execPath, [CLI, "setup", "--config", file], { encoding: "utf8", env:f.env });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /写入完成/);
  const out = loadConfig(file);
  const enabled = Object.keys(out.agents).filter((k) => out.agents[k].enabled);
  assert.ok(enabled.length >= 2, "enabled=" + enabled);
  const baks = fs.readdirSync(dir).filter((f) => f.startsWith("config.json.bak-detect-"));
  assert.ok(baks.length >= 1, "缺少备份");
});
