# 6202 真机截图当前状态

更新时间：2026-08-17（Asia/Shanghai）

## 结论

6202 debug 固件已经支持两种按需截图：串口分块回传，以及截图落盘后通过 USB/MTP 下载。当前主链路改为 MTP：真机已经完成“后台关闭 USB → 生成截图文件 → 重开 USB → Windows MTP 下载”的闭环，得到可正常显示的 410×502 BMP。串口分块链路保留为诊断回退；PhoneCam 和 ADB 都不再属于当前真机截图方案。

已验证的手工过程已经收成独立命令行工具。带序号文件方案已经刷入真机并通过验证：固件生成 `agent_capture_<seq>.bmp`，主机只下载本次请求的精确文件名，不存在的序号不会回退旧图。`MtpCaptureProvider` 已成为 Agent-loop `RealDeviceSession` 的默认截图源，并通过独立真机 Provider 验证。

当前 COM7 由改造后的 SuperCom 持有，Agent-loop 通过命名管道 `\\.\pipe\SuperCom.AgentBridge.COM7` 双向收发，不再直接打开 COM7。亮屏并用主键显式唤醒后，`GUI_PING`、页面命令、GUI 回执和 MTP 截图已完整恢复；`CALC_001` 已在保留外部测试模式的条件下端到端 PASS。低功耗后的自动恢复仍是长时间无人值守自动化的独立阻塞项。

## 隔离边界

- 6202 开发与构建：`D:\Agent-loop-workspace\6202_W5230`
- Agent-loop：`D:\Agent-loop-system`
- 上游参考：`D:\TOPSTEP\shenju_w30`，本次未修改、未构建
- 当前真机已刷入带序号 debug 固件；所有改动均未提交、未推送

## BLE 独立截图 POC（真机门未通过）

### 2026-08-17 状态校正

亿赛通加密问题已经解除，原 `.txt` 草案已恢复为正式 C 源文件。固件端、Agent-loop
组装器和 ACK 写队列已经同时升级为 v2 停等协议并完成 debug 构建；用户已经把本次
`w30.up3` 刷入手表。该包大小为 37,519,360 bytes，SHA256 为
`3F6F028C70AD208B51498204810E4679EDF20ACF3E5AF8F212B3A215FB6C623C`。最终 ELF 包含
`handle_cmd_0x7F01`，BLE 截图线程优先级为 6、栈为 2048 bytes。MTP 仍是默认截图源，
BLE 只有通过一张真实 BMP 验收后才能临时切为 Provider。

当前 v2 协议如下：

- PC 通过 FF02 写入工程命令 `0x7F01`，固件通过 FF03 Notify 返回 START、DATA、END。
- ACK 类型为 5，8-byte 载荷为 `<version:u8, type:u8, sequence:u32, next_chunk:u16>`。
- START 等待 ACK 0；DATA i 等待 ACK i+1；END 等待 ACK `0xFFFF`。
- 每帧首次发送后最多重试 3 次，每次等待 ACK 1 秒；只有收到正确 ACK 才提交偏移、CRC
  和分块序号。
- DATA 固定为 960 bytes。标准 618,518-byte BMP 应有 645 个 DATA 分块。
- PC 只有在序号、分块、长度、CRC32、BMP 头和 410×502 top-down 24-bit 像素格式全部通过
  后才原子落盘；失败不留下半成品。

### BLE 广播状态与恢复命令

本次排查确认当前 debug 包已经编入下列 Zephyr Shell 命令。它们是裸 Shell 命令，不是
`srv_quick_cmd send` 包装命令；必须通过 SuperCom 已持有的 COM7 命名管道发送，不得另外
直接打开 COM7：

```text
btm disp
btm le adv_start
btm le adv_stop
```

`btm disp` 是只读状态命令，重点看三项：

- `bthost_status:1`：蓝牙栈已经打开。
- `le adv state:1`：手表正在 BLE 广播；0 表示没有广播。
- `bt manager le connect dev number:N`：当前 BLE 中心设备连接数；大于 0 时通常是手机或
  PC 已占用连接。

安全的自动化预检顺序：

1. 先通过 SuperCom 管道执行 `btm disp`。
2. 如果 BLE 连接数大于 0，不发送 `adv_start`；先识别并断开占用者，尤其是手机 App。
3. 只有在 `bthost_status=1`、BLE 连接数为 0、`le adv state=0` 时，才执行
   `btm le adv_start`，随后再次执行 `btm disp` 验证状态变成 1。
