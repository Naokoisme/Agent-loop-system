from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _FunctionConfig(_message.Message):
    __slots__ = ("flags",)
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    flags: bytes
    def __init__(self, flags: _Optional[bytes] = ...) -> None: ...

class _UnitConfig(_message.Message):
    __slots__ = ("flags",)
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    flags: bytes
    def __init__(self, flags: _Optional[bytes] = ...) -> None: ...

class _GoalConfig(_message.Message):
    __slots__ = ("steps", "distance", "calories", "duration", "number", "sportDuration", "flags")
    STEPS_FIELD_NUMBER: _ClassVar[int]
    DISTANCE_FIELD_NUMBER: _ClassVar[int]
    CALORIES_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    NUMBER_FIELD_NUMBER: _ClassVar[int]
    SPORTDURATION_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    steps: int
    distance: float
    calories: float
    duration: int
    number: int
    sportDuration: int
    flags: int
    def __init__(self, steps: _Optional[int] = ..., distance: _Optional[float] = ..., calories: _Optional[float] = ..., duration: _Optional[int] = ..., number: _Optional[int] = ..., sportDuration: _Optional[int] = ..., flags: _Optional[int] = ...) -> None: ...

class _DndConfig(_message.Message):
    __slots__ = ("isEnabled", "mode", "periodStart", "periodEnd")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    PERIODSTART_FIELD_NUMBER: _ClassVar[int]
    PERIODEND_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    mode: int
    periodStart: int
    periodEnd: int
    def __init__(self, isEnabled: bool = ..., mode: _Optional[int] = ..., periodStart: _Optional[int] = ..., periodEnd: _Optional[int] = ...) -> None: ...

class _RaiseWakeupConfig(_message.Message):
    __slots__ = ("isEnabled", "start", "end")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    start: int
    end: int
    def __init__(self, isEnabled: bool = ..., start: _Optional[int] = ..., end: _Optional[int] = ...) -> None: ...

class _BaseMonitor(_message.Message):
    __slots__ = ("isEnabled", "start", "end", "interval")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    start: int
    end: int
    interval: int
    def __init__(self, isEnabled: bool = ..., start: _Optional[int] = ..., end: _Optional[int] = ..., interval: _Optional[int] = ...) -> None: ...

class _BaseAlarm(_message.Message):
    __slots__ = ("isEnabled", "min", "max")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    MIN_FIELD_NUMBER: _ClassVar[int]
    MAX_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    min: int
    max: int
    def __init__(self, isEnabled: bool = ..., min: _Optional[int] = ..., max: _Optional[int] = ...) -> None: ...

class _HeartRateConfig(_message.Message):
    __slots__ = ("monitor", "exerciseAlarm", "restingAlarm", "maxThreshold")
    MONITOR_FIELD_NUMBER: _ClassVar[int]
    EXERCISEALARM_FIELD_NUMBER: _ClassVar[int]
    RESTINGALARM_FIELD_NUMBER: _ClassVar[int]
    MAXTHRESHOLD_FIELD_NUMBER: _ClassVar[int]
    monitor: _BaseMonitor
    exerciseAlarm: _BaseAlarm
    restingAlarm: _BaseAlarm
    maxThreshold: int
    def __init__(self, monitor: _Optional[_Union[_BaseMonitor, _Mapping]] = ..., exerciseAlarm: _Optional[_Union[_BaseAlarm, _Mapping]] = ..., restingAlarm: _Optional[_Union[_BaseAlarm, _Mapping]] = ..., maxThreshold: _Optional[int] = ...) -> None: ...

class _PressureConfig(_message.Message):
    __slots__ = ("monitor", "alarm")
    MONITOR_FIELD_NUMBER: _ClassVar[int]
    ALARM_FIELD_NUMBER: _ClassVar[int]
    monitor: _BaseMonitor
    alarm: _BaseAlarm
    def __init__(self, monitor: _Optional[_Union[_BaseMonitor, _Mapping]] = ..., alarm: _Optional[_Union[_BaseAlarm, _Mapping]] = ...) -> None: ...

class _BloodOxygenConfig(_message.Message):
    __slots__ = ("monitor", "alarm")
    MONITOR_FIELD_NUMBER: _ClassVar[int]
    ALARM_FIELD_NUMBER: _ClassVar[int]
    monitor: _BaseMonitor
    alarm: _BaseAlarm
    def __init__(self, monitor: _Optional[_Union[_BaseMonitor, _Mapping]] = ..., alarm: _Optional[_Union[_BaseAlarm, _Mapping]] = ...) -> None: ...

