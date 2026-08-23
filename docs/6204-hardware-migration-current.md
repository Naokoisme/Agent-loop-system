# 6204 真机源码迁移现状

> 记录时间：2026-08-17（Asia/Shanghai）
> 状态：`PROTOCOL_PORTED / BUILD_NOT_RUN / RUNNER_NOT_ENABLED`
> 构建：未执行
> 刷机与真机操作：未执行

## 结论

6204 v1.6.2 已迁移到独立开发目录 `D:\Agent-loop\workspaces\firmware\6204_W5230`，并在这套
6204 自己的源码上完成 Agent-loop 通用真机协议移植：HLQ 唯一命令入口、
`TEST_SESSION`、`GUI_PING`、`GUI_TREE` 和 `SCREENSHOT_CAPTURE_FILE` 均已接入。

这不是把 6202 的整套固件覆盖到 6204。HLQ 以 6204 原有命令表为底稿，因此保留了
6204 v1.6.2 的 111 条已注册命令；只迁入通用的会话、GUI 观察和截图机制。6202 的
case map、坐标、业务命令和历史证据没有复制到 6204。

当前手表中由他人提供并已烧录的 UP3，用户确认版本号为 1.6.2。版本标签与本工作区选择的
`6204_W5230_v1.6.2` 一致，但现有 UP3 是移植前的成品，尚不包含本次源码修改；由于没有
原 UP3 文件和 SHA-256，也不能把该二进制与本工作区提交做逐字节对应。

## 源码快照

| 项目 | 当前值 |
| --- | --- |
| 上游参考目录 | `D:\TOPSTEP\shenju_w30`（只读使用） |
| 独立工作区 | `D:\Agent-loop\workspaces\firmware\6204_W5230` |
| 选定发布标签 | `6204_W5230_v1.6.2` |
| 标签对象 | `6960fb0245ccb0c734833b45563d11a9ced58d46` |
| 根仓提交 | `14b6116a55897be9ff8afb293002bb3dbeecb4ca` |
| `app` | `f6196b4206d1e335aeffea2e54f1c2434806a66b` |
| `core/comm` | `ba29c2ac423b731a0bcee12b145b46172a2310a5` |
| `core/gui` | `2f7d0b2a040926a4be13ae95f9275ba44e7e00bb` |
| `core/lvgl` | `1d764219b15e0dc2e422b98840fed592af47d201` |
| `core/platform_driver` | 此标签的根树中没有该子模块路径 |

根提交同时可能被其他产品标签指向，因此不能用 `git describe` 的自动结果判断产品。
本快照以显式选定的标签、`app/ProjectConfig.cmake` 中的 `set(PROJECT 6204_W5230)`
以及 6204 项目目录三者共同确定身份。

## 活动项目与依赖

`app/ProjectConfig.cmake` 从同一 `app` 提交中的 `ProjectConfig_template.cmake` 原样生成：

| 文件 | 长度 | SHA-256 |
| --- | ---: | --- |
| `app/ProjectConfig_template.cmake` | 992 | `CF56EA1B13E46D88B4F71218CBFD896B55BD2DB18A8921EB5CEB9F104F9D9A7F` |
| `app/ProjectConfig.cmake` | 992 | `CF56EA1B13E46D88B4F71218CBFD896B55BD2DB18A8921EB5CEB9F104F9D9A7F` |
| `app/projects/6204_W5230/Project.cmake` | 3,952 | `671B63B767FD601F389D56E4C87F9CEDF723B9522961480356D0BF444E41EFAE` |

加密/LFS 配置修正后，当前 `core/gui` 提交列出的 21 个 LFS 文件均已物化。
关键文件 `core/gui/lib/imageblur/libimageblur.a` 长度为 30,552 bytes，SHA-256 为
`CE71730A4A619AADCD21300F0A1A41463EC76A4E601050CB01F3F839128ABB67`。
没有用 6202、620C 或其他提交中的库替换它。

## 已迁移能力

### 1. HLQ 唯一命令入口

- 新增 `core/comm/srv/test/hlq_quick_cmd_handler.c/.h`。
- 文件以 6204 自己的 `srv_quick_cmd_handler.c/.h` 为基线，不从 6202 复制整张命令表。
- `srv/test/CMakeLists.txt` 永久排除旧 `srv_quick_cmd_handler.c`，避免两个命令表同时链接。
- protobuf 快捷命令入口和 GUI 消费者只引用 HLQ；旧文件只保留为源码历史参考。
- 命令解析补齐终止分号检查、空指针保护和统一 JSON 接收/拒绝回执。

### 2. `TEST_SESSION`

