"""Runtime lookup helpers for every protobuf message in PB v0.1.2."""

from __future__ import annotations

from google.protobuf import descriptor_pool, message_factory
from google.protobuf.message import Message

# Importing the generated modules registers every descriptor in the default
# pool.  Keep the list explicit so a newly added schema cannot be silently
# omitted from name-based lookup.
from agent_loop_system.protocol import (
    pb_b2b_hsd_pb2,
    pb_config_pb2,
    pb_data_pb2,
    pb_env_pb2,
    pb_notice_pb2,
    pb_setting_pb2,
    pb_stream_pb2,
)


PB_PACKAGE = "agent_loop_system.protocol"
SCHEMA_MODULES = (
    pb_config_pb2,
    pb_setting_pb2,
    pb_env_pb2,
    pb_notice_pb2,
    pb_data_pb2,
    pb_stream_pb2,
    pb_b2b_hsd_pb2,
)


def message_class(name: str) -> type[Message]:
    """Resolve ``_DeviceInfo`` or its fully qualified name to a PB class."""

    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError("message name must be a non-empty trimmed string")
    qualified_name = name if "." in name else f"{PB_PACKAGE}.{name}"
    try:
        descriptor = descriptor_pool.Default().FindMessageTypeByName(qualified_name)
    except KeyError as exc:
        raise KeyError(f"unknown PB message type {name!r}") from exc
    return message_factory.GetMessageClass(descriptor)


def new_message(name: str, **fields: object) -> Message:
    """Construct a generated PB message by its document-level name."""

    return message_class(name)(**fields)


def message_names() -> tuple[str, ...]:
    """Return all top-level message names in stable schema/file order."""

    return tuple(
        descriptor.name
        for module in SCHEMA_MODULES
        for descriptor in module.DESCRIPTOR.message_types_by_name.values()
    )


__all__ = [
    "PB_PACKAGE",
    "SCHEMA_MODULES",
    "message_class",
    "message_names",
    "new_message",
]
