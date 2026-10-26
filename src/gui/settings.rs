//! App settings shared by the tray and the settings window (`gui.json`).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// Fallback poll interval while the mouse is linked (events do the rest).
    pub interval_s: u64,
    pub auto_update_check: bool,
    pub last_update_check: u64,
    pub notified_gg: String,
    pub notified_app: String,
    pub last_battery: Option<u8>,
    pub last_seen: Option<u64>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            interval_s: 15,
            auto_update_check: true,
            last_update_check: 0,
            notified_gg: String::new(),
            notified_app: String::new(),
            last_battery: None,
            last_seen: None,
        }
    }
}

pub const INTERVALS: [(u64, &str); 7] = [
    (5, "5 seconds"),
    (10, "10 seconds"),
    (15, "15 seconds"),
    (30, "30 seconds"),
    (60, "1 minute"),
    (120, "2 minutes"),
    (300, "5 minutes"),
];

fn path() -> std::path::PathBuf {
    crate::paths::config_dir().join("gui.json")
}

impl Settings {
    pub fn load() -> Self {
        std::fs::read_to_string(path())
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) {
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = crate::paths::write_atomic(&path(), text.as_bytes());
        }
    }

    /// Load, change, save (the other process may have changed other fields).
    pub fn update(f: impl FnOnce(&mut Settings)) -> Settings {
        let mut s = Self::load();
        f(&mut s);
        s.save();
        s
    }
}

pub fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
