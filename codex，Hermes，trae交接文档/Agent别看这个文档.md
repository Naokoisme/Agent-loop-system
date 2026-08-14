
你现在作为一个资深嵌入式 AI 工程系统负责人。

你的任务不是设计一个理想化的未来平台，而是在当前已有系统基础上，提出一个符合：

- DRY（Don't Repeat Yourself）
- KISS（Keep It Simple, Stupid）
- YAGNI（You Aren't Gonna Need It）

原则的最小有效改进方案。

---

# 背景

当前系统目标：

构建 AI 驱动的智能手表固件研发闭环：

Bug输入
 ↓
AI复现
 ↓
AI分析代码
 ↓
AI修改代码
 ↓
重新Build
 ↓
运行验证
 ↓
判断修复结果

当前已经实现：

- Bug自动理解
- Simulator运行
- AI交互复现
- 操作trace保存
- 截图保存
- GUI状态获取
- AI代码修改
- Build验证
- 修复后重新运行验证

当前问题：

虽然已经可以完成部分Bug自动修复，但是：

- 修复成功率不稳定
- 验证可信度不足
- AI容易误判“修复成功”
- 缺少修复案例沉淀

---

# 重要约束

请不要提出以下方案作为第一阶段任务：

❌ 新增大量Agent

例如：

Test Planner Agent
Regression Agent
HIL Agent
Knowledge Agent
Policy Agent

除非证明当前系统无法通过简单修改解决。

❌ 重构整个测试平台

不要建议：

- 新建统一测试平台
- 新建完整测试管理系统
- 新建复杂Schema体系
- 替换现有Runner

❌ 过早工业化

不要优先考虑：

- 大规模HIL
- 机器人测试
- 完整CI Gate
- 企业级测试资产管理

这些可以作为未来路线，但不是当前修改方案。

---

# 第一阶段：审查当前实现

请先阅读当前代码结构。

重点分析：

## 1. 当前Bug修复闭环

回答：

当前流程：

Bug输入
→
复现
→
修改
→
验证

每一步：

- 输入是什么？
- 输出是什么？
- 当前代码在哪里实现？
- 是否已经满足需求？
- 最大缺陷是什么？

输出：

Current Pipeline Review

---

## 2. 找出真正瓶颈

不要罗列所有问题。

只回答：

当前AI修复成功率低，最主要的3个原因是什么？

例如：

- 复现不稳定？
- 验证标准不足？
- AI判断偏差？
- 缺少测试数据？
- 缺少代码上下文？
- 缺少回归？

按照影响排序。

---

# 第二阶段：设计最小修改方案

目标：

不是建设测试系统。

目标：

让当前Bug修复流程：

复现成功
+
修改代码
+
自动证明修复有效

请设计：

## MVP验证闭环

要求：

最多修改：

- 3个核心模块
- 不超过2周开发量
- 尽量复用现有代码

输出：

Before:

Bug
|
AI Repair
|
AI Verify

After:

Bug
|
AI Repair
|
Minimal Verification
|
Result
|
Save Case

---

# 第三阶段：重点分析验证机制

当前验证不要追求完美。

请设计最低成本可靠方案。

回答：

## 修复成功应该如何定义？

不要使用：

“AI觉得成功”

必须定义机器条件。

例如：

- 原始Bug步骤重新执行成功
- 关键GUI状态满足
- 截图差异满足
- 无Crash
- 日志无异常

但是只选择必要条件。

---

# 第四阶段：测试资产如何自然产生

不要设计复杂Test Management System。

请设计：

每修复一个Bug，自动保存：

bug_xxx/

before/
after/

trace.json

expected.json

patch.diff

result.json

作为未来测试资产。

回答：

最低需要保存什么？

哪些以后再增加？

---

# 第五阶段：评估现有架构是否需要Agent拆分

当前不要默认增加Agent。

请逐个判断：

## Repair Agent

是否需要修改？

## Verification Agent

是否真的需要？

## Test Agent

是否真的需要？

## Regression Agent

是否真的需要？

输出：

Agent Decision:

Keep:
Reason:

Merge:
Reason:

Do Later:
Reason:

Do Not Build:
Reason:

---

# 第六阶段：输出实施计划

请给出：

## 第一周

目标：

任务：

代码修改：

验证方式：

产出：

## 第二周

目标：

任务：

代码修改：

验证方式：

产出：

---

# 最终输出格式

# 1. 当前系统真实状态

# 2. 最大三个瓶颈

# 3. 最小修改架构

# 4. 需要修改的代码模块

# 5. 不应该做的事情

# 6. 两周MVP计划

# 7. 未来演进路线（仅作为参考）

请始终遵守：

不要为了架构漂亮增加复杂度。

优先让真实Bug闭环跑通。

优先产生业务价值。

如果一个复杂方案不能显著提高当前修复成功率，请明确拒绝。
