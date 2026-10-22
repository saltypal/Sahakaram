"""Start the loopback control service once; never launch a public host implicitly."""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from filelock import FileLock

from sahakaram.config import Settings


def state_identity(state_dir: Path) -> str:
    """Identify a state directory without publishing its filesystem path."""
    normalized = os.path.normcase(str(state_dir.resolve()))
    return hashlib.sha256(normalized.encode()).hexdigest()


def ensure_control(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{settings.control_port}/"
    with FileLock(str(settings.state_dir / "control-start.lock"), timeout=15):
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            try:
                response = client.get(url)
                if (
                    response.status_code == 200
                    and response.json().get("service") == "sahakaram-local"
                ):
                    if response.json().get("state_id") != state_identity(settings.state_dir):
                        raise RuntimeError("Control port belongs to another state directory")
                    return
                raise RuntimeError("Control port is already used by another service")
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            except (httpx.HTTPError, ValueError) as exc:
                raise RuntimeError("Control port did not return a valid service response") from exc
            log_path = settings.state_dir / "control.log"
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "sahakaram",
                        "control",
                        "--port",
                        str(settings.control_port),
                    ],
                    env={**os.environ, "SAHAKARAM_STATE_DIR": str(settings.state_dir.resolve())},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError("Local control process exited; inspect .state/control.log")
                try:
                    response = client.get(url)
                    if (
                        response.status_code == 200
                        and response.json().get("service") == "sahakaram-local"
                        and response.json().get("state_id") == state_identity(settings.state_dir)
                    ):
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            process.terminate()
            process.wait(timeout=5)
            raise RuntimeError("Local control service did not become ready")
