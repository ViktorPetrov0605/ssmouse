"""Checks for the packaged Linux assets (udev rule, installer, README)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_udev_rule_exists_and_matches_device() -> None:
    rule = ROOT / "udev" / "99-steelseries.rules"
    text = rule.read_text(encoding="utf-8")
    assert 'SUBSYSTEM=="hidraw"' in text
    assert 'ATTRS{idVendor}=="1038"' in text
    assert 'MODE="0666"' in text
    assert 'TAG+="uaccess"' in text
    # No trailing newline issue aside, the rule must not be commented out.
    active_lines = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert any('idVendor}=="1038"' in line for line in active_lines)


def test_installer_exists_and_documents_sudo() -> None:
    script = ROOT / "install.sh"
    text = script.read_text(encoding="utf-8")
    assert "99-steelseries.rules" in text
    assert "sudo" in text
    assert "udevadm" in text
    # pipx or venv fallback must both be offered.
    assert "pipx" in text
    assert "venv" in text
    assert text.startswith("#!/usr/bin/env bash")


def test_readme_documents_platforms_and_troubleshooting() -> None:
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    for needle in (
        "Linux",
        "Windows",
        "macOS",
        "udev",
        "Troubleshooting",
        "--json",
        "battery",
        "dpi",
        "SSMOUSE_FAKE",
    ):
        assert needle in text, needle
