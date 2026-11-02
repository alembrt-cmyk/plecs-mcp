"""
server.py  —  PLECS 4.7.x MCP Server

MCP tool 与 PLECS XML-RPC 接口一一对应（官方手册《Simulation Scripts》RPC 章节）：

    plecs_connect   → plecs.statistics（健康检查）
    plecs_status    → plecs.statistics
    plecs_load      → plecs.load
    plecs_close     → plecs.close（带仿真运行防呆）
    plecs_get       → plecs.get（param 省略 = 全部参数）
    plecs_set       → plecs.set
    plecs_simulate  → plecs.simulate（阻塞；支持批量并行）
    plecs_analyze   → plecs.analyze（阻塞；F/Gr/Gi 结果）
    plecs_scope     → plecs.scope（ClearTraces/HoldTrace/RemoveTrace/
                                   SaveTraces/LoadTraces）
                                   ※ 4.7.5 RPC 实测无 ExportCSV/ExportBitmap/
                                     GetCursorData，定量数据走 GUI 导出 CSV
    plecs_get_init  → plecs.get(model, "InitializationCommands")
    plecs_set_init_var → get init + 正则改标量 + set init
    plecs_list_methods → system.listMethods / system.methodHelp
    plecs_webserver → plecs.webserver
    plecs_codegen   → plecs.codegen

以下 5 个是**离线**工具（解析磁盘文件，不需要 PLECS 在运行）——
RPC 没有「列组件 / 列端口 / 改磁盘模型」接口，这些只能离线做：

    plecs_trace_read   → 解析 .trace（Scope SaveTraces 的产物）→ 统计量 / CSV
    plecs_file_info    → .plecs 概览（求解器、时长、组件统计、树、InitCommands）
    plecs_file_blocks  → 列组件（按名称/类型筛）+ 端口表
    plecs_file_get     → 读某块参数（C-Script 的 Declarations/Ts/Parameters…）
    plecs_file_set     → 改 .plecs 参数并写盘（自动备份 + 回读校验）

启动方式（通常由 AI Agent 自动启动，无需手动运行）：
    python server.py                   # stdio 模式
    python server.py --transport sse   # SSE 模式（远程 Agent）
"""

import os
import sys
import re
import json
import math
import time
import argparse
import logging

import yaml
from mcp.server.fastmcp import FastMCP
from plecs_client import PlecsClient, PlecsError
import trace_reader as _tr
import plecs_file as _pf

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("plecs-mcp")

# ── 配置加载 ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = {"host": "localhost", "port": 1080, "timeout": 15.0, "long_timeout": 600.0}
    cfg_path = os.path.join(os.path.dirname(__file__), "config.yml")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update(file_cfg.get("plecs", {}))
    cfg["host"] = os.getenv("PLECS_HOST", cfg["host"])
    cfg["port"] = int(os.getenv("PLECS_PORT", cfg["port"]))
    cfg["timeout"] = float(os.getenv("PLECS_TIMEOUT", cfg["timeout"]))
    cfg["long_timeout"] = float(os.getenv("PLECS_LONG_TIMEOUT", cfg["long_timeout"]))
    return cfg


# ── 结果格式化 ────────────────────────────────────────────────────────────

def _save_mat(data: bytes, target: str, model_name: str) -> str:
    """OutputFormat=MatFile 的返回值落盘。

    实测 4.7.5：PLECS **不写文件**，把 MAT 二进制当 RPC 返回值直接吐回来
    （首字节起 "Binary MATLAB 5.0 MAT-file, written by PLECS ..."）。
    """
    path = target or os.path.join(os.getcwd(), f"{model_name}_result.mat")
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        return (f"✗ 仿真已有结果（{len(data):,} 字节 MAT），但写盘失败：{e}\n"
                f"  可换一个 save_mat 路径重跑，或用 OutputFormat=\"Plain\"")
    return (
        f"✓ 仿真完成（MatFile 输出）\n"
        f"  已写盘：{path}\n"
        f"  大小：{len(data):,} 字节（全精度时序，可用 scipy.io.loadmat 读取）\n"
        f"  说明：PLECS 本身不落盘，MAT 数据是作为 RPC 返回值传回的，本工具代写。"
    )


def _fmt_models(models: list) -> str:
    """渲染 statistics().models。

    4.7.5 实测条目 = {id, filename, library, state, age}，无 simulating 键；
    仿真中 state=="simulation running"（client 已派生 simulating）。
    """
    if not models:
        return "    （无）"
    lines = []
    for m in models:
        if isinstance(m, dict):
            mid = str(m.get("id", "?"))
            tags = []
            if m.get("library"):
                tags.append("库")
            state = str(m.get("state", "") or "")
            if m.get("simulating") or "running" in state.lower():
                tags.append("仿真中")
            elif state and state.lower() != "idle":
                tags.append(state)
            fn = str(m.get("filename", "") or "")
            suffix = f"  — {fn}" if (fn and not m.get("library")) else ""
        else:
            mid, tags, suffix = str(m), [], ""
        tag_str = f"  [{','.join(tags)}]" if tags else ""
        lines.append(f"    • {mid}{tag_str}{suffix}")
    return "\n".join(lines)


def _orient_values(values: list, n_t: int) -> list[list[float]]:
    """把 Values 归一为 [通道][时刻]。兼容 [信号][时间] 与 [时间][信号] 两种朝向。"""
    if not values:
        return []
    if isinstance(values[0], (int, float)):
        return [list(values)]
    if n_t > 0 and len(values) != n_t and len(values[0]) == n_t:
        return [list(ch) for ch in values]           # [信号][时间]
    if n_t > 0 and len(values) == n_t and len(values[0]) != n_t:
        return [list(row) for row in zip(*values)]   # [时间][信号] → 转置
    return [list(ch) for ch in values]               # 默认按 [信号][时间]


