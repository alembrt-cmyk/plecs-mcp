# PLECS MCP Server

> 让 AI Agent 直接驱动 PLECS 4.7 Standalone 进行电力电子仿真

参考 MathWorks 的 MATLAB MCP Server 方案，为 PLECS 4.7 提供等效的 MCP 接入层。
AI Agent（Claude Code、GitHub Copilot 等）可以直接操作 PLECS 模型、运行仿真、做频域分析，无需手动操作界面。

```
┌─────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│    AI Agent     │◄─MCP──►│  PLECS MCP Server │◄──────►│  PLECS 4.7       │
│  (Claude Code,  │        │  (本项目, Python) │ XML-RPC│  Standalone      │
│   Copilot 等)   │        └──────────────────┘  :1080  └──────────────────┘
└─────────────────┘
```

---

## 快速开始

### 1. 前置条件

- Python 3.10+
- PLECS 4.7 Standalone（需有效许可证）
- 支持 MCP 的 AI Agent

### 2. 安装

```bash
git clone https://github.com/alembrt-cmyk/plecs-mcp.git
cd plecs-mcp
pip install -e .
```

### 3. 开启 PLECS XML-RPC 接口

在 PLECS 中：

**Preferences → Simulation → 勾选 "Start XML-RPC server" → 端口保持 1080 → OK**

每次启动 PLECS 后自动生效。

### 4. 配置 AI Agent

编辑 `config.yml`（可选，也可用环境变量覆盖）：

```yaml
plecs:
  host: "localhost"   # PLECS 所在主机
  port: 1080          # XML-RPC 端口
```

将 MCP Server 注册到你的 Agent：

**Claude Code** (`~/.claude/mcp.json`):
```json
{
  "mcpServers": {
    "plecs": {
      "command": "python",
      "args": ["/absolute/path/to/plecs-mcp/server.py"]
    }
  }
}
```

**VS Code / Copilot** (`.vscode/mcp.json`):
```json
{
  "servers": {
    "plecs": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/plecs-mcp/server.py"]
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "plecs": {
      "command": "python",
      "args": ["/absolute/path/to/plecs-mcp/server.py"]
    }
  }
}
```

> **注意**：`server.py` 由 Agent 在需要时自动启动，**无需手动运行**。

### 5. 验证

重启 Agent 后，让它执行：
```
调用 plecs_connect，告诉我 PLECS 的版本号
```

返回版本号即表示配置成功。

---

## 工具列表

MCP tool 与 PLECS 4.7.x XML-RPC 接口**一一对应**（对照官方手册 RPC 章节 + 4.7.5 实测）。

| MCP Tool | 对应 RPC | 说明 |
|---|---|---|
| `plecs_connect` | `plecs.statistics()` | 建立连接，返回版本和已打开模型（含 [库]/[仿真中] 标记） |
| `plecs_status` | `plecs.statistics()` | 查看当前连接状态、运行中的仿真/分析数 |
| `plecs_load` | `plecs.load()` | 打开 .plecs 模型文件 |
| `plecs_close` | `plecs.close()` | 关闭模型（**仿真运行中默认拒绝**，force 可强关） |
| `plecs_get` | `plecs.get()` | 读取参数；param 留空 = 全部参数 |
| `plecs_set` | `plecs.set()` | 修改参数（内存生效，不落盘） |
| `plecs_simulate` | `plecs.simulate()` | 运行时域仿真（**阻塞**；支持 list 批量并行；`save_mat` 落盘 MAT） |
| `plecs_analyze` | `plecs.analyze()` | 运行频域/稳态分析（**阻塞**；官方 F/Gr/Gi → 自动算幅相/PM） |
| `plecs_scope` | `plecs.scope()` | Scope 轨迹：ClearTraces/HoldTrace/RemoveTrace/SaveTraces/LoadTraces |
| `plecs_get_init` | `plecs.get(…, "InitializationCommands")` | 读初始化脚本全文 |
| `plecs_set_init_var` | get + 正则替换 + set | 改 init 单个标量变量（内存生效） |
| `plecs_list_methods` | `system.listMethods` / `system.methodHelp` | RPC 接口自描述 |
| `plecs_webserver` | `plecs.webserver()` | 控制 PLECS Web 仿真服务器 |
| `plecs_codegen` | `plecs.codegen()` | 触发代码生成 |
| `plecs_trace_read` | —（**离线**） | 解析 `.trace` → 逐曲线 min/max/rms，可导 CSV |
| `plecs_file_info` | —（**离线**） | `.plecs` 概览：求解器/时长/组件统计/树/InitCommands |
| `plecs_file_blocks` | —（**离线**） | 列组件（按名/类型筛）+ 端口表 |
| `plecs_file_get` | —（**离线**） | 读某块参数（含 C-Script 的 Declarations/Ts） |
| `plecs_file_set` | —（**离线**） | 改 `.plecs` 参数并写盘（自动备份 + 回读校验） |

