"""Windows 串口设备枚举与 SuperCom AgentBridge 命名管道状态探测。

提供无物理串口副作用的只读探测能力：
1. 枚举 Windows 设备管理器中的串口设备（分类 USB 与系统/板载）；
2. 共享 SuperCom 命名管道命名规范（SuperCom.AgentBridge.<normalized_port>）；
3. 使用 Windows WaitNamedPipeW API 探测 SuperCom 管道状态（available / busy / absent）；
4. 提供确定性排序与默认选中规则。
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Any


SUPERCOM_PIPE_PREFIX = "SuperCom.AgentBridge."


def normalize_serial_port_name(port: str) -> str:
    """标准化串口名称，例如 'com7' -> 'COM7', '\\\\.\\COM7' -> 'COM7'。"""
    raw = str(port or "").strip()
    if raw.startswith("\\\\.\\"):
        raw = raw[4:]
    return raw.upper()


def get_supercom_pipe_name(port: str) -> str:
    """生成与 SuperComPipeTransport 严格一致的命名管道名称。"""
    if not port or "\r" in port or "\n" in port:
        raise ValueError(f"invalid serial port: {port!r}")
    safe_port = "".join(
        value if value.isalnum() or value in "_-" else "_"
        for value in port.strip().upper()
    )
    return f"{SUPERCOM_PIPE_PREFIX}{safe_port}"


def get_supercom_pipe_path(port: str) -> str:
    """生成与 SuperComPipeTransport 严格一致的命名管道绝对路径。"""
    return rf"\\.\pipe\{get_supercom_pipe_name(port)}"


def check_supercom_pipe_open(port: str, timeout_ms: int = 10) -> bool:
    """探测指定串口的 SuperCom 命名管道是否开启。

    只读探测，绝不打开物理 COM 端口。
    使用 Windows WaitNamedPipeW API：
    - 返回非零（可用）或错误码为 121 (SEM_TIMEOUT) / 231 (PIPE_BUSY) /
      5 (ACCESS_DENIED) / 535 (PIPE_CONNECTED) / 536 (PIPE_LISTENING)：
      视为管道存在（SuperCom 已开启）；
    - 其他任何错误码（包括 2 ERROR_FILE_NOT_FOUND 以及未知错误）：返回 False。
    """
    if os.name != "nt":
        return False
    if not port:
        return False

    try:
        pipe_path = get_supercom_pipe_path(port)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL

        res = kernel32.WaitNamedPipeW(pipe_path, timeout_ms)
        if res:
            return True

        err = ctypes.get_last_error()
        if err in (121, 231, 5, 535, 536):
            return True
        return False
    except Exception:
        return False


@dataclass(frozen=True)
class SerialPortInfo:
    """串口设备信息与 SuperCom 桥接状态。"""

    port: str
    friendly_name: str
    description: str = ""
    hardware_id: str | None = None
    manufacturer: str | None = None
    kind: str = "usb"  # "usb" | "system" | "unknown"
    kind_label: str = "USB 串口"  # "USB 串口" | "系统/板载串口" | "不可用"
    present: bool = True
    supercom_open: bool = False
    pipe_name: str = ""
    pipe_path: str = ""
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# SetupAPI ctypes structures
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


# GUID for Ports class: {4D36E978-E325-11CE-BFC1-08002BE10318}
_GUID_DEVCLASS_PORTS = _GUID(
    0x4D36E978,
    0xE325,
    0x11CE,
    (wintypes.BYTE * 8)(0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18),
)

_DIGCF_PRESENT = 0x00000002
_SPDRP_DEVICEDESC = 0x00000000
_SPDRP_HARDWAREID = 0x00000001
_SPDRP_MFG = 0x0000000B
_SPDRP_FRIENDLYNAME = 0x0000000C

_DICS_FLAG_GLOBAL = 0x00000001
_DIREG_DEV = 0x00000001
_KEY_READ = 0x20019


def _classify_port_kind(
    port: str,
    friendly_name: str,
    description: str,
    hardware_id: str,
    manufacturer: str,
) -> tuple[str, str]:
    """根据硬件特征分类串口类型 (USB vs 系统/板载 vs 其他/未知)。"""
    haystack = f"{hardware_id} {friendly_name} {description} {manufacturer}".upper()

    # 明确匹配 USB / 常见 USB-串口芯片
    is_usb = False
    if any(
        token in haystack
        for token in (
            "USB",
            "FTDI",
            "FTDIBUS",
            "VID_",
            "CH340",
            "CH341",
            "CP210",
            "PL2303",
            "SILAB",
            "PROLIFIC",
            "VCP",
        )
    ):
        is_usb = True

    # 明确匹配主板/系统/ACPI 端口
    is_system = False
    if (
        hardware_id.upper().startswith("ACPI\\")
        or hardware_id.upper().startswith("ROOT\\")
        or "*PNP05" in haystack
        or "PNP0501" in haystack
        or "COMMUNICATIONS PORT" in haystack
        or "通信端口" in haystack
    ):
        if not is_usb:
            is_system = True

    if is_system or (not is_usb and port == "COM1"):
        return "system", "系统/板载串口"
    if is_usb:
        return "usb", "USB 串口"
    return "unknown", "其他串口"


def _enumerate_ports_setupapi() -> list[dict[str, Any]]:
    """使用 SetupAPI 枚举当前已连接的设备管理器串口。"""
    if os.name != "nt":
        return []

    results: list[dict[str, Any]] = []
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(_GUID),
            wintypes.LPCWSTR,
            wintypes.HWND,
            wintypes.DWORD,
        ]
        setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

        setupapi.SetupDiEnumDeviceInfo.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(_SP_DEVINFO_DATA),
        ]
        setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL

        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVINFO_DATA),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPBYTE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL

        setupapi.SetupDiOpenDevRegKey.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_SP_DEVINFO_DATA),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        setupapi.SetupDiOpenDevRegKey.restype = wintypes.HKEY

        advapi32.RegQueryValueExW.argtypes = [
            wintypes.HKEY,
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPBYTE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.RegQueryValueExW.restype = wintypes.LONG

        advapi32.RegCloseKey.argtypes = [wintypes.HKEY]
        advapi32.RegCloseKey.restype = wintypes.LONG

        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        h_dev_info = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(_GUID_DEVCLASS_PORTS),
            None,
            None,
            _DIGCF_PRESENT,
        )
        if h_dev_info == ctypes.c_void_p(-1).value or not h_dev_info:
            return []

        try:
            dev_index = 0
            dev_info_data = _SP_DEVINFO_DATA()
            dev_info_data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)

            def get_prop(prop_id: int) -> str:
                prop_buf = ctypes.create_unicode_buffer(512)
                prop_type = wintypes.DWORD()
                req_size = wintypes.DWORD()
                ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
                    h_dev_info,
                    ctypes.byref(dev_info_data),
                    prop_id,
                    ctypes.byref(prop_type),
                    ctypes.cast(prop_buf, wintypes.LPBYTE),
                    1024,
                    ctypes.byref(req_size),
                )
                return prop_buf.value.strip() if ok else ""

            while setupapi.SetupDiEnumDeviceInfo(
                h_dev_info, dev_index, ctypes.byref(dev_info_data)
            ):
                dev_index += 1

                friendly_name = get_prop(_SPDRP_FRIENDLYNAME)
                device_desc = get_prop(_SPDRP_DEVICEDESC)
                hardware_id = get_prop(_SPDRP_HARDWAREID)
                manufacturer = get_prop(_SPDRP_MFG)

                # 打开注册表获取精确的 PortName
                port_name = ""
                h_key = setupapi.SetupDiOpenDevRegKey(
                    h_dev_info,
                    ctypes.byref(dev_info_data),
                    _DICS_FLAG_GLOBAL,
                    0,
                    _DIREG_DEV,
                    _KEY_READ,
                )
                if h_key and h_key != ctypes.c_void_p(-1).value:
                    try:
                        val_buf = ctypes.create_unicode_buffer(256)
                        val_size = wintypes.DWORD(512)
                        val_type = wintypes.DWORD()
                        rc = advapi32.RegQueryValueExW(
                            h_key,
                            "PortName",
                            None,
                            ctypes.byref(val_type),
                            ctypes.cast(val_buf, wintypes.LPBYTE),
                            ctypes.byref(val_size),
                        )
                        if rc == 0:
                            port_name = val_buf.value.strip().upper()
                    finally:
                        advapi32.RegCloseKey(h_key)

                if not port_name and friendly_name:
                    match = re.search(r"\((COM\d+)\)", friendly_name, re.IGNORECASE)
                    if match:
                        port_name = match.group(1).upper()

                if not port_name or not port_name.startswith("COM"):
                    continue

                if not friendly_name:
                    friendly_name = f"{device_desc or 'Serial Port'} ({port_name})"

                kind, kind_label = _classify_port_kind(
                    port_name,
                    friendly_name,
                    device_desc,
                    hardware_id,
                    manufacturer,
                )

                results.append({
                    "port": port_name,
                    "friendly_name": friendly_name,
                    "description": device_desc,
                    "hardware_id": hardware_id,
                    "manufacturer": manufacturer,
                    "kind": kind,
                    "kind_label": kind_label,
                    "present": True,
                })
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(h_dev_info)

    except Exception:
        return []

    return results


def _enumerate_ports_winreg() -> list[dict[str, Any]]:
    """使用 winreg 从 HARDWARE\\DEVICEMAP\\SERIALCOMM 读取串口备选列表。"""
    if os.name != "nt":
        return []
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DEVICEMAP\SERIALCOMM",
        )
    except OSError:
        return []

    results = []
    try:
        index = 0
        while True:
            try:
                val_name, val_data, _ = winreg.EnumValue(key, index)
                index += 1
                port = str(val_data).strip().upper()
                if not port.startswith("COM"):
                    continue
                is_usb = any(
                    token in val_name.upper()
                    for token in ("USB", "FTDI", "VCP", "SILAB", "CH34")
                )
                is_system = any(
                    token in val_name.upper()
                    for token in ("SERIAL", "ACPI", "PNP05")
                ) or port == "COM1"
                if is_usb:
                    kind, kind_label = "usb", "USB 串口"
                    desc = "USB 串口设备"
                elif is_system:
                    kind, kind_label = "system", "系统/板载串口"
                    desc = "通信端口"
                else:
                    kind, kind_label = "unknown", "其他串口"
                    desc = "串口设备"
                friendly_name = f"{desc} ({port})"
                results.append({
                    "port": port,
                    "friendly_name": friendly_name,
                    "description": desc,
                    "hardware_id": val_name,
                    "manufacturer": "USB" if is_usb else ("Standard" if is_system else "Unknown"),
                    "kind": kind,
                    "kind_label": kind_label,
                    "present": True,
                })
            except OSError:
                break
    finally:
        winreg.CloseKey(key)
    return results


def enumerate_serial_ports() -> list[dict[str, Any]]:
    """枚举当前 Windows 设备管理器串口，合并 SetupAPI 与注册表信息。"""
    ports = _enumerate_ports_setupapi()
    if not ports:
        ports = _enumerate_ports_winreg()
    else:
        existing_ports = {item["port"] for item in ports}
        for fallback_item in _enumerate_ports_winreg():
            if fallback_item["port"] not in existing_ports:
                ports.append(fallback_item)
                existing_ports.add(fallback_item["port"])
    return ports


def _port_sort_key(item: SerialPortInfo) -> tuple[int, int, int, str]:
    """确定性排序键：
    1. supercom_open: True (0) 在前，False (1) 在后
    2. kind: "usb" (0) 在前，"system" (1) 在后，"unknown" (2)
    3. COM 编号数值大小 (COM1 -> 1, COM7 -> 7)
    4. friendly_name 字母序
    """
    open_rank = 0 if item.supercom_open else 1
    kind_rank = 0 if item.kind == "usb" else (1 if item.kind == "system" else 2)

    port_num = 99999
    if item.port.startswith("COM") and item.port[3:].isdigit():
        port_num = int(item.port[3:])

    return (open_rank, kind_rank, port_num, item.friendly_name)


def get_serial_ports_status(
    configured_port: str | None = None,
    *,
    probe_fn=check_supercom_pipe_open,
    enumeration_fn=enumerate_serial_ports,
) -> dict[str, Any]:
    """读取串口列表并探测 SuperCom 管道状态，执行确定性排序与默认选择计算。

    - 排序规则：活动 SuperCom 串口 > 其他 USB 串口 > 系统/板载串口 (COM1 在底部)
    - 选中规则：
      1. 恰有 1 个 SuperCom 开启的串口 -> 默认自动选中并显示该端口
      2. 多个 SuperCom 开启的串口 -> 若当前配置的端口就在其中则保留，否则需显式选择
      3. 无 SuperCom 开启的串口 -> 显示明确未开启状态，绝不默认自动选中 COM1
      4. 当前配置但目前未检测到的端口 -> 保持在列表中显示为“(未检测到/不可用)”，防止设置静默丢失
    """
    clean_configured = (
        normalize_serial_port_name(configured_port) if configured_port else ""
    )
    discovered_raw = enumeration_fn()

    items: list[SerialPortInfo] = []
    seen_ports: set[str] = set()

    for raw in discovered_raw:
        port = raw["port"]
        if port in seen_ports:
            continue
        seen_ports.add(port)

        is_open = bool(probe_fn(port))
        items.append(
            SerialPortInfo(
                port=port,
                friendly_name=raw.get("friendly_name") or f"Serial Port ({port})",
                description=raw.get("description", ""),
                hardware_id=raw.get("hardware_id"),
                manufacturer=raw.get("manufacturer"),
                kind=raw.get("kind", "usb"),
                kind_label=raw.get("kind_label", "USB 串口"),
                present=True,
                supercom_open=is_open,
                pipe_name=get_supercom_pipe_name(port),
                pipe_path=get_supercom_pipe_path(port),
                missing=False,
            )
        )

    # 如果配置的端口当前未被探测到，保留为一个不可用选项，防止配置丢失
    if clean_configured and clean_configured not in seen_ports:
        is_open = bool(probe_fn(clean_configured))
        items.append(
            SerialPortInfo(
                port=clean_configured,
                friendly_name=f"{clean_configured} (未检测到设备 / 不可用)",
                description="未检测到设备",
                hardware_id=None,
                manufacturer=None,
                kind="unknown",
                kind_label="不可用",
                present=False,
                supercom_open=is_open,
                pipe_name=get_supercom_pipe_name(clean_configured),
                pipe_path=get_supercom_pipe_path(clean_configured),
                missing=True,
            )
        )

    # 确定性排序
    items.sort(key=_port_sort_key)

    active_ports = [
        item.port for item in items if item.supercom_open and item.present
    ]
    active_count = len(active_ports)

    # 默认/选中端口计算
    selected_port: str | None = None
    default_port: str | None = None

    if active_count == 1:
        selected_port = active_ports[0]
        default_port = active_ports[0]
    elif active_count > 1:
        if clean_configured in active_ports:
            selected_port = clean_configured
            default_port = clean_configured
        else:
            selected_port = ""
            default_port = None
    else:  # active_count == 0
        if clean_configured:
            selected_port = clean_configured
        else:
            selected_port = ""
        default_port = None

    return {
        "configured_port": clean_configured or configured_port or "",
        "selected_port": selected_port or "",
        "default_port": default_port,
        "active_count": active_count,
        "items": [item.to_dict() for item in items],
        "available": True,
    }
