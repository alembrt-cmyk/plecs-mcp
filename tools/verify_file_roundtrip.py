# -*- coding: utf-8 -*-
"""离线解析器往返校验：改参 → 写到临时副本 → 回读 → 只应有一行不同。"""
import sys, io, difflib, shutil, tempfile, os
sys.path.insert(0, r"C:\Users\74281\.workbuddy\mcp-servers\plecs")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from plecs_file import PlecsFile, render_tree, render_ports, render_params

SRC = r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\5-level-c.plecs"
tmpd = tempfile.mkdtemp()
DST = os.path.join(tmpd, "copy.plecs")
shutil.copy2(SRC, DST)

f = PlecsFile(DST)
print("model name:", f.model.attrs.get("Name"), "| TimeSpan:", f.model.attrs.get("TimeSpan"),
      "| Solver:", f.model.attrs.get("Solver"))
print("init 行数:", len(f.model.attrs.get("InitializationCommands", "").splitlines()))
print("\n=== 树（前 6 行）===\n" + "\n".join(render_tree(f).splitlines()[:6]))

node = f.resolve("T-3LEVEL1")
print("\n=== T-3LEVEL1 端口 ===\n" + render_ports(node))

print("\n=== 环路控制 参数（前 60 字截断）===\n" + render_params(f.resolve("环路控制"), head=60))

# 1) 单行标量替换
old_ts = f.model.attrs["TimeSpan"]
f.set_param("@", "TimeSpan", "0.08")
print("\nTimeSpan:", old_ts, "->", f.model.attrs["TimeSpan"])

# 2) 多行字符串替换（Declarations 变短 → 行数减少）
d = f.resolve("环路控制")
old_decl = d.params()["Declarations"]
f.set_param("环路控制", "Declarations", '#include "cscript_declarations_spwm.h"\n#include "spwm.h"\n')
print("Declarations 行数:", len(old_decl.splitlines()), "->",
      len(f.resolve("环路控制").params()["Declarations"].splitlines()))

# 3) base64 替换（中文名）
f.set_attr(f.resolve("锁相环"), "Name", "锁相环PLL")
print("改名后 resolve 命中:", f.resolve("锁相环PLL").attrs["Name"])

bak = f.save()
print("backup:", os.path.basename(bak))

# 回读
g = PlecsFile(DST)
print("\n回读：TimeSpan =", g.model.attrs["TimeSpan"],
      "| 组件数 =", len(g.components()),
      "| 锁相环PLL 的 Ts =", g.resolve("锁相环PLL").params()["Ts"])
print("回读 Declarations 行数:", len(g.resolve("环路控制").params()["Declarations"].splitlines()))

# diff（只应改动 3 处）
a = open(SRC, "rb").read().decode("utf-8").replace("\r\n", "\n").split("\n")
b = open(DST, "rb").read().decode("utf-8").replace("\r\n", "\n").split("\n")
dl = list(difflib.unified_diff(a, b, lineterm="", n=0))
changed = [l for l in dl if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
print(f"\ndiff 变更行数 = {len(changed)}（期望 3 处左右）")
for l in changed[:12]:
    print("   ", l[:110])

# 字节级：CRLF 是否保留
raw = open(DST, "rb").read()
print("\nCRLF 保留:", raw.count(b"\r\n") > 1000, "| 文件大小:", len(raw), "原:", os.path.getsize(SRC))
shutil.rmtree(tmpd, ignore_errors=True)
