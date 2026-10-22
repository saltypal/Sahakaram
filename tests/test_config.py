"""Configuration boundary checks; invalid input must not start the transport."""

import pytest
from pydantic import ValidationError

from sahakaram.config import Settings


def test_settings_read_namespaced_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAHAKARAM_INSTANCE_NAME", "Laptop A")
    monkeypatch.setenv("SAHAKARAM_LOG_LEVEL", "WARNING")
    settings = Settings()
    assert settings.instance_name == "Laptop A"
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_invalid_instance_name_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(instance_name=name)