4. 如果 `bthost_status` 不是 1，自动化应返回明确的“蓝牙栈未就绪”，不要盲目执行
   `btm open` 或重启蓝牙栈。
5. `btm le adv_stop` 只保留给明确的工程诊断，不进入正常截图流程。

2026-08-17 19:37 的真实 `btm disp` 结果：本机地址
`54:C8:D4:D9:29:06`，BLE 名称 `oraimo Watch Tank N_2906`，`bthost_status=1`，
`le adv state=1`，BLE 连接数为 0，广播间隔约 960–1280 ms。诊断证据位于
`D:\Agent-loop-evidence\6202_ble_link_diagnostics\20260817-193710\serial`。

### 手机抢占与扫描窗口

手机连接手表时，PC 扫描不到手表属于预期的单中心连接表现，不能误判为固件 BLE 已关闭。
仅在手机 App 中点“断开”也可能被后台自动重连；测试前应完全关闭手机蓝牙或确保 App 不再
占用。手表从旧地址 `42:74:DC:C8:0A:02` 变为当前地址 `54:C8:D4:D9:29:06`，说明 Agent-loop
不能永久相信 `.env` 中的旧地址。每次真机 BLE 测试应先从 `btm disp` 读取本机地址和名称，
再进行扫描。由于广播间隔接近 1 秒且 Windows 扫描存在偶发漏报，单次未发现不能直接断言
蓝牙关闭；应把“固件广播状态”和“PC 本轮是否扫到”作为两个独立状态记录。

### 2026-08-17 单图真机结果与根因修复

手机解除占用后，PC 扫描到 `54:C8:D4:D9:29:06`，建立 GATT 会话并发出一条截图请求，
请求序号为 `514442546`。客户端等待 180 秒后返回 `SCREENSHOT_TIMEOUT`，没有组装出完整
BMP，也没有留下目标文件。测试后再次扫描仍能看到同一手表，RSSI 为 -63 dBm；随后
`btm disp` 证明蓝牙栈和广播仍然正常、BLE 连接数为 0。没有观察到重启迹象，但本次没有从
请求开始同步保存 UART 启动日志，因此不把“绝对未重启”写成已证明事实。失败范围是“截图
请求已发出之后、完整 BMP 形成之前”，不能再归类为扫描或连接失败。

随后取得的 SuperCom 日志已经把失败点收窄到 BLE 发送切片：START 已发送成功并收到 ACK 0；
第一块 DATA 的 984-byte App 帧被原发送器拆为 495 和 489 bytes 两次通知。加上每次 4-byte
L2CAP 与 3-byte ATT 头后，C400 实际收到 502 和 496-byte HCI ACL Data 包，并连续打印
`llm acl data tx Out of max buffer size`。C400 控制器库中的硬检查上限是 251 bytes，因此
DATA 0 从未到达 PC，PC 不可能返回 ACK 1，固件最终打印 `ack timeout: ... next=1`。

第一性原理上的根因不在截图 DATA 大小，也不是 ATT MTU 498 本身非法。标准 BLE Host 应把较大
L2CAP PDU 按控制器的 HCI ACL 缓冲能力拆成 START/CONT 分片；当前预编译的 C400 Host/L2CAP
路径没有在 251-byte 边界正确分片，却把整个 502/496-byte PDU 交给控制器。该长期修复需要供应商
修正 Host/HCI 的 ACL 上限记录、fallback 和分片逻辑，当前可维护源码无法安全修改这部分 `.a` 库。

本次采用的最小兼容修复位于 BLE DAL 发送适配边界：`dal_ble_get_pack_len()` 返回
`min(ATT_MTU - 3, 244)`，其中 `244 = 251 - 4-byte L2CAP - 3-byte ATT notification`。
它使同一个 984-byte App DATA 被拆成 `244+244+244+244+8` 五次通知；PC 仍按连续字节流重组，
只有第五片到齐并通过 App CRC 与截图元数据校验后才返回 ACK 1。960-byte 协议 DATA、停等 ACK、
截图格式和 MTP 主链路均不改。244 是针对 C400 缺陷的传输兼容上限，不是 Bluetooth 规范规定的
通用通知上限。

