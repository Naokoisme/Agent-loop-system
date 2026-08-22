# 6202 真机截图当前状态

> 文档角色：当前概览与问题索引
>
> 最近核对：2026-08-18（Asia/Shanghai）
> 适用目标：`D:\Agent-loop-workspace\6202_W5230` / `6202_W5230`

本文只保留当前仍然成立的结论。MTP 的具体操作见
[6202 MTP 截图操作指南](6202-mtp-screenshot-tool.md)，BLE 的诊断、截图与低功耗恢复见
[6202 BLE 当前能力](6202-watch-ble-current.md)。

## 当前结论

| 链路 | 当前状态 | 用途 |
| --- | --- | --- |
| MTP 截图 | 可用，默认 | Agent-loop 真机视觉证据 |
| BLE 截图 | 真机闭环已通过，非默认 | MTP 不可用时的可选链路 |
| BLE 启动测试会话 | 真机已验证 | 手表低功耗时的外部恢复入口 |
| 低功耗后直接发 UART | 仍未解决 | 不作为可靠唤醒方式 |
| UART 分块截图 | 仅诊断 | 不进入正式视觉证据 |
| PhoneCam / ADB | 不在本链路内 | 不能替代手表原始截图 |

COM7 由 SuperCom 独占。Agent-loop 只通过
`\\.\pipe\SuperCom.AgentBridge.COM7` 复用串口，不能并行直开 COM7。

## 当前链路

默认链路：

~~~text
Agent-loop → SuperCom 管道 → 关闭 USB → 手表生成本次 BMP
→ 恢复 USB → MTP 精确下载本次文件 → 校验并登记证据
~~~

BLE 可选链路：

~~~text
Agent-loop → SuperCom 管道关闭 USB → PC 通过 BLE 收取完整 BMP
→ 恢复 USB → 校验并登记证据
~~~

两条链路都必须取得一张属于本次检查点的新截图。ACK、日志、`GUI_TREE` 和
`GUI_PING` 只能辅助定位，不能代替产品画面的 PASS/FAIL 判定。

## 常见问题索引

### MTP 截图时设备消失

关闭 USB 后 ZORA 暂时消失是正常步骤；截图结束后仍未重新出现才是故障。当前 Provider 会尝试
恢复 USB，但不会使用旧图掩盖失败。处理步骤见
[MTP：设备消失或未恢复](6202-mtp-screenshot-tool.md#mtp-设备消失或未恢复)。

### 截图停在“1% 充电中”

现象：命令返回 `accepted`、`GUI_PING` 为 `processed`，但新截图一直显示“1% 充电中”，
没有进入目标页面。此时先按一次手表实体按键退出充电画面，再重新执行当前用例；实体按键恢复
后即可继续截图。该现象不能直接判为业务 FAIL，也不能用充电画面作为产品结果证据。

### PC 扫描不到 BLE

先区分“手表没有广播”“手机已占用连接”和“Windows 没有发现设备”，不能仅凭一次 PC 扫描
判定 BLE 故障。处理步骤见
[BLE：PC 扫描不到手表](6202-watch-ble-current.md#pc-扫描不到手表)。

### 低功耗后 UART 指令无效

直接 UART 唤醒仍未证明可靠。已经验证的低功耗恢复方式是：通过 BLE 发送
`TEST_SESSION:START`，随后 UART 收到 `active / 86400`。这条记录只证明 BLE 低功耗恢复能力；
设备重启并恢复 SuperCom 管道后，Runner 也可在批次启动流程发送 `START`。详见
[BLE：低功耗后的恢复入口](6202-watch-ble-current.md#低功耗后的恢复入口)。

## 会话与证据边界

- Runner 在真机批次启动或设备重启恢复后发送 `TEST_SESSION:START` 并等待 `active`；
  不要求外部预先持有会话，普通用例结束不发送 `STOP`。
- `command_result accepted` 只表示命令被接受，不能证明页面或业务结果正确。
- 每个视觉检查点必须对应独立的新截图，并保留请求序号、文件哈希、尺寸和格式校验。
- 6202 的命令、坐标、截图和结论不能复制到 Simulator、6204、W30 或其他目标。

## 当前真机证据

| 能力 | 当前证据 |
| --- | --- |
| 默认 MTP Provider | [result.json](../evidence/6202_hardware_solidify_20260818-004854/host-fix-default-provider-01/result.json)：自动序号 2,587,899，`receipt_verified=true`，618,518-byte、410×502、24-bit top-down BMP |
| BLE 完整截图 | [result.json](../evidence/ble_provider_usb_fix_20260818-095126/provider-run/result.json)：645 个分块，长度、CRC、格式和可读性校验通过，结束后 ZORA 已恢复 |
| 低功耗 BLE 恢复 | [result.json](../evidence/ble_test_session_start_20260818-102711/result.json) 与 [events.jsonl](../evidence/ble_test_session_start_20260818-102711/serial/events.jsonl)：BLE 发送一次 START，UART 返回 `active`、`lease_seconds=86400` |

## 仍保留的开放项

- BLE 单张截图约 172 秒，并伴随大量底层内存分配失败日志；按当前安排暂不优化，MTP 继续作为默认。
- 手表自然进入低功耗后，直接 UART 指令为什么不能可靠恢复仍未定位；现阶段使用已验证的 BLE
  外部恢复入口，不把它描述为 UART 根因修复。
