from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _DataItem(_message.Message):
    __slots__ = ("timestamp", "value")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    value: int
    def __init__(self, timestamp: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...

class _DataItem2(_message.Message):
    __slots__ = ("timestamp", "value", "value2")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    VALUE2_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    value: int
    value2: int
    def __init__(self, timestamp: _Optional[int] = ..., value: _Optional[int] = ..., value2: _Optional[int] = ...) -> None: ...

class _ActivityDay(_message.Message):
    __slots__ = ("s_timestamp", "e_timestamp", "steps", "calories", "distance", "duration", "sport_duration", "number")
    S_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    E_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    CALORIES_FIELD_NUMBER: _ClassVar[int]
    DISTANCE_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    SPORT_DURATION_FIELD_NUMBER: _ClassVar[int]
    NUMBER_FIELD_NUMBER: _ClassVar[int]
    s_timestamp: int
    e_timestamp: int
    steps: _containers.RepeatedScalarFieldContainer[int]
    calories: _containers.RepeatedScalarFieldContainer[int]
    distance: _containers.RepeatedScalarFieldContainer[int]
    duration: _containers.RepeatedScalarFieldContainer[int]
    sport_duration: _containers.RepeatedScalarFieldContainer[int]
    number: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, s_timestamp: _Optional[int] = ..., e_timestamp: _Optional[int] = ..., steps: _Optional[_Iterable[int]] = ..., calories: _Optional[_Iterable[int]] = ..., distance: _Optional[_Iterable[int]] = ..., duration: _Optional[_Iterable[int]] = ..., sport_duration: _Optional[_Iterable[int]] = ..., number: _Optional[_Iterable[int]] = ...) -> None: ...

class _HeartRateDay(_message.Message):
    __slots__ = ("s_timestamp", "e_timestamp", "manual_data", "auto_data", "resting_data", "max_min_data")
    S_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    E_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MANUAL_DATA_FIELD_NUMBER: _ClassVar[int]
    AUTO_DATA_FIELD_NUMBER: _ClassVar[int]
    RESTING_DATA_FIELD_NUMBER: _ClassVar[int]
    MAX_MIN_DATA_FIELD_NUMBER: _ClassVar[int]
    s_timestamp: int
    e_timestamp: int
    manual_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    auto_data: bytes
    resting_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    max_min_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    def __init__(self, s_timestamp: _Optional[int] = ..., e_timestamp: _Optional[int] = ..., manual_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ..., auto_data: _Optional[bytes] = ..., resting_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ..., max_min_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ...) -> None: ...

class _PressureDay(_message.Message):
    __slots__ = ("s_timestamp", "e_timestamp", "manual_data", "auto_data", "max_min_data")
    S_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    E_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MANUAL_DATA_FIELD_NUMBER: _ClassVar[int]
    AUTO_DATA_FIELD_NUMBER: _ClassVar[int]
    MAX_MIN_DATA_FIELD_NUMBER: _ClassVar[int]
    s_timestamp: int
    e_timestamp: int
    manual_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    auto_data: bytes
    max_min_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    def __init__(self, s_timestamp: _Optional[int] = ..., e_timestamp: _Optional[int] = ..., manual_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ..., auto_data: _Optional[bytes] = ..., max_min_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ...) -> None: ...

class _BloodOxygenDay(_message.Message):
    __slots__ = ("s_timestamp", "e_timestamp", "manual_data", "auto_data", "max_min_data")
    S_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    E_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MANUAL_DATA_FIELD_NUMBER: _ClassVar[int]
    AUTO_DATA_FIELD_NUMBER: _ClassVar[int]
    MAX_MIN_DATA_FIELD_NUMBER: _ClassVar[int]
    s_timestamp: int
    e_timestamp: int
    manual_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    auto_data: bytes
    max_min_data: _containers.RepeatedCompositeFieldContainer[_DataItem]
    def __init__(self, s_timestamp: _Optional[int] = ..., e_timestamp: _Optional[int] = ..., manual_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ..., auto_data: _Optional[bytes] = ..., max_min_data: _Optional[_Iterable[_Union[_DataItem, _Mapping]]] = ...) -> None: ...

class _BloodPressureDay(_message.Message):
    __slots__ = ("s_timestamp", "e_timestamp", "manual_data", "auto_data", "max_min_data")
    S_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    E_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MANUAL_DATA_FIELD_NUMBER: _ClassVar[int]
    AUTO_DATA_FIELD_NUMBER: _ClassVar[int]
    MAX_MIN_DATA_FIELD_NUMBER: _ClassVar[int]
    s_timestamp: int
    e_timestamp: int
    manual_data: _containers.RepeatedCompositeFieldContainer[_DataItem2]
    auto_data: bytes
    max_min_data: _containers.RepeatedCompositeFieldContainer[_DataItem2]
    def __init__(self, s_timestamp: _Optional[int] = ..., e_timestamp: _Optional[int] = ..., manual_data: _Optional[_Iterable[_Union[_DataItem2, _Mapping]]] = ..., auto_data: _Optional[bytes] = ..., max_min_data: _Optional[_Iterable[_Union[_DataItem2, _Mapping]]] = ...) -> None: ...

