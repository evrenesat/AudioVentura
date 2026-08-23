"""Shared validation for SaladCloud resource names."""

from __future__ import annotations

import re

SALAD_RESOURCE_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])$")


def is_salad_resource_name(value: str) -> bool:
    """Return whether *value* satisfies Salad's 2–63 character name contract."""

    return SALAD_RESOURCE_NAME_RE.fullmatch(value) is not None
