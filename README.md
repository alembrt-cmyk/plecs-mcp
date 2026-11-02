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
git clone https://github.com/your-org/plecs-mcp.git
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
| `plecs_simulate` | `plecs.simulate()` | 运行时域仿真（**阻塞**；支持 list 批量并行） |
| `plecs_analyze` | `plecs.analyze()` | 运行频域/稳态分析（**阻塞**；官方 F/Gr/Gi → 自动算幅相/PM） |
| `plecs_scope` | `plecs.scope()` | Scope 轨迹：ClearTraces/HoldTrace/RemoveTrace/SaveTraces/LoadTraces |
| `plecs_get_init` | `plecs.get(…, "InitializationCommands")` | 读初始化脚本全文 |
| `plecs_set_init_var` | get + 正则替换 + set | 改 init 单个标量变量（内存生效） |
| `plecs_list_methods` | `system.listMethods` / `system.methodHelp` | RPC 接口自描述 |
| `plecs_webserver` | `plecs.webserver()` | 控制 PLECS Web 仿真服务器 |
| `plecs_codegen` | `plecs.codegen()` | 触发代码生成 |

> **4.7.5 实测注意**（与旧文档的差异）：
> - `statistics().models` 每项是 `{id, library, simulating}` dict；`version` 是 `[4,7,5,"hash date"]` 列表（本库已归一化）。
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

返回官方 `F/Gr/Gi` → 工具自动换算幅相（dB/°）并计算穿越频率和相位裕度。
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

---

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
   （截图/CSV 需在 PLECS GUI 手动导出——4.7.5 RPC 无 ExportBitmap/ExportCSV）
```

---

## 项目结构

```
plecs-mcp/
├── server.py          # MCP Server 主入口，所有 tool 定义在此
├── plecs_client.py    # PLECS XML-RPC 客户端封装（一方法对应一 RPC）
├── config.yml         # 连接配置（host/port）
├── pyproject.toml     # 依赖和安装配置
├── tools/
│   ├── smoke_full.py  # 全链路冒烟（工程模型）
│   └── smoke_demo.py  # 全链路冒烟（官方 demo：simulate 返回值/scope/init 读写）
└── tests/
    └── test_server.py # 61 个单元测试（无需真实 PLECS）
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

61 个测试全部使用 Mock，无需启动 PLECS。

---

## 许可证

MIT License — 仅限配合合法的 PLECS 许可证使用。  
PLECS® 是 Plexim GmbH 的注册商标。
