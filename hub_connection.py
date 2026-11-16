"""Locate a local hub's data directory without exposing credentials over HTTP."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import uuid
import urllib.error
import urllib.request

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def runtime_dir():
    return Path(os.environ.get("CLAUDELINK_RUNTIME_DIR") or Path.home() / ".claudelink" / "instances")


def _local(host):
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::")


def register_instance(host, port, data_dir, config_path):
    """Local metadata only; no token contents. Called after successful bind."""
    if not _local(host):
        return None
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (str(int(port)) + ".json")
    value = {"pid": os.getpid(), "nonce": uuid.uuid4().hex,
             "port": int(port), "data_dir": os.path.abspath(data_dir),
             "config_path": os.path.abspath(config_path)}
    tmp = path.with_suffix(".tmp-" + value["nonce"])
    try:
        tmp.write_text(json.dumps(value), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    remember_connection(value)
    return value


def remember_connection(value):
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "default.json"
    tmp = directory / ("default.tmp-" + uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps({k:value[k] for k in ("data_dir","config_path","port") if k in value}), encoding="utf-8")
        os.replace(tmp,target)
    finally:
        if tmp.exists(): tmp.unlink()


def unregister_instance(value):
    if not value:
        return
    path = runtime_dir() / (str(value["port"]) + ".json")
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("nonce") == value["nonce"]:
            path.unlink()
    except (OSError, ValueError):
        pass


def _process_commands(port):
    """Legacy server fallback. Inspect only the process listening on this port."""
    try:
        if os.name == "nt":
            from hub_pid import find_listening_pids
            pids = find_listening_pids(int(port))
            if not pids:
                return []
            condition = " OR ".join("ProcessId=" + str(int(p)) for p in pids)
            command = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter '" + condition +
                       "' | Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress")
            powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if not powershell:
                return []
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, timeout=8, creationflags=NO_WINDOW)
            commands = json.loads(result.stdout.decode("utf-8-sig", "replace"))
            return [commands] if isinstance(commands, str) else commands or []
        lsof = shutil.which("lsof")
        if not lsof:
            return []
        result = subprocess.run([lsof, "-nP", "-t", "-iTCP:" + str(int(port)), "-sTCP:LISTEN"],
                                capture_output=True, text=True, timeout=4)
        commands = []
        for pid in result.stdout.split():
            if pid.isdigit():
                proc = subprocess.run(["ps", "-p", pid, "-o", "args="], capture_output=True, text=True, timeout=3)
                commands.append(proc.stdout.strip())
        return commands
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def connection_candidates(host, port):
    """Candidates still need authenticated verification against the target server."""
    if not _local(host):
        return []
    candidates = []
    try:
        record = json.loads((runtime_dir() / (str(int(port)) + ".json")).read_text(encoding="utf-8"))
        if int(record.get("port", 0)) == int(port):
            candidates.append(record)
    except (OSError, ValueError, TypeError):
        pass
    # Old servers have no registry; infer the package directory from their exact argv.
    for command in _process_commands(port):
        try:
            args = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            continue
        for arg in args:
            arg = arg.strip('"')
            if os.path.basename(arg).lower() != "hub.py" or not os.path.isabs(arg):
                continue
            directory = str(Path(arg).resolve().parent)
            candidates.append({"data_dir":directory, "config_path":str(Path(directory) / "config.json"), "port":int(port)})
    return candidates


def resolve_connection(host, port, data_dir, config_path, shutdown=False, allow_busy=False):
    """CLI control plane. Return paths/version only, never credentials or messages."""
    if not _local(host):
        return {"ok":False,"running":False,"error":"自动连接只支持本机中枢"}
    host = "127.0.0.1" if host in ("0.0.0.0","::") else host
    base = "http://" + ("["+host+"]" if ":" in host else host) + ":" + str(int(port))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base+"/api/health",timeout=2) as response: health=json.load(response)
    except (OSError,ValueError):
        return {"ok":False,"running":False,"data_dir":data_dir,"config_path":config_path,"port":int(port)}
    preferred={"data_dir":data_dir,"config_path":config_path,"port":int(port)}
    def candidates():
        yield preferred
        if not os.environ.get("CLAUDELINK_TOKEN"):
            yield from connection_candidates(host,port)
    for info in candidates():
        try:
            token=os.environ.get("CLAUDELINK_TOKEN") or (Path(info["data_dir"])/"hub.token").read_text(encoding="utf-8").strip()
        except OSError:
            token=""
        headers={"X-ClaudeLink-Token":token} if token else {}
        try:
            req=urllib.request.Request(base+"/api/context",headers=headers)
            with opener.open(req,timeout=2) as response: json.load(response)
            busy=bool(health.get("running"))
            for endpoint,field in (("/api/queue","open"),("/api/workflows","workflows")):
                try:
                    with opener.open(urllib.request.Request(base+endpoint,headers=headers),timeout=2) as response: data=json.load(response)
                    if endpoint.endswith("queue"): busy |= bool(data.get(field))
                    else: busy |= any(w.get("status") in ("planning","running","stopping") for w in data.get(field,[]))
                except (OSError,ValueError): pass
            try:
                with opener.open(urllib.request.Request(base+"/api/roundtable",headers=headers),timeout=2) as response: data=json.load(response)
                rt=data.get("roundtable") or {}
                busy |= rt.get("state") in ("running","pausing","stopping")
            except (OSError,ValueError): pass
            result={"ok":True,"running":True,"busy":busy,"version":health.get("version"),"code_revision":health.get("code_revision"),
                    "pid":health.get("pid"),"data_dir":info["data_dir"],"config_path":info.get("config_path"),"port":int(port)}
            if shutdown:
                if busy and not allow_busy: return dict(result,ok=False,error="中枢有正在执行或排队的任务，不能自动升级")
                req=urllib.request.Request(base+"/api/shutdown",data=b"{}",headers={**headers,"Content-Type":"application/json"})
                with opener.open(req,timeout=5) as response: data=json.load(response)
                if not data.get("ok"): return dict(result,ok=False,error=data.get("error") or "停止失败")
                result["stopped"]=True
            else:
                remember_connection(result)
            return result
        except (OSError,ValueError):
            continue
    return {"ok":False,"running":True,"error":"中枢在线，但当前配置无法鉴权；未启动第二个实例"}


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8787)
    parser.add_argument("--data-dir",required=True)
    parser.add_argument("--config",required=True)
    parser.add_argument("--shutdown",action="store_true")
    parser.add_argument("--allow-busy",action="store_true")
    args=parser.parse_args()
    print(json.dumps(resolve_connection(args.host,args.port,args.data_dir,args.config,args.shutdown,args.allow_busy),ensure_ascii=False))
