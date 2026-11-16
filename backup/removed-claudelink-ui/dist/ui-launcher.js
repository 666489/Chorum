"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.launchUIIfNeeded = launchUIIfNeeded;
exports.stopUI = stopUI;
exports.getUIStatus = getUIStatus;
const http_1 = __importDefault(require("http"));
const path_1 = __importDefault(require("path"));
const os_1 = __importDefault(require("os"));
const fs_1 = __importDefault(require("fs"));
const child_process_1 = require("child_process");
const NEXUS_DIR = path_1.default.join(os_1.default.homedir(), ".claudelink");
const LOCK_PATH = path_1.default.join(NEXUS_DIR, "ui.lock");
const DEFAULT_PORT = 7878;
function isProcessAlive(pid) {
    try {
        process.kill(pid, 0);
        return true;
    }
    catch {
        return false;
    }
}
function readLock() {
    try {
        return JSON.parse(fs_1.default.readFileSync(LOCK_PATH, "utf8"));
    }
    catch {
        return null;
    }
}
async function pingHeartbeat(port, timeoutMs = 800) {
    return new Promise((resolve) => {
        const req = http_1.default.get({ hostname: "127.0.0.1", port, path: "/api/heartbeat", timeout: timeoutMs }, (res) => {
            const ok = res.statusCode === 200;
            res.resume();
            resolve(ok);
        });
        req.on("error", () => resolve(false));
        req.on("timeout", () => { req.destroy(); resolve(false); });
    });
}
function openInBrowser(url) {
    try {
        if (process.platform === "darwin") {
            (0, child_process_1.spawn)("open", [url], { stdio: "ignore", detached: true }).unref();
        }
        else if (process.platform === "win32") {
            (0, child_process_1.spawn)("cmd", ["/c", "start", "", url], { stdio: "ignore", detached: true }).unref();
        }
        else {
            (0, child_process_1.spawn)("xdg-open", [url], { stdio: "ignore", detached: true }).unref();
        }
    }
    catch {
        // browser failure is non-fatal — UI still reachable at the URL
    }
}
/**
 * Spawn the UI server as a detached process if one isn't already running,
 * and open the browser. Safe to call repeatedly.
 *
 * Returns the URL of the running UI, or null if it couldn't be started.
 */
async function launchUIIfNeeded(opts = {}) {
    if (process.env.CLAUDELINK_UI === "off")
        return null;
    if (!fs_1.default.existsSync(NEXUS_DIR)) {
        try {
            fs_1.default.mkdirSync(NEXUS_DIR, { recursive: true });
        }
        catch { }
    }
    const existing = readLock();
    if (existing && isProcessAlive(existing.pid)) {
        const alive = await pingHeartbeat(existing.port);
        if (alive) {
            return `http://127.0.0.1:${existing.port}`;
        }
    }
    // Lock is stale or the previous UI died. Remove it so we can start fresh.
    try {
        fs_1.default.unlinkSync(LOCK_PATH);
    }
    catch { }
    const uiBin = path_1.default.join(__dirname, "ui-bin.js");
    if (!fs_1.default.existsSync(uiBin))
        return null;
    const child = (0, child_process_1.spawn)(process.execPath, [uiBin, String(DEFAULT_PORT)], {
        detached: true,
        stdio: "ignore",
        env: { ...process.env },
    });
    child.unref();
    // Wait briefly for the child to bind + write the lock.
    for (let i = 0; i < 25; i++) {
        await new Promise((r) => setTimeout(r, 80));
        const lock = readLock();
        if (lock && isProcessAlive(lock.pid)) {
            const ok = await pingHeartbeat(lock.port);
            if (ok) {
                const url = `http://127.0.0.1:${lock.port}`;
                if (opts.openBrowser !== false)
                    openInBrowser(url);
                return url;
            }
        }
    }
    return null;
}
function stopUI() {
    const lock = readLock();
    if (!lock)
        return false;
    if (!isProcessAlive(lock.pid)) {
        try {
            fs_1.default.unlinkSync(LOCK_PATH);
        }
        catch { }
        return false;
    }
    try {
        process.kill(lock.pid, "SIGTERM");
        return true;
    }
    catch {
        return false;
    }
}
function getUIStatus() {
    const lock = readLock();
    if (!lock)
        return { running: false };
    if (!isProcessAlive(lock.pid))
        return { running: false };
    return {
        running: true,
        pid: lock.pid,
        port: lock.port,
        url: `http://127.0.0.1:${lock.port}`,
    };
}
//# sourceMappingURL=ui-launcher.js.map