当前不预加固定延时或逐物理片 ACK：已有证据只证明单包超限，没有证明发送队列已满；停等协议
把每轮突发限制在五次通知。该源码修改尚未构建、尚未刷机、尚未做真机复测，不能写成 BLE 已
通过。下一道门依次是 debug 构建、单独授权刷机、启动日志冒烟，以及确认 oversized/buffer-full
日志均消失、ACK 连续推进和一张按序号/分块/长度/CRC/BMP 可读性完整验收的真图。

PC 单图命令仍为：

```powershell
uv run python -m agent_loop_system.tools.watch_ble screenshot `
  --address "<从 btm disp 读取的当前地址>" `
  --scan-timeout 15 `
  --timeout 180 `
  --output "D:\evidence\watch-ble.bmp"
```

`RealDeviceSession` 保持 `W30_HARDWARE_CAPTURE_PROVIDER=mtp`。BLE Provider 遵循现有
`capture(timeout, after_sequence) -> CaptureFrame` 合同，但本次真实单图门仍为 OPEN；
`.env` 中记录的旧 BLE 地址不得作为当前地址直接使用。v2 协议和 ACK 因果测试已经通过，
这只证明离线实现，不代替真实 618,518-byte BMP 验收。

## 已完成能力

1. 复用现有 `srv_quick_cmd`，统一输出 `w30_test_bridge` JSON。
2. 增加 `GUI_PING`、`GUI_STATE`、`GUI_TREE`。
3. 迁移 `WEATHER_CLEAR`、`TIMER_STOP`、`CLEAR_ALL_MSG`。
4. 增加 `SCREENSHOT_CAPTURE`：VDE 直接输出 packed BGR888，串口按 768 字节分块发送 Base64，并校验顺序、长度和 CRC32。
5. 截图只按需执行、单任务互斥；关屏/AOD 返回 `unavailable`，转场或并发返回 `busy`，不唤屏、不返回旧帧。
6. 所有工程入口统一由 `CONFIG_TOPSTEP_COMM_TEST` 控制。正常配置默认关闭；debug snippet 开启。关闭时 Quick Command 表、protobuf handler、GUI 注册、截图源文件和协议字符串均不进入最终 ELF。
7. 增加 `SCREENSHOT_CAPTURE_FILE`：复用同一 VDE 原始画面，先写固定临时文件，再原子提交为 `/root/download/agent_capture_<seq>.bmp`；新截图前只清理旧的 Agent 截图，USB 重开后的下一次 MTP 会话重新扫描存储。

## 当前默认：MTP 截图

调试 UART 参数固定为 1500000 baud、8N1、无流控、CRLF，`DTR=false`、`RTS=false`。
当前运行方式由 SuperCom 打开并持有 COM7，Agent-loop 配置
`W30_HARDWARE_TRANSPORT=supercom` 后使用 `\\.\pipe\SuperCom.AgentBridge.COM7`；不得再并行直开 COM7。

```text
dal_usb close
srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:<唯一序号>;"
dal_usb open
```

USB 重新枚举为 `ZORA`（VID `301A`、PID `6808`）后，从
`ZORA MTP Storage Volume/download/agent_capture_<seq>.bmp` 精确下载。找不到本次序号即失败，
不会回退到旧文件。整个过程可以在后台完成，
不需要打开资源管理器，也不应占用 SuperCom 前台。

2026-08-13 真机证据：

- `D:\Agent-loop-system\artifacts\usb_screenshot_smoke\831302\agent_capture.bmp`
- 文件长度 618,518 bytes，BMP 头声明与实际长度一致
- 410×502、24-bit、top-down，像素内容目视正常
- SHA256：`35B079707774A1962500EF4C2EF0BA9892103BA10CA3FF53C47D9ACC4B94A951`

独立工具已实现：

```powershell
watch-mtp-screenshot --port COM7 --output D:\evidence\watch.bmp
```

真机命令序号 `831303` 已从任意工作目录执行成功，输出：

- `D:\Agent-loop-system\artifacts\usb_screenshot_tool\831303\agent_capture.bmp`
- 画面为当时真机二维码页面，与上一张应用列表不同，排除了本次实测取到旧图
- 文件长度 618,518 bytes；410×502、24-bit、top-down
- 像素 CRC32：`5d66cc13`
- SHA256：`6EE20BD20A5CDCD7AE2BCFED7112308F8923FE350E8867FBD0F7081E6F7E71E6`
- 全仓测试：324 passed，84 subtests passed；仅一条既有 collection warning

