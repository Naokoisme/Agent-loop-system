from agent_loop_system.protocol import pb_config_pb2 as _pb_config_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _CameraAction(_message.Message):
    __slots__ = ("action",)
    ACTION_FIELD_NUMBER: _ClassVar[int]
    action: int
    def __init__(self, action: _Optional[int] = ...) -> None: ...

class _MediaVolume(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: int
    def __init__(self, volume: _Optional[int] = ...) -> None: ...

class _MediaAction(_message.Message):
    __slots__ = ("action",)
    ACTION_FIELD_NUMBER: _ClassVar[int]
    action: int
    def __init__(self, action: _Optional[int] = ...) -> None: ...

class _MusicInfo(_message.Message):
    __slots__ = ("title", "artist", "duration")
    TITLE_FIELD_NUMBER: _ClassVar[int]
    ARTIST_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    title: str
    artist: str
    duration: int
    def __init__(self, title: _Optional[str] = ..., artist: _Optional[str] = ..., duration: _Optional[int] = ...) -> None: ...

class _MusicState(_message.Message):
    __slots__ = ("state", "position", "speed")
    STATE_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    SPEED_FIELD_NUMBER: _ClassVar[int]
    state: int
    position: int
    speed: float
    def __init__(self, state: _Optional[int] = ..., position: _Optional[int] = ..., speed: _Optional[float] = ...) -> None: ...

class _AlarmItem(_message.Message):
    __slots__ = ("id", "isEnabled", "hour", "minute", "repeat", "label", "type")
    ID_FIELD_NUMBER: _ClassVar[int]
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    HOUR_FIELD_NUMBER: _ClassVar[int]
    MINUTE_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    id: int
    isEnabled: bool
    hour: int
    minute: int
    repeat: int
    label: str
    type: int
    def __init__(self, id: _Optional[int] = ..., isEnabled: bool = ..., hour: _Optional[int] = ..., minute: _Optional[int] = ..., repeat: _Optional[int] = ..., label: _Optional[str] = ..., type: _Optional[int] = ...) -> None: ...

class _AlarmList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_AlarmItem]
    def __init__(self, items: _Optional[_Iterable[_Union[_AlarmItem, _Mapping]]] = ...) -> None: ...

class _ContactItem(_message.Message):
    __slots__ = ("name", "phone")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PHONE_FIELD_NUMBER: _ClassVar[int]
    name: str
    phone: str
    def __init__(self, name: _Optional[str] = ..., phone: _Optional[str] = ...) -> None: ...

class _ContactCommon(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_ContactItem]
    def __init__(self, items: _Optional[_Iterable[_Union[_ContactItem, _Mapping]]] = ...) -> None: ...

class _ContactEmergency(_message.Message):
    __slots__ = ("isEnabled", "items")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    items: _containers.RepeatedCompositeFieldContainer[_ContactItem]
    def __init__(self, isEnabled: bool = ..., items: _Optional[_Iterable[_Union[_ContactItem, _Mapping]]] = ...) -> None: ...

class _RemindItem(_message.Message):
    __slots__ = ("id", "isEnabled", "name", "note", "dnd", "mode", "times", "start", "end", "interval", "repeat")
    class Dnd(_message.Message):
        __slots__ = ("isEnabled", "start", "end")
        ISENABLED_FIELD_NUMBER: _ClassVar[int]
        START_FIELD_NUMBER: _ClassVar[int]
        END_FIELD_NUMBER: _ClassVar[int]
        isEnabled: bool
        start: int
        end: int
        def __init__(self, isEnabled: bool = ..., start: _Optional[int] = ..., end: _Optional[int] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NOTE_FIELD_NUMBER: _ClassVar[int]
    DND_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    TIMES_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    id: int
    isEnabled: bool
    name: str
    note: str
    dnd: _RemindItem.Dnd
    mode: int
    times: _containers.RepeatedScalarFieldContainer[int]
    start: int
    end: int
    interval: int
    repeat: int
    def __init__(self, id: _Optional[int] = ..., isEnabled: bool = ..., name: _Optional[str] = ..., note: _Optional[str] = ..., dnd: _Optional[_Union[_RemindItem.Dnd, _Mapping]] = ..., mode: _Optional[int] = ..., times: _Optional[_Iterable[int]] = ..., start: _Optional[int] = ..., end: _Optional[int] = ..., interval: _Optional[int] = ..., repeat: _Optional[int] = ...) -> None: ...

class _RemindList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_RemindItem]
    def __init__(self, items: _Optional[_Iterable[_Union[_RemindItem, _Mapping]]] = ...) -> None: ...

