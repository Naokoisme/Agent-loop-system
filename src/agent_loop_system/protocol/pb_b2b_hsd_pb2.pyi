from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _HsdIce(_message.Message):
    __slots__ = ("labels",)
    LABELS_FIELD_NUMBER: _ClassVar[int]
    labels: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, labels: _Optional[_Iterable[str]] = ...) -> None: ...

class _HsdParentalMode(_message.Message):
    __slots__ = ("isEnabled", "timeSettingEnabled", "enterSettingEnabled", "alarmSettingEnabled", "gameDurationEnabled", "gameStartMinuteOffset", "gameEndMinuteOffset")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    TIMESETTINGENABLED_FIELD_NUMBER: _ClassVar[int]
    ENTERSETTINGENABLED_FIELD_NUMBER: _ClassVar[int]
    ALARMSETTINGENABLED_FIELD_NUMBER: _ClassVar[int]
    GAMEDURATIONENABLED_FIELD_NUMBER: _ClassVar[int]
    GAMESTARTMINUTEOFFSET_FIELD_NUMBER: _ClassVar[int]
    GAMEENDMINUTEOFFSET_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    timeSettingEnabled: bool
    enterSettingEnabled: bool
    alarmSettingEnabled: bool
    gameDurationEnabled: bool
    gameStartMinuteOffset: int
    gameEndMinuteOffset: int
    def __init__(self, isEnabled: bool = ..., timeSettingEnabled: bool = ..., enterSettingEnabled: bool = ..., alarmSettingEnabled: bool = ..., gameDurationEnabled: bool = ..., gameStartMinuteOffset: _Optional[int] = ..., gameEndMinuteOffset: _Optional[int] = ...) -> None: ...

class _HsdClassRoomMode(_message.Message):
    __slots__ = ("isEnabled", "start", "end", "repeat")
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    isEnabled: bool
    start: int
    end: int
    repeat: int
    def __init__(self, isEnabled: bool = ..., start: _Optional[int] = ..., end: _Optional[int] = ..., repeat: _Optional[int] = ...) -> None: ...

class _HsdTask(_message.Message):
    __slots__ = ("id", "time", "label", "isEnabled", "repeat", "type", "state", "coins", "description", "isTimeEnabled")
    ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    ISENABLED_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    COINS_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    ISTIMEENABLED_FIELD_NUMBER: _ClassVar[int]
    id: int
    time: int
    label: str
    isEnabled: bool
    repeat: int
    type: int
    state: int
    coins: int
    description: str
    isTimeEnabled: bool
    def __init__(self, id: _Optional[int] = ..., time: _Optional[int] = ..., label: _Optional[str] = ..., isEnabled: bool = ..., repeat: _Optional[int] = ..., type: _Optional[int] = ..., state: _Optional[int] = ..., coins: _Optional[int] = ..., description: _Optional[str] = ..., isTimeEnabled: bool = ...) -> None: ...

class _HsdTaskInfo(_message.Message):
    __slots__ = ("totalCoins", "items")
    TOTALCOINS_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    totalCoins: int
    items: _containers.RepeatedCompositeFieldContainer[_HsdTask]
    def __init__(self, totalCoins: _Optional[int] = ..., items: _Optional[_Iterable[_Union[_HsdTask, _Mapping]]] = ...) -> None: ...

class _HsdTaskExchangeResult(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: bool = ...) -> None: ...

class _HsdHabit(_message.Message):
    __slots__ = ("id", "type", "label", "time", "duration", "repeat", "state", "reachGoalDays", "maxReachGoalDays", "taskDays", "associatedFunction", "remindDuration", "remindAdvance", "latestAchieveGoalMonth", "latestAchieveGoalDay", "achieveGoalRepeat")
    ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REACHGOALDAYS_FIELD_NUMBER: _ClassVar[int]
    MAXREACHGOALDAYS_FIELD_NUMBER: _ClassVar[int]
    TASKDAYS_FIELD_NUMBER: _ClassVar[int]
    ASSOCIATEDFUNCTION_FIELD_NUMBER: _ClassVar[int]
    REMINDDURATION_FIELD_NUMBER: _ClassVar[int]
    REMINDADVANCE_FIELD_NUMBER: _ClassVar[int]
    LATESTACHIEVEGOALMONTH_FIELD_NUMBER: _ClassVar[int]
    LATESTACHIEVEGOALDAY_FIELD_NUMBER: _ClassVar[int]
    ACHIEVEGOALREPEAT_FIELD_NUMBER: _ClassVar[int]
    id: int
    type: int
    label: str
    time: int
    duration: int
    repeat: int
    state: int
    reachGoalDays: int
    maxReachGoalDays: int
    taskDays: int
    associatedFunction: int
    remindDuration: int
    remindAdvance: int
    latestAchieveGoalMonth: int
    latestAchieveGoalDay: int
    achieveGoalRepeat: int
    def __init__(self, id: _Optional[int] = ..., type: _Optional[int] = ..., label: _Optional[str] = ..., time: _Optional[int] = ..., duration: _Optional[int] = ..., repeat: _Optional[int] = ..., state: _Optional[int] = ..., reachGoalDays: _Optional[int] = ..., maxReachGoalDays: _Optional[int] = ..., taskDays: _Optional[int] = ..., associatedFunction: _Optional[int] = ..., remindDuration: _Optional[int] = ..., remindAdvance: _Optional[int] = ..., latestAchieveGoalMonth: _Optional[int] = ..., latestAchieveGoalDay: _Optional[int] = ..., achieveGoalRepeat: _Optional[int] = ...) -> None: ...

class _HsdHabitList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_HsdHabit]
    def __init__(self, items: _Optional[_Iterable[_Union[_HsdHabit, _Mapping]]] = ...) -> None: ...

class _HsdUsageInfo(_message.Message):
    __slots__ = ("type", "count")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    type: int
    count: int
    def __init__(self, type: _Optional[int] = ..., count: _Optional[int] = ...) -> None: ...

class _HsdUsageInfoList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_HsdUsageInfo]
    def __init__(self, items: _Optional[_Iterable[_Union[_HsdUsageInfo, _Mapping]]] = ...) -> None: ...

class _HsdGameRecord(_message.Message):
    __slots__ = ("timestamp", "gameType", "duration", "score", "level")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    GAMETYPE_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    gameType: int
    duration: int
    score: int
    level: int
    def __init__(self, timestamp: _Optional[int] = ..., gameType: _Optional[int] = ..., duration: _Optional[int] = ..., score: _Optional[int] = ..., level: _Optional[int] = ...) -> None: ...

class _HsdGameRecordList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_HsdGameRecord]
    def __init__(self, items: _Optional[_Iterable[_Union[_HsdGameRecord, _Mapping]]] = ...) -> None: ...

class _HsdGameRankingTrend(_message.Message):
    __slots__ = ("gameType", "ranking", "trend")
    GAMETYPE_FIELD_NUMBER: _ClassVar[int]
    RANKING_FIELD_NUMBER: _ClassVar[int]
    TREND_FIELD_NUMBER: _ClassVar[int]
    gameType: int
    ranking: int
    trend: int
    def __init__(self, gameType: _Optional[int] = ..., ranking: _Optional[int] = ..., trend: _Optional[int] = ...) -> None: ...

class _HsdGameRankingTrendList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_HsdGameRankingTrend]
    def __init__(self, items: _Optional[_Iterable[_Union[_HsdGameRankingTrend, _Mapping]]] = ...) -> None: ...
