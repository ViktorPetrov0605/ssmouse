"""Receiver push events (captured on a Rival 3 Wireless)."""

from __future__ import annotations

from ssmouse.events import Event, parse_event


def test_parse_captured_events() -> None:
    assert parse_event(bytes.fromhex("2801") + bytes(62)) == Event("link", 1)
    assert parse_event(bytes.fromhex("2800") + bytes(62)) == Event("link", 0)
    assert parse_event(bytes.fromhex("2800 01".replace(" ", "")) + bytes(61)) == Event("link", 0)
    assert parse_event(bytes.fromhex("1603") + bytes(62)) == Event("dpi", 3)
    assert parse_event(bytes.fromhex("2a46") + bytes(62)) == Event("battery", 70)


def test_ignores_unknown_and_invalid() -> None:
    assert parse_event(b"") is None
    assert parse_event(b"\x28") is None
    assert parse_event(bytes.fromhex("1609")) is None  # no preset 9
    assert parse_event(bytes.fromhex("2a90")) is None  # 144 %
    assert parse_event(bytes.fromhex("9900")) is None
