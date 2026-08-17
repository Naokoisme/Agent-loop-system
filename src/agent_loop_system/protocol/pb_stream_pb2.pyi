from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class _H264Head(_message.Message):
    __slots__ = ("fps", "w", "h", "db_en", "pref")
    FPS_FIELD_NUMBER: _ClassVar[int]
    W_FIELD_NUMBER: _ClassVar[int]
    H_FIELD_NUMBER: _ClassVar[int]
    DB_EN_FIELD_NUMBER: _ClassVar[int]
    PREF_FIELD_NUMBER: _ClassVar[int]
    fps: int
    w: int
    h: int
    db_en: int
    pref: int
    def __init__(self, fps: _Optional[int] = ..., w: _Optional[int] = ..., h: _Optional[int] = ..., db_en: _Optional[int] = ..., pref: _Optional[int] = ...) -> None: ...

class _OpusHead(_message.Message):
    __slots__ = ("sampling", "frame_ms", "channel", "bitrate")
    SAMPLING_FIELD_NUMBER: _ClassVar[int]
    FRAME_MS_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    BITRATE_FIELD_NUMBER: _ClassVar[int]
    sampling: int
    frame_ms: int
    channel: int
    bitrate: int
    def __init__(self, sampling: _Optional[int] = ..., frame_ms: _Optional[int] = ..., channel: _Optional[int] = ..., bitrate: _Optional[int] = ...) -> None: ...

class _OpusEnd(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: int
    def __init__(self, result: _Optional[int] = ...) -> None: ...

class _FileHead(_message.Message):
    __slots__ = ("path", "crc32", "size")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CRC32_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    path: str
    crc32: int
    size: int
    def __init__(self, path: _Optional[str] = ..., crc32: _Optional[int] = ..., size: _Optional[int] = ...) -> None: ...

class _FileHeadResponse(_message.Message):
    __slots__ = ("result", "old_size", "old_crc32", "package_size", "package_count")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    OLD_SIZE_FIELD_NUMBER: _ClassVar[int]
    OLD_CRC32_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    result: int
    old_size: int
    old_crc32: int
    package_size: int
    package_count: int
    def __init__(self, result: _Optional[int] = ..., old_size: _Optional[int] = ..., old_crc32: _Optional[int] = ..., package_size: _Optional[int] = ..., package_count: _Optional[int] = ...) -> None: ...

class _FileData(_message.Message):
    __slots__ = ("offset", "data")
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    offset: int
    data: bytes
    def __init__(self, offset: _Optional[int] = ..., data: _Optional[bytes] = ...) -> None: ...

class _FileDataResponse(_message.Message):
    __slots__ = ("result", "file_size", "file_crc32")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_FIELD_NUMBER: _ClassVar[int]
    FILE_CRC32_FIELD_NUMBER: _ClassVar[int]
    result: int
    file_size: int
    file_crc32: int
    def __init__(self, result: _Optional[int] = ..., file_size: _Optional[int] = ..., file_crc32: _Optional[int] = ...) -> None: ...

class _FilePath(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class _DirSpace(_message.Message):
    __slots__ = ("total", "free")
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    FREE_FIELD_NUMBER: _ClassVar[int]
    total: int
    free: int
    def __init__(self, total: _Optional[int] = ..., free: _Optional[int] = ...) -> None: ...

class _FileInfo(_message.Message):
    __slots__ = ("path", "size")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    path: str
    size: int
    def __init__(self, path: _Optional[str] = ..., size: _Optional[int] = ...) -> None: ...

class _FileList(_message.Message):
    __slots__ = ("files",)
    FILES_FIELD_NUMBER: _ClassVar[int]
    files: _containers.RepeatedCompositeFieldContainer[_FileInfo]
    def __init__(self, files: _Optional[_Iterable[_Union[_FileInfo, _Mapping]]] = ...) -> None: ...

class _FileOperation(_message.Message):
    __slots__ = ("path", "op")
    PATH_FIELD_NUMBER: _ClassVar[int]
    OP_FIELD_NUMBER: _ClassVar[int]
    path: str
    op: int
    def __init__(self, path: _Optional[str] = ..., op: _Optional[int] = ...) -> None: ...

class _GPSHead(_message.Message):
    __slots__ = ("status", "interval")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    status: int
    interval: int
    def __init__(self, status: _Optional[int] = ..., interval: _Optional[int] = ...) -> None: ...

class _GPSData(_message.Message):
    __slots__ = ("fix_status", "longitude", "latitude", "signal", "hdop", "timestamp", "altitude")
    FIX_STATUS_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_FIELD_NUMBER: _ClassVar[int]
    LATITUDE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    HDOP_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ALTITUDE_FIELD_NUMBER: _ClassVar[int]
    fix_status: int
    longitude: int
    latitude: int
    signal: int
    hdop: int
    timestamp: int
    altitude: int
    def __init__(self, fix_status: _Optional[int] = ..., longitude: _Optional[int] = ..., latitude: _Optional[int] = ..., signal: _Optional[int] = ..., hdop: _Optional[int] = ..., timestamp: _Optional[int] = ..., altitude: _Optional[int] = ...) -> None: ...

class _BytesData(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...
