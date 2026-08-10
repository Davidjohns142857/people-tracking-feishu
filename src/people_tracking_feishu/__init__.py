"""Portable Feishu runtime for the people-tracking skill."""

from pathlib import Path
import re


def _release_version() -> str:
    value = (Path(__file__).resolve().parents[2] / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+-portable\.\d+", value):
        raise RuntimeError("release VERSION file is invalid")
    return value


__version__ = _release_version()
