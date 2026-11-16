#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude x Codex x CodeBuddy 群聊中枢（可扩展模型）

- 统一窗口：浏览器网页群聊，所有成员发言汇进同一条时间线
- 定向/广播：发给全体，或只跟某一方说话
- 自动触发：@成员 让被点名方自动起一轮，接力协作
- 模型管理：随时添加 OpenAI 风格 / Anthropic 风格的自定义模型，可停用、可删除
- 旁听 ClaudeLink：nexus.db 里的 MCP 消息同步显示到同一个窗口
"""

import json
import os
import queue
import re
import shutil
import glob
import atexit
import hashlib
import copy
import ctypes
import signal
import socket
import sqlite3
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import contextlib
from runtime import Memory, ToolRuntime, bounded_history, api_tool_loop
from workflows import Workflows
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
# 可变状态（配置 / DB / 日志 / 上传 / PID）放在哪：
#   默认仍是包目录（向后兼容，你现在的用法不变）；
#   系统级 npm 全局安装（包目录只读）时，npm CLI 会设 CLAUDELINK_HOME 指向用户数据目录。
DATA_DIR = os.path.abspath(os.environ.get("CLAUDELINK_HOME") or HUB_DIR)

# usage 的桶口径版本（写进 state.usage_convention，见 Store._migrate 末尾）。
# 定这个口径的依据是 2026-09-29 对 api.deepseek.com 的实测，记在 .verify/sec26.md。
USAGE_CONVENTION = "disjoint-4bucket@2026-09-29"
# 冷启动提示的**次判据**默认阈值（分钟）：距上次调用超过它就提示"可能冷启动"。
# 10 = DeepSeek 档。各家文档下限：Anthropic ≈5、OpenAI ≈30、DeepSeek ≈10。
# **这是下限不是淘汰时刻** —— 同一会话内实测隔 12.8h 第 1 次调用仍能命中，
# 所以措辞只能是"可能"，真正必冷的判据是会话变更（见 Store.last_settled_turns_by_agent）。
DEFAULT_COLD_START_MINUTES = 10
CONFIG_PATH = os.environ.get("CLAUDELINK_CONFIG") or os.path.join(DATA_DIR, "config.json")
DB_PATH = os.environ.get("CLAUDELINK_DB") or os.path.join(DATA_DIR, "hub.db")
WEB_DIR = os.path.join(HUB_DIR, "web")

# --- GET /api/file 的拒绝表（§27 文件直链）---------------------------------
# 白名单规则（2026-09-26 复核，与实现一致）：
#   trace.file_roots 非空 → 只放行 realpath 落在这几个根里的路径；
#   trace.file_roots 为空/缺失 → 默认根 = 中枢目录 + workspace.cwd（不是"全盘放行"）。
# 无论走哪个白名单，下面这张拒绝表始终生效（凭据/数据库/密钥一类永不直链）。
FILE_DENY_PARTS = {".git", ".ssh", ".aws", ".gnupg", ".config", ".ccm", ".azure", ".kube",
                   ".docker", "__pycache__", "node_modules"}
FILE_DENY_NAMES = {"hub.db", "hub.db-wal", "hub.db-shm", "id_rsa", "id_ed25519",
                   "id_ecdsa", ".env", ".netrc", "credentials", ".npmrc", ".pypirc",
                   ".git-credentials", ".vault-token", "auth.json", "settings.local.json",
                   # 本仓库自己的现场/内部文件：别让本地恶意页面通过直链读走
                   "config.json", "hub.pids.json", "hub.log", "hub.errors.log", "restart.log",
                   "agents.json", "agents.detected.json", ".leak-denylist",
                   "运维.md", "交接-当前状态.md", "诊断日志-交接.md",
                   "trace-spec.md", "缓存命中成本报告.md"}
FILE_DENY_EXTS = {".db", ".sqlite", ".sqlite3", ".key", ".pem", ".pfx", ".p12", ".jks", ".token"}
FILE_MAX_BYTES = 512 * 1024
# 数据目录里的敏感文件（DATA_DIR 可能是包目录，也可能是 ~/.claudelink-hub）
HUB_SENSITIVE = {os.path.join(os.path.realpath(DATA_DIR), n) for n in
                 ("config.json", "hub.db", "hub.db-wal", "hub.db-shm", "hub.errors.log")}

def legacy_ts_to_ms(ts):
    """老表（messages.ts / perm_requests.ts，REAL 秒）→ 毫秒整数。

    §7.1：全库唯一允许出现 `*1000` 的地方。跨表比较/断言一律调它，禁止内联乘 1000。
    """
    return round(ts * 1000)


def now_ms():
    """新表（turns/steps）取当前时刻的唯一入口，复用 legacy_ts_to_ms。"""
    return legacy_ts_to_ms(time.time())


BOOT_MS = now_ms()              # 进程启动时刻（UTC 毫秒），§7.4 启动自愈的分界
MENTION_RE = re.compile(r"@([^\s@，。,.!?;:：；！？、()（）\[\]<>]+)")
PERM_RE = re.compile(r"\[\[\s*PERM\s+(rw|ro)\s+([^|\]]+?)(?:\|([^\]]*))?\s*\]\]", re.IGNORECASE)
ORDER_RE = re.compile(r"\[\[\s*ORDER\s+([^\]]+?)\s*\]\]", re.IGNORECASE)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200
STYLES = ("anthropic", "openai")
PERMISSION_MODES = ("read-only", "workspace-write", "danger")


def load_config():
    """读配置。缺失时退回 config.example.json（**不写盘**）并给可操作提示。

    这样 `python hub.py` 首次运行不会甩一个 FileNotFoundError 堆栈；
    要固化成自己的 config 就跑 `claudelink-hub start`（npm）或 `node bin/cli.js setup`。
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        example = os.path.join(HUB_DIR, "config.example.json")
        if not os.path.exists(example):
            print(f"[hub] 配置文件不存在：{CONFIG_PATH}", file=sys.stderr, flush=True)
            print("[hub] 先运行 `claudelink-hub start`（npm 安装）或 `node bin/cli.js setup` 生成，",
                  file=sys.stderr, flush=True)
            raise SystemExit(2)
        print(f"[hub] 配置不存在：{CONFIG_PATH}", flush=True)
        print(f"[hub] 暂按模板启动：{example}（要固化请跑 `claudelink-hub start`）", flush=True)
        with open(example, encoding="utf-8") as fh:
            return json.load(fh)


CONFIG = load_config()
CONFIG_LOCK = threading.Lock()
# 内存 CONFIG 对应的「上次落盘内容」：落盘时用它算 hub 自己改了哪些键
_CONFIG_BASE = copy.deepcopy(CONFIG)

# 新 schema 是嵌套的；下面这张表让老写法/手工改的扁平键也能用
_CFG_ALIASES = {
    "cache_prefix": "context.cache_prefix",
    "cwd": "workspace.cwd",
    "host": "server.host",
    "port": "server.port",
    "turn_timeout_sec": "limits.turn_timeout_sec",
    "max_output_chars": "limits.max_output_chars",
    "history_limit": "context.history_limit",
    "memory_mode": "context.memory_mode",
    "shared_context": "context.shared_context",
    "nexus_mirror": "claudelink.mirror",
    "activity_bar": "ui.activity_bar",
    "streaming": "streaming",
    "relay": "relay",
    "permissions": "permissions",
}


def _dig(data, dotted, default=None):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def cfg_get(key, default=None):
    """先按新嵌套路径取，再退回老的扁平键。"""
    dotted = _CFG_ALIASES.get(key, key)
    value = _dig(CONFIG, dotted, None)
    if value is None:
        value = CONFIG.get(key, None)
    return default if value is None else value


def cfg_agent(name):
    """CLI 成员取 agents.<name>，自定义 API 模型取 model_overrides.<name>。"""
    node = CONFIG.get("agents", {}).get(name)
    if isinstance(node, dict):
        return node
    node = CONFIG.get("model_overrides", {}).get(name)
    return node if isinstance(node, dict) else {}


def turn_timeout(agent=None):
    """单轮超时（秒）；0 或负数 = 无上限。成员级 settings 优先于全局。"""
    if agent:
        per = cfg_agent(agent).get("turn_timeout_sec")
        if per is not None and per != "":
            try:
                value = int(per)
                return 0 if value <= 0 else value
            except (TypeError, ValueError):
                pass
    try:
        value = int(cfg_get("limits.turn_timeout_sec", 1800) or 0)
    except (TypeError, ValueError):
        return 1800
    return 0 if value <= 0 else value


def http_timeout(agent=None):
    """HTTP 调用超时；无上限时给 None（urllib 语义：阻塞到返回）。"""
    value = turn_timeout(agent)
    return None if value == 0 else value


# ── 配置文件锁：与 bin/util.js 的 withFileLock 同一套协议 ────────────────────
# 目录锁（mkdir 原子）+ owner.json{pid,host,at}；pid 已死或超过 30s 视为残留。
# CLI（Node）和 hub（Python）抢的是同一把锁，不是各锁各的。
CONFIG_LOCK_TIMEOUT = 3.0
CONFIG_LOCK_STALE = 30.0
_MISSING = object()


def _config_lock_dir(path):
    folder, name = os.path.split(os.path.abspath(path))
    return os.path.join(folder, "." + name + ".lock")


def _pid_alive(pid):
    """进程是否还活着。Windows 上**不能**用 os.kill(pid, 0)——那会真的终止目标进程。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                        # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


@contextlib.contextmanager
def config_file_lock(path=None, timeout=CONFIG_LOCK_TIMEOUT, stale=CONFIG_LOCK_STALE):
    """拿跨进程配置锁；拿不到（只读目录 / 等待超时）也继续，由写前重读 + 原子替换兜底。"""
    path = path or CONFIG_PATH
    if os.environ.get("CLAUDELINK_NO_LOCK") == "1":
        yield False
        return
    lock_dir = _config_lock_dir(path)
    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                os.makedirs(os.path.dirname(lock_dir) or ".", exist_ok=True)
                os.mkdir(lock_dir)
                acquired = True
                break
            except FileExistsError:
                owner = {}
                try:
                    with open(os.path.join(lock_dir, "owner.json"), encoding="utf-8") as fh:
                        owner = json.load(fh) or {}
                except Exception:
                    owner = {}
                try:
                    age = time.time() - os.path.getmtime(lock_dir)
                except OSError:
                    age = 0.0
                if (owner.get("pid") and not _pid_alive(owner["pid"])) or age > stale:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
                if time.time() >= deadline:
                    print("[hub] 等配置锁超时（持有者 pid %s），改为直接读-改-写" % owner.get("pid"), flush=True)
                    break
                time.sleep(0.05)
            except OSError:
                break
        if acquired:
            try:
                with open(os.path.join(lock_dir, "owner.json"), "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                               "at": datetime.now(timezone.utc).isoformat()}, fh)
            except Exception:
                pass
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)


def _merge_config(disk, base, cur):
    """三方合并：以磁盘为准，只把 hub 自己改过（base → cur）的键盖上去。

    dict 递归合并；list / 标量 / 类型变化整块用 cur 替换；base 里有而 cur 里没有的键视为删除。
    这样 CLI 在 hub 运行期间改的键（新成员、端口、dispatch.order）不会被整份快照冲掉。
    """
    if not isinstance(cur, dict):
        return copy.deepcopy(cur)
    disk = disk if isinstance(disk, dict) else {}
    base = base if isinstance(base, dict) else {}
    merged = dict(disk)
    for key, value in cur.items():
        old = base.get(key, _MISSING)
        if old is _MISSING:
            merged[key] = copy.deepcopy(value)
        elif old != value:
            merged[key] = _merge_config(disk.get(key), old, value)
    for key in base:
        if key not in cur:
            merged.pop(key, None)
    return merged


def save_config():
    """把内存改动写回 config.json：同一把文件锁 + 与磁盘三方合并 + 原子替换。

    CONFIG 是「启动快照 + 运行期改动」，直接整份写会把 CLI 这期间改的东西覆盖掉；
    所以落盘时以磁盘为准，只盖 hub 改过的键，合并结果再回写内存（两边保持一致）。
    锁或合并出问题时退回原来的整份写 —— 保存功能不能因为加固而失效。
    """
    global CONFIG, _CONFIG_BASE
    with CONFIG_LOCK:
        with config_file_lock(CONFIG_PATH):
            try:
                disk = load_config() if os.path.exists(CONFIG_PATH) else {}
                merged = _merge_config(disk, _CONFIG_BASE, CONFIG)
            except Exception as exc:
                print(f"[hub] 配置合并失败，改为整份写入：{exc}", flush=True)
                disk, merged = {}, CONFIG
            if merged != disk:
                tmp = f"{CONFIG_PATH}.tmp-{os.getpid()}"
                try:
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(merged, fh, ensure_ascii=False, indent=2)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, CONFIG_PATH)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            CONFIG = merged
            _CONFIG_BASE = copy.deepcopy(merged)


def reload_config():
    global CONFIG, _CONFIG_BASE, BUILTIN, LABELS, DEFAULT_CWD, CWD_WARNING
    CONFIG = load_config()
    _CONFIG_BASE = copy.deepcopy(CONFIG)
    BUILTIN = list(CONFIG.get("agents", {}).keys())
    LABELS = {k: v.get("label", k) for k, v in CONFIG.get("agents", {}).items()}
    DEFAULT_CWD, CWD_WARNING = _resolve_workspace(cfg_get("cwd"))


BUILTIN = list(CONFIG["agents"].keys())
LABELS = {k: v.get("label", k) for k, v in CONFIG["agents"].items()}


def _resolve_workspace(raw):
    """工作目录必须真实存在；配了不存在的目录时回退到数据目录并给出告警。

    旧实现是在 main() 里无条件 `os.makedirs(DEFAULT_CWD)`：一旦 `workspace.cwd` 写的是
    迁移前的旧路径（本机就是 `D:\\AI-GroupChat`，项目已搬到 F:），中枢会**默默建出一个空目录**，
    所有 CLI 成员都在那个空目录里工作 —— 读不到项目文件、授权的目录也不在那儿；
    API 模型的工具根同样指向它，于是「连得上但什么都读不到」，排查成本很高。
    现在：存在就用，不存在就回退到数据目录，并在启动日志和群里各说一次。
    """
    if not raw:
        return DATA_DIR, ""
    path = os.path.abspath(os.path.expanduser(str(raw)))
    if os.path.isdir(path):
        return path, ""
    return DATA_DIR, (f"workspace.cwd 指向的目录不存在：{path}。本轮改用数据目录 {DATA_DIR} 作为工作目录；"
                      f"若项目换过位置，把 config.json 的 workspace.cwd 改成新路径。")


DEFAULT_CWD, CWD_WARNING = _resolve_workspace(cfg_get("cwd"))     # 读 workspace.cwd（不是旧的扁平键 cwd）


def slug(text):
    return re.sub(r"[^a-z0-9\-]+", "-", (text or "").strip().lower()).strip("-")


# --------------------------------------------------------------------- trace 工具
# now_ms / legacy_ts_to_ms / BOOT_MS 定义在文件顶部（BOOT_MS 要在导入时就取样）
def trace_cfg():
    return cfg_get("trace", {}) or {}


def trace_enabled():
    return bool(trace_cfg().get("enabled", True))


ERROR_LOG = os.path.join(DATA_DIR, "hub.errors.log")
TOKEN_FILE = os.path.join(DATA_DIR, "hub.token")
SERVER_TOKEN = ""


def _package_version(fallback="0.5.0"):
    """版本号只维护一处：package.json。

    以前 npm 包版本和这里的常量各写一遍，升级包时漏改一个就会出现
    「CLI 认为运行中的服务是旧版本」→ 每次命令都先 stop 再 start：
    正在跑的一轮被打断、`ask` 的 SSE 断在半路，看起来像「连不上」。
    """
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json"),
                  encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or fallback)
    except (OSError, ValueError):
        return fallback


APP_VERSION = _package_version()
with open(__file__, "rb") as _code_file:
    CODE_REVISION = hashlib.sha256(_code_file.read()).hexdigest()[:12]


_SECRET_RE = re.compile(r"\b(?:sk|ak|ark)-[A-Za-z0-9_-]{8,}|\bBearer\s+[A-Za-z0-9._-]{10,}", re.I)


def scrub_secrets(text):
    """把错误文本里可能回显的 API Key / Token 抹掉（供应商的调试体偶尔会带出来）。"""
    return _SECRET_RE.sub(lambda m: m.group(0)[:6] + "***", str(text or ""))


def log_agent_error(label, exc=None, prompt="", live=None):
    """把成员轮次的异常和现场写进 hub.errors.log（threading.excepthook 也用它）。"""
    try:
        import traceback as _tb

        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {label} ===\n")
            fh.write(f"prompt 长度: {len(prompt or '')}\n")
            tail = "".join(live or [])[-400:]
            if tail:
                fh.write(f"最后输出: {scrub_secrets(tail)}\n")
            if exc is not None:
                fh.write(scrub_secrets(f"{type(exc).__name__}: {exc}") + "\n")
            fh.write(scrub_secrets(_tb.format_exc()))
    except Exception:
        pass


def _thread_excepthook(args):
    try:
        name = args.thread.name if getattr(args, "thread", None) else "?"
    except Exception:
        name = "?"
    log_agent_error(f"thread:{name}", getattr(args, "exc_value", None))


def ensure_server_token():
    """读取/生成本地 API token；持久化，重启不换，避免 UI/SSE 掉线。"""
    global SERVER_TOKEN
    if SERVER_TOKEN:
        return SERVER_TOKEN
    env = (os.environ.get("CLAUDELINK_TOKEN") or "").strip()
    if env:
        SERVER_TOKEN = env
        return SERVER_TOKEN
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            SERVER_TOKEN = token
            return token
    except Exception:
        pass
    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
    except Exception as exc:
        print(f"[hub] 无法写入 token 文件 {TOKEN_FILE}: {exc}", flush=True)
    SERVER_TOKEN = token
    return token


def trace_capture_prompt():
    return bool(trace_cfg().get("capture_prompt", False))


def detail_max_bytes():
    try:
        return max(256, int(trace_cfg().get("detail_max_bytes", 8192)))
    except (TypeError, ValueError):
        return 8192


def _clip_text(text, limit):
    """按 UTF-8 字节截到 limit 以内，截断点落在合法 UTF-8 边界。"""
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text
    keep = raw[:limit].decode("utf-8", "ignore")
    return keep + f"…（截断，共 {len(raw)} 字节）"


def _clip_value(value, limit):
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, dict):
        return {k: _clip_value(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip_value(v, limit) for v in value]
    return value


def fit_detail(obj, limit=None):
    """把任意对象变成恒为 JSON 对象的字符串，超限时按字节截断并保留结构（§6）。

    limit=0 表示不截断（思维链专用；见 Store._detail_budget）。
    """
    if limit is None:
        limit = detail_max_bytes()
    if limit <= 0:
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:
            return "{}"
    per_value = max(64, limit // 2)
    try:
        body = json.dumps(_clip_value(obj, per_value), ensure_ascii=False, default=str)
    except Exception:
        body = "{}"
    raw = body.encode("utf-8")
    if len(raw) <= limit:
        return body
    head = _clip_text(body, max(64, limit // 4))
    out = {
        "truncated": True,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()[:32],
        "preview": head,
    }
    text = json.dumps(out, ensure_ascii=False)
    while len(text.encode("utf-8")) > limit and out["preview"]:
        out["preview"] = out["preview"][: len(out["preview"]) // 2]
        text = json.dumps(out, ensure_ascii=False)
    return text


# --- tool 入参 → 前端可渲染结构（§27）--------------------------------------
# 截图要的三样：Edit 的红绿 diff、Bash 的命令块、Read/Write 的文件直链。
# 三样在 tool_use 的入参里本来就都有，缺的只是**采集**和**规整**。
FILE_ARG_KEYS = ("file_path", "path", "notebook_path", "filePath", "abs_path", "target_file")
EDIT_OLD_KEYS = ("old_string", "old_str", "oldText")
EDIT_NEW_KEYS = ("new_string", "new_str", "newText")
WRITE_TEXT_KEYS = ("content", "contents", "file_text", "text")


def _parse_tool_json(raw):
    """拼回 tool_use 的流式入参。content_block_start 时 input 还是 {}，
    真值在 content_block_delta 的 partial_json 里逐片到达 —— 不拼就是空入参。"""
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except Exception:
        return {"_raw": _clip_text(raw, 2048)}
    return got if isinstance(got, dict) else {"_value": got}


def tool_detail(name, raw, output=None, budget=None):
    """把 tool 入参规整成前端可直接渲染的结构，**每个字段单独截断**。

    为什么不是整条交给 `fit_detail`：8KB 是整条 detail 的上限，一个稍大的 diff
    就能把结构挤成 `{truncated, preview}` 字符串，前端从此拿不到 old/new 的边界，
    红绿渲染就没了。所以这里先把每个字段压到 1/3 预算，保住结构；全文靠
    `GET /api/file` 直链读盘（§6 的截断上限不因此作废）。
    """
    budget = budget or max(512, detail_max_bytes() // 3)
    raw = raw if isinstance(raw, dict) else {}
    d = {"tool": (name or "").strip()}
    clipped = False

    def put(key, value):
        nonlocal clipped
        if not isinstance(value, str) or not value:
            return
        got = _clip_text(value, budget)
        if got != value:
            clipped = True
        d[key] = got

    for key in FILE_ARG_KEYS:
        if isinstance(raw.get(key), str) and raw[key]:
            put("path", raw[key])
            break
    cmd = raw.get("command")
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(part) for part in cmd)
    if isinstance(cmd, str) and cmd.strip():
        put("command", cmd)
    for key in EDIT_OLD_KEYS:
        if isinstance(raw.get(key), str):
            put("old_text", raw[key])
            break
    for key in EDIT_NEW_KEYS:
        if isinstance(raw.get(key), str):
            put("new_text", raw[key])
            break
    if "new_text" not in d:                      # Write 类：整文件内容当新增
        for key in WRITE_TEXT_KEYS:
            if isinstance(raw.get(key), str):
                put("new_text", raw[key])
                break
    for key in ("pattern", "query", "description"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            put(key, raw[key])
            break
    # codex 的 fileChange 给的是**统一 diff**（带 @@ 段号），比 old/new 更好渲染：
    # 直接按行首 +/-/空格 上色就是截图里的样子，不需要前端再算 diff。
    changes = raw.get("changes")
    if isinstance(changes, list) and changes and isinstance(changes[0], dict):
        first = changes[0]
        if "path" not in d:
            put("path", first.get("path"))
        put("diff_text", first.get("diff"))
        if len(changes) > 1:
            d["files"] = len(changes)
    put("output", output)
    if clipped:
        d["clipped"] = True
    if len(d) <= 1:                              # 只有工具名 = 没有可渲染的东西
        return None                              # 返回 None，别把已有 detail 覆盖成空壳
    return d


def est_tokens(text):
    """粗略 token 估算：CJK 按 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token。

    只用来说明「预算花到哪了」，不用于计费 —— 计费仍以 provider 回的 usage 为准
    （见 turns.usage_source）。真正的 tokenizer 要按模型区分，本地零依赖场景不划算。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff":
            cjk += 1
    return int(cjk + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------- store
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        # agent -> 最近一次 open_turn 的 turn_id（§4.3 权限 step 挂靠用）。
        # 只在 open_turn 里写，不在 _run_agent 的 finally 里清 ——
        # 权限申请是在 finally 之后才落库的，靠 running[agent] 永远拿不到。
        self.last_turn = {}
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL, sender TEXT, target TEXT, kind TEXT,
                   text TEXT, ref TEXT, client_msg_id TEXT)"""
        )
        # 原生记忆按会话隔离：一个会话一份（见 _migrate 里的重建）
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                   agent TEXT NOT NULL, conv_id INTEGER NOT NULL DEFAULT 1,
                   session_id TEXT, updated_at REAL, PRIMARY KEY(agent, conv_id))"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS models(
                   id TEXT PRIMARY KEY, label TEXT, style TEXT, base_url TEXT,
                   api_key TEXT, model TEXT, system_prompt TEXT, path TEXT,
                   max_tokens INTEGER DEFAULT 1024, enabled INTEGER DEFAULT 1,
                   created_at REAL)"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS api_history(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   model_id TEXT, role TEXT, content TEXT, ts REAL)"""
        )
        self.con.execute("CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT)")
        self.con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_ref ON messages(ref) WHERE ref IS NOT NULL"
        )
        self.con.commit()
        self._migrate()

    def _migrate(self):
        # sessions: 老结构是 PRIMARY KEY(agent) —— 所有会话共用一个原生 CLI 会话，
        # 于是「切会话」切不动记忆、「新建会话」还会把别的会话的记忆一起清掉。
        # 改成 PRIMARY KEY(agent, conv_id)。判据只能看 PRAGMA，不能靠"调用方知道这是新库"：
        # 新库由上面那条 CREATE 直接建对，老库走这里的重建。
        scols = {r[1] for r in self.con.execute("PRAGMA table_info(sessions)")}
        if "conv_id" not in scols:
            # 旧行是"当时正在用的那个会话"的 id，所以挂到迁移那一刻的 active_conv，
            # 而不是硬编码 1（conv 1 完全可能是个空的、甚至已被删掉的会话）。
            cid = self.active_conv()
            self.con.execute("ALTER TABLE sessions RENAME TO sessions_old")
            self.con.execute(
                """CREATE TABLE sessions(
                       agent TEXT NOT NULL, conv_id INTEGER NOT NULL DEFAULT 1,
                       session_id TEXT, updated_at REAL, PRIMARY KEY(agent, conv_id))"""
            )
            self.con.execute(
                """INSERT OR REPLACE INTO sessions(agent, conv_id, session_id, updated_at)
                   SELECT agent, ?, session_id, updated_at FROM sessions_old""",
                (cid,),
            )
            self.con.execute("DROP TABLE sessions_old")
            self.con.commit()
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(models)")}
        if "path" not in cols:
            self.con.execute("ALTER TABLE models ADD COLUMN path TEXT")
            self.con.commit()
        mcols = {r[1] for r in self.con.execute("PRAGMA table_info(messages)")}
        if "display_text" not in mcols:
            self.con.execute("ALTER TABLE messages ADD COLUMN display_text TEXT")
        if "retry_agent" not in mcols:
            self.con.execute("ALTER TABLE messages ADD COLUMN retry_agent TEXT")
        if "retry_prompt" not in mcols:
            self.con.execute("ALTER TABLE messages ADD COLUMN retry_prompt TEXT")
        if "highlight" not in mcols:
            self.con.execute("ALTER TABLE messages ADD COLUMN highlight INTEGER DEFAULT 0")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS nexus_agents(
                   id TEXT PRIMARY KEY, role TEXT, first_seen REAL, last_seen REAL)"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS highlights(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   message_id INTEGER, start INTEGER, end INTEGER, text TEXT, ts REAL)"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS grants(
                   agent TEXT, path TEXT, mode TEXT, ts REAL,
                   PRIMARY KEY(agent, path))"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS perm_requests(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   agent TEXT, path TEXT, mode TEXT, reason TEXT,
                   status TEXT, ts REAL, resolved_ts REAL)"""
        )
        # 已处理过的外部引用（ClaudeLink 镜像），清空消息后仍保留，避免旧消息被重新贴一遍
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS seen_refs(ref TEXT PRIMARY KEY, ts REAL)"""
        )
        # §4.3：权限申请要挂在「提出它的那个 turn」上。perm_request 是在 _run_agent 的
        # finally 之后才落库的，实时状态那时已失效，只能靠落库时写死的 turn_id 回查。
        pcols = {r[1] for r in self.con.execute("PRAGMA table_info(perm_requests)")}
        if "turn_id" not in pcols:
            self.con.execute("ALTER TABLE perm_requests ADD COLUMN turn_id TEXT")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS attachments(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT, rel TEXT, path TEXT, kind TEXT, size INTEGER,
                   text TEXT, ts REAL, message_id INTEGER)"""
        )
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_hl_msg ON highlights(message_id)")
        # -- 会话（每个会话一个独立上下文 / 独立参与成员 / 独立标题）----------
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS conversations(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   title TEXT, created_ts REAL, updated_ts REAL,
                   pinned INTEGER DEFAULT 0, members TEXT)"""
        )
        if "conv_id" not in mcols:
            # 老库升级：已有消息全部归到 1 号会话
            self.con.execute("ALTER TABLE messages ADD COLUMN conv_id INTEGER DEFAULT 1")
        if "client_msg_id" not in mcols:
            self.con.execute("ALTER TABLE messages ADD COLUMN client_msg_id TEXT")
        # 任务关联（ORC-01）：老行留空即可，历史消息本来就没有任务语义
        for col, decl in (("task_id", "INTEGER"), ("reply_to", "INTEGER"),
                          ("expects_reply", "INTEGER DEFAULT 0")):
            if col not in mcols:
                self.con.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
        self.con.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client
                   ON messages(conv_id, client_msg_id) WHERE client_msg_id IS NOT NULL"""
        )
        have = self.con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        if not have:
            first = self.con.execute(
                "SELECT text FROM messages WHERE kind='user' ORDER BY id LIMIT 1").fetchone()
            title = ((first[0] or "").strip().replace("\n", " ")[:24] if first else "") or "默认会话"
            self.con.execute(
                """INSERT INTO conversations(id,title,created_ts,updated_ts,pinned,members)
                   VALUES(1,?,?,?,0,'[]')""",
                (title, time.time(), time.time()),
            )
        self.con.commit()
        # -- trace：turns / steps（§3，单位 UTC 毫秒）-------------------------
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS turns(
                   id             TEXT PRIMARY KEY,
                   trace_id       TEXT NOT NULL,
                   parent_turn_id TEXT,
                   agent          TEXT NOT NULL,
                   depth          INTEGER NOT NULL DEFAULT 0,
                   trigger_kind   TEXT NOT NULL,
                   trigger_msg_id INTEGER,
                   trigger_by     TEXT,
                   reply_msg_id   INTEGER,
                   model          TEXT,
                   session_id     TEXT,
                   start_ts       INTEGER NOT NULL,
                   end_ts         INTEGER,
                   status         TEXT NOT NULL,
                   error_text     TEXT,
                   in_tokens      INTEGER NOT NULL DEFAULT 0,
                   out_tokens     INTEGER NOT NULL DEFAULT 0,
                   cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
                   cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                   usage_source   TEXT NOT NULL DEFAULT 'estimated',
                   in_chars       INTEGER NOT NULL DEFAULT 0,
                   out_chars      INTEGER NOT NULL DEFAULT 0)"""
        )
        tcols = {r[1] for r in self.con.execute("PRAGMA table_info(turns)")}
        if "conv_id" not in tcols:
            # 会话隔离：老轮次归到 1 号会话（必须在建表之后做）
            self.con.execute("ALTER TABLE turns ADD COLUMN conv_id INTEGER DEFAULT 1")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS steps(
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   turn_id    TEXT NOT NULL,
                   seq        INTEGER NOT NULL,
                   kind       TEXT NOT NULL,
                   name       TEXT,
                   status     TEXT NOT NULL,
                   start_ts   INTEGER NOT NULL,
                   end_ts     INTEGER,
                   in_tokens  INTEGER,
                   out_tokens INTEGER,
                   cache_read_tokens  INTEGER,
                   cache_write_tokens INTEGER,
                   detail     TEXT)"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS prompt_cache(
                   agent TEXT NOT NULL, conv_id INTEGER NOT NULL,
                   prefix TEXT, anchor_msg_id INTEGER, head_hash TEXT,
                   prefix_chars INTEGER, updated_at REAL,
                   PRIMARY KEY(agent, conv_id))"""
        )
        # 缓存命中（4 桶口径）：turns 的列 NOT NULL DEFAULT 0、steps 的列可空，
        # 与它们各自的 in_tokens/out_tokens 保持一致。steps **原先没有任何迁移块**，
        # 这里第一次补上——漏了它老库就会 ALTER 失败、整个中枢起不来。
        tcols2 = {r[1] for r in self.con.execute("PRAGMA table_info(turns)")}
        for col, decl in (("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
                          ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0")):
            if col not in tcols2:
                self.con.execute(f"ALTER TABLE turns ADD COLUMN {col} {decl}")
        scols2 = {r[1] for r in self.con.execute("PRAGMA table_info(steps)")}
        for col in ("cache_read_tokens", "cache_write_tokens"):
            if col not in scols2:
                self.con.execute(f"ALTER TABLE steps ADD COLUMN {col} INTEGER")
        self.con.commit()
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_turns_trace  ON turns(trace_id, start_ts)")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent  ON turns(agent, start_ts DESC)")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_turns_parent ON turns(parent_turn_id)")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_steps_turn   ON steps(turn_id, seq)")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_open ON turns(status) WHERE status='running'"
        )
        self.con.commit()
        # -- 排队与上下文压缩账本（ORC-02 / CTX-09）--------------------------
        # 队列是**持久**的：入队先落库、再唤醒调度器；进程重启后不会凭空多跑一轮，
        # 遗留的 queued/running 一律在启动时显式判死（见 queue_recover）。
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS turn_queue(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   agent TEXT NOT NULL,
                   conv_id INTEGER NOT NULL DEFAULT 1,
                   kind TEXT NOT NULL DEFAULT 'turn',
                   payload TEXT,
                   status TEXT NOT NULL DEFAULT 'queued',
                   priority INTEGER NOT NULL DEFAULT 0,
                   created_ts REAL, started_ts REAL, ended_ts REAL,
                   cancel_req INTEGER NOT NULL DEFAULT 0,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   error TEXT)"""
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_queue_open ON turn_queue(status, priority DESC, id)"
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS compactions(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   conv_id INTEGER NOT NULL,
                   agent TEXT NOT NULL,
                   ts REAL,
                   trigger TEXT,
                   chars_before INTEGER, chars_after INTEGER,
                   tokens_before INTEGER, tokens_after INTEGER,
                   dropped_ids TEXT, digest TEXT)"""
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_compactions ON compactions(conv_id, ts DESC)"
        )
        # -- 任务一等公民（ORC-01）：字段取 A2A 子集，将来接网关不用翻译 -------
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS tasks(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   conv_id INTEGER NOT NULL DEFAULT 1,
                   title TEXT,
                   owner TEXT,
                   status TEXT NOT NULL DEFAULT 'submitted',
                   priority INTEGER NOT NULL DEFAULT 0,
                   parent_task_id INTEGER,
                   acceptance TEXT,
                   created_by TEXT,
                   created_ts REAL, updated_ts REAL, closed_ts REAL,
                   closure_reason TEXT,
                   message_id INTEGER,
                   reply_msg_id INTEGER,
                   depth INTEGER NOT NULL DEFAULT 0)"""
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(conv_id, status, id)"
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS artifacts(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   task_id INTEGER, message_id INTEGER,
                   kind TEXT, path TEXT, hash TEXT, bytes INTEGER, ts REAL)"""
        )
        # -- OTel 用量（OBS-07）：agent 直出的实测 token，本地落一份 ----------
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS otel_usage(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL, agent TEXT, model TEXT, session_id TEXT,
                   in_tokens INTEGER, out_tokens INTEGER,
                   cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                   span_id TEXT)"""
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_otel_ts ON otel_usage(ts DESC)")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)")
        self.con.commit()
        # 口径版本标记：`in_tokens` 的含义从「provider 原样送来的那个字段」改成
        # 「**未命中**的输入」（与 deepseek harness 的四桶一致），另加两个 cache 列。
        # 老行仍是旧口径、**不迁移** —— 本地库里强行换算只会把没有证据的数字编出来。
        # 这个键就是那条分界线：见 README「缓存命中」与 docs/trace-spec.md。
        if not self.state_get("usage_convention"):
            self.state_set("usage_convention", USAGE_CONVENTION)
        # 分界时刻：老行的 in_tokens 是旧口径（可能含命中）且缓存两桶恒 0，把它们和被
        # 正确记账的行一起求和，命中率会被历史量淹掉 —— 长会话上实测过 1.85 亿 token
        # 的旧数据把命中率压到 0.04%。所有归集都从这一刻起算
        # （见 conv_usage_totals / list_conversations）。
        # 两个键分开判：中间版本的库只有 convention、没有 since，那种库的分界就是"现在"。
        if not self.state_get("usage_convention_since"):
            self.state_set("usage_convention_since", str(now_ms()))

    # -- messages -----------------------------------------------------------
    # -- 队列（ORC-02）：持久化、单飞、可取消 -------------------------------
    # 落库的原因不是「怕丢」，而是「重启后要能说清楚」：进程被杀时还挂着 queued 的
    # 任务，重启后要么补跑要么判死，绝不能静默消失 —— 否则用户看到的是「发了没反应」。
    def queue_add(self, agent, conv_id, payload, priority=0, kind="turn"):
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO turn_queue(agent, conv_id, kind, payload, status, priority,"
                " created_ts) VALUES(?,?,?,?,'queued',?,?)",
                (agent, int(conv_id), kind, json.dumps(payload, ensure_ascii=False),
                 int(priority), time.time()),
            )
            self.con.commit()
            return int(cur.lastrowid)

    def queue_claim(self, busy_agents=(), now=None):
        """认领一条可执行的任务：单条 UPDATE…RETURNING，避免「先查后改」的竞态。

        只挑 agent 当前不忙的行 —— 单飞语义（同一个成员同时只跑一轮）从这里强制，
        而不是靠线程抢锁阻塞（那会白占线程）。
        """
        busy = [a for a in busy_agents if a]
        where_busy = ""
        params = []
        if busy:
            where_busy = " AND agent NOT IN (%s)" % ",".join("?" * len(busy))
            params = list(busy)
        with self.lock:
            row = self.con.execute(
                "UPDATE turn_queue SET status='running', started_ts=?, attempts=attempts+1"
                " WHERE id = (SELECT id FROM turn_queue WHERE status='queued'"
                "              AND cancel_req=0" + where_busy +
                "              ORDER BY priority DESC, id ASC LIMIT 1)"
                " RETURNING id, agent, conv_id, kind, payload, attempts",
                [now or time.time()] + params,
            ).fetchone()
            self.con.commit()
        if not row:
            return None
        try:
            payload = json.loads(row[4] or "{}")
        except Exception:
            payload = {}
        return {"id": row[0], "agent": row[1], "conv_id": row[2], "kind": row[3],
                "payload": payload, "attempts": row[5]}

    def queue_finish(self, qid, status="done", error=None):
        with self.lock:
            self.con.execute(
                "UPDATE turn_queue SET status=?, ended_ts=?, error=? WHERE id=?",
                (status, time.time(), (error or "")[:500] or None, int(qid)),
            )
            self.con.commit()

    def queue_cancel(self, qid):
        """取消：排队中的直接作废；运行中的打标记，由执行器自己收尾。

        返回 'cancelled'（排队中）/ 'requested'（运行中，已要求中断）/ None（没这条）。
        """
        with self.lock:
            row = self.con.execute(
                "SELECT status FROM turn_queue WHERE id=?", (int(qid),)).fetchone()
            if not row:
                return None
            if row[0] == "queued":
                self.con.execute(
                    "UPDATE turn_queue SET status='cancelled', cancel_req=1, ended_ts=?"
                    " WHERE id=?", (time.time(), int(qid)))
                self.con.commit()
                return "cancelled"
            if row[0] == "running":
                self.con.execute(
                    "UPDATE turn_queue SET cancel_req=1 WHERE id=?", (int(qid),))
                self.con.commit()
                return "requested"
            return None

    def queue_open(self, conv_id=None, limit=50):
        """还没结束的任务（queued/running），按优先级与入队顺序。"""
        sql = ("SELECT id, agent, conv_id, kind, status, priority, created_ts,"
               " started_ts, attempts, error, payload FROM turn_queue"
               " WHERE status IN ('queued','running')")
        params = []
        if conv_id:
            sql += " AND conv_id=?"
            params.append(int(conv_id))
        sql += " ORDER BY priority DESC, id ASC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            rows = self.con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            body = ""
            try:
                body = (json.loads(r[10] or "{}").get("text") or "")[:80]
            except Exception:
                pass
            out.append({"id": r[0], "agent": r[1], "conv_id": r[2], "kind": r[3],
                        "status": r[4], "priority": r[5], "created_ts": r[6],
                        "started_ts": r[7], "attempts": r[8], "error": r[9],
                        "preview": body})
        return out

    def queue_depth(self, conv_id=None):
        with self.lock:
            if conv_id:
                row = self.con.execute(
                    "SELECT SUM(status='queued'), SUM(status='running') FROM turn_queue"
                    " WHERE conv_id=? AND status IN ('queued','running')",
                    (int(conv_id),)).fetchone()
            else:
                row = self.con.execute(
                    "SELECT SUM(status='queued'), SUM(status='running') FROM turn_queue"
                    " WHERE status IN ('queued','running')").fetchone()
        return {"queued": int(row[0] or 0), "running": int(row[1] or 0)}

    def queue_clear(self, reason="被停止键取消"):
        with self.lock:
            n = self.con.execute(
                "UPDATE turn_queue SET status='cancelled', ended_ts=?, error=?,"
                " cancel_req=1 WHERE status='queued'",
                (time.time(), reason)).rowcount
            self.con.commit()
        return int(n or 0)

    def queue_recover(self):
        """启动对账：上一进程留下的 queued/running 一律判死并留下原因。

        不自动补跑是刻意的 —— 补跑会重新调用模型、重新发消息，用户可能已经手动重发
        过一遍，自动补跑就等于把同一句话回答两遍。宁可显式报错让人决定。
        """
        with self.lock:
            n = self.con.execute(
                "UPDATE turn_queue SET status='failed', ended_ts=?,"
                " error='进程重启，任务未能完成'"
                " WHERE status IN ('queued','running')", (time.time(),)).rowcount
            self.con.commit()
        return int(n or 0)

    # -- 压缩账本（CTX-09）--------------------------------------------------
    def compaction_add(self, conv_id, agent, trigger, chars_before, chars_after,
                       tokens_before, tokens_after, dropped_ids, digest):
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO compactions(conv_id, agent, ts, trigger, chars_before,"
                " chars_after, tokens_before, tokens_after, dropped_ids, digest)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(conv_id), agent, time.time(), trigger, int(chars_before),
                 int(chars_after), int(tokens_before), int(tokens_after),
                 json.dumps(dropped_ids, ensure_ascii=False), digest))
            self.con.commit()
            return int(cur.lastrowid)

    def compaction_list(self, conv_id=None, limit=20):
        sql = "SELECT id, conv_id, agent, ts, trigger, chars_before, chars_after,"               " tokens_before, tokens_after, dropped_ids, digest FROM compactions"
        params = []
        if conv_id:
            sql += " WHERE conv_id=?"
            params.append(int(conv_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            rows = self.con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "conv_id": r[1], "agent": r[2], "ts": r[3],
                        "trigger": r[4], "chars_before": r[5], "chars_after": r[6],
                        "tokens_before": r[7], "tokens_after": r[8],
                        "dropped_ids": r[9], "digest": r[10]})
        return out

    # -- OTel 用量（OBS-07）-------------------------------------------------
    # 只收 OTLP/HTTP **JSON**（exporter 端 OTEL_EXPORTER_OTLP_PROTOCOL=http/json）。
    # 不引 protobuf 依赖 —— 零依赖是这个项目的分发前提；JSON 口各家 exporter 都支持。
    _OTEL_IN_KEYS = ("gen_ai.usage.input_tokens", "input_tokens",
                     "gen_ai.usage.prompt_tokens")
    _OTEL_OUT_KEYS = ("gen_ai.usage.output_tokens", "output_tokens",
                      "gen_ai.usage.completion_tokens")
    _OTEL_CACHE_READ = ("gen_ai.usage.cache_read_input_tokens",
                        "cache_read_input_tokens")
    _OTEL_CACHE_WRITE = ("gen_ai.usage.cache_write_input_tokens",
                         "cache_creation_input_tokens", "cache_write_input_tokens")

    @staticmethod
    def _otel_attr_map(attrs):
        out = {}
        for a in attrs or []:
            key = a.get("key") or ""
            val = a.get("value") or {}
            for field in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if field in val:
                    out[key] = val[field]
                    break
            else:
                if "arrayValue" in val or "kvlistValue" in val:
                    out[key] = json.dumps(val, ensure_ascii=False)[:200]
        return out

    @staticmethod
    def _otel_pick(mapping, keys):
        for k in keys:
            v = mapping.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return None

    def otel_ingest(self, payload):
        """吃一份 OTLP/JSON export 请求，抽出带 gen_ai 用量的 span 存表。返回条数。"""
        rows = []
        now = time.time()
        for rs in (payload or {}).get("resourceSpans") or []:
            for scope in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
                for span in scope.get("spans") or []:
                    m = self._otel_attr_map(span.get("attributes"))
                    has_usage = (self._otel_pick(m, self._OTEL_IN_KEYS) is not None
                                 or self._otel_pick(m, self._OTEL_OUT_KEYS) is not None)
                    if not has_usage:
                        continue
                    rows.append((
                        now,
                        str(m.get("agent.name") or m.get("service.name") or "")[:80],
                        str(m.get("gen_ai.request.model") or "")[:120],
                        str(m.get("session.id") or m.get("gen_ai.conversation.id") or "")[:120],
                        self._otel_pick(m, self._OTEL_IN_KEYS),
                        self._otel_pick(m, self._OTEL_OUT_KEYS),
                        self._otel_pick(m, self._OTEL_CACHE_READ),
                        self._otel_pick(m, self._OTEL_CACHE_WRITE),
                        str(span.get("spanId") or "")[:32],
                    ))
        if rows:
            with self.lock:
                self.con.executemany(
                    "INSERT INTO otel_usage(ts, agent, model, session_id, in_tokens,"
                    " out_tokens, cache_read_tokens, cache_write_tokens, span_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", rows)
                self.con.commit()
        return len(rows)

    def otel_totals(self, since_hours=24):
        edge = time.time() - float(since_hours or 0) * 3600
        with self.lock:
            rows = self.con.execute(
                "SELECT COALESCE(NULLIF(agent,''),'(未知)'),"
                " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0),"
                " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),"
                " COUNT(*) FROM otel_usage WHERE ts >= ? GROUP BY 1", (edge,)
            ).fetchall()
        return [{"agent": r[0], "in_tokens": int(r[1]), "out_tokens": int(r[2]),
                 "cache_read_tokens": int(r[3]), "cache_write_tokens": int(r[4]),
                 "spans": int(r[5])} for r in rows]

    def inbox(self, agent, limit=50):
        """某个成员视角的收件箱：发给它的、发给全体的，以及它自己发出的。"""
        conv = self.active_conv()
        with self.lock:
            rows = self.con.execute(
                """SELECT id, ts, sender, target, kind, text FROM messages
                    WHERE conv_id=? AND (target=? OR target='all' OR sender=?)
                    ORDER BY id DESC LIMIT ?""",
                (conv, agent, agent, max(1, min(200, int(limit)))),
            ).fetchall()
        return [{"id": r[0], "ts": r[1], "sender": r[2], "target": r[3],
                 "kind": r[4], "text": (r[5] or "")[:4000]} for r in rows]

    # -- 任务（ORC-01）------------------------------------------------------
    # 状态取 A2A 子集：submitted / working / input-required / completed / failed /
    # cancelled。完成必须带 closure_reason —— 没有理由的「完成」无法审计。
    TASK_OPEN = ("submitted", "working", "input-required")
    TASK_CLOSED = ("completed", "failed", "cancelled")

    @staticmethod
    def _task_row(r):
        return {"id": r[0], "conv_id": r[1], "title": r[2], "owner": r[3],
                "status": r[4], "priority": r[5], "parent_task_id": r[6],
                "acceptance": r[7], "created_by": r[8], "created_ts": r[9],
                "updated_ts": r[10], "closed_ts": r[11], "closure_reason": r[12],
                "message_id": r[13], "reply_msg_id": r[14], "depth": r[15]}

    def task_create(self, conv_id, owner, title, created_by=None, parent_task_id=None,
                    message_id=None, acceptance=None, priority=0, depth=0):
        now = time.time()
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO tasks(conv_id, title, owner, status, priority,"
                " parent_task_id, acceptance, created_by, created_ts, updated_ts,"
                " message_id, depth) VALUES(?,?,?,'submitted',?,?,?,?,?,?,?,?)",
                (int(conv_id), (title or "")[:200], owner, int(priority),
                 parent_task_id, acceptance, created_by, now, now, message_id,
                 int(depth)),
            )
            self.con.commit()
            tid = int(cur.lastrowid)
        return self.task_get(tid)

    def task_get(self, tid):
        with self.lock:
            r = self.con.execute(
                "SELECT id, conv_id, title, owner, status, priority, parent_task_id,"
                " acceptance, created_by, created_ts, updated_ts, closed_ts,"
                " closure_reason, message_id, reply_msg_id, depth"
                " FROM tasks WHERE id=?", (int(tid),)).fetchone()
        return self._task_row(r) if r else None

    def task_update(self, tid, **fields):
        allowed = ("title", "owner", "status", "priority", "acceptance",
                   "closure_reason", "reply_msg_id", "closed_ts")
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(key + "=?")
                params.append(value)
        if not sets:
            return self.task_get(tid)
        sets.append("updated_ts=?")
        params.extend([time.time(), int(tid)])
        with self.lock:
            self.con.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", params)
            self.con.commit()
        return self.task_get(tid)

    def task_close(self, tid, status="completed", reason=None):
        if status not in self.TASK_CLOSED:
            raise ValueError(f"非法任务终态：{status}")
        return self.task_update(tid, status=status, closure_reason=reason or status,
                                closed_ts=time.time())

    def task_list(self, conv_id=None, status=None, limit=100):
        sql = ("SELECT id, conv_id, title, owner, status, priority, parent_task_id,"
               " acceptance, created_by, created_ts, updated_ts, closed_ts,"
               " closure_reason, message_id, reply_msg_id, depth FROM tasks")
        where, params = [], []
        if conv_id:
            where.append("conv_id=?")
            params.append(int(conv_id))
        if status == "open":
            where.append("status IN ('submitted','working','input-required')")
        elif status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            rows = self.con.execute(sql, params).fetchall()
        return [self._task_row(r) for r in rows]

    def link_message_task(self, msg_id, task_id=None, reply_to=None,
                          expects_reply=None):
        """把消息挂到任务上（消息先落库、再补关联，避免改动 add_message 的插入语句）。"""
        sets, params = [], []
        if task_id is not None:
            sets.append("task_id=?")
            params.append(int(task_id))
        if reply_to is not None:
            sets.append("reply_to=?")
            params.append(int(reply_to))
        if expects_reply is not None:
            sets.append("expects_reply=?")
            params.append(1 if expects_reply else 0)
        if not sets:
            return
        params.append(int(msg_id))
        with self.lock:
            self.con.execute("UPDATE messages SET " + ",".join(sets) + " WHERE id=?",
                             params)
            self.con.commit()

    def runs(self, conv_id=None, limit=50):
        """运行表（UX-06）：一行一次轮次，左连它产生的任务，缺任务的老轮次也能显示。"""
        sql = ("SELECT t.id, t.agent, t.status, t.start_ts, t.end_ts, t.in_tokens,"
               " t.out_tokens, t.trigger_kind, t.depth, t.error_text, t.conv_id,"
               " k.id, k.status, k.title"
               " FROM turns t LEFT JOIN tasks k ON k.reply_msg_id = t.reply_msg_id")
        params = []
        if conv_id:
            sql += " WHERE t.conv_id=?"
            params.append(int(conv_id))
        sql += " ORDER BY t.start_ts DESC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            rows = self.con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            cost = (int(r[5] or 0) + int(r[6] or 0))
            out.append({"turn_id": r[0], "agent": r[1], "status": r[2],
                        "start_ts": r[3], "end_ts": r[4],
                        "in_tokens": int(r[5] or 0), "out_tokens": int(r[6] or 0),
                        "tokens": cost, "trigger_kind": r[7], "depth": r[8],
                        "error": r[9], "conv_id": r[10],
                        "task_id": r[11], "task_status": r[12], "task_title": r[13]})
        return out

    def artifact_add(self, task_id, kind, path="", digest="", size=0, message_id=None):
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO artifacts(task_id, message_id, kind, path, hash, bytes, ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (int(task_id), message_id, kind, path, digest, int(size or 0),
                 time.time()))
            self.con.commit()
            return int(cur.lastrowid)

    def artifact_list(self, task_id):
        with self.lock:
            rows = self.con.execute(
                "SELECT id, task_id, message_id, kind, path, hash, bytes, ts"
                " FROM artifacts WHERE task_id=? ORDER BY id", (int(task_id),)
            ).fetchall()
        return [{"id": r[0], "task_id": r[1], "message_id": r[2], "kind": r[3],
                 "path": r[4], "hash": r[5], "bytes": r[6], "ts": r[7]} for r in rows]

    # -- 会话 ---------------------------------------------------------------
    def active_conv(self):
        try:
            return int(self.state_get("active_conv", "1") or 1)
        except (TypeError, ValueError):
            return 1

    def set_active_conv(self, conv_id):
        self.state_set("active_conv", str(int(conv_id)))

    def list_conversations(self):
        # 口径分界：老行的 in_tokens 是旧口径、缓存桶恒 0，混进来会把命中率淹掉
        since = self.usage_convention_since()
        with self.lock:
            rows = self.con.execute(
                """SELECT c.id, c.title, c.created_ts, c.updated_ts, c.pinned, c.members,
                          (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS n,
                          (SELECT COALESCE(SUM(t.in_tokens),0) FROM turns t
                            WHERE t.conv_id=c.id AND t.start_ts >= ?),
                          (SELECT COALESCE(SUM(t.cache_read_tokens),0) FROM turns t
                            WHERE t.conv_id=c.id AND t.start_ts >= ?),
                          (SELECT COALESCE(SUM(t.cache_write_tokens),0) FROM turns t
                            WHERE t.conv_id=c.id AND t.start_ts >= ?)
                     FROM conversations c
                    ORDER BY c.pinned DESC, COALESCE(c.updated_ts, c.created_ts) DESC""",
                (since, since, since),
            ).fetchall()
        out = []
        for r in rows:
            try:
                members = json.loads(r[5] or "[]")
            except Exception:
                members = []
            # 会话级的缓存命中率：只对**已结算**的 turn 求和（turns.in_tokens 在运行中是 0）。
            # 分母用三个输入桶之和 —— 这正是归一化成互不重叠桶的意义。
            uncached, c_read, c_write = int(r[7]), int(r[8]), int(r[9])
            prompt_tokens = uncached + c_read + c_write
            out.append({"id": r[0], "title": r[1] or f"会话 {r[0]}", "created_ts": r[2],
                        "updated_ts": r[3], "pinned": bool(r[4]), "members": members,
                        "messages": r[6],
                        "cache_read_tokens": c_read, "cache_write_tokens": c_write,
                        "uncached_tokens": uncached, "prompt_tokens": prompt_tokens,
                        "cache_hit_rate": self.cache_observation(r[0])["cache_hit_rate"]})
        return out

    def cache_observation(self, conv_id):
        """Cache rates only cover calls where the provider actually reported cache use."""
        since = self.usage_convention_since()
        with self.lock:
            row = self.con.execute("""SELECT COUNT(*),
                SUM(CASE WHEN s.cache_read_tokens IS NOT NULL AND s.in_tokens IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN s.cache_read_tokens IS NOT NULL AND s.in_tokens IS NOT NULL THEN s.cache_read_tokens ELSE 0 END),
                SUM(CASE WHEN s.cache_read_tokens IS NOT NULL AND s.in_tokens IS NOT NULL THEN s.in_tokens+s.cache_read_tokens+COALESCE(s.cache_write_tokens,0) ELSE 0 END)
                FROM steps s JOIN turns t ON t.id=s.turn_id
                WHERE t.conv_id=? AND t.end_ts IS NOT NULL AND t.start_ts>=? AND s.kind='llm_call'""",
                (int(conv_id), since)).fetchone()
        total, observed, hit, prompt = (int(v or 0) for v in row)
        return {"cache_hit_rate": hit / prompt if prompt else None,
                "cache_observed_calls": observed, "cache_unreported_calls": total - observed,
                "cache_usage_source": "provider" if observed else "unreported"}

    def conv_usage_totals(self, conv_id):
        """某个会话的 (未命中输入, 命中读, 命中写) 三个桶之和。给运行视图/底栏用。

        只看 `turns` 的汇总列 —— 它们在 turn 结算时由 rollup 写全，所以运行中的 turn
        还没算进来；这正是想要的（运行中的 turn 显示命中率会是个假数字）。

        并且只统计口径分界**之后**的轮次：老行的 in_tokens 是旧口径、缓存两桶恒 0，
        混进来会把命中率淹掉（见 _migrate 里 usage_convention_since 的注释）。
        """
        return self.usage_totals_since(conv_id, self.usage_convention_since())

    def usage_convention_since(self):
        """口径分界时刻（UTC 毫秒）。没有就返回 0（= 不做过滤）。"""
        try:
            return int(self.state_get("usage_convention_since") or 0)
        except (TypeError, ValueError):
            return 0

    def usage_totals_since(self, conv_id, since_ms=0):
        with self.lock:
            row = self.con.execute(
                """SELECT COALESCE(SUM(in_tokens),0),
                          COALESCE(SUM(cache_read_tokens),0),
                          COALESCE(SUM(cache_write_tokens),0)
                     FROM turns WHERE conv_id=? AND start_ts >= ?""",
                (int(conv_id), int(since_ms or 0)),
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def count_turns(self, conv_id):
        """会话已结算的轮次数（预算闸门用）。运行中的不算：它还没花完额度。"""
        with self.lock:
            row = self.con.execute(
                "SELECT COUNT(*) FROM turns WHERE conv_id=? AND status!='running'",
                (int(conv_id),)).fetchone()
        return int(row[0] or 0)

    def create_conversation(self, title=None, members=None):
        now = time.time()
        with self.lock:
            cur = self.con.execute(
                """INSERT INTO conversations(title, created_ts, updated_ts, pinned, members)
                   VALUES(?,?,?,0,?)""",
                ((title or "").strip() or time.strftime("新会话 %m-%d %H:%M"),
                 now, now, json.dumps(members or [], ensure_ascii=False)),
            )
            self.con.commit()
            conv_id = cur.lastrowid
        return self.get_conversation(conv_id)

    def get_conversation(self, conv_id):
        for c in self.list_conversations():
            if c["id"] == int(conv_id):
                return c
        return None

    def update_conversation(self, conv_id, title=None, pinned=None, members=None):
        sets, args = [], []
        if title is not None:
            sets.append("title=?")
            args.append(str(title).strip() or f"会话 {conv_id}")
        if pinned is not None:
            sets.append("pinned=?")
            args.append(1 if pinned else 0)
        if members is not None:
            sets.append("members=?")
            args.append(json.dumps(list(members), ensure_ascii=False))
        if not sets:
            return self.get_conversation(conv_id)
        args.append(int(conv_id))
        with self.lock:
            self.con.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", args)
            self.con.commit()
        return self.get_conversation(conv_id)

    def touch_conversation(self, conv_id):
        with self.lock:
            self.con.execute("UPDATE conversations SET updated_ts=? WHERE id=?",
                             (time.time(), int(conv_id)))
            self.con.commit()

    def autotitle_conversation(self, conv_id, text):
        """会话还没有正经标题时，用第一条用户消息命名（学 DeepSeek 那个列表）。"""
        with self.lock:
            row = self.con.execute("SELECT title FROM conversations WHERE id=?",
                                   (int(conv_id),)).fetchone()
            if not row:
                return
            title = row[0] or ""
            if title and not title.startswith(("新会话", "默认会话")):
                return
            new = (text or "").strip().replace("\n", " ")[:24]
            if not new:
                return
            self.con.execute("UPDATE conversations SET title=? WHERE id=?", (new, int(conv_id)))
            self.con.commit()

    def delete_conversation(self, conv_id):
        conv_id = int(conv_id)
        with self.lock:
            self.con.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            # 原生记忆也是按会话存的：不一起删就留下永远够不着的孤儿行。
            # 直接写 SQL，不能调 clear_conv_sessions()——那会再拿一次 self.lock（不可重入）自死锁。
            self.con.execute("DELETE FROM sessions WHERE conv_id=?", (conv_id,))
            # 冻结前缀也是这个会话的一份（直接写 SQL：锁不可重入，别调 Store helper）
            self.con.execute("DELETE FROM prompt_cache WHERE conv_id=?", (conv_id,))
            # trace 也一起清（本会话的轮次与步骤）
            self.con.execute(
                "DELETE FROM steps WHERE turn_id IN (SELECT id FROM turns WHERE conv_id=?)",
                (conv_id,))
            self.con.execute("DELETE FROM turns WHERE conv_id=?", (conv_id,))
            self.con.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            self.con.commit()
        return True

    def find_by_client(self, conv_id, client_msg_id):
        """按 (会话, 客户端消息 id) 找已落库消息。"""
        if not client_msg_id:
            return None
        with self.lock:
            row = self.con.execute(
                "SELECT id FROM messages WHERE conv_id=? AND client_msg_id=?",
                (int(conv_id), str(client_msg_id)),
            ).fetchone()
        return self.get(row[0]) if row else None

    def add_message(self, sender, target, kind, text, ref=None, retry_agent=None,
                    retry_prompt=None, display_text=None, conv_id=None, client_msg_id=None):
        # 必须在拿锁**之前**解析会话 id：active_conv() 自己也要拿同一把（非重入）锁
        cid = int(conv_id) if conv_id else self.active_conv()
        client_msg_id = (str(client_msg_id).strip() or None) if client_msg_id is not None else None
        existing_id = None
        row_id = None
        with self.lock:
            if client_msg_id:
                row = self.con.execute(
                    "SELECT id FROM messages WHERE conv_id=? AND client_msg_id=?",
                    (cid, str(client_msg_id)),
                ).fetchone()
                if row:
                    existing_id = row[0]
            if existing_id is None:
                cur = self.con.execute(
                    """INSERT OR IGNORE INTO messages(
                           ts,sender,target,kind,text,ref,retry_agent,retry_prompt,display_text,conv_id,client_msg_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (time.time(), sender, target, kind, text, ref, retry_agent, retry_prompt,
                     display_text, cid, client_msg_id),
                )
                self.con.commit()
                if cur.rowcount == 0:
                    if client_msg_id:
                        row = self.con.execute(
                            "SELECT id FROM messages WHERE conv_id=? AND client_msg_id=?",
                            (cid, str(client_msg_id)),
                        ).fetchone()
                        if row:
                            existing_id = row[0]
                    if existing_id is None:
                        return None
                else:
                    row_id = cur.lastrowid
        if existing_id is not None:
            msg = self.get(existing_id)
            if msg:
                msg["_created"] = False
            return msg
        msg = self.get(row_id)
        if msg:
            msg["_created"] = True
        return msg

    def get(self, row_id):
        with self.lock:
            row = self.con.execute(
                """SELECT id,ts,sender,target,kind,text,ref,retry_agent,retry_prompt,highlight,display_text,conv_id
                     FROM messages WHERE id=?""",
                (row_id,),
            ).fetchone()
        out = self._row2(row) if row else None
        if out:
            out["highlights"] = self.list_highlights(out["id"])
            out["attachments"] = [
                {"id": a["id"], "name": a["name"], "kind": a["kind"], "size": a["size"]}
                for a in self.attachments_for_message(out["id"])
            ]
        return out

    @staticmethod
    def _row(row):
        if not row:
            return None
        return {
            "id": row[0],
            "ts": row[1],
            "sender": row[2],
            "target": row[3],
            "kind": row[4],
            "text": row[5],
            "ref": row[6],
            "retry_agent": row[7],
            "retry_prompt": row[8],
            "can_retry": bool(row[7] and row[8]),
            "highlight": bool(row[9]),
        }

    @staticmethod
    def _row2(row):
        """带 display_text 的行（recent 用）。"""
        return {
            "id": row[0], "ts": row[1], "sender": row[2], "target": row[3], "kind": row[4],
            "text": row[5], "ref": row[6], "retry_agent": row[7], "retry_prompt": row[8],
            "can_retry": bool(row[7] and row[8]), "highlight": bool(row[9]),
            "display_text": row[10],
            # conv_id 是给「改写/删除/改高亮要作废哪个会话的冻结前缀」用的：
            # 少了它 msg.get("conv_id") 恒为 None，作废钩子会**静默不触发**。
            # 只有 get() 的 SELECT 带这一列，recent() 不带 —— 所以按宽度取，别硬编下标。
            "conv_id": row[11] if len(row) > 11 else None,
        }

    def recent(self, limit=300, conv_id=None):
        """limit=None 表示取全部（上下文不限条数时用）。只取当前会话的消息。

        conv_id 显式给定时用它（冻结前缀要按**本轮锁定的**会话取，而不是"此刻活动的"）。
        """
        conv_id = int(conv_id) if conv_id else self.active_conv()
        with self.lock:
            if limit is None:
                rows = self.con.execute(
                    """SELECT id,ts,sender,target,kind,text,ref,retry_agent,retry_prompt,highlight,display_text
                         FROM messages WHERE conv_id=? ORDER BY id""",
                    (conv_id,),
                ).fetchall()
                rows = list(rows)
            else:
                rows = self.con.execute(
                    """SELECT id,ts,sender,target,kind,text,ref,retry_agent,retry_prompt,highlight,display_text
                         FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?""",
                    (conv_id, limit),
                ).fetchall()
            att_rows = self.con.execute(
                """SELECT id,message_id,name,kind,size FROM attachments
                    WHERE message_id IS NOT NULL ORDER BY id"""
            ).fetchall()
        att_map = {}
        for aid, mid, name, kind, size in att_rows:
            att_map.setdefault(mid, []).append(
                {"id": aid, "name": name, "kind": kind, "size": size}
            )
        ordered = list(rows) if limit is None else list(reversed(rows))
        out = [self._row2(r) for r in ordered]
        for item in out:
            item["highlights"] = self.list_highlights(item["id"])
            item["attachments"] = att_map.get(item["id"], [])
        return out

    def clear_messages(self):
        """清掉**当前会话**的消息 + trace（§7.5）：否则消息清了、流程还留着，两边对不上。"""
        self.delete_conv_content(self.active_conv())

    def delete_conv_content(self, conv_id):
        """只清某个会话的内容（消息 + 高亮 + trace + 原生记忆），会话行本身留着。"""
        with self.lock:
            self.con.execute(
                "DELETE FROM highlights WHERE message_id IN (SELECT id FROM messages WHERE conv_id=?)",
                (conv_id,))
            self.con.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            # 原生记忆同样是这个会话的一份，内容清了它也该清（直接写 SQL，理由同 delete_conversation）
            self.con.execute("DELETE FROM sessions WHERE conv_id=?", (conv_id,))
            self.con.execute("DELETE FROM prompt_cache WHERE conv_id=?", (conv_id,))
            self.con.execute(
                "DELETE FROM steps WHERE turn_id IN (SELECT id FROM turns WHERE conv_id=?)",
                (conv_id,))
            self.con.execute("DELETE FROM turns WHERE conv_id=?", (conv_id,))
            self.con.commit()

    def clear_all_sessions(self):
        """全库清空各成员的原生记忆（所有会话）。只给明确的"全清"场景用。"""
        with self.lock:
            self.con.execute("DELETE FROM sessions")
            self.con.commit()

    def clear_all_model_history(self):
        with self.lock:
            self.con.execute("DELETE FROM api_history")
            self.con.commit()

    def mark_ref(self, ref):
        """返回 True 表示这个外部引用第一次见到（需要展示）。"""
        with self.lock:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO seen_refs(ref, ts) VALUES(?, ?)", (ref, time.time())
            )
            self.con.commit()
            return cur.rowcount > 0

    # -- trace（turns / steps，时间一律 UTC 毫秒）------------------------------
    TURN_COLS = (
        "id", "trace_id", "parent_turn_id", "agent", "depth", "trigger_kind",
        "trigger_msg_id", "trigger_by", "reply_msg_id", "model", "session_id",
        "start_ts", "end_ts", "status", "error_text", "in_tokens", "out_tokens",
        "cache_read_tokens", "cache_write_tokens",
        "usage_source", "in_chars", "out_chars",
    )
    STEP_COLS = (
        "id", "turn_id", "seq", "kind", "name", "status", "start_ts", "end_ts",
        "in_tokens", "out_tokens", "cache_read_tokens", "cache_write_tokens", "detail",
    )

    @staticmethod
    def _turn_row(row):
        d = dict(zip(Store.TURN_COLS, row))
        if d.get("status") == "running":
            # 还没结算的 turn 没有 usage 事实可言。列上是 NOT NULL DEFAULT 'estimated'，
            # 直接透出去会让界面把「运行中」显示成「估算 0 tokens」——真值其实已经
            # 躺在 steps 里。语义在投影层收敛，不动 schema。
            d["usage_source"] = None
        return d

    @staticmethod
    def _step_row(row):
        return dict(zip(Store.STEP_COLS, row))

    @classmethod
    def _as_detail(cls, detail, kind=None):
        if detail is None:
            return "{}"
        budget = cls._detail_budget(kind)
        if isinstance(detail, str):
            return _clip_text(detail, budget) if budget else detail
        return fit_detail(mark_mojibake(detail), budget)

    @staticmethod
    def _detail_budget(kind):
        """思维链走自己的上限（trace.max_thinking_chars，0 = 不截断）。

        不能沿用 detail_max_bytes（默认 8192 字节）：思维链动辄几千字，
        用户要的是「不截断、用滚轮看」，被公共上限砍掉就白做了。
        """
        if kind != "thinking":
            return detail_max_bytes()
        try:
            chars = int(trace_cfg().get("max_thinking_chars", 0) or 0)
        except (TypeError, ValueError):
            chars = 0
        return 0 if chars <= 0 else chars * 4      # 按字节给上限，中文按 4 字节算

    def get_turn(self, turn_id):
        with self.lock:
            row = self.con.execute(
                f"SELECT {','.join(self.TURN_COLS)} FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
        return self._turn_row(row) if row else None

    def recent_turns(self, limit=60, conv_id=None, include_running=False):
        """最近若干轮次的终态（带 reply_msg_id / error_text），前端据此给回复贴状态标签。"""
        conv_id = int(conv_id or self.active_conv())
        with self.lock:
            rows = self.con.execute(
                f"""SELECT {','.join(self.TURN_COLS)} FROM turns
                    WHERE conv_id=? AND (? OR end_ts IS NOT NULL)
                    ORDER BY start_ts DESC LIMIT ?""",
                (conv_id, bool(include_running), int(limit)),
            ).fetchall()
        return [self._turn_row(r) for r in rows]

    def is_turn_running(self, turn_id):
        with self.lock:
            row = self.con.execute(
                "SELECT 1 FROM turns WHERE id=? AND status='running'", (turn_id,)
            ).fetchone()
        return bool(row)

    def get_step(self, step_id):
        with self.lock:
            row = self.con.execute(
                f"SELECT {','.join(self.STEP_COLS)} FROM steps WHERE id=?", (step_id,)
            ).fetchone()
        return self._step_row(row) if row else None

    def open_turn(self, turn_id, trace_id, agent, depth=0, parent_turn_id=None,
                  trigger_kind="user", trigger_msg_id=None, trigger_by=None,
                  model=None, session_id=None, agent_id=None, conv_id=None):
        cid = int(conv_id) if conv_id else self.active_conv()   # 同上：先解析再拿锁
        with self.lock:
            # last_turn 的 key 必须是**路由 id**（不是显示名），权限挂靠要靠它回查
            self.last_turn[agent_id or agent] = turn_id
            self.con.execute(
                """INSERT INTO turns(id,trace_id,parent_turn_id,agent,depth,trigger_kind,
                                    trigger_msg_id,trigger_by,model,session_id,start_ts,status,conv_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'running',?)""",
                (turn_id, trace_id, parent_turn_id, agent, depth, trigger_kind,
                 trigger_msg_id, trigger_by, model, session_id, now_ms(),
                 cid),
            )
            self.con.commit()
        return self.get_turn(turn_id)

    # turn 终态 → 其下 running step 的收敛终态（§3.2 对照表，唯一映射，不许硬编码）
    STEP_SETTLE = {"interrupted": "skipped", "timeout": "error", "error": "error"}

    # 「这一步有没有 provider 报的用量」。写成一个常量，是因为 settle_turn 与
    # _rollup_usage 各有一份四分支（§955-958 的注释禁止合并那两个函数），
    # 谓词一旦漂移就会出现「关 turn 时算 provider、补写时算 estimated」这种抖动。
    # 四个桶都要算：一次**完全命中**的调用只回 cache_read、input_tokens 是 0 占位
    # （而占位会被 runner 刻意丢掉，见 run_claude/run_codebuddy 里那段注释），
    # 只看 in/out 会把它误判成 estimated。
    HAS_USAGE_SQL = ("SUM(CASE WHEN in_tokens IS NOT NULL OR out_tokens IS NOT NULL "
                     "OR cache_read_tokens IS NOT NULL OR cache_write_tokens IS NOT NULL "
                     "THEN 1 ELSE 0 END)")

    def settle_turn(self, turn_id, status, *, end_ts, reply_msg_id=None, error_text=None,
                    in_chars=None, out_chars=None):
        """唯一的关 turn 入口（§3.2 / §7.3）。

        - `end_ts` **必填**：调用方传真实事件时刻，函数内不取 now（否则 post() 的
          写库耗时会算进 turn 时长，用例 16 会红）。
        - 顺序不可颠倒：先抢 turn 行（守卫就是 `rowcount`），只在拿到 rowcount==1 的
          那一次才有权收敛 step —— 反过来写会出现「turn=interrupted，step=error」。
        - 幂等：status 只允许从 running 迁出一次；end_ts 用 COALESCE（首次写入者胜）；
          reply_msg_id 只在 IS NULL 时补写。
        """
        step_status = self.STEP_SETTLE.get(status, "error")   # ok 时残留 step 按 error 收
        with self.lock:
            total, with_usage = self.con.execute(
                f"""SELECT COUNT(*), {self.HAS_USAGE_SQL}
                     FROM steps WHERE turn_id=? AND kind='llm_call'""",
                (turn_id,),
            ).fetchone()
            if not total:
                source = "estimated"
            elif with_usage == total:
                source = "provider"
            elif with_usage:
                source = "mixed"
            else:
                source = "estimated"
            cur = self.con.execute(
                """UPDATE turns
                      SET end_ts=COALESCE(end_ts, ?),
                          status=?,
                          error_text=COALESCE(?, error_text),
                          in_chars=COALESCE(?, in_chars),
                          out_chars=COALESCE(?, out_chars),
                          in_tokens=(SELECT COALESCE(SUM(in_tokens),0) FROM steps
                                      WHERE turn_id=? AND kind='llm_call'),
                          out_tokens=(SELECT COALESCE(SUM(out_tokens),0) FROM steps
                                       WHERE turn_id=? AND kind='llm_call'),
                          cache_read_tokens=(SELECT COALESCE(SUM(cache_read_tokens),0) FROM steps
                                              WHERE turn_id=? AND kind='llm_call'),
                          cache_write_tokens=(SELECT COALESCE(SUM(cache_write_tokens),0) FROM steps
                                               WHERE turn_id=? AND kind='llm_call'),
                          usage_source=?
                    WHERE id=? AND status='running'""",
                (end_ts, status, error_text, in_chars, out_chars,
                 turn_id, turn_id, turn_id, turn_id, source, turn_id),
            )
            # 只有抢到 turn 行的那一次才有权收敛 step（§3.2 顺序约束）
            if cur.rowcount == 1:
                self.con.execute(
                    "UPDATE steps SET status=?, end_ts=? WHERE turn_id=? AND status='running'",
                    (step_status, end_ts, turn_id),
                )
            if reply_msg_id is not None:
                self.con.execute(
                    "UPDATE turns SET reply_msg_id=? WHERE id=? AND reply_msg_id IS NULL",
                    (reply_msg_id, turn_id),
                )
            self.con.commit()
        return self.get_turn(turn_id)

    def _rollup_usage(self, turn_id):
        """按 §5.3 从 `steps.llm_call` 求和，刷新 turn 的 usage 三列。

        只动 usage 三列，**不碰 status/end_ts**，所以对已终态 turn 也安全。

        与 `settle_turn` 里那段不是重复实现，别合并：`settle_turn` 的 rollup 必须
        与 status 写进同一条带 `status='running'` 守卫的 UPDATE —— 拆开会让
        `rowcount` 失去「只有首次关 turn 的那次才有权收敛子 step」的判别力。
        这里走的是**关闭之后的补写**路径，没有守卫可借，也不该改状态。
        """
        total, with_usage = self.con.execute(
            f"""SELECT COUNT(*), {self.HAS_USAGE_SQL}
                 FROM steps WHERE turn_id=? AND kind='llm_call'""",
            (turn_id,),
        ).fetchone()
        if not total:
            source = "estimated"
        elif with_usage == total:
            source = "provider"
        elif with_usage:
            source = "mixed"
        else:
            source = "estimated"
        self.con.execute(
            """UPDATE turns
                  SET in_tokens=(SELECT COALESCE(SUM(in_tokens),0) FROM steps
                                  WHERE turn_id=? AND kind='llm_call'),
                      out_tokens=(SELECT COALESCE(SUM(out_tokens),0) FROM steps
                                   WHERE turn_id=? AND kind='llm_call'),
                      cache_read_tokens=(SELECT COALESCE(SUM(cache_read_tokens),0) FROM steps
                                          WHERE turn_id=? AND kind='llm_call'),
                      cache_write_tokens=(SELECT COALESCE(SUM(cache_write_tokens),0) FROM steps
                                           WHERE turn_id=? AND kind='llm_call'),
                      usage_source=?
                WHERE id=?""",
            (turn_id, turn_id, turn_id, turn_id, source, turn_id),
        )

    def add_step(self, turn_id, kind, name=None, status="ok", detail=None,
                 in_tokens=None, out_tokens=None,
                 cache_read_tokens=None, cache_write_tokens=None):
        payload = self._as_detail(detail, kind)      # 思维链用 trace.max_thinking_chars，不受 8KB 公共上限
        now = now_ms()
        with self.lock:
            # §21 允许 step 晚于 turn.end_ts 落库（权限批准跨重启），但**绝不允许
            # 停在 running**：父 turn 已终态时 `settle_turn` 不会再跑，而
            # `settle_orphans` 只扫 `turns.status='running'` —— 一条晚到的 running
            # step 没有任何路径会再收敛它，会成为永久呼吸动画并跨重启累积。
            # 就地按父 turn 终态收敛（映射沿用 §3.2 的 STEP_SETTLE）。
            row = self.con.execute(
                "SELECT status FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            parent = row[0] if row else None
            late = parent is not None and parent != "running"
            if late and status == "running":
                status = self.STEP_SETTLE.get(parent, "error")
            seq = self.con.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
            cur = self.con.execute(
                """INSERT INTO steps(turn_id,seq,kind,name,status,start_ts,end_ts,
                                     in_tokens,out_tokens,
                                     cache_read_tokens,cache_write_tokens,detail)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (turn_id, seq, kind, name or None, status, now,
                 None if status == "running" else now, in_tokens, out_tokens,
                 cache_read_tokens, cache_write_tokens, payload),
            )
            # 晚到的 llm_call 也要进 rollup，否则 `turns.*_tokens != Σ steps.llm_call`
            # 会永久不等（live 上已复现 2 条，见 §24.7）。
            if late and kind == "llm_call":
                self._rollup_usage(turn_id)
            self.con.commit()
            sid = cur.lastrowid
        return self.get_step(sid)

    def end_step(self, step_id, status="ok", detail=None, in_tokens=None, out_tokens=None,
                 cache_read_tokens=None, cache_write_tokens=None):
        payload = None if detail is None else self._as_detail(detail)
        with self.lock:
            self.con.execute(
                """UPDATE steps SET status=?, end_ts=?,
                          detail=COALESCE(?, detail),
                          in_tokens=COALESCE(?, in_tokens),
                          out_tokens=COALESCE(?, out_tokens),
                          cache_read_tokens=COALESCE(?, cache_read_tokens),
                          cache_write_tokens=COALESCE(?, cache_write_tokens)
                     WHERE id=? AND status='running'""",
                (status, now_ms(), payload, in_tokens, out_tokens,
                 cache_read_tokens, cache_write_tokens, step_id),
            )
            self.con.commit()
        return self.get_step(step_id)

    def set_turn_model(self, turn_id, model):
        """补写 turn.model —— CLI 链的模型名只有从 stream 的 message_start 里才知道。

        只在为空时写（`model IS NULL`），绝不覆盖已有值：model 是「按模型聚合成本」
        的语义位，被 agent id 兜底填过就再也修不回来了。
        """
        if not model:
            return
        with self.lock:
            self.con.execute(
                "UPDATE turns SET model=? WHERE id=? AND model IS NULL", (model, turn_id)
            )
            self.con.commit()

    def set_step_usage(self, step_id, in_tokens=None, out_tokens=None,
                       cache_read_tokens=None, cache_write_tokens=None):
        """补写一次 API 调用的 usage（不改 status、不关 step）。

        Claude 的 usage 分两处来：`message_start` 给 input、`message_delta` 给 output（§5.2），
        所以是一路补写、由最后一次 `llm_end` 收口 —— COALESCE 保证「只带 output 的那次」
        不会把已经写好的 input 抹成 NULL。缓存两桶同理：命中量常比 output 晚到。
        """
        with self.lock:
            self.con.execute(
                """UPDATE steps
                      SET in_tokens=COALESCE(?, in_tokens),
                          out_tokens=COALESCE(?, out_tokens),
                          cache_read_tokens=COALESCE(?, cache_read_tokens),
                          cache_write_tokens=COALESCE(?, cache_write_tokens)
                    WHERE id=?""",
                (in_tokens, out_tokens, cache_read_tokens, cache_write_tokens, step_id),
            )
            self.con.commit()
        return self.get_step(step_id)

    def settle_orphans(self, boot_ms=None):
        """收敛上次进程被强杀留下的 running（§7.4）。

        保守版：只动 `start_ts` 早于本次进程启动的，避免误伤本进程里真正在跑的 turn
        （同一个 hub.db 被两个实例打开时那会静默判死对方的活 turn）。
        逐条走 `settle_turn` —— 连带把子 step 收成 `skipped`，不复制 UPDATE 语句。
        """
        edge = BOOT_MS if boot_ms is None else boot_ms
        with self.lock:
            ids = [r[0] for r in self.con.execute(
                "SELECT id FROM turns WHERE status='running' AND start_ts < ?", (edge,)
            ).fetchall()]
        # settle_turn 自己要拿 self.lock（非可重入），所以必须在锁外逐条调用
        for tid in ids:
            self.settle_turn(tid, "interrupted", end_ts=edge, error_text="hub restarted")

        # 存量补救：晚期版本之前 `add_step` 无父 turn 终态判断，把 step 留在 `running`
        # 而父 turn 已终态 —— 这类行只扫 turns 覆盖不到，会跨重启累积成永久呼吸动画，
        # 并让 rollup 永久偏离 Σ steps。这里一次性收敛 + 刷新 rollup（幂等，之后自静默）。
        # 只打父 turn 已终态的 step，所以对另一实例正在跑的活 turn 天然无害。
        with self.lock:
            rows = self.con.execute(
                """SELECT s.id, s.turn_id, t.status FROM steps s
                     JOIN turns t ON t.id=s.turn_id
                    WHERE s.status='running' AND t.status!='running'"""
            ).fetchall()
            for sid, tid, tstatus in rows:
                self.con.execute(
                    "UPDATE steps SET status=?, end_ts=COALESCE(end_ts, ?) WHERE id=?",
                    (self.STEP_SETTLE.get(tstatus, "error"), edge, sid),
                )
            # 终态 turn 的 usage 三列按 Σ steps 重算一次：rollup 只在 settle_turn 里写，
            # 任何「关闭之后才到」的 step 都会让它永久偏离（§24.7，live 上 2 条）。
            # `_rollup_usage` 只动 usage 三列、不碰状态，所以对终态 turn 安全；
            # 启动时全量一遍即收敛为 0 条差异，后续每次启动都是空转。
            for (tid,) in self.con.execute(
                    "SELECT id FROM turns WHERE status!='running'").fetchall():
                self._rollup_usage(tid)
            self.con.commit()
        return len(ids)

    def trace_list(self, limit=50):
        """本会话的 trace 列表（换了会话就不该看到别的会话的执行流程）。"""
        conv_id = self.active_conv()          # 先解析：active_conv 也要拿同一把非重入锁
        with self.lock:
            rows = self.con.execute(
                """SELECT trace_id, MIN(start_ts), MAX(COALESCE(end_ts, start_ts)),
                          COUNT(*),
                          SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status IN ('error','timeout') THEN 1 ELSE 0 END),
                          MIN(trigger_msg_id), GROUP_CONCAT(DISTINCT agent)
                       FROM turns WHERE conv_id=? GROUP BY trace_id
                      ORDER BY MIN(start_ts) DESC LIMIT ?""",
                  (conv_id, limit),
              ).fetchall()
        out = []
        for tid, started, ended, count, running, failed, root_msg, agents in rows:
            out.append({
                "trace_id": tid,
                "started": started,
                "ended": ended,
                "turn_count": count,
                "agents": [a for a in (agents or "").split(",") if a],
                "status": "running" if running else ("error" if failed else "ok"),
                "root_msg_id": root_msg,
            })
        return out

    def trace_detail(self, trace_id):
        with self.lock:
            turns = self.con.execute(
                f"SELECT {','.join(self.TURN_COLS)} FROM turns WHERE trace_id=? ORDER BY start_ts, id",
                (trace_id,),
            ).fetchall()
            if not turns:
                return None
            steps = self.con.execute(
                f"""SELECT {','.join(self.STEP_COLS)} FROM steps
                     WHERE turn_id IN (SELECT id FROM turns WHERE trace_id=?)
                     ORDER BY turn_id, seq""",
                (trace_id,),
            ).fetchall()
        grouped = {}
        for row in steps:
            grouped.setdefault(row[1], []).append(self._step_row(row))
        return {"turns": [self._turn_row(r) for r in turns], "steps": grouped}

    # -- 附件 ---------------------------------------------------------------
    def add_attachment(self, name, rel, path, kind, size, text):
        with self.lock:
            cur = self.con.execute(
                """INSERT INTO attachments(name,rel,path,kind,size,text,ts)
                   VALUES(?,?,?,?,?,?,?)""",
                (name, rel, path, kind, size, text, time.time()),
            )
            self.con.commit()
            aid = cur.lastrowid
        return self.get_attachment(aid)

    def get_attachment(self, aid):
        with self.lock:
            row = self.con.execute(
                """SELECT id,name,rel,path,kind,size,text,ts,message_id
                     FROM attachments WHERE id=?""",
                (aid,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "rel": row[2], "path": row[3], "kind": row[4],
            "size": row[5], "text": row[6], "ts": row[7], "message_id": row[8],
        }

    def bind_attachments(self, ids, message_id):
        with self.lock:
            for aid in ids:
                self.con.execute(
                    "UPDATE attachments SET message_id=? WHERE id=?", (message_id, aid)
                )
            self.con.commit()
        return [self.get_attachment(a) for a in ids]

    def attachments_for_message(self, message_id):
        with self.lock:
            rows = self.con.execute(
                """SELECT id,name,rel,path,kind,size,text,ts,message_id
                     FROM attachments WHERE message_id=? ORDER BY id""",
                (message_id,),
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "rel": r[2], "path": r[3], "kind": r[4],
             "size": r[5], "text": r[6], "ts": r[7], "message_id": r[8]}
            for r in rows
        ]

    def update_message(self, row_id, text):
        with self.lock:
            cur = self.con.execute("UPDATE messages SET text=? WHERE id=?", (text, row_id))
            self.con.commit()
            if cur.rowcount == 0:
                return None
        return self.get(row_id)

    def delete_message(self, row_id):
        with self.lock:
            cur = self.con.execute("DELETE FROM messages WHERE id=?", (row_id,))
            self.con.execute("DELETE FROM highlights WHERE message_id=?", (row_id,))
            self.con.commit()
            return cur.rowcount > 0

    def close(self):
        """退出前收尾：提交、把 WAL 并回主库（失败就算了，绝不让退出卡住）。"""
        with self.lock:
            try:
                self.con.commit()
                self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self.con.close()
            except Exception:
                pass

    # -- 冻结前缀（缓存命中）-------------------------------------------------
    # 存的是**渲染好的字符串**：命中前缀缓存的前提是前缀逐字节不变，重新渲染会因为
    # 改名/成员增减/权限改动而漂移。字段语义见 _migrate 里 prompt_cache 的建表。
    def get_prompt_prefix(self, agent, conv_id):
        with self.lock:
            row = self.con.execute(
                """SELECT prefix, anchor_msg_id, head_hash, prefix_chars
                     FROM prompt_cache WHERE agent=? AND conv_id=?""",
                (agent, int(conv_id)),
            ).fetchone()
        if not row:
            return None
        return {"prefix": row[0] or "", "anchor_msg_id": int(row[1] or 0),
                "head_hash": row[2] or "", "prefix_chars": int(row[3] or 0)}

    def set_prompt_prefix(self, agent, conv_id, prefix, anchor_msg_id, head_hash):
        with self.lock:
            self.con.execute(
                """INSERT INTO prompt_cache(agent,conv_id,prefix,anchor_msg_id,head_hash,
                                            prefix_chars,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(agent,conv_id) DO UPDATE SET
                       prefix=excluded.prefix, anchor_msg_id=excluded.anchor_msg_id,
                       head_hash=excluded.head_hash, prefix_chars=excluded.prefix_chars,
                       updated_at=excluded.updated_at""",
                (agent, int(conv_id), prefix, int(anchor_msg_id or 0), head_hash,
                 len(prefix or ""), time.time()),
            )
            self.con.commit()

    def invalidate_prompt_prefix(self, conv_id, agent=None):
        """作废冻结前缀：某个会话的全部成员（默认），或该会话里某一个成员。

        前缀一旦被改写就不再是"上一轮那个前缀"，provider 那边只能全 miss —— 所以
        消息被改写/删除/改高亮之后必须重锚（付一次 miss），不能留着旧的字节。
        """
        cid = int(conv_id)
        with self.lock:
            if agent:
                self.con.execute("DELETE FROM prompt_cache WHERE conv_id=? AND agent=?",
                                 (cid, agent))
            else:
                self.con.execute("DELETE FROM prompt_cache WHERE conv_id=?", (cid,))
            self.con.commit()

    def invalidate_agent_prefix(self, agent):
        """作废该成员在**所有会话**里的前缀。

        换模型/换 profile 时用：字节可能一样，但那是另一个模型的缓存，留着只会误导
        （命中率显示得好看，实际全是 miss）。
        """
        with self.lock:
            self.con.execute("DELETE FROM prompt_cache WHERE agent=? OR substr(agent,1,length(?)+1)=?||'#'", (agent, agent, agent))
            self.con.commit()

    def messages_after(self, conv_id, after_id, kinds=("user", "reply")):
        """按 id 升序取某个会话里 id > after_id 的可见消息（追加冻结前缀用）"""
        marks = ",".join("?" for _ in kinds)
        with self.lock:
            ids = [r[0] for r in self.con.execute(
                f"""SELECT id FROM messages
                     WHERE conv_id=? AND id>? AND kind IN ({marks})
                     ORDER BY id""",
                (int(conv_id), int(after_id or 0), *kinds),
            ).fetchall()]
        # self.get() 自己也要拿同一把（非重入）锁 —— 必须出了锁再逐条取
        return [m for m in (self.get(i) for i in ids) if m]

    def delete_messages_by_ref(self, ref):
        """按 ref 删消息（权限申请卡用完即弃）；返回被删的 id 列表，调用方据此广播。"""
        with self.lock:
            rows = self.con.execute("SELECT id FROM messages WHERE ref=?", (ref,)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.con.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
                self.con.execute(f"DELETE FROM highlights WHERE message_id IN ({marks})", ids)
                self.con.commit()
            return ids

    def set_highlight(self, row_id, on):
        with self.lock:
            cur = self.con.execute(
                "UPDATE messages SET highlight=? WHERE id=?", (1 if on else 0, row_id)
            )
            self.con.commit()
            if cur.rowcount == 0:
                return None
        return self.get(row_id)

    # -- 片段高亮 -----------------------------------------------------------
    def list_highlights(self, message_id):
        with self.lock:
            rows = self.con.execute(
                "SELECT start,end,text FROM highlights WHERE message_id=? ORDER BY start",
                (message_id,),
            ).fetchall()
        return [{"start": r[0], "end": r[1], "text": r[2]} for r in rows]

    def add_highlight(self, message_id, start, end, text):
        with self.lock:
            # 与已有区间重叠的旧片段先去掉，再插入，保持互不重叠
            rows = self.con.execute(
                "SELECT id,start,end FROM highlights WHERE message_id=?", (message_id,)
            ).fetchall()
            for hid, s, e in rows:
                if not (end <= s or start >= e):
                    self.con.execute("DELETE FROM highlights WHERE id=?", (hid,))
            self.con.execute(
                "INSERT INTO highlights(message_id,start,end,text,ts) VALUES(?,?,?,?,?)",
                (message_id, start, end, text, time.time()),
            )
            self.con.execute("UPDATE messages SET highlight=1 WHERE id=?", (message_id,))
            self.con.commit()
        return self.get(message_id)

    def clear_highlights(self, message_id):
        with self.lock:
            self.con.execute("DELETE FROM highlights WHERE message_id=?", (message_id,))
            self.con.execute("UPDATE messages SET highlight=0 WHERE id=?", (message_id,))
            self.con.commit()
        return self.get(message_id)

    def highlights_map(self):
        with self.lock:
            rows = self.con.execute(
                "SELECT message_id,start,end FROM highlights ORDER BY message_id,start"
            ).fetchall()
        out = {}
        for mid, s, e in rows:
            out.setdefault(mid, []).append({"start": s, "end": e})
        return out

    # -- sessions -----------------------------------------------------------
    # 原生记忆是「一个会话一份」。conv_id 一律先解析再拿锁：active_conv() 自己会拿同一把
    # 非重入锁（它走 state_get），持锁后再调它会自死锁。
    def get_session(self, agent, conv_id=None):
        cid = int(conv_id) if conv_id else self.active_conv()
        with self.lock:
            row = self.con.execute(
                "SELECT session_id FROM sessions WHERE agent=? AND conv_id=?", (agent, cid)
            ).fetchone()
        return row[0] if row else None

    def set_session(self, agent, session_id, conv_id=None):
        cid = int(conv_id) if conv_id else self.active_conv()
        with self.lock:
            self.con.execute(
                "INSERT INTO sessions(agent,conv_id,session_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(agent,conv_id) DO UPDATE SET session_id=excluded.session_id, "
                "updated_at=excluded.updated_at",
                (agent, cid, session_id, time.time()),
            )
            self.con.commit()

    def clear_session(self, agent, conv_id=None):
        """清掉该成员在**某个会话**里的原生记忆（默认当前会话）。"""
        cid = int(conv_id) if conv_id else self.active_conv()
        with self.lock:
            self.con.execute("DELETE FROM sessions WHERE agent=? AND conv_id=?", (agent, cid))
            self.con.commit()

    def clear_agent_sessions(self, agent):
        """清掉该成员在**所有会话**里的原生记忆。

        换模型/换 profile 时必须用这个：session/resume 是绑在原模型上的，
        只清当前会话的话，切回别的会话照样会命中老模型的 session。
        """
        with self.lock:
            self.con.execute("DELETE FROM sessions WHERE agent=? OR substr(agent,1,length(?)+1)=?||'#'", (agent, agent, agent))
            self.con.commit()

    # -- state --------------------------------------------------------------
    def state_get(self, key, default=None):
        with self.lock:
            row = self.con.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def state_set(self, key, value):
        with self.lock:
            self.con.execute(
                "INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
            self.con.commit()

    # -- models -------------------------------------------------------------
    def list_models(self):
        with self.lock:
            rows = self.con.execute(
                """SELECT id,label,style,base_url,api_key,model,system_prompt,path,
                          max_tokens,enabled,created_at FROM models ORDER BY created_at"""
            ).fetchall()
        return [
            {
                "id": r[0],
                "label": r[1],
                "style": r[2],
                "base_url": r[3],
                "api_key": r[4],
                "model": r[5],
                "system_prompt": r[6],
                "path": r[7],
                "max_tokens": r[8],
                "enabled": bool(r[9]),
                "created_at": r[10],
            }
            for r in rows
        ]

    def get_model(self, model_id):
        for m in self.list_models():
            if m["id"] == model_id:
                return m
        return None

    def upsert_model(self, item):
        with self.lock:
            self.con.execute(
                """INSERT INTO models(id,label,style,base_url,api_key,model,system_prompt,path,
                                      max_tokens,enabled,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     label=excluded.label, style=excluded.style, base_url=excluded.base_url,
                     api_key=excluded.api_key, model=excluded.model,
                     system_prompt=excluded.system_prompt, path=excluded.path,
                     max_tokens=excluded.max_tokens, enabled=excluded.enabled""",
                (
                    item["id"], item["label"], item["style"], item["base_url"], item["api_key"],
                    item["model"], item.get("system_prompt", ""), item.get("path") or "",
                    int(item.get("max_tokens") or 1024),
                    1 if item.get("enabled", True) else 0, item.get("created_at") or time.time(),
                ),
            )
            self.con.commit()
        return self.get_model(item["id"])

    def delete_model(self, model_id):
        with self.lock:
            self.con.execute("DELETE FROM models WHERE id=?", (model_id,))
            self.con.execute("DELETE FROM api_history WHERE model_id=?", (model_id,))
            self.con.execute("DELETE FROM sessions WHERE agent=?", (model_id,))
            self.con.execute("DELETE FROM prompt_cache WHERE agent=?", (model_id,))
            self.con.commit()

    def history(self, model_id, limit=24):
        with self.lock:
            rows = self.con.execute(
                "SELECT role,content FROM api_history WHERE model_id=? ORDER BY id DESC LIMIT ?",
                (model_id, limit),
            ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def add_history(self, model_id, role, content):
        with self.lock:
            self.con.execute(
                "INSERT INTO api_history(model_id,role,content,ts) VALUES(?,?,?,?)",
                (model_id, role, content, time.time()),
            )
            self.con.commit()

    def clear_history(self, model_id):
        with self.lock:
            self.con.execute("DELETE FROM api_history WHERE model_id=?", (model_id,))
            self.con.commit()

    # -- stats / health -----------------------------------------------------
    def agent_stats(self):
        with self.lock:
            sent = dict(
                self.con.execute(
                    "SELECT sender, COUNT(*) FROM messages WHERE kind IN ('reply','error') GROUP BY sender"
                ).fetchall()
            )
            received = dict(
                self.con.execute("SELECT target, COUNT(*) FROM messages GROUP BY target").fetchall()
            )
            last = dict(
                self.con.execute(
                    "SELECT sender, MAX(ts) FROM messages WHERE kind IN ('reply','error') GROUP BY sender"
                ).fetchall()
            )
        return {
            name: {
                "sent": sent.get(name, 0),
                "received": received.get(name, 0),
                "last_active": last.get(name),
            }
            for name in set(sent) | set(received) | set(last)
        }

    def last_settled_turns_by_agent(self, conv_id):
        """每个成员在**该会话**里最后一轮已结算的终态，外加它第 1 次模型调用的命中情况。

        给"冷启动提示"用（前端 `coldState()`）：把上一轮用的**会话 id**和当前会话 id 一比，
        就知道这一轮会不会全冷 —— 会话变了（重置会话 / 换模型 / 换 profile / 切会话）必冷。
        这比按时间猜靠谱得多：同一会话 + 同一 session id 内实测隔 **12.8h / 8.6h / 6.4h**
        第 1 次调用**仍然命中**，所以各家文档那套 5/30/10 分钟是**保证下限**、不是淘汰时刻
        （DeepSeek 原话是"不再使用后自动清空"，尽力而为）。见 README「缓存命中」。

        `first_hit_rate` 是**事实值**（上一轮第 1 次调用的实测命中率），不是预测值；
        整轮冷启动的代价全压在第 1 次调用上（`seq` 最小那条）。

        SQLite 惯例：查询里只有一个 max() 聚合时，裸列取的就是命中该 max 的那一行 ——
        所以 end_ts / session_id / 三个 token 桶都来自 start_ts 最大的那一轮。

        **返回值按 `turns.agent` 的键来** —— 那是**显示名**（"Claude"），不是路由 id；
        见 hub.py:2551 `self.agent = agent  # 显示名（人看的）`。别处（sessions /
        messages / config / agent_defs().name）用的一律是路由 id（"claude"），
        两者混用会**静默查不到**（本文件 hub.py:979 那条注释就是踩过这个坑留下的）。
        """
        cid = int(conv_id)
        with self.lock:
            def last_rows(require_usage):
                sql = ("""SELECT id, agent, MAX(start_ts), end_ts, session_id,
                                 in_tokens, cache_read_tokens, cache_write_tokens
                          FROM turns WHERE conv_id=? AND end_ts IS NOT NULL""")
                if require_usage:
                    sql += " AND (in_tokens + cache_read_tokens + cache_write_tokens) > 0"
                sql += " GROUP BY agent"
                return self.con.execute(sql, (cid,)).fetchall()

            out = {}
            # 先铺"只要有轮次就认"的兜底（dsh 不报用量、错误轮也可能没用量），
            # 再用"报过用量"的那一轮覆盖 —— 那一条才带着可信的命中率和完整的间隔。
            # 不这么做的话：成员上一条是 error 轮时，命中率会显示成"—"、间隔也算短了。
            for rows in (last_rows(False), last_rows(True)):
                for tid, agent, _st, end_ts, session_id, miss, read, write in rows:
                    first = self.con.execute(
                        """SELECT in_tokens, cache_read_tokens FROM steps
                           WHERE turn_id=? AND kind='llm_call' ORDER BY seq LIMIT 1""",
                        (tid,),
                    ).fetchone()
                    f_miss, f_read = (first if first else (None, None))
                    cache_reported = f_read is not None and f_miss is not None
                    f_miss, f_read = (f_miss or 0), (f_read or 0)
                    out[agent] = {
                        # **秒**，不是毫秒：前端 hhmmss()（web/index.html:525）按秒 ×1000
                        "end_ts": (end_ts / 1000.0) if end_ts else None,
                        "session_id": session_id,
                        "prompt_tokens": (miss or 0) + (read or 0) + (write or 0),
                        "first_hit_rate": (f_read / (f_read + f_miss)) if cache_reported and (f_read + f_miss) else None,
                    }
            return out

    def health_counts(self):
        with self.lock:
            total = self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            errors = self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='error'").fetchone()[0]
        return {"messages": total, "errors": errors}

    def nexus_map_upsert(self, agent_id, role):
        with self.lock:
            self.con.execute(
                """INSERT INTO nexus_agents(id,role,first_seen,last_seen) VALUES(?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET role=excluded.role, last_seen=excluded.last_seen""",
                (agent_id, role, time.time(), time.time()),
            )
            self.con.commit()

    def nexus_role(self, agent_id):
        with self.lock:
            row = self.con.execute(
                "SELECT role FROM nexus_agents WHERE id=?", (agent_id,)
            ).fetchone()
        return row[0] if row else None

    # -- 工作区权限准入 -----------------------------------------------------
    def list_grants(self, agent=None):
        with self.lock:
            if agent:
                rows = self.con.execute(
                    "SELECT agent,path,mode,ts FROM grants WHERE agent=? ORDER BY ts", (agent,)
                ).fetchall()
            else:
                rows = self.con.execute(
                    "SELECT agent,path,mode,ts FROM grants ORDER BY agent,ts"
                ).fetchall()
        return [{"agent": r[0], "path": r[1], "mode": r[2], "ts": r[3]} for r in rows]

    def set_grant(self, agent, path, mode):
        with self.lock:
            self.con.execute(
                """INSERT INTO grants(agent,path,mode,ts) VALUES(?,?,?,?)
                   ON CONFLICT(agent,path) DO UPDATE SET mode=excluded.mode, ts=excluded.ts""",
                (agent, path, "rw" if mode == "rw" else "ro", time.time()),
            )
            self.con.commit()
        return self.list_grants(agent)

    def remove_grant(self, agent, path):
        with self.lock:
            self.con.execute("DELETE FROM grants WHERE agent=? AND path=?", (agent, path))
            self.con.commit()
        return self.list_grants(agent)

    def replace_grants(self, mapping):
        """用 config.json 里的授权表重建内存库（config 是唯一真源）。"""
        with self.lock:
            self.con.execute("DELETE FROM grants")
            for agent, items in (mapping or {}).items():
                for item in items or []:
                    path = (item.get("path") or "").strip()
                    if not path:
                        continue
                    self.con.execute(
                        "INSERT OR REPLACE INTO grants(agent,path,mode,ts) VALUES(?,?,?,?)",
                        (agent, path, "rw" if (item.get("mode") or "rw") == "rw" else "ro", time.time()),
                    )
            self.con.commit()

    def _perm_row(self, row):
        return {
            "id": row[0], "agent": row[1], "path": row[2], "mode": row[3],
            "reason": row[4], "status": row[5], "ts": row[6], "resolved_ts": row[7],
            "turn_id": row[8],
        }

    def add_perm_request(self, agent, path, mode, reason, turn_id=None):
        with self.lock:
            if turn_id is None:
                turn_id = self.last_turn.get(agent)
            cur = self.con.execute(
                """INSERT INTO perm_requests(agent,path,mode,reason,status,ts,turn_id)
                   VALUES(?,?,?,?, 'pending', ?, ?)""",
                (agent, path, "rw" if mode == "rw" else "ro", reason, time.time(), turn_id),
            )
            self.con.commit()
            rid = cur.lastrowid
        return self.get_perm_request(rid)

    def get_perm_request(self, rid):
        with self.lock:
            row = self.con.execute(
                """SELECT id,agent,path,mode,reason,status,ts,resolved_ts,turn_id
                     FROM perm_requests WHERE id=?""",
                (rid,),
            ).fetchone()
        if not row:
            return None
        return self._perm_row(row)

    def list_perm_requests(self, status="pending"):
        with self.lock:
            if status:
                rows = self.con.execute(
                    """SELECT id,agent,path,mode,reason,status,ts,resolved_ts,turn_id
                         FROM perm_requests WHERE status=? ORDER BY id""",
                    (status,),
                ).fetchall()
            else:
                rows = self.con.execute(
                    """SELECT id,agent,path,mode,reason,status,ts,resolved_ts,turn_id
                         FROM perm_requests ORDER BY id"""
                ).fetchall()
        return [self._perm_row(r) for r in rows]

    def resolve_perm_request(self, rid, approve):
        with self.lock:
            self.con.execute(
                "UPDATE perm_requests SET status=?, resolved_ts=? WHERE id=?",
                ("approved" if approve else "denied", time.time(), rid),
            )
            self.con.commit()
        return self.get_perm_request(rid)


# ---------------------------------------------------------------------- processes
def decode_stream_line(raw):
    """子进程输出的容忍解码：先按 UTF-8，失败再按 GBK。

    Windows 上不少工具按 ANSI 码页吐字节（PowerShell 5.1 重定向输出就是），
    死认 UTF-8 会得到一堆 U+FFFD 替换符（2026-09-27 用户报的 Codex 乱码里就有这一类）。
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp936")
        except Exception:
            return raw.decode("utf-8", "replace")


# "UTF-8 字节被 GBK 解码"这种乱码的指纹：私用区字符，或这些几乎只会出现在错码里的字
_MOJIBAKE_MARKERS = set("锛銆鈥鍣椹卞姩紙鍗鐢嬌鏈夎鏂鍚鏃鐨綘彲鑳鏉鍐鎴鎵缁鑰浠鍦澶灏甯鏇")
_MOJIBAKE_NOTE = "\n⚠ 这段输出的编码是错的（源命令按 ANSI 码页读了 UTF-8 内容）。换 Read 工具重读，或先 chcp 65001 / Get-Content -Encoding UTF8 再跑。"


def looks_mojibake(text):
    if not isinstance(text, str) or len(text) < 6:
        return False
    if any("\ue000" <= ch <= "\uf8ff" for ch in text):
        return True
    return sum(1 for ch in text if ch in _MOJIBAKE_MARKERS) >= 3


def mark_mojibake(obj, depth=0):
    """只加一句说明，不试图"还原"：错码是有损的，硬转会产出看着像正文的错文本。"""
    if depth > 4:
        return obj
    if isinstance(obj, str):
        return obj + _MOJIBAKE_NOTE if looks_mojibake(obj) else obj
    if isinstance(obj, dict):
        return {k: mark_mojibake(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mark_mojibake(v, depth + 1) for v in obj]
    return obj


def stream_command(cmd, cwd, timeout, on_line, cancel=None, on_proc=None, input_text=None,
                   extra_env=None):
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        # dsh 用它把超长 prompt 从命令行挪到环境变量里传（见 run_dsh）
        env.update({k: str(v) for k, v in extra_env.items()})
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,                      # 二进制读，逐行容忍解码（见 decode_stream_line）
            env=env,
            **_popen_session_kwargs(),
        )
    except FileNotFoundError:
        # CLI 不在 PATH 也不在常见安装目录（Windows 上很常见）：说清楚怎么修
        first = (cmd[0] if isinstance(cmd, (list, tuple)) and cmd else (cmd if isinstance(cmd, str) else ""))
        raise RuntimeError(exe_missing_hint(None, first)) from None
    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass
    lines = []
    q = queue.Queue()

    def reader():
        try:
            for raw in proc.stdout:
                q.put(decode_stream_line(raw))
        finally:
            proc.stdout.close()
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    if input_text is not None:
        # Feed stdin concurrently: a CLI can write enough output to fill stdout
        # before it finishes reading a large prompt. A blocking write deadlocks both.
        def writer():
            try:
                proc.stdin.write(input_text.encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                proc.stdin.close()
        threading.Thread(target=writer, daemon=True).start()
    deadline = None if not timeout or timeout <= 0 else time.time() + timeout
    last_data = time.time()
    started_at = time.time()
    last_beat = started_at
    while True:
        if cancel is not None and cancel.is_set():
            terminate_tree(proc)
            lines.append("[cancelled]")
            break
        if deadline is None:
            remaining = 0.5
        else:
            remaining = deadline - time.time()
            if remaining <= 0:
                terminate_tree(proc)
                lines.append("[timeout]")
                break
        try:
            item = q.get(timeout=min(0.5, remaining))
        except queue.Empty:
            if time.time() - last_beat > 60:
                try:
                    on_line(f"[仍在执行] 已运行 {int(time.time() - started_at)} 秒…")
                except Exception:
                    pass
                last_beat = time.time()
            if proc.poll() is not None:
                # 进程已退出：队列空就收工；若管道被孙进程占着导致读不到 EOF，静默 15s 也判定结束
                if q.empty():
                    break
                if time.time() - last_data > 15:
                    lines.append("[子进程已退出，管道仍被其子进程占用，判定结束]")
                    break
            continue
        if item is None:
            break
        line = item.rstrip("\r\n")
        last_data = time.time()
        lines.append(line)
        try:
            on_line(line)
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_tree(proc, timeout=2)
    return proc.returncode, lines


def _json_lines(lines):
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# 成员 CLI 的"PATH 之外"常见安装位置。起因（2026-09-26 18:35）：codex.exe 装在
# %LOCALAPPDATA%\OpenAI\Codex\bin\<版本哈希>\ 里，那个目录**只由 Codex 桌面版注入进 PATH**；
# 用户自己双击 start-hub.bat 启动中枢时 PATH 里没有它，于是 Popen 抛 WinError 2。
# 版本哈希目录会随 Codex 升级变化，所以这里用通配 + 按修改时间取最新。
_BIN_GLOBS = (
    "%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*",
)
# ★ 只放「用环境变量表达的通用位置」。这个文件要随 npm 包发给别人，
#   任何机器专属绝对路径（作者把 Node.js / Python 装在哪个盘）都不允许出现在这里。
_BIN_DIRS = (
    "%LOCALAPPDATA%\\Programs",
    "%LOCALAPPDATA%\\Microsoft\\WinGet\\Links",
    "%APPDATA%\\npm",
    # 用 ~ 表达（跨平台都能展开；%USERPROFILE% 在 POSIX 上不存在）
    "~/.local/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/.cargo/bin",
    "~/.deno/bin",
    "~/scoop/shims",
    "%ProgramFiles%\\nodejs",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

_NPM_PREFIX_CACHE = {"loaded": False, "value": ""}


def _npm_prefix():
    """npm 全局前缀（运行时推导）。Windows 上 npm 是 .cmd，必须经 cmd 执行。"""
    if _NPM_PREFIX_CACHE["loaded"]:
        return _NPM_PREFIX_CACHE["value"]
    _NPM_PREFIX_CACHE["loaded"] = True
    value = os.environ.get("CLAUDELINK_NPM_PREFIX") or os.environ.get("npm_config_prefix") or ""
    if not value:
        try:
            if os.name == "nt":
                cmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/s", "/c", "npm prefix -g"]
            else:
                cmd = [shutil.which("npm") or "npm", "prefix", "-g"]
            # npm/node 往管道写 UTF-8；不显式指定编码时 Windows 会按 cp936 解，中文前缀会变乱码
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=10).stdout or ""
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            value = lines[-1] if lines else ""
        except Exception:
            value = ""
    _NPM_PREFIX_CACHE["value"] = value if value and os.path.isdir(value) else ""
    return _NPM_PREFIX_CACHE["value"]


def candidate_bin_dirs():
    """PATH 之外还要找的目录列表（已展开环境变量，带通配的按新→旧排序）。"""
    dirs = []
    for pattern in _BIN_GLOBS:
        expanded = os.path.expandvars(pattern)
        if glob.has_magic(expanded):
            hits = [p for p in glob.glob(expanded) if os.path.isdir(p)]
            dirs.extend(sorted(hits, key=lambda p: os.path.getmtime(p), reverse=True))
        else:
            dirs.append(expanded)
    dirs.extend(os.path.expanduser(os.path.expandvars(d)) for d in _BIN_DIRS)
    prefix = _npm_prefix()
    if prefix:
        dirs.append(prefix)
        if os.name != "nt":
            dirs.append(os.path.join(prefix, "bin"))
    # 展开后仍带 % 的说明环境变量没定义 → 丢掉，别当成路径
    return [d for d in dirs if d and "%" not in d]


def resolve_command(exe):
    """把裸命令名解析成真实可执行文件（Windows 上 npm 全局包常只有 .cmd）。

    顺序：绝对路径 → PATH → candidate_bin_dirs()。都找不到就把原名返回，
    由调用方按"找不到可执行文件"报错（见 exe_missing_hint）。
    """
    exe = os.path.expandvars(os.path.expanduser(exe))
    if os.path.isabs(exe) and os.path.exists(exe):
        return exe
    names = [exe]
    if os.name == "nt" and not exe.lower().endswith((".exe", ".cmd", ".bat")):
        names = [exe + ".exe", exe + ".cmd", exe + ".bat", exe]
    search = (os.environ.get("PATH") or "").split(os.pathsep) + candidate_bin_dirs()
    for folder in search:
        if not folder or not os.path.isdir(folder):
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            # 只接受普通文件；POSIX 上还要求可执行位（否则 Popen 会抛 PermissionError）
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return exe


# --------------------------------------------------------- 成员 CLI 的路径来源
# 路径一律由环境变量决定：环境变量 → config.json 的 agents.<名>.exe（可写 %VAR%）→ CLI_DEFAULTS。
# 起因（2026-09-29）：agents.codebuddy.exe 写死了 WorkBuddy 桌面版内嵌的旧 CLI
# （其安装目录下 resources/app.asar.unpacked/cli/bin/codebuddy），那套安装被换成 npm 全局包之后
# 路径就失效了，而路径锁死在 JSON 里意味着"CLI 一升级就得改配置再重启"。
# 命名沿用启动脚本里既有的 CLAUDELINK_PYTHON。
CLI_ENV = {
    "claude": "CLAUDELINK_CLAUDE",
    "codex": "CLAUDELINK_CODEX",
    "codebuddy": "CLAUDELINK_CODEBUDDY",
    "dsh": "CLAUDELINK_DSH",
}

# 最后一道网：环境变量、config、检测缓存都拿不到时的兜底 —— **一律用裸名**，
# 交给 resolve_command()（PATH → candidate_bin_dirs()）解析。不许写机器绝对路径。
CLI_DEFAULTS = {
    "claude": "claude",
    "codex": "codex",
    "codebuddy": "codebuddy",
    "dsh": "dsh",
}
# 空 = 运行时解析（CLAUDELINK_NODE → config.node → 检测缓存 → PATH 里的 node）
CLI_NODE_DEFAULT = ""
# 用 ~ 表达：POSIX 上 %USERPROFILE% 不存在（expandvars 会原样留下）
CLAUDE_SETTINGS_DEFAULT = "~/.ccm/settings"

_DETECTED_CACHE = {"mtime": None, "data": {}}


def detected_agents():
    """读 npm 安装时 `detect --write` 生成的 ~/.claudelink/agents.detected.json。

    与「用户自定义 Agent 注册表」agents.json 是**两个文件**（见 bin/util.js 的说明），
    这样自动检测永远不会覆盖用户手写的条目。
    可用环境变量 CLAUDELINK_AGENTS 指向别的缓存文件。
    """
    path = os.environ.get("CLAUDELINK_AGENTS") or os.path.join(
        os.path.expanduser("~"), ".claudelink", "agents.detected.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _DETECTED_CACHE["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # 防御：文件可能是数组/字符串（用户误写或旧格式）→ 视为无缓存，绝不炸
            _DETECTED_CACHE["data"] = data if isinstance(data, dict) else {}
            _DETECTED_CACHE["mtime"] = mtime
        except Exception:
            _DETECTED_CACHE["data"] = {}
            _DETECTED_CACHE["mtime"] = mtime      # 坏文件只尝试一次，别每轮重读
    return _DETECTED_CACHE["data"] or {}


def _detected_by_id():
    by_id = detected_agents().get("byId")
    return by_id if isinstance(by_id, dict) else {}


def detected_exe(agent):
    """检测缓存里该成员的入口路径（没有/已失效则空串）。"""
    entry = _detected_by_id().get(agent)
    if not isinstance(entry, dict):
        return ""
    path = os.path.expandvars(entry.get("path") or "")
    return path if path and os.path.exists(path) else ""


def detected_node():
    """检测缓存里记录的 node 路径。"""
    for entry in _detected_by_id().values():
        if not isinstance(entry, dict):
            continue
        node = os.path.expandvars(entry.get("node") or "")
        if node and os.path.exists(node):
            return node
    return ""


def _looks_like_path(value):
    """判断一个值是否"路径形态"（裸命令名不算，便于与 PATH 解析区分）。"""
    v = str(value or "")
    if not v:
        return False
    if os.path.isabs(v):
        return True
    if len(v) > 2 and v[1] == ":" and v[2] in "\\/":
        return True
    return ("/" in v) or ("\\" in v)


def env_or_cfg(env_key, cfg_value, default=""):
    """环境变量优先，其次 config 里那一段（支持 %VAR%），最后 default。

    Windows 上 expandvars 对未定义的 %VAR% 原样返回，所以展开后还剩 % 就说明那个变量没配
    —— 据此跳过，而不是把一个字面量 %CLAUDELINK_X% 当成路径传下去。
    """
    for raw in (os.environ.get(env_key) if env_key else None, cfg_value):
        if raw is None or str(raw).strip() == "":
            continue
        val = os.path.expandvars(os.path.expanduser(str(raw).strip()))
        if val and "%" not in val:
            # 路径形态但已经不存在（CLI 升级换了目录/被卸载）→ 继续往下找，别把死路径当结果
            if _looks_like_path(val) and not os.path.exists(val):
                continue
            return val
    # default 同样要展开：claude 的默认 settings_dir 是 %USERPROFILE%\.ccm\settings
    return os.path.expandvars(os.path.expanduser(str(default)))


def cli_exe(agent, default=None):
    """该成员 CLI 的入口路径（未做 PATH 解析，交给 resolve_command）。

    顺序：环境变量 → config.agents.<名>.exe → **检测缓存**（npm 安装时 detect --write）
          → 裸名兜底（再交给 PATH / candidate_bin_dirs）。
    """
    cfg = (CONFIG.get("agents", {}) or {}).get(agent) or {}
    if default is None:
        default = CLI_DEFAULTS.get(agent, agent)
    fallback = detected_exe(agent) or default
    return env_or_cfg(CLI_ENV.get(agent, ""), cfg.get("exe"), fallback)


def cli_node(agent=None):
    """node 解释器路径（codebuddy / dsh 都以 `node <js入口>` 的方式调用）。

    agent 缺省沿用 codebuddy 的历史行为；按成员传可避免"codebuddy 的 node 配坏连带 dsh"。
    """
    cfg = (CONFIG.get("agents", {}) or {}).get(agent or "codebuddy") or {}
    fallback = CLI_NODE_DEFAULT or detected_node() or shutil.which("node") or "node"
    return env_or_cfg("CLAUDELINK_NODE", cfg.get("node"), fallback)


def claude_settings_dir():
    """claude 的 settings profile 目录（~/.ccm/settings 那套）。"""
    cfg = (CONFIG.get("agents", {}) or {}).get("claude") or {}
    return env_or_cfg("CLAUDELINK_CLAUDE_SETTINGS_DIR",
                      cfg.get("settings_dir"), CLAUDE_SETTINGS_DEFAULT)


def exe_missing_hint(agent, exe):
    """可执行文件找不到时给用户的修法（别只丢一个 WinError 2）。"""
    looked = "; ".join(candidate_bin_dirs()[:6])
    env_key = CLI_ENV.get(agent or "", "")
    where = f"（{exe}；PATH 和常见安装目录都没找到）" if agent else f"（{exe}；PATH 和常见安装目录都没找到）"
    if agent and env_key:
        fix = (f"修法：① 跑 `claudelink-hub detect --write` 自动识别并写入 "
               f"~/.claudelink/agents.detected.json；② 或改环境变量 {env_key} 指向真实入口"
               f"（config.json 里 agents.{agent}.exe 现在是 %{env_key}%，改完重启中枢即生效）；")
    else:
        fix = "修法：① 跑 `npx claudelink-hub detect --write` 自动识别；② 或把 config.json 里该成员的 exe 写成绝对路径；"
    return (
        f"找不到可执行文件：{where}\n"
        f"{fix}或把该目录加进系统 PATH，然后重启中枢。已找过：{looked}"
    )


def _quote_win_arg(value):
    """cmd.exe 下的参数引号：含空白/特殊符号才加引号，内部引号翻倍。"""
    v = str(value)
    if not v or any(ch in v for ch in ' \t"&|<>^'):
        return '"' + v.replace('"', '""') + '"'
    return v


def _win_short_path(path):
    """取 Win32 8.3 短路径（把空格之类换成 ~1）；拿不到就原样返回。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260)
        return buf.value if 0 < n < 260 else path
    except Exception:
        return path


def wrap_command(cmd):
    """cmd/bat 不能直接被 createProcess 执行：优先换成它包装的原生 exe，否则套 cmd /c。"""
    if os.name != "nt" or not cmd:
        return cmd
    exe = cmd[0]
    if exe.lower().endswith((".cmd", ".bat")):
        shim_dir = os.path.dirname(exe)
        stem = os.path.splitext(os.path.basename(exe))[0].lower()
        if stem == "codex":
            script = os.path.join(shim_dir, "node_modules", "@openai", "codex", "bin", "codex.js")
            if os.path.isfile(script):
                return [os.environ.get("CLAUDELINK_NODE") or shutil.which("node") or "node", script] + list(cmd[1:])
        candidates = [os.path.join("node_modules", ".bin", stem + ".exe")]
        if stem == "claude":
            candidates.insert(0, os.path.join("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"))
        for rel in candidates:
            candidate = os.path.join(shim_dir, rel)
            if os.path.exists(candidate):
                return [candidate] + cmd[1:]
        # ★ 含空格的 .cmd 路径是雷区（实测 A/B 两种 list 写法都坏）：
        #   ① 优先用 8.3 短路径（无空格 → 原始 list 形式就安全）
        short = _win_short_path(exe)
        if " " not in short:
            return [os.environ.get("ComSpec", "cmd.exe"), "/d", "/s", "/c", short] + cmd[1:]
        #   ② 8.3 被禁用：只能把整条命令作为**一个字符串**交给 Popen
        #      （list 形式会被 list2cmdline 把内层引号转义成 \"，cmd 不认）
        comspec = os.environ.get("ComSpec", "cmd.exe")
        argstr = " ".join(_quote_win_arg(a) for a in cmd[1:])
        return comspec + ' /d /s /c ""' + exe + '"' + ((" " + argstr) if argstr else "") + '"'
    return cmd


# ------------------------------------------------------------------ 附件解析
_PARSER_PY = None


def parser_python():
    """挑一个能 import pypdf 的解释器；找不到就用当前解释器（stdlib 兜底）。"""
    global _PARSER_PY
    if _PARSER_PY:
        return _PARSER_PY
    candidates = []
    configured = (cfg_get("uploads", {}) or {}).get("parser_python")
    if configured:
        candidates.append(os.path.expandvars(configured))
    bases = [
        os.path.join(os.path.expanduser("~"), ".cache", "codex-runtimes"),   # 实际位置
        os.path.join(os.environ.get("LOCALAPPDATA", ""), ".cache", "codex-runtimes"),
    ]
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            if "python.exe" in files and root.replace("/", "\\").endswith("\\dependencies\\python"):
                candidates.append(os.path.join(root, "python.exe"))
    candidates.append(sys.executable)
    for exe in candidates:
        if not exe or not os.path.exists(exe):
            continue
        try:
            probe = subprocess.run([exe, "-c", "import pypdf"],
                                   capture_output=True, timeout=40, creationflags=NO_WINDOW)
            if probe.returncode == 0:
                _PARSER_PY = exe
                return exe
        except Exception:
            continue
    _PARSER_PY = sys.executable
    return _PARSER_PY


def extract_file(path, max_chars):
    exe = parser_python()
    script = os.path.join(HUB_DIR, "extract.py")
    if not os.path.exists(script):
        return {"kind": "error",
                "text": "（附件解析器 extract.py 缺失：请重装 claudelink-hub，或把 extract.py 放回包目录）",
                "meta": {}}
    try:
        proc = subprocess.run(
            [exe, script, path, "--max-chars", str(max_chars)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, creationflags=NO_WINDOW,
        )
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "text": f"（解析失败：{exc}）", "meta": {}}
    return {"kind": "error", "text": "（解析器没有返回结果）", "meta": {}}


def human_size(size):
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ------------------------------------------------------------ 子进程台账（防孤儿）
CHILD_PID_FILE = os.path.join(DATA_DIR, "hub.pids.json")
_PIDS_LOCK = threading.Lock()
_CHILD_HINTS = ("claude", "codex", "node", "workbuddy", "codebuddy")


def load_child_pids():
    try:
        with open(CHILD_PID_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return [int(x) for x in data if str(x).isdigit()]
    except Exception:
        return []


def save_child_pids(pids):
    try:
        data = json.dumps(sorted(set(int(p) for p in pids)))
        tmp = CHILD_PID_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, CHILD_PID_FILE)
    except Exception:
        pass


def process_image_path(pid):
    """取进程可执行文件路径（Windows ctypes；POSIX /proc 或 ps）。"""
    if os.name != "nt":
        try:
            with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
                raw = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if raw:
                return raw
        except Exception:
            pass
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                                 capture_output=True, text=True, timeout=5)
            return (out.stdout or "").strip()
        except Exception:
            return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def kill_pid(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW, timeout=20)
        else:
            try:
                os.kill(int(pid), signal.SIGTERM)
                time.sleep(0.2)
            except ProcessLookupError:
                return True
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True
    except Exception:
        return False


def _popen_session_kwargs():
    """让成员进程自成一个可整体回收的进程组/会话。"""
    if os.name == "nt":
        return {"creationflags": NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"creationflags": NO_WINDOW, "start_new_session": True}


def terminate_tree(proc, timeout=5):
    """终止 proc 及其整棵进程树；Windows 用 taskkill /T，POSIX 用进程组。"""
    if proc is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(int(proc.pid)), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW, timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def sweep_stale_children():
    """启动时清掉上一次（可能是硬杀）留下的成员子进程，避免堆积。"""
    pids = load_child_pids()
    killed = []
    for pid in pids:
        path = process_image_path(pid)
        if not path:
            continue
        low = path.lower()
        if any(hint in low for hint in _CHILD_HINTS):
            if kill_pid(pid):
                killed.append(pid)
    save_child_pids([])
    return killed


# -------------------------------------------------------------------- http calls
def http_json(url, payload, headers, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        detail = scrub_secrets(err.read().decode("utf-8", "replace")[:400])
        raise RuntimeError(f"HTTP {err.code}: {detail}") from None
    except urllib.error.URLError as err:
        raise RuntimeError(f"网络错误: {err.reason}") from None


def normalize_usage(style, u, input_includes_cache=None):
    """把 provider 报的 usage 归一成**四个互不重叠的桶**（deepseek harness 的口径）。

    `input_includes_cache`：该**端点**报的 input 是不是"总 prompt 量"（含命中）。这是**逐端点**的属性，
    不是逐协议的 —— 实测过两个 Anthropic 协议的端点，结论正好相反（都记在 `.verify/sec26.md`）：

    - `api.deepseek.com/anthropic`：**不含**命中。同一 prompt 连发两次报
      `input=892 read=0` → `input=252 read=640`，input 掉了整整 640（=命中的增量）。
    - CodeBuddy 的 Tencent 网关：**含**命中。同一 prompt 连发两次报
      `input=28012 read=3072` → `input=28012 read=27776`，input 一个字都没变；
      而且第一次 `read + write = 3072 + 24940 = 28012 = input` —— 明摆着是子集关系。
    - OpenAI 协议（`api.deepseek.com/v1`）：**含**命中（`prompt=892 hit=640`，890+ 不变）。
    - codex（app-server）：**含**命中（`11246 + 2 = 11248`，见 `.verify/raw-codex.jsonl`）。

    所以按协议猜一定会错：**含命中时 `in = input - read - write`，不含时 `in = input` 原样**。
    谁含谁不含由 `usage_input_includes_cache(agent)` 从配置里取，默认值只是兜底。
    """
    if not isinstance(u, dict):
        return {}
    if input_includes_cache is None:
        input_includes_cache = (style != "anthropic")   # 兜底：Anthropic 当不含、其余当含
    out = {}
    if style == "anthropic":
        total = u.get("input_tokens")
        read = u.get("cache_read_input_tokens")
        write = u.get("cache_creation_input_tokens")
        if total is not None:
            hit = int(read or 0) + int(write or 0)
            out["in_tokens"] = max(0, int(total) - hit) if input_includes_cache else int(total)
        if read is not None:
            out["cache_read_tokens"] = int(read or 0)
        if write is not None:
            out["cache_write_tokens"] = int(write or 0)
    else:
        cached = None
        details = u.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            cached = details["cached_tokens"]
        elif u.get("prompt_cache_hit_tokens") is not None:
            cached = u["prompt_cache_hit_tokens"]
        if u.get("prompt_tokens") is not None:
            total = int(u["prompt_tokens"])
            hit = int(cached or 0)
            out["in_tokens"] = max(0, total - hit) if input_includes_cache else total
            if cached is not None:
                out["cache_read_tokens"] = hit
    if u.get("output_tokens") is not None:
        out["out_tokens"] = int(u["output_tokens"])
    elif u.get("completion_tokens") is not None:
        out["out_tokens"] = int(u["completion_tokens"])
    return out


def usage_input_includes_cache(agent=None, style=None):
    """该成员的端点报的 input 是否含缓存命中。

    优先看配置（`agents.<名>.usage_input_includes_cache`，自定义模型看
    `model_overrides.<id>.usage_input_includes_cache`），没配就按风格兜底：
    `anthropic` 当"不含"、其余当"含"。

    **必须能按成员配**：同是 Anthropic 协议，DeepSeek 自己的端点是"不含"、
    CodeBuddy 的 Tencent 网关是"含"，只看协议会把其中一个算错一倍。
    """
    if agent:
        val = cfg_agent(agent).get("usage_input_includes_cache")
        if isinstance(val, bool):
            return val
    return (style != "anthropic") if style is not None else False


def call_model_stream(style, base_url, api_key, model, messages, system="", max_tokens=1024,
                      timeout=180, path=None, on_text=None, temperature=None, usage_out=None,
                      cancel=None, on_think=None, input_includes_cache=None, effort=None):
    """流式调用；每收到一段文本就 on_text(片段)，返回完整文本。

    `usage_out`：传入 dict 就地把 provider 报的 tokens 填进去（in_tokens/out_tokens）。
    返回签名不变 —— 这个函数有多个调用点，改返回值会静默解包错位。
    """
    url = endpoint(base_url, style, path)
    parts = []
    thinking = []
    stop_reason = None

    def emit(chunk):
        if not chunk:
            return
        parts.append(chunk)
        if on_text:
            on_text(chunk)

    def emit_think(chunk):
        # 思维链实时出口（DeepSeek 系 reasoning_content / Anthropic 系 thinking_delta）
        if not chunk:
            return
        thinking.append(chunk)
        if on_think:
            on_think(chunk)

    if style == "anthropic":
        body = {"model": model, "max_tokens": int(max_tokens or 1024), "messages": messages, "stream": True}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = float(temperature)
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Accept": "text/event-stream"}
    else:
        body = {"model": model, "messages": ([{"role": "system", "content": system}] if system else []) + messages,
                "max_tokens": int(max_tokens or 1024), "stream": True}
        if temperature is not None:
            body["temperature"] = float(temperature)
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}

    if effort:
        body["output_config" if style == "anthropic" else "reasoning_effort"] = {"effort": effort} if style == "anthropic" else effort
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():   # 停止键：立刻收口
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if style == "anthropic":
                    if obj.get("type") == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            emit(delta.get("text") or "")
                        elif delta.get("type") == "thinking_delta":
                            emit_think(delta.get("thinking") or "")
                    elif obj.get("type") == "message_start":
                        # §5.2：input 在 message_start、output 在 message_delta，分两处来，
                        # 取其一都是错的 —— 这里两个都要接住。缓存两桶也随 message_start 来。
                        u = (obj.get("message") or {}).get("usage") or {}
                        if usage_out is not None:
                            usage_out.update(normalize_usage(style, u, input_includes_cache))
                    elif obj.get("type") == "message_delta":
                        u = obj.get("usage") or {}
                        if usage_out is not None:
                            usage_out.update(normalize_usage(style, u, input_includes_cache))
                        stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
                else:
                    if usage_out is not None and obj.get("usage"):
                        usage_out.update(normalize_usage(style, obj["usage"], input_includes_cache))
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        emit(delta.get("content") or "")
                        if delta.get("reasoning_content"):
                            emit_think(delta["reasoning_content"])
                        if choice.get("finish_reason"):
                            stop_reason = choice["finish_reason"]
    except urllib.error.HTTPError as err:
        detail = scrub_secrets(err.read().decode("utf-8", "replace")[:400])
        raise RuntimeError(f"HTTP {err.code}: {detail}") from None
    except urllib.error.URLError as err:
        raise RuntimeError(f"网络错误: {err.reason}") from None
    if usage_out is not None and stop_reason:
        # 让调用方看得见"为什么停"：max_tokens/length = 被上限截断，不是模型说完了
        usage_out["stop_reason"] = stop_reason
    text = "".join(parts).strip()
    if not text:
        reason = "".join(thinking).strip()
        if reason:
            text = (
                "（模型本轮只输出了思考内容，没有正文——通常是「单次最大输出」被思考占满；"
                "把该成员的 max_tokens / 单次最大输出调大即可）\n\n" + reason
            )
        else:
            raise RuntimeError(f"返回为空（stop_reason={stop_reason}）")
    return text


VERSION_SEG = re.compile(r"/v\d+[a-z0-9]*$", re.IGNORECASE)


def endpoint(base_url, style, path=None):
    """拼接真实接口地址。path 可显式给出（如 /chat/completions 或完整 URL）。"""
    base = (base_url or "").strip().rstrip("/")
    path = (path or "").strip()
    if path:
        if path.lower().startswith("http"):
            return path
        return base + (path if path.startswith("/") else "/" + path)
    if style == "anthropic":
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1") or VERSION_SEG.search(base):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def call_model(style, base_url, api_key, model, messages, system="", max_tokens=1024,
               timeout=180, path=None, temperature=None, usage_out=None,
               input_includes_cache=None, effort=None):
    """messages: [{'role': 'user'|'assistant', 'content': str}] -> text

    `usage_out`：传入 dict 就地把 provider 报的 tokens 填进去（in_tokens/out_tokens）。
    """
    url = endpoint(base_url, style, path)
    if style == "anthropic":
        body = {
            "model": model,
            "max_tokens": int(max_tokens or 1024),
            "messages": messages,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = float(temperature)
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        if effort:
            body["output_config"] = {"effort": effort}
        data = http_json(url, body, headers, timeout)
        if usage_out is not None:
            usage_out.update(normalize_usage("anthropic", data.get("usage") or {}, input_includes_cache))
        parts = data.get("content") or []
        text = "\n".join(
            p.get("text", "") for p in parts
            if isinstance(p, dict) and p.get("type") != "thinking"
        ).strip()
        if not text:
            thinking = "\n".join(
                p.get("thinking", "") for p in parts
                if isinstance(p, dict) and p.get("type") == "thinking"
            ).strip()
            if thinking:
                text = ("（模型本轮只输出了思考内容，没有正文——通常是 max_tokens 被思考占满）\n\n" + thinking)
        if not text:
            raise RuntimeError(f"返回为空：{json.dumps(data, ensure_ascii=False)[:300]}")
        return text
    body = {"model": model, "messages": ([{"role": "system", "content": system}] if system else []) + messages}
    body["max_tokens"] = int(max_tokens or 1024)
    if temperature is not None:
        body["temperature"] = float(temperature)
    if effort:
        body["reasoning_effort"] = effort
    data = http_json(url, body, {"Authorization": f"Bearer {api_key}"}, timeout)
    if usage_out is not None:
        usage_out.update(normalize_usage("openai", data.get("usage") or {}, input_includes_cache))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"返回为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    message = choices[0].get("message") or {}
    text = (message.get("content") or "").strip()
    if not text and message.get("reasoning_content"):
        text = ("（模型本轮只输出了思考内容，没有正文——通常是 max_tokens 被思考占满）\n\n"
                + str(message["reasoning_content"]).strip())
    if not text:
        raise RuntimeError(f"返回为空：{json.dumps(data, ensure_ascii=False)[:300]}")
    return text


# ------------------------------------------------------------------------- agents
ROLE_SYSTEM = (
    "你在一个本地群聊里充当成员 {label}，同群的还有 user（人）以及其他 AI 成员。"
    "你会直接收到投递给你的消息。回答保持简洁、直接、可执行；需要别人参与就在回复中 @ 对方。"
    "【编码】Windows 命令行的默认码页会把 UTF-8 文件读成乱码：要看文件内容优先用文件读取工具；"
    "必须走 shell 时，先 `chcp 65001`，PowerShell 用 `Get-Content -Encoding UTF8`，"
    "或 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。中文输出出现乱码时先换上面的写法重跑一次再说。"
)


class Cancelled(Exception):
    """本轮被用户手动中断。"""


class TraceSink:
    """一个 turn 的写入句柄（§1 持久事实层）。trace.enabled=false 时整体 no-op。

    事件协议（runner → sink）：llm_start / llm_usage / llm_end / tool_start /
    tool_end / tool_result / thinking / text / note。逐字增量**不**走这里（§11）。
    """

    def __init__(self, hub, agent, depth=0, trace_id=None, parent_turn_id=None,
                 trigger_kind="user", trigger_msg_id=None, trigger_by=None,
                 model=None, session_id=None, agent_id=None, conv_id=None):
        self.hub = hub
        self.enabled = trace_enabled()
        self.id = uuid.uuid4().hex
        self.trace_id = trace_id or uuid.uuid4().hex
        self.open_steps = {}        # key -> step_id（开放式 step 的配对钥匙）
        self.error = None           # 最近一次 trace 写入失败（不许静默，见 on_event）
        self.agent = agent          # 显示名（人看的）
        self.agent_id = agent_id or agent   # 路由 id（权限挂靠等内部用途）
        self._model_set = False     # turn.model 是否已从 stream 回填过
        if not self.enabled:
            return
        row = hub.store.open_turn(
            self.id, self.trace_id, agent, depth=depth, parent_turn_id=parent_turn_id,
            trigger_kind=trigger_kind, trigger_msg_id=trigger_msg_id, trigger_by=trigger_by,
            model=model, session_id=session_id, agent_id=self.agent_id,
            conv_id=conv_id,
        )
        hub.broadcast({"type": "turn_started", "turn": row})

    # -- 落库 -------------------------------------------------------------
    def step(self, kind, name=None, status="ok", detail=None, in_tokens=None, out_tokens=None,
             cache_read_tokens=None, cache_write_tokens=None):
        if not self.enabled:
            return None
        row = self.hub.store.add_step(self.id, kind, name, status, detail, in_tokens, out_tokens,
                                      cache_read_tokens, cache_write_tokens)
        self.hub.broadcast({"type": "step", "turn_id": self.id, "step": row})
        return row["id"]

    def open(self, kind, name=None, detail=None, key=None):
        if not self.enabled:
            return None
        row = self.hub.store.add_step(self.id, kind, name, "running", detail)
        self.hub.broadcast({"type": "step", "turn_id": self.id, "step": row})
        if key:
            self.open_steps[key] = row["id"]
        return row["id"]

    def end(self, key, status="ok", detail=None, in_tokens=None, out_tokens=None,
            cache_read_tokens=None, cache_write_tokens=None):
        if not self.enabled:
            return
        step_id = self.open_steps.pop(key, None)
        if step_id is None and key is None:
            tool_keys = [k for k in self.open_steps if k.startswith("tool:")]
            if tool_keys:
                step_id = self.open_steps.pop(tool_keys[-1])
        if step_id is None:
            return
        if detail is not None:
            previous = self.hub.store.get_step(step_id)
            try:
                old_detail = json.loads(previous.get("detail") or "{}") if previous else {}
                new_detail = json.loads(detail) if isinstance(detail, str) else detail
                if isinstance(old_detail, dict) and isinstance(new_detail, dict):
                    detail = {**old_detail, **new_detail}
            except (ValueError, TypeError):
                pass
        row = self.hub.store.end_step(step_id, status, detail, in_tokens, out_tokens,
                                     cache_read_tokens, cache_write_tokens)
        if row:
            self.hub.broadcast({"type": "step", "turn_id": self.id, "step": row})

    def usage(self, key="llm", in_tokens=None, out_tokens=None,
              cache_read_tokens=None, cache_write_tokens=None):
        """一次 API 调用的 usage 回写到还没关的 step 上（不关 step）。

        Claude 的 usage 分两处来（message_start 给 input、message_delta 给 output），
        所以是一路补写、最后一次 llm_end 才收口 —— 一个 turn 内 N 次调用各占一行（§5.2）。
        缓存两桶也在补写之列：一次**完全命中**的调用可能只回 cache_read，
        in/out 都是 0 占位（runner 会刻意丢掉占位，见 run_claude 里那段注释），
        守卫只看 in/out 就会把这次更新整个丢掉。
        """
        if not self.enabled or all(v is None for v in
                                   (in_tokens, out_tokens, cache_read_tokens, cache_write_tokens)):
            return
        step_id = self.open_steps.get(key)
        if step_id is None:
            return
        row = self.hub.store.set_step_usage(step_id, in_tokens, out_tokens,
                                            cache_read_tokens, cache_write_tokens)
        if row:
            self.hub.broadcast({"type": "step", "turn_id": self.id, "step": row})

    def close(self, status, *, end_ts, reply_msg_id=None, error_text=None,
              in_chars=None, out_chars=None):
        """收尾。幂等由 `Store.settle_turn` 保证：status 只从 running 迁出一次，
        `end_ts` 首次写入者胜，`reply_msg_id` 只在为空时补写（§7.3）。"""
        if not self.enabled:
            return None
        self.open_steps.clear()
        row = self.hub.store.settle_turn(
            self.id, status, end_ts=end_ts, reply_msg_id=reply_msg_id,
            error_text=error_text, in_chars=in_chars, out_chars=out_chars,
        )
        self.hub.broadcast({"type": "turn_ended", "turn": row})
        return row

    # -- runner 事件 -------------------------------------------------------
    def on_event(self, kind, payload):
        if not self.enabled:
            return
        payload = payload or {}
        try:
            if kind == "llm_start":
                if payload.get("model") and not self._model_set:
                    self._model_set = True     # 只补一次，别每次调用都写库
                    self.hub.store.set_turn_model(self.id, payload.get("model"))
                self.end("llm", "ok")          # 上一次调用没收到 end，先收敛
                self.open("llm_call", payload.get("model"),
                          {"phase": "start"}, key="llm")
            elif kind == "llm_usage":
                self.usage("llm", payload.get("in_tokens"), payload.get("out_tokens"),
                           payload.get("cache_read_tokens"), payload.get("cache_write_tokens"))
            elif kind == "llm_end":
                in_tokens = payload.get("in_tokens")
                out_tokens = payload.get("out_tokens")
                cr = payload.get("cache_read_tokens")
                cw = payload.get("cache_write_tokens")
                detail = {"phase": "end"}
                if payload.get("usage_keys"):
                    detail["usage_keys"] = payload["usage_keys"]   # 挖不到 provider 真值时的线索
                if "llm" not in self.open_steps:
                    self.step("llm_call", payload.get("model"), "ok",
                              detail, in_tokens, out_tokens, cr, cw)
                else:
                    self.end("llm", "ok", detail, in_tokens, out_tokens, cr, cw)
            elif kind == "tool_start":
                key = f"tool:{payload.get('id') or payload.get('name') or len(self.open_steps)}"
                self.open("tool_call", payload.get("name"),
                          tool_detail(payload.get("name"), payload.get("input")), key=key)
            elif kind == "tool_end":
                # 入参在 content_block_stop / item/completed 才齐（claude 系是流式拼出来的），
                # 所以真正的 detail 在这里落；tool_detail 无内容时返回 None，
                # COALESCE 保证不会把 tool_start 已经写好的 detail 覆盖成空。
                key = f"tool:{payload.get('id') or payload.get('name')}" if payload.get("id") or payload.get("name") else None
                detail = tool_detail(payload.get("name"), payload.get("input"), payload.get("output"))
                status = "error" if payload.get("error") else "ok"
                if key is not None and key not in self.open_steps:
                    self.step("tool_call", payload.get("name"), status, detail)
                else:
                    self.end(key, status, detail)
            elif kind == "tool_result":
                self.step("tool_result", payload.get("name"),
                          "error" if payload.get("error") else "ok",
                          {"content": payload.get("content")})
            elif kind == "thinking":
                self.step("thinking", None, "ok", {"text": payload.get("text")})
            elif kind == "text":
                self.step("text", None, "ok", {"text": payload.get("text")})
            elif kind == "note":
                self.step("note", payload.get("name"), "ok", {"text": payload.get("text")})
        except Exception as exc:   # trace 写失败不影响主流程，但**不许静默**（否则协议缺方法也看不见）
            self.error = f"{kind}: {type(exc).__name__}: {exc}"
            try:
                self.hub.trace_error = self.error
            except Exception:
                pass


HISTORY_HEAD = (
    "[群聊最近记录（这是你唯一的上下文来源，以这里的内容为准）]\n"
    "注：被【★重要★ … ★】包住的部分是用户用鼠标选中的重点内容，必须优先满足；"
    "整条前带 ★重要★ 的表示整条都重要。不得忽略或改写这些约束。\n"
)


def role_head(agent_label, others, shared_context=""):
    """prompt 的**稳定开头**（角色 + 成员表 + 群公告）。

    这里绝不能放每轮会变的东西（谁的发言、投递给谁）—— 那是 role_tail 的活。
    起因：原先 `sender`/`target` 就写在这一段里，于是 prompt 的第 0 个字节每轮都不同，
    前缀缓存**必然全 miss**（实测整个 prompt 2～3 万字，命中率只有 12%，
    而且那 12% 还是 CLI 自带 system prompt 贡献的）。见 README「缓存命中」。
    """
    names = "、".join(others) if others else "（暂无其他人）"
    head = ""
    if shared_context.strip():
        head = f"[群公告 / 共享上下文]\n{shared_context.strip()}\n\n"
    return head + (
        f"[本地群聊] 成员：user（人）、{names}。你是 {agent_label}。"
        "需要其他成员参与时，在回复里用 @成员id 点名（如 @claude、@dsh；带空格的 label 也兼容）；不需要就正常回答。\n\n"
    )


def role_tail(sender, target, text, extra_instruction=None):
    """prompt 的**易变尾部**：谁说的、投递给谁、本次消息本身。

    放在最后是有意的 —— provider 的前缀缓存从第 0 字节开始匹配到第一处不同为止，
    易变内容越靠后，能命中的前缀就越长。`extra_instruction`（auto 模式的调度指令）
    也在这里，它每轮都不同。
    """
    return (f"[本次消息] 来自 {sender}，面向对象：{target}。\n{text}"
            + (extra_instruction or ""))


def run_claude(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
               on_event=None, on_think=None, system_extra=None):
    # 思维链单独走 on_think（没给就退回老行为，混进正文一起显示）
    think_out = on_think if on_think is not None else on_text
    def _ev(kind, payload=None):
        """trace 事件出口。逐字增量绝不走这里（§11），只发结构化事件。"""
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    cfg = CONFIG["agents"][agent]
    settings_dir = claude_settings_dir()
    profile = cfg.get("settings_profile")
    cmd = [resolve_command(cli_exe("claude"))]
    if profile:
        cmd += ["--settings", os.path.join(settings_dir, profile + ".json")]
    if cfg.get("model"):
        cmd += ["--model", str(cfg["model"])]        # 成员面板里选的模型（覆盖 profile 里的默认）
    if cfg.get("effort"):
        cmd += ["--effort", str(cfg["effort"])]
    # prompt 一律走 stdin：Windows 命令行上限约 32KB，「无限上下文」很容易超过（WinError 206）
    mode = HUB.permission_mode(agent)
    if mode == "read-only":
        perm_args = ["--permission-mode", "plan", "--restricted", "--permission-prompts", "none"]
    elif mode == "danger":
        perm_args = ["--permission-mode", "bypassPermissions"]
    else:
        # workspace-write：auto 由分类器放行常用命令，避免 -p 下 Bash 全被拒；
        # 这不是文件系统沙箱，UI/README 标 classifier。
        perm_args = ["--permission-mode", "auto", "--permission-prompts", "none"]
    cmd += ["-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages"] + perm_args
    if not HUB.streaming_on(agent):
        cmd = [x for x in cmd if x not in ("stream-json", "--verbose", "--include-partial-messages")]
        cmd.insert(cmd.index("--output-format") + 1, "json")
    system_txt = ROLE_SYSTEM.format(label=LABELS.get(agent, agent))
    if system_extra:
        system_txt += "\n\n" + str(system_extra)
    cmd += ["--append-system-prompt", system_txt]
    if mode != "read-only":
        for extra_dir in HUB.grants_rw(agent):
            cmd += ["--add-dir", extra_dir]
    cmd = wrap_command(cmd)
    forms = []
    if session_id:
        forms.append(cmd + ["--resume", session_id])
    forms.append(cmd)
    argv_fallback = len(prompt) < 20000      # 提示词不长时保留旧的 argv 形式做兜底
    last_err = ""
    for form in forms:
        state = {"text": "", "session": session_id, "result": "", "thinking": ""}
        state["phase"] = None

        def on_line(line, _state=state):
            stripped = line.strip()
            if not stripped.startswith("{"):
                return
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return
            if obj.get("session_id"):
                _state["session"] = obj["session_id"]
            if obj.get("type") == "stream_event":
                event = obj.get("event") or {}
                etype = event.get("type")
                if etype == "message_start":
                    message = event.get("message") or {}
                    _state["tools"] = {}
                    _ev("llm_start", {"model": message.get("model")})
                    usage = message.get("usage") or {}
                    # 真 Anthropic：input 在 message_start 给。
                    # workbuddy 系（hy4-preview）的 message_start 是**全零占位**
                    # （实测 .verify/raw-codebuddy.jsonl：input_tokens=0），真值在
                    # message_delta —— 所以这里只认真值（>0），别把占位 0 写进列里，
                    # 那正是 CodeBuddy 链 `in_tokens` 恒 0 的成因。
                    if usage.get("input_tokens"):
                        _ev("llm_usage", {"in_tokens": usage.get("input_tokens")})
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    # 两处都取：缺失的一侧传 None，sink 侧 COALESCE 保留已有值。
                    # 只取 output_tokens 会让 workbuddy 系的 input 永远丢（§5.2）。
                    if usage.get("input_tokens") or usage.get("output_tokens") is not None:
                        _ev("llm_usage", normalize_usage("anthropic", usage,
                                                       usage_input_includes_cache(agent, "anthropic")))
                elif etype == "message_stop":
                    _ev("llm_end", {})
                elif etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        # 工具调用不再往正文里塞 "[工具] X" 文字：界面按 step 行渲染（图标+标签）
                        _state.setdefault("tools", {})[event.get("index")] = {
                            "id": block.get("id"), "name": block.get("name")}
                        _ev("tool_start", {"id": block.get("id"), "name": block.get("name")})
                elif etype == "content_block_stop":
                    info = _state.setdefault("tools", {}).pop(event.get("index"), None)
                    if info:
                        _ev("tool_end", {
                            "id": info.get("id"), "name": info.get("name"),
                            "input": _parse_tool_json(
                                _state.setdefault("tool_json", {}).pop(event.get("index"), "")),
                        })
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "input_json_delta":
                        # tool_use 的入参是**流式**到达的：content_block_start 时
                        # content_block.input 还是空 {}，真值在 partial_json 里逐片拼。
                        # 不接这一支，Edit 的 old_string/new_string 永远进不了库 ——
                        # 截图里的红绿 diff 就没有数据源（这正是本轮的缺口）。
                        _buf = _state.setdefault("tool_json", {})
                        _idx = event.get("index")
                        _buf[_idx] = _buf.get(_idx, "") + (delta.get("partial_json") or "")
                    elif delta.get("type") == "text_delta" and delta.get("text"):
                        if _state["phase"] != "text":
                            _state["phase"] = "text"
                            if on_think is None:
                                on_text("\n【正文】\n" if _state["thinking"] else "")
                        _state["text"] += delta["text"]
                        on_text(delta["text"])
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        if _state["phase"] != "think":
                            _state["phase"] = "think"
                            if on_think is None:
                                on_text("【思考中】")
                        _state["thinking"] += delta["thinking"]
                        think_out(delta["thinking"])
            elif obj.get("type") == "result" and obj.get("result"):
                _state["result"] = obj["result"]

        rc, lines = stream_command(
            form, DEFAULT_CWD, turn_timeout(agent), on_line, cancel, on_proc,
            input_text=prompt,
        )
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        text = (state["result"] or state["text"]).strip()
        if not text and state["thinking"].strip():
            text = ("（模型本轮只输出了思考内容，没有正文；把该成员的 max_tokens / 单次最大输出调大通常可解决）\n\n"
                    + state["thinking"].strip())
        if text:
            return {"session_id": state["session"], "text": text}
        last_err = " / ".join(lines[-3:])
        if argv_fallback:
            rc, lines = stream_command(
                form + [prompt], DEFAULT_CWD, turn_timeout(agent), on_line, cancel, on_proc
            )
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            text = (state["result"] or state["text"]).strip()
            if text:
                return {"session_id": state["session"], "text": text}
    raise RuntimeError(f"claude 调用失败：{last_err[:300]}")


def pick_usage(obj, _depth=0):
    """从 codex app-server 的事件里挖本 turn 的 token 用量（§5.2/§5.3）。

    各版本字段名不一；`tokenUsage` 里 `total` 是整条 thread 的累计值，
    只有 `last` 是本 turn 的 —— 拿 total 当 turn 值会把历史轮次重复计费，
    所以先在 `last` 上取值。挖不到返回 {}，调用方据此留线索，不静默当 0。

    缓存两桶也在这里归一：codex 的 `inputTokens` **含**命中
    （`.verify/raw-codex.jsonl` 里 `11246 + 2 = 11248`，`cachedInputTokens` 是它的子集），
    所以未命中要减掉，否则命中量会被重复计一遍、命中率还偏高。
    """
    if not isinstance(obj, dict) or _depth > 4:
        return {}
    node = obj["last"] if isinstance(obj.get("last"), dict) else obj
    for key_in, key_out in (("inputTokens", "outputTokens"),
                            ("input_tokens", "output_tokens")):
        i, o = node.get(key_in), node.get(key_out)
        if i is None and o is None:
            continue
        out = {}
        # 两个命名族都要认：camelCase（新版）与 snake_case（老版）
        read = node.get("cachedInputTokens", node.get("cached_input_tokens"))
        write = node.get("cacheWriteInputTokens", node.get("cache_write_input_tokens"))
        if isinstance(i, (int, float)):
            hit = int(read) if isinstance(read, (int, float)) else 0
            wr = int(write) if isinstance(write, (int, float)) else 0
            out["in_tokens"] = max(0, int(i) - hit - wr)   # inputTokens 含命中
            if isinstance(read, (int, float)):
                out["cache_read_tokens"] = hit
            if isinstance(write, (int, float)):
                out["cache_write_tokens"] = wr
        if isinstance(o, (int, float)):
            out["out_tokens"] = int(o)
        return out
    for key in ("turn", "usage", "tokenUsage"):
        got = pick_usage(obj.get(key), _depth + 1)
        if got:
            return got
    return {}


def usage_keys_hint(obj):
    """挖不到 usage 时留线索：真跑一轮后 /api/trace 里直接能看到该版本的字段名。"""
    if not isinstance(obj, dict):
        return ""
    keys = sorted(obj.keys())
    turn = obj.get("turn")
    if isinstance(turn, dict):
        keys += ["turn." + k for k in sorted(turn.keys())]
    return ",".join(keys)[:200]


def run_codex(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
              on_event=None, on_think=None, system_extra=None):
    """app-server 偶发"启动即退出"（首次握手失败）→ 自动重试一次，别把错误直接抛给用户。"""
    if cfg_agent(agent).get("transport", "exec") != "app-server":
        from codex_cli import run
        return run(sys.modules[__name__], agent, prompt, session_id, on_text, cancel,
                   on_proc, on_event, on_think, system_extra)
    attempts = 2
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_codex_once(agent, prompt, session_id, on_text, cancel, on_proc,
                                   on_event, on_think, system_extra=system_extra)
        except Cancelled:
            raise
        except RuntimeError as exc:
            last_exc = exc
            msg = str(exc)
            transient = ("app-server 已退出" in msg) or ("无响应" in msg)
            if not transient or attempt >= attempts:
                raise
            if on_event is not None:
                try:
                    on_event("note", {"name": "codex retry", "text": f"app-server 首次握手失败，重试 {attempt + 1}/{attempts}"})
                except Exception:
                    pass
            time.sleep(2)
    raise last_exc if last_exc else RuntimeError("codex 调用失败")


def _run_codex_once(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
                    on_event=None, on_think=None, system_extra=None):
    def _ev(kind, payload=None):
        # trace 事件出口（codex app-server 的 item/* 事件）
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    cfg = CONFIG["agents"][agent]
    exe = resolve_command(cli_exe("codex"))
    # 成员面板里选的模型/provider：用 -c 覆盖（app-server 也认这个全局覆盖）
    overrides = []
    if cfg.get("model"):
        # 注意写法：argv 里必须是 model=xxx，**不要**加引号。
        # 探针实测（work\codex_model_probe.py）：不加引号时 thread.model=deepseek-v4-pro，
        # 带引号那份会被当成带引号的字符串而不生效，于是静默用回 config 默认。
        overrides += ["-c", f'model={cfg["model"]}']
    if cfg.get("model_provider"):
        overrides += ["-c", f'model_provider={cfg["model_provider"]}']
    if cfg.get("effort"):
        overrides += ["-c", "model_reasoning_effort=" + json.dumps(cfg["effort"])]
    if not HUB.streaming_on(agent):
        on_text = lambda _chunk: None      # 关掉流式：只留最终结果
    # app-server 的 stdio 通道才有 token 级增量（exec --json 只有整条消息）
    sandbox, cwd = HUB.codex_turn_policy(agent)
    try:
        proc = subprocess.Popen(
            # npm 全局安装的 codex 可能是 codex.cmd → 需要 wrap_command 才能启动
            wrap_command([exe] + overrides + ["app-server"]),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,                      # 二进制读，逐行容忍解码
            **_popen_session_kwargs(),
        )
    except FileNotFoundError:
        # 用户能看到的必须是一句"怎么修"，不是一个 WinError 2
        raise RuntimeError(exe_missing_hint(agent, exe)) from None
    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass
    inbox = queue.Queue()

    def reader():
        for line in proc.stdout:
                inbox.put(decode_stream_line(line))
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        # stdout 改成二进制读之后 stdin 也是二进制，必须自己编码
        # （2026-09-27 漏了这步，Codex 直接报 "a bytes-like object is required"）
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def next_obj(timeout):
        try:
            line = inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise RuntimeError("app-server 已退出")
        line = line.strip()
        if not line.startswith("{"):
            return {}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {}

    def wait_response(rid, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            obj = next_obj(0.5)
            if obj is None or not obj:
                continue
            if obj.get("id") == rid:
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"])[:200])
                return obj.get("result") or {}
        raise RuntimeError("codex app-server 无响应")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "claudelink-hub", "version": "1.0"}}})
        wait_response(1, 90)

        tid = None
        thread_model = None
        if session_id:
            try:
                send({"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": {"threadId": session_id}})
                res = wait_response(2, 60)
                tid = (res.get("thread") or {}).get("id") or res.get("threadId") or session_id
                thread_model = (res.get("thread") or {}).get("model") or res.get("model")
            except Exception:
                tid = None
        if not tid:
            send({"jsonrpc": "2.0", "id": 2, "method": "thread/start",
                  "params": {"cwd": cwd, "approvalPolicy": "never", "sandboxPolicy": sandbox}})
            res = wait_response(2, 90)
            tid = (res.get("thread") or {}).get("id") or res.get("threadId")
            thread_model = (res.get("thread") or {}).get("model") or res.get("model")
        if not tid:
            raise RuntimeError("无法创建 codex thread")

        send({"jsonrpc": "2.0", "id": 3, "method": "turn/start",
              "params": {"threadId": tid,
                         "input": ([{"type": "text", "text": prompt}]
                                   + [{"type": "localImage", "path": p}
                                      for p in (HUB.current_images.get(agent) or [])]),
                         "cwd": cwd, "approvalPolicy": "never", "sandboxPolicy": sandbox}})

        deltas, final_text, turn_id = [], "", None
        pending_usage = {}      # 见下面 thread/tokenUsage/updated 分支
        phase = None
        limit = turn_timeout(agent)
        deadline = None if limit == 0 else time.time() + limit
        started_at = time.time()
        last_event = started_at
        idle_exit = 15          # app-server 退出后允许的静默秒数
        while True:
            if cancel is not None and cancel.is_set():
                try:
                    send({"jsonrpc": "2.0", "id": 9, "method": "turn/interrupt",
                          "params": {"threadId": tid, "turnId": turn_id}})
                except Exception:
                    pass
                raise Cancelled()
            if deadline is not None and time.time() > deadline:
                raise RuntimeError("超时")
            obj = next_obj(0.5)
            if obj is None or not obj:
                if proc.poll() is not None and time.time() - last_event > idle_exit:
                    if final_text or deltas:
                        # 静默退出也要收口，否则 llm 步骤永远 running。
                        # 带上已收到的 usage —— app-server 常在 turn/completed 之前
                        # 就退出，丢掉它就等于这一轮的 token 永远是 0。
                        _ev("llm_end", dict(pending_usage))
                        break
                    raise RuntimeError("codex app-server 已退出，但没有返回任何结果")
                if time.time() - last_event > 60:
                    on_text(f"\n[仍在执行] 已运行 {int(time.time() - started_at)} 秒…\n")
                    last_event = time.time()
                continue
            last_event = time.time()
            method = obj.get("method")
            params = obj.get("params") or {}
            if method and obj.get("id") is not None:      # 服务端反向请求：一律拒绝
                send({"jsonrpc": "2.0", "id": obj["id"], "result": {}})
                continue
            if method == "turn/started":
                turn_id = params.get("turnId") or (params.get("turn") or {}).get("id") or turn_id
                # 开 llm 步骤：只靠 turn/completed 的 llm_end 建行，界面上一整轮
                # 「正在思考」都不会出现，且 turn.model 永远补不上（§3.1）
                turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                # model 名不在 turn/started 里：实测它在 thread/start 的响应
                # `thread.model`（.verify/raw-codex.jsonl）。thread/started 那条通知
                # 会被 wait_response 当无关对象吃掉，所以只能从响应里带下来 ——
                # 否则 turn.model 永远 NULL，按模型聚合成本这条就直接废掉。
                _ev("llm_start", {"model": params.get("model") or turn_obj.get("model")
                                           or thread_model})
            elif method == "thread/tokenUsage/updated":
                # ★ 实测（.verify/raw-codex.jsonl）：codex 的 token 用量**不在**
                # turn/completed 里 —— 后者的 params 只有 threadId/turn，
                # 真值是一条独立的 thread/tokenUsage/updated：
                #   params.tokenUsage.last.{inputTokens,outputTokens,cachedInputTokens,...}
                # 只读 turn/completed ⇒ pick_usage 恒空 ⇒ codex 永远 estimated/0。
                # 取 `last`（本 turn）不取 `total`（整条 thread 累计，会把历史轮次重复计费）。
                if not params.get("turnId") or turn_id in (None, params.get("turnId")):
                    got = pick_usage(params)
                    if got:
                        pending_usage = got
            elif method == "item/started":
                item = params.get("item") or {}
                kind_ = item.get("type")
                if kind_ and kind_ not in ("userMessage", "agentMessage", "reasoning"):
                    detail = (item.get("command") or item.get("path") or item.get("name")
                              or item.get("tool") or "")
                    # 同样不再往正文塞文字：工具调用由 step 行（图标+标签）呈现
                    # name 只放工具名，参数进 input（§3.1）—— 塞成
                    # 「commandExecution "C:\..."」会让界面上顶出一整行路径。
                    # input 带上原始字段而不是 str(detail)[:400]：那个截断会把命令
                    # 从中间切断，截图里的 Shell 命令块就永远展示不全；按字段截断
                    # 由 tool_detail 统一做。
                    raw_in = {k: item[k] for k in
                              ("command", "path", "name", "query", "pattern", "cwd",
                               "changes") if item.get(k)}
                    if not raw_in and detail:
                        raw_in = {"name": str(detail)[:400]}
                    _ev("tool_start", {"id": item.get("id"), "name": kind_, "input": raw_in})
            elif method == "item/agentMessage/delta":
                delta = params.get("delta") or ""
                if delta:
                    if phase != "text":
                        phase = "text"
                        if deltas and on_think is None:
                            on_text("\n【正文】\n")
                    deltas.append(delta)
                    on_text(delta)
            elif method == "item/reasoning/textDelta":
                delta = params.get("delta") or ""
                if delta and phase is None:
                    phase = "think"
                    if on_think is None:
                        on_text("【思考中】")
                if delta and phase == "think":
                    (on_think if on_think is not None else on_text)(delta)
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") not in ("userMessage", "agentMessage", "reasoning"):
                    # 命令的输出体与退出码也在这里回来 —— 截图里 Shell 块下面的
                    # 那几行就是它。exitCode 非 0 才是失败，别把 0 当 error。
                    out = item.get("aggregatedOutput")
                    if not isinstance(out, str):
                        out = item.get("output")
                    _ev("tool_end", {"id": item.get("id"), "name": item.get("type"),
                                     "output": out if isinstance(out, str) else None,
                                     "error": bool(item.get("exitCode"))})
                if item.get("type") == "agentMessage" and item.get("text"):
                    final_text = item["text"]
            elif method == "turn/completed":
                # §5.2：codex 的真值在 turn.usage（本 turn 的），不是 thread 累计。
                # 挖不到时带上字段名线索 —— 别静默当 0，那正是「永远 estimated」的成因。
                usage = pick_usage(params) or pending_usage
                if usage:
                    _ev("llm_end", usage)
                else:
                    _ev("llm_end", {"usage_keys": usage_keys_hint(params)})
                break
            elif method in ("turn/failed", "error"):
                raise RuntimeError(json.dumps(obj, ensure_ascii=False)[:300])

        text = (final_text or "".join(deltas)).strip()
        if not text:
            raise RuntimeError("codex 没有返回文本")
        return {"session_id": tid, "text": text}
    finally:
        terminate_tree(proc)


def _free_port():
    """拿一个当前空闲的 TCP 端口（绑定后立刻释放；有极小竞态窗口，够用）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def run_codebuddy(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
                  on_event=None, on_think=None, system_extra=None):
    think_out = on_think if on_think is not None else on_text
    def _ev(kind, payload=None):
        """trace 事件出口（同 run_claude）。"""
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    cfg = CONFIG["agents"][agent]
    node = resolve_command(cli_node(agent))
    # 以前这里是 cfg["exe"]：既没走环境变量也没 expandvars，路径一改就静默失败
    script = resolve_command(cli_exe("codebuddy"))
    system = ROLE_SYSTEM.format(label=LABELS.get(agent, agent))
    if system_extra:
        system += "\n\n" + str(system_extra)
    mode = HUB.permission_mode(agent)
    allow_bypass = bool((cfg.get("permissions") or {}).get("allow_bypass"))
    if mode == "read-only":
        perm_args = ["--permission-mode", "plan", "--tools", "Read,Grep,Glob"]
    elif mode == "danger":
        perm_args = ["--permission-mode", "bypassPermissions", "-y"]
    else:
        perm_args = ["--permission-mode", "acceptEdits", "--tools", "Read,Edit,Write,Bash,Grep,Glob"]
        if allow_bypass:
            perm_args += ["-y"]
    common = ["-p", "--output-format", "stream-json", "--include-partial-messages",
              *perm_args, "--append-system-prompt", system]
    # 无头实例必须钉死自己的 HTTP 端口：交互式 CodeBuddy 会话会占一个稳定端口，
    # 无头模式不传 --port 时也会去抢同一个端口 → EADDRINUSE 秒失败。
    common += ["--port", str(_free_port())]
    if cfg.get("model"):
        common += ["--model", str(cfg["model"])]     # 成员面板里选的模型
    if not HUB.streaming_on(agent):
        common = [x for x in common if x not in ("stream-json", "--include-partial-messages")]
        common.insert(common.index("--output-format") + 1, "json")
    # --add-dir 是可变参数，必须放在最后，否则会把后面的 positional 吞掉
    extra_dirs = HUB.grants_rw(agent)
    dir_args = (["--add-dir"] + extra_dirs) if extra_dirs else []
    forms = []
    if session_id:
        forms.append([node, script] + common + ["--resume", session_id] + dir_args)
    forms.append([node, script] + common + ["--session-id", session_id or str(uuid.uuid4())] + dir_args)
    argv_fallback = len(prompt) < 20000      # 提示词不长时保留 argv 形式兜底
    last_err = ""
    for form in forms:
        sid = session_id if "--resume" in form else form[form.index("--session-id") + 1]
        state = {"text": "", "result": "", "thinking": ""}
        state["phase"] = None

        def on_line(line, _state=state):
            stripped = line.strip()
            if not stripped.startswith("{"):
                return
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return
            if obj.get("type") == "stream_event":
                event = obj.get("event") or {}
                etype = event.get("type")
                if etype == "message_start":
                    message = event.get("message") or {}
                    _state["tools"] = {}
                    _ev("llm_start", {"model": message.get("model")})
                    usage = message.get("usage") or {}
                    # 真 Anthropic：input 在 message_start 给。
                    # workbuddy 系（hy4-preview）的 message_start 是**全零占位**
                    # （实测 .verify/raw-codebuddy.jsonl：input_tokens=0），真值在
                    # message_delta —— 所以这里只认真值（>0），别把占位 0 写进列里，
                    # 那正是 CodeBuddy 链 `in_tokens` 恒 0 的成因。
                    if usage.get("input_tokens"):
                        _ev("llm_usage", {"in_tokens": usage.get("input_tokens")})
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    # 两处都取：缺失的一侧传 None，sink 侧 COALESCE 保留已有值。
                    # 只取 output_tokens 会让 workbuddy 系的 input 永远丢（§5.2）。
                    if usage.get("input_tokens") or usage.get("output_tokens") is not None:
                        _ev("llm_usage", normalize_usage("anthropic", usage,
                                                       usage_input_includes_cache(agent, "anthropic")))
                elif etype == "message_stop":
                    _ev("llm_end", {})
                elif etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        # 同 run_claude：工具调用不再混进正文文字
                        _state.setdefault("tools", {})[event.get("index")] = {
                            "id": block.get("id"), "name": block.get("name")}
                        _ev("tool_start", {"id": block.get("id"), "name": block.get("name")})
                elif etype == "content_block_stop":
                    info = _state.setdefault("tools", {}).pop(event.get("index"), None)
                    if info:
                        _ev("tool_end", {
                            "id": info.get("id"), "name": info.get("name"),
                            "input": _parse_tool_json(
                                _state.setdefault("tool_json", {}).pop(event.get("index"), "")),
                        })
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "input_json_delta":
                        # tool_use 的入参是**流式**到达的：content_block_start 时
                        # content_block.input 还是空 {}，真值在 partial_json 里逐片拼。
                        # 不接这一支，Edit 的 old_string/new_string 永远进不了库 ——
                        # 截图里的红绿 diff 就没有数据源（这正是本轮的缺口）。
                        _buf = _state.setdefault("tool_json", {})
                        _idx = event.get("index")
                        _buf[_idx] = _buf.get(_idx, "") + (delta.get("partial_json") or "")
                    elif delta.get("type") == "text_delta" and delta.get("text"):
                        if _state["phase"] != "text":
                            _state["phase"] = "text"
                            if on_think is None:
                                on_text("\n【正文】\n" if _state["thinking"] else "")
                        _state["text"] += delta["text"]
                        on_text(delta["text"])
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        if _state["phase"] != "think":
                            _state["phase"] = "think"
                            if on_think is None:
                                on_text("【思考中】")
                        _state["thinking"] += delta["thinking"]
                        think_out(delta["thinking"])
            elif obj.get("type") == "result":
                for key in ("result", "text"):
                    if isinstance(obj.get(key), str) and obj[key]:
                        _state["result"] = obj[key]

        rc, lines = stream_command(
            form, DEFAULT_CWD, turn_timeout(agent), on_line, cancel, on_proc,
            input_text=prompt,
        )
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        text = (state["result"] or state["text"]).strip()
        if not text and state["thinking"].strip():
            text = ("（模型本轮只输出了思考内容，没有正文；把该成员的 max_tokens / 单次最大输出调大通常可解决）\n\n"
                    + state["thinking"].strip())
        if text:
            return {"session_id": sid, "text": text}
        last_err = "\n".join(lines[-3:])[-300:]
        # stream-json 的失败 result 是一整行大 JSON，人类可读的报错在**行首**的
        # errors[] 字段里；只留尾部 300 字符会把真因切掉（只剩 usage/requestId
        # 这类尾巴）。从后往前找能解析的 result 行，抽 errors[] / result 文本。
        for _ln in reversed(lines):
            _s = _ln.strip()
            if not _s.startswith("{"):
                continue
            try:
                _obj = json.loads(_s)
            except json.JSONDecodeError:
                continue
            if _obj.get("type") == "result" and _obj.get("is_error"):
                _errs = [str(e) for e in (_obj.get("errors") or []) if e]
                if _errs:
                    last_err = "; ".join(_errs)[:500]
                elif _obj.get("result") or _obj.get("text"):
                    last_err = str(_obj.get("result") or _obj.get("text"))[:500]
                break
        if argv_fallback:
            rc, lines = stream_command(
                form + [prompt], DEFAULT_CWD, turn_timeout(agent), on_line, cancel, on_proc
            )
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            text = (state["result"] or state["text"]).strip()
            if text:
                return {"session_id": sid, "text": text}
    raise RuntimeError(f"codebuddy 调用失败：{last_err}")


def run_api_model(model_cfg, prompt, session_id, on_text, history, cancel=None,
                  on_proc=None, on_event=None, on_think=None, system_extra=None, conv_id=None):
    def _ev(kind, payload=None):
        """API 模型没有工具调用，只发 llm 生命周期 + 正文（§5.2）。"""
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    label = model_cfg["label"]
    system = model_cfg.get("system_prompt") or ROLE_SYSTEM.format(label=label)
    if system_extra:
        system += "\n\n" + str(system_extra)
    messages = list(history) + [{"role": "user", "content": prompt}]
    overrides = cfg_agent(model_cfg["id"])
    common = dict(
        style=model_cfg["style"],
        base_url=model_cfg["base_url"],
        api_key=model_cfg["api_key"],
        model=model_cfg["model"],
        messages=messages,
        system=system,
        max_tokens=int(overrides.get("max_tokens") or model_cfg.get("max_tokens") or 1024),
        timeout=http_timeout(model_cfg["id"]),
        path=model_cfg.get("path"),
        temperature=overrides.get("temperature"),
        effort=overrides.get("effort"),
    )
    if overrides.get("tools_enabled", cfg_get("tools.enabled", True)):
        inclusive = usage_input_includes_cache(model_cfg["id"], model_cfg["style"])
        def transport(payload):
            headers = ({"x-api-key": common["api_key"], "anthropic-version": "2023-06-01"}
                       if common["style"] == "anthropic" else {"Authorization": "Bearer " + common["api_key"]})
            data = http_json(endpoint(common["base_url"], common["style"], common["path"]), payload, headers, common["timeout"])
            data["_usage"] = normalize_usage(common["style"], data.get("usage") or {}, inclusive)
            return data
        try:
            text = api_tool_loop(common, transport, HUB.tool_runtime(model_cfg["id"], conv_id or HUB.store.active_conv()),
                                 _ev, on_text, on_think, cancel,
                                 max_steps=max(1, min(20, int(overrides.get("max_tool_steps", cfg_get("tools.max_steps", 6)) or 6))),
                                 effort=overrides.get("effort"))
        except InterruptedError:
            raise Cancelled() from None
        return {"session_id": model_cfg["id"], "text": text}
    usage = {}
    _ev("llm_start", {"model": model_cfg.get("model")})
    # 该端点的 input 含不含缓存命中，逐端点不同（同是 anthropic 风格也可能相反），
    # 所以从 model_overrides.<id> / 风格兜底里取，不能按协议猜。
    inclusive = usage_input_includes_cache(model_cfg["id"], model_cfg["style"])
    if HUB.streaming_on(model_cfg["id"]):
        text = call_model_stream(on_text=on_text, usage_out=usage, cancel=cancel,
                                 on_think=on_think, input_includes_cache=inclusive, **common)
    else:
        # 非流式路径没有实时出口：思考内容只在「无正文」时由 call_model 兜底发出来
        text = call_model(usage_out=usage, input_includes_cache=inclusive, **common)
    # §5.2：provider 真值随 llm_end 一起收口。少了这一步，steps.llm_call 的
    # in_tokens/out_tokens 永远是 NULL，turn 的 rollup（只读列）也就永远算不出 provider。
    _ev("llm_end", {"in_tokens": usage.get("in_tokens"),
                    "out_tokens": usage.get("out_tokens"),
                    "cache_read_tokens": usage.get("cache_read_tokens"),
                    "cache_write_tokens": usage.get("cache_write_tokens")})
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    stop_reason = str(usage.get("stop_reason") or "").lower()
    if text and stop_reason in ("max_tokens", "length", "max_output_tokens"):
        # 被上限截断要说出来：以前是静默少半句，用户只觉得"DeepSeek 老是被截断"
        cap = int(overrides.get("max_tokens") or model_cfg.get("max_tokens") or 1024)
        text = (text.rstrip() +
                f"\n\n…（本轮达到 max_tokens={cap} 上限，正文被截断。"
                f"把该成员的 max_tokens 调大，或让它分两次说）")
        _ev("note", {"name": "truncated",
                     "text": f"stop_reason={usage.get('stop_reason')} max_tokens={cap}"})
    return {"session_id": model_cfg["id"], "text": text}


DSH_ARGV_LIMIT = 28000     # Windows 命令行上限约 32KB，给解释器和参数留余量
_DSH_NOISE = ("[cancelled]", "[timeout]", "[子进程已退出", "[仍在执行]")
_DSH_SPILL_NOTE_POSTED = False     # 「落盘兜底」的解释只发一次，别每轮刷
# 把 task 挪出命令行的那条路：profile 的用户 patch 层读这个环境变量（见 CLAUDE_DSH_PATCH）。
DSH_ENV_TASK = "DSH_TASK"
# 走环境变量时命令行上仍要塞一个占位 positional —— headless-startup 的 provider
# 要求 task 非空，空了会直接 program.error 退出。真值会被 patch 覆盖掉。
DSH_ARGV_PLACEHOLDER = "hub-task-via-env"


def dsh_profiles_dir():
    """dsh 的 profiles 根（$DSH_HOME/profiles，默认 ~/.dsh/profiles）。"""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.join(home, "profiles")


def dsh_env_task_wired(profile):
    """profile 的用户 patch 层有没有把 task 接到 $DSH_TASK。

    有 → prompt 走环境变量（实测 25 万字都塞得下），完全不落盘；
    没有 → 退回「落盘 + 让它读文件」。
    读不到文件就当作没接，宁可走兜底也不要发一个只有占位符的假任务。
    """
    try:
        with open(os.path.join(dsh_profiles_dir(), profile, "cordis.patch.yml"),
                  encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return False
    # 认「同一行里既有 task: 又有 DSH_TASK」——只看关键字的话，一句提到它的注释
    # 就会让中枢以为接好了，然后发一个只有占位符的假任务出去（静默答错）。
    return any("task:" in line and DSH_ENV_TASK in line
               for line in body.splitlines() if not line.lstrip().startswith("#"))


def dsh_patch_snippet():
    """接上环境变量要往 profile 的 cordis.patch.yml 里加什么（报错时直接给用户）。"""
    return (f"- id: headless-runner\n"
            f"  config:\n"
            f"    task: !!js process.env.{DSH_ENV_TASK} || ctx.headlessStartup.task")


def run_dsh(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
            on_event=None, on_think=None, system_extra=None):
    """DeepSeek Harness（dsh）的一次性调用。

    与其它成员的差别（都是 dsh headless profile 自己的限制，见 dsh-headless/lib/index.js）：
    - 任务官方只从 argv 来，**不收 stdin**，所以没有 `input_text=prompt` 这一路。
      命令行上限约 32KB，而群里的 prompt（角色前言 + 权限 + 历史）常年 4 万字以上 —— 所以
      **默认走环境变量**：profile 的用户 patch 层把 task 接到 $DSH_TASK 上（见 dsh_patch_snippet），
      词条从环境块进，命令行上只留一个短占位。实测 25 万字都塞得下，彻底不用落盘。
      profile 没接这条线时退回「落盘 + 让它读文件」，能跑但每轮多一次读文件的工具调用。
    - 没有 `--resume`，每次都新建会话 => **没有原生记忆**（config 里 supports_native_memory=false）；
    - 没有任何 app 参数（`dsh --profile headless --help` 只有 -h），所以模型只能由
      ~/.dsh/settings.yaml 的 agent-default-model 决定，外部覆盖不了（config 里 model_capable=false）；
    - 输出不是 JSON 事件流，stdout 就是最终回答的纯文本。
    """
    def _ev(kind, payload=None):
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    cfg = CONFIG["agents"][agent]
    node = resolve_command(cli_node(agent))
    script = resolve_command(cli_exe("dsh"))
    profile = cfg.get("profile") or "headless"
    # dsh 没有 --append-system-prompt：角色约定直接拼在任务前面
    task = ROLE_SYSTEM.format(label=LABELS.get(agent, agent))
    if system_extra:
        task += "\n\n" + str(system_extra)
    task += "\n\n" + prompt

    spill = None
    argv_task = task
    extra_env = None
    if dsh_env_task_wired(profile):
        # 首选：整条任务走环境变量，命令行上只放一个短占位。
        # 起因（2026-09-29）：群里 prompt 常年 4 万字以上，每次都被迫落盘 + 让模型读文件，
        # 「已落盘 …md」那条提示每轮都刷。环境块不受 32KB 命令行限制（实测 25 万字 OK）。
        extra_env = {DSH_ENV_TASK: task}
        argv_task = DSH_ARGV_PLACEHOLDER
    elif len(task) > DSH_ARGV_LIMIT:
        # 兜底：profile 没接 $DSH_TASK，命令行又塞不下 → 落盘 + "先读这个文件再作答"。
        # 必须落在 DEFAULT_CWD（子进程的 cwd，也是 dsh 的 workspace 根）——写中枢目录它可能读不到。
        spill = os.path.join(DEFAULT_CWD, f".dsh-prompt-{uuid.uuid4().hex}.md")
        try:
            with open(spill, "w", encoding="utf-8") as fh:
                fh.write(task)
        except OSError as exc:
            raise RuntimeError(
                f"dsh 的提示词太长（{len(task)} 字，命令行放不下），"
                f"落盘到 {spill} 也失败：{exc}。把该成员的 history_limit 调小可以避免。") from None
        argv_task = (f"本轮的全部上下文与指令在文件 {spill} 里。"
                     f"先用读取文件的工具完整读它，再按其中【本次消息】一节作答。")
        # 这条只在第一次出现时发一次：它是「怎么把 dsh 配得更好」的说明，不是每轮都要看的事件。
        global _DSH_SPILL_NOTE_POSTED
        if not _DSH_SPILL_NOTE_POSTED:
            _DSH_SPILL_NOTE_POSTED = True
            print(f"[hub] dsh 走落盘兜底（prompt {len(prompt)} 字超过命令行上限）。"
                  f"想让它改走环境变量、不再落盘：把下面两行加进 "
                  f"{os.path.join(dsh_profiles_dir(), profile, 'cordis.patch.yml')}\n"
                  f"{dsh_patch_snippet()}", flush=True)
            _ev("note", {"name": "dsh_spill_fallback",
                         "text": (f"dsh 的提示词超过命令行上限（{len(prompt)} 字），本轮改用落盘 + 读文件。"
                                  f"要一劳永逸：在 {profile} profile 的 cordis.patch.yml 里接上 "
                                  f"${DSH_ENV_TASK}（写法见中枢控制台）。")})

    chunks = []

    def collect(line):
        # stream_command 会往输出里掺自己造的行（心跳 "[仍在执行] 已运行 N 秒…"、
        # [timeout]/[cancelled]/管道占用判定）。claude/codebuddy 只认 `{` 开头的 JSON 行，
        # 天然忽略；dsh 是纯文本，不过滤就会被拼进模型回答里。
        if not line or any(mark in line for mark in _DSH_NOISE):
            return
        chunks.append(line)
        on_text(line + "\n")

    try:
        _ev("llm_start", {"model": cfg.get("model") or profile})
        cmd = wrap_command([node, script, "--profile", profile, argv_task])
        rc, lines = stream_command(cmd, DEFAULT_CWD, turn_timeout(agent),
                                  collect, cancel, on_proc, extra_env=extra_env)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        text = "\n".join(chunks).strip()
        if not text:
            tail = "\n".join(lines[-3:])[-300:]
            raise RuntimeError(
                f"dsh 没有输出（退出码 {rc}）。{('最后几行：' + tail) if tail else ''}"
                f"\n排查：手动跑 `node \"{script}\" --profile {profile} \"只回复 OK\"`。"
            )
        _ev("llm_end", {})
        return {"session_id": None, "text": text}
    finally:
        if spill:
            try:
                os.remove(spill)
            except OSError:
                pass


RUNNERS = {"claude": run_claude, "codex": run_codex, "codebuddy": run_codebuddy,
           "dsh": run_dsh}



# --- 通用 CLI 成员（config.agents.<名>.generic 描述） ---------------------------
def _dig_path(obj, dotted):
    if not dotted:
        return None
    node = obj
    for part in str(dotted).split("."):
        if node is None:
            return None
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except Exception:
                return None
        else:
            return None
    return node


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("text"):
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _extract_generic(obj, spec):
    text = ""
    list_field = spec.get("list_field")
    if list_field:
        items = _dig_path(obj, list_field)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("role") in (None, "assistant"):
                    text += _content_text(item.get("content"))
        elif isinstance(items, dict):
            text += _content_text(items.get("content"))
    if not text:
        field = spec.get("text_field")
        if field:
            val = _dig_path(obj, field)
            text = val if isinstance(val, str) else _content_text(val)
    if not text:
        for field in ("result", "response", "text", "output", "message.content"):
            val = _dig_path(obj, field)
            if isinstance(val, str) and val.strip():
                return val
            if val is not None:
                got = _content_text(val)
                if got.strip():
                    return got
    return text


def _extract_session(obj, spec):
    field = spec.get("session_field")
    if field:
        val = _dig_path(obj, field)
        if isinstance(val, (str, int)) and str(val):
            return str(val)
    return None


def run_generic_cli(agent, prompt, session_id, on_text, cancel=None, on_proc=None,
                    on_event=None, on_think=None, system_extra=None):
    cfg = CONFIG["agents"].get(agent) or {}
    spec = cfg.get("generic") or {}
    exe = resolve_command(cli_exe(agent, cfg.get("exe") or agent))
    node = cfg.get("node")
    cmd = [resolve_command(node), exe] if (node and str(exe).lower().endswith((".js", ".mjs"))) else [exe]

    new_session = None
    if session_id:
        resume = spec.get("resume_args") or []
        cmd += [str(x).replace("{session}", session_id) for x in resume]
    else:
        create = spec.get("create_args") or []
        if create:
            new_session = uuid.uuid4().hex
            cmd += [str(x).replace("{session}", new_session) for x in create]

    # 模型：只有声明了 model_args 的成员才传（{model} 取自 config.agents.<名>.model）
    model_name = str(cfg.get("model") or "").strip()
    if model_name:
        cmd += [str(x).replace("{model}", model_name) for x in (spec.get("model_args") or [])]

    cmd += [str(x).replace("{prompt}", prompt) for x in (spec.get("args") or [])]
    stdin_prompt = prompt if spec.get("stdin") else None
    output_mode = (spec.get("output") or "text").lower()

    raw_lines = []
    state = {"text": "", "session": None}

    def _on_line(line):
        raw_lines.append(line)
        if output_mode == "jsonl":
            stripped = line.strip()
            if not stripped.startswith("{"):
                return
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return
            text = _extract_generic(obj, spec)
            if text:
                state["text"] += text
            sid = _extract_session(obj, spec)
            if sid:
                state["session"] = sid
        elif output_mode == "text":
            try:
                on_text(line)
            except Exception:
                pass

    rc, lines = stream_command(cmd, DEFAULT_CWD, turn_timeout(agent), _on_line, cancel,
                               on_proc, input_text=stdin_prompt)
    if cancel is not None and cancel.is_set():
        raise Cancelled()

    raw_text = "\n".join(raw_lines).strip()
    if output_mode in ("json", "jsonl"):
        if not state["text"] and output_mode == "json":
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                state["text"] = _extract_generic(parsed, spec)
                sid = _extract_session(parsed, spec)
                if sid:
                    state["session"] = sid
        if not state["text"]:
            state["text"] = raw_text
    else:
        state["text"] = raw_text

    text = (state["text"] or "").strip()
    if not text:
        tail = "\n".join(raw_lines[-3:])[-300:]
        raise RuntimeError(f"{agent} 调用失败（退出码 {rc}）：{tail}")
    try:
        on_text(text)
    except Exception:
        pass
    return {"session_id": state["session"] or new_session or session_id, "text": text}



# --------------------------------------------------------------------------- hub
def db_size_bytes(path):
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return total


class Hub:
    def __init__(self):
        self.store = Store(DB_PATH)
        self.memory = Memory(os.path.join(DATA_DIR, "memory.db"))
        self.subs = []
        self.sub_lock = threading.Lock()
        self.pids_lock = threading.Lock()
        self.locks = {}
        self.status = {}
        self.cancel = {}
        self.procs = {}
        self.running = {}
        self.current_images = {}
        self.stop_epoch = 0
        self.last_live = {}
        self.last_live_len = {}
        self.last_nexus_scan = None
        self._codebuddy_models = {}    # codebuddy --help 的模型清单缓存
        self.last_error = None
        self.trace_error = None      # trace 子系统最近一次写入失败（原先是静默吞掉的）
        # 终端「圆桌模式」独立状态：不参与 dispatch/relay，固定顺序、固定轮数。
        # 只存内存（重启即清空）；配置由终端自己保存，中枢只负责执行。
        self.rt_lock = threading.Lock()
        self.rt = None
        self.rt_stop = None
        self.rt_thread = None
        # 调度器（ORC-02）：并发上限、单飞、可取消；状态都在内存，任务落库
        self.sched_lock = threading.Lock()
        self.sched_cond = threading.Condition()
        self.sched_running = {}     # qid -> {agent, thread, started, conv_id}
        self.sched_waiters = {}     # qid -> {event, box}（wait=True 的同步调用）
        self.sched_thread = None
        # 熔断（ORC-05）：连续失败到阈值就冷却一段时间，@all 直接跳过它
        self.breaker = {}
        self.breaker_lock = threading.Lock()
        self.budget_warned = set()
        # 注：曾有 self.perm_turns 内存 dict 做 perm→turn 映射，全文无人读写且重启即失。
        # 权限的落点只有一个：写入时定格的 perm_requests.turn_id 列（§4.3）。
        for name in self.agent_names():
            self.locks[name] = threading.Lock()
            self.status[name] = {"state": "idle", "live": "", "since": None, "last": None}
        self.relay_enabled = self.relay_enabled_from_config()
        # DB 侧留一份镜像：老界面/老代码读 state.relay 时不会看到不一致的值
        self.store.state_set("relay", "1" if self.relay_enabled else "0")
        self.max_depth = int(cfg_get("relay", {}).get("max_depth", 2))
        self.sync_grants_from_config()
        # §7.4 启动自愈：上次进程被强杀留下的 running 一律收敛，否则泳道图会越重启越脏
        settled = self.store.settle_orphans(BOOT_MS)
        if settled:
            self.post("system", "all", "event", f"启动自愈：收敛 {settled} 个未完成轮次（上次进程异常退出）")
        # 队列对账：不自动补跑（用户可能已手动重发），只把遗留任务标失败并说清楚
        dropped = self.store.queue_recover()
        if dropped:
            self.post("system", "all", "event",
                      f"启动对账：{dropped} 个排队任务因进程重启未能完成，已标失败（需要的话重新发送）")
        self.workflows = Workflows(self)
        self.sched_start()

    def project_key(self):
        return os.path.normcase(os.path.realpath(DEFAULT_CWD))

    def tool_runtime(self, agent, conv):
        roots = [DEFAULT_CWD] + [g["path"] for g in self.store.list_grants(agent)]
        return ToolRuntime(self.memory, self.project_key(), conv, roots, FILE_DENY_NAMES,
                           search_provider=cfg_get("tools.search_provider", "bing"))

    def set_effort(self, agent, effort):
        d = self.find_agent(agent)
        if not d:
            raise ValueError("未知成员")
        if any(v.get("agent") == d["name"] or k == d["name"] for k, v in list(self.running.items())):
            raise ValueError("成员执行中，不能切换 effort")
        choices = self.effort_choices(d)
        if effort not in choices:
            raise ValueError("该成员支持的 effort：" + ", ".join(choices))
        section = "agents" if d["kind"] == "cli" else "model_overrides"
        CONFIG.setdefault(section, {}).setdefault(d["name"], {})["effort"] = effort
        self.store.clear_agent_sessions(d["name"])
        self.store.invalidate_agent_prefix(d["name"])
        save_config()
        self.broadcast({"type": "agents"})
        return self.agent_defs()

    @staticmethod
    def effort_choices(d):
        if d["name"] == "codex":
            return ["", "minimal", "low", "medium", "high", "xhigh"]
        if d["name"] == "claude":
            return ["", "low", "medium", "high", "max"]
        if d["kind"] == "openai":
            return ["", "none", "minimal", "low", "medium", "high", "xhigh"]
        if d["kind"] == "anthropic":
            return ["", "low", "medium", "high", "max"]
        return [""]

    # -- 配置 ↔ config.json --------------------------------------------------
    def relay_enabled_from_config(self):
        """全局接力开关：config.relay.enabled 优先，缺省时退回 DB 的运行时值。

        2026-09-26 18:5x 修：以前只读 DB 的 state.relay，而 config.json 里
        relay.enabled 全文没人读 → 按过停止键（会顺手关接力）之后再在成员面板
        打开成员级接力，全局那半就永远是关的，表现就是"自动接力失效"。
        """
        value = (cfg_get("relay", {}) or {}).get("enabled")
        if value is None:
            return self.store.state_get("relay", "1") == "1"
        return bool(value)

    def agent_relay_enabled(self, name):
        """成员级接力开关：config 优先（成员面板写的就是它），缺省看 DB 运行时值。"""
        value = cfg_agent(name).get("relay")
        if value is None:
            return self.store.state_get(f"relay:{name}", "1") != "0"
        return bool(value)

    # -- 执行顺序（发给"全体"时）-------------------------------------------
    # -- 成员模型切换 --------------------------------------------------------
    def model_choices(self, agent):
        """给某个 CLI 成员列出可切换的模型（各自 CLI 的机制不同，见下）。

        - claude：`~/.ccm/settings/*.json` 每个 profile 就是"端点+密钥+模型"，
          切换 profile 等于换模型/换供应商；也能用 `--model` 只覆盖模型名。
        - codex：`~/.codex/models.json` 的 slug 目录 + config.toml 的当前 model/provider，
          用 `-c model="…"` 覆盖，必要时再加 `-c model_provider="…"`。
        - codebuddy：`codebuddy --help` 里直接列了支持清单，现场解析（带缓存）。
        """
        cfg = (CONFIG.get("agents", {}) or {}).get(agent) or {}
        extra = cfg.get("model_choices") or []
        out = [{"value": str(x), "label": str(x), "note": "自定义"} for x in extra if str(x).strip()]
        if agent == "claude":
            folder = claude_settings_dir()
            try:
                for name in sorted(os.listdir(folder)):
                    if not name.lower().endswith(".json"):
                        continue
                    path = os.path.join(folder, name)
                    try:
                        with open(path, encoding="utf-8") as fh:
                            data = json.load(fh)
                        env = data.get("env") or {}
                        out.append({
                            "value": os.path.splitext(name)[0],
                            "label": os.path.splitext(name)[0],
                            "note": f"{env.get('ANTHROPIC_MODEL') or '?'} · {env.get('ANTHROPIC_BASE_URL') or ''}",
                            "kind": "profile",
                        })
                    except Exception:
                        out.append({"value": os.path.splitext(name)[0], "label": os.path.splitext(name)[0],
                                    "note": "（读不出内容）", "kind": "profile"})
            except OSError:
                pass
        elif agent == "codex":
            catalog = os.path.expanduser("~/.codex/models.json")   # expandvars 不认 ~，别用错
            try:
                with open(catalog, encoding="utf-8") as fh:
                    data = json.load(fh)
                for m in data.get("models") or []:
                    if m.get("visibility") not in (None, "list"):
                        continue
                    out.append({"value": m.get("slug"), "label": m.get("display_name") or m.get("slug"),
                                "note": f"context {m.get('context_window')}" if m.get("context_window") else ""})
            except Exception:
                pass
        elif agent == "codebuddy":
            cached = self._codebuddy_models.get("list")
            if cached is None:
                cached = []
                try:
                    node = resolve_command(cli_node())
                    proc = subprocess.run([node, resolve_command(cli_exe("codebuddy")), "--help"],
                                          capture_output=True, timeout=25, creationflags=NO_WINDOW)
                    text = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
                    m = re.search(r"Currently supported:\s*\(([^)]*)\)", text)
                    if m:
                        cached = [x.strip() for x in m.group(1).split(",") if x.strip()]
                except Exception:
                    cached = []
                self._codebuddy_models["list"] = cached
            for name in cached:
                out.append({"value": name, "label": name, "note": ""})
        return {
            "agent": agent,
            "current": {
                "model": cfg.get("model") or "",
                "model_provider": cfg.get("model_provider") or "",
                "settings_profile": cfg.get("settings_profile") or "",
            },
            "choices": out,
        }

    def set_agent_model(self, name, model=None, provider=None, profile=None,
                        base_url=None, api_key=None, style=None):
        """切换成员的模型。

        - CLI 成员（claude / codex / codebuddy）：写 config.json，下一轮生效，并清掉原会话。
        - 「＋添加模型」直接加进来的自定义模型：改 models 表里的 model / base_url / api_key / style
          （以前这块没有入口，只能删了重建 —— 用户 2026-09-28 报的就是这个）。
        """
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        if any(v.get("agent") == d["name"] or k == d["name"] for k, v in list(self.running.items())):
            raise ValueError("成员执行中，不能切换模型")
        if d["kind"] != "cli":
            item = self.store.get_model(d["name"])
            if not item:
                raise ValueError("模型不存在")
            if model is not None and str(model).strip():
                item["model"] = str(model).strip()
            if base_url is not None and str(base_url).strip():
                item["base_url"] = str(base_url).strip()
            if api_key:
                item["api_key"] = str(api_key).strip()
            if style and str(style).strip().lower() in STYLES:
                item["style"] = str(style).strip().lower()
            saved = self.store.upsert_model(item)
            self.post("system", saved["id"], "event",
                      f"{saved['label']} 的接口参数已更新：{saved['style']} / {saved['model']} @ {saved['base_url']}"
                      f"（下一轮生效）")
            self.broadcast({"type": "agents"})
            return self.agent_defs()
        if model is not None and d.get("model_capable") is False:
            raise ValueError("该 CLI 尚未提供模型切换参数")
        cfg = CONFIG.setdefault("agents", {}).setdefault(d["name"], {})
        if model is not None:
            cfg["model"] = str(model).strip()
        if provider is not None:
            cfg["model_provider"] = str(provider).strip()
        if profile is not None:
            prof = str(profile).strip()
            if prof:
                folder = claude_settings_dir()
                if not os.path.exists(os.path.join(folder, prof + ".json")):
                    raise ValueError(f"找不到 settings profile：{prof}")
            cfg["settings_profile"] = prof
        save_config()
        # 换模型/换 profile 之后必须丢掉原会话：CLI 的 session/resume 是绑在
        # 原模型（有时还是原端点）上的，拿 A 的会话去 resume B 会卡住或直接报错
        # （2026-09-28 实测：Claude 从 deepseek-f 换到 deepseek-p 时 --resume 老会话，7 分钟无输出）。
        # 清的是**所有会话**：原生记忆现在按会话各存一份，只清当前会话的话，
        # 切回别的会话照样会拿老模型的 session 去 resume，那次的挂起会原样复现。
        if any(v is not None for v in (model, provider, profile)):
            try:
                self.store.clear_agent_sessions(d["name"])
            except Exception:
                pass
            try:
                # 前缀缓存是绑模型的：字节可能一模一样，但那是另一个模型的缓存。
                # 留着只会让命中率**看着好看、实际全是 miss**，所以一并作废。
                self.store.invalidate_agent_prefix(d["name"])
            except Exception:
                pass
        bits = []
        if cfg.get("settings_profile"):
            bits.append(f"profile={cfg['settings_profile']}")
        if cfg.get("model"):
            bits.append(f"model={cfg['model']}")
        if cfg.get("model_provider"):
            bits.append(f"provider={cfg['model_provider']}")
        self.post("system", d["name"], "event",
                  f"{d['label']} 的模型已切换为：{'、'.join(bits) if bits else '（跟随 CLI 自身配置）'}"
                  f"（下一轮生效；原会话已清空，新模型从新对话开始）")
        self.broadcast({"type": "agents"})
        return self.agent_defs()

    def dispatch_mode(self):
        """sequential（默认，一个跑完再跑下一个）| parallel（同时）| auto（成员自己排）。"""
        mode = str((cfg_get("dispatch", {}) or {}).get("mode") or "sequential").strip().lower()
        return mode if mode in ("sequential", "parallel", "auto") else "sequential"

    def plan_spec(self):
        """auto 模式下先出场排期的调度者（默认：次序里的第一个成员）。"""
        name = str((cfg_get("dispatch", {}) or {}).get("planner") or "").strip()
        d = self.find_agent(name) if name else None
        if d:
            return d["name"]
        order = self.order_members([x["name"] for x in self.agent_defs() if x["enabled"]])
        return order[0] if order else ""

    # -- 圆桌模式（终端专用，独立于 dispatch/relay）------------------------
    def roundtable_status(self):
        with self.rt_lock:
            if not self.rt:
                return None
            out = copy.deepcopy(self.rt)
            # 系统提示词只在执行线程里用；不往 SSE/快照里反复搬（最多 12000 字/人）。
            out.pop("system_prompts", None)
            return out

    def _rt_broadcast(self):
        self.broadcast({"type": "roundtable", "roundtable": self.roundtable_status()})

    def _rt_update(self, **fields):
        with self.rt_lock:
            if not self.rt:
                return
            self.rt.update(fields)
        self._rt_broadcast()

    def roundtable_start(self, payload):
        """开一桌：固定顺序 + 每角色系统提示 + 固定轮数。

        刻意不复用 dispatch/relay：圆桌是「可预测的会议」，不是「谁点名谁接力」。
        每一位都直接调 _run_agent，只把前一位的回复留在会话历史里给下一位看。
        """
        if not isinstance(payload, dict):
            raise ValueError("参数必须是 JSON 对象")
        raw = payload.get("participants") or []
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValueError("圆桌至少需要 2 个成员")
        parts = []
        for item in raw:
            if isinstance(item, str):
                item = {"agent": item}
            if not isinstance(item, dict):
                raise ValueError("participants 每项必须是成员 id 或对象")
            d = self.find_agent(str(item.get("agent") or item.get("name") or ""))
            if not d:
                raise ValueError("未知成员：" + str(item.get("agent") or item.get("name") or ""))
            if not d.get("enabled"):
                raise ValueError("成员未启用：" + d["label"])
            role = str(item.get("role") or d["label"]).strip()[:40] or d["label"]
            prompt = str(item.get("system_prompt") or item.get("prompt") or "").strip()
            if len(prompt) > 12000:
                raise ValueError(d["label"] + " 的系统提示词过长（上限 12000 字）")
            parts.append({"agent": d["name"], "label": d["label"], "role": role,
                          "system_prompt": prompt,
                          "slot": str(item.get("slot") or "").strip()})
        raw_rounds = payload.get("rounds")
        if raw_rounds in (None, ""):
            rounds = 2
        else:
            try:
                rounds = int(raw_rounds)
            except (TypeError, ValueError):
                raise ValueError("轮数必须是整数")
        if not 1 <= rounds <= 20:
            raise ValueError("轮数必须在 1-20 之间")
        topic = str(payload.get("topic") or "").strip()[:2000] or "自由讨论"
        try:
            max_chars = int(payload.get("max_chars")) if payload.get("max_chars") not in (None, "") else 600
        except (TypeError, ValueError):
            max_chars = 600
        max_chars = max(0, min(max_chars, 4000))
        with self.rt_lock:
            if self.rt and self.rt.get("state") in ("running", "stopping", "pausing"):
                raise ValueError("圆桌正在进行中；先暂停再开新的一桌")
        # 同一个 Agent 可以出现多次：slot 是「实例身份」，agent 才是真正的 CLI。
        # 名字必须唯一（AutoGen 也强制这一条），否则消息归属和会话都会串。
        counts, idx_seen, used_slots, used_labels = {}, {}, set(), set()
        for p in parts:
            counts[p["agent"]] = counts.get(p["agent"], 0) + 1
        for p in parts:
            n = idx_seen.get(p["agent"], 0) + 1
            idx_seen[p["agent"]] = n
            slot = p["slot"] or (p["agent"] if n == 1 else p["agent"] + "#" + str(n))
            if slot in used_slots:
                raise ValueError("实例身份重复：" + slot)
            used_slots.add(slot)
            p["slot"] = slot
            if counts[p["agent"]] == 1:
                label = p["label"]
            elif p["role"] != p["label"]:
                label = p["label"] + "·" + p["role"]
            elif n == 1:
                label = p["label"]
            else:
                label = p["label"] + " #" + str(n)
            if label in used_labels:
                label = label + "#" + str(n)
            used_labels.add(label)
            p["label"] = label
        order = [p["slot"] for p in parts]
        members = list(dict.fromkeys(p["agent"] for p in parts))
        if bool(payload.get("new_conversation", True)):
            title = "圆桌 · " + (topic[:28] if topic != "自由讨论" else time.strftime("%m-%d %H:%M"))
            conv_row = self.store.create_conversation(title=title, members=members)
            conv = int(conv_row["id"])
            self.store.set_active_conv(conv)
            self.broadcast({"type": "conversation", "conversation": conv})
            self.broadcast({"type": "trace_reset"})
        else:
            conv = self.store.active_conv()
        rt = {
            "id": uuid.uuid4().hex[:12],
            "state": "running",
            "topic": topic,
            "rounds": rounds,
            "max_chars": max_chars,
            "round": 0,
            "turn": 0,
            "completed": 0,
            "total": len(order) * rounds,
            "order": order,
            "agents": {p["slot"]: p["agent"] for p in parts},
            "labels": {p["slot"]: p["label"] for p in parts},
            "roles": {p["slot"]: p["role"] for p in parts},
            "system_prompts": {p["slot"]: p["system_prompt"] for p in parts},
            "conversation": conv,
            "started": time.time(),
            "ended": None,
            "paused_at": None,
            "current": None,
            "current_agent": None,
            "current_role": None,
            "error": "",
        }
        ev = threading.Event()
        with self.rt_lock:
            self.rt = rt
            self.rt_stop = ev
        self.post("system", "all", "event",
                  "圆桌开始（#" + rt["id"] + "）：" + topic + "；" +
                  str(rounds) + " 轮 × " + str(len(order)) + " 位；顺序：" +
                  " → ".join(p["label"] + ("（" + p["role"] + "）"
                                           if p["role"] not in p["label"] else "")
                            for p in parts),
                  conv_id=conv)
        self._rt_broadcast()
        th = threading.Thread(target=self._run_roundtable, args=(rt, ev), daemon=True)
        self.rt_thread = th
        th.start()
        return self.roundtable_status()

    def roundtable_stop(self):
        with self.rt_lock:
            rt, ev = self.rt, self.rt_stop
        if not rt or rt.get("state") not in ("running", "stopping"):
            return self.roundtable_status()
        if ev:
            ev.set()
        current = rt.get("current_agent") or rt.get("agents", {}).get(rt.get("current"))
        if current:
            try:
                self.interrupt(current, slot=rt.get("current"))
            except Exception:
                pass
        self._rt_update(state="pausing")
        return self.roundtable_status()

    def roundtable_resume(self):
        """从暂停处继续；如果上一轮已经全部完成，则自动追加一轮。"""
        with self.rt_lock:
            rt = self.rt
            if not rt:
                raise ValueError("没有可继续的圆桌")
            state = rt.get("state")
            if state in ("running", "pausing", "stopping"):
                raise ValueError("圆桌正在运行；先 /round stop 暂停")
            total = len(rt.get("order") or [])
            if not total:
                raise ValueError("圆桌上没有成员")
            completed = int(rt.get("completed") or 0)
            if state == "done" or completed >= total * int(rt.get("rounds") or 0):
                rt["rounds"] = int(rt.get("rounds") or 0) + 1
                rt["total"] = total * rt["rounds"]
            elif state not in ("paused", "stopped", "error"):
                raise ValueError("当前圆桌状态不能继续：" + str(state))
            rt["state"] = "running"
            rt["error"] = ""
            rt["ended"] = None
            rt["paused_at"] = None
            ev = threading.Event()
            self.rt_stop = ev
            snapshot = copy.deepcopy(rt)
        self._rt_broadcast()
        try:
            pos = min(completed, max(0, snapshot["rounds"] * total - 1))
            self.post("system", "all", "event",
                      "圆桌继续（#" + snapshot.get("id", "?") + "）：第 " +
                      str(pos // total + 1) + "/" + str(snapshot["rounds"]) + " 轮 · 第 " +
                      str(pos % total + 1) + "/" + str(total) + " 位",
                      conv_id=snapshot.get("conversation"))
        except Exception:
            pass
        th = threading.Thread(target=self._run_roundtable, args=(rt, ev), daemon=True)
        self.rt_thread = th
        th.start()
        return self.roundtable_status()

    def _rt_finish(self, state, message):
        with self.rt_lock:
            if not self.rt:
                return None
            self.rt.update({"ended": time.time() if state in ("done", "error") else None,
                            "current": None, "current_agent": None,
                            "current_role": None})
            self.rt["state"] = state
            if state == "paused":
                self.rt["paused_at"] = time.time()
            elif state == "done":
                self.rt["paused_at"] = None
                self.rt["completed"] = int(self.rt.get("rounds") or 0) * len(self.rt.get("order") or [])
            if state == "error":
                self.rt["error"] = message
            snapshot = copy.deepcopy(self.rt)
        self._rt_broadcast()
        head = "圆桌已暂停" if state in ("paused", "stopped") else (
            "本轮圆桌完成" if state == "done" else "圆桌出错")
        tail = "；/round resume 继续" if state in ("paused", "stopped", "error") else (
            "；/round resume 可再加一轮" if state == "done" else "")
        try:
            self.post("system", "all", "event",
                      head + "（#" + snapshot.get("id", "?") + "）：" + message + tail,
                      conv_id=snapshot.get("conversation"))
        except Exception:
            pass
        return snapshot

    def _run_roundtable(self, rt, stop):
        conv = rt["conversation"]
        labels = rt["labels"]
        order = rt["order"]
        total = len(order)
        try:
            # 用「已完成第几位」做游标：暂停后 resume 从下一位继续，
            # 被打断的那一位没产出回复就不算完成，resume 会重跑它。
            start = int(rt.get("completed") or 0)
            end = int(rt.get("rounds") or 0) * total
            for pos in range(start, end):
                if stop.is_set():
                    return self._rt_finish("paused", "已暂停")
                # 用户切走会话就暂停：否则回复会落到一个当前看不到的会话里。
                if self.store.active_conv() != conv:
                    return self._rt_finish("paused", "会话已切换，圆桌暂停")
                r = pos // total + 1
                i = pos % total + 1
                slot = order[pos % total]
                agent = rt.get("agents", {}).get(slot) or slot
                role = rt["roles"].get(slot) or labels.get(slot, slot)
                label = labels.get(slot, slot)
                with self.rt_lock:
                    if self.rt:
                        self.rt.update({"round": r, "turn": i, "current": slot,
                                        "current_agent": agent, "current_role": role})
                self._rt_broadcast()
                others = [labels[s] for s in order if s != slot]
                text = (
                    "【圆桌 · 第 " + str(r) + "/" + str(rt["rounds"]) + " 轮 · 第 " +
                    str(i) + "/" + str(total) + " 位】\n"
                    "议题：" + rt["topic"] + "\n"
                    "你的角色：" + role + "\n"
                    "第一句给出你自己的实质结论；逐项回答原始议题的要求，再指出与已有意见的具体分歧或增量。"
                    "原始议题始终优先于上一位发言，不要把讨论对象替换成文档评价或流程设计（除非原始议题要求）。"
                    "不要复述他人全文，不要替其他角色发言，不要输出流程说明或 @ 别人。"
                    + (" 本轮控制在 " + str(rt["max_chars"]) + " 字以内。" if rt.get("max_chars") else "")
                )
                role_block = (
                    "\n[圆桌角色设定 · 本会话系统级约束]\n"
                    "你是「" + role + "」。\n"
                    + (rt["system_prompts"].get(slot) or ("你是" + role + "，只代表本角色发言。")) + "\n"
                    "这是一场固定顺序的圆桌讨论：严格按主持人安排的顺序发言，"
                    "禁止 @ 或点名其他成员触发接力，禁止替其他角色说话。"
                    "每一轮必须直接回答原始议题，保留用户指定的对象、材料范围、输出要求与禁止事项。"
                    "先独立主张，再比较他人；无法读取材料就明确指出缺口，禁止虚构材料名或已读事实。\n"
                    "[角色设定结束]\n"
                )
                result = self._run_agent(
                    agent, text, sender="圆桌主持人", target="all", depth=0,
                    conv_id=conv, role_prompt=role_block, trigger_kind="roundtable",
                    others_override=others, slot=slot, sender_label=label,
                )
                # _run_agent 被中断/出错返回 None；只有真正跑完（哪怕空回复）才推进游标。
                if result is None:
                    if stop.is_set():
                        return self._rt_finish("paused", "已暂停（当前发言已中断）")
                    # 成员失败不能静默跳过、更不能照报「本轮完成」：圆桌立即转 error，
                    # 游标停在失败位，/round resume 修好后从这一位重跑。
                    return self._rt_finish(
                        "error",
                        "第 " + str(r) + " 轮第 " + str(i) + " 位 " + label +
                        " 发言失败（群里已贴具体报错）；/round resume 从该位继续",
                    )
                with self.rt_lock:
                    if self.rt:
                        self.rt["completed"] = pos + 1
                if stop.is_set():
                    return self._rt_finish("paused", "已暂停")
            return self._rt_finish("done", "完成 " + str(rt["rounds"]) + " 轮 × " + str(total) + " 位")
        except Exception as exc:  # noqa: BLE001
            return self._rt_finish("error", type(exc).__name__ + ": " + str(exc))

    def order_members(self, names):
        """按 config.dispatch.order 排序；没列进去的按成员表顺序垫在后面。"""
        order = (cfg_get("dispatch", {}) or {}).get("order") or []
        rank = {str(n): i for i, n in enumerate(order)}
        base = {d["name"]: i for i, d in enumerate(self.agent_defs())}
        return sorted(names, key=lambda n: (rank.get(n, len(rank) + base.get(n, 0)),))

    @staticmethod
    def _grants_signature(grants):
        """config 侧准入表的指纹：用来区分「config 被手工改过」与「运行时授权丢了」。"""
        items = []
        for agent in grants or {}:
            for g in (grants.get(agent) or []):
                items.append("%s|%s|%s" % (agent, (g.get("path") or "").strip(),
                                           g.get("mode") or "rw"))
        return hashlib.sha1("\n".join(sorted(items)).encode("utf-8")).hexdigest()

    def sync_grants_from_config(self):
        """config.json 的 permissions.grants 是准入真源；启动/重载/改配置时同步进内存库。

        并集保护（2026-09-26 修）：DB 里有、config 里没有的授权，若指纹显示 config
        自上次同步后没有被手工改过，说明那是运行时批准的授权（写 config 失败或老库残留），
        并回 config，绝不静默删掉用户的运行时授权；指纹变了 = 用户手工改了 config，按 config 为准。
        """
        section = CONFIG.setdefault("permissions", {})
        grants = section.get("grants") or {}
        sig = self._grants_signature(grants)
        last = self.store.state_get("grants_sig")
        if last is None or last == sig:            # config 未变 → DB 多出来的就是运行时授权
            known = {(a, (g.get("path") or "").strip())
                     for a, items in list(grants.items()) for g in (items or [])}
            gained = {}
            for g in self.store.list_grants(None):
                if (g["agent"], g["path"]) in known:
                    continue
                gained.setdefault(g["agent"], []).append({"path": g["path"], "mode": g["mode"]})
            if gained:
                for agent, items in list(gained.items()):
                    grants.setdefault(agent, []).extend(items)
                section["grants"] = grants
                save_config()
        if not grants:
            current = self.store.list_grants(None)
            if current:
                restored = {}
                for g in current:
                    restored.setdefault(g["agent"], []).append({"path": g["path"], "mode": g["mode"]})
                section["grants"] = restored
                save_config()
                grants = restored
        self.store.replace_grants(grants)
        self.store.state_set("grants_sig", self._grants_signature(grants))

    def set_grant(self, agent, path, mode):
        mode = "rw" if mode == "rw" else "ro"
        grants = CONFIG.setdefault("permissions", {}).setdefault("grants", {})
        items = [g for g in grants.get(agent, []) if g.get("path") != path]
        items.append({"path": path, "mode": mode})
        grants[agent] = items
        save_config()
        return self.store.set_grant(agent, path, mode)

    def remove_grant(self, agent, path):
        grants = CONFIG.setdefault("permissions", {}).setdefault("grants", {})
        grants[agent] = [g for g in grants.get(agent, []) if g.get("path") != path]
        if not grants[agent]:
            grants.pop(agent, None)
        save_config()
        return self.store.remove_grant(agent, path)

    def streaming_on(self, agent):
        if not bool(cfg_get("streaming", {}).get("enabled", True)):
            return False
        return bool(cfg_agent(agent).get("stream", True))

    def output_cap(self, agent):
        per = int(cfg_agent(agent).get("max_output_chars") or 0)
        if per:
            return per
        return int(cfg_get("max_output_chars", 0) or 0)

    # -- 附件 ---------------------------------------------------------------
    def upload_root(self):
        rel = (cfg_get("uploads", {}) or {}).get("dir") or "uploads"
        return os.path.join(DATA_DIR, rel)

    def receive_upload(self, rel_name, data):
        """保存一个拖入的文件并解析成文本，返回附件记录。"""
        conf = cfg_get("uploads", {}) or {}
        max_bytes = int(conf.get("max_mb", 100)) * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError(f"超过单文件上限 {conf.get('max_mb', 100)} MB")
        safe = (rel_name or "file").replace("\\", "/").lstrip("/")
        safe = re.sub(r"^[A-Za-z]:", "", safe)
        parts = [p for p in safe.split("/") if p not in ("", ".", "..")]
        if not parts:
            raise ValueError("文件名无效")
        safe = "/".join(parts)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target_dir = os.path.join(self.upload_root(), stamp, os.path.dirname(safe))
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(safe))
        with open(target, "wb") as fh:
            fh.write(data)
        result = extract_file(target, int(conf.get("max_text_chars_per_file", 20000)))
        text = result.get("text") or ""
        note = result.get("note")
        if note and note not in text:
            text = f"{text}\n{note}".strip()
        return self.store.add_attachment(
            os.path.basename(safe), safe, target, result.get("kind") or "binary", len(data), text
        )

    def attachments_block(self, atts):
        """把附件拼成注入上下文的一段文本（带总量上限）。"""
        conf = cfg_get("uploads", {}) or {}
        total_cap = int(conf.get("max_total_chars", 120000))
        lines = ["[附件] 用户拖入了以下内容，请把它们当作本次任务的材料："]
        used = 0
        for index, att in enumerate(atts, 1):
            lines.append(
                f"\n【附件 {index}】{att['name']}（{att['kind']}，{human_size(att['size'])}）"
                f"\n本地路径：{att['path']}"
            )
            body = (att.get("text") or "").strip()
            if not body:
                continue
            room = max(0, total_cap - used)
            if room <= 0:
                lines.append("（附件文本总量已达上限，其余内容未展开）")
                break
            snippet = body[:room]
            used += len(snippet)
            lines.append("----- 内容开始 -----\n" + snippet + "\n----- 内容结束 -----")
            if len(body) > len(snippet):
                lines.append(f"（该附件内容较长，已截断到 {len(snippet)} 字）")
        return "\n".join(lines)

    # -- agent registry -----------------------------------------------------
    def agent_names(self):
        return BUILTIN + [m["id"] for m in self.store.list_models()]

    def agent_defs(self, cold=None):
        out = []
        stats = self.store.agent_stats()
        # cold：`Store.last_settled_turns_by_agent()` 的结果，**只在 snapshot() 里算一次**再传进来。
        # 本函数在热路径上（find_agent 每轮反复调用），不能让它自己去查库。
        cold = cold or {}

        def cold_fields(name, label):
            """冷启动提示要用的那一组字段（前端 `coldState()` 读它们）。

            `cold` 是按 **`turns.agent` = 显示名** 建的 key，这里挂到成员上要用 label 查；
            同时兜一下路由 id（换过名字的老轮次会查不到 —— 那种情况宁可不提示，
            也别误报）。见 `Store.last_settled_turns_by_agent` 的注释。
            """
            c = cold.get(label) or cold.get(name) or {}
            return {
                "last_turn_end_ts": c.get("end_ts"),          # 秒
                "last_turn_session": c.get("session_id"),
                "last_turn_prompt_tokens": c.get("prompt_tokens"),
                "last_turn_hit_rate": c.get("first_hit_rate"),  # 事实值，不是预测
                "cache_ttl_min": self.cold_start_minutes(name),
            }

        # 一次解析、整表复用：get_session 会自己取当前会话（active_conv→state_get→拿锁），
        # 而 agent_defs 在一轮里被 find_agent 反复调用，逐个成员再解析一遍纯属浪费
        conv = self.store.active_conv()
        for name in BUILTIN:
            flag = self.store.state_get(f"enabled:{name}")
            enabled = (flag != "0") if flag is not None else CONFIG["agents"][name].get("enabled", True)
            st = stats.get(name, {})
            out.append(
                {
                    "name": name,
                    "label": LABELS.get(name, name),
                    "kind": "cli",
                    "enabled": enabled,
                    "removable": False,
                    "session": self.store.get_session(name, conv),
                    "relay": self.agent_relay_enabled(name),
                    "sent": st.get("sent", 0),
                    "received": st.get("received", 0),
                    "last_active": st.get("last_active"),
                    "running_since": (self.running.get(name) or {}).get("started"),
                    "native_memory": self.native_memory(name),
                    # 前端据此决定「原生记忆」勾选框和模型区块要不要显示（见 dsh）
                    "native_memory_capable": cfg_agent(name).get("supports_native_memory") is not False,
                    "model_capable": cfg_agent(name).get("model_capable") is not False,
                    "model": cfg_agent(name).get("model") or "",
                    "model_provider": cfg_agent(name).get("model_provider") or "",
                    "effort": cfg_agent(name).get("effort") or "",
                    "settings_profile": cfg_agent(name).get("settings_profile") or "",
                    "grants": self.store.list_grants(name),
                    "live": (self.status.get(name) or {}).get("live", "")[-400:],
                    "stream": self.streaming_on(name),
                    "max_output_chars": int(cfg_agent(name).get("max_output_chars") or 0),
                    "history_limit": self.history_limit(name),
                    "turn_timeout_sec": turn_timeout(name),
                    "history_override": cfg_agent(name).get("history_limit"),
                    "timeout_override": cfg_agent(name).get("turn_timeout_sec"),
                    # label 用本行上面那个 "label" 字段的同一个来源（LABELS），别再算一遍
                    **cold_fields(name, LABELS.get(name, name)),
                }
            )
        for model in self.store.list_models():
            st = stats.get(model["id"], {})
            out.append(
                {
                    "name": model["id"],
                    "label": model["label"],
                    "kind": model["style"],
                    "enabled": model["enabled"],
                    "removable": True,
                    "session": model["model"],
                    "base_url": model["base_url"],
                    "api_key_masked": mask_key(model["api_key"]),
                    "relay": self.agent_relay_enabled(model["id"]),
                    "sent": st.get("sent", 0),
                    "received": st.get("received", 0),
                    "last_active": st.get("last_active"),
                    "running_since": (self.running.get(model["id"]) or {}).get("started"),
                    "native_memory": False,
                    "grants": self.store.list_grants(model["id"]),
                    "live": (self.status.get(model["id"]) or {}).get("live", "")[-400:],
                    "stream": self.streaming_on(model["id"]),
                    "max_output_chars": int(cfg_agent(model["id"]).get("max_output_chars") or 0),
                    "max_tokens": int(model.get("max_tokens") or 1024),
                    "history_limit": self.history_limit(model["id"]),
                    "turn_timeout_sec": turn_timeout(model["id"]),
                    "history_override": cfg_agent(model["id"]).get("history_limit"),
                    "timeout_override": cfg_agent(model["id"]).get("turn_timeout_sec"),
                    "temperature": cfg_agent(model["id"]).get("temperature"),
                    "model": model["model"],
                    "effort": cfg_agent(model["id"]).get("effort") or "",
                    **cold_fields(model["id"], model["label"]),
                }
            )
        return out

    def find_agent(self, key):
        key = (key or "").strip()
        low = key.lower()
        for d in self.agent_defs():
            if d["name"] == key or d["label"].lower() == low or slug(d["label"]) == low:
                return d
        return None

    # -- memory -------------------------------------------------------------
    def native_memory(self, name):
        """True = 用 CLI 自己的会话记忆；False（默认）= 记忆由本群聊窗口掌管。"""
        if name not in BUILTIN:  # 自定义 API 模型始终由窗口供上下文
            return False
        # 该 CLI 根本没有续接会话的能力（dsh headless 每次都新建 session）：
        # 勾了也没用，所以直接钉死 false，别在界面上给一个骗人的开关。
        if cfg_agent(name).get("supports_native_memory") is False:
            return False
        flag = self.store.state_get(f"native_memory:{name}")
        if flag is None:
            return (cfg_get("memory_mode") or "hub") == "native"
        return flag == "1"

    def history_limit(self, agent=None):
        """注入多少条历史；0 或负数 = 无上限（整段历史都注入）。成员级优先。"""
        if agent:
            per = cfg_agent(agent).get("history_limit")
            if per is not None and per != "":
                try:
                    value = int(per)
                    return 0 if value <= 0 else max(2, value)
                except (TypeError, ValueError):
                    pass
        try:
            value = int(cfg_get("history_limit"))
        except (TypeError, ValueError):
            return 20
        if value <= 0:
            return 0
        return max(2, value)

    def label_for(self, name):
        if name == "user":
            return "用户"
        if name and "," in str(name):          # 多选发送：target 存成 "a,b"
            parts = [self.label_for(x.strip()) for x in str(name).split(",") if x.strip()]
            return "、".join(parts) if parts else str(name)
        d = self.find_agent(name)
        return d["label"] if d else name

    def cache_prefix_cfg(self):
        """`context.cache_prefix`：冻结前缀的开关、重锚阈值、冷启动时间阈值。"""
        conf = cfg_get("cache_prefix", {}) or {}
        return {
            "enabled": bool(conf.get("enabled", True)),
            "reanchor_chars": int(conf.get("reanchor_chars", 16000) or 0),
            # 冷启动提示的**次判据**用（主判据是会话变更，见 store.last_settled_turns_by_agent）。
            # 0 = 关掉时间判据，只留会话变更。
            "cold_start_minutes": int(conf.get("cold_start_minutes", DEFAULT_COLD_START_MINUTES)),
        }

    def cold_start_minutes(self, agent=None):
        """该成员的冷启动时间阈值（分钟）。成员级 `cache_ttl_min` 覆盖全局。

        逐成员可覆盖是有意义的：各家缓存下限不同（Anthropic ≈5、OpenAI ≈30、
        DeepSeek ≈10），而 `agents.<名>.cache_ttl_min` 就是按端点配的那一格 ——
        和 `usage_input_includes_cache` 那套"逐端点属性 + 兜底"同一个思路。
        """
        if agent:
            override = cfg_agent(agent).get("cache_ttl_min")
            if override is not None:
                try:
                    return int(override)
                except (TypeError, ValueError):
                    pass
        return self.cache_prefix_cfg()["cold_start_minutes"]

    def _history_line(self, m):
        """一条历史消息渲染成一行（锚定与追加两条路径必须共用，否则字节会不一致）。"""
        target = "全体" if m["target"] == "all" else self.label_for(m["target"])
        ranges = m.get("highlights") or []
        body = self.render_marks(m["text"], ranges)
        mark = "★重要★ " if (m.get("highlight") and not ranges) else ""
        return (f"[{time.strftime('%H:%M:%S', time.localtime(m['ts']))}] "
                f"{mark}{self.label_for(m['sender'])} → {target}: {body}")

    def _visible_history(self, agent, conv, sender, text):
        """锚定时要放进前缀的那批历史行（按 id 升序）。

        `limit * 4` 那个放大是有原因的：中间夹着 event/error/perm 这些**不进 prompt** 的行，
        不多取一些原始行就凑不满 limit 条可见消息。
        """
        limit = self.history_limit(agent)
        rows = [m for m in self.store.recent(None if not limit else limit * 4, conv)
                if m["kind"] in ("user", "reply")]
        if limit:
            rows = rows[-limit:]
        # 正在回的那条消息已经落库了（post 在 dispatch 之前），但它属于"本次消息"、
        # 不属于历史 —— 从尾部摘掉。只摘尾部连续匹配的那几条，别动中间。
        while rows and rows[-1]["sender"] == sender and rows[-1]["text"] == text:
            rows.pop()
        cap = int(cfg_get("context.max_history_chars", 12000) or 0)
        return bounded_history(rows, cap) if cap else rows

    def _anchor_prefix(self, agent, conv, head, conf, sender, text):
        """重新锚定：从最近的历史里重建前缀，太长就从**最前面**砍到装得下。

        砍前面（而不是砍后面）是刻意的：最新的对话最重要；而且砍掉的都是 id 小于
        anchor 的行，永远不会被后续追加路径补回来，不会出现 transcript 里的空洞。
        """
        rows = self._visible_history(agent, conv, sender, text)
        cap = conf["reanchor_chars"]
        if cap:
            fixed = len(head) + len(HISTORY_HEAD)
            # 逐条从头部砍到装得下，给尾部留一点余量
            while rows and fixed + sum(len(self._history_line(m)) + 1 for m in rows) > cap:
                rows.pop(0)
        prefix = head + HISTORY_HEAD + "".join(self._history_line(m) + "\n" for m in rows)
        if rows:
            prefix += "\n"
        anchor = rows[-1]["id"] if rows else 0
        return prefix, anchor

    def frozen_prompt(self, agent, conv, label, others, shared, sender, text, role_prompt="", slot=None):
        """本轮要发的那段「冻结前缀」——**尽量逐字节复用上一轮的**。

        命中前缀缓存的前提就是逐字节相同，所以这里存的是渲染好的字符串，
        下一轮原样取出、只在尾部追加新消息，绝不重排/重渲染已有内容。

        什么时候重建（付一次 miss，之后重新稳定）：
        1. 稳定输入变了（角色文案、权限块、成员表、群公告、生效的 history_limit）——
           用 head_hash 判定，因为重新渲染出来的字节会漂移；
        2. 长度超过 `cache_prefix.reanchor_chars`；
        3. 消息被改写/删除/改高亮之后（那几种情况下前缀里的字节已经是**错的**，
           留着比 miss 更糟，由各处 invalidate 钩子删掉这一行）；
        4. 追加时发现新消息 id 不大于 anchor（transcript 里出现空洞，肉眼看不见，
           所以宁可重锚）。
        """
        # role_prompt：圆桌模式给每个角色注入的「系统级」设定。它属于稳定前缀，
        # 同一桌里逐字节不变，所以不会破坏前缀缓存；换角色/换桌时 hash 变化，重锚一次。
        head = role_head(label, others, shared) + (role_prompt or "") + self.grants_block(agent)
        limit = self.history_limit(agent)
        head_hash = hashlib.sha1(f"{head}\n<<limit={limit}>>".encode("utf-8")).hexdigest()
        conf = self.cache_prefix_cfg()

        # prompt 前缀缓存也要按实例隔离：两个 CodeBuddy 的 role_prompt/成员表不同，
        # 共用一个 cache row 会轮流把对方的前缀顶掉（每轮都重锚）。
        cache_key = slot or agent
        if conf["enabled"]:
            row = self.store.get_prompt_prefix(cache_key, conv)
            if row and row["head_hash"] == head_hash:
                prefix, anchor = row["prefix"], row["anchor_msg_id"]
                new = self.store.messages_after(conv, anchor)
                new = [m for m in new if m.get("kind") in ("user", "reply")]
                while new and new[-1]["sender"] == sender and new[-1]["text"] == text:
                    new.pop()
                # 兜底断言：新消息的 id 必须都大于 anchor。AUTOINCREMENT 下理论上不会破，
                # 但一旦破了就是 transcript 里一个**肉眼看不见的空洞**，宁可重锚。
                if new and min(m["id"] for m in new) > anchor:
                    for m in new:
                        prefix += self._history_line(m) + "\n"
                    anchor = new[-1]["id"]
                    if not (conf["reanchor_chars"] and len(prefix) > conf["reanchor_chars"]):
                        self.store.set_prompt_prefix(cache_key, conv, prefix, anchor, head_hash)
                        return prefix
                elif not new:
                    if not (conf["reanchor_chars"] and len(prefix) > conf["reanchor_chars"]):
                        return prefix          # 没有新消息、也没超阈值：原样复用
                # 落到这里 = 需要重锚（超阈值 / id 异常）

        prefix, anchor = self._anchor_prefix(agent, conv, head, conf, sender, text)
        self.store.set_prompt_prefix(cache_key, conv, prefix, anchor, head_hash)
        return prefix

    def render_marks(self, text, ranges):
        """把被高亮的片段包上 ★重要★ 标记，交给模型看。"""
        if not ranges:
            return text
        out, pos = [], 0
        for r in sorted(ranges, key=lambda x: x.get("start", 0)):
            s = max(0, int(r.get("start", 0)))
            e = min(len(text), int(r.get("end", 0)))
            if s >= e or s < pos:
                continue
            out.append(text[pos:s])
            out.append("【★重要★ " + text[s:e] + " ★】")
            pos = e
        out.append(text[pos:])
        return "".join(out)

    def grants_block(self, agent):
        """把该成员的工作区权限与真实执行模式写进 prompt。"""
        grants = self.store.list_grants(agent)
        mode = self.permission_mode(agent)
        enf = self.permission_enforcement(agent)
        rw = [g["path"] for g in grants if g["mode"] == "rw"]
        ro = [g["path"] for g in grants if g["mode"] == "ro"]
        lines = ["[工作区权限] 模式：%s（enforcement: %s）" % (mode, enf)]
        if rw:
            lines.append("可写：" + "、".join(rw))
        if ro:
            lines.append("只读：" + "、".join(ro))
        if not grants:
            lines.append("当前没有额外路径授权。")
        if mode == "read-only":
            lines.append("当前是只读模式：不要写入任何文件，也不要执行会改变系统状态的命令。")
        elif mode == "danger":
            lines.append("当前是不受限模式：仍须遵守用户指令，不要越权或做未请求的破坏性操作。")
        else:
            lines.append("这些之外你没有写权限。若需要写入未授权的位置，不要假装写过，也不要绕过；")
        lines.append("若需要额外权限，请在回复中原样输出这一行交给用户审批：[[PERM rw <完整路径>|<一句话原因]]")
        lines.append("用户同意后你才会拿到权限。")
        return "\n".join(lines) + "\n\n"

    def grants_rw(self, agent):
        return [g["path"] for g in self.store.list_grants(agent) if g["mode"] == "rw"]

    def permission_mode(self, agent):
        """生效的权限模式：成员级 > 全局 > 旧 permissions.default > 默认。

        旧配置里 permissions.default="unrestricted" 映射为 danger，避免升级后
        现有成员突然被收紧；新模板写 workspace-write。
        """
        d = cfg_agent(agent)
        mode = str(((d.get("permissions") or {}).get("mode") or "")).strip().lower()
        if mode in PERMISSION_MODES:
            return mode
        mode = str(cfg_get("permissions.mode", "") or "").strip().lower()
        if mode in PERMISSION_MODES:
            return mode
        legacy = str(cfg_get("permissions.default", "") or "").strip().lower()
        legacy_map = {
            "unrestricted": "danger",
            "workspace": "workspace-write",
            "read-only": "read-only",
            "readonly": "read-only",
        }
        if legacy in legacy_map:
            return legacy_map[legacy]
        # 老 Codex 只写了 agents.codex.sandbox，没有 permissions 段：沿用它的语义。
        sandbox = str(d.get("sandbox") or "").strip().lower()
        if sandbox == "read-only":
            return "read-only"
        if sandbox in ("workspace-write", "danger-full-access", "bypass"):
            return "workspace-write" if sandbox == "workspace-write" else "danger"
        return "workspace-write"

    def permission_enforcement(self, agent):
        """给 UI/日志用的真实性标签；不参与执行。"""
        d = self.find_agent(agent)
        name = d["name"] if d else agent
        kind = d.get("kind") if d else "cli"
        if kind != "cli":
            return "prompt-only"
        if name == "codex":
            return "native"
        if name == "claude":
            return {"read-only": "native", "workspace-write": "classifier", "danger": "none"}.get(
                self.permission_mode(name), "classifier")
        if name == "codebuddy":
            return "tool-allowlist"
        return "prompt-only"

    def codex_sandbox_args(self, agent):
        """按工作区授权构造 codex 沙箱参数；返回 (参数列表, 工作目录覆盖)。"""
        rw = self.grants_rw(agent)
        if rw:
            args = ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
            for extra in rw[1:]:
                args += ["--add-dir", extra]
            return args, rw[0]
        cfg = CONFIG["agents"].get(agent, {})
        sandbox = (cfg.get("sandbox") or "danger-full-access").strip()
        if sandbox == "bypass":
            return ["--dangerously-bypass-approvals-and-sandbox"], None
        if sandbox == "workspace-write":
            return ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"], None
        if sandbox:
            return ["--sandbox", sandbox], None
        return [], None

    def codex_turn_policy(self, agent):
        """app-server turn/start 用的 sandboxPolicy + 工作目录。"""
        mode = self.permission_mode(agent)
        rw = self.grants_rw(agent)
        if mode == "read-only":
            return {"type": "readOnly", "networkAccess": False}, DEFAULT_CWD
        # grants 是显式白名单：即使 mode=danger，有 rw grants 也优先按 workspaceWrite 收口。
        if rw:
            return {"type": "workspaceWrite", "writableRoots": rw, "networkAccess": True}, rw[0]
        if mode == "workspace-write":
            return {"type": "workspaceWrite", "writableRoots": [DEFAULT_CWD], "networkAccess": True}, DEFAULT_CWD
        return {"type": "dangerFullAccess"}, DEFAULT_CWD

    def extract_perm_requests(self, agent, reply):
        """把回复里的 [[PERM ...]] 抽出来变成待审批申请，并从展示文本里去掉。"""
        found = []
        for m in PERM_RE.finditer(reply or ""):
            mode = m.group(1).lower()
            path = (m.group(2) or "").strip().strip('"').strip("'")
            reason = (m.group(3) or "").strip() or "（未填写原因）"
            if not path:
                continue
            req = self.store.add_perm_request(agent, path, mode, reason)
            found.append(req)
        cleaned = PERM_RE.sub("", reply or "").strip()
        return cleaned, found

    def request_permission(self, agent, path, mode, reason):
        d = self.find_agent(agent)
        if not d:
            raise ValueError("未知成员")
        if not path.strip():
            raise ValueError("路径不能为空")
        req = self.store.add_perm_request(d["name"], path.strip(), mode, reason or "")
        self.post_perm_request(req)
        return req

    def post_perm_request(self, req):
        label = self.label_for(req["agent"])
        text = (
            f"{label} 申请工作区权限：{'可读写' if req['mode'] == 'rw' else '只读'}\n"
            f"路径：{req['path']}\n原因：{req['reason']}"
        )
        self.post(req["agent"], "all", "perm", text, ref=f"perm:{req['id']}")
        self.trace_step(
            req.get("turn_id"), "perm_request", "permission", "running",
            {"path": req["path"], "mode": req["mode"], "reason": req["reason"]},
        )

    def resolve_permission(self, rid, approve):
        req = self.store.get_perm_request(int(rid))
        if not req:
            raise ValueError("申请不存在")
        if req["status"] != "pending":
            raise ValueError("该申请已处理过")
        req = self.store.resolve_perm_request(int(rid), approve)
        self.trace_step(
            req.get("turn_id"), "perm_resolve", "permission", "ok",
            {"decision": "approved" if approve else "denied",
             "path": req["path"], "mode": req["mode"]},
        )
        label = self.label_for(req["agent"])
        if approve:
            # 必须走 Hub.set_grant：它同时写 config.json。只写 DB 的话，下一次
            # /api/config（或重启同步）会按 config 重建授权表，运行时批准的路径会被抹掉。
            self.set_grant(req["agent"], req["path"], req["mode"])
            self.post("system", req["agent"], "event", f"已批准 {label} 的权限申请：{req['path']}（{req['mode']}）")
        else:
            self.post("system", req["agent"], "event", f"已拒绝 {label} 的权限申请：{req['path']}")
        # 申请卡用完即弃：删掉 messages 里那条（DB + 内存 + 前端 DOM 一起释放），
        # 并广播 message_deleted 让所有打开的窗口立刻收起，不必等下一次整页加载。
        for mid in self.store.delete_messages_by_ref(f"perm:{req['id']}"):
            self.broadcast({"type": "message_deleted", "id": mid})
        self.broadcast({"type": "perms"})
        self.broadcast({"type": "agents"})
        return req

    def set_agent_memory(self, name, native):
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        if d["kind"] != "cli":
            raise ValueError("自定义 API 模型始终使用窗口记忆")
        if not d.get("native_memory_capable", True):
            # 不挡的话这里会照样写 native_memory:<名>=1，而 native_memory() 仍返回 false，
            # 界面上就是"勾了→刷新→又弹回去"。
            raise ValueError(f"{d['label']} 不支持原生记忆（该 CLI 无法续接会话），只能用窗口记忆")
        self.store.state_set(f"native_memory:{d['name']}", "1" if native else "0")
        self.post(
            "system", d["name"], "event",
            f"{d['label']} 记忆来源切换为{'原生会话（删改窗口消息不会影响它）' if native else '群聊窗口（删改立即生效）'}",
        )
        self.broadcast({"type": "agents"})
        return self.agent_defs()

    def ensure_agent(self, name):
        if name not in self.locks:
            self.locks[name] = threading.Lock()
            self.status[name] = {"state": "idle", "live": "", "since": None, "last": None}

    # -- events -------------------------------------------------------------
    def subscribe(self):
        q = queue.Queue()
        with self.sub_lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            if q in self.subs:
                self.subs.remove(q)

    def broadcast(self, event):
        with self.sub_lock:
            subs = list(self.subs)
        for q in subs:
            q.put(event)

    def trace_step(self, turn_id, kind, name=None, status="ok", detail=None,
                   in_tokens=None, out_tokens=None):
        """trace 之外的地方（权限申请/批准等）挂 step 的唯一入口。turn 已关闭时的
        开放式 step 会永远停在 running（泳道图上的假呼吸），所以这里对已终态的 turn
        一律落成关闭式。"""
        if not trace_enabled() or not turn_id:
            return None
        try:
            if status == "running" and not self.store.is_turn_running(turn_id):
                status = "ok"
            row = self.store.add_step(turn_id, kind, name, status, detail, in_tokens, out_tokens)
            self.broadcast({"type": "step", "turn_id": turn_id, "step": row})
            return row["id"]
        except Exception:
            return None

    def snapshot(self):
        # UI 侧要的是"能看全历史 + 别把 payload 撑爆"：条数放宽到 1000，
        # 超长正文（附件注入文本经常上万字）在列表里截断，DB/上下文里的原文不动。
        msgs = self.store.recent(1000)
        for m in msgs:
            text = m.get("text") or ""
            if len(text) > 8000:
                m["text"] = text[:8000] + "\n…（界面预览已截断，完整内容仍在上下文里）"
        conv = self.store.active_conv()
        # 冷启动取数在这里**只算一次**，再传进 agent_defs（它在热路径上，见那里的注释）
        cold = self.store.last_settled_turns_by_agent(conv)
        return {
            "messages": msgs,
            "agents": self.agent_defs(cold=cold),
            "queue": self.queue_state(),
            "tasks": self.store.task_list(conv, status="open", limit=40),
            "relay": self.relay_enabled,
            "max_depth": self.max_depth,
            "cwd": DEFAULT_CWD,
            # 前端「授权路径」的快捷入口用它，别再在前端写死 C:\Users\<某人>
            "home": os.path.expanduser("~"),
            "styles": list(STYLES),
            "context": self.store.state_get("context", "") or "",
            "history_limit": self.history_limit(),
            "activity": cfg_get("activity_bar") or {},
            "dispatch": {
                "mode": self.dispatch_mode(),
                "order": self.order_members([d["name"] for d in self.agent_defs()]),
                "planner": self.plan_spec(),
            },
            "conversation": conv,
            "conversations": self.store.list_conversations(),
            "roundtable": self.roundtable_status(),
            # 每轮的终态：前端给对应回复贴"完成/失败/超时/中断"，失败时把 error_text 显示出来
            "turns": self.store.recent_turns(80),
            "perms": self.store.list_perm_requests("pending"),
            "uploads": cfg_get("uploads") or {},
            "limits": {
                "turn_timeout_sec": cfg_get("limits.turn_timeout_sec", 420),
                "max_output_chars": cfg_get("max_output_chars", 0),
                "history_limit": self.history_limit(),
            },
            # 冷启动提示的时间阈值（**次判据**；主判据是每行 agents[] 里的
            # last_turn_session vs session 是否相等）。0 = 只用会话变更判据。
            # 成员级覆盖在每行 agents[].cache_ttl_min 上，前端优先取它。
            "cold_start": {"minutes": self.cache_prefix_cfg()["cold_start_minutes"]},
            "health": self.health(),
        }

    def health(self):
        counts = self.store.health_counts()
        defs = self.agent_defs()
        # 当前会话的缓存命中率。分母是三个输入桶之和（互不重叠），只统计已结算的 turn。
        conv = self.store.active_conv()
        uncached, c_read, c_write = self.store.conv_usage_totals(conv)
        prompt_tokens = uncached + c_read + c_write
        return {
            "version": APP_VERSION,
            "code_revision": CODE_REVISION,
            "pid": os.getpid(),
            "conv_id": conv,
            "cache_read_tokens": c_read,
            "cache_write_tokens": c_write,
            "uncached_tokens": uncached,
            "prompt_tokens": prompt_tokens,
            **self.store.cache_observation(conv),
            "agents_total": len(defs),
            "agents_enabled": sum(1 for a in defs if a["enabled"]),
            "messages": counts["messages"],
            "errors": counts["errors"],
            "running": [
                {"agent": name, "label": info.get("label") or (self.find_agent(name) or {}).get("label", name),
                 "started": info["started"]}
                # 必须 list() 取快照：轮次在别的线程里加/删，直接遍历 items() 会
                # RuntimeError: dictionary changed size during iteration（2026-09-28 现场）
                for name, info in list(self.running.items())
            ],
            "db_bytes": db_size_bytes(DB_PATH),
            "nexus_last_scan": self.last_nexus_scan,
            "last_error": self.last_error,
            "trace_error": getattr(self, "trace_error", None),
            # 客户端提前断开（刷新/关标签）的次数：只计数不打堆栈，见 HubHTTPServer
            "client_aborts": getattr(getattr(self, "httpd", None), "client_aborts", 0),
            "context_len": len(self.store.state_get("context", "") or ""),
            "history_limit": self.history_limit(),
        }

    # -- messages -----------------------------------------------------------
    def post(self, sender, target, kind, text, ref=None, retry_agent=None,
             retry_prompt=None, display_text=None, conv_id=None, client_msg_id=None,
             task_id=None, reply_to=None, expects_reply=None):
        # conv_id：一轮跑起来之后用户可能切走会话。_run_agent 会把它开局时锁定的那个会话
        # 一路带下来，否则这条消息会落到"发出来时恰好处于活动状态"的另一个会话里。
        cid = int(conv_id) if conv_id else None
        if display_text is None and kind == "user":
            display_text = text
        msg = self.store.add_message(
            sender, target, kind, text, ref, retry_agent, retry_prompt, display_text,
            conv_id=cid, client_msg_id=client_msg_id,
        )
        if msg and msg.get("_created", True):
            self.store.touch_conversation(cid or self.store.active_conv())
            if kind == "user":
                self.store.autotitle_conversation(cid or self.store.active_conv(),
                                                 display_text or text)
            self.broadcast({"type": "message", "message": msg})
        # 任务关联先落库、再补边：add_message 的插入语句不动，老库也不用迁移数据
        if msg and msg.get("id") and (task_id or reply_to or expects_reply is not None):
            try:
                self.store.link_message_task(msg["id"], task_id=task_id,
                                             reply_to=reply_to,
                                             expects_reply=expects_reply)
                if task_id:
                    msg["task_id"] = task_id
                if reply_to:
                    msg["reply_to"] = reply_to
            except Exception:
                pass
        return msg

    def set_status(self, agent, state, live=None, think=None, label=None):
        self.ensure_agent(agent)
        st = self.status[agent]
        st["state"] = state
        if label:
            st["label"] = label
        now = time.time()
        if live is not None or think is not None:
            # 思维链与正文分两条字段走：前端要把思维链折叠/单独滚动，混在 live 里做不到。
            # 上限默认 0 = 不截断（界面靠滚轮看），要限就在 config.json 的
            # streaming.live_max_chars 里给个字数（保留末尾，与旧行为一致）。
            cap = int((cfg_get("streaming", {}) or {}).get("live_max_chars", 0) or 0)
            if live is not None:
                st["live"] = live[-cap:] if cap else live
            if think is not None:
                st["think"] = think[-cap:] if cap else think
            total = len(st.get("live") or "") + len(st.get("think") or "")
            # 逐字增量太密：满足"距上次≥0.08s"或"新增≥8字"才广播，其余只更新本地
            grown = total - self.last_live_len.get(agent, 0)
            stream_cfg = cfg_get("streaming", {}) or {}
            gap = max(0.0, float(stream_cfg.get("min_interval_ms", 80)) / 1000.0)
            min_chars = max(1, int(stream_cfg.get("min_chars", 8)))
            if state == "running" and now - self.last_live.get(agent, 0) < gap and grown < min_chars:
                return
            self.last_live[agent] = now
            self.last_live_len[agent] = total
        st["since"] = time.time() if state == "running" else None
        if state == "idle":
            st["last"] = time.time()
        self.broadcast({"type": "status", "agent": agent, **st})

    # -- dispatch -----------------------------------------------------------
    # -- 调度器（ORC-02）：有界并发 + 单飞 + 可取消 + 预算闸门 ----------------
    # 为什么要有它：以前 dispatch 对每个成员直接起线程，20 个并发就是 20 个线程，
    # 第 3 个任务没法单独取消，重启后队列直接消失。现在统一走「落库 → 单线程调度
    # 器认领 → 有界 worker」，线程数只跟 max_parallel 有关，跟用户发多少条无关。
    def scheduler_cfg(self):
        conf = cfg_get("queue", {}) or {}

        def num(key, default):
            try:
                return int(conf.get(key, default) if conf.get(key) is not None else default)
            except (TypeError, ValueError):
                return default

        return {"max_parallel": max(1, num("max_parallel", 2)),
                "max_depth": max(0, num("max_depth", 0)),
                "cooldown_sec": max(0, num("cooldown_sec", 300)),
                "fail_streak": max(1, num("fail_streak", 3))}

    def budget_cfg(self):
        conf = cfg_get("budget", {}) or {}

        def num(key, default, cast=int):
            try:
                return cast(conf.get(key, default) if conf.get(key) is not None else default)
            except (TypeError, ValueError):
                return default

        return {"max_tokens": num("max_tokens", 0),
                "max_turns": num("max_turns", 0),
                "max_wall_clock_sec": num("max_wall_clock_sec", 0),
                "warn_ratio": num("warn_ratio", 0.8, float)}

    def conv_usage(self, conv_id):
        """会话已消耗：(token 总数, 轮次数)。运行中的轮次还没结算，不计入。"""
        in_tok, read_tok, write_tok = self.store.conv_usage_totals(conv_id)
        turns = self.store.count_turns(conv_id)
        with self.store.lock:
            output = self.store.con.execute("SELECT COALESCE(SUM(out_tokens),0) FROM turns WHERE conv_id=? AND end_ts IS NOT NULL", (conv_id,)).fetchone()[0]
        return int(in_tok + read_tok + write_tok + output), int(turns)

    def admit(self, agent, conv_id, depth=0):
        """准入闸门：预算 / 熔断 / 深度。返回 (True, None) 或 (False, 原因)。"""
        qcfg = self.scheduler_cfg()
        if qcfg["max_depth"] and depth > qcfg["max_depth"]:
            return False, f"接力深度超限（{depth} > {qcfg['max_depth']}）"
        now = time.time()
        with self.breaker_lock:
            st = self.breaker.get(agent)
            if st and st.get("until", 0) > now:
                return False, (f"{self.label_for(agent)} 连续失败 {st['n']} 次，"
                               f"冷却中（剩 {int(st['until'] - now)} 秒）")
        b = self.budget_cfg()
        if b["max_tokens"] or b["max_turns"]:
            used, turns = self.conv_usage(conv_id)
            if b["max_tokens"] and used >= b["max_tokens"]:
                return False, f"会话 token 预算已用尽（{used}/{b['max_tokens']}）"
            if b["max_turns"] and turns >= b["max_turns"]:
                return False, f"会话轮次预算已用尽（{turns}/{b['max_turns']}）"
            if b["max_tokens"] and used >= b["max_tokens"] * b["warn_ratio"]:
                self.budget_warned.add(conv_id)
        return True, None

    def sched_start(self):
        with self.sched_lock:
            if self.sched_thread and self.sched_thread.is_alive():
                return
            self.sched_thread = threading.Thread(
                target=self._sched_loop, name="hub-scheduler", daemon=True)
            self.sched_thread.start()

    def sched_wake(self):
        with self.sched_cond:
            self.sched_cond.notify_all()

    def _sched_loop(self):
        while True:
            try:
                self._sched_tick()
            except Exception as exc:              # 调度器不能死：死了就永远卡住
                self.trace_error = f"scheduler: {exc!r}"
            with self.sched_cond:
                self.sched_cond.wait(0.5)

    def _sched_tick(self):
        cfg = self.scheduler_cfg()
        while True:
            with self.sched_lock:
                if len(self.sched_running) >= cfg["max_parallel"]:
                    return
                busy = [j["agent"] for j in self.sched_running.values()]
                job = self.store.queue_claim(busy)
                if not job:
                    return
                th = threading.Thread(
                    target=self._sched_run, args=(job,), name=f"hub-job-{job['id']}",
                    daemon=True)
                self.sched_running[job["id"]] = {"agent": job["agent"], "thread": th,
                                                 "started": time.time(),
                                                 "conv_id": job["conv_id"]}
            th.start()

    def _sched_run(self, job):
        qid, agent, conv = job["id"], job["agent"], job["conv_id"]
        p = job["payload"]
        task_id = p.get("task_id")
        result, err = None, None
        ok, why = self.admit(agent, conv, depth=int(p.get("depth") or 0))
        if not ok:
            self.store.queue_finish(qid, "blocked", why)
            if task_id:
                self.store.task_close(task_id, "failed", why)
            self.post("system", agent, "error", f"未执行：{why}", conv_id=conv)
            with self.sched_lock:
                self.sched_running.pop(qid, None)
                waiter = self.sched_waiters.pop(qid, None)
            if waiter:
                waiter["box"]["error"] = why
                waiter["event"].set()
            self.broadcast_queue()
            self.sched_wake()
            return
        try:
            result = self._run_agent(
                agent, p.get("text") or "", p.get("sender") or "user",
                p.get("target") or "all", int(p.get("depth") or 0),
                p.get("attachments") or [],
                trace_id=p.get("trace_id"), parent_turn_id=p.get("parent_turn_id"),
                trigger_kind=p.get("trigger_kind") or "user",
                trigger_msg_id=p.get("trigger_msg_id"),
                epoch=p.get("epoch"),
                extra_instruction=p.get("extra_instruction"),
                task_id=task_id, conv_id=conv)
            if result is None:
                raise RuntimeError("成员调用失败或被中断；未完成")
            with self.breaker_lock:
                self.breaker.pop(agent, None)
            self.store.queue_finish(qid, "done")
            if task_id:
                self.store.task_close(task_id, "completed",
                                      "done" if result is not None else "empty-reply")
        except ValueError as exc:                 # 成员被停用之类：不算成员的错
            err = str(exc)
            self.store.queue_finish(qid, "failed", err)
            if task_id:
                self.store.task_close(task_id, "failed", err[:200])
        except Exception as exc:
            err = repr(exc)
            self.store.queue_finish(qid, "failed", err)
            if task_id:
                self.store.task_close(task_id, "failed", err[:200])
            self._breaker_bump(agent)
        finally:
            with self.sched_lock:
                self.sched_running.pop(qid, None)
                waiter = self.sched_waiters.pop(qid, None)
            if waiter:
                waiter["box"]["result"] = result
                waiter["box"]["error"] = err
                waiter["event"].set()
            self.broadcast_queue()
            self.sched_wake()

    def _breaker_bump(self, agent):
        cfg = self.scheduler_cfg()
        with self.breaker_lock:
            st = self.breaker.setdefault(agent, {"n": 0, "until": 0})
            st["n"] = int(st.get("n", 0)) + 1
            if st["n"] >= cfg["fail_streak"]:
                st["until"] = time.time() + cfg["cooldown_sec"]
                self.post("system", agent, "event",
                          f"{self.label_for(agent)} 连续失败 {st['n']} 次，"
                          f"冷却 {cfg['cooldown_sec']} 秒后再接新任务")

    def job_submit(self, agent, text, sender, target, depth, attachments=None,
                   trace_id=None, parent_turn_id=None, trigger_kind="user",
                   trigger_msg_id=None, priority=0, conv_id=None,
                   extra_instruction=None, wait=False, parent_task_id=None):
        """入队并立刻返回。wait=True 时返回 ticket，用 job_wait(ticket) 等结果。

        分成「入队」与「等待」两步，是为了让并行组能先全部入队、再一起等 ——
        若在循环里逐个 wait，组内就退化成串行了。
        """
        conv = int(conv_id or self.store.active_conv())
        # 每个待执行的动作就是一个任务（ORC-01）：入队即 submitted，跑起来 working，
        # 结束必须有终态与 closure_reason。这是「谁在做、做到哪」能回答的前提。
        task = None
        if trigger_kind != "roundtable":
            try:
                task = self.store.task_create(
                    conv, agent,
                    (text or "").strip().split("\n")[0][:60] or "（无标题）",
                    created_by=sender, parent_task_id=parent_task_id,
                    message_id=trigger_msg_id, depth=int(depth or 0))
            except Exception:
                task = None
        payload = {"task_id": (task or {}).get("id"),
                   "parent_task_id": parent_task_id,
                   "text": text, "sender": sender, "target": target, "depth": depth,
                   "attachments": attachments or [], "trace_id": trace_id,
                   "parent_turn_id": parent_turn_id, "trigger_kind": trigger_kind,
                   "trigger_msg_id": trigger_msg_id,
                   "extra_instruction": extra_instruction, "epoch": self.stop_epoch}
        with self.sched_lock:
            qid = self.store.queue_add(agent, conv, payload, priority=priority)
            ticket = {"qid": qid, "agent": agent}
            if wait:
                ticket["event"] = threading.Event()
                ticket["box"] = {}
                self.sched_waiters[qid] = ticket
        self.sched_start()
        self.broadcast_queue()
        self.sched_wake()
        return ticket if wait else qid

    def job_wait(self, ticket):
        if not ticket:
            return None
        ev = ticket.get("event")
        if ev is not None:
            ev.wait()
        return (ticket.get("box") or {}).get("result")

    def cancel_job(self, qid):
        """取消一条任务：排队中直接作废，运行中要求中断（不杀进程之外的东西）。"""
        qid = int(qid)
        state = self.store.queue_cancel(qid)
        if state == "cancelled":
            with self.sched_lock:
                waiter = self.sched_waiters.pop(qid, None)
            if waiter:
                waiter["box"]["error"] = "已取消"
                waiter["event"].set()
        if state == "requested":
            with self.sched_lock:
                job = self.sched_running.get(qid)
            agent = (job or {}).get("agent")
            ev = self.cancel.get(agent) if agent else None
            if ev is not None:
                ev.set()
        self.broadcast_queue()
        self.sched_wake()
        return state

    def queue_state(self):
        depth = self.store.queue_depth()
        return {"depth": depth,
                "open": self.store.queue_open(limit=30),
                "max_parallel": self.scheduler_cfg()["max_parallel"]}

    def broadcast_queue(self):
        try:
            self.broadcast({"type": "queue", "queue": self.queue_state()})
        except Exception:
            pass

    def dispatch(self, target, text, sender="user", depth=0, attachments=None, *,
                 trace_id=None, parent_turn_id=None, trigger_kind="user",
                 trigger_msg_id=None, parent_task_id=None):
        """新增的三个 trace 参数一律关键字传（§4.1：进 args 元组会静默落到 attachments 上）。

        一次用户投递 = 1 个 trace_id：扇出的 N 个 turn 共用，接力继承父的。
        """
        if attachments is not None and not isinstance(attachments, list):
            # 约定挡不住下一个人：位置错位之所以难查就是因为它不报错（§4.1 fail-fast）
            raise TypeError(f"attachments 必须是 list，收到 {type(attachments).__name__}")
        if target == "all":
            names = [d["name"] for d in self.agent_defs() if d["enabled"]]
        elif "," in str(target):
            # 多选发送：target = "codex,claude"（界面上的多选就是走这条）
            names = []
            for part in str(target).split(","):
                d = self.find_agent(part.strip())
                if d and d.get("enabled") and d["name"] not in names:
                    names.append(d["name"])
            if not names:
                raise ValueError(f"多选里没有有效成员：{target}")
        else:
            d = self.find_agent(target)
            if not d:
                raise ValueError(f"未知成员：{target}")
            if not d.get("enabled"):
                raise ValueError(f"成员未启用：{d['label']}")
            names = [d["name"]]
        if not names:
            raise ValueError("没有启用中的成员")
        if trace_id is None:
            trace_id = uuid.uuid4().hex
            if target == "all":
                trigger_kind = "fanout"
        names = self.order_members(names)
        mode = self.dispatch_mode()
        # 接力不再跑 planner：它已经是被上一轮派出来的，再排一次会多一轮调度。
        if trigger_kind == "relay" and mode == "auto":
            mode = "sequential"
        if len(names) > 1 and mode == "auto":
            threading.Thread(
                target=self._run_auto,
                args=(names, text, sender, target, depth, attachments or []),
                kwargs={"trace_id": trace_id, "parent_turn_id": parent_turn_id,
                        "trigger_kind": trigger_kind, "trigger_msg_id": trigger_msg_id,
                        "epoch": self.stop_epoch},
                daemon=True,
            ).start()
            return names
        # 依次执行：同时触发时每个成员拿到的是同一份上下文，谁也看不到别人刚说的话，
        # 所以多目标默认排队；顺序来自 config.dispatch.order（界面可拖/上下移）。
        if len(names) > 1 and mode == "sequential":
            Thread = threading.Thread
            Thread(
                target=self._run_sequential,
                args=(names, text, sender, target, depth, attachments or []),
                kwargs={"trace_id": trace_id, "parent_turn_id": parent_turn_id,
                        "trigger_kind": trigger_kind, "trigger_msg_id": trigger_msg_id,
                        "epoch": self.stop_epoch},
                daemon=True,
            ).start()
            return names
        for name in names:
            self.ensure_agent(name)
            self.job_submit(
                name, text, sender, target, depth, attachments or [],
                trace_id=trace_id, parent_turn_id=parent_turn_id,
                trigger_kind=trigger_kind, trigger_msg_id=trigger_msg_id,
                parent_task_id=parent_task_id)
        return names

    def _run_sequential(self, names, text, sender, target, depth, attachments,
                        *, trace_id=None, parent_turn_id=None, trigger_kind="user",
                        trigger_msg_id=None, epoch=None):
        """按次序一个个跑；每个成员开跑时，前一个的回复已经落库，于是能读得到。"""
        total = len(names)
        for idx, name in enumerate(names):
            if epoch is not None and epoch != self.stop_epoch:
                self.post("system", "all", "event",
                          f"依次执行被停止键打断：已跑 {idx}/{total}，剩余未开始")
                return
            if idx:
                self.post("system", name, "event",
                          f"按执行顺序轮到 {self.label_for(name)}（第 {idx + 1}/{total} 位）")
            self.job_wait(self.job_submit(
                name, text, sender, target, depth, attachments,
                trace_id=trace_id, parent_turn_id=parent_turn_id,
                trigger_kind=trigger_kind, trigger_msg_id=trigger_msg_id, wait=True,
            ))

    def _run_auto(self, names, text, sender, target, depth, attachments,
                  *, trace_id=None, parent_turn_id=None, trigger_kind="user",
                  trigger_msg_id=None, epoch=None):
        """让成员自己排：先只跑调度者，读它给的 [[ORDER …]]，再按组执行。

        没给指令就退回 `dispatch.order` 的依次执行 —— 不猜，也不静默什么都不做。
        """
        planner = self.plan_spec()
        if planner not in names:                    # 调度者没被选中就换第一个
            planner = names[0]
        plan_spec = (
            "\n\n【调度者任务】本轮由你来排先后。先正常回答用户，然后在**最后单独一行**输出：\n"
            "[[ORDER 名字>名字|名字,名字]]\n"
            "规则：`>` = 先后依次；`|` 或 `,` = 同时进行；名单只能用成员 id：" + "、".join(names) + "。\n"
            "例：`[[ORDER a>b,c>d]]` 表示先 a，再 b 与 c 同时，最后 d；\n"
            "`[[ORDER " + names[0] + "|" + "|".join(names[1:]) + "]]` "
            "表示大家同时。这一行只给系统看，会从你的回复里去掉。"
        )
        self.post("system", planner, "event",
                  f"本轮由 {self.label_for(planner)} 排先后（其余成员等它的调度指令）")
        raw = self.job_wait(self.job_submit(
            planner, text, sender, target, depth, attachments,
            trace_id=trace_id, parent_turn_id=parent_turn_id, trigger_kind=trigger_kind,
            trigger_msg_id=trigger_msg_id, extra_instruction=plan_spec, wait=True,
        ))
        if epoch is not None and epoch != self.stop_epoch:
            return
        groups = self.parse_order(raw, names)
        rest = [n for n in names if n != planner]      # 调度者已经说过了
        if not rest:
            return
        if groups is None:
            self.post("system", "all", "event",
                      f"{self.label_for(planner)} 没给调度指令，按面板里的次序依次执行")
            groups = [[n] for n in rest]
        else:
            groups = [[n for n in grp if n in rest] for grp in groups]
            groups = [g for g in groups if g]
            missing = [n for n in rest if not any(n in g for g in groups)]
            if missing:                                 # 漏掉的按面板次序补在最后
                groups.extend([[n] for n in missing])
            plan_txt = " → ".join("+".join(self.label_for(n) for n in g) for g in groups)
            self.post("system", "all", "event", f"执行安排：{plan_txt}")
        for grp in groups:
            if epoch is not None and epoch != self.stop_epoch:
                self.post("system", "all", "event", "自行排期被停止键打断，剩余成员未开始")
                return
            if len(grp) == 1:
                self.job_wait(self.job_submit(
                    grp[0], text, sender, target, depth, attachments,
                    trace_id=trace_id, parent_turn_id=parent_turn_id,
                    trigger_kind=trigger_kind, trigger_msg_id=trigger_msg_id, wait=True))
                continue
            # 组内并行：先全部入队，再一起等 —— 真正的并发上限由调度器管
            tickets = [self.job_submit(
                name, text, sender, target, depth, attachments,
                trace_id=trace_id, parent_turn_id=parent_turn_id,
                trigger_kind=trigger_kind, trigger_msg_id=trigger_msg_id,
                wait=True) for name in grp]
            for t in tickets:
                self.job_wait(t)

    def parse_order(self, reply, names):
        """解析调度者的 [[ORDER …]]；返回分组列表（组内并行、组间依次），没有则 None。"""
        m = ORDER_RE.search(reply or "")
        if not m:
            return None
        known = set(names)
        groups = []
        for seg in m.group(1).split("|"):
            for part in seg.split(">"):
                grp = []
                for tok in re.split(r"[,+、]+", part.strip()):
                    tok = tok.strip()
                    if not tok:
                        continue
                    d = self.find_agent(tok)
                    if d and d["name"] in known and d["name"] not in grp:
                        grp.append(d["name"])
                if grp:
                    groups.append(grp)
        return groups or None

    def _runner_for(self, name):
        d = self.find_agent(name)
        if not d:
            raise ValueError(f"未知成员：{name}")
        if d["kind"] == "cli":
            # .get + 明确报错：裸 RUNNERS[name] 对任何没注册 runner 的 CLI 成员
            # （名字拼错、或先加进 config 后上代码）会抛一个完全看不出所以然的 KeyError
            runner = RUNNERS.get(name)
            if runner is None:
                if (cfg_agent(name).get("runner") or "").strip().lower() == "generic":
                    return run_generic_cli, None
                raise ValueError(
                    f"{name} 是 config.json 里的 CLI 成员，但 hub.py 没有它的 runner"
                    f"（已注册：{'、'.join(sorted(RUNNERS))}；或在 agents.{name} 里配 "
                    f"runner=generic 走通用 runner）")
            return runner, None
        return None, self.store.get_model(d["name"])

    def _run_agent(self, agent, text, sender, target, depth, attachments=None,
                   trace_id=None, parent_turn_id=None, trigger_kind="user",
                       trigger_msg_id=None, epoch=None, extra_instruction=None,
                       conv_id=None, role_prompt="", others_override=None,
                       slot=None, sender_label=None, task_id=None):
        self.ensure_agent(agent)
        # slot：同一个 Agent 的某个实例。它只影响「运行时身份」——锁、原生会话、
        # 子进程、流式状态都按 slot 分开；真正的 CLI/配置仍用 agent。
        slot_key = slot or agent
        if slot_key not in self.locks:
            self.locks[slot_key] = threading.Lock()
        with self.locks[slot_key]:
            # 本轮锁定的会话：读原生记忆、写原生记忆、落这条回复，全部用它。
            # 不锁的话，用户在回合中途切走会话，就会出现「会话记忆写在旧会话、回复落在新会话」。
            # conv_id：圆桌模式显式钉住开桌时的会话，不跟 active_conv 漂移。
            conv = int(conv_id) if conv_id else self.store.active_conv()
            if epoch is not None and epoch != self.stop_epoch:
                # 派出后被「停止键」作废：拿到锁就直接收工，不占用模型额度
                self.post("system", agent, "event",
                          f"{self.label_for(agent)} 本轮已被停止键取消（排队中）",
                          conv_id=conv)
                return
            d = self.find_agent(agent)
            label = sender_label or (d["label"] if d else agent)
            if task_id:
                try:
                    self.store.task_update(task_id, status="working")
                except Exception:
                    pass
            native = self.native_memory(agent) and trigger_kind not in ("planner", "worker")
            session = self.store.get_session(slot_key, conv) if native else None
            cursor_key = f"native_cursor:{slot_key}:{conv}"
            native_cursor = int(self.store.state_get(cursor_key, "0") or 0) if session else 0
            others = (list(others_override) if others_override is not None
                      else [a["label"] for a in self.agent_defs() if a["name"] != agent])
            shared = self.store.state_get("context", "") or ""
            memory_rows = self.memory.search(self.project_key(), conv, text, limit=4)
            memory_block = ""
            if memory_rows:
                memory_block = "\n[检索记忆；仅供参考，以原始任务和当前证据为准]\n" + "\n".join(
                    "[" + m["kind"] + ";" + m["source"] + "] " + m["text"][:1200] for m in memory_rows)
            prompt = (
                # 稳定前缀（角色 + 权限 + 历史），逐字节复用上一轮的那份 —— 缓存命中全靠它
                (role_head(label, others, shared) + role_prompt + self.grants_block(agent)
                 if trigger_kind in ("planner", "worker") else
                 self.frozen_prompt(agent, conv, label, others, shared, sender, text,
                                    role_prompt=role_prompt, slot=slot_key))
                # 易变尾部：谁说的、投递给谁、本次消息、auto 模式的调度指令（都不进群聊记录）
                + memory_block + role_tail(sender, target, text, extra_instruction)
            )
            if native and session:
                delta = [m for m in self.store.messages_after(conv, native_cursor)
                         if m.get("kind") in ("user", "reply") and not (m.get("kind") == "reply" and m.get("sender") == label)]
                while delta and delta[-1]["sender"] == sender and delta[-1]["text"] == text:
                    delta.pop()
                delta_cap = int(cfg_get("context.max_history_chars", 12000) or 0)
                if delta_cap:
                    delta = bounded_history(delta, delta_cap)
                prompt = (role_head(label, others, shared) + role_prompt + self.grants_block(agent)
                          + "\n[上次调用之后的群聊增量]\n" + "\n".join(self._history_line(m) for m in delta)
                          + memory_block + role_tail(sender, target, text, extra_instruction))
            visible_before = self.store.recent(1, conv)
            delivered_cursor = visible_before[-1]["id"] if visible_before else native_cursor
            cancel = threading.Event()
            self.cancel[slot_key] = cancel
            self.procs[slot_key] = None
            _images = [a["path"] for a in (attachments or [])
                       if a.get("kind") == "image" and a.get("path")]
            self.current_images[slot_key] = _images
            # runner 内部按 base agent 查当前图片；重复实例串行跑，覆盖同一份即可。
            if slot_key != agent:
                self.current_images[agent] = _images
            # 必须把 live 和 think 一起清空：只清 live 的话，新一轮的流式卡会先显示
            # 上一轮的思维链（本轮不产思维链的成员会把上一轮那串一直挂到回复落地）。
            self.set_status(agent, "running", "", "", label=label)
            self.post("system", agent, "event", f"{label} 开始处理（深度 {depth}）", conv_id=conv)
            live, think = [], []           # 正文/工具行 与 思维链 分开走（§流式：前端要能折叠思维链）

            def push():
                self.set_status(agent, "running", "".join(live), "".join(think), label=label)

            def on_line(line):
                if line is None or line == "":
                    return
                live.append(line)          # 保留原始片段（含换行），流式渲染才对
                push()

            def on_think(chunk):
                if chunk is None or chunk == "":
                    return
                think.append(chunk)
                push()

            def on_proc(proc):
                self.procs[slot_key] = proc
                self.track_child(proc.pid)

            sink = None
            turn_status, ended_ms, err_text = "ok", None, None
            settled = False            # 成功路径是否已经一次性写全（§7.3：不许拆成两次 close）
            reply, reqs = "", []
            raw_reply = ""             # 含 [[ORDER …]] 的原文（auto 模式读它排期）

            def emit(kind, payload=None):
                """runner → TraceSink 的唯一事件通道。trace 写失败绝不影响主流程。"""
                if sink is None:
                    return
                try:
                    sink.on_event(kind, payload)
                except Exception:
                    pass

            try:
                runner, model_cfg = self._runner_for(agent)
                try:
                    sink = TraceSink(
                        # agent 传**显示名**、agent_id 传路由 id（§3.1：model 是模型名，
                        # 不能拿 agent id 兜底，否则「按模型聚合成本」这列永久废掉）
                        self, label, agent_id=agent, depth=depth, trace_id=trace_id,
                        parent_turn_id=parent_turn_id, trigger_kind=trigger_kind,
                        trigger_msg_id=trigger_msg_id, trigger_by=sender,
                        model=(model_cfg or {}).get("model") if model_cfg else None,
                        session_id=session, conv_id=conv,
                    )
                except Exception:      # trace 建不起来就照常跑，不影响成员
                    sink = None
                self.running[slot_key] = {
                    "started": time.time(),
                    "turn_id": sink.id if sink else None,
                    "trace_id": sink.trace_id if sink else trace_id,
                    "slot": slot_key, "agent": agent, "label": label,
                }
                if runner:
                    result = runner(agent, prompt, session, on_line, cancel, on_proc,
                                    on_event=emit, on_think=on_think,
                                    system_extra=role_prompt or None)
                else:
                    result = run_api_model(model_cfg, prompt, session, on_line, [], cancel,
                                           on_proc, on_event=emit, on_think=on_think,
                                           system_extra=role_prompt or None, conv_id=conv)
                ended_ms = now_ms()        # runner 拿到 stream 结束的时刻（§7.3，不是落库时刻）
                # —— 成功路径：先把回复落库拿到 id，再由**唯一一次** close 写全终态 ——
                # 不能留到 finally：那里 reply 还不存在，out_chars/reply_msg_id 会丢；
                # 也不能拆成两次 close：第二次会被 settle_turn 的 `status='running'` 守卫吞掉
                # （静默，不报错 —— 这正是 out_chars 恒 0 的成因）。
                reply, reqs = self.extract_perm_requests(agent, result["text"])
                raw_reply = reply
                if ORDER_RE.search(reply):
                    # 调度指令只给系统看：去掉再入群聊（但 raw_reply 留给 _run_auto 解析）
                    reply = ORDER_RE.sub("", reply).strip()
                cap = self.output_cap(agent)
                if cap and len(reply) > cap:
                    reply = reply[:cap] + f"\n…（已按「单次最大输出」{cap} 字截断）"
                # 思维链落一条 trace 步骤（不进 messages、不进模型上下文）：
                # 流式期间前端看实时那份，收尾后还能在「执行流程」里展开回看。
                think_text = "".join(think).strip()
                if think_text:
                    tcap = int((trace_cfg().get("max_thinking_chars", 0) or 0))
                    if tcap and len(think_text) > tcap:
                        think_text = think_text[:tcap] + f"\n…（已按 trace.max_thinking_chars={tcap} 截断）"
                    emit("thinking", {"text": think_text})
                reply_msg_id = None
                if reply:
                    msg = self.post(label, target if target != "all" else "all", "reply", reply,
                                       conv_id=conv, task_id=task_id,
                                       reply_to=trigger_msg_id)
                    if msg and msg.get("id"):
                        reply_msg_id = msg["id"]
                if sink is not None:
                    # 空回复（本轮只有思考、无正文）也必须在这里收口，否则 turn 永久 running。
                    # close 自身失败不能把已成功的本轮判成 error —— 交给 finally 兜底。
                    try:
                        sink.close("ok", end_ts=ended_ms, in_chars=len(prompt),
                                   out_chars=len(reply), reply_msg_id=reply_msg_id)
                        settled = True
                    except Exception:
                        pass
            except Cancelled:
                turn_status, ended_ms = "interrupted", now_ms()
                self.set_status(agent, "idle")
                self.post("system", agent, "event", f"{label} 本轮被手动中断", conv_id=conv)
                return None
            except Exception as exc:  # noqa: BLE001
                turn_status, ended_ms, err_text = "error", now_ms(), str(exc)[:2000]
                self.set_status(agent, "idle")
                self.last_error = f"[{time.strftime('%H:%M:%S')}] {label}: {exc}"
                log_agent_error(label, exc, prompt, live)
                self.post(
                    label, "all", "error", f"{label} 出错：{exc}",
                    retry_agent=agent, retry_prompt=text, conv_id=conv,
                )
                return None
            finally:
                try:
                    _proc = self.procs.get(slot_key)
                    if _proc is not None:
                        self.untrack_child(_proc.pid)
                except Exception:
                    pass
                if sink is not None and not settled:
                    # 兜底路径：中断 / 出错 / 成功路径的 close 自己抛异常时才会走到这里。
                    # 成功路径已一次性写全（settled），这里不再重复调用。
                    # 中断时其下 running step 由 settle_turn 连带收敛为 skipped（§3.2）
                    try:
                        sink.close(turn_status, end_ts=ended_ms or now_ms(),
                                   error_text=err_text, in_chars=len(prompt))
                    except Exception:
                        pass
                self.running.pop(slot_key, None)
                self.cancel.pop(slot_key, None)
                self.procs.pop(slot_key, None)
                self.current_images.pop(slot_key, None)
                if slot_key != agent:
                    self.current_images.pop(agent, None)
            if d and d["kind"] == "cli" and native and result.get("session_id"):
                # 写回**实例**的会话；同一个 CodeBuddy 的另一个角色不会共用 --resume。
                self.store.set_session(slot_key, result["session_id"], conv)
                self.store.state_set(cursor_key, str(delivered_cursor))
            self.set_status(agent, "idle")
            # reply / reqs 已在成功路径里产生并结算完毕（§7.3：一次 close 写全），这里只做后续动作
            for req in reqs:
                self.post_perm_request(req)
            if reply and trigger_kind not in ("roundtable", "planner", "worker"):
                # 圆桌的发言顺序由主持人固定，绝不允许 @ 触发接力把顺序打乱。
                self._maybe_relay(agent, label, reply, depth,
                                  trace_id=sink.trace_id if sink else trace_id,
                                  parent_turn_id=sink.id if sink else None,
                                  reply_msg_id=reply_msg_id, epoch=epoch,
                                  task_id=task_id)
            # 返回**含调度指令**的原文：auto 模式要靠它读 [[ORDER …]]
            return raw_reply

    def _resolve_mention(self, reply, match):
        """把 @token 解析成成员；支持 @DeepSeek Harness 这种带空格的 label。"""
        token = match.group(1)
        d = self.find_agent(token)
        if d:
            return d
        rest = reply[match.end():].lstrip()
        low_token = token.lower()
        best = None
        for cand in self.agent_defs():
            for text in (cand.get("label") or "", cand.get("name") or "",
                         slug(cand.get("label") or "")):
                if not text:
                    continue
                low = text.lower()
                if not low.startswith(low_token):
                    continue
                suffix = low[len(low_token):].lstrip()
                if suffix and rest.lower().startswith(suffix):
                    if best is None or len(text) > len(best[1]):
                        best = (cand, text)
        return best[0] if best else None

    def _maybe_relay(self, agent, label, reply, depth, trace_id=None, parent_turn_id=None,
                     reply_msg_id=None, epoch=None, task_id=None):
        if not self.relay_enabled:
            return
        if self.max_depth and depth >= self.max_depth:   # 0 = 无上限
            return
        if not self.agent_relay_enabled(agent):
            return
        if epoch is not None and epoch != self.stop_epoch:
            return
        nxt = []
        for match in MENTION_RE.finditer(reply):
            token = match.group(1)
            if token.lower() in ("all", "everyone"):
                nxt.extend(a["name"] for a in self.agent_defs()
                           if a["name"] != agent and a.get("enabled"))
                continue
            d = self._resolve_mention(reply, match)
            if d and d.get("enabled") and d["name"] != agent:
                nxt.append(d["name"])
        nxt = list(dict.fromkeys(nxt))
        cap = int((cfg_get("relay", {}) or {}).get("max_fanout", 4) or 0)
        if cap and len(nxt) > cap:
            self.post("system", "all", "event",
                      f"{label} 点名了 {len(nxt)} 人，按 relay.max_fanout={cap} 只接力前 {cap} 位")
            nxt = nxt[:cap]
        if not nxt:
            return
        labels = "、".join(self.label_for(n) for n in nxt)
        self.post("system", "all", "event", f"{label} 点名了 {labels}，自动接力")
        summary = (reply or "").strip()
        if len(summary) > 500:
            summary = summary[-500:]
        handoff = (f"[接力] {label} 在 #{reply_msg_id or '?'} 点名了你。完整内容见群聊记录，勿重复。\n"
                   f"摘要（末 500 字）：{summary}\n"
                   "请判读后继续；如无必要不要重复前文。")
        # 一次 dispatch：让 sequential/parallel 真正生效，而不是每人一个线程。
        self.dispatch(",".join(nxt), handoff, sender=label, depth=depth + 1,
                      trace_id=trace_id, parent_turn_id=parent_turn_id,
                      trigger_kind="relay", parent_task_id=task_id)

    # -- management ---------------------------------------------------------
    def reset_agent(self, name):
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        if d["kind"] == "cli":
            self.store.clear_session(name)
        else:
            self.store.clear_history(name)
            self.store.clear_session(name)
        self.post("system", name, "event", f"{d['label']} 会话已重置，下一轮开新对话")

    def set_agent_enabled(self, name, enabled):
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        if d["kind"] == "cli":
            self.store.state_set(f"enabled:{name}", "1" if enabled else "0")
            CONFIG.setdefault("agents", {}).setdefault(name, {})["enabled"] = bool(enabled)
            CONFIG["agents"][name]["enabled_auto"] = False
            save_config()
        else:
            model = self.store.get_model(name)
            model["enabled"] = bool(enabled)
            self.store.upsert_model(model)
        self.post(
            "system", name, "event",
            f"{d['label']} 已{'加入' if enabled else '取消'}全体发送",
        )
        self.broadcast({"type": "agents"})
        return self.agent_defs()

    def add_model(self, payload):
        style = (payload.get("style") or "").strip().lower()
        if style not in STYLES:
            raise ValueError("style 必须是 anthropic 或 openai")
        label = (payload.get("label") or "").strip()
        base_url = (payload.get("base_url") or "").strip()
        model = (payload.get("model") or "").strip()
        api_key = (payload.get("api_key") or "").strip()
        if not (label and base_url and model):
            raise ValueError("名称 / Base URL / 模型名 必填")
        existing_id = (payload.get("id") or "").strip()
        if not existing_id:
            for old in self.store.list_models():
                if (
                    old["label"] == label
                    and old["style"] == style
                    and old["base_url"].rstrip("/") == base_url.rstrip("/")
                    and old["model"] == model
                ):
                    existing_id = old["id"]
                    break
        if existing_id:
            old = self.store.get_model(existing_id)
            if not old:
                raise ValueError("要修改的模型不存在")
            api_key = api_key or old["api_key"]
        item = {
            "id": existing_id or uuid.uuid4().hex[:8],
            "label": label,
            "style": style,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "system_prompt": (payload.get("system_prompt") or "").strip(),
            "path": (payload.get("path") or "").strip(),
            "max_tokens": int(payload.get("max_tokens") or 1024),
            "enabled": bool(payload.get("enabled", True)),
        }
        saved = self.store.upsert_model(item)
        self.ensure_agent(saved["id"])
        self.post("system", saved["id"], "event", f"已添加模型 {saved['label']}（{saved['style']} / {saved['model']}）")
        self.broadcast({"type": "agents"})
        return saved

    def delete_model(self, model_id):
        model = self.store.get_model(model_id)
        if not model:
            raise ValueError("模型不存在")
        self.store.delete_model(model_id)
        self.locks.pop(model_id, None)
        self.status.pop(model_id, None)
        self.post("system", "all", "event", f"已移除模型 {model['label']}")
        self.broadcast({"type": "agents"})
        return True

    def set_relay(self, enabled):
        self.relay_enabled = bool(enabled)
        self.store.state_set("relay", "1" if self.relay_enabled else "0")
        # config 也要写：它是唯一真源，不写的话重启后会被 config 里的旧值盖回去
        CONFIG.setdefault("relay", {})["enabled"] = bool(self.relay_enabled)
        save_config()
        self.broadcast({"type": "relay", "relay": self.relay_enabled})

    def stop_children(self):
        self.workflows.stop()
        """退出时结束所有还在跑的子进程，避免留下没人收结果的孤儿进程。"""
        for name, proc in list(self.procs.items()):
            try:
                if proc is not None and proc.poll() is None:
                    terminate_tree(proc)
            except Exception:
                pass
        save_child_pids([])

    def shutdown(self):
        """界面「关闭」键：中断轮次 → 结束子进程 → 收尾数据库 → 停掉 HTTP 服务，让进程退出。

        在**后台线程**里真正收工：先把 HTTP 响应还给浏览器（0.4s），否则页面拿到的是
        连接被断而不是 {"ok":true}。serve_forever 在主线程返回后进程自然结束；
        1 秒后还没退就 os._exit 兜底（正常路径走不到）。
        """
        try:
            self.post("system", "all", "event",
                      "已通过界面「关闭」结束中枢进程（下次双击 start-hub.bat 重新开始）")
        except Exception:
            pass
        # 同样要快照：轮次线程会同时改这两个 dict
        if (self.running or any(flag is not None for flag in list(self.cancel.values()))
                or (self.rt and self.rt.get("state") == "running")):
            try:
                # 有轮次才走 stop_all（它自己会发一条"已按停止键"）；空跑会白留一行记录
                self.stop_all(disable_relay=False)
            except Exception:
                pass
        self.stop_epoch += 1                        # 排队中的轮次直接作废
        self.stop_children()
        try:
            self.store.close()                      # 提交 + WAL checkpoint（-wal 别留 4MB 垃圾）
        except Exception:
            pass
        httpd = getattr(self, "httpd", None)

        def _stop():
            time.sleep(0.4)
            try:
                if httpd is not None:
                    httpd.shutdown()
                    httpd.server_close()
            except Exception:
                pass
            time.sleep(1.0)
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()
        return True

    def track_child(self, pid):
        with self.pids_lock:
            pids = load_child_pids()
            pids.append(int(pid))
            save_child_pids(pids)

    def untrack_child(self, pid):
        with self.pids_lock:
            save_child_pids([p for p in load_child_pids() if int(p) != int(pid)])

    def set_agent_relay(self, name, enabled):
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        self.store.state_set(f"relay:{d['name']}", "1" if enabled else "0")
        key = "agents" if d["kind"] == "cli" else "model_overrides"
        CONFIG.setdefault(key, {}).setdefault(d["name"], {})["relay"] = bool(enabled)
        save_config()
        if enabled and not self.relay_enabled:
            # 只开成员级、全局还关着的话，接力依然不会发生（正是"自动接力失效"的成因）
            self.set_relay(True)
            self.post("system", d["name"], "event",
                      f"顺手开启了全局接力（全局关着时，只开成员级接力不生效）")
        self.post("system", d["name"], "event", f"{d['label']} 的自动接力已{'开启' if enabled else '关闭'}")
        self.broadcast({"type": "agents"})
        return self.agent_defs()

    def set_context(self, text):
        self.store.state_set("context", text or "")
        self.post("system", "all", "event", "共享上下文已更新（下一条消息起对全体生效）")
        self.broadcast({"type": "context", "context": text or ""})
        return text or ""

    def interrupt(self, name, slot=None):
        d = self.find_agent(name)
        if not d:
            raise ValueError("未知成员")
        key = slot or d["name"]
        flag = self.cancel.get(key)
        proc = self.procs.get(key)
        if not flag:
            # 没指定 slot 时，兼容「同一个 Agent 在圆桌里有多个实例」：
            # /interrupt <成员> 打断它的所有实例，而不是误报「没在跑」。
            keys = [k for k in list(self.cancel)
                    if k == d["name"] or str(k).startswith(d["name"] + "#")]
            if not keys:
                raise ValueError(f"{d['label']} 当前没有在跑的任务")
            for k in keys:
                try:
                    self.cancel[k].set()
                    p = self.procs.get(k)
                    if p is not None:
                        terminate_tree(p)
                except Exception:
                    pass
            return True
        flag.set()
        if proc is not None:
            terminate_tree(proc)
        return True

    def stop_all(self, disable_relay=True):
        """一键停止：掐掉所有在跑的轮次 + 作废排队中的轮次（可选关掉接力，防互相点名又跑起来）。"""
        self.workflows.stop()
        try:
            if self.rt_stop is not None:
                self.rt_stop.set()               # 圆桌也吃同一个停止键（它自己不复用 relay）
                if self.rt and self.rt.get("state") == "running":
                    self._rt_update(state="pausing")
        except Exception:
            pass
        self.stop_epoch += 1                     # 排队中的轮次拿到锁后会发现自己过期
        try:
            self.store.queue_clear("被停止键取消")   # 队列里还没开跑的直接作废
        except Exception:
            pass
        with self.sched_lock:
            queued_waiters = [(qid, t) for qid, t in self.sched_waiters.items() if qid not in self.sched_running]
            for qid, ticket in queued_waiters:
                self.sched_waiters.pop(qid, None)
                ticket["box"]["error"] = "已停止"
                ticket["event"].set()
        stopped, killed = [], []
        for name, flag in list(self.cancel.items()):
            if flag is None:
                continue
            try:
                flag.set()
                stopped.append(name)
            except Exception:
                pass
            proc = self.procs.get(name)
            if proc is not None:
                try:
                    terminate_tree(proc)
                    killed.append(name)
                except Exception:
                    pass
        relay_was = self.relay_enabled
        # 只有在真的打断了轮次时才关接力：什么都没在跑的时候按停止键，
        # 把接力也关掉只会让人下次莫名其妙地"接力失效"（2026-09-26 实例）
        if disable_relay and relay_was and stopped:
            self.set_relay(False)
        rt_labels = ((self.rt or {}).get("labels") or {})
        labels = [rt_labels.get(n) or self.label_for(n) for n in stopped]
        self.post(
            "system", "all", "event",
            f"已按停止键：中断 {len(stopped)} 个轮次"
            + (f"（{'、'.join(labels)}）" if labels else "")
            + ("；自动接力已关闭（状态栏可重新开启）"
               if disable_relay and relay_was and stopped else ""),
        )
        self.broadcast({"type": "agents"})
        return {"stopped": stopped, "killed": killed, "relay_was": relay_was}

    def retry(self, message_id):
        msg = self.store.get(int(message_id))
        if not msg or not msg.get("can_retry"):
            raise ValueError("这条消息不能重试")
        agent, prompt = msg["retry_agent"], msg["retry_prompt"]
        self.post("system", agent, "event", f"手动重试上一轮（原错误 #{msg['id']}）")
        names = self.dispatch(agent, prompt, sender="user", depth=0,
                              trigger_msg_id=msg.get("id"))
        return names

    def nexus_orphans(self):
        db = os.path.join(os.path.expanduser("~"), ".claudelink", "nexus.db")
        if not os.path.exists(db):
            return {"exists": False, "orphans": 0}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            orphan = con.execute(
                """SELECT COUNT(*) FROM messages
                    WHERE (read IS NULL OR read=0)
                      AND to_agent NOT IN (SELECT id FROM agents)"""
            ).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            con.close()
        return {"exists": True, "orphans": orphan, "messages": total}

    def adopt_orphans(self):
        """把投给已失效 agent id 的未读消息改挂到同名当前 agent（不删消息）。"""
        db = os.path.join(os.path.expanduser("~"), ".claudelink", "nexus.db")
        if not os.path.exists(db):
            raise ValueError("nexus.db 不存在")
        backup = os.path.join(DATA_DIR, f"nexus-backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
        try:
            shutil.copy2(db, backup)
        except OSError:
            backup = "(备份失败，已跳过)"
        con = sqlite3.connect(db, timeout=10)
        con.execute("PRAGMA busy_timeout=10000")
        adopted, skipped, details = 0, 0, []
        try:
            current = {}
            for aid, role in con.execute("SELECT id, role FROM agents").fetchall():
                current.setdefault(role, []).append(aid)
            orphans = con.execute(
                """SELECT id, to_agent, substr(content,1,120) FROM messages
                    WHERE (read IS NULL OR read=0)
                      AND to_agent NOT IN (SELECT id FROM agents)
                    ORDER BY id"""
            ).fetchall()
            for mid, to_agent, preview in orphans:
                role = self.store.nexus_role(to_agent)
                targets = current.get(role or "", [])
                if not role or not targets:
                    skipped += 1
                    details.append({"message_id": mid, "reason": f"无法判定归属（历史 id {str(to_agent)[:8]}）"})
                    continue
                con.execute("UPDATE messages SET to_agent=? WHERE id=?", (targets[-1], mid))
                adopted += 1
                details.append({"message_id": mid, "role": role, "to": targets[-1][:8], "preview": preview})
            con.commit()
        finally:
            con.close()
        self.post("system", "all", "event", f"孤儿消息收养：迁移 {adopted} 条，跳过 {skipped} 条（备份 {os.path.basename(backup)}）")
        return {"adopted": adopted, "skipped": skipped, "details": details[:30], "backup": backup}

    def clear_messages(self):
        """清空全部上下文：**当前会话的**消息 + 各方会话记忆；
        保留公告板、系统提示词、准入授权、成员与模型配置，以及**其它会话**的记忆。不写入任何新消息。

        只作用于当前会话：消息本来就按 conv_id 存，原生记忆现在也按会话各存一份，
        两边必须同口径 —— 否则会出现「消息清了、别的会话的原生记忆也没了」。
        """
        conv = self.store.active_conv()
        self.store.clear_messages()      # 内部走 delete_conv_content，已连带清本会话的 sessions
        self.memory.clear_short(self.project_key(), conv)
        # trace_reset 无条件发（trace.enabled=false 时也要发）——它是清缓存信号，不是 trace 数据。
        # 客户端收到后：清空本地 trace store → 重拉 /api/trace（§7.5，清空后不重拉会留永久空洞）
        self.broadcast({"type": "trace_reset"})
        self.broadcast({"type": "cleared"})
        return True

    def start_new_session(self, members, clear_messages=True, clear_sessions=True):
        """新开一个会话：独立上下文 + 独立参与成员；旧会话留在左侧历史里。

        - 参与成员之外的会被停用（等价于成员面板里的「参与全体」不勾）。
        - 原生记忆按会话各存一份，所以**新会话天然是空记忆**，不需要（也不许）去清任何东西。
          `clear_sessions` 只作为 HTTP 接口的兼容参数保留，已不再有实际动作：
          以前它会 clear_all_sessions()，那正是「新建会话把别的会话的原生记忆一起清掉」的根源。
        - `clear_messages` 保留旧语义：把**当前（旧）会话**的记录也清掉（默认关，历史要留着）。
        """
        wanted = []
        for item in members or []:
            d = self.find_agent(str(item))
            if d and d["name"] not in wanted:
                wanted.append(d["name"])
        if not wanted:
            raise ValueError("至少要选一个参与成员")
        if clear_messages:                       # 想连旧会话一起清掉（可选）
            old = self.store.active_conv()
            self.store.delete_conv_content(old)
        labels = []
        for d in self.agent_defs():
            on = d["name"] in wanted
            if on:
                labels.append(d["label"])
            if d["kind"] == "cli":
                self.store.state_set(f"enabled:{d['name']}", "1" if on else "0")
                CONFIG.setdefault("agents", {}).setdefault(d["name"], {})["enabled"] = on
            else:
                model = self.store.get_model(d["name"])
                if model:
                    model["enabled"] = on
                    self.store.upsert_model(model)
        save_config()
        # 这里以前是 clear_all_sessions()+clear_all_model_history()：新建会话会把**所有**会话的
        # 原生记忆一起清掉。原生记忆改成按会话各存一份之后，新会话本来就是空记忆，
        # 什么都不用清 —— 清了反而正好是用户抱怨的"会话之间互相影响"。
        conv = self.store.create_conversation(members=wanted)
        self.store.set_active_conv(conv["id"])
        self.broadcast({"type": "conversation", "conversation": conv["id"]})
        self.broadcast({"type": "trace_reset"})      # 换了会话：前端重拉执行流程
        self.post("system", "all", "event",
                  f"新会话开始（#{conv['id']}）：参与成员 " + "、".join(labels)
                  + "（原生记忆按会话独立，已有会话不受影响）")
        self.broadcast({"type": "agents"})
        return {"ok": True, "conversation": conv["id"], "members": wanted,
                "cleared_old": bool(clear_messages), "sessions_cleared": False}

    def switch_conversation(self, conv_id):
        """切到某个会话：应用它记着的参与成员，前端收到 conversation 事件后整屏重载。"""
        conv = self.store.get_conversation(conv_id)
        if not conv:
            raise ValueError("会话不存在")
        self.store.set_active_conv(conv["id"])
        members = conv.get("members") or []
        if not members:
            # 老会话没记过参与成员（功能上线前的会话）→ 默认全员参与
            members = [d["name"] for d in self.agent_defs()]
        if members:
            for d in self.agent_defs():
                on = d["name"] in members
                if d["kind"] == "cli":
                    self.store.state_set(f"enabled:{d['name']}", "1" if on else "0")
                    CONFIG.setdefault("agents", {}).setdefault(d["name"], {})["enabled"] = on
                else:
                    model = self.store.get_model(d["name"])
                    if model:
                        model["enabled"] = on
                        self.store.upsert_model(model)
            save_config()
        self.broadcast({"type": "conversation", "conversation": conv["id"]})
        self.broadcast({"type": "trace_reset"})
        self.broadcast({"type": "agents"})
        return {"ok": True, "conversation": conv["id"], "title": conv["title"]}

    def edit_message(self, message_id, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("内容不能为空")
        msg = self.store.update_message(int(message_id), text)
        if not msg:
            raise ValueError("消息不存在")
        # 冻结前缀里那条消息的字节已经错了 —— 必须作废重锚，否则模型下一轮看到的还是旧文本，
        # 而 README 承诺「改写立即生效」。作废整个会话的全部成员（一个会话一份前缀，逐个算不值当）。
        if msg.get("conv_id"):
            self.store.invalidate_prompt_prefix(msg["conv_id"])
        if msg.get("highlight"):
            # 文本变了，旧片段偏移会错位，直接清掉更安全
            msg = self.store.clear_highlights(msg["id"])
        self.broadcast({"type": "message_updated", "message": msg})
        self.post("system", "all", "event", f"消息 #{msg['id']} 已改写，下一轮起模型看到的是新内容")
        return msg

    def delete_message(self, message_id):
        msg = self.store.get(int(message_id))
        if not msg:
            raise ValueError("消息不存在")
        # 必须在删之前作废：删完这一行就查不到它属于哪个会话了（Store.delete_message 只按 id）
        if msg.get("conv_id"):
            self.store.invalidate_prompt_prefix(msg["conv_id"])
        if not self.store.delete_message(int(message_id)):
            raise ValueError("删除失败")
        self.broadcast({"type": "message_deleted", "id": int(message_id)})
        self.post("system", "all", "event", f"消息 #{message_id} 已删除，下一轮起上下文不再包含它")
        return True

    def highlight_message(self, message_id, on, start=None, end=None):
        """on=True 且给了 start/end：高亮该片段；on=True 无区间：整条高亮；on=False：清除该条全部高亮。"""
        mid = int(message_id)
        msg = self.store.get(mid)
        if not msg:
            raise ValueError("消息不存在")
        # 高亮会把【★重要★ … ★】注进 prompt 的字节里 —— 这不是"过时"而是"改错"，
        # 所以冻结前缀必须作废（head_hash 覆盖不到它，那是渲染期才算出来的东西）。
        if msg.get("conv_id"):
            self.store.invalidate_prompt_prefix(msg["conv_id"])
        if not on:
            msg = self.store.clear_highlights(mid)
        elif start is None or end is None:
            text = msg["text"] or ""
            msg = self.store.add_highlight(mid, 0, len(text), text)
        else:
            text = msg["text"] or ""
            s = max(0, min(len(text), int(start)))
            e = max(0, min(len(text), int(end)))
            if e <= s:
                raise ValueError("选中的范围无效")
            if s == 0 and e >= len(text):
                # 选中了整条 → 记为整条高亮（与「整条高亮」按钮同一种表示）
                self.store.clear_highlights(mid)
                msg = self.store.set_highlight(mid, True)
            else:
                msg = self.store.add_highlight(mid, s, e, text[s:e])
        self.broadcast({"type": "message_highlighted", "message": msg})
        return msg

    # -- claudelink mirror --------------------------------------------------
    def nexus_loop(self):
        db = os.path.join(os.path.expanduser("~"), ".claudelink", "nexus.db")
        while True:
            try:
                if os.path.exists(db):
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                    for aid, role in con.execute("SELECT id, role FROM agents").fetchall():
                        self.store.nexus_map_upsert(aid, role)
                    rows = con.execute(
                        """SELECT m.id, m.content, fa.role, ta.role, m.created_at
                             FROM messages m
                             LEFT JOIN agents fa ON fa.id = m.from_agent
                             LEFT JOIN agents ta ON ta.id = m.to_agent
                            ORDER BY m.id DESC LIMIT 40"""
                    ).fetchall()
                    con.close()
                    for mid, content, from_role, to_role, created in reversed(rows):
                        ref = f"nexus:{mid}"
                        if not self.store.mark_ref(ref):
                            continue          # 清空上下文后不再重放同一条
                        text = f"{from_role or '?'} → {to_role or '(已离线)'}：{content}"
                        self.post("claudelink", "all", "claudelink", text, ref=ref)
                    self.last_nexus_scan = time.time()
            except Exception:
                pass
            time.sleep(3)


def mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "…" + key[-4:]


def validate_permission_config(patch):
    """拒绝非法的 permissions.mode，避免配置里写出一个不存在的模式。"""
    def check(mode):
        if mode not in (None, "") and str(mode).strip().lower() not in PERMISSION_MODES:
            raise ValueError("permissions.mode 只能是 read-only / workspace-write / danger")

    if not isinstance(patch, dict):
        return
    perms = patch.get("permissions")
    if isinstance(perms, dict):
        if "mode" in perms:
            check(perms.get("mode"))
        per_agents = perms.get("agents")
        for _, node in (per_agents if isinstance(per_agents, dict) else {}).items():
            if isinstance(node, dict) and "mode" in node:
                check(node.get("mode"))
    agents = patch.get("agents")
    if isinstance(agents, dict):
        for _, node in agents.items():
            if not isinstance(node, dict):
                continue
            node_perms = node.get("permissions")
            if isinstance(node_perms, dict) and "mode" in node_perms:
                check(node_perms.get("mode"))


def redact_config(value, depth=0):
    """递归脱敏配置里的密钥字段；不修改原对象。"""
    if depth > 20:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(mark in low for mark in ("api_key", "apikey", "token", "password", "secret")):
                out[key] = "***" if item not in (None, "") else item
            else:
                out[key] = redact_config(item, depth + 1)
        return out
    if isinstance(value, list):
        return [redact_config(item, depth + 1) for item in value]
    return value


def deep_merge(base, patch):
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


HUB = Hub()


# --------------------------------------------------------------------------- http
class HubHTTPServer(ThreadingHTTPServer):
    """只多一件事：客户端"提前断开"不再刷一屏 traceback。

    页面刷新 / 关标签 / 取消请求 / SSE 被掐时，浏览器会在服务器阻塞读请求行
    （handle_one_request → rfile.readline）的时候把连接 RST 掉，Windows 上就是
    ConnectionAbortedError [WinError 10053] 或 ConnectionResetError [WinError 10054]。
    这是常态，不是故障 —— 但它会走 socketserver.handle_error 打一整段堆栈到控制台，
    用户看到的就是"又出问题了"。

    处理：这类错误只累加计数（/api/health 的 client_aborts 能看到），不打堆栈；
    其它任何异常仍然照原样打印（不许静默吞掉真问题）。
    """
    daemon_threads = True
    client_aborts = 0

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if os.environ.get("CLAUDELINK_TRACE_HTTP"):
            try:
                import traceback
                with open(os.path.join(DATA_DIR, "http_trace.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write("=== handle_error ===\n" + traceback.format_exc())
            except Exception:
                pass
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            self.client_aborts += 1
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeLinkHub/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _check_request(self, need_token=True, allow_query_token=False):
        """本地 API 门禁：Host/Origin/Sec-Fetch-Site + token。

        只监听回环还不够：DNS rebinding 下浏览器会以为自己在访问同源。
        返回 (ok, code, message)，调用方负责写响应。
        """
        port = int(cfg_get("server.port", 8787) or 8787)
        allowed = {"127.0.0.1", "localhost", "::1"}
        cfg_host = str(cfg_get("server.host", "127.0.0.1") or "").strip().lower()
        if cfg_host and cfg_host not in ("0.0.0.0", "::"):
            allowed.add(cfg_host)

        raw_host = (self.headers.get("Host") or "").strip()
        try:
            hp = urlsplit("//" + raw_host)
            if hp.hostname is None or hp.hostname.lower() not in allowed:
                return False, 403, "forbidden host"
            if (hp.port or 80) != port:
                return False, 403, "forbidden host port"
        except Exception:
            return False, 403, "forbidden host"

        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            # 允许从外部链接“打开”页面（顶层导航），但拒绝跨源 fetch/API。
            top_nav = (self.command == "GET"
                       and self.path.split("?")[0] in ("/", "/index.html")
                       and (self.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate")
            if not top_nav:
                return False, 403, "forbidden cross-site"

        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin.lower() == "null":
                return False, 403, "forbidden origin"
            try:
                op = urlsplit(origin)
                default_port = 443 if op.scheme == "https" else 80
                if (op.scheme not in ("http", "https") or op.hostname is None
                        or op.hostname.lower() not in allowed
                        or (op.port or default_port) != port):
                    return False, 403, "forbidden origin"
            except Exception:
                return False, 403, "forbidden origin"

        if need_token and bool(cfg_get("server.auth", True)):
            supplied = (self.headers.get("X-ClaudeLink-Token") or "").strip()
            if not supplied and allow_query_token:
                supplied = (parse_qs(urlsplit(self.path).query).get("token") or [""])[0].strip()
            if not supplied:
                try:
                    jar = SimpleCookie()
                    jar.load(self.headers.get("Cookie") or "")
                    supplied = (jar["hub_token"].value if "hub_token" in jar else "").strip()
                except Exception:
                    supplied = ""
            if not supplied or not secrets.compare_digest(str(supplied), ensure_server_token()):
                return False, 401, "unauthorized"
        return True, 200, ""

    def _file(self):
        """GET /api/file?path=…&line=N —— 只读文本直链（§27）。

        **不用 file://**：Chrome 从 http 页面跳 file:// 会被拦掉，用户点了没反应。
        安全边界见文件头 FILE_DENY_* 的说明。
        """
        from urllib.parse import parse_qs, urlparse

        ok, code, msg = self._check_request(need_token=True)
        if not ok:
            self._json({"ok": False, "error": msg}, code)
            return

        q = parse_qs(urlparse(self.path).query)
        raw = (q.get("path") or [""])[0].strip().strip('"')
        if not raw:
            self._json({"ok": False, "error": "path required"}, 400)
            return
        target = os.path.realpath(os.path.expandvars(os.path.expanduser(raw)))
        roots = (cfg_get("trace", {}) or {}).get("file_roots") or []
        if isinstance(roots, str):
            roots = [roots]
        if not roots:
            # 默认白名单：hub 项目目录 + agent 工作区。空 = 全盘放行是漏洞，
            # 不放行则直链功能无意义；收紧/放宽都在 config.json 的 trace.file_roots。
            roots = [HUB_DIR, DEFAULT_CWD]
        if roots:
            allowed = False
            for root in roots:
                if not isinstance(root, str) or not root:
                    continue
                real_root = os.path.realpath(os.path.expandvars(root))
                if target == real_root or target.startswith(real_root + os.sep):
                    allowed = True
                    break
            if not allowed:
                self._json({"ok": False, "error": "路径不在 trace.file_roots 内"}, 403)
                return
        base = os.path.basename(target).lower()
        parts = {part.lower() for part in target.replace("\\", "/").split("/") if part}
        if (parts & FILE_DENY_PARTS or base in FILE_DENY_NAMES
                or os.path.splitext(base)[1] in FILE_DENY_EXTS
                or target.lower() in {p.lower() for p in HUB_SENSITIVE}):
            self._json({"ok": False, "error": "该文件不可直链"}, 403)
            return
        if not os.path.isfile(target):
            self._json({"ok": False, "error": "不是文件"}, 404)
            return
        try:
            size = os.path.getsize(target)
            with open(target, "rb") as fh:
                blob = fh.read(FILE_MAX_BYTES)
        except OSError as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if b"\x00" in blob:
            self._json({"ok": False, "error": "二进制文件，不直链"}, 415)
            return
        text = None
        for enc in ("utf-8", "gbk"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            self._json({"ok": False, "error": "无法按 utf-8/gbk 解码"}, 415)
            return
        try:
            want = int((q.get("line") or ["0"])[0])
        except ValueError:
            want = 0
        self._json({"ok": True, "path": target, "size": size, "line": want,
                    "total_lines": text.count("\n") + 1,
                    "truncated": size > FILE_MAX_BYTES, "text": text})

    def do_GET(self):
        from urllib.parse import parse_qs
        path = self.path.split("?")[0]
        if os.environ.get("CLAUDELINK_TRACE_HTTP"):
            try:
                with open(os.path.join(DATA_DIR, "http_trace.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write("GET %s\n" % self.path)
            except Exception:
                pass
        need_token = path.startswith("/api/") and path != "/api/health"
        ok, code, msg = self._check_request(
            need_token=need_token, allow_query_token=(path == "/api/stream"))
        if not ok:
            self._json({"ok": False, "error": msg}, code)
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
                    html = fh.read()
                token = ensure_server_token()
                html = html.replace('"__CLAUDELINK_TOKEN__"', json.dumps(token), 1)
                self._send(200, html, "text/html; charset=utf-8", headers={
                    "Set-Cookie": f"hub_token={token}; HttpOnly; SameSite=Strict; Path=/"
                })
            except OSError:
                self._send(404, "index.html missing", "text/plain; charset=utf-8")
        elif path in ("/assets/workbench.js", "/assets/workbench.css"):
            filename = "workbench.js" if path.endswith(".js") else "workbench.css"
            with open(os.path.join(WEB_DIR, filename), encoding="utf-8") as fh:
                self._send(200, fh.read(), "application/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8")
        elif path.startswith("/assets/theme/") and path.endswith(".css"):
            # 主题层：只允许 theme/ 下的单层 .css，文件名做 basename 收敛，防目录穿越
            name = os.path.basename(path)
            full = os.path.join(WEB_DIR, "theme", name)
            if not name or not os.path.isfile(full):
                self._send(404, "theme asset missing", "text/plain; charset=utf-8")
            else:
                with open(full, encoding="utf-8") as fh:
                    self._send(200, fh.read(), "text/css; charset=utf-8")
        elif path == "/api/state":
            self._json(HUB.snapshot())
        elif path == "/api/queue":
            self._json({"ok": True, **HUB.queue_state()})
        elif path == "/api/otel":
            self._json({"ok": True, "totals": HUB.store.otel_totals()})
        elif path == "/api/tasks":
            # 注意：do_GET 里其他分支有 `from urllib.parse import parse_qs` 的局部导入，
            # 会把全局 parse_qs 变成本地未绑定名 —— 这里必须手工拆 query，别用 parse_qs。
            try:
                q = dict(kv.split("=", 1) for kv in urlsplit(self.path).query.split("&")
                         if "=" in kv)
                status = q.get("status") or None
                conv = q.get("conv") or None
                self._json({"ok": True, "tasks": HUB.store.task_list(
                    int(conv) if conv else HUB.store.active_conv(), status=status)})
            except Exception:
                import traceback
                self._json({"ok": False,
                            "error": traceback.format_exc()[-800:]}, 500)
        elif path == "/api/runs":
            try:
                q = dict(kv.split("=", 1) for kv in urlsplit(self.path).query.split("&")
                         if "=" in kv)
                conv = q.get("conv") or HUB.store.active_conv()
                try:
                    limit = int(q.get("limit") or 50)
                except ValueError:
                    limit = 50
                self._json({"ok": True, "runs": HUB.store.runs(int(conv), limit=limit)})
            except Exception:
                import traceback
                self._json({"ok": False,
                            "error": traceback.format_exc()[-800:]}, 500)
        elif path == "/api/roundtable":
            self._json({"ok": True, "roundtable": HUB.roundtable_status()})
        elif path == "/api/health":
            payload = HUB.health()
            payload["nexus"] = HUB.nexus_orphans()
            self._json(payload)
        elif path == "/api/context":
            self._json({"ok": True, "context": HUB.store.state_get("context", "") or ""})
        elif path == "/api/grants":
            from urllib.parse import parse_qs, urlparse

            agent = (parse_qs(urlparse(self.path).query).get("agent") or [""])[0]
            self._json({"ok": True, "grants": HUB.store.list_grants(agent or None)})
        elif path == "/api/perms":
            self._json({"ok": True, "perms": HUB.store.list_perm_requests("pending")})
        elif path == "/api/config":
            self._json({"ok": True, "config": redact_config(copy.deepcopy(CONFIG))})
        elif path == "/api/debug/threads":
            import traceback

            frames = sys._current_frames()
            out = {}
            for thread in threading.enumerate():
                frame = frames.get(thread.ident)
                if frame is None:
                    continue
                stack = traceback.extract_stack(frame)
                out[thread.name] = [f"{f.filename.split(os.sep)[-1]}:{f.lineno} {f.name}" for f in stack[-8:]]
            self._json({"ok": True, "threads": out, "running": list(HUB.running.keys())})
        elif path == "/api/activity":
            turns = HUB.store.recent_turns(80, include_running=True)
            tids = list(dict.fromkeys(t["trace_id"] for t in turns if t.get("trace_id")))
            steps = {}
            for tid in tids:
                steps.update((HUB.store.trace_detail(tid) or {}).get("steps") or {})
            self._json({"ok": True, "turns": turns, "steps": steps})
        elif path == "/api/memory":
            query = parse_qs(urlsplit(self.path).query)
            self._json({"ok": True, "memories": HUB.memory.search(HUB.project_key(), HUB.store.active_conv(), (query.get("q") or [""])[0], limit=30)})
        elif path == "/api/workflows":
            self._json({"ok": True, "workflows": [r for r in HUB.workflows.list() if r["conversation"] == HUB.store.active_conv()]})
        elif path == "/api/capabilities":
            self._json({"ok": True, "tools": HUB.tool_runtime("", HUB.store.active_conv()).specs(),
                        "effort": {d["name"]: HUB.effort_choices(d) for d in HUB.agent_defs()}})
        elif path == "/api/trace":
            # M1：limit 先写死 50（do_GET 第一行已吃掉 query string）
            self._json({"ok": True, "traces": HUB.store.trace_list(50)})
        elif path.startswith("/api/trace/"):
            detail = HUB.store.trace_detail(path[len("/api/trace/"):])
            self._json({"ok": True, **(detail or {"turns": [], "steps": {}})})
        elif path == "/api/file":
            # 只读文本直链（截图里的文件名要点得动）。query string 在 self.path 里还在。
            self._file()
        elif path == "/api/models":
            models = [
                {**m, "api_key": mask_key(m["api_key"]), "api_key_set": bool(m["api_key"])}
                for m in HUB.store.list_models()
            ]
            self._json({"models": models})
        elif path == "/api/model-choices":
            from urllib.parse import parse_qs, urlparse

            agent = (parse_qs(urlparse(self.path).query).get("agent") or [""])[0]
            d = HUB.find_agent(agent)
            if not d or d["kind"] != "cli":
                self._json({"ok": False, "error": "只能查 CLI 成员（" + " / ".join(BUILTIN) + "）"}, 400)
                return
            self._json({"ok": True, **HUB.model_choices(d["name"])})
        elif path == "/api/stream":
            self._stream()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _stream(self):
        ok, code, msg = self._check_request(need_token=True, allow_query_token=True)
        if not ok:
            self._json({"ok": False, "error": msg}, code)
            return
        q = HUB.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    def do_POST(self):
        path = self.path.split("?")[0]
        ok, code, msg = self._check_request(need_token=True)
        if not ok:
            return self._json({"ok": False, "error": msg}, code)
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if path == "/api/upload":
            if ctype != "application/octet-stream":
                return self._json({"ok": False, "error": "upload requires application/octet-stream"}, 415)
        elif ctype != "application/json":
            return self._json({"ok": False, "error": "content-type must be application/json"}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/api/upload":
            from urllib.parse import parse_qs, unquote, urlparse

            try:
                name = unquote((parse_qs(urlparse(self.path).query).get("name") or ["file"])[0])
                att = HUB.receive_upload(name, raw)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return self._json({"ok": True, "attachment": {
                "id": att["id"], "name": att["name"], "kind": att["kind"],
                "size": att["size"], "path": att["path"],
                "preview": (att["text"] or "")[:400],
            }})
        raw = raw or b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "bad json"}, 400)
        try:
            return self._route(path, body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _route(self, path, body):
        if path == "/api/send":
            text = (body.get("text") or "").strip()
            client_msg_id = str(body.get("client_msg_id") or "").strip()[:128]
            conv = HUB.store.active_conv()
            if client_msg_id:
                existing = HUB.store.find_by_client(conv, client_msg_id)
                if existing:
                    return self._json({"ok": True, "targets": [], "duplicate": True,
                                       "message": existing})
            # 目标：targets=[] 多选（界面用它）；target 单值/逗号列表/ all 为兼容写法
            picked = []
            for item in body.get("targets") or []:
                d = HUB.find_agent(str(item))
                if not d or not d.get("enabled"):
                    raise ValueError("目标成员不存在或未启用：" + str(item))
                if d and d["name"] not in picked:
                    picked.append(d["name"])
            if picked:
                enabled = [d["name"] for d in HUB.agent_defs() if d["enabled"]]
                target = "all" if set(picked) == set(enabled) else ",".join(picked)
            else:
                target = body.get("target") or "all"
            if target == "all":
                if not any(a.get("enabled") for a in HUB.agent_defs()):
                    raise ValueError("没有启用中的成员；先添加或启用成员")
            else:
                for recipient in str(target).split(","):
                    d = HUB.find_agent(recipient.strip())
                    if not d or not d.get("enabled"):
                        raise ValueError("目标成员不存在或未启用：" + recipient)
            ids = []
            for item in body.get("attachments") or []:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            atts = [a for a in (HUB.store.get_attachment(i) for i in ids) if a]
            if not text and not atts:
                raise ValueError("消息为空")
            full_text = text
            if atts:
                full_text = (text + "\n\n" if text else "") + HUB.attachments_block(atts)
            msg = HUB.post("user", target, "user", full_text, conv_id=conv,
                           display_text=text or "（仅附件）", client_msg_id=client_msg_id)
            if msg and msg.get("_created") is False:
                return self._json({"ok": True, "targets": [], "duplicate": True,
                                   "message": msg})
            if atts and msg:
                HUB.store.bind_attachments([a["id"] for a in atts], msg["id"])
                HUB.broadcast({"type": "message", "message": HUB.store.get(msg["id"])})
            names = HUB.dispatch(target, full_text, sender="user", depth=0, attachments=atts,
                                 trigger_msg_id=(msg or {}).get("id"))
            return self._json({"ok": True, "targets": names})
        if path == "/api/relay":
            HUB.set_relay(body.get("enabled", True))
            return self._json({"ok": True, "relay": HUB.relay_enabled})
        if path == "/api/roundtable/start":
            return self._json({"ok": True, "roundtable": HUB.roundtable_start(body)})
        if path == "/api/roundtable/stop":
            return self._json({"ok": True, "roundtable": HUB.roundtable_stop()})
        if path == "/api/roundtable/resume":
            return self._json({"ok": True, "roundtable": HUB.roundtable_resume()})
        if path == "/api/reset":
            HUB.reset_agent(body.get("agent") or "")
            return self._json({"ok": True})
        if path == "/api/clear":
            HUB.clear_messages()
            return self._json({"ok": True})
        if path == "/api/session/new":
            return self._json(HUB.start_new_session(
                body.get("members") or [],
                bool(body.get("clear_messages", True)),
                bool(body.get("clear_sessions", True)),
            ))
        if path == "/api/conversations/switch":
            return self._json(HUB.switch_conversation(body.get("id") or 0))
        if path == "/api/conversations/update":
            conv = HUB.store.update_conversation(
                body.get("id") or 0,
                title=body.get("title") if "title" in body else None,
                pinned=bool(body.get("pinned")) if "pinned" in body else None,
            )
            if not conv:
                raise ValueError("会话不存在")
            HUB.broadcast({"type": "conversations"})
            return self._json({"ok": True, "conversation": conv})
        if path == "/api/conversations/delete":
            cid = int(body.get("id") or 0)
            if cid == HUB.store.active_conv():
                return self._json({"ok": False, "error": "不能删掉当前正在用的会话，先切到别的会话"}, 400)
            HUB.store.delete_conversation(cid)
            HUB.memory.clear_short(HUB.project_key(), cid)
            HUB.broadcast({"type": "conversations"})
            return self._json({"ok": True})
        if path == "/v1/traces":
            # OTLP/HTTP JSON 口（OBS-07）：agent 的 exporter 直接连这里
            n = HUB.store.otel_ingest(body)
            return self._json({"partialSuccess": {}, "acceptedSpans": n})
        if path == "/mcp":
            # MCP Server（INT-01）：Streamable HTTP 的无状态用法 —— 一次 POST 一次响应。
            # 鉴权沿用 /api 的 token 门禁（X-ClaudeLink-Token 头）。
            return self._mcp(body)
        if path == "/api/task/close":
            tid = int(body.get("id") or 0)
            status = (body.get("status") or "cancelled").strip()
            reason = (body.get("reason") or "").strip() or None
            if not tid:
                raise ValueError("缺少任务 id")
            task = HUB.store.task_close(tid, status, reason)
            HUB.broadcast({"type": "tasks", "tasks": HUB.store.task_list(
                task["conv_id"] if task else None, status="open", limit=40)})
            return self._json({"ok": bool(task), "task": task})
        if path == "/api/replay":
            mid = int(body.get("message_id") or 0)
            msg = HUB.store.get(mid) if mid else None
            if not msg:
                raise ValueError("找不到这条消息")
            target = msg.get("sender") if msg.get("kind") == "reply" else msg.get("target")
            names = HUB.dispatch(target, msg.get("text") or "", sender="user",
                                 trigger_kind="replay")
            return self._json({"ok": True, "dispatched": names})
        if path == "/api/agents/effort":
            return self._json({"ok": True, "agents": HUB.set_effort(body.get("agent") or "", str(body.get("effort") or ""))})
        if path == "/api/memory":
            if body.get("action") == "delete":
                return self._json({"ok": HUB.memory.remove(HUB.project_key(), str(body.get("id") or ""))})
            mid = HUB.memory.put(HUB.project_key(), HUB.store.active_conv(), body.get("kind") or "short", body.get("text") or "", body.get("source") or "user")
            return self._json({"ok": True, "id": mid})
        if path == "/api/workflows/start":
            item = HUB.workflows.start(body.get("topic") or "", body.get("planner") or HUB.plan_spec(), body.get("agents") or [d["name"] for d in HUB.agent_defs() if d.get("enabled")], body.get("max_steps", 4))
            return self._json({"ok": True, "workflow": item})
        if path == "/api/workflows/resume":
            return self._json({"ok": True, "workflow": HUB.workflows.resume(str(body.get("id") or ""))})
        if path == "/api/workflows/stop":
            HUB.workflows.stop(body.get("id"))
            return self._json({"ok": True})
        if path == "/api/queue/cancel":
            state = HUB.cancel_job(body.get("id"))
            return self._json({"ok": bool(state), "state": state})
        if path == "/api/interrupt":
            HUB.interrupt(body.get("agent") or "")
            return self._json({"ok": True})
        if path == "/api/stop":
            result = HUB.stop_all(disable_relay=bool(body.get("disable_relay", True)))
            return self._json({"ok": True, **result})
        if path == "/api/shutdown":
            if not bool((cfg_get("server", {}) or {}).get("allow_shutdown", True)):
                raise ValueError("界面关闭已禁用（config.json 里 server.allow_shutdown=false）")
            HUB.shutdown()
            return self._json({"ok": True, "bye": True})
        if path == "/api/dispatch":
            section = CONFIG.setdefault("dispatch", {})
            if "mode" in body:
                mode = str(body.get("mode") or "").strip().lower()
                if mode not in ("sequential", "parallel", "auto"):
                    raise ValueError("mode 只能是 sequential（依次）/ parallel（同时）/ auto（成员自己排）")
                section["mode"] = mode
            if "planner" in body:
                name = str(body.get("planner") or "").strip()
                if name:
                    d = HUB.find_agent(name)
                    if not d:
                        raise ValueError("调度者必须是现有成员")
                    section["planner"] = d["name"]
                else:
                    section["planner"] = ""
            if "order" in body:
                known = {d["name"] for d in HUB.agent_defs()}
                order = [str(x) for x in (body.get("order") or []) if str(x) in known]
                for name in [d["name"] for d in HUB.agent_defs()]:      # 顺序表始终是全量
                    if name not in order:
                        order.append(name)
                section["order"] = order
            save_config()
            HUB.broadcast({"type": "dispatch", "dispatch": section})
            return self._json({"ok": True, "dispatch": section})
        if path == "/api/retry":
            names = HUB.retry(body.get("message_id") or 0)
            return self._json({"ok": True, "targets": names})
        if path == "/api/context":
            return self._json({"ok": True, "context": HUB.set_context(body.get("text") or "")})
        if path == "/api/nexus/adopt":
            return self._json({"ok": True, **HUB.adopt_orphans()})
        if path == "/api/nexus/orphans":
            return self._json({"ok": True, **HUB.nexus_orphans()})
        if path == "/api/messages/edit":
            msg = HUB.edit_message(body.get("id") or 0, body.get("text") or "")
            return self._json({"ok": True, "message": msg})
        if path == "/api/messages/delete":
            HUB.delete_message(body.get("id") or 0)
            return self._json({"ok": True})
        if path == "/api/messages/highlight":
            msg = HUB.highlight_message(
                body.get("id") or 0,
                bool(body.get("on", True)),
                body.get("start"),
                body.get("end"),
            )
            return self._json({"ok": True, "message": msg})
        if path == "/api/agents/memory":
            agents = HUB.set_agent_memory(body.get("agent") or "", bool(body.get("native", False)))
            return self._json({"ok": True, "agents": agents})
        if path == "/api/agents/model":
            agents = HUB.set_agent_model(
                body.get("agent") or "",
                body.get("model") if "model" in body else None,
                body.get("model_provider") if "model_provider" in body else None,
                body.get("settings_profile") if "settings_profile" in body else None,
                body.get("base_url") if "base_url" in body else None,
                body.get("api_key") if "api_key" in body else None,
                body.get("style") if "style" in body else None,
            )
            return self._json({"ok": True, "agents": agents})
        if path == "/api/grants":
            agent = body.get("agent") or ""
            d = HUB.find_agent(agent)
            if not d:
                raise ValueError("未知成员")
            action = (body.get("action") or "add").strip()
            path_value = (body.get("path") or "").strip().strip('"')
            if action == "remove":
                grants = HUB.remove_grant(d["name"], path_value)
                HUB.post("system", d["name"], "event", f"{d['label']} 的权限已移除：{path_value}")
            else:
                if not path_value:
                    raise ValueError("路径不能为空")
                mode = "rw" if (body.get("mode") or "rw").lower() == "rw" else "ro"
                grants = HUB.set_grant(d["name"], path_value, mode)
                HUB.post(
                    "system", d["name"], "event",
                    f"{d['label']} 获得{'可读写' if mode == 'rw' else '只读'}权限：{path_value}",
                )
            HUB.broadcast({"type": "agents"})
            return self._json({"ok": True, "grants": grants})
        if path == "/api/config":
            patch = body.get("config") if isinstance(body.get("config"), dict) else body
            patch = {k: v for k, v in (patch or {}).items() if k != "ok"}
            if not patch:
                raise ValueError("没有要写入的配置")
            validate_permission_config(patch)
            deep_merge(CONFIG, patch)
            save_config()
            reload_config()
            HUB.max_depth = int(cfg_get("relay", {}).get("max_depth", 2))
            HUB.sync_grants_from_config()
            HUB.broadcast({"type": "config", "config": CONFIG})
            HUB.broadcast({"type": "agents"})
            return self._json({"ok": True, "config": redact_config(copy.deepcopy(CONFIG))})
        if path == "/api/perms/request":
            req = HUB.request_permission(
                body.get("agent") or "", body.get("path") or "",
                body.get("mode") or "rw", body.get("reason") or "",
            )
            return self._json({"ok": True, "perm": req})
        if path == "/api/perms/resolve":
            req = HUB.resolve_permission(body.get("id") or 0, bool(body.get("approve", True)))
            return self._json({"ok": True, "perm": req})
        if path == "/api/agents/relay":
            agents = HUB.set_agent_relay(body.get("agent") or "", bool(body.get("enabled", True)))
            return self._json({"ok": True, "agents": agents})
        if path == "/api/agents/toggle":
            agents = HUB.set_agent_enabled(body.get("agent") or "", bool(body.get("enabled", True)))
            return self._json({"ok": True, "agents": agents})
        if path == "/api/models":
            saved = HUB.add_model(body)
            return self._json({"ok": True, "model": {**saved, "api_key": mask_key(saved["api_key"])}})
        if path == "/api/models/delete":
            HUB.delete_model(body.get("id") or "")
            return self._json({"ok": True})
        if path == "/api/models/test":
            style = (body.get("style") or "").strip().lower()
            if style not in STYLES:
                raise ValueError("style 必须是 anthropic 或 openai")
            text = call_model(
                style,
                body.get("base_url") or "",
                body.get("api_key") or "",
                body.get("model") or "",
                [{"role": "user", "content": body.get("prompt") or "只回复两个字：可用"}],
                system="",
                max_tokens=64,
                timeout=60,
                path=body.get("path") or "",
            )
            return self._json({"ok": True, "reply": text})
        return self._json({"ok": False, "error": "not found"}, 404)

    # -- MCP Server（INT-01）：把中枢以 MCP 工具开放给任意客户端 ------------
    MCP_TOOLS = [
        {"name": "list_agents", "description": "列出启用中的成员（内部 id 与显示名）",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "send_message",
         "description": "把一条消息发给某个成员或全体；正文里 @ 谁，接力会自动触发谁。target 填成员 id 或 all，多个用逗号分隔",
         "inputSchema": {"type": "object",
                         "properties": {"target": {"type": "string"},
                                        "text": {"type": "string"}},
                         "required": ["target", "text"]}},
        {"name": "read_inbox",
         "description": "以某个成员的视角读最近消息（发给它的、发给全体的、它自己发的）",
         "inputSchema": {"type": "object",
                         "properties": {"agent": {"type": "string"},
                                        "limit": {"type": "integer"}},
                         "required": ["agent"]}},
        {"name": "get_history", "description": "读取群聊最近的可见消息",
         "inputSchema": {"type": "object",
                         "properties": {"limit": {"type": "integer"}}}},
        {"name": "list_tasks", "description": "列出进行中的任务",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "set_context",
         "description": "更新群公告/共享上下文（全部成员下一轮可见；会作废前缀缓存）",
         "inputSchema": {"type": "object",
                         "properties": {"text": {"type": "string"}},
                         "required": ["text"]}},
    ]

    @staticmethod
    def _mcp_text(text):
        return {"content": [{"type": "text", "text": str(text)[:20000]}]}

    def _mcp_call(self, name, args):
        if name == "list_agents":
            return self._mcp_text(json.dumps(
                [{"name": a["name"], "label": a["label"], "kind": a.get("kind")}
                 for a in HUB.agent_defs() if a.get("enabled")], ensure_ascii=False))
        if name == "send_message":
            names = HUB.dispatch(args.get("target") or "all", args.get("text") or "",
                                 sender="mcp")
            return self._mcp_text("已投递给：" + "、".join(names))
        if name == "read_inbox":
            rows = HUB.store.inbox(args.get("agent") or "", int(args.get("limit") or 30))
            return self._mcp_text(json.dumps(rows, ensure_ascii=False))
        if name == "get_history":
            rows = HUB.store.recent(int(args.get("limit") or 30))
            return self._mcp_text(json.dumps(
                [{"id": m["id"], "sender": m["sender"], "target": m["target"],
                  "text": m["text"]} for m in rows], ensure_ascii=False))
        if name == "list_tasks":
            return self._mcp_text(json.dumps(
                HUB.store.task_list(HUB.store.active_conv(), status="open"),
                ensure_ascii=False))
        if name == "set_context":
            text = str(args.get("text") or "")
            HUB.store.state_set("context", text)
            HUB.store.invalidate_prompt_prefix(HUB.store.active_conv())
            return self._mcp_text("共享上下文已更新（前缀缓存已作废）")
        raise ValueError("未知工具：" + str(name))

    def _mcp(self, body):
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            requested = str((body.get("params") or {}).get("protocolVersion") or "")
            return self._json({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": requested or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claudelink-hub", "version": APP_VERSION},
            }})
        if rid is None or str(method or "").startswith("notifications/"):
            # 通知不回 JSON-RPC 包：204 即「已收到」
            self.send_response(204)
            self.end_headers()
            return None
        if method == "tools/list":
            result = {"tools": self.MCP_TOOLS}
        elif method == "tools/call":
            params = body.get("params") or {}
            try:
                result = self._mcp_call(params.get("name"), params.get("arguments") or {})
            except Exception as exc:
                result = {"content": [{"type": "text", "text": "错误：" + repr(exc)}],
                          "isError": True}
        elif method == "ping":
            result = {}
        else:
            return self._json({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32601, "message": str(method)}})
        return self._json({"jsonrpc": "2.0", "id": rid, "result": result})



def main():
    # 只保证「确实要用」的目录存在：配置里的 cwd 已由 _resolve_workspace 校验过，
    # 不存在的旧路径不会被就地重建（否则成员会在一个空气目录里工作）。
    os.makedirs(DEFAULT_CWD, exist_ok=True)
    if CWD_WARNING:
        print("[hub] " + CWD_WARNING, flush=True)
        try:
            HUB.post("system", "all", "event", CWD_WARNING)
        except Exception:
            pass
    threading.excepthook = _thread_excepthook
    swept = sweep_stale_children()
    if swept:
        print(f"[hub] 清理上次残留的子进程: {swept}", flush=True)
    atexit.register(HUB.stop_children)

    def _graceful(signum, frame):
        HUB.stop_children()
        raise SystemExit(0)

    for _sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _graceful)
        except Exception:
            pass
    if cfg_get("nexus_mirror", True):
        threading.Thread(target=HUB.nexus_loop, daemon=True).start()
    # 预热 codebuddy 的模型清单（要跑一次 `codebuddy --help`，约 2 秒）：
    # 放到后台，别让用户第一次打开成员面板时干等
    def _warm_model_choices():
        for name in ("codebuddy", "codex", "claude"):
            try:
                HUB.model_choices(name)
            except Exception:
                pass

    threading.Thread(target=_warm_model_choices, daemon=True).start()
    if not HUB.store.recent(1):
        HUB.post("system", "all", "event", "群聊中枢已启动：可以发给全体，也可以只跟某一方说话。")
    host = cfg_get("host", "127.0.0.1")
    port = int(cfg_get("port", 8787))
    # 打一行成员命令的落点：WinError 2 这类"谁的 PATH 少了什么"一眼能看出来。
    # 必须走 cli_exe/cli_node：配置里存的是 %CLAUDELINK_*% 占位符，直接读 exe 打印的是假路径，
    # 而这行本来就是拿来排查 WinError 2 的。
    print(f"[hub] node: {resolve_command(cli_node())}", flush=True)
    for _name in (CONFIG.get("agents", {}) or {}):
        try:
            print(f"[hub] {_name}: {resolve_command(cli_exe(_name))}", flush=True)
            print(f"[hub] permission: {_name}={HUB.permission_mode(_name)}"
                  f" ({HUB.permission_enforcement(_name)})", flush=True)
        except Exception:
            pass
    ensure_server_token()
    print(f"[hub] auth: {'on' if cfg_get('server.auth', True) else 'off'}"
          f" token={TOKEN_FILE}", flush=True)
    httpd = HubHTTPServer((host, port), Handler)
    from hub_connection import register_instance, unregister_instance
    try:
        instance = register_instance(host, port, DATA_DIR, CONFIG_PATH)
        atexit.register(unregister_instance, instance)
    except OSError as exc:
        print("[hub] 本机实例登记失败：" + type(exc).__name__, flush=True)
    HUB.httpd = httpd          # 界面「关闭」键要用它来停 serve_forever
    print(f"[hub] http://{host}:{port}  (cwd={DEFAULT_CWD})", flush=True)
    print("[hub] Ctrl+C 退出", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
