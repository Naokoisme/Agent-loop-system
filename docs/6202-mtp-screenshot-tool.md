# 6202 MTP 截图操作指南

> 文档角色：MTP 当前操作与故障恢复
>
> 最近核对：2026-08-18（Asia/Shanghai）

本页只说明 6202 当前默认的 MTP 截图链路。总体状态和问题索引见
[6202 真机截图当前状态](6202-hardware-screenshot-current.md)。

本流程不构建、不刷机；测试会话由 Runner 在每条批次用例启动前的受控重启流程建立，
MTP 截图步骤不自行停止会话。

## 前置条件

1. SuperCom 已独占 COM7，并提供双向命名管道
   `\\.\pipe\SuperCom.AgentBridge.COM7`。
2. `W30_HARDWARE_SOURCE_ROOT` 和 `W30_HARDWARE_WORKSPACE_ROOT` 都指向
   `D:\Agent-loop-workspace\6202_W5230`，项目为 `6202_W5230`。
3. Runner 已完成逐用例重启恢复，发送 `TEST_SESSION:START` 并确认会话为 `active`。
4. 手表运行支持 `SCREENSHOT_CAPTURE_FILE` 的工程固件，屏幕处于可截图状态。

## 配置

在本机 `.env` 中使用等价配置；设备绑定不要提交到仓库：

~~~dotenv
W30_HARDWARE_SOURCE_ROOT=D:\Agent-loop-workspace\6202_W5230
W30_HARDWARE_WORKSPACE_ROOT=D:\Agent-loop-workspace\6202_W5230
W30_HARDWARE_PROJECT=6202_W5230
W30_HARDWARE_TRANSPORT=supercom
W30_HARDWARE_PORT=COM7
W30_HARDWARE_BAUDRATE=1500000
W30_HARDWARE_CAPTURE_PROVIDER=mtp
~~~

`W30_HARDWARE_TRANSPORT=supercom` 表示 Agent-loop 通过 SuperCom 管道使用共享串口，不会并行
打开物理 COM7。`MtpCaptureProvider` 借用同一个 `RealDeviceSession`，不会重复创建串口会话。

## 通过 Runner 取图

为本轮结果和截图使用唯一目录：

~~~powershell
uv run python -m agent_loop_system.tools.test --sheet <模块> --case-id <CASE_ID> --target hardware --case-map-profile 6202_W5230 --result-file <本轮唯一目录>\result.json --screenshot-path <本轮唯一目录>\screenshot.bmp
~~~

普通运行只会执行符合当前 case map 合同的路径。`--candidate-replay` 仅供独占用例的外部准入流程
使用，不能为了绕过映射状态临时添加。

旧的 `watch-mtp-screenshot --port COM7` 和
`python -m agent_loop_system.tools.mtp_screenshot --port COM7` 会直接取得物理串口所有权，不适用于
当前 SuperCom 持有 COM7 的配置。

## Provider 的实际顺序

~~~text
关闭 USB → 触发 SCREENSHOT_CAPTURE_FILE → 恢复 USB
→ 精确下载 agent_capture_<seq>.bmp → 校验 BMP → 保存证据
~~~

固件先写临时文件，再原子提交为带完整 uint32 请求序号的文件。主机只接受本次精确文件名；
找不到即失败，不回退旧图。`receipt_verified=false` 只表示没有额外收到同序号 UART 终态，
不否定已经由精确文件名确认的 MTP 帧身份。

当前真机 shell 要求每条命令在一次不超过 64 bytes 的完整写入中送达。截图序号 9,999,999
对应的完整命令（含 CRLF）刚好是 64 bytes；从 10,000,000 起会超过边界。因此宿主只分配
1..9,999,999，越界会在关闭 USB 前失败，不拆包发送。

## 常见问题

### MTP 设备消失或未恢复

现象分两种：

- 关闭 USB 后 ZORA 暂时从资源管理器消失：这是截图步骤的一部分。
- 截图结束后 ZORA 仍未重新出现：这是恢复失败，不能继续当作截图成功。

当前 Provider 会发送 `dal_usb open` 并等待重新枚举；第一次等待超时会再发送一次。无论截图
在哪一步失败，退出前还会做一次尽力恢复。成功必须同时看到 ZORA 以 VID `301A`、
PID `6808` 重新出现并能访问其 WPD/MTP 存储。

如果仍未恢复：

1. 停止本次用例并保留串口、MTP 和结构化错误证据。
2. 不直开 COM7，不关闭 SuperCom，不使用旧 BMP 继续判定。
3. 把问题报告为“USB 未恢复”，不要笼统写成“截图失败”。

### 本次截图文件找不到

Windows 的 MTP 命名空间偶尔会在设备刚重新枚举时保留旧视图。当前 Provider 只会针对同一个
精确序号刷新并重查一次；仍找不到就失败。它不会下载较早的
`agent_capture_*.bmp` 作为替代。

### 返回 unavailable 或 busy

关屏、AOD、页面转场或并发截图都可能让固件拒绝取图。保留固件给出的结构化原因并停止该
检查点；不要伪造帧，也不要把 ACK 当作截图。

### 测试会话不是 active

Runner 在每条批次用例启动前重启设备，随后发送 `TEST_SESSION:START` 并等待 `active`。
若启动或最终 `DIAL + popup=null` 门槛失败，保留结构化错误并停止批次。低功耗场景的已验证 BLE 恢复入口见
[6202 BLE 当前能力](6202-watch-ble-current.md#低功耗后的恢复入口)。

## 成功判据

- SuperCom 管道持续可用，没有改为直开 COM7。
- ZORA 已恢复并可访问。
- 只下载本次序号的 `agent_capture_<seq>.bmp`。
- BMP 为 618,518 bytes、410×502、24-bit、top-down，文件头长度与实际长度一致。
- 元数据包含请求序号、文件路径、SHA256、像素 CRC32 和 `receipt_verified`。
- 每个视觉检查点都有一张独立新图，产品 verdict 只由截图决定。

## 当前真机证据

[2026-08-18 默认 Provider 结果](../evidence/6202_hardware_solidify_20260818-004854/host-fix-default-provider-01/result.json)
使用自动序号 2,587,899，`receipt_verified=true`，得到 618,518-byte、410×502、
24-bit top-down BMP。本文不再保留已被该结果替代的旧包哈希、旧序号和旧失败记录。

实现位置：[mtp_screenshot.py](../src/agent_loop_system/tools/mtp_screenshot.py)。
