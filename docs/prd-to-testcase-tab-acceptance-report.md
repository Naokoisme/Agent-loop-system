# PRD 转用例功能验收报告

## 1. 验收结论

**结论：通过，可交付。**

本次已完成本机核心闭环：

```text
上传 PRD
  -> 校验固定版本 QA Skill
  -> 解析 PRD
  -> 模型按 Skill 规则生成结构化用例
  -> 输出并重新打开 Excel
  -> 质量门禁与可视化人工审查
  -> 驳回重生成 / 审查通过
  -> 审批与 Excel SHA-256 绑定
  -> 直接同步用例管理并保留来源
```

最终发布包已构建并完成 EXE 实际启动验证。新用例同步后保持 `UNMAPPED`，不会被误标为可自动执行。

## 2. 验收范围

- 一级导航新增“PRD 转用例”，位于“用例管理”前。
- 支持上传 `.md`、`.txt`、`.docx`，单文件最大 20 MB。
- 指定 QA Skill ZIP 作为只读发布资源，启动生成前强制校验 SHA-256 和关键入口。
- 复用平台模型配置，按固定层级生成 Excel：`功能模块 > 功能点 > 测试项 > 测试点 > 用例详情`。
- 页面展示从最终 Excel 回读的全部用例，而非内存草稿。
- 支持搜索、展开步骤与预期、逐条意见、整体意见、驳回重生成、审查通过和 Excel 下载。
- 只有 `APPROVED + GO` 且 Excel 哈希未变化时允许同步。
- 同步具备事务、稳定 ID 冲突保护、幂等返回、来源审计和写入后读回。
- Windows x64 便携版包含 QA Skill，无需外部 D 盘 ZIP。

## 3. 需求验收矩阵

| 编号 | 验收项 | 结果 | 证据 |
|---|---|---|---|
| AC-01 | “PRD 转用例”位于“用例管理”前 | 通过 | 浏览器 DOM 顺序：项目总览、PRD 转用例、用例管理；桌面端无页面横向溢出 |
| AC-02 | PRD 上传与输入校验 | 通过 | Base64、扩展名、20 MB、空内容、短内容和 DOCX 结构均有失败关闭校验 |
| AC-03 | 使用指定 QA Skill | 通过 | 开发包与发布包 Skill SHA-256 均为 `FFDF484A2F24F30A7A9CAD757F8545F4ECEC2F990B5DE9768A6BB9EA93DB6DBA` |
| AC-04 | 真实模型可完成转换 | 通过 | 合成“倒计时 PRD”真实调用生成 10 条用例，状态 `READY_FOR_REVIEW`，质量门禁通过 |
| AC-05 | 生成 Excel 并从 Excel 回读 | 通过 | `generated-test-cases.xlsx` 与 `review-snapshot.json` 绑定同一 workbook SHA-256 |
| AC-06 | 可视化展示完整用例 | 通过 | 浏览器显示编号、模块/功能点、测试项/测试点、详情、需求 ID 和逐条审查意见 |
| AC-07 | 驳回与重新生成 | 通过 | 驳回必须填写整体或逐条原因；重生成把意见传回生成器并产生新 Excel/快照 |
| AC-08 | 审查通过门禁 | 通过 | 审查人必填；审批绑定 workbook SHA-256；外部修改 Excel 后旧审批被拒绝 |
| AC-09 | 审批后同步用例管理 | 通过 | API 闭环新增用例并读回 `source_type=PRD_APPROVED`、功能点、job_id 和 workbook SHA-256 |
| AC-10 | 冲突与重复同步 | 通过 | 已有人工用例不被覆盖；页面显示“部分同步”；同任务重复同步返回原结果 |
| AC-11 | 原功能回归 | 通过 | `825 passed, 17 skipped, 9812 subtests passed` |
| AC-12 | Windows 便携版 | 通过 | PyInstaller 发布审计通过；最终 EXE 启动后 `/prd-cases` 为 HTTP 200，读取到 5 个项目 |

## 4. 自动化与真实运行结果

### 4.1 完整回归

```text
命令：.\.venv\Scripts\python.exe -m pytest -q
结果：825 passed, 17 skipped, 9812 subtests passed in 50.43s
```

覆盖的新增关键场景：

