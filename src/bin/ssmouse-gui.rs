//! `ssmouse-gui`: battery in the system tray plus the settings window.
//!
//! ```text
//! ssmouse-gui               tray + settings window
//! ssmouse-gui --minimized   tray only (used for start on login)
//! ssmouse-gui --window      settings window only (the tray opens it this way)
//! ssmouse-gui --fake        demo with a simulated mouse
//! ```
#![cfg_attr(windows, windows_subsystem = "windows")]

use std::process::{Command, ExitCode};

use ssmouse::gui;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let has = |flag: &str| args.iter().any(|a| a == flag);
    if has("--version") || has("-V") {
        println!("ssmouse-gui {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    if has("--help") || has("-h") {
        println!("usage: ssmouse-gui [--minimized | --window] [--fake]");
        return ExitCode::SUCCESS;
    }
    if has("--fake") {
        // SAFETY: no other threads exist yet.
        unsafe { std::env::set_var(ssmouse::fake::ENV_FAKE, "1") };
    }
    if has("--window") {
        let Some(_lock) = gui::instance_lock("window") else {
            return ExitCode::SUCCESS;
        };
        return match gui::window::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("ssmouse-gui: {e}");
                ExitCode::FAILURE
            }
        };
    }
    let Some(_lock) = gui::instance_lock("tray") else {
        // The tray is already running: just open the window.
        if let Ok(exe) = std::env::current_exe() {
            let _ = Command::new(exe)
                .arg("--window")
                .args(has("--fake").then_some("--fake"))
                .spawn();
        }
        return ExitCode::SUCCESS;
    };
    gui::tray::run(!has("--minimized"));
    ExitCode::SUCCESS
}
