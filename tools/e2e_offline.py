# -*- coding: utf-8 -*-
"""离线工具端到端冒烟：走 FastMCP.call_tool 全链路，不需要 PLECS 在运行。

覆盖 P1 新增的 5 个工具：plecs_trace_read / plecs_file_info / _blocks / _get / _set
（其中 _set 只做 dry_run，另用临时副本做真实写盘验证）。
"""
import asyncio
import io
import os
import shutil
import sys
import tempfile

ROOT = r"C:\Users\74281\.workbuddy\mcp-servers\plecs"
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import server  # noqa: E402

MODEL = r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\5-level-c.plecs"
T_LVRT = r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\lvrt\_lvrt4.trace"
T_SMALL = r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\lvrt\_scope_only.trace"
T_BIG = r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\scope_data\1.trace"

mcp = server.build_server({"host": "localhost", "port": 1080,
                           "timeout": 15.0, "long_timeout": 600.0})

FAIL = []


def call(name, **kw):
    res = asyncio.run(mcp.call_tool(name, kw))
    # FastMCP 返回 (content_list, meta) 或 content_list
    if isinstance(res, tuple):
        res = res[0]
    txt = []
    for c in res:
        txt.append(getattr(c, "text", str(c)))
    return "\n".join(txt)


def check(label, fn, must=(), expect_error=False):
    print("\n" + "=" * 72)
    print("▶", label)
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001
        print("✗ 异常:", type(e).__name__, e)
        FAIL.append(label)
        return
    print(out[:1600])
    bad = [k for k in must if k not in out]
    if bad:
        print("✗ 缺少关键内容:", bad)
        FAIL.append(label)
    elif not expect_error and out.lstrip().startswith("✗"):
        print("✗ 工具报错")
        FAIL.append(label)


# ── 1. .trace 解析 ──
check("trace_read 3 曲线（LVRT）",
      lambda: call("plecs_trace_read", trace_path=T_LVRT),
      must=("曲线：", "rms="))

check("trace_read 单曲线 + 前 3 点样本",
      lambda: call("plecs_trace_read", trace_path=T_SMALL, head=3),
      must=("样本：",))

tmpd = tempfile.mkdtemp()
csv = os.path.join(tmpd, "lvrt.csv")
check("trace_read 导 CSV",
      lambda: call("plecs_trace_read", trace_path=T_LVRT, save_csv=csv),
      must=("CSV 已写盘",))
if os.path.exists(csv):
    with open(csv, encoding="utf-8") as fh:
        head = [next(fh) for _ in range(3)]
    print("  CSV 前 2 行：", head[0].strip()[:120], "|", head[1].strip()[:120])
    print("  CSV 行数：", sum(1 for _ in open(csv, encoding="utf-8")))
else:
    print("✗ CSV 未生成")
    FAIL.append("csv")

check("trace_read 112 MB（自动统计模式）",
      lambda: call("plecs_trace_read", trace_path=T_BIG),
      must=("自动切到统计模式", "曲线："))

check("trace_read 不存在的文件",
      lambda: call("plecs_trace_read", trace_path=os.path.join(tmpd, "nope.trace")),
      must=("文件不存在",), expect_error=True)

# ── 2. .plecs 离线解析 ──
check("file_info 概览",
      lambda: call("plecs_file_info", path=MODEL, tree_depth=1),
      must=("模型名：5-level-c", "组件总数：229", "radau", "InitCommands"))

check("file_blocks 按类型筛 脚本块",
      lambda: call("plecs_file_blocks", path=MODEL, kind="CScript", ports=True),
      must=("环路控制", "NumInputs"))

check("file_blocks 按名字筛 + 端口（Subsystem）",
      lambda: call("plecs_file_blocks", path=MODEL, query="3LEVEL1", ports=True),
      must=("Port", "Direction" if False else "Input"))

check("file_blocks 子串筛电阻",
      lambda: call("plecs_file_blocks", path=MODEL, kind="Resistor", limit=5),
      must=("命中",))

check("file_get 锁相环全部参数",
      lambda: call("plecs_file_get", path=MODEL, block="锁相环"),
      must=("Ts", "1/fsw", "Parameters"),)

check("file_get 单个参数（模型级 TimeSpan）",
      lambda: call("plecs_file_get", path=MODEL, block="@", var="TimeSpan"),
      must=("TimeSpan = 0.05",))

check("file_get 不存在的参数",
      lambda: call("plecs_file_get", path=MODEL, block="锁相环", var="NoSuchVar"),
      must=("没有参数",), expect_error=True)

check("file_set dry_run（不写盘）",
      lambda: call("plecs_file_set", path=MODEL, block="锁相环",
                   var="Parameters", value="60, fsw, 1.414, 100, 5001", dry_run=True),
      must=("dry-run", "5001"))

# ── 3. 真实写盘（在副本上做）──
copy = os.path.join(tmpd, "copy.plecs")
shutil.copy2(MODEL, copy)
size0 = os.path.getsize(copy)
check("file_set 真实写盘（副本）",
      lambda: call("plecs_file_set", path=copy, block="锁相环",
                   var="Parameters", value="60, fsw, 1.414, 100, 4999"),
      must=("回读校验通过", "备份"))
check("file_set 后回读确认",
      lambda: call("plecs_file_get", path=copy, block="锁相环", var="Parameters"),
      must=("4999",))
baks = [x for x in os.listdir(tmpd) if ".bak" in x]
print(f"\n  副本大小 {size0} → {os.path.getsize(copy)}；备份 {baks}")
if not baks:
    FAIL.append("backup")

shutil.rmtree(tmpd, ignore_errors=True)

print("\n" + "=" * 72)
print("全部通过 ✓" if not FAIL else f"失败 {len(FAIL)} 项：{FAIL}")
sys.exit(1 if FAIL else 0)