> 标「离线」的四个工具**不需要 PLECS 在运行**：直接解析磁盘文件。RPC 没有「列组件 / 列端口」
> 接口，也没有「改磁盘模型」接口，所以这两类需求只能走离线工具。

> **4.7.5 实测注意**（与旧文档的差异）：
> - `statistics().models` 每项是 `{id, filename, library, state, age}`（**没有 `simulating` 键**；仿真中 `state="simulation running"`，本库已派生 `simulating` 并保留 `filename/state`）。
> - `plecs.load` **成功返回 `0`**（不是模型名），文件不存在才抛 Fault；真实模型名由本库从 statistics 反查。
> - `OutputFormat="MatFile"` **不写文件**，PLECS 把 MAT 二进制当 RPC 返回值传回；本库用 `save_mat` 代为落盘。
> - `version` 是 `[4,7,5,"hash date"]` 列表（本库已归一化）。
> - 仿真时长写 `SolverOpts.TimeSpan`（秒）；`StopTime` 已废弃（工具会自动归入 SolverOpts 并警告）。
> - `plecs.simulate` **阻塞**，且仅当模型顶层有 Outport 才返回 `{Time, Values}`；否则空返回属正常，取波形走 `SaveTraces`。
> - `plecs.scope` 的 **ExportCSV / ExportBitmap / GetCursorData 在 4.7.5 RPC 上不可用**，定量数据请在 GUI 导出 CSV 或离线解析 `.trace`。
> - 仿真运行中绝不可 `close`（工具已加防呆）。

---

## 工具详细说明

### `plecs_connect`

每次 Agent 会话开始时**必须首先调用**。底层调用 `plecs.statistics()` 验证连接。

```
参数：
  host  (str, 可选)  PLECS 主机，默认读 config.yml
  port  (int, 可选)  端口，默认读 config.yml

返回：版本号、build 信息、当前已打开的模型列表
```

### `plecs_status`

无参数。随时可调用，用于确认连接是否仍然有效。

### `plecs_load` / `plecs_close`

```
plecs_load(path)          打开文件，返回模型名称
plecs_close(model_name)   关闭模型（不自动保存）
```

### `plecs_get` / `plecs_set`

```
plecs_get(path, param)              读取参数值
plecs_set(path, param, value)       修改参数值，自动显示修改前的旧值
```

**路径格式**（用 `/` 分隔层级）：

```
"BuckConv"           → 模型级参数（TimeSpan、Solver、AbsTol 等）
"BuckConv/L1"        → 顶层组件参数（L、R、C、V_init 等）
"BuckConv/Ctrl/PID"  → 子系统内组件参数
```

### `plecs_simulate`

```
plecs_simulate(model_name, opts='{}')
```

`opts` 是 JSON 字符串（官方 optStruct，支持便捷键自动归位）：

```json
{
  "ModelVars":    {"Vin": 48, "R_load": 10},
  "TimeSpan":     0.005,
  "SolverOpts":   {"Solver": "radau", "MaxStep": 1e-6, "AbsTol": 1e-6, "RelTol": 1e-3},
  "OutputFormat": "Plain"
}
```

- `TimeSpan` 为便捷键（自动归入 `SolverOpts.TimeSpan`）；`StopTime` 已废弃会告警。
- 顶层无 Outport 时返回空属正常——工具会提示改走 `plecs_scope` 的 `SaveTraces`。
- `opts` 传 JSON **数组**走官方并行接口（多核分发），返回逐条结果/错误列表：
  `'[{"ModelVars":{"R":5},"Name":"轻载"},{"ModelVars":{"R":10},"Name":"满载"}]'`

