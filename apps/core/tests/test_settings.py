from pathlib import Path

import pytest

from core.settings import Settings, SettingsError

ENV = {
    "REPOS_DIR": "/repos",
    "OPENCODE_URL": "http://opencode:4096/",
    "OPENCODE_SERVER_PASSWORD": "secret",
}


def test_from_env() -> None:
    settings = Settings.from_env(ENV)
    assert settings == Settings(Path("/repos"), "http://opencode:4096", "secret")
    assert "secret" not in repr(settings)


@pytest.mark.parametrize("name", list(ENV))
def test_required(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({**ENV, name: " "})


def test_password_kept_as_is() -> None:
    settings = Settings.from_env({**ENV, "OPENCODE_SERVER_PASSWORD": " secret "})
    assert settings.opencode_password == " secret "