def _fmt_sim(result, model_vars=None) -> str:
    if not result:
        return (
            "✓ 仿真已结束，但没有返回数值（无数据）。\n"
            "  这是正常的：plecs.simulate 仅当模型顶层有 Output 端口（Outport）时才返回 {Time, Values}。\n"
            "  取波形走 Scope 轨迹：\n"
            "    plecs_scope(scope_path, \"SaveTraces\", '[\"绝对路径.trace\"]')\n"
            "  （4.7.5 RPC 无 ExportCSV；定量数据在 PLECS GUI 导出 CSV，\n"
            "   或 SaveTraces 后用离线工具解析 .trace。）"
        )

    t = result.get("Time", [])
    if not t:
        return "仿真完成，Time 向量为空（仿真可能被中断或未产生输出点）"

    lines = [
        "✓ 仿真完成",
        f"  时间：{t[0]:.6g} s → {t[-1]:.6g} s（{len(t)} 个点）",
    ]
    if model_vars:
        lines.append(f"  变量：{', '.join(f'{k}={v}' for k, v in model_vars.items())}")

    channels = _orient_values(result.get("Values", []), len(t))
    if not channels:
        lines.append("  （无数值输出通道，请确认模型顶层有 Outport）")
        return "\n".join(lines)

    lines.append(f"\n  输出通道（共 {len(channels)} 个）：")
    lines.append(f"  {'#':>3}  {'min':>10}  {'max':>10}  {'avg':>10}  {'pp':>10}  {'rms':>10}")
    lines.append("  " + "─" * 58)

    for i, ch in enumerate(channels):
        if not ch:
            lines.append(f"  {i+1:>3}  （空）")
            continue
        mn, mx = min(ch), max(ch)
        avg = sum(ch) / len(ch)
        rms = (sum(x * x for x in ch) / len(ch)) ** 0.5
        lines.append(
            f"  {i+1:>3}  {mn:>10.4g}  {mx:>10.4g}  {avg:>10.4g}  {mx-mn:>10.4g}  {rms:>10.4g}"
        )
    return "\n".join(lines)


def _to_complex_rows(Gr, Gi, n_f):
    """Gr/Gi → [(mag_dB, phase_deg), ...]，每个输出一行（MIMO 不再丢通道）。

    兼容 1D（SISO → 单行）与 2D（多输出 → 逐行）。
    """
    def rows(x):
        if not x:
            return []
        if isinstance(x[0], (list, tuple)):
            return [[float(v) for v in r] for r in x]
        return [[float(v) for v in x]]

    grs, gis = rows(Gr), rows(Gi)
    if not grs or not gis:
        return []
    out = []
    for k in range(min(len(grs), len(gis))):
        g_r, g_i = grs[k], gis[k]
        n = min(len(g_r), len(g_i))
        if n_f:
            n = min(n, n_f)
        mag, phase = [], []
        for j in range(n):
            g = complex(g_r[j], g_i[j])
            mag.append(20.0 * math.log10(abs(g)) if abs(g) > 0 else -999.0)
            phase.append(math.degrees(math.atan2(g.imag, g.real)))
        out.append((mag, phase))
    return out


def _crossing(mag, phase, F):
    """0dB 穿越频率与相位裕度；无穿越返回 (None, None)。"""
    if not mag or len(mag) != len(F):
        return None, None
    for i in range(len(mag) - 1):
        if mag[i] >= 0 > mag[i + 1]:
            r = -mag[i] / (mag[i + 1] - mag[i])
            fc = F[i] + r * (F[i + 1] - F[i])
            pm = None
            if phase and len(phase) == len(F):
                pm = 180 + (phase[i] + r * (phase[i + 1] - phase[i]))
            return fc, pm
    return None, None


def _fmt_analyze(result, analysis_name: str) -> str:
    if not result:
        return f"{analysis_name} 完成，但无数据返回"

    # 归一化：优先官方 {F, Gr, Gi}；兼容旧 {Frequencies, Magnitude, Phase}
    F = result.get("F")
    if F is not None:
        F = [float(v) for v in F]
        rows = _to_complex_rows(result.get("Gr"), result.get("Gi"), len(F))
    else:
        F = [float(v) for v in (result.get("Frequencies", result.get("Frequency")) or [])]
        mag = [float(v) for v in (result.get("Magnitude") or [])]
        phase = [float(v) for v in (result.get("Phase") or [])]
        rows = [(mag, phase)] if (mag or phase) else []

    if F and rows:
        lines = [
            f"✓ {analysis_name} 完成（小信号）",
            f"  频率范围：{F[0]:.4g} Hz → {F[-1]:.4g} Hz（{len(F)} 点）",
            f"  输出通道：{len(rows)} 个",
        ]

        # 逐通道算穿越频率 / 相位裕度（MIMO 全部列出）
        for k, (mag, phase) in enumerate(rows):
            prefix = f"  [通道 {k+1}] " if len(rows) > 1 else "  "
            if mag:
                lines.append(f"{prefix}幅值范围：{min(mag):.2f} → {max(mag):.2f} dB")
            fc, pm = _crossing(mag, phase, F)
            if fc is not None:
                head = f"{prefix}穿越频率（0dB）：{fc:.2f} Hz"
                if pm is not None:
                    verdict = ("✓ 良好(≥45°)" if pm >= 45
                               else "⚠ 偏低(<45°)" if pm >= 30
                               else "✗ 不足(<30°，系统接近不稳定)")
                    head += f"，相位裕度：{pm:.1f}°  {verdict}"
                lines.append(head)
            else:
                lines.append(f"{prefix}（扫频范围内未发现 0dB 穿越点）")

        mag0, ph0 = rows[0]
        lines.append(f"\n  {'频率(Hz)':>12}  {'幅值(dB)':>10}  {'相位(°)':>10}")
        lines.append("  " + "─" * 38)
        for i in range(min(10, len(F))):
            mg_s = f"{mag0[i]:>10.2f}" if i < len(mag0) else "       N/A"
            ph_s = f"{ph0[i]:>10.1f}" if i < len(ph0) else "       N/A"
            lines.append(f"  {F[i]:>12.4g}  {mg_s}  {ph_s}")
        if len(F) > 10:
            lines.append(f"  ...（共 {len(F)} 点）")
        if len(rows) > 1:
            lines.append(f"  （上表仅通道 1，共 {len(rows)} 个通道）")
        return "\n".join(lines)

    # 稳态等：{Time, Values}
    if result.get("Time"):
        return _fmt_sim(result)

    return f"✓ {analysis_name} 完成\n{json.dumps(result, ensure_ascii=False, indent=2)}"


