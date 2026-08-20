# 6202 Windows 模拟器当前链路

> last_verified: 2026-08-14

## 隔离范围

- 源码：`D:\Agent-loop-workspace\6202_W5230`
- 构建目录：`D:\Agent-loop-workspace\6202_W5230\core\gui\simulator\out\build\6202_W5230`
- 运行产物：`D:\Agent-loop-workspace\6202_W5230\core\gui\simulator\bin\main.exe`
- 用例映射：`D:\Agent-loop-system\case_map\6202_simulator_case_map`
- 前端项目：`6202_W5230_SIMULATOR`（显示为 `6202 W5230 · 模拟器`）

不得把 `D:\TOPSTEP\shenju_w30` 当作开发、构建或补丁目录。

## 构建开关

Windows 模拟器只启用窄范围命令桥：

- `CONFIG_TOPSTEP_COMM_TEST=OFF`
- `CONFIG_TOPSTEP_COMM_QUICK_CMD=ON`

这样不会把 Zephyr 的整套测试代码拉进 Windows 构建，同时保留 Agent-loop 所需的 `srv_quick_cmd` 与 GUI quick-command 模块。

6202 的项目自有命令桥是 `core/comm/srv/test/srv_quick_cmd_handler.c/.h`。CMake 在启用 `CONFIG_TOPSTEP_COMM_TEST` 或 `CONFIG_TOPSTEP_COMM_QUICK_CMD` 时编译这一份实现，protobuf 命令入口和 GUI 命令消费者也使用同一入口。Agent-loop 直接从该文件提取当前命令表；文件缺失时应明确报错，不能回退到历史文件或跨项目缓存。

## Agent-loop 启动条件

前端执行 6202 模拟器项目时会为子进程设置：

- `W30_SOURCE_ROOT` 与 `W30_AGENT_WORKSPACE_ROOT` 指向 6202 隔离源码
- `W30_PROJECT=6202_W5230`
- `SIMULATOR_BUILD_DIRECTORY` 与 `SIMULATOR_ARTIFACT_PATH` 指向上述 6202 构建和产物
- `SIMULATOR_SHELL_READY_MARKER=W30_SIM_SHELL_READY`
- `SIMULATOR_GUI_COMMAND_READY_MARKER=W30_QUICK_CMD_GUI_READY`

启动时必须先观察到 shell 与 GUI 命令桥两个 ready marker，再发送首个 `GUI_PING`。

## 用例数据

- 共 40 个模块、3164 条用例。
- 当前有效可执行 779 条，保留 `unable=true` 2385 条；当前运行中的旧批次在创建时已冻结为 863 条，不回写其计划。
- 779 条可执行用例全部包含 `HOST_SCREENSHOT`。
- 不包含 `SCREENSHOT_CAPTURE`、`SCREENSHOT_CAPTURE_FILE`、`SCREENSHOT_PRINT`；只迁入经过合同测试的通用能力：`SIM_ACTIVITY_SET`、`SIM_CONNECTION_SET` 和 `SIM_SOS_CONTACT_SET`，不自动继承620C的其他 `SIM_*`。

截图是 PASS、FAIL、CANNOT_VERIFY 的唯一产品判据；命令回执、日志和 GUI 树只用于诊断。

## 已修复的公共问题

1. 6202 的 TP_CLICK/TP_PRESS/TP_RELEASE 曾在右移前把 32 位坐标强转成 16 位，导致所有 x 坐标变成 0。现已改为先右移再转成 16 位。
2. `ENTER_PAGE` 的 processed 回执早于窗口动画完成。Runner 在屏障后统一等待 1 秒，避免后续第一下触控被 LVGL 在屏幕动画期间丢弃。
3. GUI_PING/GUI_STATE/GUI_TREE 使用持续订阅，保证同一模拟器会话中可以反复同步。
4. case_map 支持 sheet 级 `supported=false` / `execution_supported=false`：当前项目未启用功能或整个模块尚无已验证入口时，前端、Runner 和静态审计统一把该 sheet 视为 `unable`，清空执行命令，避免 accepted 回执或表盘截图冒充产品页面。
5. 活动记录数据用例统一使用 Windows 专用 `SIM_ACTIVITY_SET` 固定当日步数、消耗和距离，避免模拟器随机计步覆盖前置；图表页统一使用页面自身的编码器分屏导航。只能由连续画面判断的圆环动画用例保持 `unable`。
6. `EXERCISE_TIME` 同时建立当日总活动时长与当前半小时槽，并在跨越目标时走产品提醒事件；Windows `TIME_SET` 跨日期时调用实际日切处理，统一清空当日活动值和48个半小时槽但保留目标。持续5分钟、振动、完整运动会话、异常数据源和未确认Oracle仍保持 `unable`。
7. `SLEEP_RECORD_CREATE` 统一为 `0` 默认数据，或 `1,deep,light,rem,awake,nap` 六参数自定义数据；6202解析器、映射审计和合同测试使用同一参数约定。睡眠页面按502像素分为总览、时间线、阶段详情和小睡列表四屏；只有能由现有数据源和真实分屏导航直接取证的阶段占比用例保持可执行。
8. `GUI_PING`只能证明旋钮事件已经离开命令队列，不能证明列表滚动动画已经稳定。Runner 对 `QDEC_SET` / `QINC_SET` 统一增加250毫秒稳定等待，防止紧随其后的点击落在旧坐标；这条规则不包含 `case_id` 分支。

## 已验证证据

- `main.exe`：21,508,491 bytes，SHA-256 `ebb941f35bee87ec9ae6d27d85fb937ddd566c7730995db7c40d5d7a731ff003`。
- `str_res.bin`：源码资源与模拟器运行资源 SHA-256 都是 `5143fc003ecfd4e23a23524c2f5bdfdace779bdc4d48257e0bc65c8de5d556e1`。
- `img_res.bin`：源码资源与模拟器运行资源 SHA-256 都是 `0018dd562a4e9c8817c32db4e76e6efca43c552f11cfd3ecee90acc90235c63a`。
- `CALC_001`：截图显示数值 0 与完整五行键盘。
- `CALC_003`：依次点击 1、2、3 后，截图显示 123。
- `SOS_016`：Agent-loop两张截图分别显示“Dial automatically in 8 s”和“Calling out”，截图支持PASS。
- `SOS_023`：通用滚动稳定后已经进入Emergency contact页面，但截图只显示`QA SOS`，没有`12345678901`，因此仍按产品画面判FAIL。
- 当前前端批次：`81305896a815`，项目 `6202_W5230_SIMULATOR`，计划执行 863 条未测试用例；09:59在第628条后暂停，10:10复用原批次和原游标恢复，未建立第二批。

批量期间每条用例独立启动并关闭模拟器，结果和截图写入前端测试历史；同一时间不得再启动第二个批次。
