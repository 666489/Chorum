#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_accept.py —— 任务书《ClaudeLink 自动识别 AI Agent》第 5 节验收脚本

维护者：ClaudeLink「项目助手」（验收/复核角色）。
用法（唯一验证过的解释器）：
    D:\Anaconda3\envs\py11\python.exe tools/check_accept.py
    D:\Anaconda3\envs\py11\python.exe tools/check_accept.py --single-agent-cmd "<只剩1个Agent的启动命令>"
    D:\Anaconda3\envs\py11\python.exe tools/check_accept.py --keep-config   # 测完不还原 config.json

规则：逐条 PASS/FAIL/SKIP，任一 FAIL 退出码 1。
只读仓库；A2/A4 会真实执行 setup/start（实现自带 .bak-detect 备份，脚本测完默认还原 config.json）。
不修改 hub.py / bin / package.json —— 那是「项目大师」的落盘范围。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
NODE = shutil.which("node") or "node"
NPM = shutil.which("npm") or "npm"
CONFIG = os.path.join(HUB, "config.json")
KEY = "没有识别到AIAgent"
FOUR = ("claude", "codex", "codebuddy", "dsh")
RESULTS = []


def rep(cid, status, detail=""):
    RESULTS.append((cid, status))
    print("[%s] %s  %s" % (status, cid, detail))
    return status == "PASS"


