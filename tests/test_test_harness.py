"""Regression coverage for the optional-dependency test harness."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_importable_homeassistant_does_not_skip_duepi_tests(tmp_path: Path) -> None:
    """The suite must run when Home Assistant is available in the environment."""
    package = tmp_path / "homeassistant"
    (package / "helpers").mkdir(parents=True)
    (package / "components").mkdir()

    files = {
        "__init__.py": "",
        "config_entries.py": '''\
class ConfigEntry:
    def __class_getitem__(cls, _item):
        return cls


class ConfigFlow:
    def __init_subclass__(cls, **_kwargs):
        super().__init_subclass__()


class OptionsFlow:
    pass
''',
        "core.py": "class HomeAssistant:\n    pass\n\ndef callback(func):\n    return func\n",
        "exceptions.py": "class ConfigEntryAuthFailed(Exception):\n    pass\n\nclass ConfigEntryNotReady(Exception):\n    pass\n",
        "const.py": "CONF_EMAIL = 'email'\nCONF_PASSWORD = 'password'\n",
        "data_entry_flow.py": "FlowResult = dict\n",
        "helpers/__init__.py": "",
        "helpers/event.py": "def async_call_later(*_args):\n    raise AssertionError('test scheduler was not installed')\n",
        "helpers/aiohttp_client.py": "def async_create_clientsession(*_args, **_kwargs):\n    return object()\n",
        "helpers/update_coordinator.py": '''\
class DataUpdateCoordinator:
    def __init__(self, hass, logger, **_kwargs):
        self.hass = hass
        self.logger = logger
        self.data = None

    def async_set_updated_data(self, data):
        self.data = data

    def __class_getitem__(cls, _item):
        return cls


class UpdateFailed(Exception):
    pass
''',
        "components/__init__.py": "",
        "components/diagnostics.py": '''\
def async_redact_data(data, keys):
    return {
        key: "**REDACTED**" if key in keys and value is not None else value
        for key, value in data.items()
    }
''',
    }
    for relative_path, contents in files.items():
        (package / relative_path).write_text(contents)

    repository = Path(__file__).parents[1]
    environment = os.environ | {"PYTHONPATH": f"{tmp_path}{os.pathsep}{repository}"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_api_hardening.py::test_login_server_error_is_not_invalid_credentials",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" not in result.stdout
    assert "1 passed" in result.stdout
