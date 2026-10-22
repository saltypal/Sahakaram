"""Launch the actual local control service and reject a mismatched profile."""

import socket
import subprocess

import pytest

from sahakaram.bootstrap import ensure_control
from sahakaram.config import Settings


def test_control_bootstrap_reuses_only_the_same_state(tmp_path, monkeypatch):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    processes = []
    real_popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("sahakaram.bootstrap.subprocess.Popen", launch)
    settings = Settings(state_dir=tmp_path / "first", control_port=port)
    try:
        ensure_control(settings)
        ensure_control(settings)
        assert len(processes) == 1
        with pytest.raises(RuntimeError, match="another state directory"):
            ensure_control(Settings(state_dir=tmp_path / "second", control_port=port))
        assert len(processes) == 1
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=10)
