"""重启前一致性备份：用 sqlite3.Connection.backup() 而非文件复制。

WAL 模式下单独复制 hub.db 会丢未 checkpoint 的帧（DeepSeek §迁移与验证隔离 定的口径）。
"""
import os
import sqlite3

HUB = r"C:\Users\Administrator\Documents\Codex\2026-09-26\bang\outputs\claudelink-hub"
out_dir = os.path.join(HUB, ".verify", "pre-restart")
os.makedirs(out_dir, exist_ok=True)

src = os.path.join(HUB, "hub.db")
dst = os.path.join(out_dir, "hub-pre-restart.db")

con = sqlite3.connect("file:" + src.replace("\\", "/") + "?mode=ro", uri=True)
bck = sqlite3.connect(dst)
con.backup(bck)
bck.close()
con.close()

# 顺带留档当前 live 计数，供重启后对比
con = sqlite3.connect("file:" + dst.replace("\\", "/") + "?mode=ro", uri=True)
cur = con.cursor()
for sql, label in [
    ("SELECT COUNT(*) FROM turns", "turns"),
    ("SELECT COUNT(*) FROM steps", "steps"),
    ("SELECT COUNT(*) FROM turns WHERE out_chars=0", "out_chars=0"),
    ("SELECT COUNT(*) FROM turns WHERE trigger_msg_id IS NULL", "trigger_msg_id NULL"),
    ("SELECT COUNT(*) FROM steps WHERE status='running'", "running steps"),
]:
    try:
        print(f"{label:24s} = {cur.execute(sql).fetchone()[0]}")
    except Exception as e:
        print(f"{label:24s} ! {e}")
con.close()
print("backup ok:", dst, os.path.getsize(dst), "bytes")
