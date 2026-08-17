# Agent 工作边界

说人话。

## 三套源码必须隔离

- 620C 模拟器：`D:\Agent-loop-workspace\620C_W6830`
- 6202 真机：`D:\Agent-loop-workspace\6202_W5230`
- 6204 真机：`D:\Agent-loop-workspace\6204_W5230`
- `D:\TOPSTEP\shenju_w30` 只作上游参考。不要在那里开发、构建或打补丁。
- 真机任务必须同时使用 `W30_HARDWARE_SOURCE_ROOT` 和
  `W30_HARDWARE_WORKSPACE_ROOT`，两者必须指向同一个、与当前项目匹配的隔离工作区。

## 当前已验证的 6202 真机截图方向

- COM7 当前由改造后的 SuperCom 持有；Agent-loop 必须通过命名管道
  `\\.\pipe\SuperCom.AgentBridge.COM7` 双向收发，不得并行直接打开 COM7。
- 对应运行配置为 `W30_HARDWARE_TRANSPORT=supercom`；管道名由 COM7 固定映射得到。
- 当前目标只是让真机测试流程按需取得手表屏幕截图。
- 当前已验证主链路是：后台 UART 控制（DTR/RTS 关闭）→
  `SCREENSHOT_CAPTURE_FILE` → USB/MTP 下载 → BMP 证据。
- Agent-loop 默认 `RealDeviceSession` 已使用 `MtpCaptureProvider`；Provider 借用同一个
  `HardwareSerialSession`，不得重复打开或关闭 COM 口。
- 串口分块截图只保留为诊断和回退链路，不再作为真机测试的默认图片传输方式。
- 不要恢复 PhoneCam 真机链路，也不要引入 ADB；独立截图工具必须脱离具体固件源码目录，
  只依赖调试串口、Windows MTP 和已烧录的工程截图命令。
- 6202 固件截图代码只允许在 6202 隔离工作区开发。
- 未经用户明确授权，不得刷机。

开始真机截图工作前，先读
`docs/6202-hardware-screenshot-current.md`。

## 6204 当前边界

- 6204 源码已迁移到独立工作区，当前快照、版本和兼容性结论见
  `docs/6204-hardware-migration-current.md`。
- 6204 v1.6.2 工作区已完成 HLQ、会话、GUI 栅栏/树和 MTP 文件截图的源码移植，
  但尚未构建、刷入或真机验证；不得把源码存在误报成当前手表已经具备该能力。
- 6204 截图代码只允许在 6204 隔离工作区开发；不得把 6202 的 case map、命令、坐标或
  测试证据直接复用到 6204。
- 在 6204 完成单独授权构建、再次授权刷机和真机最小能力验证前，
  不要把 6204 注册成可执行测试目标。
- 用户确认别人提供且已烧录的 UP3 版本为 1.6.2，与选定标签的版本号一致；原文件哈希
  仍未知，而且该旧 UP3 不包含本次源码移植。
- 开始 6204 截图、构建或运行配置工作前，先读
  `docs/6204-hardware-migration-current.md`。