当前 UART 仍未返回同序号 `screenshot_end`，所以工具输出 `receipt_verified=false`。
MTP 文件名已经携带完整 uint32 请求序号，工具强制精确匹配，所以新帧身份不再依赖 UART 回执。
UART 回执若恢复，工具仍会额外核对路径、尺寸和 CRC32。

2026-08-13 带序号真机门：

- 请求序号：`831304`
- MTP 目录中仅有 `agent_capture_831304.bmp`，旧固定文件和旧序号均已清理
- 请求不存在的 `agent_capture_831305.bmp` 时明确找不到，没有回退到 `831304`
- 输出：`D:\Agent-loop-system\artifacts\usb_screenshot_sequence\831304\agent_capture.bmp`
- 画面为当时真机表盘，目视正常
- 文件长度 618,518 bytes；410×502、24-bit、top-down
- 像素 CRC32：`7352b6ac`
- SHA256：`B8E1D90F0CB669842FA01BAEE684B176261E98C6672E519F19E9DDCDBCDC431F`

## 串口命令

```text
srv_quick_cmd send "TOP5STEP:GUI_PING:1;"
srv_quick_cmd send "TOP5STEP:GUI_STATE:2;"
srv_quick_cmd send "TOP5STEP:GUI_TREE:3;"
srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE:4;"
srv_quick_cmd send "TOP5STEP:WEATHER_CLEAR:;"
srv_quick_cmd send "TOP5STEP:TIMER_STOP:;"
srv_quick_cmd send "TOP5STEP:CLEAR_ALL_MSG:;"
```

### 真机测试会话起步

手表亮屏且串口可用时，依次开启 24 小时测试租约、设置简体中文，再临时跳过手机绑定页进入表盘：

```text
srv_quick_cmd send "TOP5STEP:TEST_SESSION:START;"
srv_quick_cmd send "TOP5STEP:LANGUAGE_SET:1;"
srv_quick_cmd send "TOP5STEP:ENTER_PAGE:DIAL,0;"
```

`LANGUAGE_SET:1` 对应简体中文并保存语言设置。这组命令不写入真实绑定状态；重启后仍会回到未绑定流程。不要用
`ENTER_BIND_RESULT_PAGE:1` 代替进入表盘命令，因为它会注入“绑定成功”事件及其附带逻辑。
测试租约由用户在批次开始前开启一次，Agent-loop 不启动、不续租、也不停止它。真机批次进入用例循环前只发送一次只读的
`TEST_SESSION:STATUS`；状态不是 `active` 时直接拒绝启动。通过 Agent 执行时仍需按顺序设置语言并进入表盘。

Agent-loop 默认使用同一个 `HardwareSerialSession` 和 `MtpCaptureProvider`。串口由
`RealDeviceSession` 唯一管理；Provider 只借用会话发送 `dal_usb close/open` 和截图命令，
不会重复打开或关闭串口。原 `WatchCaptureProvider` 保留为可显式注入的串口诊断回退。
`RealDeviceSession` 始终复用外部测试模式，不再提供按用例管理租约的开关。

## 验证结果

- Agent-loop 全量测试：329 passed，92 subtests passed；仅有一条既有 collection warning。
- 重点串口/截图/真机流程测试：77 passed，64 subtests passed。
- debug C500：1908/1908 编译并链接通过，`-Werror` 生效。
- 完整 debug 打包：ramrun、bootloader、C500、C400 均完成；生成四个 UP3。
- 工程 ELF 包含 Quick Command、截图入口和协议字符串。
- 隔离验证 ELF 在 `CONFIG_TOPSTEP_COMM_TEST=n` 时完整链接通过，且不含上述入口和字符串。
- 带序号版本 C500 增量编译和链接通过；ELF 已确认包含 `agent_capture_%u.bmp`。
- 工程 C500 内存：SRAMCODEDATA 243529/253792（95.96%）；PSRAMFLASHCODE 2556176/2568192（99.53%）；PSRAMDATA 161432/13103104（1.23%）。
- 亮屏真机截图：410×502 BGR888、804/804 分块、CRC32 校验通过，可生成并目视确认 BMP；睡眠页真机截图已显示 08 小时 29 分钟。
- `MtpCaptureProvider` 真机验证通过：借用已启动的 `HardwareSerialSession`，序号 `831306`，
  精确文件 `agent_capture_831306.bmp`，618,518 bytes，像素 CRC32 `83507ebd`；Provider
  关闭未关闭共享串口。证据：
  `D:\Agent-loop-system\artifacts\mtp_capture_provider\831306\provider_capture.bmp`。
