# 6202 MTP 独立截图工具

## 目标

提供一条独立命令，从已经烧录工程截图命令的 6202 真机取得 BMP。工具不读取
`D:\Agent-loop-workspace\6202_W5230`，也不依赖测试用例或 Agent-loop Runner。

输入只有调试串口和输出路径；内部流程固定为：

```text
关闭 USB → 触发 SCREENSHOT_CAPTURE_FILE → 重开 USB → 精确下载带序号文件 → 校验 BMP
```

## 使用

```powershell
watch-mtp-screenshot --port COM7 --output D:\evidence\watch.bmp
```

未安装命令行入口时也可以运行：

```powershell
python -m agent_loop_system.tools.mtp_screenshot --port COM7 --output D:\evidence\watch.bmp
```

工具必须后台运行，不打开资源管理器，也不操作 SuperCom。若 COM7 被占用，应明确报错，
由用户关闭占用程序后重试。

## 成功判据

- USB 重新枚举为 `ZORA`（VID `301A`、PID `6808`）。
- 只下载本次序号对应的 `agent_capture_<seq>.bmp`，找不到即失败，不回退旧文件。
- 下载到的文件是完整的 24-bit BMP，文件头长度等于实际长度。
- 当前 6202 图片必须为 410×502、top-down。
- 输出 JSON 包含命令序号、文件路径、SHA256、像素 CRC32 和是否收到同序号串口终态。

固件以固定临时文件写入并原子替换为 `agent_capture_<seq>.bmp`，新截图前只清理旧的 Agent
截图文件。主机只接受本次完整 uint32 序号对应的文件名，因此 UART 没有回执时仍可校验新帧身份。
`receipt_verified=false` 仅表示没有额外验证串口终态，不再表示 MTP 文件序号无法确认。

## 当前验证结果

2026-08-13 使用命令序号 `831303` 完成真机验证。工具从后台控制 COM7，USB 成功重新枚举，
并下载出当时的二维码页面：

- 路径：`D:\Agent-loop-system\artifacts\usb_screenshot_tool\831303\agent_capture.bmp`
- 618,518 bytes，410×502、24-bit、top-down
- 像素 CRC32：`5d66cc13`
- SHA256：`6EE20BD20A5CDCD7AE2BCFED7112308F8923FE350E8867FBD0F7081E6F7E71E6`
- `receipt_verified=false`：当前板子没有返回文件截图的 UART 终态

带序号版本已刷入并完成真机门。序号 `831304` 成功生成并下载
`agent_capture_831304.bmp`；设备目录里只有该文件。只读查询不存在的
`agent_capture_831305.bmp` 得到明确找不到，没有回退旧图。

证据：`D:\Agent-loop-system\artifacts\usb_screenshot_sequence\831304\agent_capture.bmp`；
618,518 bytes，像素 CRC32 `7352b6ac`，SHA256
`B8E1D90F0CB669842FA01BAEE684B176261E98C6672E519F19E9DDCDBCDC431F`。

## Agent-loop 接入

`MtpCaptureProvider` 实现通用 `capture(timeout, after_sequence) -> frame` 和 `close()`。
它复用同一套独立工具核心，并借用 `RealDeviceSession` 已启动的 `HardwareSerialSession`；
不会二次打开或关闭 COM 口。`frame.save_bmp(path)` 直接保存已经完成格式、尺寸和 CRC 校验的
MTP BMP。

`RealDeviceSession` 默认使用该 Provider；原串口分块 `WatchCaptureProvider` 仅保留显式注入能力。
2026-08-13 真机 Provider 序号 `831306` 已成功生成 618,518-byte BMP。完整
`RealDeviceSession` 仍在启动 `GUI_PING` 处因 UART 无回执超时，尚未进入 MTP 基线阶段。