def run(cmd, args, cwd=HUB, timeout=180, shell=False):
    if shell:
        return subprocess.run(cmd if isinstance(cmd, str) else " ".join(cmd),
                              shell=True, cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return subprocess.run([cmd] + list(args), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


# ---------- 入口发现 ----------
def _has_main(js):
    return "require.main" in read_text(js)


def factories():
    """返回 (detect, setup, start) 三个 argv 工厂；未落盘的为 None。"""
    pkg = os.path.join(HUB, "package.json")
    if os.path.exists(pkg):
        try:
            b = read_json(pkg).get("bin") or {}
            name = b.get("claudelink-hub") or b.get("claude-hub")
            if name:
                p = os.path.normpath(name if os.path.isabs(name) else os.path.join(HUB, name))
                if os.path.exists(p):
                    mk = lambda sub: (lambda extra=[]: [NODE, p, sub] + extra)
                    return mk("detect"), mk("setup"), mk("start")
        except Exception:
            pass
    det = os.path.join(HUB, "bin", "detect.js")
    stp = os.path.join(HUB, "bin", "setup.js")
    if os.path.exists(det):
        det_f = lambda extra=[]: [NODE, det] + extra
        setup_f = (lambda extra=[]: [NODE, stp] + extra) if (os.path.exists(stp) and _has_main(stp)) else None
        return det_f, setup_f, None
    return None, None, None


# ---------- A1 detect ----------
def a1(det):
    if not det:
        return rep("A1", "SKIP", "detect 入口未落盘")
    argv = det(["--json"])
    p = run(argv[0], argv[1:])
    if p.returncode != 0:
        return rep("A1", "FAIL", "detect --json 退出码 %s：%s" % (p.returncode, (p.stdout + p.stderr)[:200]))
    raw = p.stdout.strip()
    m = re.search(r"[\[\{]", raw)
    if not m:
        return rep("A1", "FAIL", "detect --json 未输出 JSON：%s" % raw[:120])
    try:
        found = json.loads(raw[m.start():])
    except Exception as e:
        return rep("A1", "FAIL", "JSON 解析失败：%s" % e)
    hit = [a for a in found if a.get("id") in FOUR]
    ok = len(hit) >= 2
    bad = []
    for a in hit:
        if not re.match(r"^[A-Za-z]:[\\/]", str(a.get("path", ""))):
            bad.append(a.get("id") + ":路径非绝对")
        if str(a.get("version", "?")) in ("?", ""):
            bad.append(a.get("id") + ":版本缺失")
    ok = ok and not bad
    return rep("A1", "PASS" if ok else "FAIL",
               "命中 %d/%s 个：%s%s" % (len(hit), "+".join(FOUR),
               ", ".join("%s@%s" % (a.get("id"), a.get("version")) for a in hit),
               ("；问题：" + ";".join(bad)) if bad else ""))


# ---------- A2L setup 库级语义（不依赖 CLI 落盘） ----------
A2L_JS = r"""
const path = require("path");
const fs = require("fs");
const { patchConfig, backup, writeConfig } = require("__HUB__/bin/setup.js");
const dir = process.argv[2];
const cfgPath = path.join(dir, "config.json");
const cfg = {
  dispatch: { mode: "sequential", order: ["codex", "claude"] },
  agents: {
    claude: { enabled: true, exe: "C:/old/claude.cmd", model: "custom-model-x" },
    codex:  { enabled: true, exe: "C:/old/codex.exe" }
  }
};
fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), "utf8");
const before = fs.readFileSync(cfgPath, "utf8");
const bak = backup(cfgPath);
const r = patchConfig(cfg, [{ id: "claude", path: "C:/fake/claude.cmd" }], {});
writeConfig(cfgPath, r.config);
const after = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
console.log(JSON.stringify({
  bak: bak ? path.basename(bak) : "",
  bakContent: bak ? fs.readFileSync(bak, "utf8") : "",
  before: before,
  after: after
}));
"""


def is_subseq(small, big):
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def a2l():
    js = os.path.join(HUB, "bin", "setup.js")
    if not os.path.exists(js):
        return rep("A2L", "SKIP", "bin/setup.js 未落盘")
    tmp = tempfile.mkdtemp(prefix="clh-a2l-")
    jf = os.path.join(tmp, "t.js")
    with open(jf, "w", encoding="utf-8") as f:
        f.write(A2L_JS.replace("__HUB__", HUB.replace("\\", "/")))
    p = run(NODE, [jf, tmp])
    ok = p.returncode == 0
    detail = ""
    if ok:
        try:
            d = json.loads(p.stdout.strip())
        except Exception as e:
            return rep("A2L", "FAIL", "结果解析失败：%s / %s" % (e, (p.stdout + p.stderr)[:200]))
        problems = []
        if not re.search(r"\.bak-detect-\d{8}-\d{6}$", d["bak"] or ""):
            problems.append("备份命名不符合 .bak-detect-<时间戳>：" + str(d["bak"]))
        if d["bakContent"] != d["before"]:
            problems.append("备份内容 != 写前内容（写前未备份）")
        ag = d["after"]["agents"]
        if ag.get("claude", {}).get("model") != "custom-model-x":
            problems.append("自定义 model 被丢弃")
        if ag.get("claude", {}).get("exe") != "C:/fake/claude.cmd":
            problems.append("exe 未按检测结果更新")
        if ag.get("codex", {}).get("enabled") is not False:
            problems.append("未检测到的 codex 没有 enabled=false")
        if ag.get("codex", {}).get("exe") != "C:/old/codex.exe":
            problems.append("未检测成员的既有 exe 被改动")
        old_order = [x for x in d["before"] and json.loads(d["before"])["dispatch"]["order"]
                     if x in ag]
        new_order = d["after"]["dispatch"]["order"]
        if not is_subseq(old_order, new_order):
            problems.append("自定义 dispatch.order 顺序被打乱：旧 %s -> 新 %s" % (old_order, new_order))
        ok = not problems
        detail = "备份=%s；" % (d["bak"] or "无") + ("；".join(problems) if problems else "语义全部符合")
    else:
        detail = "node 测试脚本报错：%s" % (p.stderr or p.stdout)[:200]
    shutil.rmtree(tmp, ignore_errors=True)
    return rep("A2L", "PASS" if ok else "FAIL", detail)


# ---------- A2 setup CLI ----------
def a2(setup_f, keep):
    if not setup_f:
        return rep("A2", "SKIP", "setup CLI 未落盘（bin/setup.js 无 require.main 入口）")
    before = read_text(CONFIG)
    baks_before = set(_bak_list())
    argv = setup_f(["--yes"])
    p = run(argv[0], argv[1:])
    if p.returncode != 0:
        return rep("A2", "FAIL", "setup 退出码 %s：%s" % (p.returncode, (p.stdout + p.stderr)[:200]))
    new_baks = _bak_list()
    added = [b for b in new_baks if b not in baks_before]
    problems = []
    if not added:
        problems.append("写前未生成 .bak-detect 备份")
    else:
        if read_text(os.path.join(HUB, added[-1])) != before:
            problems.append("备份内容 != 写前 config.json")
    try:
        cfg = read_json(CONFIG)
    except Exception as e:
        problems.append("setup 后 config.json 解析失败：%s" % e)
        cfg = None
    if cfg is not None:
        det = _detect_ids()
        for name, a in (cfg.get("agents") or {}).items():
            if a.get("enabled") and name not in det:
                problems.append("未检测到的 %s 仍 enabled=true" % name)
        for name in det:
            if not (cfg.get("agents") or {}).get(name, {}).get("enabled"):
                problems.append("检测到的 %s 未启用" % name)
    if not keep and before != "":
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write(before)
    return rep("A2", "PASS" if not problems else "FAIL",
               "新增备份=%s；%s" % (added[-1] if added else "无", "；".join(problems) or "配置装配符合要求"))


def _bak_list():
    try:
        return sorted(f for f in os.listdir(HUB) if re.match(r"config\.json\.bak-detect-", f))
    except Exception:
        return []


def _detect_ids():
    det, _, _ = factories()
    if not det:
        return set()
    argv = det(["--json"])
    p = run(argv[0], argv[1:])
    try:
        i = re.search(r"[\[\{]", p.stdout)
        return {a.get("id") for a in json.loads(p.stdout[i.start():]) if a.get("usable")}
    except Exception:
        return set()


# ---------- A3 稀缺（<2 个可用 Agent） ----------
def a3(args):
    if not args.single_agent_cmd:
        print()
        print("【验收前置要求】A3 需要「只剩 1 个 Agent」的确定性模拟入口"
              "（例如 CLAUDELINK_DETECT_ONLY=<id> 环境变量或 detect --only <id>），"
              "当前实现只能靠固定绝对路径发现 Agent，无法用 PATH/环境隔离模拟稀缺。"
              "请在实现里提供该入口后，用 --single-agent-cmd 传入完整命令重跑本条。")
        return rep("A3", "SKIP", "未提供 --single_agent_cmd（稀缺模拟入口待项目大师提供）")
    port = args.port
    before = listeners(port)
    p = run(args.single_agent_cmd, [], cwd=HUB, timeout=90, shell=True)
    after = listeners(port)
    problems = []
    if p.returncode != 1:
        problems.append("退出码=%s（要求 1）" % p.returncode)
    if (p.stdout or "").strip() != KEY:
        problems.append("stdout 不是唯一关键句：%r" % (p.stdout or "")[:120])
    if (p.stderr or "").strip():
        problems.append("stderr 非空：%r" % p.stderr[:120])
    if after - before:
        problems.append("仍然启动了服务（端口 %d 出现新监听）" % port)
    return rep("A3", "PASS" if not problems else "FAIL", "；".join(problems) or "仅输出关键句 + exit 1 + 未启动服务")


# ---------- A4 start ----------
def http_status(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def listeners(port):
    out = run("netstat", ["-ano"], cwd=os.getcwd()).stdout or ""
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-1] != "0" and parts[3] == "LISTENING":
            lp = parts[1].rsplit(":", 1)
            if len(lp) == 2 and lp[1] == str(port):
                pids.add(parts[4])
    return pids


def a4(start_f, keep):
    if not start_f:
        return rep("A4", "SKIP", "start 入口未落盘（package.json/bin 启动器未交付）")
    port = 18787
    if listeners(port):
        return rep("A4", "SKIP", "端口 %d 已被占用，跳过" % port)
    before_cfg = read_text(CONFIG)
    argv = start_f(["--port", str(port)])
    p = run(argv[0], argv[1:], timeout=90)
    problems = []
    st = None
    for _ in range(20):
        st = http_status("http://127.0.0.1:%d/api/health" % port)
        if st:
            break
        time.sleep(1)
    if st != 200:
        problems.append("/api/health 未返回 200（%s）" % st)
    pids1 = listeners(port)
    if len(pids1) != 1:
        problems.append("监听进程数 %d != 1" % len(pids1))
    argv2 = start_f(["--port", str(port)])
    run(argv2[0], argv2[1:], timeout=60)
    time.sleep(3)
    pids2 = listeners(port)
    if pids2 != pids1:
        problems.append("重复 start 产生了第二个进程：%s -> %s" % (pids1, pids2))
    # 清理：先请求关停，再兜底 kill 本端口新增进程
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/shutdown" % port,
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    time.sleep(3)
    for pid in listeners(port):
        run("taskkill", ["/PID", pid, "/F"], cwd=os.getcwd())
    if not keep and before_cfg != "":
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write(before_cfg)
    return rep("A4", "PASS" if not problems else "FAIL",
               "；".join(problems) or "health 200 + 单实例 + 重复 start 不重复拉起（已清理）")


# ---------- A5 npm pack + 临时安装 ----------
FORBIDDEN = ("node_modules/", ".verify/", "hub.db", "uploads/", "backup/")


def a5():
    pkg = os.path.join(HUB, "package.json")
    if not os.path.exists(pkg):
        return rep("A5", "SKIP", "package.json 未落盘")
    tmp = tempfile.mkdtemp(prefix="clh-pack-")
    tmpi = tempfile.mkdtemp(prefix="clh-inst-")
    try:
        problems = []
        p = run(NPM, ["pack", "--pack-destination", tmp], timeout=300)
        tgzs = [f for f in os.listdir(tmp) if f.endswith(".tgz")] if p.returncode == 0 else []
        if not tgzs:
            return rep("A5", "FAIL", "npm pack 失败：%s" % ((p.stdout or "") + (p.stderr or ""))[:300])
        tgz = os.path.join(tmp, tgzs[0])
        t = run("tar", ["-tzf", tgz], cwd=tmp)
        bad = [f for f in (t.stdout or "").splitlines() if any(k in f for k in FORBIDDEN)]
        if bad:
            problems.append("tgz 含禁止路径：" + ";".join(bad[:5]))
        pi = run(NPM, ["install", tgz, "--no-audit", "--no-fund"], cwd=tmpi, timeout=300)
        if pi.returncode != 0:
            problems.append("临时目录 npm i 失败：%s" % (pi.stderr or "")[:200])
        else:
            try:
                b = read_json(os.path.join(tmpi, "node_modules", "claudelink-hub", "package.json")).get("bin") or {}
                name = b.get("claudelink-hub") or b.get("claude-hub")
                binp = os.path.normpath(os.path.join(tmpi, "node_modules", "claudelink-hub", name))
                pd = run(NODE, [binp, "detect", "--json"], cwd=tmpi, timeout=120)
                i = re.search(r"[\[\{]", pd.stdout or "")
                found = json.loads(pd.stdout[i.start():]) if i else []
                if pd.returncode != 0 or len(found) < 2:
                    problems.append("安装后 detect 异常（rc=%s, 命中=%d）" % (pd.returncode, len(found)))
                else:
                    pass
            except Exception as e:
                problems.append("安装后解析/执行失败：%s" % e)
        return rep("A5", "PASS" if not problems else "FAIL",
                   "tgz=%s；%s" % (tgzs[0], "；".join(problems) or "pack + 临时安装 + detect 全部可用"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmpi, ignore_errors=True)


# ---------- A6 既有回归 ----------
def a6():
    ok = run(PY, ["-m", "py_compile", "hub.py"]).returncode == 0
    rep("A6.1", "PASS" if ok else "FAIL", "py_compile hub.py")
    for cid, script in (("A6.2", "tools/check_static.py"), ("A6.3", "tools/check_locks.py")):
        sp = os.path.join(HUB, script)
        if not os.path.exists(sp):
            rep(cid, "SKIP", script + " 不存在")
            continue
        r = run(PY, [script], timeout=180)
        rep(cid, "PASS" if r.returncode == 0 else "FAIL",
            "%s%s" % (script, "" if r.returncode == 0 else "：" + ((r.stdout or "") + (r.stderr or ""))[:200]))
    r = run(PY, ["hub_tui.py", "--check"], timeout=60)
    up = None
    try:
        s = r.stdout or ""
        i = s.find("{")
        up = json.loads(s[i:]).get("up") if i >= 0 else None
    except Exception:
        pass
    if r.returncode != 0 and up is False and not listeners(8787):
        rep("A6.4", "SKIP", "hub_tui --check：hub 未运行（环境状态，非回归）")
    else:
        rep("A6.4", "PASS" if r.returncode == 0 else "FAIL",
            "hub_tui --check%s" % ("" if r.returncode == 0 else "：" + ((r.stdout or "") + (r.stderr or ""))[:200]))
    web = os.path.join(HUB, "web", "index.html")
    rep("A6.5", "PASS" if os.path.getsize(web) > 1000 else "FAIL", "web/index.html 存在且非空")


# ---------- A7 文档 ----------
def a7():
    rd = read_text(os.path.join(HUB, "README.md"))
    need = ("安装", "检测", "启动", "打包")
    miss = [k for k in need if k not in rd and k != "打包"] + (["打包"] if ("打包" not in rd and "npm pack" not in rd) else [])
    rep("A7.1", "PASS" if not miss else "SKIP" if len(rd) < 200 else "FAIL",
        "README 章节" + ("齐全（安装/检测/启动/打包）" if not miss else "缺：" + ",".join(miss)))
    doc = os.path.join(HUB, "docs", "agent-integration.md")
    if not os.path.exists(doc):
        return rep("A7.2", "SKIP", "docs/agent-integration.md 未落盘")
    txt = read_text(doc)
    ids = [a for a in ("claude", "codex", "codebuddy", "dsh", "gemini", "qwen", "opencode", "aider") if a in txt.lower()]
    rep("A7.2", "PASS" if (os.path.getsize(doc) > 1024 and len(ids) >= 4) else "FAIL",
        "agent-integration.md 覆盖 %d 个 Agent：%s" % (len(ids), ",".join(ids)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--single-agent-cmd", default="", help="只剩1个Agent时的完整启动命令（测第3条）")
    ap.add_argument("--port", type=int, default=8787, help="A3 用的服务端口")
    ap.add_argument("--keep-config", action="store_true", help="测完不还原 config.json")
    args = ap.parse_args()

    print("hub     =", HUB)
    print("python  =", PY)
    print("node    =", NODE)
    print()
    det, setup_f, start_f = factories()
    print("入口    : detect=%s setup=%s start=%s" % ("有" if det else "无", "有" if setup_f else "无", "有" if start_f else "无"))
    print()
    a1(det)
    a2l()
    a2(setup_f, args.keep_config)
    a3(args)
    a4(start_f, args.keep_config)
    a5()
    a6()
    a7()

    n = {s: sum(1 for _, x in RESULTS if x == s) for s in ("PASS", "FAIL", "SKIP")}
    print()
    print("汇总：PASS=%d FAIL=%d SKIP=%d" % (n["PASS"], n["FAIL"], n["SKIP"]))
    print("重点关注：F1(<2个Agent)=A3  F2(写前备份)=A2/A2L  F3(pack+临时安装)=A5")
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()
