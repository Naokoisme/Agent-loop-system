from __future__ import annotations


def infrastructure_for(*, transport_ok: bool, observation_ok: bool, cleanup_ok: bool) -> str:
    if not cleanup_ok:
        return "CLEANUP_REQUIRED"
    if not transport_ok:
        return "TRANSPORT_ERROR"
    if not observation_ok:
        return "OBSERVATION_INCOMPLETE"
    return "READY"
