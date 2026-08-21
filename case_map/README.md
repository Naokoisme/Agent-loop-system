# case_map 数据合同

更新时间：2026-08-21

## 一句话规则

3164 条用例全部可以从前端交给 Runner。`PROMOTED` 用例按固化步骤执行；没有固化步骤的用例由 Agent-loop 在本次运行中临时探索。普通运行只写历史；只有用户显式点击“生成候选、复跑并晋升”，系统才可从完整探索结果暂存候选，并在独立候选复跑通过后写入正式步骤。任何站内流程都不得伪造外部探索账本。

## 目录与数据源

| profile | 目录 | 用例定义 | 外部探索事实 |
|---|---|---|---|
| `620C_W6830` | `620C_simulator_case_map` | 目录内模块 JSON | 同目录 `external_execution_history.jsonl` |
| `6202_W5230` | `6202_case_map` | 目录内模块 JSON | 同目录 `external_execution_history.jsonl` |
| `6202_W5230_SIMULATOR` | `6202_simulator_case_map` | 目录内模块 JSON | 同目录 `external_execution_history.jsonl` |

三套目录当前都包含 40 个模块 JSON 和一份外部探索账本。实时分类数量由前端或审计程序直接读取这两个数据源计算；本规范不复制会随探索进度变化的数字。目标之间不得复制命令、坐标、页面、证据或结论，也不得在缺文件时回退到另一套目录。

## 外部探索与固化是两个独立事实源

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
- 正式 `result.json` 使用 `schema_version: 3`；其中 `provenance` 必须精确包含
  `target`、`case_map_profile`、`project`、`artifact_path`、`artifact_sha256` 五个字段
- Simulator 的 `artifact_path` 必须是预检锁定的非空绝对路径，`artifact_sha256` 必须是对应的
  64 位大写 SHA256；Hardware 的两个 artifact 字段必须为空字符串
- 每个视觉检查点有一张独立新截图，证据合同完整
- 最终截图结论为 PASS 或明确的产品 FAIL

产品 FAIL 可以固化正确路径；CANNOT_VERIFY、ERROR、缺图、步骤中断或候选已变化不能固化。

未固化条目长期状态必须是：四组执行字段为空、没有 `mapping_status`。`unable` 是旧字段，不再参与分类或运行入口；新流程不得用它表达“未探索”或“未固化”。

模块 JSON 顶层也不得再使用 `supported`、`execution_supported` 或 `unavailable_reason` 充当入口闸门。能力缺口由 Agent-loop 本次运行形成 CANNOT_VERIFY 或 ERROR，不产生第六种用例成熟度。

## 分类如何计算

```text
全部用例
├─ 已固化、Agent-loop 可执行：mapping_status=PROMOTED
├─ 已探索但未固化：未 PROMOTED，且 case_id 在外部探索账本
└─ 尚未探索：未 PROMOTED，且 case_id 不在外部探索账本
```

`PROMOTED` 的来源可以是外部 Agent 的正式候选复跑，也可以是用户显式批准的 Agent-loop 自主探索候选复跑。后一种来源不写 `external_execution_history.jsonl`，因此 `PROMOTED` 不再要求是外部账本的子集。

运行结果是另一条独立轴：未运行、PASS、FAIL、CANNOT_VERIFY、ERROR。它来自 `history/tests` 的最近一次 Agent-loop 运行，不参与上面的成熟度分类。

前端列表每条用例只显示一个主状态：尚未外部探索显示“尚未探索”，已探索但未固化显示“未固化”；只有已固化用例才显示最近一次 Agent-loop 结果，没有历史时显示“未运行”。

## Runner 选择规则

普通前端、单条和批量 Runner 按以下顺序选择执行方式：

1. `mapping_status=PROMOTED`：执行固化步骤。
2. 其余情况：由 Agent-loop 根据 `precondition_text`、`steps_text`、`expected_text` 临时探索；即使 JSON 正处于候选复跑窗口，普通运行也不把候选当成固化步骤。

只有正式准入复跑才可以显式传 `--candidate-replay`，让 Runner 执行尚未晋升的临时候选 `actions`。入口有两种：外部 Agent 的正式流程，或用户在前端对一轮完整自主探索显式点击“生成候选、复跑并晋升”。普通单条运行和批量运行仍不传该参数。复跑未形成 PASS/FAIL、证据不完整、执行错误、任务中断或候选发生变化时，必须立即恢复写候选前的当前 case 字段；不得把候选长期留在 JSON。

## Agent-loop 自主探索的显式晋升流程

自主探索可以算作候选来源，但不能把同一轮探索结果直接改成 `PROMOTED`：

1. 最新历史必须是 `execution_mode=agent_exploration`，结论为 PASS 或明确产品 FAIL，动作 trace、检查点和截图证据合同完整。
2. 用户显式点击晋升后，前端服务从成功 action trace 和截图顺序生成临时候选；不修改原始人工语义，也不写外部账本。
3. 服务启动独立任务，强制传 `--candidate-replay`，用新证据重新执行暂存的候选。
4. 原始 Runner 结果必须是 schema 3、`execution_mode=candidate_mapping`，目标 provenance 正确，计划与 trace 精确一致，至少有一个成功业务动作，截图与检查点一一对应。
5. 审计通过才新增精确的 `mapping_status=PROMOTED`。任一门禁失败自动回滚；服务重启会回收仍处于排队、运行或审计中的临时候选。

产品 verdict 与映射资格保持独立：可重复执行并完整证明产品 FAIL 的路径可以固化；CANNOT_VERIFY、ERROR 或基础设施中断不能固化。

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
8. 最后核对 JSON 可解析、账本 case_id 唯一、截图与检查点一一对应；外部流程还要确认当前 case 在目标账本中有终态记录。

外部 Agent 只写自己锁定的目标和 case_id，不改另一套 case_map，不把 Agent-loop 自身运行历史反填成“外部探索”，也不修改人工预期来制造 PASS。

## 用例 JSON 保留字段

`6202_case_map` 和 `6202_simulator_case_map` 的模块文件使用目标绑定的顶层对象：

```json
{
  "profile": "6202_W5230_SIMULATOR",
  "sheet": "计算器",
  "cases": []
}
```

`profile` 必须与目录目标一致，`sheet` 必须等于文件名，且每个 `cases[*].sheet` 也必须相同。
`620C_simulator_case_map` 现有文件保留历史顶层数组格式，但每条 `sheet` 仍必须等于文件名；不为统一外形批量重写 3164 条数据。

单条用例保留以下业务字段：

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
  "note": ""
}
```

旧文件中已经存在的 `unable` 只兼容读取；新建或回写条目不得新增、设置或依赖它。`note` 只写
映射本身无法从字段看出的短说明，不复制分类、verdict 或账本内容。
