# -*- coding: utf-8 -*-
"""
trace_reader.py — PLECS `.trace` 波形文件解析器

★ 4.7.5 实测逆向（没有官方文档，靠 3 个样本字节级对齐）：
   文件为**大端**裸二进制，无魔数：

   i32 version(=2)
   i32 nTrace
   nTrace × { str name（长度 = -1 表示空）,
              i32 nSig,
              nSig × str sigName }
   i32 nLabel
   nLabel × str label                 # HoldTrace 的标签，如 "Saved trace"

   然后**文件级**时间基（只存一份）：
     ...（前缀：0,0,tEnd,若干 1,tEnd）
     5×i32 0
     i32 nPoints                       # 实际点数 = nPoints - 1
     i32 0, i32 0
     (nPoints-1) × f64   时间向量
   之后逐条曲线（先 trace 内再 trace 间，顺序 = 头部声明顺序）：
     [可变长前缀][f64 ymax][f64 ymin] 24B 尾（12B 0 + i32 点数 + 8B 0）
     (nPoints-1) × f64 数据
     ※ 前缀长度随曲线变化（1~8B），所以用 24B 尾当锚点、数据紧随其后。

   str = i32 字节长度 + UTF-16**BE** 正文。

   本解析器不依赖那些前缀字段，而是用「5×0 + nPoints + 2×0」这个锚点定位，
   再靠时间向量单调性做校验；解析完要求正好吃到文件末尾，否则报错。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TraceError", "TraceCurve", "read_trace", "summarize", "write_csv"]


class TraceError(RuntimeError):
    """`.trace` 解析失败"""


@dataclass
class TraceCurve:
    trace_label: str          # 所属 trace 名（Scope 轴名）
    signal: str               # 信号名
    t: list[float] = field(default_factory=list)
    v: list[float] = field(default_factory=list)
    ymax: float = 0.0
    ymin: float = 0.0
    _agg: dict | None = None      # keep=False 时的流式统计

    def stats(self) -> dict:
        if self._agg is not None:
            return self._agg
        v = self.v
        if not v:
            return {"n": 0, "min": 0.0, "max": 0.0, "avg": 0.0, "pp": 0.0, "rms": 0.0}
        mn, mx = min(v), max(v)
        avg = sum(v) / len(v)
        rms = (sum(x * x for x in v) / len(v)) ** 0.5
        return {"n": len(v), "min": mn, "max": mx, "avg": avg,
                "pp": mx - mn, "rms": rms}


class _Reader:
    def __init__(self, buf: bytes):
        self.b = buf
        self.o = 0
        self.n = len(buf)

    def need(self, k: int) -> None:
        if self.o + k > self.n:
            raise TraceError(f"数据越界：需要 {k} 字节 @ {self.o}，文件只有 {self.n} 字节")

    def i32(self) -> int:
        self.need(4)
        v = struct.unpack_from(">i", self.b, self.o)[0]
        self.o += 4
        return v

    def f64(self, k: int = 1) -> tuple:
        self.need(8 * k)
        v = struct.unpack_from(f">{k}d", self.b, self.o)
        self.o += 8 * k
        return v

    def s(self) -> str:
        n = self.i32()
        if n < 0:
            return ""
        if n > 1_000_000:
            raise TraceError(f"字符串长度异常（{n} @ {self.o - 4}），文件不是 .trace 或已损坏")
        self.need(n)
        txt = self.b[self.o:self.o + n].decode("utf-16-be", "replace")
        self.o += n
        return txt


def _find_anchor(r: _Reader, t_end_hint: float | None,
                 window: int | None = None) -> tuple[int, int] | None:
    """从当前位置找『5×i32 0 + i32 nPoints + 2×i32 0』锚点，返回 (时间数组起点, 点数-1)。

    校验：其后 nPts 个 f64 必须单调不减且落在 [0, 1.05*tEnd] 内。
    """
    start = r.o
    b, n = r.b, r.n
    o = start
    stop = n if window is None else min(n, start + window)
    while o + 32 <= stop:
        # 5 个 0
        if b[o:o + 20] != b"\x00" * 20:
            o += 4
            continue
        npts = struct.unpack_from(">i", b, o + 20)[0]
        if not (2 <= npts <= 20_000_000):
            o += 4
            continue
        if b[o + 24:o + 32] != b"\x00" * 8:
            o += 4
            continue
        t0 = o + 32
        cnt = npts - 1
        if t0 + 8 * cnt > n:
            o += 4
            continue
        # 抽检单调性（首、中、尾）
        try:
            first = struct.unpack_from(">d", b, t0)[0]
            mid = struct.unpack_from(">d", b, t0 + 8 * (cnt // 2))[0]
            last = struct.unpack_from(">d", b, t0 + 8 * (cnt - 1))[0]
        except struct.error:
            o += 4
            continue
        ok = (first >= -1e-12 and last >= first >= -1e-12 and mid >= first
              and last >= mid and last < 1e12)
        if t_end_hint is not None and last > t_end_hint * 1.05 + 1e-9:
            ok = False
        if ok:
            r.o = t0
            return t0, cnt
        o += 4
    if window is not None:
        return None
    raise TraceError(
        f"在偏移 {start} 之后找不到数据区锚点（i32 0×5 + nPoints）；"
        f".trace 版本可能与本解析器（4.7.5）不同"
    )


def read_trace(path: str | Path, max_curves: int = 0,
               keep: bool = True) -> list[TraceCurve]:
    """解析 .trace → 曲线列表（每条曲线自带时间向量）。

    Args:
        path: .trace 文件路径
        max_curves: >0 时只解析前 N 条曲线（大文件快速预览）
        keep: False 时不保留数组，只算统计量（>100 MB 的 .trace 用，省内存）
    """
    p = Path(path)
    buf = p.read_bytes()
    if len(buf) < 32:
        raise TraceError(f"文件太小（{len(buf)} 字节），不是有效 .trace")

    r = _Reader(buf)
    ver = r.i32()
    if ver != 2:
        raise TraceError(f"未知 .trace 版本 {ver}（本解析器按 4.7.5 的 v2 实现）")

    n_trace = r.i32()
    if not (0 < n_trace <= 4096):
        raise TraceError(f"trace 数量异常：{n_trace}")

    layout = []          # [(trace_name, [sig names])]
    for _ in range(n_trace):
        name = r.s()
        n_sig = r.i32()
        if not (0 <= n_sig <= 4096):
            raise TraceError(f"信号数量异常：{n_sig}")
        layout.append((name, [r.s() for _ in range(n_sig)]))

    n_label = r.i32()
    if not (0 <= n_label <= 4096):
        raise TraceError(f"标签数量异常：{n_label}")
    labels = [r.s() for _ in range(n_label)]

    # 时间基只有一份，位于全部 trace 前缀之后
    found = _find_anchor(r, None)
    _, cnt0 = found
    times = list(r.f64(cnt0))

    # 曲线槽位（顺序 = 头部声明顺序）；点数优先用尾部自带的 n，cnt0 仅作兜底
    slots: list[tuple[str, str, int]] = []
    for ti, (tname, sigs) in enumerate(layout):
        label = tname or (labels[ti] if ti < len(labels) else f"trace{ti}")
        for si, sname in enumerate(sigs):
            slots.append((label, sname or f"sig{si}", cnt0))

    # 每条曲线的头部长度有 8 字节抖动，用回溯保证正好吃到文件末尾
    start_off = r.o
    solution: list[tuple[int, int]] = []

    def dfs(idx: int, off: int) -> bool:
        if idx == len(slots):
            return off == len(buf)
        for cand, c in _trailer_candidates(buf, off, slots[idx][2]):
            if cand + 8 * c > len(buf):
                continue
            solution.append((cand, c))
            if dfs(idx + 1, cand + 8 * c):
                return True
            solution.pop()
        return False

    greedy = False
    if not dfs(0, start_off):
        # 头部声明的曲线数与实际数据量对不上（多组 hold 轨迹等）→ 按自描述点数贪心读完
        solution, greedy = _greedy_walk(buf, start_off, cnt0)

    if greedy:
        # 头部只声明了 N 条曲线，多出来的数据块无法命名 → 只取前 N 块
        solution = solution[:len(slots)]

    curves: list[TraceCurve] = []
    for (label, sname, _), (off, c) in zip(slots, solution):
        ymax, ymin = _ylim(buf, off)
        if keep:
            vals = list(struct.unpack_from(f">{c}d", buf, off))
            curves.append(TraceCurve(label, sname, t=times, v=vals,
                                     ymax=ymax, ymin=ymin))
        else:
            cur = TraceCurve(label, sname, t=times, v=[], ymax=ymax, ymin=ymin)
            cur._agg = _agg_chunked(buf, off, c)
            curves.append(cur)
        if max_curves and len(curves) >= max_curves:
            return curves
    return curves


def _ylim(buf: bytes, off: int) -> tuple[float, float]:
    """尽力取曲线块头里的 (ymax, ymin)。

    块头长度有 8 字节抖动，所以 40 不一定是准确偏移；依次试几个位置，
    取第一组「有限、量级正常、ymax ≥ ymin」的值。取不到就回 (0, 0)。
    """
    for shift in (40, 32, 48, 24, 56):
        p = off - shift
        if p < 0 or p + 16 > len(buf):
            continue
        a, b = struct.unpack_from(">dd", buf, p)
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        if abs(a) > 1e15 or abs(b) > 1e15:
            continue
        if a >= b:
            return a, b
    return 0.0, 0.0


def _trailer_candidates(buf: bytes, start: int, cnt: int,
                        window: int = 160) -> list[tuple[int, int]]:
    """某条曲线可能的 (数据起点, 点数)。

    尾部（大端）：12 字节 0 + i32 n（n = 点数+1，0 表示未写）+ 可能还有 8 字节 0。
    尾部里的 n 是自描述的，比外部推断可靠；n==0 时回落到 cnt。

    ★ 坑：上一条曲线的 ymin 若是「整值」（如 -3.0 = 0xC008000000000000），
      它尾部的 0 字节会和本块的 12 字节零连成一片，从那里读到的 i32 是 0 →
      产生一个偏 8 字节的假候选。所以**自描述点数（n≥2）的候选必须优先尝试**，
      n==0 的模糊候选一律排到全部强候选之后。
    """
    n_bytes = len(buf)
    stop = min(n_bytes - 24, start + window)
    strong: list[tuple[int, int]] = []
    weak: list[tuple[int, int]] = []
    o = start
    while o <= stop:
        if buf[o:o + 12] == b"\x00" * 12:
            k = struct.unpack_from(">i", buf, o + 12)[0]
            if 2 <= k <= 20_000_000:
                strong.append((o + 24, k - 1))
                strong.append((o + 16, k - 1))
            elif k == 0 and cnt > 0:
                weak.append((o + 24, cnt))
        o += 4
    return strong + weak


def _greedy_walk(buf: bytes, start: int, cnt_hint: int
                 ) -> tuple[list[tuple[int, int]], bool]:
    """头部信息与数据量不符时的兜底：按尾部自描述点数一路读到文件末尾。

    返回 ([(数据起点, 点数)], 是否走的是兜底路径)。
    """
    n = len(buf)
    out: list[tuple[int, int]] = []
    off = start
    guard = 0
    while off < n and guard < 10000:
        guard += 1
        picked = None
        for cand, c in _trailer_candidates(buf, off, cnt_hint):
            if c <= 0 or cand + 8 * c > n:
                continue
            probes = (struct.unpack_from(">d", buf, cand)[0],
                      struct.unpack_from(">d", buf, cand + 8 * (c // 2))[0],
                      struct.unpack_from(">d", buf, cand + 8 * (c - 1))[0])
            if not all(abs(v) < 1e12 for v in probes):
                continue
            # 块头里的 ymax/ymin 也要像样（cand 前 40B 处）
            if cand >= 40:
                y1, y2 = struct.unpack_from(">dd", buf, cand - 40)
                if not (abs(y1) < 1e12 and abs(y2) < 1e12):
                    continue
            picked = (cand, c)
            break
        if picked is None:
            break
        out.append(picked)
        cnt_hint = picked[1]
        off = picked[0] + 8 * picked[1]
    return out, True


def _agg_chunked(buf: bytes, off: int, n: int, chunk: int = 1 << 16) -> dict:
    """不保留数组，流式计算统计量（大文件用）。"""
    if n <= 0:
        return {"n": 0, "min": 0.0, "max": 0.0, "avg": 0.0, "pp": 0.0, "rms": 0.0}
    mn, mx, s, ss, i = float("inf"), float("-inf"), 0.0, 0.0, 0
    while i < n:
        k = min(chunk, n - i)
        for x in struct.unpack_from(f">{k}d", buf, off + 8 * i):
            if x < mn:
                mn = x
            if x > mx:
                mx = x
            s += x
            ss += x * x
        i += k
    return {"n": n, "min": mn, "max": mx, "avg": s / n,
            "pp": mx - mn, "rms": (ss / n) ** 0.5}


def summarize(curves: list[TraceCurve], head: int = 0) -> str:
    """渲染摘要（每条曲线：点数、时间范围、统计量）。head>0 时附前 N 点样本。"""
    if not curves:
        return "（无曲线）"
    lines = [f"曲线：{len(curves)} 条"]
    for i, c in enumerate(curves):
        s = c.stats()
        t0 = c.t[0] if c.t else 0.0
        t1 = c.t[-1] if c.t else 0.0
        dt = (c.t[1] - c.t[0]) if len(c.t) > 1 else 0.0
        lines.append(
            f"  [{i+1}] {c.trace_label} / {c.signal}\n"
            f"      {s['n']} 点  t={t0:.6g}→{t1:.6g} s  dt≈{dt:.4g} s\n"
            f"      min={s['min']:.6g}  max={s['max']:.6g}  avg={s['avg']:.6g}  "
            f"pp={s['pp']:.6g}  rms={s['rms']:.6g}"
        )
        if head:
            k = min(head, len(c.v))
            lines.append("      样本：" + ", ".join(
                f"({c.t[j]:.6g}, {c.v[j]:.6g})" for j in range(k)))
    return "\n".join(lines)


def write_csv(path: str | Path, curves: list[TraceCurve]) -> str:
    """把曲线写成 CSV。时间轴不一致时按各自时间列展开（每条曲线两列 t,v）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    same_t = all(len(c.t) == len(curves[0].t) and c.t == curves[0].t for c in curves)

    with p.open("w", encoding="utf-8", newline="") as f:
        if same_t:
            f.write("t," + ",".join(f"{c.trace_label}/{c.signal}" for c in curves) + "\n")
            for i, tv in enumerate(curves[0].t):
                f.write(f"{tv:.9g}," + ",".join(f"{c.v[i]:.9g}" for c in curves) + "\n")
        else:
            cols = []
            for c in curves:
                cols.append(f"t[{c.signal}]")
                cols.append(f"v[{c.trace_label}/{c.signal}]")
            f.write(",".join(cols) + "\n")
            rows = max(len(c.t) for c in curves)
            for i in range(rows):
                cells = []
                for c in curves:
                    ok = i < len(c.t)
                    cells.append(f"{c.t[i]:.9g}" if ok else "")
                    cells.append(f"{c.v[i]:.9g}" if ok else "")
                f.write(",".join(cells) + "\n")
    return str(p)
