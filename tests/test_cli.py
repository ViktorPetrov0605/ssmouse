"""End-to-end CLI tests through ``python -m ssmouse`` with the fake transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(*args: str, env_extra: dict | None = None, timeout: float = 20.0):
    env = dict(os.environ)
    env.update({"SSMOUSE_FAKE": "1", "PYTHONPATH": str(ROOT)})
    env.pop("SSMOUSE_FRAMING", None)
    env.pop("SSMOUSE_BACKEND", None)
    env.pop("SSMOUSE_HIDRAW", None)
    env.pop("SSMOUSE_INTERFACE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, "-m", "ssmouse", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_json_info_full_payload() -> None:
    result = _run("--json", "info")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["name"] == "SteelSeries Rival 3 Wireless"
    assert payload["vendor_id"] == "1038"
    assert payload["product_id"] == "1830"
    assert payload["model"] == "M-0000022"
    assert payload["firmware"] == "1.04"
    assert payload["firmware_source"] == "dongle"
    assert payload["battery"] == {"level": 47, "charging": None}
    assert payload["polling_rate_hz"] == 250
    assert payload["connected"] is True

    dpi = payload["dpi"]
    assert dpi["level_max"] == 5
    assert dpi["active_level"] == 2
    assert dpi["current_cpi"] == 600
    assert [preset["cpi"] for preset in dpi["presets"]] == [
        400,
        600,
        1400,
        2800,
        4100,
    ]

    assert payload["power_mode"]["idle_sleep"] == 900
    assert "framing" in payload["protocol"]


def test_json_battery() -> None:
    result = _run("--json", "battery")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"level": 47, "charging": None}


def test_json_dpi() -> None:
    result = _run("--json", "dpi")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["available"] is True
    assert payload["current_cpi"] == 600
    assert payload["presets"][1] == {"level": 2, "sensor": 6, "cpi": 600}


def test_text_info() -> None:
    result = _run("info")
    assert result.returncode == 0, result.stderr
    assert "47%" in result.stdout
    assert "600 CPI" in result.stdout
    assert "1.04" in result.stdout
    assert "250 Hz" in result.stdout


def test_text_battery() -> None:
    result = _run("battery")
    assert result.returncode == 0, result.stderr
    assert "Battery: 47%" in result.stdout
    assert "not reported" in result.stdout


def test_fake_flag_without_env() -> None:
    env = {"PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [PYTHON, "-m", "ssmouse", "--fake", "--json", "battery"],
        cwd=str(ROOT),
        env={k: v for k, v in os.environ.items() if not k.startswith("SSMOUSE_")}
        | env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["level"] == 47


def test_version_and_help() -> None:
    version = _run("--version")
    assert version.returncode == 0
    assert "ssmouse" in version.stdout
    help_result = _run("--help")
    assert help_result.returncode == 0
    assert "info" in help_result.stdout
    assert "battery" in help_result.stdout
    assert "dpi" in help_result.stdout


def test_no_command_shows_help() -> None:
    result = _run()
    assert result.returncode == 1
    assert "usage: ssmouse" in result.stdout


def test_watch_prints_repeatedly() -> None:
    env = dict(os.environ)
    env.update({"SSMOUSE_FAKE": "1", "PYTHONPATH": str(ROOT)})
    process = subprocess.Popen(
        [PYTHON, "-m", "ssmouse", "--watch", "--interval", "0.2", "battery"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.2)
    finally:
        process.terminate()
        stdout, _ = process.communicate(timeout=10)
    lines = [line for line in stdout.splitlines() if "Battery" in line]
    assert len(lines) >= 2, stdout
    assert all("47%" in line for line in lines)


def test_debug_hexdump_on_stderr() -> None:
    result = _run("--debug", "battery")
    assert result.returncode == 0, result.stderr
    assert "TX" in result.stderr
    assert "AA 01" in result.stderr


def test_json_watch_one_line_per_sample() -> None:
    env = dict(os.environ)
    env.update({"SSMOUSE_FAKE": "1", "PYTHONPATH": str(ROOT)})
    process = subprocess.Popen(
        [PYTHON, "-m", "ssmouse", "--watch", "--interval", "0.2", "--json", "battery"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.0)
    finally:
        process.terminate()
        stdout, _ = process.communicate(timeout=10)
    samples = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert len(samples) >= 2
    assert all(sample["level"] == 47 for sample in samples)


def test_override_options_do_not_crash() -> None:
    """Regression: --interface/--path/--backend/--framing env overrides."""
    result = _run(
        "--fake",
        "--interface",
        "3",
        "--path",
        "/dev/hidraw7",
        "--backend",
        "auto",
        "--framing",
        "default",
        "--json",
        "battery",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["level"] == 47
    assert "Traceback" not in result.stderr


def test_apply_env_overrides_sets_variables(monkeypatch) -> None:
    from ssmouse import cli

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "--interface",
            "3",
            "--path",
            "/dev/hidraw9",
            "--backend",
            "ctypes-hidraw",
            "--framing",
            "canonical",
            "battery",
        ]
    )
    for key in (
        "SSMOUSE_FAKE",
        "SSMOUSE_INTERFACE",
        "SSMOUSE_HIDRAW",
        "SSMOUSE_BACKEND",
        "SSMOUSE_FRAMING",
    ):
        monkeypatch.delenv(key, raising=False)
    cli._apply_env_overrides(args)
    assert os.environ["SSMOUSE_FAKE"] == "1"
    assert os.environ["SSMOUSE_INTERFACE"] == "3"
    assert os.environ["SSMOUSE_HIDRAW"] == "/dev/hidraw9"
    assert os.environ["SSMOUSE_BACKEND"] == "ctypes-hidraw"
    assert os.environ["SSMOUSE_FRAMING"] == "canonical"


def test_global_options_before_and_after_command() -> None:
    before = _run("--json", "battery")
    after = _run("battery", "--json")
    assert before.returncode == 0, before.stderr
    assert after.returncode == 0, after.stderr
    assert json.loads(before.stdout)["level"] == 47
    assert json.loads(after.stdout)["level"] == 47


def _unlinked_mouse():
    """Receiver answers (firmware, rf_status=1) but the mouse itself is asleep."""
    from ssmouse.device import Mouse
    from ssmouse.fake import (
        CAPTURED_RF_STATUS_UNPAIRED_RESPONSE,
        FAKE_FIRMWARE_RESPONSE,
        FAKE_INTERFACE,
        FakeTransport,
    )

    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport(
        script=[
            (bytes([0xE8]), CAPTURED_RF_STATUS_UNPAIRED_RESPONSE),
            (bytes([0xD0]), FAKE_FIRMWARE_RESPONSE),
        ]
    )
    transport.open()
    mouse._transport = transport  # noqa: SLF001
    return mouse, transport


def test_info_skips_mouse_queries_when_not_linked() -> None:
    from ssmouse.status import NOT_LINKED_MESSAGE, collect_info

    mouse, transport = _unlinked_mouse()
    payload = collect_info(mouse)
    assert payload["connected"] is False
    assert payload["firmware"] == "1.04"
    assert payload["battery"] is None
    assert payload["dpi"] is None
    assert payload["errors"] == [NOT_LINKED_MESSAGE]
    # Only dongle-side commands were sent: no battery/DPI/polling/power reads.
    assert {write[0] for write in transport.writes} <= {0xE8, 0xA8, 0xD0}


def test_battery_fails_fast_when_not_linked() -> None:
    import pytest

    from ssmouse.cli import _collect_battery
    from ssmouse.protocol import ProtocolError

    mouse, transport = _unlinked_mouse()
    with pytest.raises(ProtocolError, match="not linked"):
        _collect_battery(mouse)
    assert all(write[0] != 0xAA for write in transport.writes)