class _SleepDay(_message.Message):
    __slots__ = ("timestamp", "items")
    class Item(_message.Message):
        __slots__ = ("timestamp", "duration", "type")
        TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
        DURATION_FIELD_NUMBER: _ClassVar[int]
        TYPE_FIELD_NUMBER: _ClassVar[int]
        timestamp: int
        duration: int
        type: int
        def __init__(self, timestamp: _Optional[int] = ..., duration: _Optional[int] = ..., type: _Optional[int] = ...) -> None: ...
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    items: _containers.RepeatedCompositeFieldContainer[_SleepDay.Item]
    def __init__(self, timestamp: _Optional[int] = ..., items: _Optional[_Iterable[_Union[_SleepDay.Item, _Mapping]]] = ...) -> None: ...

class _SyncStartRequest(_message.Message):
    __slots__ = ("start", "end", "type")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    start: int
    end: int
    type: int
    def __init__(self, start: _Optional[int] = ..., end: _Optional[int] = ..., type: _Optional[int] = ...) -> None: ...

class _SyncStartResponse(_message.Message):
    __slots__ = ("type", "result")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    type: int
    result: int
    def __init__(self, type: _Optional[int] = ..., result: _Optional[int] = ...) -> None: ...

class _SyncStopRequest(_message.Message):
    __slots__ = ("type",)
    TYPE_FIELD_NUMBER: _ClassVar[int]
    type: int
    def __init__(self, type: _Optional[int] = ...) -> None: ...

class _RealtimeOperation(_message.Message):
    __slots__ = ("type", "action", "duration")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    type: int
    action: int
    duration: int
    def __init__(self, type: _Optional[int] = ..., action: _Optional[int] = ..., duration: _Optional[int] = ...) -> None: ...

class _RealtimeData(_message.Message):
    __slots__ = ("value", "value2")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    VALUE2_FIELD_NUMBER: _ClassVar[int]
    value: int
    value2: int
    def __init__(self, value: _Optional[int] = ..., value2: _Optional[int] = ...) -> None: ...

class _SportRecord(_message.Message):
    __slots__ = ("start", "end", "type", "duration", "steps", "distance", "calories", "hr_max", "hr_min", "hr_avg", "cadence_max", "cadence_min", "cadence_avg", "pace_max", "pace_min", "pace_avg", "speed_max", "speed_min", "speed_avg", "hr_warm_up", "hr_fat_burning", "hr_aerobic", "hr_anaerobic", "hr_limit", "display_configs", "file_detail")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    DISTANCE_FIELD_NUMBER: _ClassVar[int]
    CALORIES_FIELD_NUMBER: _ClassVar[int]
    HR_MAX_FIELD_NUMBER: _ClassVar[int]
    HR_MIN_FIELD_NUMBER: _ClassVar[int]
    HR_AVG_FIELD_NUMBER: _ClassVar[int]
    CADENCE_MAX_FIELD_NUMBER: _ClassVar[int]
    CADENCE_MIN_FIELD_NUMBER: _ClassVar[int]
    CADENCE_AVG_FIELD_NUMBER: _ClassVar[int]
    PACE_MAX_FIELD_NUMBER: _ClassVar[int]
    PACE_MIN_FIELD_NUMBER: _ClassVar[int]
    PACE_AVG_FIELD_NUMBER: _ClassVar[int]
    SPEED_MAX_FIELD_NUMBER: _ClassVar[int]
    SPEED_MIN_FIELD_NUMBER: _ClassVar[int]
    SPEED_AVG_FIELD_NUMBER: _ClassVar[int]
    HR_WARM_UP_FIELD_NUMBER: _ClassVar[int]
    HR_FAT_BURNING_FIELD_NUMBER: _ClassVar[int]
    HR_AEROBIC_FIELD_NUMBER: _ClassVar[int]
    HR_ANAEROBIC_FIELD_NUMBER: _ClassVar[int]
    HR_LIMIT_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_CONFIGS_FIELD_NUMBER: _ClassVar[int]
    FILE_DETAIL_FIELD_NUMBER: _ClassVar[int]
    start: int
    end: int
    type: int
    duration: int
    steps: int
    distance: float
    calories: float
    hr_max: int
    hr_min: int
    hr_avg: int
    cadence_max: int
    cadence_min: int
    cadence_avg: int
    pace_max: int
    pace_min: int
    pace_avg: int
    speed_max: int
    speed_min: int
    speed_avg: int
    hr_warm_up: int
    hr_fat_burning: int
    hr_aerobic: int
    hr_anaerobic: int
    hr_limit: int
    display_configs: bytes
    file_detail: str
    def __init__(self, start: _Optional[int] = ..., end: _Optional[int] = ..., type: _Optional[int] = ..., duration: _Optional[int] = ..., steps: _Optional[int] = ..., distance: _Optional[float] = ..., calories: _Optional[float] = ..., hr_max: _Optional[int] = ..., hr_min: _Optional[int] = ..., hr_avg: _Optional[int] = ..., cadence_max: _Optional[int] = ..., cadence_min: _Optional[int] = ..., cadence_avg: _Optional[int] = ..., pace_max: _Optional[int] = ..., pace_min: _Optional[int] = ..., pace_avg: _Optional[int] = ..., speed_max: _Optional[int] = ..., speed_min: _Optional[int] = ..., speed_avg: _Optional[int] = ..., hr_warm_up: _Optional[int] = ..., hr_fat_burning: _Optional[int] = ..., hr_aerobic: _Optional[int] = ..., hr_anaerobic: _Optional[int] = ..., hr_limit: _Optional[int] = ..., display_configs: _Optional[bytes] = ..., file_detail: _Optional[str] = ...) -> None: ...
