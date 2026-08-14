# case_map — 按目标隔离的测试用例操作映射

> 用途：让 `case_map.py` 按 `setup -> actions -> collect` 执行用例。
> last_verified: 2026-08-14

## 目录隔离

```text
case_map/
├─ 620C_case_map/              # 620C_W6830 Windows Simulator
├─ 6202_case_map/              # 6202_W5230 真机
└─ 6202_simulator_case_map/    # 6202_W5230 Windows Simulator
```

- `profile=620C_W6830` 读取 `620C_case_map`。
- `profile=6202_W5230` 读取 `6202_case_map`。
- `profile=6202_W5230_SIMULATOR` 读取 `6202_simulator_case_map`。
- 两个模拟器 profile 都复用 `target=simulator` 传输，但源码、产物和 case_map 彼此隔离。
- 目标目录缺少模块时必须明确报错，不得回退另一套映射。
- 两套映射保留相同 `case_id`，运行结果另带 target，不使用 `HW_` 改写用例编号。

## 数据原则

- 每个 sheet 一个 JSON，用例原文保留在 `precondition_text`、`steps_text`、`expected_text`。
- JSON 是经过审核的数据，不再由 `translate_*.py` 批量生成。
- 命令名和窗口名必须来自当前 `W30_SOURCE_ROOT/W30_PROJECT` 的真实源码。
- 参数含义由固件 handler 定义；本地只检查命令格式、128 字节限制、注入风险、是否注册及是否明确不可用。
- 坐标必须来自当前界面的 GUI 树和实际点击验证，不从历史脚本猜测。
- 截图是产品 PASS/FAIL/CANNOT_VERIFY 的唯一判决证据；GUI 树、命令回执和日志只用于诊断页面、操作、时序与环境。
- 6202 映射不得包含 `SIM_*`、`SCREENSHOT_PRINT`；检查点使用 `GUI_TREE`，截图由真机会话通过 MTP 保存。
- 6202 模拟器映射同样不得借用 620C 的 `SIM_*`；检查点统一使用 `HOST_SCREENSHOT` 捕获真实窗口像素，产品结论只看截图。
- 6202 需要纯主机等待时使用 `HOST_WAIT:milliseconds`，执行器只在主机等待，不向固件发送该伪命令。
- 6202 坐标必须来自当前 `6202_W5230` 窗口版本源码并经真机点击复核，不能复用620C坐标。
- Windows 模拟器的步数、界面 Cal 和距离统一使用 `SIM_ACTIVITY_SET:seq,profile,steps,calories,distance`；`STEP/CALORIES/DISTANCE` 只保留给非 Windows 真机测试兼容，禁止新增到 case_map。
- `SIM_ACTIVITY_SET` 的卡路里单位是界面显示的 Cal，不是旧 `CALORIES` 命令使用的千倍内部值；活动时长仍使用 `EXERCISE_TIME`。
- QR Hub 的 UPI、WiFi、Movie 测试数据统一使用 `SIM_QR_HUB_SET:seq,upi|wifi|movie,content`；`content=-` 清空该类型。该命令仅在 Windows 模拟器注册，写入与 App 同一套钱包卡片存储并发送真实刷新事件。
- `TIME_SET` 接受 `YYMMDDHHMMSS`（12 位，年份按 2000～2099 解释）和 `YYYYMMDDHHMMSS`（14 位）；不再把 12 位年份误读成前四位。
- 当前天气的完整模拟数据使用 `SIM_WEATHER_SET_EXT:seq,city,code,type,current,min,max,visibility,uv,wind_scale,wind_speed,humidity,aqi,future_days`。该命令仅在 Windows 模拟器注册，通过天气服务的保存/刷新路径建立最高低温、风速、湿度、AQI 与多日预报；`future_days` 为 0～7。
- OTA 进度和阶段事件使用 `SIM_OTA_EVENT:seq,start|complete|fail|cancel` 或 `SIM_OTA_EVENT:seq,progress,0..100`。该命令仅在 Windows 模拟器注册，发送真实 OTA 服务事件，不直接改页面控件。
- 工厂模式业务状态使用 `SIM_FACTORY_MODE_SET:seq,0|1`。该命令仅在 Windows 模拟器注册，通过工厂服务切换模式；它不直接打开页面，页面进入仍使用真实操作路径，必要时由通用 `ENTER_PAGE` 只负责导航。
- 用户设置前置状态使用 `SIM_USER_SETTING_SET:seq,setting,value`。该命令仅在 Windows 模拟器注册；`setting` 支持 `dnd_enable`、`dnd_mode`、`dnd_start`、`dnd_end`、`mute`、`wrist_wake`、`aod_enable`，以及 Profile 使用的 `gender`（0女/1男/2未知）、`height`（0表示未配置，或62..275cm）、`weight`（0表示未配置，或1..500kg整数测试值）、`length_unit`（0公制/1英制）、`weight_unit`（0公制/1英制）、`birth_year`（1900..2099）。命令只建立前置业务状态，并通过已有设置/用户资料事件刷新；页面操作仍使用真实点击、滑动或编码器输入，不会向手机 App 同步测试状态。
- 运动记录前置使用 `CLEAR_ALL_SPORT_RECORD` 清空后，再用 `SET_SPORT_RECORD_DATA:sport_id[,count]` 创建 1..20 条同类型记录；`count` 省略时为 1。该命令只在 PC 模拟器测试路径注册，批量创建仍逐条走真实运动记录保存接口，不直接修改运动记录列表控件。
- 编码器命令 `QDEC_SET:is_inc[,repeat_count]` 支持可选的 `1..256` 次重复输入；省略次数时保持单步行为。它用于长滚轮的通用边界操作，不直接改写滚轮值。
- 实体按键统一使用 `BUTTON_PRESS:key_index,press_type,press_time`：`press_type=1` 单击、`2` 完整长按（`press_time` 为毫秒，模拟器按顺序发送按下、长按、保持、释放）、`3` 双击、`4` 仅按下、`5` 长按释放。普通长按优先使用类型2；需要在持续按住期间截图时，才使用类型4和类型5分开控制。执行器会按类型2的持续时间放宽命令超时。
- Windows模拟器处于熄屏时，首个 `BUTTON_PRESS` 只用于点亮屏幕，不再同时把该次按键送给当前页面；这与真实设备的首键唤醒语义一致。
- 屏幕长按不是实体按键长按：使用 `TP_PRESS:x,y,1` 按下、`SIM_WAIT` 保持、`TP_PRESS:x,y,0` 释放。
- `SIM_WAIT:seq,milliseconds` 支持 0～120000ms；更长等待拆成多个不超过120000ms的片段。执行器按等待时长动态放宽命令超时，不再使用固定5秒超时误杀长等待。
- `SIM_WAIT`、`GUI_PING`、`GUI_TREE`、`SCREENSHOT_PRINT`、`GUI_STATE` 等等待/观察命令不得改变亮灭屏状态；主动交互命令仍沿用原有的唤醒行为。
- 计时器前置状态使用 `SIM_TIMER_STATE_SET:seq,idle|running|paused|finished,total_seconds,remain_seconds`。该命令仅在 Windows 模拟器注册，通过共享计时器业务模块构造状态，不直接写页面控件；`idle` 要求两个时间均为 0，`running/paused` 要求 `1 <= remain_seconds <= total_seconds <= 86400`，`finished` 要求 `remain_seconds=0`，并走真实计时结束回调进入提醒态。
- 闹钟前置状态使用 `SIM_ALARM_SET:seq,index,hour,minute,repeat_mask,status`。该命令仅在 Windows 模拟器注册，`index` 为 0～9，按索引写入共享闹钟列表并发送真实闹钟刷新事件；需要空列表时先用 `CLEAN_ALARM`。它不直接操作或伪造页面控件。
- 历史通话记录使用 `SIM_CALL_RECORD_SET:seq,name,number,type,age_seconds`。该命令仅在 Windows 模拟器注册，以当前 RTC 为基准写入指定秒数前的记录并复用真实保存/刷新路径；`name=_` 表示无备注。它只建立历史记录，不能代替真实手机来电、接听、拒接、Active Call 或双向音频。
- `SET_CONTACTS_TEST_DATA` 的 `name=_` 表示无备注联系人，写入电话簿时名称为空，不再把下划线显示到联系人列表。

