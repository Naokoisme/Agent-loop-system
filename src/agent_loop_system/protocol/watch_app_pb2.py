"""Runtime protobuf schema for the 6202 authentication messages.

This is the single local schema definition.  It mirrors only the four messages
needed from the firmware's ``pb_env.proto`` and builds normal protobuf message
classes at import time; no checked-in generated copy is required.
"""

from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf.internal import builder as _builder


_F = _descriptor_pb2.FieldDescriptorProto
_MESSAGES = {
    "_AuthRequest": (
        (1, "userId", _F.TYPE_STRING, None),
        (2, "system", _F.TYPE_INT32, None),
        (3, "version", _F.TYPE_STRING, None),
        (4, "brand", _F.TYPE_STRING, None),
        (5, "model", _F.TYPE_STRING, None),
        (6, "authCode", _F.TYPE_STRING, None),
        (7, "user", _F.TYPE_MESSAGE, "._UserInfo"),
        (8, "time", _F.TYPE_MESSAGE, "._TimeInfo"),
    ),
    "_AuthResponse": (
        (1, "result", _F.TYPE_INT32, None),
        (2, "everBind", _F.TYPE_INT32, None),
        (3, "bindTime", _F.TYPE_INT64, None),
    ),
    "_UserInfo": (
        (1, "gender", _F.TYPE_INT32, None),
        (2, "age", _F.TYPE_INT32, None),
        (3, "height", _F.TYPE_INT32, None),
        (4, "weight", _F.TYPE_FLOAT, None),
    ),
    "_TimeInfo": (
        (1, "timestamp", _F.TYPE_INT32, None),
        (2, "zoneOffset", _F.TYPE_SINT32, None),
    ),
}

_file = _descriptor_pb2.FileDescriptorProto(
    name="agent_loop_system/protocol/watch_app.proto",
    syntax="proto3",
)
_file.options.java_package = "com.topstep.wearkit.prototb.internal.pb"
_file.options.java_multiple_files = True

for _message_name, _fields in _MESSAGES.items():
    _message = _file.message_type.add(name=_message_name)
    for _number, _name, _field_type, _type_name in _fields:
        _field = _message.field.add(
            name=_name,
            number=_number,
            label=_F.LABEL_OPTIONAL,
            type=_field_type,
        )
        if _type_name is not None:
            _field.type_name = _type_name

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    _file.SerializeToString()
)
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, __name__, globals())

