//! Online update checks: new firmware (via SteelSeries GG releases) and new ssmouse versions.
//!
//! SteelSeries has no separate firmware feed: firmware ships inside GG
//! releases and GG compares the firmware it bundles with the device.
//!
//! 1. `GET https://steelseries.com/api/v2/releases/gg/check-version?platform=windows&current_version=X`
//!    answers 204 when GG X is current, otherwise 200 with `version`,
//!    `installer_url`, an MD5 `checksum` and `type`.  The previous release
//!    gets a small `patch` installer without firmware, very old ones a
//!    stepping-stone release, so the full installer is resolved separately.
//! 2. The installer is downloaded from SteelSeries' CDN, MD5-verified, and
//!    7-Zip extracts only `apps/engine/firmware/272111664/`.
//! 3. [`crate::firmware`] installs it only after the user confirms.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use md5::{Digest, Md5};
use serde::Deserialize;

use crate::error::{Error, Result};
use crate::firmware::{self, Package, parse_version};

pub const GG_CHECK_URL: &str = "https://steelseries.com/api/v2/releases/gg/check-version";
/// The GG release whose firmware ssmouse knows ([`firmware::LATEST_KNOWN`]).
pub const BUNDLED_GG_VERSION: &str = "122.0.0";
pub const FIRMWARE_DIR_IN_INSTALLER: &str = "apps/engine/firmware/272111664";
pub const GITHUB_RELEASES_URL: &str =
    "https://api.github.com/repos/ViktorPetrov0605/ssmouse/releases/latest";
pub const RELEASES_PAGE: &str = "https://github.com/ViktorPetrov0605/ssmouse/releases";
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct GgRelease {
    pub version: String,
    pub installer_url: String,
    /// MD5, upper-case hex ("" if unknown).
    #[serde(default)]
    pub checksum: String,
    /// "full" installer, or "patch" (no firmware inside).
    #[serde(default = "full", rename = "type")]
    pub kind: String,
}

fn full() -> String {
    "full".into()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppRelease {
    pub version: String,
    pub url: String,
}

fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(std::time::Duration::from_secs(30)))
        .user_agent(format!(
            "ssmouse/{APP_VERSION} (+https://github.com/ViktorPetrov0605/ssmouse)"
        ))
        .build()
        .into()
}

fn net_error(host: &str, e: ureq::Error) -> Error {
    match e {
        ureq::Error::StatusCode(code) => Error::Update(format!("{host} answered HTTP {code}")),
        other => Error::Update(format!("could not reach {host}: {other}")),
    }
}

/// Raw HTTP GET used by the checks; returns (status, body).
type Fetch<'a> = &'a dyn Fn(&str) -> Result<(u16, String)>;

pub fn http_get(url: &str) -> Result<(u16, String)> {
    let host = url.split('/').nth(2).unwrap_or(url).to_string();
    match agent().get(url).header("Accept", "application/json").call() {
        Ok(mut response) => {
            let status = response.status().as_u16();
            let body = if status == 204 {
                String::new()
            } else {
                response
                    .body_mut()
                    .read_to_string()
                    .map_err(|e| net_error(&host, e))?
            };
            Ok((status, body))
        }
        Err(ureq::Error::StatusCode(code)) => Ok((code, String::new())),
        Err(e) => Err(net_error(&host, e)),
    }
}

// ---------------------------------------------------------------------------
// State: newest GG release whose firmware we already looked at
// ---------------------------------------------------------------------------

fn state_path() -> PathBuf {
    crate::paths::cache_dir().join("state.json")
}

pub fn last_checked_gg() -> String {
    std::fs::read_to_string(state_path())
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("gg_version").and_then(|s| s.as_str()).map(String::from))
        .filter(|v| matches!((parse_version(v), parse_version(BUNDLED_GG_VERSION)), (Ok(a), Ok(b)) if a > b))
        .unwrap_or_else(|| BUNDLED_GG_VERSION.into())
}

