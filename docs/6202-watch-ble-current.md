# 6202 Windows BLE 阶段性归档

> 归档时间：2026-08-13  
> 状态：暂停推进，代码保留，不作为当前真机自动化主链路。

## 阶段结论

Agent-loop 已具备一套实验性的 Windows BLE GATT 客户端，可以扫描 6202、编码/解码 App 协议，并尝试绑定和登录。

它没有完成真机绑定闭环，也不能代替真实手机提供经典蓝牙通话、手机通知权限、音乐播放、遥控相机或 Android/iOS 兼容性环境。

当前真机自动化主链路是：

- UART：命令与日志。
- MTP：按需取得屏幕截图。
- Agent-loop：执行用例、保存证据和判定结果。

基于 KISS/YAGNI，BLE Quick Command 和“电脑扮演完整手机 App”暂停，不继续扩建。

## 已保留的能力

### 协议层

- 6202 App 帧：`0xAB` 十字节头、CRC-16/IBM、命令号、序号和增量重组。
- 绑定/登录 protobuf：`0x0301` / `0x0302`。
- 请求、响应和用户资料的范围校验。

实现：

- `src/agent_loop_system/tools/watch_app_protocol.py`
- `src/agent_loop_system/protocol/watch_app_pb2.py`

### Windows BLE 客户端

- 按设备名前缀或 Service UUID 扫描。
- 唯一设备选择和 GATT UUID 校验。
- FF03 Notify 订阅、FF02 分片写入和响应重组。
- 请求命令号、序号和 CRC 校验。
- 首次绑定实验时序：连接 -> 订阅 -> 写入 `0x0301` -> Windows BLE 配对 -> 等待原请求响应。
- 对扫描、连接、断连、配对、超时、协议和认证错误做结构化分类。
- `close()` 可重复调用，客户端可重新连接。

实现：

- `src/agent_loop_system/tools/watch_ble.py`

依赖：

- `bleak>=2.0,<4`
- `protobuf>=6.31,<7`

## 验证状态

2026-08-13 复跑：

```text
uv run pytest -q tests/test_watch_app_protocol.py tests/test_watch_ble.py
29 passed, 11 subtests passed
```

这些测试使用模拟 BLE 后端，证明主机侧协议、时序和错误处理符合代码约定；不证明真机 BLE 通信或产品绑定成功。

当前没有可归档的真机成功证据：

- 未证明恢复出厂后的 6202 已通过 Windows 完成 App 协议绑定。
- 未证明绑定后 `0x0302` 登录成功。
- 未证明重启后绑定信息和 `bind_time` 保持一致。
- 未证明 Windows BLE 配对会为当前固件建立所需的经典蓝牙状态。

因此不得把“扫描到设备”“GATT 已连接”或“Windows 显示已配对”写成绑定成功。

## 固件侧已知门槛

当前固件的绑定/登录处理还依赖经典蓝牙手机连接状态。BLE 配对与经典蓝牙产品状态不是同一件事。

首次 `0x0301` 可能借助“先写请求、后触发 BLE 配对”的特殊时序获得响应；即便首次绑定返回成功，也不能推出后续登录和完整手机能力已经建立。

`BLOCKED_BY_CLASSIC_BT_GATE` 仅表示主机根据超时和已知固件条件做出的谨慎分类，不是手表返回的明确错误码。

## 与当前测试用例的关系

当前 `case_map` 有 3164 条用例，其中 85 条文本明确出现 BLE/蓝牙：79 条为 `unable`，68 条集中在通话模块。大多数通话用例依赖经典蓝牙/HFP或真实手机，不会因 Windows BLE GATT 客户端而自动变得可执行。

现有 BLE 能力直接适合的窄场景是：

- `START_005`：扫描手表开机阶段的可绑定 BLE 广播。
- BLE App 帧格式、认证响应和异常时序的协议测试。
- 将来明确选中的纯 BLE 数据同步用例。

通知页面等测试已有 Quick Command 数据注入方式；没有必要为了页面判定再复制一套 BLE Quick Command。

## 保留命令

只读扫描，不连接、不配对：

```powershell
uv run python -m agent_loop_system.tools.watch_ble scan --timeout 6
```

以下命令会改变手表或 Windows 的状态，暂停阶段不要自动执行：

```powershell
uv run python -m agent_loop_system.tools.watch_ble bind --address "<address>" --auth-code "<二维码验证码>" --user-id "agent-loop-system" --timeout 60
uv run python -m agent_loop_system.tools.watch_ble login --address "<address>" --user-id "agent-loop-system" --timeout 60
```

## 恢复推进的条件

只有满足以下任一条件时再恢复 BLE：

1. 已选中的真机 P0 用例明确需要 BLE 广播或 App 协议，UART Quick Command 无法满足验收语义。
2. 目标变为验证手机 App 协议本身，而不只是验证手表页面。
3. 已确定要建设真实 Android/iOS 手机自动化，并需要 BLE 客户端做协议诊断或故障注入。

恢复时从最小闭环开始：扫描 -> 单次绑定 -> 单次登录 -> 重启后登录。不要先增加 BLE Quick Command、天气、通知、音乐、相机或经典蓝牙模拟。

