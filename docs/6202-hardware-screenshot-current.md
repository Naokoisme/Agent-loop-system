# 6202 真机截图当前状态

更新时间：2026-08-14（Asia/Shanghai）

## 结论

6202 debug 固件已经支持两种按需截图：串口分块回传，以及截图落盘后通过 USB/MTP 下载。当前主链路改为 MTP：真机已经完成“后台关闭 USB → 生成截图文件 → 重开 USB → Windows MTP 下载”的闭环，得到可正常显示的 410×502 BMP。串口分块链路保留为诊断回退；PhoneCam 和 ADB 都不再属于当前真机截图方案。

已验证的手工过程已经收成独立命令行工具。带序号文件方案已经刷入真机并通过验证：固件生成 `agent_capture_<seq>.bmp`，主机只下载本次请求的精确文件名，不存在的序号不会回退旧图。`MtpCaptureProvider` 已成为 Agent-loop `RealDeviceSession` 的默认截图源，并通过独立真机 Provider 验证。

当前 COM7 由改造后的 SuperCom 持有，Agent-loop 通过命名管道 `\\.\pipe\SuperCom.AgentBridge.COM7` 双向收发，不再直接打开 COM7。亮屏并用主键显式唤醒后，`GUI_PING`、页面命令、GUI 回执和 MTP 截图已完整恢复；`CALC_001` 已在保留外部测试模式的条件下端到端 PASS。低功耗后的自动恢复仍是长时间无人值守自动化的独立阻塞项。

## 隔离边界

- 6202 开发与构建：`D:\Agent-loop-workspace\6202_W5230`
- Agent-loop：`D:\Agent-loop-system`
- 上游参考：`D:\TOPSTEP\shenju_w30`，本次未修改、未构建
- 当前真机已刷入带序号 debug 固件；所有改动均未提交、未推送

## BLE 独立截图 POC（待真机门）

已在 6202 隔离工作区和 Agent-loop 中实现最小闭环源码：PC 通过现有 FF02
写入工程命令 `0x7F01`，固件复用现有
`gui_comm_watch_capture_file_request(seq)` 生成同一份原子 BMP，再通过 FF03 Notify
发送 START、顺序 DATA 和 END。DATA 固定为 960 字节，END 携带文件长度、分块数和
CRC32；PC 只在顺序、长度、CRC32 以及 410×502 top-down 24-bit BMP 全部通过后原子落盘。

PC 命令：

```powershell
uv run python -m agent_loop_system.tools.watch_ble screenshot `
  --address "42:74:DC:C8:0A:02" `
  --scan-timeout 15 `
  --timeout 180 `
  --output "D:\evidence\watch-ble.bmp"
```

当前验证边界：PC 侧协议与模拟 FF02/FF03 端到端测试通过；目标手表 GATT 连接已通过。
固件桥接源码尚未在受保护的工程会话中编译，未刷机，也未完成真实 618,518-byte BLE
传图。因此 MTP 仍是 Agent-loop 默认截图源，BLE 仅是可选 POC；在“debug 构建通过 →
用户另行授权刷机 → 单张真机截图通过”之前不得切换默认路线。

当前 Codex 进程即使进入 Zora 环境，CMake 也会在预处理受 E-SafeNet 保护的
`zephyr/include/zephyr/dt-bindings/adc/adc.h` 时提前停止，尚未编译到新增桥接文件；
不得修改该受保护头文件。下一次构建应由可读取受保护源码的工程终端执行：

延期记录（2026-08-14）：用户当前不在工程现场，暂不执行构建、刷机和真机单图门。
离线阶段只继续完善 PC/Agent-loop 的可选 BLE Provider 与模拟测试；恢复现场后从下列
构建命令继续，不重复前面的协议实现。

```text
cmd
zora
cd /d D:\Agent-loop-workspace\6202_W5230
python build.py -b w30_wcs -c500 -S debug projects/tb_watch
```

### Agent-loop 可选接入（离线模拟已通过）

`RealDeviceSession` 保持 MTP 为默认截图源。只有显式设置下列变量时，截图 Provider
才切到 BLE；真机业务命令仍使用现有 UART/SuperCom，不会被蓝牙替代：

```powershell
$env:W30_HARDWARE_CAPTURE_PROVIDER = "ble"
$env:W30_HARDWARE_BLE_ADDRESS = "42:74:DC:C8:0A:02"
$env:W30_HARDWARE_BLE_SCAN_TIMEOUT = "15"
```

BLE Provider 遵循现有 `capture(timeout, after_sequence) -> CaptureFrame` 合同；每次只建立
一次短连接，完成扫描、连接、截图、校验后立即断开。默认单图总超时为 180 秒，证据元数据
保留目标 BLE 地址、文件 CRC32、像素 CRC32、分块大小和数量。取消上述变量或设置
`W30_HARDWARE_CAPTURE_PROVIDER=mtp` 即继续使用原 MTP 路线。

实测准备记录（2026-08-14）：上述三项已经写入 `D:\Agent-loop-system\.env`，目标地址为
`42:74:DC:C8:0A:02`。明日启动 Agent-loop 测试进程时会自动加载；如果 PowerShell
进程已经预先设置了同名变量，应先确认，因为进程变量优先于 `.env`。

截至 2026-08-14，假 GATT 流、BLE Provider、环境选择和 `RealDeviceSession` 定向回归为
57 passed、14 subtests passed。该结果只证明离线代码接线，不能代替固件构建、刷机和真实
FF03 传图门。

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
