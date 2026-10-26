//! Per-OS config and cache directories (no extra dependencies).

use std::env;
use std::path::PathBuf;

fn home() -> PathBuf {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// `~/.config/ssmouse`, `%APPDATA%\ssmouse`, `~/Library/Application Support/ssmouse`.
pub fn config_dir() -> PathBuf {
    let base = if cfg!(windows) {
        env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join("AppData").join("Roaming"))
    } else if cfg!(target_os = "macos") {
        home().join("Library").join("Application Support")
    } else {
        env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".config"))
    };
    base.join("ssmouse")
}

/// `~/.cache/ssmouse`, `%LOCALAPPDATA%\ssmouse`, `~/Library/Caches/ssmouse`.
pub fn cache_dir() -> PathBuf {
    let base = if cfg!(windows) {
        env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join("AppData").join("Local"))
    } else if cfg!(target_os = "macos") {
        home().join("Library").join("Caches")
    } else {
        env::var_os("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".cache"))
    };
    base.join("ssmouse")
}

/// Write via a temporary file so a crash never leaves half a file behind.
pub fn write_atomic(path: &std::path::Path, contents: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, contents)?;
    std::fs::rename(tmp, path)
}
