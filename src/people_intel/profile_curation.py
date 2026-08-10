from __future__ import annotations

import json
from typing import Any


def merge_profile_patch(
    profile: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge profile metadata while preserving list order.

    List elements may be dictionaries (for example discovery provenance), so
    stable JSON markers are used for deduplication instead of Python hashing.
    """

    output = dict(profile)
    for key, value in patch.items():
        if isinstance(value, list):
            current = output.get(key)
            current = current if isinstance(current, list) else []
            merged: list[Any] = []
            seen: set[str] = set()
            for item in [*current, *value]:
                marker = json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if marker in seen:
                    continue
                seen.add(marker)
                merged.append(item)
            output[key] = merged
        elif isinstance(value, dict):
            current = output.get(key)
            current = current if isinstance(current, dict) else {}
            output[key] = merge_profile_patch(current, value)
        else:
            output[key] = value
    return output


__all__ = ["merge_profile_patch"]
