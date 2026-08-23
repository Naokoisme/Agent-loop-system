# 真机工作区自动探测与 NAS 迁移指南

本文档介绍在进行 NAS 迁移、多机部署或本地开发时，如何利用 **工作区自动探测（Auto-Discovery）** 和 **零硬编码路径规范** 轻松管理 6202 / 6204 真机测试环境。

---

## 1. 设计原理

系统遵循现代多工程架构的最佳实践（如 Rust Cargo、Bazel、Zephyr RTOS）：
1. **约定优于配置**：默认以 `system` 同级布局中的 `../workspaces/firmware/{PROJECT}` 作为隔离源码工作区。
2. **特征指纹核验**：自动校验目标目录下的 `app/ProjectConfig.cmake` 中的 `PROJECT` 名称，确保不发生项目混淆。
3. **单一意图源**：支持通过单一配置项指定路径，无需重复配置 `SOURCE_ROOT` 和 `WORKSPACE_ROOT`。
4. **安全防呆黑名单**：严格禁止直接指向 `shenju_w30`（上游参考库），避免破坏主线。

---

## 2. 目录组织规范（推荐）

在 NAS 共享盘或开发机本地，推荐采用以下标准同级目录结构：

```text
/your-storage-or-nas/Agent-loop/
├── system/                     # 本自动化测试与 Agent 系统仓
└── workspaces/
    └── firmware/               # 隔离固件工作区父目录
        ├── 620C_W6830/         # 620C PC 模拟器工作区
        ├── 6202_W5230/         # 6202 真机隔离工作区
        └── 6204_W5230/         # 6204 真机隔离工作区
```

---

## 3. 配置方式（从简到繁）

### 模式 A：零路径配置（完全自动探测，推荐）
如果目录结构符合上述同级规范，在 `.env` 中**无需配置任何路径**，系统会根据项目名自动定位：
```ini
W30_HARDWARE_PROJECT=6202_W5230
W30_HARDWARE_TRANSPORT=supercom
W30_HARDWARE_PORT=COM7
W30_HARDWARE_CAPTURE_PROVIDER=mtp
```

### 模式 B：相对路径配置（适用于异构挂载或非标准层级）
支持在 `.env` 中使用单一相对路径变量：
```ini
W30_HARDWARE_WORKSPACE=../workspaces/firmware/6202_W5230
W30_HARDWARE_PROJECT=6202_W5230
W30_HARDWARE_TRANSPORT=supercom
W30_HARDWARE_PORT=COM7
```

### 模式 C：全局工作区基准目录（适用于统一 NAS 存储）
如果所有的固件工程都统一存放在 NAS 某个特定共享路径：
```ini
AGENT_LOOP_WORKSPACE_BASE=\\NAS-SERVER\workspaces
W30_HARDWARE_PROJECT=6202_W5230
```
系统会自动解析到 `\\NAS-SERVER\workspaces\6202_W5230`。

### 模式 D：传统显式绝对路径（向后兼容）
```ini
W30_HARDWARE_WORKSPACE_ROOT=../workspaces/firmware/6202_W5230
# W30_HARDWARE_SOURCE_ROOT 可省略，系统自动互通
```

---

## 4. 常见问题与排错

### Q1: `HARDWARE_WORKSPACE_NOT_FOUND`
- **原因**：未显式配置路径，且在 `../workspaces/firmware/{PROJECT}` 中未找到目标项目。
- **解决**：
  1. 确认目录命名是否与 `W30_HARDWARE_PROJECT` 完全一致。
  2. 或者在 `.env` 中显式指定 `W30_HARDWARE_WORKSPACE=<真实路径>`。

### Q2: `HARDWARE_SOURCE_CONFLICT`
- **原因**：目标目录下的 `app/ProjectConfig.cmake` 中定义的 `PROJECT` 与当前配置的 `W30_HARDWARE_PROJECT` 不符（例如把 6204 的目录配给了 6202 任务）。
- **解决**：检查目标固件目录的 `app/ProjectConfig.cmake` 配置。

### Q3: `HARDWARE_WORKSPACE_FORBIDDEN`
- **原因**：路径指向了 `shenju_w30` 上游公共参考库。
- **解决**：系统严禁将上游只读参考库作为真机工作区，请迁移或克隆至独立的隔离工作区（如 `workspaces/firmware/6202_W5230`）中执行。
