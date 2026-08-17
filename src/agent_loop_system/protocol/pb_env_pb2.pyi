from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _AuthRequest(_message.Message):
    __slots__ = ("userId", "system", "version", "brand", "model", "authCode", "user", "time")
    USERID_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    BRAND_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    AUTHCODE_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    userId: str
    system: int
    version: str
    brand: str
    model: str
    authCode: str
    user: _UserInfo
    time: _TimeInfo
    def __init__(self, userId: _Optional[str] = ..., system: _Optional[int] = ..., version: _Optional[str] = ..., brand: _Optional[str] = ..., model: _Optional[str] = ..., authCode: _Optional[str] = ..., user: _Optional[_Union[_UserInfo, _Mapping]] = ..., time: _Optional[_Union[_TimeInfo, _Mapping]] = ...) -> None: ...

class _AuthResponse(_message.Message):
    __slots__ = ("result", "everBind", "bindTime")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    EVERBIND_FIELD_NUMBER: _ClassVar[int]
    BINDTIME_FIELD_NUMBER: _ClassVar[int]
    result: int
    everBind: int
    bindTime: int
    def __init__(self, result: _Optional[int] = ..., everBind: _Optional[int] = ..., bindTime: _Optional[int] = ...) -> None: ...

class _UserInfo(_message.Message):
    __slots__ = ("gender", "age", "height", "weight")
    GENDER_FIELD_NUMBER: _ClassVar[int]
    AGE_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    gender: int
    age: int
    height: int
    weight: float
    def __init__(self, gender: _Optional[int] = ..., age: _Optional[int] = ..., height: _Optional[int] = ..., weight: _Optional[float] = ...) -> None: ...

class _TimeInfo(_message.Message):
    __slots__ = ("timestamp", "zoneOffset")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ZONEOFFSET_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    zoneOffset: int
    def __init__(self, timestamp: _Optional[int] = ..., zoneOffset: _Optional[int] = ...) -> None: ...

class _UnAuthResponse(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: int
    def __init__(self, result: _Optional[int] = ...) -> None: ...

class _BatteryInfo(_message.Message):
    __slots__ = ("level", "charging")
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    CHARGING_FIELD_NUMBER: _ClassVar[int]
    level: int
    charging: int
    def __init__(self, level: _Optional[int] = ..., charging: _Optional[int] = ...) -> None: ...

class _LanguageInfo(_message.Message):
    __slots__ = ("language",)
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    language: int
    def __init__(self, language: _Optional[int] = ...) -> None: ...

class _AppStatus(_message.Message):
    __slots__ = ("isForeground", "permissionSms", "permissionLocation", "permissionCamera")
    ISFOREGROUND_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONSMS_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONLOCATION_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONCAMERA_FIELD_NUMBER: _ClassVar[int]
    isForeground: int
    permissionSms: int
    permissionLocation: int
    permissionCamera: int
    def __init__(self, isForeground: _Optional[int] = ..., permissionSms: _Optional[int] = ..., permissionLocation: _Optional[int] = ..., permissionCamera: _Optional[int] = ...) -> None: ...

class _LanguageList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: bytes
    def __init__(self, items: _Optional[bytes] = ...) -> None: ...

class _LocationInfo(_message.Message):
    __slots__ = ("longitude", "latitude", "snr")
    LONGITUDE_FIELD_NUMBER: _ClassVar[int]
    LATITUDE_FIELD_NUMBER: _ClassVar[int]
    SNR_FIELD_NUMBER: _ClassVar[int]
    longitude: int
    latitude: int
    snr: int
    def __init__(self, longitude: _Optional[int] = ..., latitude: _Optional[int] = ..., snr: _Optional[int] = ...) -> None: ...

class _BluetoothInfo(_message.Message):
    __slots__ = ("ble_mac", "ble_name", "ble_status", "bt_mac", "bt_name", "bt_status")
    BLE_MAC_FIELD_NUMBER: _ClassVar[int]
    BLE_NAME_FIELD_NUMBER: _ClassVar[int]
    BLE_STATUS_FIELD_NUMBER: _ClassVar[int]
    BT_MAC_FIELD_NUMBER: _ClassVar[int]
    BT_NAME_FIELD_NUMBER: _ClassVar[int]
    BT_STATUS_FIELD_NUMBER: _ClassVar[int]
    ble_mac: str
    ble_name: str
    ble_status: int
    bt_mac: str
    bt_name: str
    bt_status: int
    def __init__(self, ble_mac: _Optional[str] = ..., ble_name: _Optional[str] = ..., ble_status: _Optional[int] = ..., bt_mac: _Optional[str] = ..., bt_name: _Optional[str] = ..., bt_status: _Optional[int] = ...) -> None: ...
