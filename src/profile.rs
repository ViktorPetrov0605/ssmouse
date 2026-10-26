//! The settings ssmouse manages, stored as JSON.
//!
//! Every section is optional (`None` = leave the mouse alone).  The mouse
//! cannot report its lighting or button mappings, so they are remembered
//! here; on the mouse everything is made permanent with a save to flash.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::bindings::{self, Bindings};
use crate::device::Mouse;
use crate::error::{Error, Result};
use crate::lighting::Effect;
use crate::protocol::{self as proto, PowerMode};

/// GG writes 100 for "sensor rest on".
pub const SENSOR_REST_ON: u8 = 100;
/// High-efficiency mode forces this report rate (as GG does).
pub const HIGH_EFFICIENCY_HZ: u16 = 125;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Dpi {
    pub presets: Vec<u16>,
    pub active: u8,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Power {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sleep_s: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sensor_rest: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub high_efficiency: Option<bool>,
}

impl Power {
    pub fn is_empty(&self) -> bool {
        self.sleep_s.is_none() && self.sensor_rest.is_none() && self.high_efficiency.is_none()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Debounce {
    pub make_ms: u8,
    pub break_ms: u8,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dpi: Option<Dpi>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub polling_hz: Option<u16>,
    #[serde(default, skip_serializing_if = "Power::is_empty")]
    pub power: Power,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lighting: Option<Effect>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub bindings: Bindings,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub debounce: Option<Debounce>,
}

pub fn default_path() -> PathBuf {
    crate::paths::config_dir().join("profile.json")
}

impl Profile {
    pub fn validate(&self) -> Result<()> {
        if let Some(dpi) = &self.dpi {
            if dpi.presets.is_empty() || dpi.presets.len() > proto::MAX_DPI_LEVELS {
                return Err(Error::Invalid("1..5 DPI presets are required".into()));
            }
            if dpi.presets.iter().any(|c| !(100..=18000).contains(c)) {
                return Err(Error::Invalid("DPI presets must be 100..18000".into()));
            }
            if dpi.active == 0 || dpi.active as usize > dpi.presets.len() {
                return Err(Error::Invalid("active DPI preset out of range".into()));
            }
        }
        if let Some(hz) = self.polling_hz {
            proto::polling_level(hz).ok_or_else(|| {
                Error::Invalid("polling rate must be 125, 250, 500 or 1000".into())
            })?;
        }
        if let Some(s) = self.power.sleep_s
            && !(60..=3600).contains(&s)
        {
            return Err(Error::Invalid("sleep timer must be 1..60 minutes".into()));
        }
        for (control, spec) in &self.bindings {
            bindings::default_for(control)
                .ok_or_else(|| Error::Invalid(format!("unknown control {control:?}")))?;
            bindings::encode(spec)?;
        }
        if let Some(effect) = &self.lighting {
            effect.validate()?;
        }
        Ok(())
    }

    /// Drop bindings that equal the default, so the file only lists changes.
    pub fn normalize(&mut self) {
        self.bindings
            .retain(|control, spec| bindings::default_for(control) != Some(spec.as_str()));
    }

    pub fn load_from(path: &Path) -> Result<Self> {
        match std::fs::read_to_string(path) {
            Ok(text) => {
                let profile: Profile = serde_json::from_str(&text)?;
                profile.validate()?;
                Ok(profile)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn load() -> Result<Self> {
        Self::load_from(&default_path())
    }

    pub fn save_to(&self, path: &Path) -> Result<()> {
        self.validate()?;
        let mut copy = self.clone();
        copy.normalize();
        let text = serde_json::to_string_pretty(&copy)? + "\n";
        crate::paths::write_atomic(path, text.as_bytes())?;
        Ok(())
    }

    pub fn save(&self) -> Result<()> {
        self.save_to(&default_path())
    }
}

/// Read-modify-write of the power settings; only after two identical,
/// plausible reads of the current values.
pub fn apply_power(mouse: &mut Mouse, power: &Power) -> Result<()> {
    mouse.require_link()?;
    let current = mouse.power_mode_confirmed().ok_or(Error::Timeout)?;
    let movement = match power.sensor_rest {
        Some(true) if current.movement_saving == 0 => SENSOR_REST_ON,
        Some(true) => current.movement_saving,
        Some(false) => 0,
        None => current.movement_saving,
    };
    let mode = PowerMode {
        power_saving: power
            .high_efficiency
            .map(u8::from)
            .unwrap_or(current.power_saving),
        movement_saving: movement,
        idle_lighting: current.idle_lighting,
        idle_sleep: power.sleep_s.unwrap_or(current.idle_sleep),
    };
    if mode != current {
        mouse.set_power_mode(mode)?;
    }
    Ok(())
}

pub const ALL_SECTIONS: [&str; 6] = [
    "dpi", "polling", "power", "debounce", "lighting", "bindings",
];

/// Write the configured sections and (with `persist`) save them to flash.
/// Returns the names of the sections written.
pub fn apply(
    mouse: &mut Mouse,
    profile: &Profile,
    sections: &[&str],
    persist: bool,
) -> Result<Vec<&'static str>> {
    profile.validate()?;
    let mut applied = Vec::new();
    let wants = |name: &str| sections.contains(&name);
    if let (true, Some(dpi)) = (wants("dpi"), &profile.dpi) {
        mouse.set_dpi(&dpi.presets, dpi.active)?;
        applied.push("dpi");
    }
    if wants("power") && !profile.power.is_empty() {
        apply_power(mouse, &profile.power)?;
        applied.push("power");
    }
    if wants("polling") {
        let hz = if profile.power.high_efficiency == Some(true) {
            Some(HIGH_EFFICIENCY_HZ)
        } else {
            profile.polling_hz
        };
        if let Some(hz) = hz
            && mouse.polling_rate() != Some(hz)
        {
            mouse.set_polling_rate(hz)?;
            applied.push("polling");
        }
    }
    if let (true, Some(d)) = (wants("debounce"), profile.debounce) {
        mouse.set_debounce(d.make_ms, d.break_ms)?;
        applied.push("debounce");
    }
    if let (true, Some(effect)) = (wants("lighting"), &profile.lighting) {
        mouse.set_lighting(effect)?;
        applied.push("lighting");
    }
    if wants("bindings") && !profile.bindings.is_empty() {
        mouse.set_bindings(&profile.bindings)?;
        applied.push("bindings");
    }
    if persist && !applied.is_empty() {
        mouse.save_to_flash()?;
    }
    Ok(applied)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lighting::Mode;

    #[test]
    fn roundtrip_and_python_compat() {
        let dir = std::env::temp_dir().join(format!("ssmouse-test-{}", std::process::id()));
        let path = dir.join("p.json");
        let mut profile = Profile {
            dpi: Some(Dpi {
                presets: vec![400, 1600],
                active: 2,
            }),
            polling_hz: Some(500),
            power: Power {
                sleep_s: Some(600),
                sensor_rest: Some(false),
                high_efficiency: None,
            },
            lighting: Some(Effect::new(Mode::Breathe, vec![[0, 255, 255]])),
            debounce: Some(Debounce {
                make_ms: 6,
                break_ms: 20,
            }),
            ..Default::default()
        };
        profile.bindings.insert("back".into(), "key:ctrl+c".into());
        profile.bindings.insert("left".into(), "left".into());
        profile.save_to(&path).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(!text.contains("\"left\""), "defaults are not stored");
        let loaded = Profile::load_from(&path).unwrap();
        assert_eq!(loaded.dpi, profile.dpi);
        assert_eq!(loaded.bindings.len(), 1);
        assert_eq!(
            Profile::load_from(&dir.join("missing.json")).unwrap(),
            Profile::default()
        );
        // A profile written by ssmouse 0.3 (Python).
        let py = r#"{"dpi": {"presets": [400, 800], "active": 2}, "polling_hz": 1000,
                     "power": {"sleep_s": 300}, "bindings": {"dpi": "key:ctrl+shift+z"},
                     "debounce": {"make_ms": 6, "break_ms": 20}}"#;
        let p: Profile = serde_json::from_str(py).unwrap();
        p.validate().unwrap();
        assert_eq!(p.bindings["dpi"], "key:ctrl+shift+z");
        assert!(
            serde_json::from_str::<Profile>(r#"{"polling_hz": 300}"#)
                .unwrap()
                .validate()
                .is_err()
        );
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn apply_writes_and_persists() {
        let (mut mouse, writes) = crate::fake::scripted(crate::fake::default_script());
        let mut profile = Profile {
            dpi: Some(Dpi {
                presets: vec![800],
                active: 1,
            }),
            power: Power {
                sleep_s: Some(600),
                sensor_rest: Some(false),
                high_efficiency: None,
            },
            lighting: Some(Effect::new(Mode::Steady, vec![[0, 0, 255]])),
            ..Default::default()
        };
        profile.bindings.insert("back".into(), "media:mute".into());
        let applied = apply(&mut mouse, &profile, &ALL_SECTIONS, true).unwrap();
        assert_eq!(applied, vec!["dpi", "power", "lighting", "bindings"]);
        let writes = writes.lock().unwrap();
        let ops: Vec<u8> = writes.iter().map(|w| w[0]).collect();
        for op in [0x20, 0x2B, 0x1C, 0x19] {
            assert!(ops.contains(&op), "{op:#x}");
        }
        assert_eq!(*ops.last().unwrap(), 0x09);
        let power = writes.iter().find(|w| w[0] == 0x2B).unwrap();
        assert_eq!(power[..7], [0x2B, 0, 0, 0x58, 0x02, 0x58, 0x02]);
    }
}