## JSON 结构

```json
{
  "case_id": "HR_008",
  "sheet": "心率",
  "priority": "P0",
  "precondition_text": "原始前置条件",
  "steps_text": "原始步骤",
  "expected_text": "原始预期",
  "setup": [
    "srv_quick_cmd send TOP5STEP:WEAR_SET:9601,1;",
    "srv_quick_cmd send TOP5STEP:SIM_SENSOR_SET:9602,hr,silent,0,0;",
    "srv_quick_cmd send TOP5STEP:ENTER_PAGE:HEART_RATE,0;"
  ],
  "actions": ["srv_quick_cmd send TOP5STEP:TP_CLICK:312,202,1;"],
  "collect": [
    "srv_quick_cmd send TOP5STEP:SCREENSHOT_PRINT:;",
    "srv_quick_cmd send TOP5STEP:GUI_TREE:1;"
  ],
  "unable": false,
  "note": "命令和坐标的核查说明"
}
```

## 执行规则

- `setup`：先准备业务数据，再进入目标页面。
- `actions`：触发用例需要的操作。
- 每条 setup/action 后，执行器自动发 `GUI_PING` 并等待 `processed`；`accepted` 只代表入队。
- `ENTER_PAGE` 的处理回执早于窗口动画结束；执行器在屏障后统一等待 1 秒，再发送下一条输入，避免吞掉第一下触控。
- `collect`：收集截图和 GUI 树；不要把“最后一条必须是 GUI_TREE”当成阻断规则。
- `unable=true`：`actions` 必须为空，`note` 写清真实原因。不得保留虚假命令来伪装可执行。

## unable 的真实边界

下列情况可以标记 unable：

- 必须依赖手机 App、BLE、扫码、真实传感器或可编程电源。
- 当前源码没有所需的数据注入或触发命令。
- 只能通过未验证的屏幕外坐标或猜测操作达到。
- 用例要求的外部设计稿、Designer/Figma 信息尚未接入。

“脚本没写规则”、“当时没找到窗口”、“还没实测”不是永久 unable 理由，应先对照当前源码和实际模拟器复核。

## 维护和验证

1. 从当前真实源码生成能力目录：`.\.venv\Scripts\python.exe -m sim_tools.extract_kb`。
2. 逐条检查 JSON 命令格式、命令注册、窗口注册和明确不可用状态。
3. 用模拟器跑代表性 case，核对截图、GUI 树和固件回执。
4. 修改坐标时，必须重新采集目标界面，不得复用其他页面的“看起来差不多”坐标。
5. 全量单测：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。

## 当前工具

- `sim_tools/extract_kb.py`：只从当前固件源码提取命令和窗口能力。
- `sim_tools/collect.py`：采集 GUI 状态/树和坐标证据。
- `sim_tools/sim_client.py`：模拟器通信工具。
- `src/agent_loop_system/tools/case_map.py`：执行已审核的 JSON。