- 支持 `START`、`STATUS`、`STOP`。
- 会话租期为 86,400 秒；到期会自动释放常亮与运行锁并输出 `expired`。
- 生命周期仍由用户或批次外部管理，Agent 不会在单条用例结束时自动发送 `STOP`。
- 启动后通过 `dal_sys_screen_on_test(true)` 和 GUI 生命周期保护阻止测试中途灭屏。

### 3. `GUI_PING` 与 `GUI_TREE`

- `GUI_PING:<seq>` 进入真实 GUI 消息队列后才输出同序号 `gui_ack`，用作队列栅栏。
- `GUI_TREE:<seq>` 只作诊断，不作截图证据；输出 begin、逐节点记录和 end。
- 树遍历最多 512 个节点、32 层，文本按 UTF-8 边界截断，避免无界串口输出。
- 屏幕关闭、转场中或没有活动窗口时返回明确状态，不伪造树结果。

### 4. `SCREENSHOT_CAPTURE_FILE`

- 命令参数使用 uint32 序号，生成唯一文件 `download/agent_capture_<seq>.bmp`。
- 截图源是 VDE 最终 LCD 合成结果，像素格式为 BGR888，不是单个 LVGL 图层。
- BMP 为 24 位、顶向下存储；写入临时文件并同步后原子改名，旧截图会先清理。
- 6204 项目屏幕尺寸由源码确认为 466×466；对应 BMP 文件大小应为 652,454 bytes。
- 文件完成后标记 MTP 存储索引失效，USB 再次打开时重建对象列表并识别 BMP 格式。
- 截图时要求 USB 已关闭；完成回执携带序号、尺寸、文件大小、像素 CRC 和耗时。
- 串口分块实现只保留为诊断回退，Agent-loop 默认仍走 MTP 文件链路。

## Agent-loop 侧接入

- `HardwareTargetConfig` 现在显式登记 `6202_W5230` 与 `6204_W5230`，不回退到旧 handler。
- 6202 GUI 命令源仍是 `app/comm/TuoBu/quick_cmd/gui_comm_quick_cmd.c`；6204 使用自己的
  `app/comm/quick_cmd/gui_comm_quick_cmd.c`。
- MTP Provider 根据 `W30_HARDWARE_PROJECT` 严格选择几何尺寸：6202 为 410×502，6204 为
  466×466；序号、新鲜度、BMP 布局、文件大小和 CRC 校验均保留。
- 当前 `.env` 仍指向 6202，这是有意保留的安全门。6204 未构建、未刷入、未验证前，
  不把现有 Runner 切到 6204，也不在前端创建一个看似可运行的 6204 项目。

## 离线验证

- 当前 HLQ 源码提取到 111 条可用命令。
- `GUI_PING`、`GUI_TREE`、`TEST_SESSION`、`SCREENSHOT_CAPTURE_FILE` 均被源码提取器识别为可用。
- `tests/test_hardware_target.py tests/test_graph.py tests/test_main.py`：33 passed，14 subtests passed。
- `tests/test_mtp_screenshot.py tests/test_real_device.py`：29 passed，6 subtests passed。
- 固件根仓、`app` 和 `core/comm` 的差异检查未发现空白错误。
- 已核对命令注册、事件消费、订阅/反订阅、VDE 捕获 API、MTP 刷新 API 和 BMP 支持均成对存在。

这些都是源码和主机侧离线验证，不等于固件编译成功，更不等于真机截图已通过。

## 下一道验收门槛

1. 用户单独授权后，在 `D:\Agent-loop\workspaces\firmware\6204_W5230` 构建 6204，并记录工具链、
   根仓/子仓状态、配置、新 UP3 路径与 SHA-256。
2. 构建成功后仍需用户再次明确授权才能刷机；构建许可不包含刷机许可。
3. 刷入新产物后，通过 SuperCom 命名管道验证 HLQ、会话和 GUI 栅栏，再用 MTP 取得一张
   466×466、652,454-byte、序号与 CRC 都匹配的新 BMP。
4. 最小能力验证通过后，才把 `.env` 的真机源码/工作区/项目三项一起切到 6204，并登记
   6204 Runner 目标。
5. 6204 的 case map 必须按当前源码与真机页面逐条探索，不能复制 6202 的命令、坐标或证据。

## 本轮明确未执行

- 未修改 `D:\TOPSTEP\shenju_w30` 的源码、提交、分支或工作区状态。
- 未运行 6204 固件构建，未生成新的 UP3。
- 未打开 COM7、未使用 SuperCom 管道、未进行 MTP 真机截图。
- 未连接、复位、清除或刷写手表。
- 未切换当前 `.env` 的 6202 真机配置。
- 未创建 6204 case map，未复用任何 6202 测试证据。
