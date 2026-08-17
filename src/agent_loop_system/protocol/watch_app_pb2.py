"""Backward-compatible aliases for the complete PB environment schema.

New code should import :mod:`agent_loop_system.protocol.pb_env_pb2`. This
module remains so existing authentication callers keep their original import
path while sharing the single generated schema definition.
"""

from agent_loop_system.protocol.pb_env_pb2 import (
    DESCRIPTOR,
    _AppStatus,
    _AuthRequest,
    _AuthResponse,
    _BatteryInfo,
    _BluetoothInfo,
    _LanguageInfo,
    _LanguageList,
    _LocationInfo,
    _TimeInfo,
    _UnAuthResponse,
    _UserInfo,
)

__all__ = [
    "DESCRIPTOR",
    "_AppStatus",
    "_AuthRequest",
    "_AuthResponse",
    "_BatteryInfo",
    "_BluetoothInfo",
    "_LanguageInfo",
    "_LanguageList",
    "_LocationInfo",
    "_TimeInfo",
    "_UnAuthResponse",
    "_UserInfo",
]