- Skill ZIP 篡改时失败关闭。
- PRD 生成、Excel 输出、Excel 回读和稳定 ID。
- 驳回理由必填和按意见重生成。
- Excel 改动使旧审查失效。
- 未审批禁止同步。
- 审批后同步、来源读回和重复同步幂等。
- 人工用例编号冲突时不覆盖并标记部分同步。
- HTTP 上传、查询、审查、同步和 SPA 路由。
- 导航、静态资源版本和默认页面内容回归。

### 4.2 真实模型生成

使用当前平台模型配置，对不含真实业务数据的合成倒计时 PRD 进行调用：

```json
{
  "status": "READY_FOR_REVIEW",
  "case_count": 10,
  "gate_passed": true,
  "skill_sha256": "FFDF484A2F24F30A7A9CAD757F8545F4ECEC2F990B5DE9768A6BB9EA93DB6DBA",
  "error_code": ""
}
```

### 4.3 浏览器可视化验收

- 页面标题：`PRD 转用例 · Agent-loop`。
- 导航下标：PRD 转用例为 1，用例管理为 2。
- 1440×900 桌面视口：主布局为 `300px + 1043px`，页面无横向溢出。
- 审查样例：Excel 回读 3 条；质量门禁的措辞、分类、顺序、功能边界、稳定 ID 均为 PASS。
- 页面存在逐条意见、审查人、整体意见、驳回修改、审查通过和下载 Excel 操作。

### 4.4 便携版启动验收

构建中首次实启发现并修复 `platform_data` 未被 PyInstaller 收集的问题。最终包重新构建后：

```text
W30 Agent UI: http://127.0.0.1:8877
GET /prd-cases            -> 200
GET /assets/app.js        -> 200
GET /api/projects         -> 200，项目数 5
导航包含 /prd-cases       -> True
前端路由包含 prd-cases    -> True
```

发布清单共记录 463 个文件；未发现 `_internal` 外暴露的 `.py` 文件。

## 5. 交付物与哈希

### 5.1 便携发布包

- 文件：`dist/Agent-loop-system-0.4.6-windows-x64.zip`
- 大小：67,529,499 bytes
- SHA-256：`6A8B4CA1B0568448F48AE2BD76D6C0EE04670618D43ECE5B056FBA148AA2688A`

### 5.2 可执行文件

- 文件：`dist/agent-loop-windows-x64/Agent-loop.exe`
- 大小：20,490,175 bytes
- SHA-256：`2286110169C7BA2BB63481328867F2EF547851E852B9DE1D40158758B97D9B98`

### 5.3 发布清单

- 文件：`dist/agent-loop-windows-x64/release_manifest.json`
- SHA-256：`761D93DEFD2286410709A9F593FE45EB14D2824FBD6819567F03534E7B02CCF3`

### 5.4 内置 QA Skill

- 文件：`dist/agent-loop-windows-x64/resources/skills/xiaozhou-portable-skill-execution-quality-20260825.zip`
- 大小：1,029,185 bytes
- SHA-256：`FFDF484A2F24F30A7A9CAD757F8545F4ECEC2F990B5DE9768A6BB9EA93DB6DBA`

## 6. 安全与数据边界

- HTTP 服务继续只允许 loopback，不开放远程审查入口。
- 发布包未内置开发机 `.env`、API Key 或本机路径；使用前按原平台方式配置模型服务。
- Skill ZIP 只读使用，所有 PRD、Excel、快照和审计日志写入 `project_data/prd_cases/jobs/`。
- 审查和意见按稳定 ID 绑定，不按可变化的展示行号绑定。
- Excel 单元格对公式前缀做文本转义。
- 同步不会覆盖现有手工来源或已有人工作业覆盖的用例。

## 7. 已知边界

- 首版支持 `.md`、`.txt`、`.docx`；PDF、扫描件和图片型 PRD 尚未开放。
- 当前为本机审查，审查人采用姓名/工号留痕，不具备企业账号认证；如需跨电脑审查必须先补登录和权限。
- 当前按 QA Skill 核心规则生成并以 `GO + READY_WITH_RISKS` 准出，不宣称 `STRICT_GO`。
- 生成任务状态已持久化，但运行中的模型请求不做跨进程断点续跑；服务异常退出后应由用户重新生成。
- 同步到用例管理不等于自动化绑定完成，新用例保持 `UNMAPPED`。

以上边界不影响本次“上传 PRD → 生成 Excel → 可视化审查 → 审批后同步用例管理”的本机核心闭环。