返回各输出通道的 min / max / avg / 峰峰值 / RMS 统计（有 Outport 时）。

**要全精度时序**（不只是统计量）时用 MatFile：

```
plecs_simulate("M", '{"TimeSpan": 0.02, "OutputFormat": "MatFile"}', save_mat="C:/out/r.mat")
```

PLECS 本身不落盘，它把 MAT 二进制直接当返回值传回，本工具代写到 `save_mat` 路径
（留空则写 `<当前目录>/<模型名>_result.mat`）。

### `plecs_analyze`

```
plecs_analyze(model_name, analysis_type, opts='{}')
```

`analysis_type` 必须与模型中 Analysis Tools 定义的名称完全一致（如 `"ACSweep"`、`"SteadyState"`）。

`opts` 官方字段：`ModelVars` / `SolverOpts` / `AnalysisOpts`。ACSweep 示例：
```json
{
  "AnalysisOpts": {
    "FrequencyRange": [10, 100000],
    "FrequencyScale": "log",
    "NumPoints":      50,
    "AmplitudeRange": [0.01, 0.01],
    "Perturbation":   "Ctrl/Pert",
    "Response":       "Ctrl/Resp"
  }
}
```

返回官方 `F/Gr/Gi` → 工具自动换算幅相（dB/°）并**逐通道**给出穿越频率与相位裕度
（MIMO 多行输出不再只取第 1 行；文本表只列通道 1，其余通道给摘要）。
旧键名（`FreqRange`/`LogScale`/`Amplitude`）自动映射并告警；`SysName`/`SettlingTime` 已废弃。

### `plecs_scope`

```
plecs_scope(scope_path, command, args='[]')
```

**4.7.5 RPC 实测可用命令**：

| command | args 说明 |
|---|---|
| `"ClearTraces"` | 无（`"[]"`） |
| `"HoldTrace"` | 可选标签：`'["轻载"]'`，返回轨迹名 |
| `"RemoveTrace"` | 轨迹名：`'["轻载"]'` |
| `"SaveTraces"` | 文件路径：`'["C:/out/wave.trace"]'` |
| `"LoadTraces"` | 文件路径：`'["C:/out/wave.trace"]'` |

> `ExportCSV` / `ExportBitmap` / `GetCursorData` 在 4.7.5 RPC 端报
> "Invalid scope command" / "invalid method parameters"，**不可用**。
> 定量数据请在 PLECS GUI 导出 CSV，或 `SaveTraces` 后离线解析 `.trace`。

### `plecs_webserver`

```
plecs_webserver(command, args='[]')
command: "start" / "stop" / "status"
```

### `plecs_codegen`

```
plecs_codegen(model_name, opts='{}')
```

`opts` 示例：`'{"GenerateCode": true, "BuildCode": true}'`

### `plecs_trace_read`（离线）

```
plecs_trace_read(trace_path, save_csv="", head=0, max_curves=0, keep=True)
```

`Score.SaveTraces` 存下来的 `.trace` 是二进制私有格式（无官方文档），本工具内置逆向出来的
v2 解析器（见文末附录）。**顶层没有 Outport 的模型，这是唯一能拿到数值波形的路**。

```
plecs_scope("M/Scope", "SaveTraces", '["C:/out/wave.trace"]')
plecs_trace_read("C:/out/wave.trace", save_csv="C:/out/wave.csv")
```

- `save_csv`：留空不写；给 `.csv` 写该文件；给目录写 `<目录>/<trace 名>.csv`。
  时间基一致时写宽表（首列 `t`），否则每条曲线两列 `t,v`。
- `keep=False`：不保留数组、只流式算统计量（>100 MB 文件必用，此时不能导 CSV）。
  超过 80 MB 会自动切到这个模式。
- 实测：6303 点 3 曲线 ≈ 0 ms；112 MB / 6 曲线 / 每曲线 100 万点 `keep=False` 0.33 s。

