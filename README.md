# Agent-loop-system

Agent-loop-system 是面向手表固件的自动化闭环工作台：读取缺陷或测试用例，驱动 Windows Simulator 或真实手表，采集新鲜证据，并输出可审计的执行结果。

本仓库只保存 Agent-loop 的编排代码、测试、用例映射和稳定文档。固件源码、设备绑定、密钥以及运行证据均有独立边界，不应混入同一个 Git 基线。

## 工作流

```text
缺陷 / 测试用例
        ↓
Agent-loop Runner
        ↓
执行目标（Simulator / Hardware）
        ↓
控制通道 + 截图通道
        ↓
result.json + 新截图 + 日志
        ↓
产品判定 / 修复迭代
```

截图是产品视觉结论的权威证据；命令 ACK、GUI tree、串口日志和模型输出用于定位问题，不能单独替代截图判定。

## 当前目标

| 目标 ID | 执行方式 | 用例映射 |
| --- | --- | --- |
| `620C_W6830` | Windows Simulator | `case_map/620C_simulator_case_map` |
| `6202_W5230` | 真实手表 | `case_map/6202_case_map` |
| `6202_W5230_SIMULATOR` | Windows Simulator | `case_map/6202_simulator_case_map` |

当前版本仍有部分项目、路径和模型选择分散在环境变量及 Python 配置字典中。后续迁移会将这些差异收敛为数据化 profile；在该迁移完成前，不要把“计划中的 profile 接口”当成已经可用的功能。

## 快速开始

要求：Windows、Python 3.12 和 `uv`。

```powershell
Set-Location C:\path\to\Agent-loop-system
Copy-Item .env.example .env
uv sync
uv run pytest -q
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器入口默认为 <http://127.0.0.1:8765>。

`.env.example` 只描述配置字段；复制后在本机填写 `.env`。以下内容不得提交：

- API key、token、账号标识；
- BLE 地址、COM 口等设备绑定；
- 开发机绝对路径；
- 运行日志、截图、数据库和测试证据。

## 仓库边界

Agent-loop 与固件仓库必须独立管理：

- `D:\Agent-loop-system`：编排器、Runner、前端、测试、case map 和文档；
- 固件工作区：独立 Git 仓库，可能包含 `app`、`core/comm`、`core/gui`、`core/lvgl` 等嵌套仓库；
- 上游固件目录：默认只读，不作为 Agent-loop 的提交目标；
- `artifacts/`、`evidence/`、`history/`、`logs/` 等目录：运行输出，不进入源码基线。

固件版本不能只记录根仓库 HEAD。可复现清单至少要包含根仓库和所有嵌套仓库的 commit、dirty 状态、项目配置以及关键构建产物哈希。

## 基线定义

本项目区分两个层级：

1. **源码快照（source snapshot）**：完整、脱敏、可以从 Git 恢复的 Agent-loop 源码，即使尚有已知测试失败也可以建立。
2. **已验证基线（validated baseline）**：源码快照之上，固定目标 profile、固件多仓版本、配置哈希、产物哈希和测试结果，且没有未说明的工作区漂移。

并行 Agent 仍在同一工作树写入时，只允许准备不重叠的文档和忽略规则；不要切分支、暂存或提交。当前基线事实和验收门槛见 [docs/baselines/current.md](docs/baselines/current.md)。

## 可插拔迁移约束

后续新增项目、固件、手表或模型时，目标是只增加配置和业务映射，不修改 Runner 主流程。一个目标 profile 应组合：

- 执行目标：Simulator 或 Hardware；
- 固件 profile：源码布局、项目名、构建与产物规则；
- 手表 profile：控制能力、截图能力和本机设备绑定键；
- case-map profile：业务步骤到目标命令的映射；
- model profile：模型、端点、超时和角色用途；
- 受控 adapter：构建、控制、截图和模型 provider。

adapter 使用显式、静态注册表，避免从配置文件动态导入任意 Python 模块。真实路径、设备地址、端口和密钥继续只放在本机 `.env`。

## 真机注意事项

- 6202 测试会话由外部人员或批次控制器启动；Runner 只读查询状态，不负责 `START`、续期或 `STOP`。
- 当前正式截图路径以 MTP 证据合同为准；BLE 截图仍属于独立实验通道，必须经过完整性、CRC、图像可读性和断线恢复验收后才能切换默认值。
- 构建成功不等于刷机或真机验收成功；刷机必须另行明确授权。

## 文档入口

- [当前 Git 与固件基线](docs/baselines/current.md)
- [6202 真机截图现状](docs/6202-hardware-screenshot-current.md)
- [case map 说明](case_map/README.md)
- [Agent 工作约束](AGENTS.md)

## 安全约定

- 不自动修改 Git remote，不自动 push，不自动刷机；
- 不在其他 Agent 活跃写入期间操作共享 branch 或 index；
- 不为追求“全绿”而隐藏失败，测试失败必须记录适用范围和原因；
- 不将历史证据改写成当前证据，每次视觉检查点都采集独立的新截图。
