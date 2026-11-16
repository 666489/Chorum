#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查「持锁时又调用会加锁的方法」—— Store.lock 是非重入 Lock，这种调用会直接把中枢卡死。

用法：python tools/check_locks.py
输出为空 = 没有这类嵌套加锁。

来历：2026-09-28 加会话隔离时，add_message / open_turn / trace_list 里在 `with self.lock`
内部调用了同样要加锁的 active_conv()，导致整个中枢（连 /api/health 都）永久卡住，只能重启。
"""
import ast
import os
import sys

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HUB, "hub.py")

# 会自己加 Store.lock 的方法（新增这类方法时记得加进来）
LOCKED = {
    "state_get", "state_set", "active_conv", "set_active_conv",
    "list_highlights", "attachments_for_message", "get", "recent",
    "list_conversations", "get_conversation", "update_conversation",
    "touch_conversation", "autotitle_conversation", "create_conversation",
    "delete_conversation", "delete_conv_content", "add_message", "open_turn",
    "trace_list", "recent_turns", "health_counts",
}


def main():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    bad = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.With):
                    continue
                holds = any(isinstance(i.context_expr, ast.Attribute)
                            and i.context_expr.attr == "lock" for i in node.items)
                if not holds:
                    continue
                for call in [c for c in ast.walk(node) if isinstance(c, ast.Call)]:
                    # 只看 self.xxx(...)：形如 item.get(...) 是普通 dict.get，不算
                    if not (isinstance(call.func, ast.Attribute)
                            and isinstance(call.func.value, ast.Name)
                            and call.func.value.id == "self"):
                        continue
                    name = call.func.attr
                    if name in LOCKED and name != fn.name:
                        bad.append(f"{cls.name}.{fn.name}() 持锁时调用 self.{name}()  (hub.py:{call.lineno})")
    if bad:
        print("发现嵌套加锁（会死锁，必须改成先取快照再进锁）：")
        for b in bad:
            print("  ", b)
        sys.exit(1)
    print("== 没有嵌套加锁 ==")


if __name__ == "__main__":
    main()
