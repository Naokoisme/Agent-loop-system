# 6202 Windows 模拟器当前链路

> 文档角色：reference / dated snapshot
> 链路事实最后核对：2026-08-14
> case map 统计快照：2026-08-17

## 隔离范围

- 源码：`D:\Agent-loop\workspaces\firmware\6202_W5230`
- 构建目录：`D:\Agent-loop\workspaces\firmware\6202_W5230\core\gui\simulator\out\build\6202_W5230`
- 运行产物：`D:\Agent-loop\workspaces\firmware\6202_W5230\core\gui\simulator\bin\main.exe`
- 用例映射：`D:\Agent-loop\system\case_map\6202_simulator_case_map`
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

成熟度和 Runner 入口的唯一规范是 [case map 数据合同](../case_map/README.md)，本页不另造分类。
截至 2026-08-17 的统计快照为：

- 40 个模块、3164 条用例；
- 外部探索账本登记 563 条；
- 其中 465 条已探索但未固化，98 条精确标记为 `mapping_status=PROMOTED`；
- 未 `PROMOTED` 的用例仍可由 Agent-loop 在本轮临时探索，但临时命令不自动写回正式映射。

`mapping_status` 只描述映射成熟度，PASS、FAIL、CANNOT_VERIFY、ERROR 描述某次运行的 verdict；
两者必须分开。产品 FAIL 在路径正确、正式复跑和截图证据完整时也可以晋升。

6202 Simulator 的固化步骤使用 `HOST_SCREENSHOT`，不使用真机的 `SCREENSHOT_CAPTURE`、
`SCREENSHOT_CAPTURE_FILE` 或 `SCREENSHOT_PRINT`。它只采用在本目标验证过的能力，例如
`SIM_ACTIVITY_SET`、`SIM_CONNECTION_SET` 和 `SIM_SOS_CONTACT_SET`，不自动继承 620C 的其他
`SIM_*`。

截图是 PASS、FAIL、CANNOT_VERIFY 的唯一产品判据；命令回执、日志和 GUI 树只用于诊断。

## 已修复的公共问题

1. 6202 的 TP_CLICK/TP_PRESS/TP_RELEASE 曾在右移前把 32 位坐标强转成 16 位，导致所有 x 坐标变成 0。现已改为先右移再转成 16 位。
2. `ENTER_PAGE` 的 processed 回执早于窗口动画完成。Runner 在屏障后统一等待 1 秒，避免后续第一下触控被 LVGL 在屏幕动画期间丢弃。
3. GUI_PING/GUI_STATE/GUI_TREE 使用持续订阅，保证同一模拟器会话中可以反复同步。
4. 旧的 sheet 级 `supported`、`execution_supported`、`unavailable_reason` 以及用例级 `unable`
   不再充当成熟度或运行入口。能力缺口在本轮形成 CANNOT_VERIFY 或 ERROR；是否固化只看精确的
   `mapping_status=PROMOTED`。
5. 活动记录数据用例使用 Windows 专用 `SIM_ACTIVITY_SET` 固定当日步数、消耗和距离，避免
   模拟器随机计步覆盖前置；图表页使用页面自身的编码器分屏导航。需要连续画面而当前证据不足的
   路径不得晋升。
6. `EXERCISE_TIME` 同时建立当日总活动时长与当前半小时槽，并在跨越目标时走产品提醒事件；
   Windows `TIME_SET` 跨日期时调用实际日切处理，清空当日活动值和 48 个半小时槽但保留目标。
   持续 5 分钟、振动、完整运动会话、异常数据源和未确认 Oracle 仍需在具体运行中返回可审计的
   CANNOT_VERIFY 或 ERROR，不能靠旧状态字段提前制造结论。
7. `SLEEP_RECORD_CREATE` 统一为 `0` 默认数据，或 `1,deep,light,rem,awake,nap` 六参数自定义
   数据；6202 解析器、映射审计和合同测试使用同一参数约定。睡眠页面按 502 像素分为总览、
   时间线、阶段详情和小睡列表四屏；只有具有真实分屏导航和完整截图证据的候选才可晋升。
8. `GUI_PING`只能证明旋钮事件已经离开命令队列，不能证明列表滚动动画已经稳定。Runner 对 `QDEC_SET` / `QINC_SET` 统一增加250毫秒稳定等待，防止紧随其后的点击落在旧坐标；这条规则不包含 `case_id` 分支。

## 2026-08-14 已验证证据快照

- `main.exe`：21,508,491 bytes，SHA-256 `ebb941f35bee87ec9ae6d27d85fb937ddd566c7730995db7c40d5d7a731ff003`。
- `str_res.bin`：源码资源与模拟器运行资源 SHA-256 都是 `5143fc003ecfd4e23a23524c2f5bdfdace779bdc4d48257e0bc65c8de5d556e1`。
- `img_res.bin`：源码资源与模拟器运行资源 SHA-256 都是 `0018dd562a4e9c8817c32db4e76e6efca43c552f11cfd3ecee90acc90235c63a`。
- `CALC_001`：截图显示数值 0 与完整五行键盘。
- `CALC_003`：依次点击 1、2、3 后，截图显示 123。
- `SOS_016`：Agent-loop两张截图分别显示“Dial automatically in 8 s”和“Calling out”，截图支持PASS。
- `SOS_023`：通用滚动稳定后已经进入Emergency contact页面，但截图只显示`QA SOS`，没有`12345678901`，因此仍按产品画面判FAIL。
- 当时的前端批次 `81305896a815` 属于项目 `6202_W5230_SIMULATOR`；09:59 在第 628 条后
  暂停，10:10 复用原批次和原游标恢复，未建立第二批。该历史计划使用旧成熟度口径，不代表
  2026-08-17 的 case map 分类。

该批量期间每条用例独立启动并关闭模拟器，结果和截图写入前端测试历史；这是一条历史运行记录，
不是当前批次状态。
