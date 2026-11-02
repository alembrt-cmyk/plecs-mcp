# -*- coding: utf-8 -*-
"""smoke_full.py — 一条龙冒烟：connect → load → get → set TimeSpan → simulate → scope → close"""
import json, sys, time
sys.path.insert(0, r"C:\Users\74281\.workbuddy\mcp-servers\plecs")
from plecs_client import PlecsClient, PlecsError

FILE = r"C:/Users/74281/WorkBuddy/WORKBUDDY_TEST/5-level-c.plecs"
MODEL = "5-level-c"

c = PlecsClient(host="localhost", port=1080)
stats = c.connect()
print("== statistics 全字段 ==")
print(json.dumps(stats, ensure_ascii=False, indent=1))
print()

# 载入（若已开先关）
ids = [m["id"] for m in stats["models"]]
if MODEL in ids:
    c.close(MODEL)
    print("已关闭旧实例")

name = c.load(FILE)
print("load →", name)
print()

print("== plecs.get(模型, TimeSpan) ==", c.get(MODEL, "TimeSpan"))
print("== plecs.get(模型) 全参数键 ==", list(c.get(MODEL).keys()) if isinstance(c.get(MODEL), dict) else type(c.get(MODEL)))
print()

init = c.get(MODEL, "InitializationCommands")
print("== InitializationCommands ==", len(init) if isinstance(init, str) else type(init), "字符")
print()

c.set_param(MODEL, "TimeSpan", "0.01")
print("== set TimeSpan=0.01 →", c.get(MODEL, "TimeSpan"))
print()

t0 = time.time()
res = c.simulate(MODEL, {"SolverOpts": {"TimeSpan": 0.01}, "ModelVars": {}})
print("== simulate 返回 ==", type(res), "keys:", list(res.keys()) if isinstance(res, dict) else "-",
      "耗时 %.2f s" % (time.time() - t0))
if isinstance(res, dict) and res.get("Time"):
    print("   Time 点数:", len(res["Time"]), "Values 通道:", len(res.get("Values") or []))
print()

# scope 导出 CSV + GetCursorData
CSV = r"C:/Users/74281/WorkBuddy/WORKBUDDY_TEST/scope_data/mcp_smoke.csv"
for scope in (MODEL + "/Scope2", MODEL + "/Scope"):
    try:
        r = c.scope(scope, "ExportCSV", CSV)
        print(f"== scope {scope} ExportCSV →", r, "（文件存在待验证）")
        break
    except PlecsError as e:
        print(f"== scope {scope} ExportCSV 失败：{str(e)[:100]}")

try:
    r = c.scope(MODEL + "/Scope2", "GetCursorData", [0.005, 0.006], "rms")
    print("== GetCursorData rms ==", json.dumps(r, ensure_ascii=False, default=str)[:400])
except PlecsError as e:
    print("== GetCursorData 失败：", str(e)[:120])

print()
st2 = c.statistics()
busy = [m["id"] for m in st2["models"] if m.get("simulating")]
print("== 仿真后 simulating 列表 ==", busy or "（无，可安全 close）")
c.close(MODEL)
print("== close 完成 ==")
import os
print("== CSV 落地 ==", os.path.exists(CSV), os.path.getsize(CSV) if os.path.exists(CSV) else 0, "bytes")
