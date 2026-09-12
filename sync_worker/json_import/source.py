"""Stable identities for exports without an explicitly supplied username."""

import hashlib
import json


def export_source_id(data: dict) -> tuple[int, bool]:
    raw_id = str(data.get("id", "")).strip()
    kind = str(data.get("type", ""))
    channel_types = {"public_channel", "private_channel", "public_supergroup", "private_supergroup"}
    try:
        value = int(raw_id)
    except ValueError:
        value = 0
    if value:
        if kind in channel_types:
            if 0 < value < 10**12:
                return -(10**12 + value), False
            if -(2 * 10**12) < value < -(10**12):
                return value, False
        if kind in {"personal_chat", "bot_chat", "private_group"} and value > 0:
            identity = f"export-peer:{kind}:{value}"
        else:
            identity = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return _scope_hash(identity), True
        return _scope_hash(identity), False
    identity = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _scope_hash(identity), True


def _scope_hash(identity: str) -> int:
    # Separate from Telegram peer IDs and legacy username hashes; fits SQLite int64.
    digest = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:15], 16)
    return -(2**62 + digest)