class _Weather(_message.Message):
    __slots__ = ("city", "updateTime", "code", "tempMin", "tempMax", "tempCurrent", "pressure", "quality", "humidity", "ultraviolet", "windAngle", "windScale", "windSpeed", "visibility", "type", "codeN", "typeN", "sunrise", "sunset")
    CITY_FIELD_NUMBER: _ClassVar[int]
    UPDATETIME_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    TEMPMIN_FIELD_NUMBER: _ClassVar[int]
    TEMPMAX_FIELD_NUMBER: _ClassVar[int]
    TEMPCURRENT_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    ULTRAVIOLET_FIELD_NUMBER: _ClassVar[int]
    WINDANGLE_FIELD_NUMBER: _ClassVar[int]
    WINDSCALE_FIELD_NUMBER: _ClassVar[int]
    WINDSPEED_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CODEN_FIELD_NUMBER: _ClassVar[int]
    TYPEN_FIELD_NUMBER: _ClassVar[int]
    SUNRISE_FIELD_NUMBER: _ClassVar[int]
    SUNSET_FIELD_NUMBER: _ClassVar[int]
    city: str
    updateTime: int
    code: int
    tempMin: int
    tempMax: int
    tempCurrent: int
    pressure: int
    quality: int
    humidity: int
    ultraviolet: int
    windAngle: float
    windScale: int
    windSpeed: float
    visibility: float
    type: int
    codeN: int
    typeN: int
    sunrise: int
    sunset: int
    def __init__(self, city: _Optional[str] = ..., updateTime: _Optional[int] = ..., code: _Optional[int] = ..., tempMin: _Optional[int] = ..., tempMax: _Optional[int] = ..., tempCurrent: _Optional[int] = ..., pressure: _Optional[int] = ..., quality: _Optional[int] = ..., humidity: _Optional[int] = ..., ultraviolet: _Optional[int] = ..., windAngle: _Optional[float] = ..., windScale: _Optional[int] = ..., windSpeed: _Optional[float] = ..., visibility: _Optional[float] = ..., type: _Optional[int] = ..., codeN: _Optional[int] = ..., typeN: _Optional[int] = ..., sunrise: _Optional[int] = ..., sunset: _Optional[int] = ...) -> None: ...

class _WeatherDay(_message.Message):
    __slots__ = ("timestamp", "code", "type", "tempMin", "tempMax", "codeN", "typeN", "pressure", "windAngle", "visibility")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TEMPMIN_FIELD_NUMBER: _ClassVar[int]
    TEMPMAX_FIELD_NUMBER: _ClassVar[int]
    CODEN_FIELD_NUMBER: _ClassVar[int]
    TYPEN_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_FIELD_NUMBER: _ClassVar[int]
    WINDANGLE_FIELD_NUMBER: _ClassVar[int]
    VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    code: int
    type: int
    tempMin: int
    tempMax: int
    codeN: int
    typeN: int
    pressure: int
    windAngle: float
    visibility: float
    def __init__(self, timestamp: _Optional[int] = ..., code: _Optional[int] = ..., type: _Optional[int] = ..., tempMin: _Optional[int] = ..., tempMax: _Optional[int] = ..., codeN: _Optional[int] = ..., typeN: _Optional[int] = ..., pressure: _Optional[int] = ..., windAngle: _Optional[float] = ..., visibility: _Optional[float] = ...) -> None: ...

class _WeatherDayList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_WeatherDay]
    def __init__(self, items: _Optional[_Iterable[_Union[_WeatherDay, _Mapping]]] = ...) -> None: ...

class _WeatherHour(_message.Message):
    __slots__ = ("timestamp", "code", "type", "tempCurrent", "codeN", "typeN")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TEMPCURRENT_FIELD_NUMBER: _ClassVar[int]
    CODEN_FIELD_NUMBER: _ClassVar[int]
    TYPEN_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    code: int
    type: int
    tempCurrent: int
    codeN: int
    typeN: int
    def __init__(self, timestamp: _Optional[int] = ..., code: _Optional[int] = ..., type: _Optional[int] = ..., tempCurrent: _Optional[int] = ..., codeN: _Optional[int] = ..., typeN: _Optional[int] = ...) -> None: ...

class _WeatherHourList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_WeatherHour]
    def __init__(self, items: _Optional[_Iterable[_Union[_WeatherHour, _Mapping]]] = ...) -> None: ...

class _SomeDataChange(_message.Message):
    __slots__ = ("type",)
    TYPE_FIELD_NUMBER: _ClassVar[int]
    type: int
    def __init__(self, type: _Optional[int] = ...) -> None: ...

class _DialItem(_message.Message):
    __slots__ = ("id", "is_builtin", "is_select")
    ID_FIELD_NUMBER: _ClassVar[int]
    IS_BUILTIN_FIELD_NUMBER: _ClassVar[int]
    IS_SELECT_FIELD_NUMBER: _ClassVar[int]
    id: int
    is_builtin: bool
    is_select: bool
    def __init__(self, id: _Optional[int] = ..., is_builtin: bool = ..., is_select: bool = ...) -> None: ...

class _DialList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_DialItem]
    def __init__(self, items: _Optional[_Iterable[_Union[_DialItem, _Mapping]]] = ...) -> None: ...

