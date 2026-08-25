# Plan

## Approach

- 复用现有本地 HTTP、项目注册表、案例管理仓储、LLM 配置和持久化任务模式。
- 新建独立 `prd_cases` 包，HTTP 层仅做路由适配。
- 将指定 QA Skill ZIP 作为受校验的版本化发布资源；任务工作区始终外置。
- 首先实现确定性的 PRD 解析、Excel 回读、审查和同步闭环；真实 AI 生成通过现有模型配置接入，并保留可测试的确定性生成适配边界。

## Files

- `src/agent_loop_system/prd_cases/*`
- `src/agent_loop_system/case_management/*`
- `frontend/server.py`
- `frontend/index.html`、`frontend/app.js`、`frontend/styles.css`
- `scripts/build_exe.py`、发布资源清单
- `tests/test_prd_cases.py`、`tests/test_frontend.py`、`tests/test_frontend_assets.py`
- `docs/prd-to-testcase-tab-acceptance-report.md`

## Steps

1. 建立模型、仓储、PRD 解析、Skill 资源与任务状态。
2. 实现生成编排、Excel 输出和回读审查快照。
3. 实现审查意见、退回、通过、哈希失效和准出。
4. 扩展用例管理来源字段，实现同步预览、事务、幂等和读回。
5. 增加 HTTP API、SPA 路由、页面和交互。
6. 将 Skill ZIP 纳入开发与便携版资源。
7. 执行测试、真实服务/API/Excel/同步验收和便携版构建验证。
8. 输出验收报告及哈希。

## Verification

- `uv run pytest` 聚焦与全量回归。
- HTTP 服务真实启动后的 API 冒烟。
- 真实 PRD → Excel → 回读快照 → 审查 → GO → 同步 → 数据库读回。
- 前端路由/文案/交互静态测试和浏览器视觉验收。
- PyInstaller 构建、发布清单和解压后启动验证。

## Risks

- QA Skill 原本是 Agent 编排规则而非单一 SDK；适配层必须显式区分 AI 阶段与机械脚本阶段。
- 正式 Skill 门禁需要完整项目合同；测试夹具不能冒充真实正式 GO。
- 大用例集需要分页，不能全量渲染。
- 当前系统 Python 缺项目依赖；验收统一使用 `uv` 环境。

## Rollback

- 新功能使用独立路由、API 前缀、数据目录和资源，不迁移现有数据。
- 如需回退，只移除新导航/API/包和来源字段读取；现有用例与同步批次保留可追溯记录。
