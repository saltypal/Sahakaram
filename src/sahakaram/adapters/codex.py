"""Optional official Python Codex SDK bridge, isolated per local account.

No global auth/config is copied. Approval escalation is denied; the adapter
never grants full filesystem access or silently falls back to SDK defaults.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


class CodexUnavailable(RuntimeError):
    """SDK/runtime/capability is unavailable without claiming operation success."""


class CodexAdapter:
    def __init__(self, home: Path, *, codex_bin: str | None = None) -> None:
        self.home = home.resolve()
        self.codex_bin = codex_bin
        self.client: Any = None
        self.sdk: Any = None
        self.threads: dict[str, Any] = {}
        self.sandboxes: dict[str, str] = {}
        self.active: dict[str, Any] = {}
        self.login_handle: Any = None
        self.init_lock = asyncio.Lock()

    async def connect(self) -> Any:
        async with self.init_lock:
            if self.client is not None:
                return self.client
            try:
                sdk = importlib.import_module("openai_codex")
            except ImportError as exc:
                raise CodexUnavailable("Install sahakaram[codex] to enable Codex accounts") from exc
            try:
                # SDK env overlays its parent's environment. Empty overrides prevent
                # provider credentials from being inherited from this server process.
                config = sdk.CodexConfig(
                    codex_bin=self.codex_bin,
                    cwd=str(self.home),
                    config_overrides=('cli_auth_credentials_store="file"',),
                    env={
                        "CODEX_HOME": str(self.home),
                        "OPENAI_API_KEY": "",
                        "CODEX_API_KEY": "",
                        "OPENAI_ACCESS_TOKEN": "",
                    },
                )
                client = sdk.AsyncCodex(config=config)
                await client.__aenter__()
            except Exception as exc:
                raise CodexUnavailable("Codex runtime initialization failed") from exc
            self.sdk, self.client = sdk, client
            return client

    def sandbox(self, value: str) -> Any:
        if value not in {"read-only", "workspace-write"}:
            raise ValueError("Unsupported sandbox")
        try:
            return getattr(self.sdk.Sandbox, value.replace("-", "_"))
        except AttributeError as exc:
            raise CodexUnavailable("SDK lacks required sandbox support; operation refused") from exc

    def approvals(self) -> Any:
        try:
            return self.sdk.ApprovalMode.deny_all
        except AttributeError as exc:
            raise CodexUnavailable("SDK lacks deny-all approvals; operation refused") from exc

    async def login(self) -> dict[str, Any]:
        client = await self.connect()
        if self.login_handle is not None:
            await self.login_handle.cancel()
        try:
            self.login_handle = await client.login_chatgpt()
        except Exception as exc:
            raise CodexUnavailable("Codex browser login could not be initiated") from exc
        return {
            "status": "login-pending",
            "login_id": self.login_handle.login_id,
            "auth_url": self.login_handle.auth_url,
        }

    async def usage(self) -> dict[str, Any]:
        client = await self.connect()
        try:
            result = await client.account()
        except Exception as exc:
            raise CodexUnavailable("Codex account state could not be read") from exc
        authenticated = result.account is not None
        return await self._limits(client, authenticated)

    async def _limits(self, client: Any, authenticated: bool) -> dict[str, Any]:
        """Bounded 0.163.0 private bridge: one read on the already owned transport.

        AsyncCodex omits a public limits method. Keep this single compatibility
        boundary explicit, version checked, and fail closed on SDK/protocol drift.
        """

        def unavailable(reason: str) -> dict[str, Any]:
            return {"available": False, "authenticated": authenticated, "reason": reason}

        try:
            if version("openai-codex") != "0.163.0":
                return unavailable("sdk-version-unsupported")
        except PackageNotFoundError:
            return unavailable("sdk-version-unavailable")
        request = getattr(getattr(client, "_client", None), "request", None)
        if not callable(request):
            return unavailable("limits-capability-unavailable")
        try:
            models = importlib.import_module("openai_codex.generated.v2_all")
            response_model = models.GetAccountRateLimitsResponse
            inspect.signature(request).bind(
                "account/rateLimits/read", None, response_model=response_model
            )
        except (ImportError, AttributeError, TypeError, ValueError):
            return unavailable("limits-capability-incompatible")
        try:
            response = request("account/rateLimits/read", None, response_model=response_model)
            if not inspect.isawaitable(response):
                return unavailable("limits-capability-incompatible")
            response = await response
        except Exception as exc:
            if not authenticated:
                return unavailable("authentication-required")
            if getattr(exc, "code", None) == -32601:
                return unavailable("limits-method-unsupported")
            return unavailable("limits-request-failed")
        try:
            # Validate again because map values in the generated model are Any.
            response = response_model.model_validate(response)
            buckets: dict[str, Any] = {}
            historical = response.rate_limits
            key = historical.limit_id or "default"
            snapshots = {key: historical}
            for limit_id, snapshot in (response.rate_limits_by_limit_id or {}).items():
                snapshots[limit_id] = models.RateLimitSnapshot.model_validate(snapshot)
            for limit_id, snapshot in snapshots.items():
                windows: dict[str, Any] = {}
                for name in ("primary", "secondary"):
                    window = getattr(snapshot, name)
                    if window is not None:
                        if (
                            not math.isfinite(window.used_percent)
                            or not (0 <= window.used_percent <= 100)
                            or (
                                window.window_duration_mins is not None
                                and window.window_duration_mins < 0
                            )
                        ):
                            raise ValueError("Invalid quota window")
                        windows[name] = {
                            "used_percent": window.used_percent,
                            "resets_at_unix_seconds": window.resets_at,
                            "window_duration_minutes": window.window_duration_mins,
                        }
                if windows:
                    buckets[limit_id] = {"limit_name": snapshot.limit_name, "windows": windows}
            if not buckets:
                return unavailable("limits-response-unrecognized")
            return {
                "available": True,
                "authenticated": authenticated,
                "buckets": buckets,
                "ordinary_usage_allowed": response.ordinary_usage_allowed,
            }
        except (AttributeError, TypeError, ValueError):
            return unavailable("limits-response-unrecognized")

    async def start(self, workspace: Path, sandbox: str) -> str:
        client = await self.connect()
        try:
            thread = await client.thread_start(
                cwd=str(workspace), sandbox=self.sandbox(sandbox), approval_mode=self.approvals()
            )
        except (TypeError, AttributeError) as exc:
            raise CodexUnavailable("SDK cannot enforce required session controls") from exc
        self.threads[thread.id] = thread
        self.sandboxes[thread.id] = sandbox
        return str(thread.id)

    async def resume(self, thread_id: str, workspace: Path) -> None:
        client = await self.connect()
        # Persisted sessions resume read-only until explicitly created anew with write access.
        try:
            thread = await client.thread_resume(
                thread_id,
                cwd=str(workspace),
                sandbox=self.sandbox("read-only"),
                approval_mode=self.approvals(),
            )
        except (TypeError, AttributeError) as exc:
            raise CodexUnavailable("SDK cannot enforce required resume controls") from exc
        self.threads[thread_id] = thread
        self.sandboxes[thread_id] = "read-only"

    async def turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ValueError("Resume this session before sending a turn")
        try:
            handle = await thread.turn(
                prompt,
                sandbox=self.sandbox(self.sandboxes[thread_id]),
                approval_mode=self.approvals(),
            )
            self.active[thread_id] = handle
            result = await handle.run()
            return {
                "status": result.status.value,
                "final_response": result.final_response,
                "turn_id": handle.id,
            }
        except asyncio.CancelledError:
            # Kill this account runtime before releasing serialization. Interrupt
            # acknowledgement alone does not establish that its turn has stopped.
            cleanup = asyncio.create_task(self.close())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not release the account lock
                    # while the owned process is still executing the previous turn.
                    continue
            cleanup.result()
            raise
        finally:
            self.active.pop(thread_id, None)

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        handle = self.active.get(thread_id)
        if handle is None:
            raise ValueError("No active turn for this session")
        await handle.interrupt()
        return {"status": "interrupt-requested", "turn_id": handle.id}

    async def close(self) -> None:
        async with self.init_lock:
            if self.client is not None:
                await self.client.close()
                self.client = None
            self.active.clear()
            self.threads.clear()
