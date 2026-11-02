# -*- coding: utf-8 -*-
"""smoke_demo.py — 用官方 demo 验证 simulate 返回/格式化 + scope 导出 + init 变量读写"""
import json, os, sys, time
sys.path.insert(0, r"C:\Users\74281\.workbuddy\mcp-servers\plecs")
from plecs_client import PlecsClient, PlecsError

FILE = r"D:/PLECS/demos/buck_converter_with_parameter_sweep/buck_converter_with_parameter_sweep.plecs"
MODEL = "buck_converter_with_parameter_sweep"
CSV = r"C:/Users/74281/WorkBuddy/WORKBUDDY_TEST/scope_data/mcp_smoke_demo.csv"

c = PlecsClient(host="localhost", port=1080)
stats = c.connect()
print("version:", stats["version"], "| build:", stats.get("build", ""))
ids = [m["id"] for m in stats["models"]]
if MODEL in ids:
    c.close(MODEL)

name = c.load(FILE)
print("load →", name)

# --- simulate（demo 顶层有 Outport → 应返回 Time/Values）---
t0 = time.time()
res = c.simulate(MODEL, {"ModelVars": {"varL": 50e-6}, "SolverOpts": {"TimeSpan": 0.002}})
dt = time.time() - t0
print("simulate: keys=%s Time点数=%s 通道数=%s 耗时%.2fs"
      % (list(res.keys()), len(res.get("Time") or []),
         len(res.get("Values") or []), dt))
if res.get("Values"):
    v0 = res["Values"][0]
    print("  通道0: min=%.4g max=%.4g n=%d" % (min(v0), max(v0), len(v0)))

# --- scope ExportCSV ---
try:
    r = c.scope(MODEL + "/Scope", "ExportCSV", CSV)
    print("ExportCSV →", r, "| 落地:", os.path.exists(CSV),
          os.path.getsize(CSV) if os.path.exists(CSV) else 0, "B")
except PlecsError as e:
    print("ExportCSV 失败:", str(e)[:150])

# --- scope GetCursorData ---
try:
    r = c.scope(MODEL + "/Scope", "GetCursorData", [0.001, 0.002], "rms", "max")
    print("GetCursorData →", json.dumps(r, ensure_ascii=False, default=str)[:300])
except PlecsError as e:
    print("GetCursorData 失败:", str(e)[:150])

# --- HoldTrace / SaveTraces / LoadTraces ---
TRACE = r"C:/Users/74281/WorkBuddy/WORKBUDDY_TEST/scope_data/mcp_smoke_demo.trace"
try:
    r = c.scope(MODEL + "/Scope", "HoldTrace", "smoke1")
    print("HoldTrace →", r)
    c.scope(MODEL + "/Scope", "SaveTraces", TRACE)
    print("SaveTraces 落地:", os.path.exists(TRACE),
          os.path.getsize(TRACE) if os.path.exists(TRACE) else 0, "B")
    c.scope(MODEL + "/Scope", "ClearTraces")
    c.scope(MODEL + "/Scope", "LoadTraces", TRACE)
    print("LoadTraces OK")
except PlecsError as e:
    print("trace 链失败:", str(e)[:150])

# --- init 变量读写（demo 的 init 里有 varL/varR）---
init = c.get(MODEL, "InitializationCommands")
print("init 长度:", len(init) if isinstance(init, str) else type(init))
if isinstance(init, str):
    import re
    m = re.search(r'^\s*varL\s*=\s*([^;]+);', init, re.M)
    print("varL 行:", m.group(0).strip() if m else "（找不到）")
    # 改 varL=1e-4 写回
    if m:
        new = init[:m.start()] + "varL = 1e-4;" + init[m.end():]
        c.set_param(MODEL, "InitializationCommands", new)
        back = c.get(MODEL, "InitializationCommands")
        ok = re.search(r'^\s*varL\s*=\s*1e-4;', back or "", re.M)
        print("init 写回验证:", "OK" if ok else "FAIL")
        c.set_param(MODEL, "InitializationCommands", init)  # 还原

c.close(MODEL)
print("close OK")
