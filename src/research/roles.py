from __future__ import annotations

from typing import Protocol

from .models import RoleRequest, RoleResponse


class RoleBackend(Protocol):
    async def generate(self, request: RoleRequest) -> RoleResponse: ...
