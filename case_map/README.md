# case_map 数据合同

更新时间：2026-08-17

## 一句话规则

3164 条用例全部可以从前端交给 Runner。`PROMOTED` 用例按固化步骤执行；没有固化步骤的用例由 Agent-loop 在本次运行中临时探索。临时探索只写运行历史，不得改变外部探索状态，也不得自动写回正式步骤。

## 目录与当前基线

| profile | 目录 | 全部 | 外部已探索 | 已探索未固化 | 已固化 |
|---|---|---:|---:|---:|---:|
| `620C_W6830` | `620C_simulator_case_map` | 3164 | 0 | 0 | 0 |
| `6202_W5230` | `6202_case_map` | 3164 | 11 | 11 | 0 |
| `6202_W5230_SIMULATOR` | `6202_simulator_case_map` | 3164 | 563 | 465 | 98 |

每个目录包含 40 个模块 JSON 和一份 `external_execution_history.jsonl`。目标之间不得复制命令、坐标、页面、证据或结论，也不得在缺文件时回退到另一套目录。

## 成熟度只有两个事实源

前端的“外部探索与固化”分类只读取下面两个地方。

### 1. 外部探索账本

目标目录中的 `external_execution_history.jsonl` 只回答：这条用例是否被 Agent-loop 之外的外部 Agent 完整探索过。

一条用例一行、`case_id` 唯一，最小格式如下：

```json
{"case_id":"CALC_003","sheet":"计算器","target":"6202_W5230","last_verified":"2026-08-17","evidence_root":"D:/Agent-loop-system","evidence_paths":["evidence/batch/CALC_003/result.json"]}
```

只写事实字段：

- `case_id`、`sheet`、`target`
- `last_verified`
- `evidence_root` 和本轮真实 `evidence_paths`

不要在账本重复写 `mapping_status`、PASS/FAIL、固化结论、运行次数、`unable` 或资格说明。这些字段会与正式映射和运行历史漂移。

只有探索到达明确终点才登记：完成原始步骤并取得证据，或者用当前目标的源码与实际执行证明确认了具体能力/外部判据缺口。尚未执行到业务动作、基础设施中断、只选中了用例或只阅读了旧记录，都不算外部探索完成。

### 2. 正式 case_map

40 个模块 JSON 只回答：Runner 应当怎样执行已经固化的路径。

正式固化条目必须同时满足：

- `mapping_status` 精确等于 `PROMOTED`
- `setup`、`actions`、`collect` 和 `verification_points` 来自当前目标的真实探索
- `actions` 至少有一个原始业务动作
- 正式 Runner 已用新证据目录复跑
- 每个视觉检查点有一张独立新截图，证据合同完整
- 最终截图结论为 PASS 或明确的产品 FAIL

产品 FAIL 可以固化正确路径；CANNOT_VERIFY、ERROR、缺图、步骤中断或候选已变化不能固化。

未固化条目长期状态必须是：四组执行字段为空、没有 `mapping_status`。`unable` 是旧字段，不再参与分类或运行入口；新流程不得用它表达“未探索”或“未固化”。

模块 JSON 顶层也不得再使用 `supported`、`execution_supported` 或 `unavailable_reason` 充当入口闸门。能力缺口由 Agent-loop 本次运行形成 CANNOT_VERIFY 或 ERROR，不产生第六种用例成熟度。

## 分类如何计算

```text
全部用例
├─ 尚未外部探索：case_id 不在外部探索账本
└─ 已经外部探索：case_id 在外部探索账本
   ├─ 已探索但未固化：没有精确的 mapping_status=PROMOTED
   └─ 已固化、Agent-loop 可执行：mapping_status=PROMOTED
```

数据必须满足：每个 `PROMOTED` 用例都已存在于同目标的外部探索账本。

运行结果是另一条独立轴：未运行、PASS、FAIL、CANNOT_VERIFY、ERROR。它来自 `history/tests` 的最近一次 Agent-loop 运行，不参与上面的成熟度分类。

## Runner 选择规则

普通前端、单条和批量 Runner 按以下顺序选择执行方式：

