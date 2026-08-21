# 6202 BLE 当前能力

> 文档角色：BLE 当前能力、诊断与恢复
>
> 最近核对：2026-08-18（Asia/Shanghai）

本页只记录 6202 当前已经证明的 BLE 能力和仍然有效的处理步骤。正式真机截图仍默认使用 MTP；
总体结论见 [6202 真机截图当前状态](6202-hardware-screenshot-current.md)。

## 当前结论

| 能力 | 当前状态 |
| --- | --- |
| Windows 扫描并连接手表 BLE | 已验证，但受手机占用和广播状态影响 |
| 通过 App BLE 协议发送 PB / QuickCmd | 已验证 |
| 通过 BLE 收取完整原始 BMP | 真机闭环通过，非默认 |
| 低功耗时通过 BLE 启动测试会话 | 真机已验证 |
| 低功耗后直接 UART 自行恢复 | 未证明 |
| 手机绑定、登录、HFP/经典蓝牙通话 | 不由当前 PC BLE 链路替代 |

Agent-loop 的 BLE 客户端复用手表现有 App 协议，不发送 579 的
`RawCommand.packet()` 原始包，也不把任意序列化 protobuf 字节机械切片。新平台 PB 使用
10-byte 大端 `0xAB` 信封和 CRC-16/IBM；需要分片时必须按协议语义拆分对象或列表。

## PC 扫描不到手表

一次 Windows 扫描为空，不能证明手表 BLE 坏了。按下面顺序定位，找到所属层后停止：

1. 通过 SuperCom 命名管道执行只读 `btm disp`；不得为诊断而直开 COM7。
2. 查看蓝牙栈、LE 广播状态和当前 BLE 连接数。
3. 如果已有 BLE 连接，先确认是否被手机官方 App 占用。需要 PC 连接时，让手机 App 断开并
   避免自动重连，再重新扫描。
4. 只有在 `bthost_status=1`、BLE 连接数为 0、广播状态为 0 时，才发送
   `btm le adv_start`；随后再次执行 `btm disp` 确认广播已经开启。
5. 广播已开启且无连接后，再运行一次有界 Windows 扫描：

~~~powershell
uv run python -m agent_loop_system.tools.watch_ble scan --timeout 8
~~~

2026-08-18 最近一次实机发现为 `oraimo Watch Tank N_2906` /
`54:C8:D4:D9:29:06`。该记录用于识别设备，不应把旧地址长期写死为“当前设备”；每轮仍以
实时扫描和固件状态为准。

如果固件显示正在广播、连接数为 0，但 Windows 仍扫描不到，应报告为“Windows 发现层失败”；
不要反复重启蓝牙栈、刷机或修改协议。`btm le adv_stop` 只用于明确的诊断实验，不是常规恢复
步骤。

扫描成功只证明 Windows 发现了 BLE 外设，不证明手机绑定、账号登录、HFP 或产品业务已经成功。

## 低功耗后的恢复入口

手表自然进入低功耗后，直接 UART 指令不可靠的问题仍未定位。2026-08-18 已证明一个最小外部
恢复入口：

~~~text
低功耗
→ PC 通过 BLE PB QuickCmd 0x0406 发送一次 TOP5STEP:TEST_SESSION:START;
→ UART 收到 command_result accepted
→ UART 收到 test_session active / lease_seconds=86400
→ 屏幕常亮测试策略恢复
~~~

这次验证在发送 BLE START 前没有写入任何 UART 唤醒命令，因此能够证明恢复由 BLE 入口触发。
证据：

- [result.json](../evidence/ble_test_session_start_20260818-102711/result.json)
- [serial/events.jsonl](../evidence/ble_test_session_start_20260818-102711/serial/events.jsonl)

它不证明任意 UART 指令能在低功耗后自行恢复，也不改变普通 Runner 的会话规则。该 START 是
一次单独授权的外部动作；普通 Runner 仍只查询 `TEST_SESSION:STATUS`，不自动 START、续期或
STOP。本次会话没有由 Runner STOP。

## BLE 截图

当前可选 Provider 使用以下顺序：

~~~text
关闭 USB → BLE 请求完整 BMP → 接收并校验所有分块 → 恢复 USB
~~~

关闭 USB 是为了避免截图过程中 USB/MTP 与 BLE 传输互相干扰。成功后必须确认 ZORA 已恢复；
失败路径也会尝试恢复 USB，并把恢复失败与原始 BLE 错误一起保留。

当前固件的 `dal_ble_get_pack_len()` 上限为 244 bytes，用于避开本目标 C400 控制器的 ACL
缓冲区问题。这是 6202 当前实现的兼容值，不是通用 BLE 上限。

[2026-08-18 BLE Provider 真机结果](../evidence/ble_provider_usb_fix_20260818-095126/provider-run/result.json)
完成 645 个分块、618,518-byte BMP，长度、CRC、410×502、24-bit top-down 格式和可读性均
通过，结束后 ZORA 已恢复。

该次单张截图约 172 秒，并出现 644 条 `lld mem alloc buf fail type 0x2`。此项已登记为后续
优化，当前不继续扩张调查范围；MTP 仍是默认截图链路。

## 不在当前 BLE 链路内的能力

- 手机官方 App 的账号体系、绑定状态和业务编排。
- 经典蓝牙 / HFP 通话链路。
- 手机界面截图或 ADB 自动化。
- 用 BLE ACK 代替手表画面验收。

这些能力如需验证，必须单独定义目标和证据，不能从 PC BLE 扫描或 PB ACK 推断。

## 实现与协议入口

- [watch_ble.py](../src/agent_loop_system/tools/watch_ble.py)：Windows BLE 客户端。
- [watch_ble_provider.py](../src/agent_loop_system/tools/watch_ble_provider.py)：关闭 USB、BLE
  取图和恢复 USB。
- [watch_app_protocol.py](../src/agent_loop_system/tools/watch_app_protocol.py)：App PB 信封与 CRC。
- [PB 协议当前说明](pb-protocol-current.md)：命令编码边界。
