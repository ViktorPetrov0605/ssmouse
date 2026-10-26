//! Tray app and settings window (`ssmouse-gui`).

pub mod icon;
pub mod monitor;
pub mod settings;
pub mod tray;
pub mod window;

use std::fs::{File, OpenOptions};

/// Hold a per-user lock named `name` for the life of the process; `None` if
/// another process already holds it (single instance).
pub fn instance_lock(name: &str) -> Option<File> {
    let user = std::env::var("USER")
        .or_else(|_| std::env::var("USERNAME"))
        .unwrap_or_default();
    let path = std::env::temp_dir().join(format!("ssmouse-{name}-{user}.lock"));
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)
        .ok()?;
    file.try_lock().ok()?;
    Some(file)
}