pub fn remember_checked_gg(version: &str) -> Result<()> {
    let body = serde_json::json!({ "gg_version": version }).to_string();
    crate::paths::write_atomic(&state_path(), body.as_bytes())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// SteelSeries GG releases
// ---------------------------------------------------------------------------

fn query_gg(fetch: Fetch, current: &str) -> Result<Option<GgRelease>> {
    let (status, body) = fetch(&format!(
        "{GG_CHECK_URL}?platform=windows&current_version={current}"
    ))?;
    match status {
        204 => Ok(None),
        200 => {
            let mut release: GgRelease = serde_json::from_str(&body)
                .map_err(|e| Error::Update(format!("unexpected answer from SteelSeries: {e}")))?;
            if !release.installer_url.starts_with("https://") {
                return Err(Error::Update(
                    "unexpected installer URL from SteelSeries".into(),
                ));
            }
            release.checksum = release.checksum.to_uppercase();
            Ok(Some(release))
        }
        code => Err(Error::Update(format!(
            "steelseries.com answered HTTP {code}"
        ))),
    }
}

/// The newest GG release if newer than `current` (default: last checked).
pub fn check_gg_release_with(fetch: Fetch, current: Option<&str>) -> Result<Option<GgRelease>> {
    let current = current.map(String::from).unwrap_or_else(last_checked_gg);
    Ok(query_gg(fetch, &current)?.filter(
        |r| matches!((parse_version(&r.version), parse_version(&current)), (Ok(a), Ok(b)) if a > b),
    ))
}

pub fn check_gg_release(current: Option<&str>) -> Result<Option<GgRelease>> {
    check_gg_release_with(&http_get, current)
}

/// The full installer (which contains the firmware) for `release.version`.
pub fn full_installer_with(fetch: Fetch, release: &GgRelease) -> Result<GgRelease> {
    if release.kind == "full" {
        return Ok(release.clone());
    }
    let major = parse_version(&release.version)?[0];
    for back in [5, 10, 20, 30] {
        let current = format!("{}.0.0", major.saturating_sub(back).max(1));
        if let Some(candidate) = query_gg(fetch, &current)?
            && candidate.version == release.version
            && candidate.kind == "full"
        {
            return Ok(candidate);
        }
    }
    // Full installers follow a fixed naming scheme on SteelSeries' CDN; the
    // firmware images are still validated after extraction.
    Ok(GgRelease {
        version: release.version.clone(),
        installer_url: format!(
            "https://engine.steelseriescdn.com/SteelSeriesGG{}Setup.exe",
            release.version
        ),
        checksum: String::new(),
        kind: "full".into(),
    })
}

fn md5_file(path: &Path) -> Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Md5::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex_upper(&hasher.finalize()))
}

fn hex_upper(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02X}")).collect()
}

/// Download and MD5-verify the installer; reuses an already verified copy.
pub fn download_installer(
    release: &GgRelease,
    dest: &Path,
    mut progress: impl FnMut(u64, u64),
) -> Result<PathBuf> {
    std::fs::create_dir_all(dest)?;
    let name = release
        .installer_url
        .rsplit('/')
        .next()
        .unwrap_or("SteelSeriesGGSetup.exe");
    let target = dest.join(name);
    if target.exists() && !release.checksum.is_empty() && md5_file(&target)? == release.checksum {
        return Ok(target);
    }
    let partial = dest.join(format!("{name}.part"));
    let mut response = agent()
        .get(&release.installer_url)
        .call()
        .map_err(|e| net_error("engine.steelseriescdn.com", e))?;
    let total: u64 = response
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut reader = response.body_mut().with_config().limit(2 << 30).reader();
    let mut out = std::fs::File::create(&partial)?;
    let mut hasher = Md5::new();
    let mut buf = vec![0u8; 1 << 20];
    let mut done = 0u64;
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n])?;
        hasher.update(&buf[..n]);
        done += n as u64;
        progress(done, total);
    }
    drop(out);
    if !release.checksum.is_empty() && hex_upper(&hasher.finalize()) != release.checksum {
        std::fs::remove_file(&partial).ok();
        return Err(Error::Update(
            "download corrupted (MD5 mismatch); try again".into(),
        ));
    }
    std::fs::rename(&partial, &target)?;
    Ok(target)
}

