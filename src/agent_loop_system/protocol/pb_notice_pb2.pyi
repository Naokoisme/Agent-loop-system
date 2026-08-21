from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class _AppNotice(_message.Message):
    __slots__ = ("type", "title", "content")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    type: int
    title: str
    content: str
    def __init__(self, type: _Optional[int] = ..., title: _Optional[str] = ..., content: _Optional[str] = ...) -> None: ...

class _TelephonyNotice(_message.Message):
    __slots__ = ("type", "phone", "name")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PHONE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    type: int
    phone: str
    name: str
    def __init__(self, type: _Optional[int] = ..., phone: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class _HangupRequest(_message.Message):
    __slots__ = ("phone", "endCall", "sendSms")
    PHONE_FIELD_NUMBER: _ClassVar[int]
    ENDCALL_FIELD_NUMBER: _ClassVar[int]
    SENDSMS_FIELD_NUMBER: _ClassVar[int]
    phone: str
    endCall: int
    sendSms: str
    def __init__(self, phone: _Optional[str] = ..., endCall: _Optional[int] = ..., sendSms: _Optional[str] = ...) -> None: ...

class _HangupResponse(_message.Message):
    __slots__ = ("endCall", "sendSms")
    ENDCALL_FIELD_NUMBER: _ClassVar[int]
    SENDSMS_FIELD_NUMBER: _ClassVar[int]
    endCall: int
    sendSms: int
    def __init__(self, endCall: _Optional[int] = ..., sendSms: _Optional[int] = ...) -> None: ...

class _SOSRequest(_message.Message):
    __slots__ = ("phone",)
    PHONE_FIELD_NUMBER: _ClassVar[int]
    phone: str
    def __init__(self, phone: _Optional[str] = ...) -> None: ...

class _QuickCmd(_message.Message):
    __slots__ = ("cmd",)
    CMD_FIELD_NUMBER: _ClassVar[int]
    cmd: str
    def __init__(self, cmd: _Optional[str] = ...) -> None: ...