- SuperCom 管道端到端真机用例 `CALC_001` 已 PASS：无 setup/action/collect 错误，取得一张
  410×502 BMP；截图显示数值 0 和五行完整计算键盘。证据：
  `D:\Agent-loop-system\artifacts\case_map_migration\6202_W5230\calculator\20260813-190100-runner-supercom-CALC_001\result.json`。
- `CALC_003` 的 `TP_CLICK` 和拆分 `TP_PRESS` 已真机探测：坐标与 GUI tree 控件矩形一致，
  命令可由 SuperCom 管道发出，但截图数值仍为 0。当前触摸注入通道未通过，不能把依赖触摸的
  用例标成可执行。

## 已知阻塞

### 低功耗后 UART 乱码或持续静默

状态：**OPEN；24 小时及更长的无人值守执行前必须解决。**

2026-08-13 真机复现：屏幕亮起且 UART 正常时，COM7 的 Quick Command、GUI 回执和截图传输正常；设备进入低功耗后，从 COM7 发送 `BUTTON_PRESS` 不再得到 `command_result`。第一种现象是命令正文乱码，同时出现 `exit wfi`、`sleep`；第二种现象是 COM7 仍能成功打开，但接收持续为零字节。第二种状态下，手工点亮并滑动屏幕后继续每 2 秒发送无副作用 `GUI_PING`，60 秒内仍未恢复。该问题已超过“首包丢失”范围。

复现证据：

- `D:\Agent-loop-system\artifacts\6202_real_cases\SLEEP_001\20260813-101404-key-to-dial\raw.log`
- `D:\Agent-loop-system\artifacts\6202_real_cases\SLEEP_001\20260813-101459-wake-key\raw.log`
- `D:\Agent-loop-system\artifacts\6202_real_cases\SLEEP_001\20260813-103049-live-wake-final\raw.log`

影响：测试间隔超过息屏时间后，Runner 可能连第一条唤醒或同步命令都无法可靠送达；不能把命令超时误报为产品页面失败，也不能靠重复发送有副作用的命令碰运气。`SCREENSHOT_CAPTURE` 仍应保持不唤屏，关屏时返回 `unavailable`；无人值守流程需要独立、可确认完成的显式唤醒路径。

关闭条件：至少覆盖 30 秒和 5 分钟息屏配置，完成 100 次“自然息屏 → 显式唤醒 → `GUI_PING` → 截图”循环；不得出现命令乱码、重复动作或旧帧，失败必须返回结构化原因。可选实现是硬件按键治具，或经验证不会丢首包的工程唤醒通道；尚未选型。

主刷机包：

- `D:\Agent-loop-workspace\6202_W5230\projects\tb_watch\build\zephyr\w30.up3`
- 大小：37,519,360 bytes
- SHA256：`6E858EB9A96BF1EF6A40D18ED76B818A2B4E30D7E3598BDF1F85556A1D0D1F38`
- 生成时间：2026-08-13 14:58:46
- 已增量编译、链接、签名和打包；已刷入真机并通过带序号 MTP 截图门

### 真机虚拟触摸未生效

状态：**OPEN。** `TP_CLICK` 和按下/抬起拆分命令均未改变计算器画面；当前 6202 计算器
case_map 只保留不依赖触摸的 `CALC_001` 为可执行，其余触摸类用例标记为 unable。关闭条件是
先让一个最小探针（计算器依次输入 1、2、3）稳定显示 123，再恢复批量用例。

## 下一道门

1. 独立 MTP 截图工具、带序号真机门、MTP `CaptureProvider` 和 Agent-loop 默认接入已经完成。
2. SuperCom 管道和首个真实端到端用例已经通过；后续继续保留外部测试会话执行。
3. 先修复或替换真机触摸执行通道，用 `CALC_003` 完成“输入 123”最小门。
4. 再恢复其余触摸类用例，并验证 AOD、关屏、转场和 popup；最后做 100 次循环和长时间 soak。
