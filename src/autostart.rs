//! Start the tray on login: XDG autostart (Linux), Run key (Windows), LaunchAgent (macOS).

use std::path::PathBuf;

use crate::error::Result;

const LABEL: &str = "io.github.viktorpetrov0605.ssmouse";

/// The tray binary, started hidden.
fn command() -> Vec<String> {
    let exe = std::env::current_exe().ok().map(|p| {
        p.with_file_name(if cfg!(windows) {
            "ssmouse-gui.exe"
        } else {
            "ssmouse-gui"
        })
    });
    let exe = exe
        .filter(|p| p.exists())
        .or_else(|| std::env::current_exe().ok())
        .unwrap_or_else(|| PathBuf::from("ssmouse-gui"));
    vec![exe.to_string_lossy().into_owned(), "--minimized".into()]
}

pub fn entry_path() -> PathBuf {
    if cfg!(target_os = "macos") {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
            .join("Library/LaunchAgents")
            .join(format!("{LABEL}.plist"))
    } else {
        let base = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config")
            });
        base.join("autostart").join("ssmouse-gui.desktop")
    }
}

fn quote(arg: &str) -> String {
    if arg
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "/._-".contains(c))
    {
        arg.into()
    } else {
        format!("\"{}\"", arg.replace('"', "\\\""))
    }
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

#[cfg(windows)]
mod imp {
    use super::*;
    use winreg::RegKey;
    use winreg::enums::HKEY_CURRENT_USER;

    const RUN: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

    pub fn is_enabled() -> bool {
        RegKey::predef(HKEY_CURRENT_USER)
            .open_subkey(RUN)
            .and_then(|k| k.get_value::<String, _>("ssmouse"))
            .is_ok()
    }

    pub fn set_enabled(enabled: bool) -> Result<()> {
        let (key, _) = RegKey::predef(HKEY_CURRENT_USER).create_subkey(RUN)?;
        if enabled {
            key.set_value(
                "ssmouse",
                &command()
                    .iter()
                    .map(|a| quote(a))
                    .collect::<Vec<_>>()
                    .join(" "),
            )?;
        } else {
            let _ = key.delete_value("ssmouse");
        }
        Ok(())
    }
}

#[cfg(not(windows))]
mod imp {
    use super::*;

    pub fn is_enabled() -> bool {
        entry_path().is_file()
    }

    pub fn set_enabled(enabled: bool) -> Result<()> {
        let path = entry_path();
        if !enabled {
            let _ = std::fs::remove_file(path);
            return Ok(());
        }
        let args = command();
        let contents = if cfg!(target_os = "macos") {
            let items: String = args
                .iter()
                .map(|a| format!("<string>{}</string>", xml_escape(a)))
                .collect();
            format!(
                concat!(
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
                    "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n",
                    "<plist version=\"1.0\"><dict><key>Label</key><string>{}</string>",
                    "<key>ProgramArguments</key><array>{}</array>",
                    "<key>RunAtLoad</key><true/><key>ProcessType</key><string>Interactive</string></dict></plist>\n"
                ),
                LABEL, items
            )
        } else {
            format!(
                "[Desktop Entry]\nType=Application\nName=ssmouse\nComment=SteelSeries mouse battery in the system tray\nExec={}\nIcon=input-mouse\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
                args.iter().map(|a| quote(a)).collect::<Vec<_>>().join(" ")
            )
        };
        crate::paths::write_atomic(&path, contents.as_bytes())?;
        Ok(())
    }
}

pub use imp::{is_enabled, set_enabled};

#[cfg(all(test, target_os = "linux"))]
mod tests {
    #[test]
    fn xdg_entry() {
        let dir = std::env::temp_dir().join(format!("ssmouse-auto-{}", std::process::id()));
        // SAFETY: tests touching XDG_CONFIG_HOME run in this one test only.
        unsafe { std::env::set_var("XDG_CONFIG_HOME", &dir) };
        assert!(!super::is_enabled());
        super::set_enabled(true).unwrap();
        let text = std::fs::read_to_string(super::entry_path()).unwrap();
        assert!(text.starts_with("[Desktop Entry]") && text.contains("--minimized"));
        super::set_enabled(false).unwrap();
        assert!(!super::is_enabled());
        std::fs::remove_dir_all(dir).ok();
    }
}