pub fn find_7zip() -> Option<PathBuf> {
    let names: &[&str] = if cfg!(windows) {
        &["7z.exe", "7za.exe"]
    } else {
        &["7z", "7zz", "7za"]
    };
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        for name in names {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    if cfg!(windows) {
        for var in ["ProgramFiles", "ProgramFiles(x86)"] {
            if let Some(base) = std::env::var_os(var) {
                let candidate = PathBuf::from(base).join("7-Zip").join("7z.exe");
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

/// Extract the Rival 3 Wireless firmware folder from a GG installer.
pub fn extract_firmware(installer: &Path, dest: &Path) -> Result<Package> {
    let seven = find_7zip().ok_or_else(|| Error::Update(
        "7-Zip is needed to unpack the SteelSeries installer (Linux: install 7zip/p7zip, macOS: brew install sevenzip, Windows: 7-zip.org)".into()))?;
    let tmp = std::env::temp_dir().join(format!("ssmouse-gg-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let output = Command::new(seven)
        .arg("x")
        .arg("-y")
        .arg(format!("-o{}", tmp.display()))
        .arg(installer)
        .arg(format!("{FIRMWARE_DIR_IN_INSTALLER}/*"))
        .output()?;
    let source = tmp.join(FIRMWARE_DIR_IN_INSTALLER);
    let result = (|| {
        if !output.status.success() || !source.is_dir() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(Error::Update(format!(
                "could not extract the firmware: {}",
                stderr.trim()
            )));
        }
        let package = firmware::load_package(&source)?;
        let target = dest.join(&package.version);
        let _ = std::fs::remove_dir_all(&target);
        std::fs::create_dir_all(&target)?;
        for entry in std::fs::read_dir(&source)? {
            let entry = entry?;
            std::fs::copy(entry.path(), target.join(entry.file_name()))?;
        }
        firmware::load_package(&target)
    })();
    let _ = std::fs::remove_dir_all(&tmp);
    result
}

/// Check for a newer GG; if there is one, download it and extract its firmware.
pub fn fetch_firmware(
    current: Option<&str>,
    progress: impl FnMut(u64, u64),
) -> Result<Option<(GgRelease, Package)>> {
    let Some(release) = check_gg_release(current)? else {
        return Ok(None);
    };
    let release = full_installer_with(&http_get, &release)?;
    let base = crate::paths::cache_dir();
    let installer = download_installer(&release, &base.join("downloads"), progress)?;
    let package = extract_firmware(&installer, &base.join("firmware")).map_err(|e| {
        Error::Update(format!(
            "GG {} has no usable Rival 3 Wireless firmware: {e}",
            release.version
        ))
    })?;
    std::fs::remove_file(&installer).ok(); // ~430 MB; only the firmware is kept
    remember_checked_gg(&release.version)?;
    Ok(Some((release, package)))
}

/// Newest firmware downloaded earlier, if any.
pub fn best_package() -> Option<Package> {
    let root = crate::paths::cache_dir().join("firmware");
    std::fs::read_dir(root)
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| firmware::load_package(&e.path()).ok())
        .max_by(|a, b| {
            parse_version(&a.version)
                .ok()
                .cmp(&parse_version(&b.version).ok())
        })
}

// ---------------------------------------------------------------------------
// ssmouse itself
// ---------------------------------------------------------------------------

pub fn check_app_release_with(fetch: Fetch, current: &str) -> Result<Option<AppRelease>> {
    let (status, body) = fetch(GITHUB_RELEASES_URL)?;
    if status == 404 {
        return Ok(None); // nothing published yet
    }
    if status != 200 {
        return Err(Error::Update(format!(
            "api.github.com answered HTTP {status}"
        )));
    }
    let data: serde_json::Value = serde_json::from_str(&body)
        .map_err(|e| Error::Update(format!("unexpected answer from GitHub: {e}")))?;
    let tag = data
        .get("tag_name")
        .and_then(|t| t.as_str())
        .unwrap_or("")
        .trim_start_matches('v')
        .to_string();
    let newer = matches!((parse_version(&tag), parse_version(current)), (Ok(a), Ok(b)) if a > b);
    Ok(newer.then(|| AppRelease {
        version: tag,
        url: data
            .get("html_url")
            .and_then(|u| u.as_str())
            .unwrap_or(RELEASES_PAGE)
            .to_string(),
    }))
}

pub fn check_app_release() -> Result<Option<AppRelease>> {
    check_app_release_with(&http_get, APP_VERSION)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded server behaviour: 204 when current, a patch for the previous
    /// release, the full installer for older clients.
    fn server(url: &str) -> Result<(u16, String)> {
        if url.starts_with(GG_CHECK_URL) {
            let current = url.split("current_version=").nth(1).unwrap();
            let major: u32 = current.split('.').next().unwrap().parse().unwrap();
            let body = match major {
                122.. => return Ok((204, String::new())),
                121 => {
                    r#"{"version":"122.0.0","type":"patch","checksum":"00","installer_url":"https://engine.steelseriescdn.com/SteelSeriesGG122.0.0PatchFrom121.0.0Setup.exe"}"#
                }
                _ => {
                    r#"{"version":"122.0.0","type":"full","checksum":"a09436242d0f9c1e7bd0c4a477f00361","installer_url":"https://engine.steelseriescdn.com/SteelSeriesGG122.0.0Setup.exe"}"#
                }
            };
            return Ok((200, body.into()));
        }
        Ok((404, String::new()))
    }

    #[test]
    fn gg_release_checks() {
        assert_eq!(
            check_gg_release_with(&server, Some("122.0.0")).unwrap(),
            None
        );
        let patch = check_gg_release_with(&server, Some("121.0.0"))
            .unwrap()
            .unwrap();
        assert_eq!(patch.kind, "patch");
        let full = full_installer_with(&server, &patch).unwrap();
        assert_eq!(full.kind, "full");
        assert!(
            full.installer_url
                .ends_with("SteelSeriesGG122.0.0Setup.exe")
        );
        assert_eq!(full.checksum, "A09436242D0F9C1E7BD0C4A477F00361");
    }

    #[test]
    fn app_release_checks() {
        assert_eq!(check_app_release_with(&server, "0.4.0").unwrap(), None);
        let gh = |_: &str| {
            Ok((
                200,
                r#"{"tag_name":"v9.1.0","html_url":"https://example/r"}"#.to_string(),
            ))
        };
        assert_eq!(
            check_app_release_with(&gh, "0.4.0").unwrap(),
            Some(AppRelease {
                version: "9.1.0".into(),
                url: "https://example/r".into()
            })
        );
        assert_eq!(check_app_release_with(&gh, "9.1.0").unwrap(), None);
    }

    #[test]
    fn extract_from_archive() {
        let Some(seven) = find_7zip() else { return };
        let root = std::env::temp_dir().join(format!("ssmouse-x-{}", std::process::id()));
        let src = root.join("src");
        crate::firmware::tests::synthetic_package(&src.join(FIRMWARE_DIR_IN_INSTALLER), "1.05");
        std::fs::write(src.join("unrelated.txt"), "x").unwrap();
        let archive = root.join("installer.7z");
        let ok = Command::new(seven)
            .current_dir(&src)
            .args(["a", archive.to_str().unwrap(), "apps", "unrelated.txt"])
            .output()
            .unwrap()
            .status
            .success();
        assert!(ok);
        let package = extract_firmware(&archive, &root.join("out")).unwrap();
        assert_eq!(package.version, "1.05");
        assert!(!root.join("out/1.05/unrelated.txt").exists());
        std::fs::remove_dir_all(root).ok();
    }
}
