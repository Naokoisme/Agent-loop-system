# 新平台 PB 协议实现说明

> 实现日期：2026-08-17
> 规格来源：`D:\新平台指令协议.md` 中 v0.1.2 的协议表和 protobuf 代码块
> 范围：Agent-loop 主机侧协议与 Windows BLE 客户端；未修改、构建或刷写固件

## 结论

Agent-loop 已具备可复用的新平台 PB 主机实现：完整帧编解码、CRC、App/Device
单包限制、对象/列表分包、乱序与重发重组、跨通道隔离、阻塞请求关联、普通通知接收，
以及文档代码块中 7 组 protobuf schema。

该实现可在没有 BLE 的情况下独立测试，也已接入现有 `WatchBleClient`。主机单元测试通过不等于 6202 真机绑定、登录、文件传输或产品功能已经通过。

## 实现组成

- `src/agent_loop_system/tools/watch_app_protocol.py`
  - 10 字节 `0xAB` 帧头。
  - CRC-16/IBM，初值 `0`、多项式 `0xA001`。
  - `cmd`、`key`、payload 长度、CRC、sequence 全部按文档使用大端。
  - App 数据区默认最大 1024 字节；Device 数据区最大 4096 字节。
  - 发送前校验文档声明的 UTF-8 字符串、bytes 和 repeated 字段上限。
  - 分包 payload 前 8 字节为 `total_packets` 和从 0 开始的 `packet_index`。
  - 对象分包和列表分包均按序合并 protobuf wire 数据。
  - 相同 command/sequence 的重复包必须内容一致；冲突重发会报协议错误。
  - 重组状态按外部 channel、command、sequence、基础 flags 隔离，可同时处理多个通道和消息。
- `src/agent_loop_system/protocol/pb_commands.py`
  - 7 个命令大类和 164 个文档 key 常量。
- `src/agent_loop_system/protocol/pb_schema.py`
  - 按 `_DeviceInfo` 这类文档名称动态查找或创建消息。
- `src/agent_loop_system/protocol/pb_*.proto`
  - 文档代码块中的 7 组 schema，共 114 个顶层消息类型。
- `src/agent_loop_system/protocol/pb_*_pb2.py` / `.pyi`
  - 由 `libprotoc 25.1` 生成的 Python 运行时代码和类型声明。
- `src/agent_loop_system/protocol/watch_app_pb2.py`
  - 旧认证导入路径的兼容层，统一复用完整 `pb_env` schema，不再维护重复定义。
- `src/agent_loop_system/tools/watch_ble.py`
  - 单包严格执行 App 1024 字节上限；超限 protobuf 不做危险的字节硬切。
  - 对象/列表分包通过 `send_multipart()`、`send_protobuf_chunks()`、
    `exchange_multipart()` 或 `exchange_protobuf_chunks()` 传入可独立解码的语义分片。
  - 多包响应自动重组后再按 command/sequence 完成请求。
  - 多个阻塞请求可以并发等待，GATT 写入保持帧字节原子性。
  - 普通无响应指令使用 `send()` / `send_protobuf()`。
  - App 阻塞请求使用 `exchange()` / `exchange_protobuf()`。
  - Device 主动消息使用 `receive()` / `receive_protobuf()`。

## 最小用法

构造命令和 protobuf 消息：

```python
from agent_loop_system.protocol import pb_config_pb2
from agent_loop_system.protocol.pb_commands import (
    ConfigKey,
    PbCommandGroup,
    command_id,
)

command = command_id(PbCommandGroup.SETTING, ConfigKey.SET_FUNCTION_CONFIG)
request = pb_config_pb2._FunctionConfig(flags=b"\x03")
```

通过 BLE 执行 App 阻塞请求：

```python
response = await client.exchange_protobuf(
    command,
    request,
    pb_config_pb2._CommonResponse,
)
if response.result != 0:
    raise RuntimeError(f"device rejected config: {response.result}")
```

发送不需要同 command/key 响应的普通指令：

```python
sequence = await client.send_protobuf(command, request)
```

列表或对象超出 1024 字节时，每个分片都必须是可独立解码的 protobuf 消息。例如把列表按 item 分组后：

```python
from agent_loop_system.tools.watch_app_protocol import split_protobuf_list

partial_lists = split_protobuf_list(full_list, "items")
sequence = await client.send_protobuf_chunks(
    command,
    partial_lists,
)
```

顶层字段很多的对象可使用 `split_protobuf_object()` 按已设置字段贪心分组。
单个字段或单个列表 item 自身超过 1024 字节时，helper 会明确报错，调用方需要采用
该业务结构专用的拆分策略。

不能把一个序列化后的 protobuf 字节串在任意偏移直接切开。当前固件会逐片调用 protobuf decoder；硬切可能把一个 field 截断并导致解码失败。

接收 Device 主动通知：

```python
envelope, message = await client.receive_protobuf(
    pb_config_pb2._FunctionConfig,
    timeout=10,
)
```

只使用传输无关的协议层：

```python
from agent_loop_system.tools.watch_app_protocol import (
    WatchAppMessageDecoder,
    encode_frame,
    encode_protobuf_frames,
)

frames = encode_protobuf_frames(command, 0, request)
wire = b"".join(encode_frame(frame) for frame in frames)
messages = WatchAppMessageDecoder().feed(wire, channel="gatt")
```

## 文档自身的不一致

v0.1.2 版本记录列出了一些新增字段，但后面的 protobuf 代码块没有给出这些字段的 tag、类型或定义：

- `_DeviceInfo.icType`
- `_DeviceInfo.deviceSpeechScene`
- `_DeviceInfo.appSpeechScene`
- `_DeviceInfo.aiSDKs`
- `_DeviceInfo.aiTextBytes`
- `_UnitConfig.updateTime`
- `_UserInfo.birthday`
- `_UserInfo.updateTime`
- `_AiMessage` 的文字说明提到文本/错误内容，但实际 message 只定义了 `type` 和 `shape`

代码没有猜测这些字段号。当前 schema 严格实现文档中可编译的 protobuf 定义；
要加入上述字段，需要提供带明确 tag 和类型的新版 `.proto`。版本记录中仅扩展已有
整数取值或 bit 含义的内容不受此问题影响。

## 验证边界

已完成的本地验证覆盖：

- 已知 CRC 向量和完整帧往返。
- BLE 字节碎片、连续多帧和垃圾前缀处理。
- App 1024 字节单包边界、超限拒绝、语义分包，以及 4096 字节 Device 接收边界。
- 分包乱序、跨通道隔离、相同重发和冲突重发。
- 列表分包的 protobuf 合并语义。
- 7 组生成 schema 的导入和代表性 v0.1.2 字段往返。
- 文档中 `max_length`、`max_size` 和 `max_count` 边界。
- BLE 大请求分包、多包响应重组、并发请求反序回复路由。
- 普通发送、阻塞请求和 Device 主动消息接收。

最终回归命令与结果：

```text
uv run python -m pytest -q tests/test_watch_app_protocol.py tests/test_watch_ble.py tests/test_watch_ble_screenshot.py tests/test_watch_ble_screenshot_client.py
69 passed, 30 subtests passed
```

没有执行真机扫描、连接、配对、绑定、登录、文件传输、截图或刷机。MTP 仍是真机截图默认链路；PB/BLE 主机测试不能替代真机产品证据。
