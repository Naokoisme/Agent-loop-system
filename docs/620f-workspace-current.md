# 620F 电子吧唧工作区现状

> 记录时间：2026-08-26（Asia/Shanghai）
> 状态：`OFFICIAL_V2.6.3 / AGENT_LOOP_BRIDGE_BUILT / PACKAGED / RUNNER_REGISTERED / HARDWARE_NOT_VERIFIED`

## 结论

620F 当前 Agent-loop 固件工作区是：
`D:\Agent-loop\workspaces\firmware\620F_W7830_v2.6.3`。

该工作区基于 GitLab 正式标签 `620F_W7830_v2.6.3`，已保留上游“支持手机动态调整焦距范围”
更新，并完成 QuickCmd、GUI 栅栏、窗口树、测试会话、MTP 文件截图和提示窗口数字参数适配。
C500、Ramrun、BL1、C400 及最终 UP3/OTA 打包已经通过。

旧目录 `D:\Agent-loop\workspaces\firmware\620F_W7830` 继续保留为早期本机缓存基线，
不是当前工作区，不应继续在其中开发或构建。

## 正式源码基线

| 项目 | 固定值 |
| --- | --- |
| 当前工作区 | `D:\Agent-loop\workspaces\firmware\620F_W7830_v2.6.3` |
| 根仓标签 | `620F_W7830_v2.6.3` |
| 根仓提交 | `15dbcb30cf6349eacd319e95d4d7be2f93f5307d` |
| `app` | `c065b8ac55a0f0bad1763af27dbe694f18c1ccae` |
| `core/comm` | `a56b4b6722d05fa55a4a1763cdd5fe0b84dcbf19` |
| `core/gui` | `816eac0f97b641b2a6237fc2f008d130be901124` |
| `core/lvgl` | `486231089fb6ab32cbab6a5ed488055847074866` |
| `core/platform_driver` | `b37039526449141cb205f99b7064dda6369f22e8` |
| 活动项目 | `app/ProjectConfig.cmake` 中的 `set(PROJECT 620F_W7830)` |

正式版本上游功能已确认保留：
`CameraInfo.zoomRatioMin`、`CameraInfo.zoomRatioMax` 以及相机界面的动态 Min/1X/2X/3X/Max 焦距范围。

## Agent-loop 本地适配

本地未提交改动包括：

- `CONFIG_TOPSTEP_AGENT_LOOP_AUTOMATION` 工程开关及独立 overlay：
  `projects/tb_watch/agent_loop_automation.conf`。
- QuickCmd 协议收敛和参数校验：
  `TEST_SESSION`、`GUI_PING`、`GUI_TREE`、`SCREENSHOT_CAPTURE_FILE`、
  `ENTER_PAGE`、触摸命令。
- 620F 窗口参数适配，采用数字方案编号：
  `COMMON_BUTTON_TIP,1`、`COMMON_TIP,1`、
  `COMMON_TITLE_BUTTON_TIP,1/2/3`、`LOW_BATTERY_TIP,1/2`。
- 真机截图底层链路：VDE 原始 BGR888 捕获、BMP 文件写入、MTP 文件索引刷新。
- 620F 命令表：
  `D:\Agent-loop\workspaces\firmware\620F_W7830_v2.6.3\docs\620F命令映射表.txt`。

命令表重新按 v2.6.3 源码核对：71 条可直接发送命令均唯一，覆盖 65 个已注册窗口 ID；
`WHETHER_TO_BIND`、`LOW_POWER_SHUTDOWN_TIP`、`POWER_OFF` 仍明确不作为普通数字命令发送。

## 构建与产物验证

最终 C500/签名/打包命令：

```powershell
python .\build.py -b w30_wcs -c500 -v 2.6.3 projects\tb_watch -- -DOVERLAY_CONFIG=agent_loop_automation.conf
```

Agent-loop 构建配置已确认：

- `CONFIG_TOPSTEP_AGENT_LOOP_AUTOMATION=y`
- `CONFIG_SYS_HEAP_RUNTIME_STATS=y`
- `CONFIG_SHELL=y`
- `CONFIG_DEBUG_UNWIND=y`

ELF 已确认包含：
`gui_comm_watch_capture_file_request`、`hwd_raw_capture_start`、
`hwd_raw_capture_finish`、`hwd_raw_capture_cancel`、`mtp_mark_storage_changed`。

主要产物位于：
`D:\Agent-loop\workspaces\firmware\620F_W7830_v2.6.3\projects\tb_watch\build\zephyr`。

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `zephyr_sig.bin` | 2,247,592 | `37F3FBEA50D946E3A1F79E01C3B9B373D35FCEEC50F132A7F843702E59346F8E` |
| `w30.up3` | 35,348,480 | `402C3CD82EBD06F9B3383BF583C02A572D035CFD06CB6241011F2B0D5B8CC1E5` |
| `w30_ota.up3` | 3,624,960 | `0C3752A1FE7C30B3AE1D570528BD5B62FDEC923F47A1E8E206C0D884C218DBD6` |
| `w30_ota_full.up3` | 35,215,360 | `787475AFDD410B5B19A0A26A63ED1BEC3CCBAF5166B22A18D2AA0FCA4D79C4EB` |
| `w30_2.6.3.zip` | 119,572,227 | `9AA154547DADEB431CD9E253E861147D622D7DA44A53F11042B2DDB58E8AF05E` |

`w30.up3` 已核对包含 `ramrun.up`、`bl1.up`、签名后的 `zephyr.bin` 和 NAND 资源；
`w30_ota.up3` 已核对包含 `zephyr.bin`、`c400.bin`、模型和 `bl1-ota.up`；
`w30_2.6.3.zip` 完整性测试通过。官方打包脚本提示 `zephyr.lst` 未生成并跳过，
但固件、OTA 和 `zephyr.map` 均已进入版本归档。

## 当前边界与下一步

当前已完成源码工作区、Agent-loop 协议移植、数字参数方案、编译、签名、打包及 Agent-loop 注册：

- 项目 ID：`620F_W7830`
- 真机目标：`w30.620f.hardware`
- Runtime Profile：`profiles/620F_W7830/releases/v2.6.3-agentloop.2`
- 独立用例目录：`case_map/620f_case_map`（当前为空，不复用 6202 数据）
- 截图规格：`466 × 466`
- Runtime Profile 已通过清单、文件大小及 SHA-256 自校验；绑定固件 SHA-256 为
  `402C3CD82EBD06F9B3383BF583C02A572D035CFD06CB6241011F2B0D5B8CC1E5`。

尚未做以下工作：

1. 未刷机，也未连接或控制 620F 真机。
2. 未验证真机上的 `GUI_PING`、`GUI_TREE`、测试会话和 MTP 截图闭环。
3. 620F 独立用例目录目前没有正式用例，因此前端会显示项目和执行目标，但用例数为 0。
4. 本地适配尚未提交或推送，远端正式标签和旧工作区均未改动。

下一阶段应在用户明确授权刷机/真机操作后，先验证最小协议和截图闭环，再基于 620F 真机
独立探索并固化第一批用例；不能复制 6202 的命令坐标、截图或 verdict。
