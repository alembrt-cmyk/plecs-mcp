"""
plecs_client.py  —  PLECS 4.7.x XML-RPC 客户端

与 PLECS 暴露的 RPC 接口一一对应（官方手册《Simulation Scripts》RPC 章节）：
    system.listMethods   → list_methods()
    system.methodHelp    → method_help()
    plecs.statistics     → statistics()
    plecs.load           → load()
    plecs.close          → close()
    plecs.get            → get()
    plecs.set            → set_param()
    plecs.simulate       → simulate()
    plecs.analyze        → analyze()
    plecs.scope          → scope()
    plecs.webserver      → webserver()
    plecs.codegen        → codegen()

实测要点（4.7.5，2026-11-02 复测）：
  - statistics().models 是 dict 列表：{id, filename, library, state, age}
    ★ **没有 `simulating` 键**：仿真中 state == "simulation running"，空闲为 "idle"。
    本模块统一派生 simulating 布尔量，并保留 filename/state。
  - plecs.load 成功返回 0（int），文件不存在抛 Fault("File not found")
    ⇒ 真实模型名需从 statistics 反查（见 load()）。
  - simulate/analyze 是**阻塞**命令，RPC 同时只允许一个阻塞命令
  - 仿真时长在 SolverOpts.TimeSpan（秒）；StopTime 已废弃
  - simulate 仅当模型顶层有 outport 时才返回 {Time, Values}，否则返回空
  - OutputFormat="MatFile" 时 PLECS **不落盘**，直接把 MAT 二进制当返回值吐回来
  - 同一个 ServerProxy 的 HTTP 连接不可并发（会 CannotSendRequest）；
    但**不同连接可并发** —— 故 statistics() 走独立连接，仿真期间也能查状态
  - 仿真运行中绝不可 close
"""

import http.client
import logging
import os
import socket
import threading
import xmlrpc.client
from typing import Any

logger = logging.getLogger(__name__)


class PlecsError(RuntimeError):
    """PLECS XML-RPC 调用失败"""