def _norm_sim_opts(opts_dict: dict) -> tuple[dict, list[str]]:
    """把便捷键归一到官方 optStruct。返回 (optStruct, 警告列表)。"""
    warns = []
    out = {k: v for k, v in opts_dict.items()
           if k in ("ModelVars", "SolverOpts", "AnalysisOpts", "OutputFormat", "Name")}
    so = dict(opts_dict.get("SolverOpts") or {})

    if "TimeSpan" in opts_dict:
        so["TimeSpan"] = opts_dict["TimeSpan"]
        if "StopTime" in so:
            warns.append("同时给了 TimeSpan 与 SolverOpts.StopTime，以 TimeSpan 为准")
            so.pop("StopTime", None)
    if "StopTime" in opts_dict and "TimeSpan" not in opts_dict:
        so["StopTime"] = opts_dict["StopTime"]
        warns.append("StopTime 已被官方标记废弃，建议改用 TimeSpan（时长，秒）")
    for k in ("Solver", "StartTime", "OutputTimes", "OutputTimesOption",
              "MaxStep", "InitStep", "FixedStep", "AbsTol", "RelTol",
              "Refine", "Timeout", "InitialSystemState"):
        if k in opts_dict and k not in so:
            so[k] = opts_dict[k]
            warns.append(f"{k} 顶层写法已归入 SolverOpts.{k}")
    if so:
        out["SolverOpts"] = so
    unknown = [k for k in opts_dict
               if k not in out and k not in so and k != "SolverOpts"
               and k not in ("TimeSpan", "StopTime")
               and k not in ("Solver", "StartTime", "OutputTimes", "OutputTimesOption",
                             "MaxStep", "InitStep", "FixedStep", "AbsTol", "RelTol",
                             "Refine", "Timeout", "InitialSystemState")]
    for k in unknown:
        warns.append(f"未知键 {k} 已忽略（官方字段：ModelVars/SolverOpts/AnalysisOpts/OutputFormat/Name）")
    return out, warns


# ── Server 构建 ───────────────────────────────────────────────────────────