class _BloodPressureConfig(_message.Message):
    __slots__ = ("monitor", "alarm")
    class Alarm(_message.Message):
        __slots__ = ("isEnabled", "sbpMin", "sbpMax", "dbpMin", "dbpMax")
        ISENABLED_FIELD_NUMBER: _ClassVar[int]
        SBPMIN_FIELD_NUMBER: _ClassVar[int]
        SBPMAX_FIELD_NUMBER: _ClassVar[int]
        DBPMIN_FIELD_NUMBER: _ClassVar[int]
        DBPMAX_FIELD_NUMBER: _ClassVar[int]
        isEnabled: bool
        sbpMin: int
        sbpMax: int
        dbpMin: int
        dbpMax: int
        def __init__(self, isEnabled: bool = ..., sbpMin: _Optional[int] = ..., sbpMax: _Optional[int] = ..., dbpMin: _Optional[int] = ..., dbpMax: _Optional[int] = ...) -> None: ...
    MONITOR_FIELD_NUMBER: _ClassVar[int]
    ALARM_FIELD_NUMBER: _ClassVar[int]
    monitor: _BaseMonitor
    alarm: _BloodPressureConfig.Alarm
    def __init__(self, monitor: _Optional[_Union[_BaseMonitor, _Mapping]] = ..., alarm: _Optional[_Union[_BloodPressureConfig.Alarm, _Mapping]] = ...) -> None: ...

class _WomenHealthConfig(_message.Message):
    __slots__ = ("mode", "remindTime", "remindAdvance", "remindType", "cycle", "duration", "latest", "menstruationEnd", "remindFlags")
    MODE_FIELD_NUMBER: _ClassVar[int]
    REMINDTIME_FIELD_NUMBER: _ClassVar[int]
    REMINDADVANCE_FIELD_NUMBER: _ClassVar[int]
    REMINDTYPE_FIELD_NUMBER: _ClassVar[int]
    CYCLE_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    LATEST_FIELD_NUMBER: _ClassVar[int]
    MENSTRUATIONEND_FIELD_NUMBER: _ClassVar[int]
    REMINDFLAGS_FIELD_NUMBER: _ClassVar[int]
    mode: int
    remindTime: int
    remindAdvance: int
    remindType: int
    cycle: int
    duration: int
    latest: int
    menstruationEnd: int
    remindFlags: int
    def __init__(self, mode: _Optional[int] = ..., remindTime: _Optional[int] = ..., remindAdvance: _Optional[int] = ..., remindType: _Optional[int] = ..., cycle: _Optional[int] = ..., duration: _Optional[int] = ..., latest: _Optional[int] = ..., menstruationEnd: _Optional[int] = ..., remindFlags: _Optional[int] = ...) -> None: ...

class _NotificationConfig(_message.Message):
    __slots__ = ("flags", "app", "telephony")
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    APP_FIELD_NUMBER: _ClassVar[int]
    TELEPHONY_FIELD_NUMBER: _ClassVar[int]
    flags: bytes
    app: int
    telephony: int
    def __init__(self, flags: _Optional[bytes] = ..., app: _Optional[int] = ..., telephony: _Optional[int] = ...) -> None: ...

class _Shape(_message.Message):
    __slots__ = ("shape", "width", "height", "corners")
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    CORNERS_FIELD_NUMBER: _ClassVar[int]
    shape: int
    width: int
    height: int
    corners: int
    def __init__(self, shape: _Optional[int] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., corners: _Optional[int] = ...) -> None: ...

class _DeviceInfo(_message.Message):
    __slots__ = ("project", "version", "brand", "model", "serial", "mainBoard", "shape", "ability", "activity", "sleepAlgorithm", "preview", "limits", "notifications", "companyId", "productType", "video", "platform", "dialFeatures")
    class Limits(_message.Message):
        __slots__ = ("builtInDial", "cloudDial", "alarm", "contact", "remind", "worldClock")
        BUILTINDIAL_FIELD_NUMBER: _ClassVar[int]
        CLOUDDIAL_FIELD_NUMBER: _ClassVar[int]
        ALARM_FIELD_NUMBER: _ClassVar[int]
        CONTACT_FIELD_NUMBER: _ClassVar[int]
        REMIND_FIELD_NUMBER: _ClassVar[int]
        WORLDCLOCK_FIELD_NUMBER: _ClassVar[int]
        builtInDial: int
        cloudDial: int
        alarm: int
        contact: int
        remind: _containers.RepeatedScalarFieldContainer[int]
        worldClock: int
        def __init__(self, builtInDial: _Optional[int] = ..., cloudDial: _Optional[int] = ..., alarm: _Optional[int] = ..., contact: _Optional[int] = ..., remind: _Optional[_Iterable[int]] = ..., worldClock: _Optional[int] = ...) -> None: ...
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    BRAND_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SERIAL_FIELD_NUMBER: _ClassVar[int]
    MAINBOARD_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    ABILITY_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    SLEEPALGORITHM_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    NOTIFICATIONS_FIELD_NUMBER: _ClassVar[int]
    COMPANYID_FIELD_NUMBER: _ClassVar[int]
    PRODUCTTYPE_FIELD_NUMBER: _ClassVar[int]
    VIDEO_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    DIALFEATURES_FIELD_NUMBER: _ClassVar[int]
    project: str
    version: str
    brand: str
    model: str
    serial: str
    mainBoard: str
    shape: _Shape
    ability: bytes
    activity: bytes
    sleepAlgorithm: int
    preview: _Shape
    limits: _DeviceInfo.Limits
    notifications: bytes
    companyId: int
    productType: int
    video: _Shape
    platform: int
    dialFeatures: int
    def __init__(self, project: _Optional[str] = ..., version: _Optional[str] = ..., brand: _Optional[str] = ..., model: _Optional[str] = ..., serial: _Optional[str] = ..., mainBoard: _Optional[str] = ..., shape: _Optional[_Union[_Shape, _Mapping]] = ..., ability: _Optional[bytes] = ..., activity: _Optional[bytes] = ..., sleepAlgorithm: _Optional[int] = ..., preview: _Optional[_Union[_Shape, _Mapping]] = ..., limits: _Optional[_Union[_DeviceInfo.Limits, _Mapping]] = ..., notifications: _Optional[bytes] = ..., companyId: _Optional[int] = ..., productType: _Optional[int] = ..., video: _Optional[_Union[_Shape, _Mapping]] = ..., platform: _Optional[int] = ..., dialFeatures: _Optional[int] = ...) -> None: ...

