"""Online update checks: new firmware (via SteelSeries GG releases) and new ssmouse versions.

SteelSeries has no separate firmware feed.  New firmware ships inside SteelSeries
GG releases; GG polls ``check-version`` and compares the firmware bundled in
the installed release with the device.  ssmouse does the same:

1. ``GET https://steelseries.com/api/v2/releases/gg/check-version?platform=windows&current_version=X``
   returns 204 if GG X is current, otherwise 200 with ``version``,
   ``installer_url`` (engine.steelseriescdn.com) and an MD5 ``checksum``.
2. If there is a newer GG, its installer is downloaded from SteelSeries' CDN,
   verified, and only ``apps/engine/firmware/272111664/`` is extracted (7-Zip
   reads the NSIS installer).  The firmware is never redistributed by ssmouse.
3. :mod:`ssmouse.firmware` compares the version with the mouse and installs it
   only after the user confirms.

ssmouse's own updates are checked against the GitHub releases of the project.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from .firmware import FirmwareError, FirmwarePackage, load_package, parse_version

GG_CHECK_URL = "https://steelseries.com/api/v2/releases/gg/check-version"
#: SteelSeries GG release whose firmware ssmouse knows (``firmware.LATEST_KNOWN``).
BUNDLED_GG_VERSION = "122.0.0"
FIRMWARE_DIR_IN_INSTALLER = "apps/engine/firmware/272111664"
GITHUB_RELEASES_URL = "https://api.github.com/repos/ViktorPetrov0605/ssmouse/releases/latest"
RELEASES_PAGE = "https://github.com/ViktorPetrov0605/ssmouse/releases"
USER_AGENT = "ssmouse/%s (+https://github.com/ViktorPetrov0605/ssmouse)" % __version__
TIMEOUT_S = 15


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class GGRelease:
    version: str
    installer_url: str
    checksum: str  # MD5, upper-case hex ("" if unknown)
    kind: str = "full"  # "full" installer, or "patch" (no firmware inside)


@dataclass(frozen=True)
class AppRelease:
    version: str
    url: str


def _get(url: str, *, accept: str = "application/json"):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT_S)  # noqa: S310 - fixed https URLs
    except urllib.error.HTTPError as exc:
        raise UpdateError("%s answered HTTP %d" % (urllib.parse.urlsplit(url).netloc, exc.code))
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError("could not reach %s: %s" % (urllib.parse.urlsplit(url).netloc, exc))


def cache_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "ssmouse"


def _state_path() -> Path:
    return cache_dir() / "state.json"


def last_checked_gg() -> str:
    """Newest GG release whose firmware ssmouse has already looked at."""
    try:
        version = json.loads(_state_path().read_text())["gg_version"]
        if parse_version(version) > parse_version(BUNDLED_GG_VERSION):
            return version
    except (OSError, ValueError, KeyError, TypeError, FirmwareError):
        pass
    return BUNDLED_GG_VERSION


def remember_checked_gg(version: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"gg_version": version}))


# ---------------------------------------------------------------------------
# SteelSeries GG releases (firmware source)
# ---------------------------------------------------------------------------


def _query_gg(current: str) -> Optional[GGRelease]:
    query = urllib.parse.urlencode({"platform": "windows", "current_version": current})
    with _get("%s?%s" % (GG_CHECK_URL, query)) as response:
        if response.status == 204:
            return None
        try:
            data = json.loads(response.read().decode("utf-8"))
        except ValueError as exc:
            raise UpdateError("unexpected answer from SteelSeries: %s" % exc)
    version, url = data.get("version"), data.get("installer_url", "")
    if not version or not url.startswith("https://"):
        raise UpdateError("unexpected answer from SteelSeries: %r" % data)
    return GGRelease(version, url, str(data.get("checksum", "")).upper(),
                     str(data.get("type") or "full"))


def check_gg_release(current: Optional[str] = None) -> Optional[GGRelease]:
    """The newest GG release if it is newer than *current*, else ``None``.

    The server answers an up-to-date client with 204, the previous release
    with a small patch installer, and very old clients with a stepping-stone
    release, so the result may be a patch; see :func:`full_installer`.
    """
    current = current or last_checked_gg()
    release = _query_gg(current)
    if release is None or parse_version(release.version) <= parse_version(current):
        return None
    return release


def full_installer(release: GGRelease) -> GGRelease:
    """The full installer (which contains the firmware) for *release.version*."""
    if release.kind == "full":
        return release
    major = parse_version(release.version)[0]
    for back in (5, 10, 20, 30):
        candidate = _query_gg("%d.0.0" % max(1, major - back))
        if candidate and candidate.version == release.version and candidate.kind == "full":
            return candidate
    # Full installers follow a fixed naming scheme on SteelSeries' CDN; the
    # firmware images are still validated after extraction.
    return GGRelease(release.version,
                     "https://engine.steelseriescdn.com/SteelSeriesGG%sSetup.exe" % release.version,
                     "", "full")


def download_installer(
    release: GGRelease, dest: Path, progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """Download and MD5-verify the GG installer; reuses a verified copy."""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(urllib.parse.urlsplit(release.installer_url).path).name
    if target.exists() and _md5(target) == release.checksum:
        return target
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.md5()  # noqa: S324 - SteelSeries publishes MD5
    with _get(release.installer_url, accept="*/*") as response, partial.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if release.checksum and digest.hexdigest().upper() != release.checksum:
        partial.unlink(missing_ok=True)
        raise UpdateError("download corrupted (MD5 mismatch); try again")
    partial.replace(target)
    return target


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - SteelSeries publishes MD5
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest().upper()


def find_7zip() -> Optional[str]:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            candidate = Path(base or "") / "7-Zip" / "7z.exe"
            if base and candidate.exists():
                return str(candidate)
    return None


def extract_firmware(installer: Path, dest: Path) -> FirmwarePackage:
    """Extract the Rival 3 Wireless firmware folder from a GG installer."""
    seven = find_7zip()
    if seven is None:
        raise UpdateError("7-Zip is needed to unpack the SteelSeries installer "
                          "(Linux: install p7zip/7zip, macOS: brew install sevenzip, "
                          "Windows: 7-zip.org)")
    with tempfile.TemporaryDirectory(prefix="ssmouse-gg-") as tmp:
        result = subprocess.run(
            [seven, "x", "-y", "-o" + tmp, str(installer), FIRMWARE_DIR_IN_INSTALLER + "/*"],
            capture_output=True, text=True, check=False,
        )
        source = Path(tmp) / FIRMWARE_DIR_IN_INSTALLER
        if result.returncode != 0 or not source.is_dir():
            raise UpdateError("could not extract the firmware: %s"
                              % (result.stderr.strip() or result.stdout.strip()[-300:]))
        package = load_package(source)
        target = dest / package.version
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    return load_package(target)


def fetch_firmware(
    current_gg: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[Optional[GGRelease], Optional[FirmwarePackage]]:
    """Check for a newer GG; if there is one, download it and extract its firmware."""
    release = check_gg_release(current_gg)
    if release is None:
        return None, None
    release = full_installer(release)
    base = cache_dir()
    installer = download_installer(release, base / "downloads", progress)
    try:
        package = extract_firmware(installer, base / "firmware")
    except FirmwareError as exc:
        raise UpdateError("GG %s has no usable Rival 3 Wireless firmware: %s"
                          % (release.version, exc))
    installer.unlink(missing_ok=True)  # ~430 MB; only the firmware is kept
    remember_checked_gg(release.version)
    return release, package


def best_package() -> Optional[FirmwarePackage]:
    """Newest firmware available locally (downloaded earlier), if any."""
    packages = cached_packages()
    return packages[-1] if packages else None


def cached_packages() -> list[FirmwarePackage]:
    packages = []
    root = cache_dir() / "firmware"
    for folder in sorted(root.glob("*")) if root.is_dir() else []:
        try:
            packages.append(load_package(folder))
        except FirmwareError:
            continue
    return sorted(packages, key=lambda package: parse_version(package.version))


# ---------------------------------------------------------------------------
# ssmouse itself
# ---------------------------------------------------------------------------


def check_app_release(current: str = __version__) -> Optional[AppRelease]:
    """The newest ssmouse release on GitHub if newer than *current*."""
    try:
        response = _get(GITHUB_RELEASES_URL, accept="application/vnd.github+json")
    except UpdateError as exc:
        if "HTTP 404" in str(exc):  # no release published yet
            return None
        raise
    with response:
        try:
            data = json.loads(response.read().decode("utf-8"))
        except ValueError as exc:
            raise UpdateError("unexpected answer from GitHub: %s" % exc)
    tag = str(data.get("tag_name", "")).lstrip("v")
    if not tag:
        return None
    try:
        newer = parse_version(tag) > parse_version(current)
    except FirmwareError:
        return None
    return AppRelease(tag, data.get("html_url") or RELEASES_PAGE) if newer else None