1. `mapping_status=PROMOTED`：执行固化步骤。
2. 其余情况：由 Agent-loop 根据 `precondition_text`、`steps_text`、`expected_text` 临时探索；即使 JSON 正处于候选复跑窗口，普通运行也不把候选当成固化步骤。

外部 Agent 独占用例并做正式准入复跑时，才可以显式传 `--candidate-replay`，让 Runner 执行尚未晋升的临时候选 `actions`。前端和普通批量不传这个参数。复跑失败、ERROR、CANNOT_VERIFY 或任务中断时，必须立即清空候选四组字段；不得把候选长期留在 JSON。Agent-loop 临时探索产生的命令只进入本次运行历史，不写入账本或 case_map。

## 外部 Agent 统一工作流

按目标使用对应技能：

- 6202 真机：`C:\Users\Administrator\.codex\skills\explore-agent-loop-hardware-cases\SKILL.md`
- Windows Simulator：`C:\Users\Administrator\.codex\skills\explore-agent-loop-simulator-cases\SKILL.md`

仓库本 README 和用户当轮明确要求优先于技能中的旧状态字段说明。

| 目标 | 候选步骤写入 | 外部探索事实写入 | 正式复跑参数 |
|---|---|---|---|
| 620C 模拟器 | `620C_simulator_case_map/<模块>.json` | `620C_simulator_case_map/external_execution_history.jsonl` | `--target simulator --case-map-profile 620C_W6830` |
| 6202 模拟器 | `6202_simulator_case_map/<模块>.json` | `6202_simulator_case_map/external_execution_history.jsonl` | `--target simulator --case-map-profile 6202_W5230_SIMULATOR` |
| 6202 真机 | `6202_case_map/<模块>.json` | `6202_case_map/external_execution_history.jsonl` | `--target hardware --case-map-profile 6202_W5230` |

候选正式复跑统一使用：

```powershell
python -m agent_loop_system.tools.test `
  --sheet <模块> --case-id <CASE_ID> `
  <上表正式复跑参数> --candidate-replay `
  --result-file <本轮唯一证据目录>/result.json `
  --screenshot-path <本轮唯一证据目录>/screenshot.bmp
```

Simulator 和真机仍分别遵守对应技能中的环境、会话、截图和证据门禁；上面的统一命令只规定数据入口与 Runner 选择方式，不允许跨目标复用命令或证据。

每条用例按同一顺序处理：

1. 锁定 profile、模块 JSON、case_id、当前目标源码/产物和唯一证据目录。
2. 先查本目标的 `external_execution_history.jsonl`；已有记录默认不重复探索，除非用户要求复测、目标版本变化或原证据不足。
3. 原样保留 `case_id`、`precondition_text`、`steps_text`、`expected_text`，只在当前目标真实探索，不从另一目标猜命令或坐标。
4. 探索到明确终点后，在本目标账本新增或更新该 case_id；证据路径必须指向本轮真实文件。
5. 能形成候选时，临时写入该 case 的 `setup/actions/collect/verification_points`，但先不要写 `PROMOTED`。
6. 用正式 Runner、全新证据目录和显式 `--candidate-replay` 复跑候选。
7. 准入通过才写精确的 `mapping_status: "PROMOTED"`；未通过立即清空候选，但保留外部探索账本记录。
8. 最后核对 JSON 可解析、账本 case_id 唯一、PROMOTED 是账本子集、截图与检查点一一对应。

外部 Agent 只写自己锁定的目标和 case_id，不改另一套 case_map，不把 Agent-loop 自身运行历史反填成“外部探索”，也不修改人工预期来制造 PASS。

## 用例 JSON 保留字段

```json
{
  "case_id": "CALC_003",
  "sheet": "计算器",
  "priority": "P0",
  "precondition_text": "原始前置条件",
  "steps_text": "原始操作步骤",
  "expected_text": "原始预期结果",
  "setup": [],
  "actions": [],
  "collect": [],
  "verification_points": [],
  "unable": false,
  "note": ""
}
```

`note` 只写映射本身无法从字段看出的短说明，不复制分类、verdict 或账本内容。
