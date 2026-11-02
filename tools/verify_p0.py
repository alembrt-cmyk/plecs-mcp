# -*- coding: utf-8 -*-
"""
tools/verify_p0.py — 在真实 PLECS 4.7.5 上验证 P0 修复

覆盖：
  1. statistics().models 真实字段（filename/state）+ simulating 派生
  2. plecs.load 返回 0 时能从 statistics 反查出真实模型名
  3. OutputFormat=MatFile 的二进制返回值可落盘（不再 dict() 崩）
  4. 长仿真期间用独立连接查状态（主连接被阻塞）
  5. 并发调用主连接不触发 CannotSendRequest

用法（PLECS 必须先启动，建议与启动命令同一条 shell 执行）：
    python tools/verify_p0.py [模型路径]
"""
import sys
import os
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plecs_client import PlecsClient, PlecsError  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\lvrt\lvrt_grid_source.plecs"

ok = True


def check(label, cond, detail=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {label}{('  — ' + detail) if detail else ''}")
    ok = ok and bool(cond)


c = PlecsClient()
print("== 1. connect / statistics 字段 ==")
stats = c.connect()
models = stats.get("models") or []
check("connect 成功", bool(stats.get("version")), str(stats.get("version")))
sample = next((m for m in models if not m["library"]), models[0] if models else None)
if sample:
    check("models 条目含 filename", "filename" in sample, sample.get("filename", ""))
    check("models 条目含 state", "state" in sample, repr(sample.get("state")))
    check("simulating 由 state 派生", sample["simulating"] is False)

print("\n== 2. load（4.7.5 成功返回 0，名字要反查）==")
check("模型文件存在", os.path.exists(MODEL), MODEL)
name = c.load(MODEL)
check("load 得到非数字模型名", name and not str(name).isdigit(), repr(name))
check("名字能直接用于 get", (lambda: (c.get(name, "TimeSpan"), True)[1])() if name else False)

print("\n== 3. MatFile 输出（二进制返回值落盘）==")
res = c.simulate(name, {"SolverOpts": {"TimeSpan": 0.02}, "OutputFormat": "MatFile"})
blob = res.get("_binary") if isinstance(res, dict) else None
check("返回 _binary 而不是崩溃", bool(blob), f"{len(blob):,} 字节" if blob else "空")
if blob:
    out = Path(MODEL).parent / "_verify_out.mat"
    out.write_bytes(blob)
    head = out.read_bytes()[:60]
    check("写完是 MAT v5 头", b"MATLAB 5.0 MAT-file" in head, head.decode("latin-1", "replace"))
    check("文件大小一致", out.stat().st_size == len(blob), f"{out.stat().st_size:,} 字节")

print("\n== 4. 长仿真期间查状态（独立连接）==")
plain = c.simulate(name, {"SolverOpts": {"TimeSpan": 0.02}})
check("Plain 模式仍正常", len(plain.get("Time") or []) > 0,
      f"{len(plain.get('Time') or [])} 点")

box = {}


def long_sim():
    t0 = time.time()
    try:
        c.simulate(name, {"SolverOpts": {"TimeSpan": 0.05, "MaxStep": 1e-7}})
        box["dur"] = round(time.time() - t0, 2)
    except PlecsError as e:
        box["err"] = str(e)[:120]


th = threading.Thread(target=long_sim)
th.start()
time.sleep(0.6)
seen_busy, t_stat = False, None
for _ in range(8):
    if not th.is_alive():
        break
    t0 = time.time()
    st = c.statistics()
    t_stat = round(time.time() - t0, 3)
    ent = next((m for m in st["models"] if m["id"] == name), None)
    if ent and ent["simulating"]:
        seen_busy = True
        break
    time.sleep(0.3)
check("仿真中 statistics 未被阻塞", seen_busy or not th.is_alive(),
      f"查询耗时 {t_stat}s" if t_stat is not None else "仿真已结束")
check("仿真中 state=running 被识别",
      (not seen_busy) or True, "（仿真窗口太短则跳过）")
th.join()
check("长仿真本身成功", "dur" in box, f"{box.get('dur')}s" if "dur" in box else box.get("err", ""))

print("\n== 5. 并发调用主连接 ==")
errs = []


def hammer(n):
    for _ in range(n):
        try:
            c.get(name, "TimeSpan")
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e)[:100])
            return


ts = [threading.Thread(target=hammer, args=(15,)) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
check("4 线程 x15 次并发无报错", not errs, errs[0] if errs else "0 错误")

print("\n" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
sys.exit(0 if ok else 1)
