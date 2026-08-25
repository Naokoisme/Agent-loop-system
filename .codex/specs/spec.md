# Spec

## Summary

新增 `/prd-cases` 工作台。后端提供持久化生成任务、PRD 标准化、QA Skill 编排、Excel 回读审查、审查决定、准出与同步服务；前端在同一工作台提供任务列表、审查和同步结果。

## Behavior

1. 用户选择目标项目并上传 PRD。
2. 服务固化输入和 SHA-256，标准化 PRD，并按 QA Skill 生成 Excel 与质量证据。
3. 服务回读 Excel 生成 `review-snapshot.json`，任务进入 `READY_FOR_REVIEW`。
4. 审查人按稳定 ID 留意见；退回后生成新版本，通过后运行最终准出。
5. `APPROVED + GO` 开放同步预览与同步；事务成功后读回并生成同步批次。

## Interfaces

- 页面：`/prd-cases?job=<job_id>`。
- API 前缀：`/api/prd-cases`。
- 关键接口：任务创建/列表/详情、审查快照、审查提交、驳回重生成、同步和 Excel 下载。

## Data And State

- 任务目录：`project_data/prd_cases/jobs/<job_id>/`。
- 生成、审查、同步、QA 准出分别保存状态。
- 审查与同步按稳定 ID 工作，不按展示行号工作。
- 同步来源通过 `source_ref` 保存任务 ID、稳定 ID、审查人、审查时间和工作簿 SHA-256。

## Edge Cases

- 不可信文件、路径穿越、超限 DOCX/PDF、扫描 PDF、空 PRD、模型不可用、Skill 缺失或哈希不匹配。
- 服务重启、Worker 丢失、取消竞争、重复同步、编号冲突、稳定 ID 冲突、同步中途失败。
- Excel 合并单元格造成模块/功能点/测试项空白时必须向下继承。
- Excel 或源 spec 变化时旧审查和 GO 必须失效。

## Non-Goals

- 本次不开放远程监听或实现企业账号认证。
- 本次不同步为自动化可执行映射；新用例保持 `UNMAPPED`。
- 本次不修改共享 QA Skill 规则和知识库。
