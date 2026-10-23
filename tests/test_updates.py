"""Update checks against recorded SteelSeries / GitHub API behaviour (no network)."""

from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import urllib.parse

import pytest

from ssmouse import updates

FULL_122 = {"version": "122.0.0", "type": "full", "checksum": "a09436242d0f9c1e7bd0c4a477f00361",
            "installer_url": "https://engine.steelseriescdn.com/SteelSeriesGG122.0.0Setup.exe"}
PATCH_122 = {"version": "122.0.0", "type": "patch", "checksum": "00",
             "installer_url": "https://engine.steelseriescdn.com/"
                              "SteelSeriesGG122.0.0PatchFrom121.0.0Setup.exe"}


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes = b"") -> None:
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Recorded server behaviour: 204 when current, patch for the previous
    release, the full installer for older clients."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = []

    def fake_get(url: str, *, accept: str = "application/json"):
        calls.append(url)
        if url.startswith(updates.GG_CHECK_URL):
            current = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["current_version"][0]
            major = int(current.split(".")[0])
            if major >= 122:
                return _Response(204)
            body = PATCH_122 if major == 121 else FULL_122
            return _Response(200, json.dumps(body).encode())
        if url == updates.GITHUB_RELEASES_URL:
            raise updates.UpdateError("api.github.com answered HTTP 404")
        raise AssertionError(url)

    monkeypatch.setattr(updates, "_get", fake_get)
    return calls


def test_up_to_date_gg(api) -> None:
    assert updates.check_gg_release() is None  # bundled 122.0.0 -> 204


def test_newer_gg_resolves_full_installer(api) -> None:
    release = updates.check_gg_release("121.0.0")
    assert release.version == "122.0.0" and release.kind == "patch"
    full = updates.full_installer(release)
    assert full.kind == "full"
    assert full.installer_url.endswith("SteelSeriesGG122.0.0Setup.exe")
    assert full.checksum == "A09436242D0F9C1E7BD0C4A477F00361"


def test_remembered_gg_version(api) -> None:
    assert updates.last_checked_gg() == updates.BUNDLED_GG_VERSION
    updates.remember_checked_gg("123.0.0")
    assert updates.last_checked_gg() == "123.0.0"
    updates.remember_checked_gg("1.0.0")  # never below the bundled release
    assert updates.last_checked_gg() == updates.BUNDLED_GG_VERSION


def test_app_release_without_releases(api) -> None:
    assert updates.check_app_release() is None


def test_app_release_newer(monkeypatch) -> None:
    body = json.dumps({"tag_name": "v9.1.0", "html_url": "https://example/r"}).encode()
    monkeypatch.setattr(updates, "_get", lambda url, accept="": _Response(200, body))
    assert updates.check_app_release("0.3.0") == updates.AppRelease("9.1.0", "https://example/r")
    assert updates.check_app_release("9.1.0") is None


def test_download_verifies_md5(monkeypatch, tmp_path) -> None:
    payload = b"installer bytes"
    monkeypatch.setattr(updates, "_get", lambda url, accept="": _Response(200, payload))
    good = updates.GGRelease("123.0.0", "https://cdn/SteelSeriesGG123.0.0Setup.exe",
                             "B1E4E5F1D1F8F4A5D2A43D4A0D96E9C5")
    with pytest.raises(updates.UpdateError, match="MD5"):
        updates.download_installer(good, tmp_path)
    import hashlib

    ok = updates.GGRelease(good.version, good.installer_url,
                           hashlib.md5(payload).hexdigest().upper())  # noqa: S324
    path = updates.download_installer(ok, tmp_path)
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.skipif(updates.find_7zip() is None, reason="7-Zip not installed")
def test_extract_firmware_from_archive(tmp_path) -> None:
    folder = tmp_path / "src" / updates.FIRMWARE_DIR_IN_INSTALLER
    folder.mkdir(parents=True)
    vectors = struct.pack("<II", 0x20005FE0, 0x00025521)
    for name, size in (("firmware-rival-3-wireless-mouse-v1.05.bin", 0x12000),
                       ("nordic-dongle-firmware-rival-3-wireless-v1.05.bin", 0x9000),
                       ("holtek-dongle-firmware-rival-3-wireless-v1.05.bin", 0x4000)):
        (folder / name).write_bytes(vectors + bytes(size - len(vectors)))
    (folder / "version.json").write_text('{"firmware_version": "1.05.0.0"}')
    (tmp_path / "src" / "unrelated.txt").write_text("not extracted")
    archive = tmp_path / "installer.7z"
    subprocess.run([updates.find_7zip(), "a", str(archive), "apps", "unrelated.txt"],
                   cwd=tmp_path / "src", check=True, capture_output=True)
    shutil.rmtree(tmp_path / "src")
    package = updates.extract_firmware(archive, tmp_path / "out")
    assert package.version == "1.05"
    assert package.source == tmp_path / "out" / "1.05"
    assert not (tmp_path / "out" / "1.05" / "unrelated.txt").exists()
