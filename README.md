# Agent-loop-system

Agent-loop-system 是面向手表固件的自动化闭环工作台：读取缺陷或测试用例，驱动 Windows
Simulator 或真实手表，采集新鲜证据，并输出可审计的执行结果。

本仓库只保存 Agent-loop 的编排代码、测试、用例映射和稳定文档。固件源码、设备绑定、密钥
以及运行证据都有独立边界，不应混入同一个 Git 基线。

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

截图是产品视觉结论的权威证据。命令 ACK、GUI tree、串口日志、源码和模型输出只用于诊断，
不能单独替代截图判定。

## 目标、profile 与 case map

| 目标 profile | 执行方式 | 固件工作区 | case map |
| --- | --- | --- | --- |
| `620C_W6830` | Windows Simulator | `D:\Agent-loop-workspace\620C_W6830` | `case_map/620C_simulator_case_map` |
| `6202_W5230_SIMULATOR` | Windows Simulator | `D:\Agent-loop-workspace\6202_W5230` | `case_map/6202_simulator_case_map` |
| `6202_W5230` | 真实手表 | `D:\Agent-loop-workspace\6202_W5230` | `case_map/6202_case_map` |

三套映射相互隔离，不得跨目标复制命令、坐标、页面、截图或 verdict。6204 真机源码位于
`D:\Agent-loop-workspace\6204_W5230`；在独立构建、另行授权刷机和真机最小能力验证完成前，
它不是可执行 Runner 目标。`D:\TOPSTEP\shenju_w30` 只作上游参考，不在其中开发、构建或
打补丁。

部分路径和模型选择仍由环境变量及显式 Python 配置提供。在数据化 profile 迁移真正完成前，
不要把计划中的接口当成已交付能力。

## 快速开始

要求：Windows、Python 3.12 和 `uv`。

```powershell
Set-Location C:\path\to\Agent-loop-system
Copy-Item .env.example .env
uv sync
uv run pytest -q
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器入口默认为 <http://127.0.0.1:8765>。`.env.example` 只描述配置字段；复制后在本机填写
`.env`。API key、token、账号标识、BLE 地址、COM 口和开发机绝对路径不得提交。

## 验证层级

以下事实彼此独立，不能互相代替：

1. **传输完成**：命令已接收、队列已通过栅栏或文件已传输，只证明通道工作。
2. **证据完整**：每个视觉检查点都有一张本轮新截图，且 `result.json` 的证据合同完整。
3. **产品 verdict**：PASS、FAIL、CANNOT_VERIFY 或 ERROR；视觉结论只由截图支持。
4. **映射成熟度**：只有精确的 `mapping_status=PROMOTED` 才表示步骤已固化。产品 FAIL 也可在
   路径与证据完整时晋升；`mapping_status` 与 verdict 必须分开记录。
5. **基线状态**：构建成功不等于刷机成功，刷机成功也不等于产品行为通过；源码快照更不等于
   已验证基线。

case map 的唯一数据合同和动态统计见 [case_map 数据合同](case_map/README.md)。

## 仓库与基线边界

- `D:\Agent-loop-system` 保存编排器、Runner、前端、测试、case map 和文档。
- 固件工作区是独立 Git 仓库，可能包含 `app`、`core/comm`、`core/gui`、`core/lvgl` 等嵌套仓库。
- `artifacts/`、`evidence/`、`history/`、`logs/` 等目录是本机运行输出，不进入源码基线。
- 可复现固件清单必须记录根仓库和嵌套仓库的提交、dirty 状态、项目配置及关键产物哈希。

本项目区分源码快照和已验证基线。源码快照只保证仓库可恢复；已验证基线还必须固定目标、固件
多仓版本、配置、资源、产物哈希和测试结果，并说明所有工作区漂移。历史预基线记录与通用验收
条件见 [基线说明与 2026-08-14 历史快照](docs/baselines/current.md)，该页面不是实时 Git 状态。

## 真机安全边界

- 6202 真机批次由 Runner 在启动或设备重启恢复后发送 `TEST_SESSION:START` 并等待
  `active`；不要求外部预先持有测试会话，普通用例结束也不发送 `STOP`。
- MTP 是 6202 默认截图链路；BLE 已通过真实完整 BMP 功能验证，但当前性能不足，不切换默认值。
- 构建、刷机和真机操作是分别授权的活动；构建许可不包含刷机许可。
- 不自动修改 Git remote，不自动 push，不在其他 Agent 活跃写入期间操作共享 branch 或 index。

## 后续优化安排

- BLE 截图性能：2026-08-18 真机单图约 172 秒，期间出现 644 次
  `lld mem alloc buf fail type 0x2`。图片完整、校验通过，关闭 USB 后也能恢复 USB；当前没有明确可落地的
  修复方案，因此接受该限制并暂不处理。只有后续明确要求缩短 BLE 截图时间，或该日志开始影响稳定性时，
  才重新调查；在此之前继续以 MTP 为默认截图方式。

## 文档入口

- [Agent 工作边界](AGENTS.md)：必须遵守的安全和目标隔离政策。
- [case map 数据合同](case_map/README.md)：映射成熟度、外部探索账本与 Runner 选择规则。
- [6202 Simulator 当前链路](docs/6202-simulator-current.md)：Simulator 配置与已验证事实快照。
- [6202 真机截图概览](docs/6202-hardware-screenshot-current.md)：默认链路、已验证边界和开放阻塞。
- [6202 MTP 截图操作指南](docs/6202-mtp-screenshot-tool.md)：通过 SuperCom 共享会话取得 MTP 证据。
- [6202 Windows BLE 实验快照](docs/6202-watch-ble-current.md)：绑定实验与 BLE 截图 POC 边界。
- [6204 真机迁移现状](docs/6204-hardware-migration-current.md)：尚未启用 Runner 的 6204 源码快照。
- [基线说明与历史快照](docs/baselines/current.md)：基线术语、验收条件和非实时历史记录。
- [公开仓库快照边界](docs/repository-snapshot.md)：三类本地目录的纳入范围、排除项和工作区清单。

`codex，Hermes，trae交接文档/trae工作交接.md` 仅为历史交接材料，不是现行操作手册。
