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

启动方式（通常由 AI Agent 自动启动，无需手动运行）：
    python server.py                   # stdio 模式
    python server.py --transport sse   # SSE 模式（远程 Agent）
"""

import os
import sys
import re
import json
import math
import argparse
import logging

import yaml
from mcp.server.fastmcp import FastMCP
from plecs_client import PlecsClient, PlecsError

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

def _fmt_models(models: list) -> str:
    if not models:
        return "    （无）"
    lines = []
    for m in models:
        if isinstance(m, dict):
            mid = str(m.get("id", "?"))
            tags = []
            if m.get("library"):
                tags.append("库")
            if m.get("simulating"):
                tags.append("仿真中")
        else:
            mid, tags = str(m), []
        tag_str = f"  [{','.join(tags)}]" if tags else ""
        lines.append(f"    • {mid}{tag_str}")
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
    """Gr/Gi → (mag_dB, phase_deg) 逐频率。兼容 1D（SISO）与 2D（取第 1 行）。"""
    def flat(x):
        if not x:
            return []
        if isinstance(x[0], (list, tuple)):
            return [float(v) for v in x[0]]   # 多输出时取第 1 行
        return [float(v) for v in x]

    gr, gi = flat(Gr), flat(Gi)
    n = min(len(gr), len(gi), n_f or max(len(gr), len(gi)))
    mag, phase = [], []
    for k in range(n):
        g = complex(gr[k], gi[k])
        mag.append(20.0 * math.log10(abs(g)) if abs(g) > 0 else -999.0)
        phase.append(math.degrees(math.atan2(g.imag, g.real)))
    return mag, phase


def _fmt_analyze(result, analysis_name: str) -> str:
    if not result:
        return f"{analysis_name} 完成，但无数据返回"

    # 归一化：优先官方 {F, Gr, Gi}；兼容旧 {Frequencies, Magnitude, Phase}
    F = result.get("F")
    if F is not None:
        mag, phase = _to_complex_rows(result.get("Gr"), result.get("Gi"), len(F))
        F = [float(v) for v in F]
    else:
        F = result.get("Frequencies", result.get("Frequency")) or []
        F = [float(v) for v in F]
        mag = [float(v) for v in (result.get("Magnitude") or [])]
        phase = [float(v) for v in (result.get("Phase") or [])]

    if F and (mag or result.get("Gr") is not None or result.get("Magnitude") is not None):
        lines = [
            f"✓ {analysis_name} 完成（小信号）",
            f"  频率范围：{F[0]:.4g} Hz → {F[-1]:.4g} Hz（{len(F)} 点）",
        ]
        if mag:
            lines.append(f"  幅值范围：{min(mag):.2f} dB → {max(mag):.2f} dB")
        if phase:
            lines.append(f"  相位范围：{min(phase):.1f}° → {max(phase):.1f}°")

        # 穿越频率 / 相位裕度
        if mag and len(mag) == len(F):
            for i in range(len(mag) - 1):
                if mag[i] >= 0 > mag[i + 1]:
                    r = -mag[i] / (mag[i + 1] - mag[i])
                    fc = F[i] + r * (F[i + 1] - F[i])
                    lines.append(f"\n  穿越频率（0dB）：{fc:.2f} Hz")
                    if phase and len(phase) == len(F):
                        pm = 180 + (phase[i] + r * (phase[i + 1] - phase[i]))
                        verdict = ("✓ 良好(≥45°)" if pm >= 45
                                   else "⚠ 偏低(<45°)" if pm >= 30
                                   else "✗ 不足(<30°，系统接近不稳定)")
                        lines.append(f"  相位裕度：{pm:.1f}°  {verdict}")
                    break
            else:
                lines.append("\n  （扫频范围内未发现 0dB 穿越点）")

        lines.append(f"\n  {'频率(Hz)':>12}  {'幅值(dB)':>10}  {'相位(°)':>10}")
        lines.append("  " + "─" * 38)
        for i in range(min(10, len(F))):
            mg_s = f"{mag[i]:>10.2f}" if i < len(mag) else "       N/A"
            ph_s = f"{phase[i]:>10.1f}" if i < len(phase) else "       N/A"
            lines.append(f"  {F[i]:>12.4g}  {mg_s}  {ph_s}")
        if len(F) > 10:
            lines.append(f"  ...（共 {len(F)} 点）")
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
  5. plecs_scope    — SaveTraces 存 .trace（顶层无 Outport 时的取数正路；
                       定量 CSV 在 GUI 导出）
  6. plecs_close    — 完成后关闭模型（仿真运行中会拒绝）

关键事实（4.7.x 实测）：
  - 仿真时长写 SolverOpts.TimeSpan（秒）；StopTime 已废弃
  - simulate 阻塞且仅当模型顶层有 Outport 才返回 {Time, Values}
  - statistics().models 每项是 {id, library, simulating}
  - 仿真运行中绝不可 close
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
                    busy = [
                        (m.get("id") if isinstance(m, dict) else str(m))
                        for m in stats.get("models", [])
                        if isinstance(m, dict) and m.get("simulating")
                    ]
                    if model_name in busy:
                        return (
                            f"✗ 拒绝关闭 \"{model_name}\"：该模型正在仿真运行中。\n"
                            f"  请等仿真结束（或确认丢弃结果后用 force=true 强关）。"
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
    def plecs_simulate(model_name: str, opts: str = "{}") -> str:
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

        Returns:
            顶层有 Outport → 各通道 min/max/avg/pp/rms 统计；
            顶层无 Outport → 不返回数值（官方行为），提示改用 plecs_scope 导出波形。
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
                    if isinstance(r, str):
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
                        上不可用（报 Invalid scope command）；定量数据请在 PLECS
                        GUI 中导出 CSV，或 SaveTraces 后用离线工具解析 .trace。

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
