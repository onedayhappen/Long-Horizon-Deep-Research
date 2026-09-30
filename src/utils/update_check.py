from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

PYPI_PROJECT_URL = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    status: Literal["up_to_date", "update_available", "failed", "disabled"]
    current_version: str
    latest_version: str = ""
    error: str = ""


class UpdateCheckHandle:
    def __init__(self, current_version: str, *, timeout: float) -> None:
        self._result: UpdateCheckResult | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(current_version, timeout),
            name="lh-harness-update-check",
            daemon=True,
        )
        self._thread.start()

    def _run(self, current_version: str, timeout: float) -> None:
        try:
            self._result = check_for_update(current_version, timeout=timeout)
        finally:
            self._done.set()

    def result(self, timeout: float = 0) -> UpdateCheckResult | None:
        self._done.wait(max(0.0, timeout))
        return self._result if self._done.is_set() else None


def check_for_update(current_version: str, *, timeout: float = 3.0) -> UpdateCheckResult:
    """Local source is authoritative; never query or suggest an upstream release."""
    return UpdateCheckResult("disabled", current_version)


def start_update_check(current_version: str, *, timeout: float = 3.0) -> UpdateCheckHandle:
    return UpdateCheckHandle(current_version, timeout=timeout)