class _DialId(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: int
    def __init__(self, id: _Optional[int] = ...) -> None: ...

class _WorldClockItem(_message.Message):
    __slots__ = ("city", "zoneId", "zoneOffset")
    CITY_FIELD_NUMBER: _ClassVar[int]
    ZONEID_FIELD_NUMBER: _ClassVar[int]
    ZONEOFFSET_FIELD_NUMBER: _ClassVar[int]
    city: str
    zoneId: str
    zoneOffset: int
    def __init__(self, city: _Optional[str] = ..., zoneId: _Optional[str] = ..., zoneOffset: _Optional[int] = ...) -> None: ...

class _WorldClockList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_WorldClockItem]
    def __init__(self, items: _Optional[_Iterable[_Union[_WorldClockItem, _Mapping]]] = ...) -> None: ...

class _PrayerConfig(_message.Message):
    __slots__ = ("isEnabled", "items")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    items: _containers.RepeatedScalarFieldContainer[bool]
    def __init__(self, isEnabled: bool = ..., items: _Optional[_Iterable[bool]] = ...) -> None: ...

class _PrayerDay(_message.Message):
    __slots__ = ("timestamp", "items")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    items: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, timestamp: _Optional[int] = ..., items: _Optional[_Iterable[int]] = ...) -> None: ...

class _PrayerDayList(_message.Message):
    __slots__ = ("days",)
    DAYS_FIELD_NUMBER: _ClassVar[int]
    days: _containers.RepeatedCompositeFieldContainer[_PrayerDay]
    def __init__(self, days: _Optional[_Iterable[_Union[_PrayerDay, _Mapping]]] = ...) -> None: ...

class _LockObj(_message.Message):
    __slots__ = ("isEnabled", "password", "start", "end")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    password: bytes
    start: int
    end: int
    def __init__(self, isEnabled: bool = ..., password: _Optional[bytes] = ..., start: _Optional[int] = ..., end: _Optional[int] = ...) -> None: ...

class _OfflineMapAuthInfo(_message.Message):
    __slots__ = ("key", "license", "device", "area", "zoom")
    KEY_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    AREA_FIELD_NUMBER: _ClassVar[int]
    ZOOM_FIELD_NUMBER: _ClassVar[int]
    key: str
    license: str
    device: str
    area: int
    zoom: str
    def __init__(self, key: _Optional[str] = ..., license: _Optional[str] = ..., device: _Optional[str] = ..., area: _Optional[int] = ..., zoom: _Optional[str] = ...) -> None: ...

class _QrCodeSet(_message.Message):
    __slots__ = ("type", "hash", "content")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    type: int
    hash: int
    content: str
    def __init__(self, type: _Optional[int] = ..., hash: _Optional[int] = ..., content: _Optional[str] = ...) -> None: ...

class _QrCodeStatus(_message.Message):
    __slots__ = ("type", "hash")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    type: int
    hash: int
    def __init__(self, type: _Optional[int] = ..., hash: _Optional[int] = ...) -> None: ...

class _QrCodeStatusList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_QrCodeStatus]
    def __init__(self, items: _Optional[_Iterable[_Union[_QrCodeStatus, _Mapping]]] = ...) -> None: ...

class _EpoTimeInfo(_message.Message):
    __slots__ = ("validTime", "updateTime")
    VALIDTIME_FIELD_NUMBER: _ClassVar[int]
    UPDATETIME_FIELD_NUMBER: _ClassVar[int]
    validTime: int
    updateTime: int
    def __init__(self, validTime: _Optional[int] = ..., updateTime: _Optional[int] = ...) -> None: ...

class _AiResult(_message.Message):
    __slots__ = ("type", "error", "text", "isCompleted")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ISCOMPLETED_FIELD_NUMBER: _ClassVar[int]
    type: int
    error: int
    text: str
    isCompleted: int
    def __init__(self, type: _Optional[int] = ..., error: _Optional[int] = ..., text: _Optional[str] = ..., isCompleted: _Optional[int] = ...) -> None: ...

class _AiMessage(_message.Message):
    __slots__ = ("type", "shape")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    type: int
    shape: _pb_config_pb2._Shape
    def __init__(self, type: _Optional[int] = ..., shape: _Optional[_Union[_pb_config_pb2._Shape, _Mapping]] = ...) -> None: ...

class _TestBatteryInfo(_message.Message):
    __slots__ = ("percent", "charging", "voltage")
    PERCENT_FIELD_NUMBER: _ClassVar[int]
    CHARGING_FIELD_NUMBER: _ClassVar[int]
    VOLTAGE_FIELD_NUMBER: _ClassVar[int]
    percent: int
    charging: int
    voltage: int
    def __init__(self, percent: _Optional[int] = ..., charging: _Optional[int] = ..., voltage: _Optional[int] = ...) -> None: ...

class _TestDeviceInfo(_message.Message):
    __slots__ = ("deviceName", "bluetoothName", "model", "version")
    DEVICENAME_FIELD_NUMBER: _ClassVar[int]
    BLUETOOTHNAME_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    deviceName: str
    bluetoothName: str
    model: str
    version: str
    def __init__(self, deviceName: _Optional[str] = ..., bluetoothName: _Optional[str] = ..., model: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...