class TimeoutTransport(xmlrpc.client.Transport):
    """带 socket 超时的 XML-RPC Transport。"""

    def __init__(self, timeout: float | None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        if self._timeout is not None:
            conn.timeout = self._timeout
        return conn


def _norm_models(models: Any) -> list[dict]:
    """statistics().models 归一化。

    4.7.5 实测条目：{id, filename, library, state, age}，**无 simulating 键**；
    仿真进行中 state == "simulation running"。
    这里统一补齐 simulating（由 state/旧键推导）并保留 filename/state。
    """
    out = []
    for m in models or []:
        if isinstance(m, dict):
            state = str(m.get("state", "") or "")
            sim = bool(m.get("simulating")) or ("running" in state.lower())
            out.append({
                "id": str(m.get("id", "")),
                "filename": str(m.get("filename", "") or ""),
                "library": bool(m.get("library", False)),
                "state": state,
                "simulating": sim,
            })
        else:
            out.append({"id": str(m), "filename": "", "library": False,
                        "state": "", "simulating": False})
    return out


def _norm_path(p: str) -> str:
    """路径比较用归一化：统一分隔符 + 小写（Windows 不区分大小写）。"""
    return os.path.normpath(str(p).replace("\\", "/")).replace("\\", "/").casefold()


def _as_bytes(x: Any) -> bytes | None:
    """XML-RPC 的 <base64> 会解码成 xmlrpc.client.Binary（不是 bytes），统一转 bytes。"""
    if isinstance(x, xmlrpc.client.Binary):
        return x.data
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    return None


def _norm_stats(result: Any) -> dict:
    """statistics 原始结果 → 归一化 dict（version/build 转字符串等）。"""
    stats = dict(result) if result else {}
    stats["models"] = _norm_models(stats.get("models"))
    ver = stats.get("version")
    if isinstance(ver, (list, tuple)):
        stats["version_raw"] = list(ver)
        stats["version"] = ".".join(str(v) for v in ver[:3])
        stats.setdefault("build", str(ver[3]) if len(ver) > 3 else "")
    return stats


class PlecsClient:
    def __init__(self, host: str = "localhost", port: int = 1080,
                 timeout: float = 15.0, long_timeout: float = 600.0):
        """
        Args:
            timeout:      普通 RPC 调用超时（秒）
            long_timeout: 阻塞命令（simulate/analyze/codegen）超时（秒）
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.long_timeout = long_timeout
        self._proxy: xmlrpc.client.ServerProxy | None = None
        self._proxy_long: xmlrpc.client.ServerProxy | None = None
        # statistics 专用连接：与主连接分开，长仿真期间仍可查状态
        self._stat_proxy: xmlrpc.client.ServerProxy | None = None
        # 主连接串行锁：同一 ServerProxy 的 HTTP 连接不能并发（实测 CannotSendRequest）
        self._lock = threading.RLock()

    # ── 连接 ─────────────────────────────────────────────────────────────

    def _make_proxy(self, timeout: float | None):
        return xmlrpc.client.ServerProxy(
            f"http://{self.host}:{self.port}/RPC2",
            allow_none=True,
            transport=TimeoutTransport(timeout),
        )

    def connect(self) -> dict:
        """
        建立连接并返回 plecs.statistics() 归一化结果。
        成功：含 version / build / models 的 dict；失败：抛 PlecsError。
        """
        try:
            self._proxy = self._make_proxy(self.timeout)
            self._proxy_long = self._make_proxy(self.long_timeout)
            self._stat_proxy = self._make_proxy(self.timeout)
            # 直连原始 proxy，避免经 _call 触发递归重连
            stats = _norm_stats(self._proxy.plecs.statistics())
            logger.info("已连接 PLECS %s @ %s:%d",
                        stats.get("version"), self.host, self.port)
            return stats
        except xmlrpc.client.Fault as e:
            self._reset()
            raise PlecsError(f"plecs.statistics() 调用失败 — {e.faultString}") from e
        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            self._reset()
            raise PlecsError(f"无法连接到 {self.host}:{self.port} — {e}") from e

    def _reset(self) -> None:
        self._proxy = self._proxy_long = self._stat_proxy = None

    def is_connected(self) -> bool:
        if self._proxy is None:
            return False
        try:
            self.statistics()
            return True
        except Exception:
            return False

    # ── 调用核心（断连自动重连一次） ─────────────────────────────────────

    def _call(self, method: str, *args, long_op: bool = False) -> Any:
        # 同一 ServerProxy 的 HTTP 连接不能并发，主连接一律串行；
        # 需要仿真期间查状态 → 用 statistics()（走独立连接）
        with self._lock:
            return self._call_locked(method, *args, long_op=long_op)

    def _call_locked(self, method: str, *args, long_op: bool = False) -> Any:
        if self._proxy is None:
            raise PlecsError("未连接到 PLECS，请先调用 plecs_connect")
        if long_op:
            proxy = self._proxy_long if self._proxy_long is not None else self._proxy
        else:
            proxy = self._proxy

        def _invoke(p):
            obj = p
            for part in method.split("."):
                obj = getattr(obj, part)
            return obj(*args)

        try:
            return _invoke(proxy)
        except xmlrpc.client.Fault as e:
            raise PlecsError(f"[{method}] 失败：{e.faultString}") from e
        except TimeoutError as e:
            # 超时不重试（simulate 重试会重复跑仿真）
            raise PlecsError(
                f"[{method}] 超时（{'long_timeout' if long_op else 'timeout'}="
                f"{self.long_timeout if long_op else self.timeout}s）。"
                f"注意：超时只是本端放弃等待，PLECS 里该仿真可能仍在继续；"
                f"先 plecs_status 确认 state，必要时用 SolverOpts.Timeout 或加大 PLECS_LONG_TIMEOUT"
            ) from e
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError,
                http.client.CannotSendRequest, http.client.ResponseNotReady,
                http.client.RemoteDisconnected, socket.gaierror, OSError) as e:
            # 连接级故障：重建代理并重试一次
            logger.warning("连接故障（%s），尝试自动重连…", e)
            try:
                self.connect()
            except PlecsError as e2:
                self._reset()
                raise PlecsError(f"连接已断开且自动重连失败：{e2}") from e2
            if long_op and self._proxy_long is not None:
                proxy = self._proxy_long
            else:
                proxy = self._proxy
            try:
                return _invoke(proxy)
            except xmlrpc.client.Fault as e3:
                raise PlecsError(f"[{method}] 失败：{e3.faultString}") from e3
            except TimeoutError as e3:
                raise PlecsError(f"[{method}] 超时：{e3}") from e3
            except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError,
                    http.client.CannotSendRequest, http.client.ResponseNotReady,
                    http.client.RemoteDisconnected, socket.gaierror, OSError) as e3:
                self._reset()
                raise PlecsError(f"连接已断开：{e3}") from e3

    # ── system.* ─────────────────────────────────────────────────────────

    def list_methods(self) -> list[str]:
        """system.listMethods — PLECS 暴露的全部 RPC 方法名。"""
        return list(self._call("system.listMethods"))

    def method_help(self, method_name: str) -> str:
        """system.methodHelp — 指定方法的官方帮助文本。"""
        return str(self._call("system.methodHelp", method_name))

    # ── plecs.statistics ─────────────────────────────────────────────────

    def statistics(self) -> dict:
        """
        plecs.statistics() — 实例全局信息。

        实测 4.7.5 返回字段：
            version   : list  [major, minor, patch, "hash build-date"]（非字符串！）
            models    : list[dict]  每项 {id, filename, library, state, age}
                        ★ 无 simulating 键；仿真中 state == "simulation running"
            numberOfOpenModels / numberOfRunningSimulations /
            numberOfRunningAnalyses / uptime

        返回归一化 dict：version/build 转为友好字符串（原值存 version_raw），
        models 统一为 {id, filename, library, state, simulating}。

        ★ 走**独立连接**（_stat_proxy），因此长仿真阻塞主连接时仍可查状态。
        """
        if self._proxy is None:
            raise PlecsError("未连接到 PLECS，请先调用 plecs_connect")
        p = self._stat_proxy if self._stat_proxy is not None else self._proxy
        try:
            return _norm_stats(p.plecs.statistics())
        except xmlrpc.client.Fault as e:
            raise PlecsError(f"[plecs.statistics] 失败：{e.faultString}") from e
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError,
                http.client.CannotSendRequest, http.client.ResponseNotReady,
                http.client.RemoteDisconnected, socket.gaierror, OSError, TimeoutError) as e:
            # 状态连接失效 → 重建一次再试
            try:
                self._stat_proxy = self._make_proxy(self.timeout)
                return _norm_stats(self._stat_proxy.plecs.statistics())
            except Exception as e2:
                raise PlecsError(f"[plecs.statistics] 连接失效且重建失败：{e2}") from e2

    # ── plecs.load / plecs.close ─────────────────────────────────────────

    def load(self, path: str) -> str:
        """
        plecs.load(mdlFileName) — 打开 .plecs 模型（绝对路径）。

        ★ 实测 4.7.5：**成功返回 0（int）**，文件不存在抛 Fault("File not found")。
        故真实模型名必须从 statistics 反查：
          1) 与 load 前相比新出现的 id
          2) 新 id 查不到时，按 filename 路径匹配（处理重载同名模型）
          3) 都失败才回落到「文件名去扩展名」
        """
        before: set[str] = set()
        try:
            before = {m["id"] for m in self.statistics().get("models", [])}
        except PlecsError:
            pass

        result = self._call("plecs.load", path)

        name = result.strip() if isinstance(result, str) else ""
        if name and not name.lstrip("+-").isdigit():
            return name

        try:
            models = self.statistics().get("models", [])
        except PlecsError:
            models = []
        for m in models:
            if m["id"] not in before:
                return m["id"]
        target = _norm_path(path)
        for m in models:
            if m.get("filename") and _norm_path(m["filename"]) == target:
                return m["id"]
        return _name_from_path(path)

    def close(self, model_name: str) -> None:
        """
        plecs.close(mdlName) — 关闭模型。
        官方行为：无条件关闭且**不保存**；仿真运行中绝不可调用。
        """
        self._call("plecs.close", model_name)

    # ── plecs.get / plecs.set ────────────────────────────────────────────

    def get(self, path: str, param: str = "") -> Any:
        """
        plecs.get(componentPath[, parameter]) — 读参数。
        param 省略/为空时返回该组件**全部**参数（struct 数组）。
        """
        if param:
            return self._call("plecs.get", path, param)
        return self._call("plecs.get", path)

    def set_param(self, path: str, param: str, value: Any) -> None:
        """plecs.set(componentPath, parameter, value) — 写参数（内存生效，不落盘）。"""
        self._call("plecs.set", path, param, value)

    # ── plecs.simulate ───────────────────────────────────────────────────

    def simulate(self, model_name: str, opts: dict | list | None = None) -> Any:
        """
        plecs.simulate(mdlName[, optStruct]) — 阻塞运行时域仿真。

        optStruct 官方字段：
            ModelVars    : dict   覆盖模型初始化变量
            SolverOpts   : dict   Solver/StartTime/TimeSpan/OutputTimes/
                                  OutputTimesOption/MaxStep/InitStep/FixedStep/
                                  AbsTol/RelTol/Refine/Timeout/InitialSystemState
            OutputFormat : str    "Plain"（默认）或 "MatFile"
            Name         : str    批量仿真时单条命名

        opts 传 list[dict] 时走官方并行接口（多核分发），返回 list。
        返回：顶层有 outport 时 {Time, Values}；否则空（None/{}）。

        ★ OutputFormat="MatFile" 时 PLECS **不写文件**，直接把 MAT 二进制当返回值
          返回，这里包装成 {"_binary": bytes} 交给上层落盘（否则 dict(bytes) 会崩）。
        """
        if opts is None:
            result = self._call("plecs.simulate", model_name, long_op=True)
        else:
            result = self._call("plecs.simulate", model_name, opts, long_op=True)
        if isinstance(result, list):
            return [{"_binary": b} if (b := _as_bytes(r)) is not None else r
                    for r in result]
        blob = _as_bytes(result)
        if blob is not None:
            return {"_binary": blob}
        return dict(result) if result else {}

    # ── plecs.analyze ────────────────────────────────────────────────────

    def analyze(self, model_name: str, analysis_name: str,
                opts: dict | list | None = None) -> Any:
        """
        plecs.analyze(mdlName, analysisName[, optStruct]) — 阻塞运行分析。
        analysisName 必须与模型 Analysis Tools 中定义的名称一致。

        optStruct：ModelVars / SolverOpts / AnalysisOpts。
        AnalysisOpts 常用键（小信号）：
            FrequencyRange : [f_min, f_max]（Hz）
            FrequencyScale : "lin" / "log"
            NumPoints      : int
            AmplitudeRange : [a_min, a_max]（AC sweep 扰动幅值范围）
            Perturbation   : 小信号扰动块路径（不含模型名）
            Response       : 小信号响应块路径（不含模型名）
        稳态分析常用：TimeSpan / Tolerance / MaxIter / InitCycles / ShowCycles。

        返回：AC sweep / 脉冲 / 多音 → {F, Gr, Gi}（逐频率实部/虚部数组）；
              稳态 → {Time, Values}；opts 传 list 时返回 list。
        """
        if opts is None:
            result = self._call("plecs.analyze", model_name, analysis_name, long_op=True)
        else:
            result = self._call("plecs.analyze", model_name, analysis_name, opts, long_op=True)
        if isinstance(result, list):
            return [{"_binary": b} if (b := _as_bytes(r)) is not None else r
                    for r in result]
        blob = _as_bytes(result)
        if blob is not None:
            return {"_binary": blob}
        return dict(result) if result else {}

    # ── plecs.scope ──────────────────────────────────────────────────────

    def scope(self, scope_path: str, command: str, *args) -> Any:
        """
        plecs.scope(scopePath, command, ...) — Scope 轨迹操作。

        ★ 4.7.5 RPC 实测可用命令（其余文档命令 RPC 端被拒）：
            ClearTraces                     清除全部轨迹
            HoldTrace [traceName]           保留上次仿真波形为轨迹，返回轨迹名
            RemoveTrace traceName           删除指定轨迹
            SaveTraces fileName             保存轨迹数据（.trace 专有格式）
            LoadTraces fileName             载入轨迹数据

        ★ ExportCSV / ExportBitmap / GetCursorData 在 4.7.5 RPC 上报
          "Invalid scope command"/"invalid method parameters"；
          定量数据请在 PLECS GUI 导出 CSV（或 SaveTraces 后离线解析 .trace）。

        scopePath 含模型名，如 "BuckConv/Scope"；分析图用
        "Model/Analyses/<分析名>"。相对文件路径按模型所在目录解释。
        """
        return self._call("plecs.scope", scope_path, command, *args)

    # ── plecs.webserver / plecs.codegen ──────────────────────────────────

    def webserver(self, command: str, *args) -> Any:
        """plecs.webserver(command, ...) — Web 仿真服务器 start/stop/status。"""
        return self._call("plecs.webserver", command, *args)

    def codegen(self, model_name: str, opts: dict | None = None) -> dict:
        """plecs.codegen(mdlName[, opts]) — 代码生成。"""
        result = self._call("plecs.codegen", model_name, opts or {}, long_op=True)
        return dict(result) if result else {}


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _name_from_path(path: str) -> str:
    import os
    return os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]