### `plecs_file_info` / `plecs_file_blocks` / `plecs_file_get` / `plecs_file_set`（离线）

```
plecs_file_info(path, tree_depth=1)                      模型概览 + 组件统计 + 树 + InitCommands
plecs_file_blocks(path, query="", kind="", ports=False)   列组件（query 同时匹配名称/类型）
plecs_file_get(path, block="@", var="", max_len=400)      读参数
plecs_file_set(path, block, var, value, dry_run=False)    改参数并写盘
```

`block` 选择器（四种写法等价可用）：

| 写法 | 含义 |
|---|---|
| `@` | 模型根（`TimeSpan`/`Solver`/`InitializationCommands` 都在这） |
| `#12` | 第 12 个组件（序号与 `plecs_file_blocks` 输出一致） |
| `"环路控制"` | 名称精确匹配；不中则退化为「唯一子串」（名称或类型） |
| `"Sub1/L1"` | 子系统路径 |

`plecs_file_set` 只改目标那几行，其余字节不动（CRLF/LF、缩进、列对齐、base64 分段都保留），
动前生成带时间戳的 `.bak`，写完回读校验。

```
plecs_file_get("m.plecs", block="环路控制", var="Declarations")
plecs_file_set("m.plecs", block="锁相环", var="Parameters", value="60, fsw, 1.414, 100, 5000")
```

> ⚠ 改的是**磁盘文件**。PLECS 里若已打开该模型，要关闭重开才会重新加载（内存模型不受影响，
> 改内存用 `plecs_set` / `plecs_set_init_var`）。


---

## 并发与连接

- 主连接（get/set/load/simulate…）内部串行加锁：同一 `ServerProxy` 的 HTTP 连接不能并发，
  并发调用会 `CannotSendRequest`，加锁后安全。
- `plecs_status` / `plecs_close` 的检查走**独立连接**，因此**长仿真跑着时仍能查状态**
  （实测 5 s 仿真期间查询耗时 2 ms）。
- RPC 没有 abort 接口：仿真超时只是本端放弃等待，PLECS 里可能还在跑，先 `plecs_status` 看 `state`。

## 典型工作流

### 参数研究

```
1. plecs_connect
2. plecs_load("C:/models/buck.plecs")       → 得到 model_name="BuckConverter"
3. plecs_get("BuckConverter/L1", "L")        → 查看当前电感值
4. plecs_set("BuckConverter", "TimeSpan", "0.005")
5. plecs_simulate("BuckConverter", '{"ModelVars": {"Vin": 48, "R_load": 10}}')
6. plecs_close("BuckConverter")
```

### 环路增益分析

```
1. plecs_connect
2. plecs_load("C:/models/buck_closed_loop.plecs")
3. plecs_analyze("BuckConverter", "ACSweep",
     '{"AnalysisOpts": {"FrequencyRange": [10, 500000], "NumPoints": 60,
                         "Perturbation": "Pert", "Response": "Resp"}}')
   → 自动输出穿越频率和相位裕度
```

### 波形对比

```
1. plecs_scope("BuckConverter/Scope", "ClearTraces")
2. plecs_simulate("BuckConverter", '{"ModelVars": {"R_load": 50}}')
3. plecs_scope("BuckConverter/Scope", "HoldTrace", '["轻载 50Ω"]')
4. plecs_simulate("BuckConverter", '{"ModelVars": {"R_load": 10}}')
5. plecs_scope("BuckConverter/Scope", "HoldTrace", '["满载 10Ω"]')
6. plecs_scope("BuckConverter/Scope", "SaveTraces", '["C:/compare.trace"]')
7. plecs_trace_read("C:/compare.trace", save_csv="C:/compare.csv")
   → 逐曲线 min/max/avg/pp/rms + 可给 pandas/numpy 直接读的 CSV
   （截图仍要在 PLECS GUI 导出——4.7.5 RPC 无 ExportBitmap）
```

### 离线查模型结构（PLECS 没开也行）

