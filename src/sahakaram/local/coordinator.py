"""Account-scoped async execution; interrupt deliberately bypasses turn serialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from sahakaram.local.registry import Registry


class Adapter(Protocol):
    async def login(self) -> dict[str, Any]: ...
    async def usage(self) -> dict[str, Any]: ...
    async def start(self, workspace: Path, sandbox: str) -> str: ...
    async def resume(self, thread_id: str, workspace: Path) -> None: ...
    async def turn(self, thread_id: str, prompt: str) -> dict[str, Any]: ...
    async def interrupt(self, thread_id: str) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class Coordinator:
    def __init__(self, registry: Registry, factory: Callable[[Path], Adapter]) -> None:
        self.registry = registry
        self.factory = factory
        self.adapters: dict[str, Adapter] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.suspended = False
        self.generation = 0

    def adapter(self, account_id: str) -> Adapter:
        if self.suspended:
            raise PermissionError("Profile locked")
        account = self.registry.account(account_id)
        if account_id not in self.adapters:
            self.adapters[account_id] = self.factory(Path(account["codex_home"]))
        return self.adapters[account_id]

    @asynccontextmanager
    async def serial(self, account_id: str) -> AsyncIterator[None]:
        self.registry.account(account_id)
        generation = self.generation
        async with self.locks.setdefault(account_id, asyncio.Lock()):
            if self.suspended or generation != self.generation:
                raise PermissionError("Profile locked; unlock and retry")
            yield

    def activate(self) -> None:
        self.suspended = False

    async def start(self, account_id: str, workspace: str, sandbox: str) -> str:
        path = Path(workspace).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Workspace must be an explicit existing absolute directory")
        path = path.resolve()
        async with self.serial(account_id):
            thread_id = await self.adapter(account_id).start(path, sandbox)
            self.registry.remember(account_id, thread_id, str(path))
            return thread_id

    async def resume(self, account_id: str, thread_id: str) -> None:
        workspace = Path(self.registry.workspace(account_id, thread_id))
        if not workspace.is_dir():
            raise ValueError("Session workspace no longer exists")
        async with self.serial(account_id):
            await self.adapter(account_id).resume(thread_id, workspace)

    async def turn(self, account_id: str, thread_id: str, prompt: str) -> dict[str, Any]:
        self.registry.workspace(account_id, thread_id)
        async with self.serial(account_id):
            return await self.adapter(account_id).turn(thread_id, prompt)

    async def interrupt(self, account_id: str, thread_id: str) -> dict[str, Any]:
        self.registry.workspace(account_id, thread_id)
        return await self.adapter(account_id).interrupt(thread_id)

    async def close(self) -> None:
        self.suspended = True
        self.generation += 1
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