class _Configs(_message.Message):
    __slots__ = ("deviceInfo", "functionConfig", "unitConfig", "goalConfig", "dndConfig", "raiseWakeupConfig", "heartRateConfig", "pressureConfig", "bloodOxygenConfig", "bloodPressureConfig", "womenHealthConfig", "notificationConfig")
    DEVICEINFO_FIELD_NUMBER: _ClassVar[int]
    FUNCTIONCONFIG_FIELD_NUMBER: _ClassVar[int]
    UNITCONFIG_FIELD_NUMBER: _ClassVar[int]
    GOALCONFIG_FIELD_NUMBER: _ClassVar[int]
    DNDCONFIG_FIELD_NUMBER: _ClassVar[int]
    RAISEWAKEUPCONFIG_FIELD_NUMBER: _ClassVar[int]
    HEARTRATECONFIG_FIELD_NUMBER: _ClassVar[int]
    PRESSURECONFIG_FIELD_NUMBER: _ClassVar[int]
    BLOODOXYGENCONFIG_FIELD_NUMBER: _ClassVar[int]
    BLOODPRESSURECONFIG_FIELD_NUMBER: _ClassVar[int]
    WOMENHEALTHCONFIG_FIELD_NUMBER: _ClassVar[int]
    NOTIFICATIONCONFIG_FIELD_NUMBER: _ClassVar[int]
    deviceInfo: _DeviceInfo
    functionConfig: _FunctionConfig
    unitConfig: _UnitConfig
    goalConfig: _GoalConfig
    dndConfig: _DndConfig
    raiseWakeupConfig: _RaiseWakeupConfig
    heartRateConfig: _HeartRateConfig
    pressureConfig: _PressureConfig
    bloodOxygenConfig: _BloodOxygenConfig
    bloodPressureConfig: _BloodPressureConfig
    womenHealthConfig: _WomenHealthConfig
    notificationConfig: _NotificationConfig
    def __init__(self, deviceInfo: _Optional[_Union[_DeviceInfo, _Mapping]] = ..., functionConfig: _Optional[_Union[_FunctionConfig, _Mapping]] = ..., unitConfig: _Optional[_Union[_UnitConfig, _Mapping]] = ..., goalConfig: _Optional[_Union[_GoalConfig, _Mapping]] = ..., dndConfig: _Optional[_Union[_DndConfig, _Mapping]] = ..., raiseWakeupConfig: _Optional[_Union[_RaiseWakeupConfig, _Mapping]] = ..., heartRateConfig: _Optional[_Union[_HeartRateConfig, _Mapping]] = ..., pressureConfig: _Optional[_Union[_PressureConfig, _Mapping]] = ..., bloodOxygenConfig: _Optional[_Union[_BloodOxygenConfig, _Mapping]] = ..., bloodPressureConfig: _Optional[_Union[_BloodPressureConfig, _Mapping]] = ..., womenHealthConfig: _Optional[_Union[_WomenHealthConfig, _Mapping]] = ..., notificationConfig: _Optional[_Union[_NotificationConfig, _Mapping]] = ...) -> None: ...

class _CommonResponse(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: int
    def __init__(self, result: _Optional[int] = ...) -> None: ...

class _CommonIntRequest(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: int
    def __init__(self, value: _Optional[int] = ...) -> None: ...