```
1. plecs_file_info("C:/models/5-level-c.plecs")            概览：229 个组件、radau、0.05 s
2. plecs_file_blocks("C:/models/5-level-c.plecs", kind="CScript", ports=True)
   → #171 环路控制 [12 12 16 1] / #172 锁相环 [3 → 11]
3. plecs_file_get("C:/models/5-level-c.plecs", block="环路控制", var="Ts")
4. plecs_file_set("C:/models/5-level-c.plecs", block="环路控制", var="Ts", value="1/(fc*100)")
```

---

## 项目结构

```
plecs-mcp/
├── server.py          # MCP Server 主入口，所有 tool 定义在此
├── plecs_client.py    # PLECS XML-RPC 客户端封装（一方法对应一 RPC）
├── trace_reader.py    # .trace 二进制解析器（离线，逆向自 4.7.5）
├── plecs_file.py      # .plecs 私有大括号格式解析 / 改参（离线）
├── config.yml         # 连接配置（host/port）
├── pyproject.toml     # 依赖和安装配置
├── tools/
│   ├── smoke_full.py     # 全链路冒烟（工程模型）
│   ├── smoke_demo.py     # 全链路冒烟（官方 demo）
│   ├── e2e_smoke.py      # 走 FastMCP.call_tool 的在线端到端
│   ├── e2e_offline.py    # 离线 5 工具端到端（不需要 PLECS）
│   ├── verify_p0.py      # 真机验证 P0 修复（并发/MatFile/状态）
│   └── verify_file_roundtrip.py  # .plecs 改参往返 + diff 校验
└── tests/
    ├── test_server.py   # 73 个 RPC 工具单元测试
    └── test_offline.py  # 44 个离线解析器/工具单元测试
```

---

## 环境变量

所有配置均可被环境变量覆盖（优先级高于 config.yml）：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `PLECS_HOST` | PLECS 主机地址 | `localhost` |
| `PLECS_PORT` | XML-RPC 端口 | `1080` |
| `PLECS_TIMEOUT` | 普通 RPC 超时（秒） | `15` |
| `PLECS_LONG_TIMEOUT` | 阻塞命令（simulate/analyze/codegen）超时（秒） | `600` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

断连时客户端自动重连一次；超时/仿真类错误不重试（避免重复跑仿真）。

---

## 运行测试

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

117 个测试全部使用 Mock / 合成数据，无需启动 PLECS（其中 73 个覆盖 RPC 工具，
44 个覆盖 `.trace` 与 `.plecs` 离线解析）。

---

## 附录：`.trace` 格式（逆向，无官方文档）

大端裸二进制、无魔数。4.7.5 实测布局：

```
i32 version(=2)
i32 nTrace
nTrace × { str name, i32 nSig, nSig × str sigName }
i32 nLabel
nLabel × str label                       # HoldTrace 的标签
--- 文件级时间基（只存一份）---
5×i32 0
i32 nPoints                              # 实际点数 = nPoints - 1
i32 0, i32 0
(nPoints-1) × f64 时间向量
--- 逐条曲线（顺序 = 头部声明顺序）---
[可变长前缀 1~8B][f64 ymax][f64 ymin] 12B 0 + i32 nPoints + 8B 0
(nPoints-1) × f64 数据

str = i32 字节长度 + UTF-16BE 正文（长度 -1 = 空）
```

解析要点（都是踩出来的）：

1. 时间基只有**一份**，在整个头部之后；不要按「每条 trace 一组时间基」去解。
2. 曲线块头长度有 8 字节抖动 → 不能按固定偏移切，必须以尾部
   `12B 0 + 自描述点数 + 8B 0` 为锚点，再要求「正好吃到文件末尾」（回溯法）。
3. 上一条曲线的 `ymin` 若是整值（如 `-3.0`），其尾随零字节会与本块 12 字节零连成一片，
   产生偏 8 字节的假锚点 → **自描述点数的候选必须优先**，`n==0` 的模糊候选排最后。
4. 头部声明的曲线数与实际数据块数可能不符（多次 HoldTrace）→ 兜底按自描述点数贪心读完，
   并截断到头声明数（多出来的块无法命名）。
5. 超过 100 MB 的文件用 `keep=False` 流式统计，别把数组留在内存里。


---

## 许可证

MIT License — 仅限配合合法的 PLECS 许可证使用。  
PLECS® 是 Plexim GmbH 的注册商标。
