"""按监听端口反查 hub 本体的 PID（restart-hub.bat 用）。

存在理由：restart-hub.bat 曾把 PID 写死成 30676，重启后 hub 变成 13760，
脚本就会「杀空 + 再拉起第二个 hub」→ 端口冲突。锚点必须每次从现场取。

只用 stdlib，中文/英文 Windows 的 netstat 输出列结构一致（状态词是英文）。
找不到监听者时输出空行、退出码 1 —— 让调用方自己决定"没在跑"要不要当成错误。

2026-09-26 修：原来只返回**第一个**监听者。Python 的 http.server 默认
allow_reuse_address，同一端口上可以并存 N 个 hub 实例，只杀一个、再拉一个，
实例数就从 1 涨到 2，每次重启涨一个（棘轮）。现在返回**全部**监听者，
一行空格分隔、升序去重；调用方必须全部杀掉并复查端口。
"""
import subprocess
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "8787"


def _netstat_text():
    """netstat -ano 的输出，解码成 str（失败返回 ""）。"""
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15).stdout or b""
    except Exception:
        return ""
    # netstat 的输出长在控制台代码页上（中文 Windows 是 GBK，不是 UTF-8）——
    # 用 text=True 会直接 UnicodeDecodeError。只取纯 ASCII 的列，所以按
    # utf-8 → gbk → cp437 依次试，全失败就用 replace 硬解，绝不返回 None。
    for enc in ("utf-8", "gbk", "cp437"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_listening_pids(port):
    """返回监听该端口的**全部** PID（升序去重的 str 列表）。找不到返回 []。"""
    needle = f":{port}"
    found = set()
    for line in _netstat_text().splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if not parts[1].endswith(needle):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if parts[4].isdigit():
            found.add(int(parts[4]))
    return [str(p) for p in sorted(found)]


def find_listening_pid(port):
    """兼容旧调用：只取一个。多实例时这是**不安全**的，请用 find_listening_pids。"""
    pids = find_listening_pids(port)
    return pids[0] if pids else ""


def port_answers(port, timeout=2.0):
    """TCP 连一下端口：有人应答 True，无人 False。

    这是比 netstat 更可靠的「端口是否被占」判据 —— netstat 解析一旦失败
    （见 restart-hub.bat NOTE 2），空结果会被读成「没人在跑」，从而跳过 kill
    再拉一个实例。connect 不会静默失败。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--probe":
        port = args[1] if len(args) > 1 else PORT
        # 退出码约定与 find_listening_pids 相反：0 = 有人在应答（被占）
        sys.exit(0 if port_answers(port) else 1)
    pids = find_listening_pids(PORT)
    print(" ".join(pids))
    sys.exit(0 if pids else 1)