def build_server(cfg: dict) -> FastMCP:
    mcp = FastMCP(
        name="PLECS MCP Server",
        instructions="""
PLECS 4.7.x 仿真控制 MCP Server（与官方 RPC 手册一一对应）。

工具调用顺序：
  1. plecs_connect  — 每次会话开始时必须首先调用
  2. plecs_load     — 打开模型，拿到 model_name
  3. plecs_get / plecs_set / plecs_get_init / plecs_set_init_var — 读写参数（可选）
  4. plecs_simulate / plecs_analyze — 运行仿真或分析（阻塞命令）
  5. plecs_scope    — SaveTraces 存 .trace（顶层无 Outport 时的取数正路）
  6. plecs_trace_read — 解析 .trace 出数值统计 / CSV（离线，不需要连接）
  7. plecs_close    — 完成后关闭模型（仿真运行中会拒绝）

离线工具（不需要 PLECS 在运行，直接读磁盘）：
  - plecs_trace_read / plecs_file_info / plecs_file_blocks /
    plecs_file_get / plecs_file_set
  - 「列组件 / 列端口」只能靠 plecs_file_blocks：RPC 没有组件树接口
  - 「改磁盘 .plecs」只能靠 plecs_file_set：plecs_set 只改内存模型

关键事实（4.7.x 实测）：
  - 仿真时长写 SolverOpts.TimeSpan（秒）；StopTime 已废弃
  - simulate 阻塞且仅当模型顶层有 Outport 才返回 {Time, Values}
  - OutputFormat="MatFile" 不会落盘：PLECS 把 MAT 二进制当返回值传回，
    plecs_simulate(save_mat=...) 代写文件（要全精度时序就靠它）
  - statistics().models 每项是 {id, filename, library, state, age}，
    仿真中 state="simulation running"（无 simulating 键，工具已派生）
  - plecs.load 成功返回 0，真实模型名由工具从 statistics 反查
  - 仿真运行中绝不可 close；RPC 无 abort，超时后仿真可能仍在跑（先 plecs_status）
  - 改 InitializationCommands 是在内存模型上生效（不会改磁盘 .plecs）

前置条件：PLECS Standalone 已启动，
Preferences → Simulation → "Start XML-RPC server" 已勾选（端口 1080）。
        """.strip(),
    )

    # 共享客户端单例
    _state: dict = {"client": None}

    def client() -> PlecsClient:
        c = _state["client"]
        if c is None:
            raise PlecsError("未连接到 PLECS，请先调用 plecs_connect")
        return c

    def _mk_client(host: str, port: int) -> PlecsClient:
        return PlecsClient(
            host=host, port=port,
            timeout=cfg.get("timeout", 15.0),
            long_timeout=cfg.get("long_timeout", 600.0),
        )

    # ── plecs_connect ─────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_connect(host: str = "", port: int = 0) -> str:
        """
        连接到正在运行的 PLECS 4.7.x Standalone 实例。

        每次 Agent 会话开始时必须首先调用此工具。
        底层调用 plecs.statistics() 验证连接并获取版本与模型列表。

        Args:
            host: PLECS 所在主机（留空则读 config.yml / 环境变量 PLECS_HOST）
            port: XML-RPC 端口（填 0 则读 config.yml / 环境变量 PLECS_PORT）

        Returns:
            连接成功：版本号、build 信息、已打开模型（含 [库]/[仿真中] 标记）。
            连接失败：错误原因和排查建议。
        """
        h = host or cfg["host"]
        p = port or cfg["port"]

        c = _mk_client(h, p)
        try:
            stats = c.connect()
            _state["client"] = c
            return (
                f"✓ 已连接到 PLECS\n"
                f"  地址：{h}:{p}\n"
                f"  版本：{stats.get('version', '?')}  build：{stats.get('build', '?')}\n"
                f"  运行中：仿真 {stats.get('numberOfRunningSimulations', '?')} 个 / "
                f"分析 {stats.get('numberOfRunningAnalyses', '?')} 个\n"
                f"  已打开模型：\n{_fmt_models(stats.get('models'))}"
            )
        except PlecsError as e:
            _state["client"] = None
            return (
                f"✗ 连接失败：{e}\n\n"
                f"排查步骤：\n"
                f"  1. 确认 PLECS Standalone 已启动\n"
                f"  2. PLECS → Preferences → Simulation → 勾选 \"Start XML-RPC server\"\n"
                f"  3. 确认端口为 {p}（PLECS 界面右下角会显示）\n"
                f"  4. RPC 仅接受本机连接，客户端必须与 PLECS 同机"
            )

    # ── plecs_status ──────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_status() -> str:
        """
        调用 plecs.statistics() 获取 PLECS 当前状态。

        无需参数。返回版本、build、已打开模型列表。
        模型条目标记：[库]=library 模型，[仿真中]=正在跑仿真（此时绝不可 close）。

        Returns:
            版本信息和模型列表；连接断开时提示重连。
        """
        try:
            stats = client().statistics()
            return (
                f"PLECS 状态\n"
                f"  版本：{stats.get('version', '?')}  build：{stats.get('build', '?')}\n"
                f"  运行中：仿真 {stats.get('numberOfRunningSimulations', '?')} 个 / "
                f"分析 {stats.get('numberOfRunningAnalyses', '?')} 个\n"
                f"  已打开模型：\n{_fmt_models(stats.get('models'))}"
            )
        except PlecsError as e:
            return f"✗ {e}"

    # ── plecs_load ────────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_load(path: str) -> str:
        """
        调用 plecs.load(path) 在 PLECS 中打开一个 .plecs 模型文件。

        Args:
            path: .plecs 文件的绝对路径。
                  Windows 示例："C:/models/buck_converter.plecs"

        Returns:
            成功：模型名称（后续所有工具调用中用此名称引用该模型）。
            失败：PLECS 返回的错误信息。
        """
        try:
            name = client().load(path)
            return (
                f"✓ 模型已打开\n"
                f"  名称：{name}\n"
                f"  路径：{path}\n"
                f"  后续请用 \"{name}\" 引用此模型"
            )
        except PlecsError as e:
            return f"✗ 打开模型失败：{e}"

    # ── plecs_close ───────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_close(model_name: str, force: bool = False) -> str:
        """
        调用 plecs.close(modelName) 关闭已打开的模型。

        官方行为：无条件关闭且**不保存**（未保存的修改会丢失）。
        防呆：模型正在仿真时默认拒绝关闭（实测此时 close 会坏事）。

        Args:
            model_name: 要关闭的模型名称（来自 plecs_load 的返回值）
            force: true 时跳过仿真运行检查强制关闭（确认仿真可丢弃才用）

        Returns:
            操作结果确认。
        """
        try:
            if not force:
                try:
                    stats = client().statistics()
                    busy = {}
                    for m in stats.get("models", []):
                        if not isinstance(m, dict):
                            continue
                        # 4.7.5 实测：无 simulating 键，状态在 state（"simulation running"/"idle"）
                        state = str(m.get("state", "") or "")
                        if m.get("simulating") or "running" in state.lower():
                            busy[str(m.get("id"))] = state or "simulation running"
                    if model_name in busy:
                        return (
                            f"✗ 拒绝关闭 \"{model_name}\"：该模型正在仿真运行中（state={busy[model_name]}）。\n"
                            f"  注意 RPC 没有 abort 接口，只能等它跑完或手动在 GUI 里停。\n"
                            f"  确认丢弃结果后才用 force=true 强关。"
                        )
                except PlecsError:
                    pass  # 查不到状态时不阻塞关闭
            client().close(model_name)
            return f"✓ 模型 \"{model_name}\" 已关闭（未保存的修改已丢弃）"
        except PlecsError as e:
            return f"✗ 关闭失败：{e}"

    # ── plecs_get ─────────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_get(path: str, param: str = "") -> str:
        """
        调用 plecs.get(path[, param]) 读取模型或组件的参数值。

        Args:
            path: 组件层级路径（用 "/" 分隔）。
                  "BuckConv"          → 模型级参数
                  "BuckConv/L1"       → 顶层组件参数
                  "BuckConv/Ctrl/PID" → 子系统内组件参数

            param: 参数名称。**留空则返回该组件全部参数列表**。
                   模型级常用：TimeSpan、Solver、AbsTol、RelTol、
                               InitializationCommands、SystemState
                   组件常用：L、C、R、V_init、I_init 等

        Returns:
            参数的当前值（字符串化输出）；param 为空时输出全部参数。
        """
        try:
            value = client().get(path, param)
            if not param:
                return f"{path}  全部参数：\n{json.dumps(value, ensure_ascii=False, indent=2, default=str)}"
            return f"{path}  →  {param} = {value}"
        except PlecsError as e:
            return f"✗ 读取失败：{e}"

    # ── plecs_set ─────────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_set(path: str, param: str, value: str) -> str:
        """
        调用 plecs.set(path, param, value) 修改模型或组件的参数值（内存生效，不落盘）。

        Args:
            path:  组件层级路径，格式同 plecs_get。

            param: 参数名称。示例："L"、"TimeSpan"、"Declarations"

            value: 新参数值（字符串）。PLECS 解析数值和表达式：
                   "47e-6" → 47 μH；"Vin/2" → 表达式。
                   仿真时长示例：path=模型名, param="TimeSpan", value="0.05"

        Returns:
            修改确认，显示修改前的值。
        """
        try:
            try:
                old = client().get(path, param)
                old_str = f"（原值 {old}）"
            except Exception:
                old_str = ""

            client().set_param(path, param, value)
            return f"✓ 已修改\n  {path}  →  {param} = {value}  {old_str}\n  （仅内存生效，未写入 .plecs 文件）"
        except PlecsError as e:
            return f"✗ 修改失败：{e}"

    # ── plecs_simulate ────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_simulate(model_name: str, opts: str = "{}", save_mat: str = "") -> str:
        """
        调用 plecs.simulate(modelName[, optStruct]) 运行时域仿真。**阻塞**直到仿真结束。

        Args:
            model_name: 已打开的模型名称（来自 plecs_load）。

            opts: JSON 字符串。官方 optStruct 字段：
                ModelVars   (dict)  覆盖模型初始化变量。
                                    示例：{"ModelVars": {"Vin": 48, "R_load": 10}}

                TimeSpan    (float) 仿真时长（秒）。便捷键，等价 SolverOpts.TimeSpan。
                SolverOpts  (dict)  求解器覆盖：Solver("auto"/"dopri"/"radau"/"discrete")、
                                    StartTime、TimeSpan、OutputTimes、OutputTimesOption、
                                    MaxStep、InitStep、FixedStep、AbsTol、RelTol、
                                    Refine、Timeout（墙钟秒，0=不限）、InitialSystemState
                OutputFormat(str)   "Plain"（默认）或 "MatFile"
                Name        (str)   批量仿真单条命名

                批量并行：opts 传 JSON 数组（list[optStruct]），
                PLECS 自动多核分发，返回逐条结果/错误列表。
                示例：'[{"ModelVars":{"R":5},"Name":"轻载"},{"ModelVars":{"R":10},"Name":"满载"}]'

                完整示例：
                '{"ModelVars": {"Vin": 48}, "TimeSpan": 0.005}'

            save_mat: 仅当 opts 里 OutputFormat="MatFile" 时有效。
                      **PLECS 不会自己写文件**，它把 MAT 二进制当 RPC 返回值吐回来；
                      本工具代写到该路径（留空则写 <当前目录>/<模型名>_result.mat）。

        Returns:
            顶层有 Outport → 各通道 min/max/avg/pp/rms 统计；
            OutputFormat=MatFile → 落盘 .mat 并回报路径与字节数（全精度时序）；
            顶层无 Outport 且非 MatFile → 不返回数值（官方行为），提示走 plecs_scope。
        """
        try:
            raw = json.loads(opts)
        except json.JSONDecodeError as e:
            return f"✗ opts 格式错误（需合法 JSON）：{e}"

        try:
            if isinstance(raw, list):
                normed, warns = [], []
                for item in raw:
                    o, w = _norm_sim_opts(item)
                    normed.append(o)
                    warns.extend(w)
                result = client().simulate(model_name, normed)
                parts = [f"✓ 批量仿真完成（{len(normed)} 条）"]
                if warns:
                    parts.append("  ⚠ " + "；".join(dict.fromkeys(warns)))
                for i, r in enumerate(result):
                    name = raw[i].get("Name", f"#{i+1}") if isinstance(raw[i], dict) else f"#{i+1}"
                    if isinstance(r, dict) and r.get("_binary"):
                        parts.append(f"\n── {name}：MatFile 输出 {len(r['_binary']):,} 字节"
                                     f"（批量模式不落盘，请逐条单独跑以指定 save_mat）")
                    elif isinstance(r, str):
                        parts.append(f"\n── {name}：✗ 错误：{r}")
                    elif isinstance(r, dict) and r.get("Time"):
                        parts.append(f"\n── {name}：" + _fmt_sim(r).split("\n", 1)[-1])
                    else:
                        parts.append(f"\n── {name}：完成（无 Outport 数据，取数走 plecs_scope）")
                return "\n".join(parts)

            opts_dict, warns = _norm_sim_opts(raw or {})
            result = client().simulate(model_name, opts_dict or None)
        except PlecsError as e:
            return f"✗ 仿真失败：{e}"

        if isinstance(result, dict) and result.get("_binary"):
            out = _save_mat(result["_binary"], save_mat, model_name)
        else:
            out = _fmt_sim(result, (raw or {}).get("ModelVars"))
        if warns:
            out = "  ⚠ " + "；".join(dict.fromkeys(warns)) + "\n" + out
        return out

    # ── plecs_analyze ─────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_analyze(model_name: str, analysis_type: str, opts: str = "{}") -> str:
        """
        调用 plecs.analyze(modelName, analysisName[, optStruct]) 运行频域/稳态分析。**阻塞**。

        Args:
            model_name:    已打开的模型名称。

            analysis_type: 分析名称，必须与模型 Analysis Tools 里定义的**完全一致**。
                           常见："SteadyState"、"ACSweep"、"FreqResp"、
                                 "ImpulseResp"、"Multitone"

            opts: JSON 字符串。官方 optStruct：ModelVars / SolverOpts / AnalysisOpts。
                  AnalysisOpts 小信号常用键（注意是官方字段名）：
                  {
                    "FrequencyRange": [10, 1e5],   频率范围（Hz）
                    "FrequencyScale": "log",       "lin" 或 "log"
                    "NumPoints":      50,          自动分布频率点数
                    "AmplitudeRange": [0.01, 0.01],AC sweep 扰动幅值范围
                    "Perturbation":   "Ctrl/Pert", 扰动块路径（不含模型名）
                    "Response":       "Ctrl/Resp", 响应块路径（不含模型名）
                  }
                  稳态常用：{"TimeSpan": 0.02, "Tolerance": 1e-4, "MaxIter": 50,
                            "InitCycles": 2, "ShowCycles": 1}

        Returns:
            小信号分析（官方返回 F/Gr/Gi）→ 幅相表 + 自动计算穿越频率与相位裕度。
            稳态 → Outport 统计。
        """
        try:
            raw = json.loads(opts)
        except json.JSONDecodeError as e:
            return f"✗ opts 格式错误：{e}"

        opts_dict = raw or {}
        # 旧文档键名兼容映射（仅映射语义明确的键；SysName/SettlingTime 官方无此字段）
        compat = []
        ao = dict(opts_dict.get("AnalysisOpts") or {})
        alias = {"FreqRange": "FrequencyRange", "LogScale": "FrequencyScale",
                 "Amplitude": "AmplitudeRange"}
        for old, new in alias.items():
            if old in opts_dict and new not in ao:
                v = opts_dict[old]
                if old == "LogScale":
                    v = "log" if v else "lin"
                elif old == "Amplitude":
                    v = [v, v]
                ao[new] = v
                compat.append(f"{old}→AnalysisOpts.{new}")
        if "SysName" in opts_dict:
            compat.append("SysName 已废弃：小信号源/响应块请用 AnalysisOpts.Perturbation / AnalysisOpts.Response")
        if "SettlingTime" in opts_dict:
            compat.append("SettlingTime 已废弃：稳定等待用 AnalysisOpts.InitCycles")
        if ao:
            opts_dict = dict(opts_dict)
            opts_dict["AnalysisOpts"] = ao

        try:
            result = client().analyze(model_name, analysis_type, opts_dict or None)
        except PlecsError as e:
            return f"✗ 分析失败：{e}"

        if isinstance(result, dict) and result.get("_binary"):
            out = (f"✓ {analysis_type} 完成，但返回的是 MAT 二进制（{len(result['_binary']):,} 字节），"
                   f"本工具不落盘。\n"
                   f"  改法：去掉 opts 里的 OutputFormat=MatFile，用默认 Plain 取 F/Gr/Gi。")
        else:
            out = _fmt_analyze(result, analysis_type)
        if compat:
            out = "  ⚠ 旧键名映射：" + "；".join(compat) + "\n" + out
        return out

    # ── plecs_scope ───────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_scope(scope_path: str, command: str, args: str = "[]") -> str:
        """
        调用 plecs.scope(scopePath, command, ...) 操作 Scope 轨迹。

        Args:
            scope_path: Scope 完整层级路径（含模型名）。
                        示例："BuckConv/Scope"、"BuckConv/Control/VScope"

            command:    ★ 4.7.5 RPC 实测可用（其余命令 RPC 端会拒绝）：
                        "ClearTraces"    清除所有轨迹（args "[]"）
                        "HoldTrace"      保留上次仿真波形为轨迹（args '[]' 或 '["标签"]'）
                                         返回轨迹名
                        "RemoveTrace"    删除轨迹（args '["轨迹名"]'）
                        "SaveTraces"     保存轨迹到 .trace 文件（args '["绝对路径.trace"]'）
                        "LoadTraces"     从 .trace 载入轨迹（args '["绝对路径.trace"]'）

                        注：ExportCSV / ExportBitmap / GetCursorData 在 4.7.5 RPC
                        上不可用（报 Invalid scope command）；定量数据用
                        SaveTraces 存 .trace 后交给 plecs_trace_read 解析
                        （可导 CSV），或 SaveTraces → LoadTraces 回看波形。

            args: JSON 数组字符串，按顺序传给 command 的额外参数。默认 "[]"。

        Returns:
            操作结果。HoldTrace 返回轨迹名。
        """
        try:
            extra = json.loads(args)
        except json.JSONDecodeError as e:
            return f"✗ args 格式错误（需合法 JSON 数组）：{e}"
        if not isinstance(extra, list):
            return "✗ args 必须是 JSON 数组"

        try:
            result = client().scope(scope_path, command, *extra)
        except PlecsError as e:
            return f"✗ scope {command} 失败：{e}"

        if result is None:
            return f"✓ scope {command} 完成：{scope_path}"
        return f"✓ scope {command} 完成：{scope_path}\n结果：{json.dumps(result, ensure_ascii=False, default=str)}"

    # ── plecs_get_init ────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_get_init(model_name: str) -> str:
        """
        读取模型的 InitializationCommands（初始化命令脚本，Octave 语法）。

        这是模型变量（fsw、PQ_T_DELAY 等）的定义处。改内存模型用
        plecs_set_init_var；改磁盘文件需另行编辑 .plecs（base64 段）。

        Args:
            model_name: 已打开的模型名称

        Returns:
            初始化脚本全文（长文可能截断显示）。
        """
        try:
            text = client().get(model_name, "InitializationCommands")
        except PlecsError as e:
            return f"✗ 读取失败：{e}"
        text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
        return f"=== {model_name} InitializationCommands（{len(text)} 字符）===\n{text}"

    # ── plecs_set_init_var ────────────────────────────────────────────────

    @mcp.tool()
    def plecs_set_init_var(model_name: str, var_name: str, value: str,
                           comment: str = "") -> str:
        """
        修改 InitializationCommands 中的一个标量变量赋值（`var = ...;`），
        并把整个 init 写回模型。**仅内存生效**，不改磁盘 .plecs 文件。

        Args:
            model_name: 已打开的模型名称
            var_name:   变量名，如 "fsw"
            value:      新值（文本，如 "50000" 或 "2*pi*fo"）
            comment:    可选，替换该行行尾注释（不带 %；留空则保留原注释）

        Returns:
            修改前后对照。变量不存在时报错。
        """
        try:
            text = client().get(model_name, "InitializationCommands")
        except PlecsError as e:
            return f"✗ 读取 InitializationCommands 失败：{e}"
        if not isinstance(text, str):
            return f"✗ InitializationCommands 返回类型异常：{type(text).__name__}"

        pat = re.compile(
            r'^([^\S\n]*' + re.escape(var_name) + r'\s*=\s*)([^;\n]+)(;)([^\n]*)$',
            re.M,
        )
        m = pat.search(text)
        if not m:
            return f"✗ init 里找不到 `{var_name} = ...;`（用 plecs_get_init 查看全文确认变量名）"

        old_line = m.group(0)
        new_comment = m.group(4)
        if comment:
            c = comment if comment.strip().startswith("%") else "  %" + comment
            new_comment = c
        new_line = f"{m.group(1)}{value}{m.group(3)}{new_comment}"
        if old_line == new_line:
            return f"✓ 已是目标值：{old_line.strip()}"

        new_text = text[:m.start()] + new_line + text[m.end():]
        try:
            client().set_param(model_name, "InitializationCommands", new_text)
        except PlecsError as e:
            return f"✗ 写回失败：{e}"

        return (
            f"✓ 已修改 init（内存生效）\n"
            f"  [改] {old_line.strip()}\n"
            f"   →  {new_line.strip()}"
        )

    # ── plecs_list_methods ────────────────────────────────────────────────

    @mcp.tool()
    def plecs_list_methods(help_for: str = "") -> str:
        """
        查看 PLECS RPC 接口自描述信息。

        Args:
            help_for: 留空列出全部方法名；填方法名（如 "plecs.simulate"）
                      则返回该方法的官方帮助文本。

        Returns:
            方法名列表或指定方法的帮助文本。
        """
        try:
            if help_for:
                text = client().method_help(help_for)
                return f"=== {help_for} ===\n{text or '（无帮助文本）'}"
            methods = client().list_methods()
            return "PLECS RPC 方法：\n" + "\n".join(f"  • {m}" for m in methods)
        except PlecsError as e:
            return f"✗ {e}"

    # ── plecs_webserver ───────────────────────────────────────────────────

    @mcp.tool()
    def plecs_webserver(command: str, args: str = "[]") -> str:
        """
        调用 plecs.webserver(command, ...) 控制 PLECS Web 仿真服务器。

        Args:
            command: "start" / "stop" / "status"
            args:    JSON 数组，命令相关参数（通常为空 "[]"）

        Returns:
            服务器操作结果或当前状态信息。
        """
        try:
            extra = json.loads(args)
        except json.JSONDecodeError as e:
            return f"✗ args 格式错误：{e}"

        try:
            result = client().webserver(command, *extra)
        except PlecsError as e:
            return f"✗ webserver {command} 失败：{e}"

        return f"✓ webserver {command}\n结果：{result}"

    # ── plecs_codegen ─────────────────────────────────────────────────────

    @mcp.tool()
    def plecs_codegen(model_name: str, opts: str = "{}") -> str:
        """
        调用 plecs.codegen(modelName, opts) 触发 PLECS 代码生成（阻塞）。

        模型中须已配置好代码生成目标（Coder Options）。

        Args:
            model_name: 已打开的模型名称。

            opts: JSON 字符串，代码生成选项。常用键：
                  {
                    "GenerateCode": true,      是否生成代码（默认 true）
                    "BuildCode":    true,       是否编译代码
                    "RunCode":      false       是否运行生成的代码
                  }

        Returns:
            代码生成结果，包含输出目录和生成的文件列表（如 PLECS 返回此信息）。
        """
        try:
            opts_dict = json.loads(opts)
        except json.JSONDecodeError as e:
            return f"✗ opts 格式错误：{e}"

        try:
            result = client().codegen(model_name, opts_dict or None)
        except PlecsError as e:
            return f"✗ codegen 失败：{e}"

        if not result:
            return f"✓ codegen 完成：{model_name}"
        return f"✓ codegen 完成：{model_name}\n{json.dumps(result, ensure_ascii=False, indent=2)}"

    # ── plecs_trace_read（离线，不需要 PLECS 在运行）──────────────────────

    @mcp.tool()
    def plecs_trace_read(trace_path: str, save_csv: str = "", head: int = 0,
                         max_curves: int = 0, keep: bool = True) -> str:
        """
        解析 `.trace` 波形文件，给出逐曲线统计，可选导出 CSV。
        **纯离线**（读文件，不连 PLECS），是「顶层无 Outport 模型」的定量取数正路。

        典型链路：plecs_simulate(Plain/MatFile) 跑完后
          → plecs_scope(scope_path, "SaveTraces", '["D:/x.trace"]')
          → plecs_trace_read("D:/x.trace", save_csv="D:/x.csv")

        Args:
            trace_path: .trace 文件绝对路径
            save_csv:   留空不写；给 .csv 路径则写该文件；给目录则写
                        `<目录>/<trace 名>.csv`。同时间基写宽表（首列 t），
                        否则每条曲线两列（t,v）。
            head:       >0 时在文本里附前 N 点样本（默认 0，只给统计量）
            max_curves: >0 时只解析前 N 条曲线
            keep:       False 不保留数组，只流式算统计量（>100 MB 文件必用；
                        此时不能导 CSV）

        Returns:
            逐曲线：时间范围、dt、min/max/avg/pp/rms；CSV 落盘路径。
        """
        p = os.path.abspath(os.path.expanduser(trace_path))
        if not os.path.exists(p):
            return (f"✗ 文件不存在：{p}\n"
                    f"  先用 plecs_scope(scope_path, \"SaveTraces\", '[\"{p}\"]') 生成")
        size = os.path.getsize(p)
        note = ""
        if keep and size > 80 * 1024 * 1024:
            keep = False
            note = (f"\n  ⚠ 文件 {size / 1048576:.0f} MB，已自动切到统计模式（不保留数组）；"
                    f"要全量数组请显式传 keep=True 并注意内存\n")

        t0 = time.time()
        try:
            curves = _tr.read_trace(p, max_curves=max_curves, keep=keep)
        except _tr.TraceError as e:
            return (f"✗ .trace 解析失败：{e}\n"
                    f"  文件：{p}（{size} 字节）\n"
                    f"  提示：本解析器按 PLECS 4.7.5 的 v2 格式实现；"
                    f"若该文件来自别的版本，用 GUI 导出 CSV 更稳")
        except Exception as e:                                  # noqa: BLE001
            return f"✗ 解析异常：{type(e).__name__}: {e}"
        dt = time.time() - t0

        body = _tr.summarize(curves, head=head)
        out = [f"✓ {os.path.basename(p)}（{size / 1024:.1f} KB，解析 {dt:.2f}s）", body]
        if note:
            out.append(note.rstrip())

        if save_csv:
            if not keep:
                out.append("  ⚠ 统计模式未保留数组，已跳过 CSV 导出（需要 CSV 请 keep=True）")
            else:
                target = save_csv
                if os.path.isdir(target):
                    target = os.path.join(target, os.path.splitext(os.path.basename(p))[0] + ".csv")
                try:
                    written = _tr.write_csv(target, curves)
                    nrow = len(curves[0].t) if curves and curves[0].t else 0
                    out.append(f"  ✓ CSV 已写盘：{written}（{nrow} 行 × {len(curves) + 1} 列）")
                except OSError as e:
                    out.append(f"  ✗ CSV 写盘失败：{e}")
        return "\n".join(out)

    # ── plecs_file_info / blocks / get / set（离线改 .plecs）───────────────

    def _open_plecs(path: str) -> _pf.PlecsFile:
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(p):
            raise _pf.PlecsFileError(f"文件不存在：{p}")
        return _pf.PlecsFile(p)

    @mcp.tool()
    def plecs_file_info(path: str, tree_depth: int = 1) -> str:
        """
        离线读取 `.plecs` 模型文件的概览。**纯离线**，不需要 PLECS 在运行。

        用于：模型里到底有哪些组件、求解器/时长设置、InitializationCommands 内容、
        顶层子系统结构。RPC 的 plecs.get 只回模型级参数、不给组件树，所以要列组件
        只能走本工具。

        Args:
            path: .plecs 文件路径
            tree_depth: 组件树展开层级（0=不展开，1=顶层，2=含子系统内一层）

        Returns:
            模型名/版本/求解器/时长、组件类型统计、组件树、顶层端口。
        """
        try:
            f = _open_plecs(path)
        except _pf.PlecsFileError as e:
            return f"✗ {e}"

        m = f.model
        init = m.attrs.get("InitializationCommands", "")
        out = [
            f"✓ {os.path.basename(f.path)}（{len(f.lines)} 行，"
            f"{f.path.stat().st_size / 1024:.0f} KB，换行 {f.newline!r}）",
            f"  模型名：{m.attrs.get('Name', '?')}   版本：{m.attrs.get('Version', '?')}   "
            f"CircuitModel：{m.attrs.get('CircuitModel', '?')}",
            f"  时长：{m.attrs.get('TimeSpan', '?')} s   求解器：{m.attrs.get('Solver', '?')}   "
            f"MaxStep：{m.attrs.get('MaxStep', '?')}",
            f"  组件总数：{len(f.components())}",
        ]
        tc = f.type_counts()
        if tc:
            items = [f"{k}×{v}" for k, v in tc.items()]
            line, buf = "  类型：", ""
            for it in items:
                if len(buf) + len(it) > 76:
                    out.append(line + buf.rstrip("、"))
                    line, buf = "        ", ""
                buf += it + "、"
            if buf:
                out.append(line + buf.rstrip("、"))

        if init:
            first = [l for l in init.splitlines() if l.strip()][:3]
            out.append(f"  InitCommands：{len(init.splitlines())} 行，前 3 行")
            out += [f"      {l}" for l in first]
            out.append(f"    （全文用 plecs_file_get(path=\"{path}\", block=\"@\", "
                       f"var=\"InitializationCommands\"）")

        if tree_depth > 0:
            out.append(f"\n组件树（展开 {tree_depth} 层）：")
            tree = _pf.render_tree(f, max_depth=tree_depth).splitlines()
            out += ["  " + l for l in tree[:60]]
            if len(tree) > 60:
                out.append(f"  …（共 {len(tree)} 行，用 plecs_file_blocks 精确筛）")
        return "\n".join(out)

    @mcp.tool()
    def plecs_file_blocks(path: str, query: str = "", kind: str = "",
                          ports: bool = False, limit: int = 80) -> str:
        """
        离线列出 `.plecs` 里的组件（可按名称/类型筛），`ports=True` 时附端口表。

        Args:
            path:  .plecs 文件路径
            query: 子串筛选，同时匹配名称与类型（不区分大小写）；留空 = 全部
            kind:  类型精确筛选，如 "CScript"、"Subsystem"、"Resistor"
            ports: True 时对每个命中组件展示端口（Subsystem 的 Terminal 或
                   CScript 的 NumInputs/NumOutputs）
            limit: 最多显示多少个（默认 80）

        Returns:
            组件清单：序号（可直接当选择器 `#N` 用）、名称、类型、位置。
        """
        try:
            f = _open_plecs(path)
        except _pf.PlecsFileError as e:
            return f"✗ {e}"

        comps = f.components()
        q = query.strip().lower()
        hit = []
        for i, c in enumerate(comps, 1):
            nm, ty = c.attrs.get("Name", ""), c.attrs.get("Type", "")
            if kind and ty.lower() != kind.strip().lower():
                continue
            if q and q not in nm.lower() and q not in ty.lower():
                continue
            hit.append((i, c))

        if not hit:
            return (f"✗ 没有匹配的组件（query={query!r}, kind={kind!r}）\n"
                    f"  模型共 {len(comps)} 个组件，类型："
                    + "、".join(f"{k}×{v}" for k, v in f.type_counts().items()))

        out = [f"✓ {os.path.basename(f.path)}：命中 {len(hit)} / {len(comps)} 个组件"
               + (f"（只显示前 {limit}）" if len(hit) > limit else "")]
        for i, c in hit[:limit]:
            out.append(f"  #{i:<4} {c.attrs.get('Name', '?'):<22} "
                       f"{c.attrs.get('Type', '?'):<20} {c.attrs.get('Position', '')}"
                       + (f"  ⟵ {c.path}" if "/" in c.path else ""))
            if ports:
                for line in _pf.render_ports(c).splitlines():
                    out.append("        " + line.strip())
        if len(hit) > limit:
            out.append(f"  …共 {len(hit)} 个，收窄 query/kind 或调大 limit")
        return "\n".join(out)

    @mcp.tool()
    def plecs_file_get(path: str, block: str = "@", var: str = "",
                       max_len: int = 400) -> str:
        """
        离线读取 `.plecs` 里某个块的参数值（C-Script 的 Declarations/Ts/Parameters、
        模型级 TimeSpan/Solver、电阻电感值、InitializationCommands 等）。

        Args:
            path:    .plecs 文件路径
            block:   块选择器：`@`=模型根；`#12`=第 12 个组件；
                     名字（如 "环路控制"，唯一子串也行）；路径 `Sub1/Sub2`
            var:     参数名（如 "Ts"、"Declarations"、"TimeSpan"）；留空=列全部
            max_len: 单个值最多显示多少字符（长脚本会截断，给 0 表示全显示）

        Returns:
            参数表或单个参数的完整值。
        """
        try:
            f = _open_plecs(path)
            node, ps = f.get_param(block, var)
        except _pf.PlecsFileError as e:
            return f"✗ {e}"

        title = node.attrs.get("Name") or node.key
        ty = node.attrs.get("Type")
        head = f"✓ {os.path.basename(f.path)} :: {title}" + (f"〔{ty}〕" if ty else "")
        if var:
            v = ps[var]
            return f"{head}\n  {var} = {v if max_len <= 0 or len(v) <= max_len else v[:max_len] + f'…（共 {len(v)} 字）'}"
        return head + "\n" + _pf.render_params(node, head=max_len)

    @mcp.tool()
    def plecs_file_set(path: str, block: str, var: str, value: str,
                       dry_run: bool = False) -> str:
        """
        离线修改 `.plecs` 里的参数并写盘（**会改磁盘文件**，动前自动备份）。

        只改目标那几行，其余字节不动（保留 CRLF/LF、缩进、列对齐、base64 分段）。
        改完自动回读校验。

        Args:
            path:    .plecs 文件路径
            block:   块选择器（同 plecs_file_get）：`@` / `#12` / 名字 / 路径
            var:     参数名（如 "Ts"、"NumDiscStates"；模型级用 "TimeSpan"）
            value:   新值文本。原文是 base64 的属性会自动重新 base64 编码，
                     引号字符串会自动转义并重新分段
            dry_run: True 只预览改动、不写盘

        Returns:
            改动前后对照 + 备份路径；失败时给出可用参数名。
        """
        try:
            f = _open_plecs(path)
        except _pf.PlecsFileError as e:
            return f"✗ {e}"

        try:
            old, new = f.set_param(block, var, value)
        except _pf.PlecsFileError as e:
            return f"✗ {e}"

        title = f.resolve(block).attrs.get("Name") or block
        if old == new:
            return f"✓ 已是目标值，未改动：{title} / {var} = {old}"
        if dry_run:
            return (f"（dry-run，未写盘）\n"
                    f"  {title} / {var}\n   [改] {old[:200]}\n   →  {new[:200]}")

        try:
            bak = f.save()
        except OSError as e:
            return f"✗ 写盘失败：{e}"

        # 回读校验
        try:
            back = _pf.PlecsFile(f.path).get_param(block, var)[1][var]
            ok = back == new
        except _pf.PlecsFileError as e:
            return f"✗ 已写入但回读失败：{e}（备份：{bak}）"
        if not ok:
            return (f"✗ 回读不一致，请用备份恢复：{bak}\n"
                    f"   期望：{new[:120]}\n   实际：{back[:120]}")

        return (f"✓ 已改并回读校验通过\n"
                f"  文件：{f.path}\n"
                f"  块：{title} / {var}\n"
                f"  [改] {old[:200]}\n"
                f"   →  {new[:200]}\n"
                f"  备份：{bak}\n"
                f"  ⚠ 这是磁盘文件；PLECS 里若已打开该模型，需关闭重开才会加载新值")

    return mcp


# ── CLI 入口 ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="PLECS 4.7.x MCP Server")
    p.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="SSE 监听地址")
    p.add_argument("--port", type=int, default=8765, help="SSE 监听端口")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config()
    mcp = build_server(cfg)
    logger.info("PLECS MCP Server 启动（%s）", args.transport)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
