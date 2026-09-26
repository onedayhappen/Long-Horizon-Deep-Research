"""Packaged role prompt templates."""

from __future__ import annotations

import json
from importlib.resources import files


def template(role: str) -> str:
    data = json.loads(files("lh_harness.research").joinpath("prompts/v1/roles.json").read_text("utf-8"))
    return data[role]
