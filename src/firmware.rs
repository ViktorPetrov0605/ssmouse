//! Firmware version check and update.
//!
//! A direct port of `fw_update` from SteelSeries GG's device descriptor.
//! Three images are written as 0x80-byte blocks:
//!
//! | Image                 | Size    | Device mask | Flash entry |
//! |-----------------------|---------|-------------|-------------|
//! | mouse (over 2.4 GHz)  | 0x12000 | 0x00        | 2           |
//! | receiver radio (nRF)  | 0x9000  | 0x40        | 2           |
//! | receiver USB (Holtek) | 0x4000  | 0x40        | 3           |
//!
//! Per image: erase `(08|mask) 00 <id>`, wait 1 s, then for every block three
//! buffer writes `(03|mask) 00 <off> <len> <data>` (0x30 + 0x30 + 0x20 bytes)
//! and a push `(05|mask) 00 <id> <block offset u32> 0080`.  Low-power gaming
//! mode and the polling rate are normalised first; afterwards the mouse (`01`)
//! and the receiver (`41`) are reset and the settings restored.
//!
//! There is no checksum or verification step, so [`update`] refuses unless
//! the images are newer than the installed firmware (or `force` is given).
//! SteelSeries firmware is never redistributed: it comes from a GG install or
//! is downloaded from SteelSeries (see [`crate::updates`]).

use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::Duration;

use crate::device::Mouse;
use crate::error::{Error, Result};
use crate::protocol as proto;

/// Newest firmware known to ssmouse (shipped with SteelSeries GG 122).
pub const LATEST_KNOWN: &str = "1.04";
pub const MIN_BATTERY: u8 = 30;
/// SteelSeries GG's command spacing during updates.
pub const UPDATE_INTERVAL: Duration = Duration::from_millis(12);
const BLOCK: usize = 0x80;
const BLOCK_CHUNKS: [(usize, usize); 3] = [(0x00, 0x30), (0x30, 0x30), (0x60, 0x20)];

pub struct Image {
    pub name: &'static str,
    pub prefix: &'static str,
    pub size: usize,
    pub mask: u8,
    pub entry: u16,
}

pub const IMAGES: [Image; 3] = [
    Image {
        name: "mouse",
        prefix: "firmware-rival-3-wireless-mouse-v",
        size: 0x12000,
        mask: 0x00,
        entry: 2,
    },
    Image {
        name: "receiver radio",
        prefix: "nordic-dongle-firmware-rival-3-wireless-v",
        size: 0x9000,
        mask: 0x40,
        entry: 2,
    },
    Image {
        name: "receiver USB",
        prefix: "holtek-dongle-firmware-rival-3-wireless-v",
        size: 0x4000,
        mask: 0x40,
        entry: 3,
    },
];

/// `"1.04.0.0"` / `"1.04"` -> `[1, 4]` (trailing zeros beyond two parts dropped).
pub fn parse_version(text: &str) -> Result<Vec<u32>> {
    let mut parts: Vec<u32> = text
        .split(|c: char| !c.is_ascii_digit())
        .filter(|p| !p.is_empty())
        .map(|p| {
            p.parse()
                .map_err(|_| Error::Firmware(format!("invalid version {text:?}")))
        })
        .collect::<Result<_>>()?;
    while parts.len() > 2 && parts.last() == Some(&0) {
        parts.pop();
    }
    if parts.is_empty() {
        return Err(Error::Firmware(format!("invalid version {text:?}")));
    }
    Ok(parts)
}

pub fn format_version(v: &[u32]) -> String {
    let mut out = v[0].to_string();
    for part in &v[1..] {
        out.push_str(&format!(".{part:02}"));
    }
    out
}

fn newer(a: &str, b: &str) -> bool {
    matches!((parse_version(a), parse_version(b)), (Ok(a), Ok(b)) if a > b)
}

#[derive(Debug, Clone, Default)]
pub struct FirmwareStatus {
    pub mouse: Option<String>,
    pub receiver: Option<String>,
}

impl FirmwareStatus {
    pub fn read(mouse: &mut Mouse) -> Self {
        Self {
            mouse: mouse.mouse_firmware(),
            receiver: mouse.receiver_firmware(),
        }
    }

    /// `Some(true)` if every known version is at least `latest`.
    pub fn up_to_date(&self, latest: &str) -> Option<bool> {
        let known: Vec<&String> = [&self.mouse, &self.receiver]
            .into_iter()
            .flatten()
            .collect();
        (!known.is_empty()).then(|| known.iter().all(|v| !newer(latest, v)))
    }
}

#[derive(Debug, Clone)]
pub struct Package {
    pub version: String,
    pub images: Vec<Vec<u8>>, // same order as IMAGES
    pub source: PathBuf,
}

fn check_image(image: &Image, data: &[u8]) -> Result<()> {
    if data.len() != image.size {
        return Err(Error::Firmware(format!(
            "{} image is {} bytes, expected {}",
            image.name,
            data.len(),
            image.size
        )));
    }
    let sp = u32::from_le_bytes(data[0..4].try_into().unwrap());
    let reset = u32::from_le_bytes(data[4..8].try_into().unwrap());
    // ARM Cortex-M vector table: initial SP in RAM, Thumb reset vector in flash.
    if !(0x2000_0000..=0x2010_0000).contains(&sp) || reset & 1 == 0 || reset >= 0x10_0000 {
        return Err(Error::Firmware(format!(
            "{} image does not look like Rival 3 Wireless firmware",
            image.name
        )));
    }
    Ok(())
}

/// Load the three images (and version.json) from a GG `firmware/272111664` folder.
pub fn load_package(folder: &Path) -> Result<Package> {
    let entries: Vec<String> = std::fs::read_dir(folder)
        .map_err(|e| Error::Firmware(format!("{}: {e}", folder.display())))?
        .filter_map(|e| e.ok().and_then(|e| e.file_name().into_string().ok()))
        .collect();
    let mut images = Vec::new();
    let mut file_version = None;
    for image in &IMAGES {
        let matches: Vec<&String> = entries
            .iter()
            .filter(|n| n.starts_with(image.prefix) && n.ends_with(".bin"))
            .collect();
        let [name] = matches.as_slice() else {
            return Err(Error::Firmware(format!(
                "expected one {} image in {}",
                image.name,
                folder.display()
            )));
        };
        let data = std::fs::read(folder.join(name))?;
        check_image(image, &data)?;
        images.push(data);
        file_version = Some(name[image.prefix.len()..name.len() - 4].to_string());
    }
    let mut version = file_version;
    if let Ok(text) = std::fs::read_to_string(folder.join("version.json")) {
        let meta: serde_json::Value = serde_json::from_str(&text)?;
        if let Some(device) = meta.get("device_name").and_then(|d| d.as_str())
            && device != "rival_3_wireless"
        {
            return Err(Error::Firmware(format!("firmware is for {device}")));
        }
        if let Some(v) = meta.get("firmware_version").and_then(|v| v.as_str()) {
            version = Some(v.to_string());
        }
    }
    let version = version
        .ok_or_else(|| Error::Firmware("could not determine the firmware version".into()))?;
    Ok(Package {
        version: format_version(&parse_version(&version)?),
        images,
        source: folder.to_path_buf(),
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    Write(Vec<u8>),
    /// The receiver resets while this is sent; errors are expected.
    WriteNoFail(Vec<u8>),
    Sleep(Duration),
    Progress(u8),
}

/// Every command of the update, in order (mirrors GG's `fw_update`).
/// `power` is the current power mode and `polling_level` the current level.
pub fn plan(
    package: &Package,
    power: Option<proto::PowerMode>,
    polling_level: Option<u8>,
) -> Vec<Step> {
    let mut steps = Vec::new();
    let ms = |ms| Step::Sleep(Duration::from_millis(ms));
    let total: usize = package.images.iter().map(Vec::len).sum();
    let lpgm = power.filter(|p| p.power_saving != 0);
    let polling = polling_level.filter(|level| *level != 0);
    if let Some(p) = lpgm {
        steps.push(Step::Write(proto::set_power(proto::PowerMode {
            power_saving: 0,
            ..p
        })));
        steps.push(ms(3000));
    }
    if polling.is_some() {
        steps.push(Step::Write(vec![0x17, 0x00]));
        steps.push(ms(1000));
    }
    let mut done = 0;
    for (image, data) in IMAGES.iter().zip(&package.images) {
        steps.push(ms(100));
        let mut erase = vec![0x08 | image.mask, 0x00];
        erase.extend_from_slice(&image.entry.to_le_bytes());
        steps.push(Step::Write(erase));
        steps.push(ms(1000));
        for base in (0..data.len()).step_by(BLOCK) {
            for (offset, size) in BLOCK_CHUNKS {
                let mut chunk = vec![0x03 | image.mask, 0x00];
                chunk.extend_from_slice(&(offset as u16).to_le_bytes());
                chunk.extend_from_slice(&(size as u16).to_le_bytes());
                chunk.extend_from_slice(&data[base + offset..base + offset + size]);
                steps.push(Step::Write(chunk));
            }
            let mut push = vec![0x05 | image.mask, 0x00];
            push.extend_from_slice(&image.entry.to_le_bytes());
            push.extend_from_slice(&(base as u32).to_le_bytes());
            push.extend_from_slice(&(BLOCK as u16).to_le_bytes());
            steps.push(Step::Write(push));
            done += BLOCK;
            steps.push(Step::Progress((done * 100 / total) as u8));
        }
    }
    steps.push(Step::Write(vec![0x01])); // reset the mouse so the new firmware runs
    steps.push(ms(2000));
    if let Some(level) = polling {
        steps.push(Step::Write(vec![0x17, level]));
        steps.push(ms(1000));
    }
    if let Some(p) = lpgm {
        steps.push(Step::Write(proto::set_power(p)));
        steps.push(ms(3000));
    }
    steps.push(Step::WriteNoFail(vec![0x41])); // reset the receiver
    steps.push(ms(2000));
    steps
}

pub fn estimate(steps: &[Step]) -> (usize, Duration) {
    let writes = steps
        .iter()
        .filter(|s| matches!(s, Step::Write(_) | Step::WriteNoFail(_)))
        .count();
    let sleeps: Duration = steps
        .iter()
        .filter_map(|s| {
            if let Step::Sleep(d) = s {
                Some(*d)
            } else {
                None
            }
        })
        .sum();
    (writes, sleeps + UPDATE_INTERVAL * writes as u32)
}

/// Check every precondition and return the plan (nothing is written).
pub fn prepare(mouse: &mut Mouse, package: &Package, force: bool) -> Result<Vec<Step>> {
    if mouse.is_linked() != Some(true) {
        return Err(Error::Firmware(
            "the mouse must be switched on and linked to the receiver".into(),
        ));
    }
    let status = FirmwareStatus::read(mouse);
    let (Some(m), Some(r)) = (&status.mouse, &status.receiver) else {
        return Err(Error::Firmware(
            "could not read the installed firmware versions".into(),
        ));
    };
    if !force && !newer(&package.version, m) && !newer(&package.version, r) {
        return Err(Error::Firmware(format!(
            "installed firmware (mouse {m}, receiver {r}) is already {} or newer",
            package.version
        )));
    }
    let battery = mouse.battery()?;
    if battery < MIN_BATTERY {
        return Err(Error::Firmware(format!(
            "battery at {battery}%; replace the batteries first (need {MIN_BATTERY}%)"
        )));
    }
    let power = mouse.power_mode();
    let level = mouse.polling_rate().and_then(proto::polling_level);
    Ok(plan(package, power, level))
}

/// Flash `package`.  `progress` receives 0..=100.
pub fn update(
    mouse: &mut Mouse,
    package: &Package,
    force: bool,
    mut progress: impl FnMut(u8),
) -> Result<()> {
    let steps = prepare(mouse, package, force)?;
    let saved = mouse.interval;
    mouse.interval = UPDATE_INTERVAL;
    let result = (|| {
        for step in &steps {
            match step {
                Step::Sleep(d) => sleep(*d),
                Step::Progress(p) => progress(*p),
                Step::Write(payload) => mouse.write(payload)?,
                Step::WriteNoFail(payload) => {
                    let _ = mouse.write(payload);
                }
            }
        }
        Ok(())
    })();
    mouse.interval = saved;
    result
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub fn synthetic_package(dir: &Path, version: &str) -> Package {
        std::fs::create_dir_all(dir).unwrap();
        for (image, fill) in IMAGES.iter().zip([1u8, 2, 3]) {
            let mut data = vec![fill; image.size];
            data[..8].copy_from_slice(&[0xE0, 0x5F, 0x00, 0x20, 0x21, 0x55, 0x02, 0x00]);
            std::fs::write(dir.join(format!("{}{version}.bin", image.prefix)), data).unwrap();
        }
        std::fs::write(
            dir.join("version.json"),
            format!(
                r#"{{"firmware_version": "{version}.0.0", "device_name": "rival_3_wireless"}}"#
            ),
        )
        .unwrap();
        load_package(dir).unwrap()
    }

    fn tmp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("ssmouse-fw-{name}-{}", std::process::id()))
    }

    #[test]
    fn versions() {
        assert_eq!(parse_version("1.04.0.0").unwrap(), vec![1, 4]);
        assert_eq!(format_version(&[1, 4]), "1.04");
        let s = FirmwareStatus {
            mouse: Some("1.04".into()),
            receiver: Some("1.04".into()),
        };
        assert_eq!(s.up_to_date("1.04"), Some(true));
        assert_eq!(s.up_to_date("1.05"), Some(false));
        assert_eq!(FirmwareStatus::default().up_to_date("1.04"), None);
    }

    #[test]
    fn package_validation() {
        let dir = tmp("pkg");
        let package = synthetic_package(&dir, "1.05");
        assert_eq!(package.version, "1.05");
        std::fs::write(
            dir.join("holtek-dongle-firmware-rival-3-wireless-v1.05.bin"),
            vec![0xFF; 0x4000],
        )
        .unwrap();
        assert!(
            load_package(&dir)
                .unwrap_err()
                .to_string()
                .contains("does not look like")
        );
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn plan_matches_vendor_sequence() {
        let dir = tmp("plan");
        let package = synthetic_package(&dir, "1.05");
        let power = proto::PowerMode {
            power_saving: 1,
            movement_saving: 50,
            idle_lighting: 1200,
            idle_sleep: 300,
        };
        let steps = plan(&package, Some(power), Some(1));
        let writes: Vec<&Vec<u8>> = steps
            .iter()
            .filter_map(|s| match s {
                Step::Write(p) | Step::WriteNoFail(p) => Some(p),
                _ => None,
            })
            .collect();
        assert_eq!(writes[0], &vec![0x2B, 0, 0x32, 0xB0, 0x04, 0x2C, 0x01]);
        assert_eq!(writes[1], &vec![0x17, 0]);
        assert_eq!(writes[2], &vec![0x08, 0, 2, 0]);
        assert_eq!(&writes[3][..6], &[0x03, 0, 0, 0, 0x30, 0]);
        assert_eq!(writes[3].len(), 6 + 0x30);
        assert_eq!(&writes[5][..6], &[0x03, 0, 0x60, 0, 0x20, 0]);
        assert_eq!(writes[6], &vec![0x05, 0, 2, 0, 0, 0, 0, 0, 0x80, 0]);
        let radio = 3 + (0x12000 / 0x80) * 4;
        assert_eq!(writes[radio], &vec![0x48, 0, 2, 0]);
        let usb = radio + 1 + (0x9000 / 0x80) * 4;
        assert_eq!(writes[usb], &vec![0x48, 0, 3, 0]);
        assert_eq!(writes[usb + 4], &vec![0x45, 0, 3, 0, 0, 0, 0, 0, 0x80, 0]);
        let n = writes.len();
        assert_eq!(
            writes[n - 4..],
            [
                &vec![0x01],
                &vec![0x17, 1],
                &vec![0x2B, 1, 0x32, 0xB0, 0x04, 0x2C, 0x01],
                &vec![0x41]
            ]
        );
        let progress: Vec<u8> = steps
            .iter()
            .filter_map(|s| {
                if let Step::Progress(p) = s {
                    Some(*p)
                } else {
                    None
                }
            })
            .collect();
        assert_eq!(*progress.last().unwrap(), 100);
        assert!(progress.is_sorted());
        let lean = plan(
            &package,
            Some(proto::PowerMode {
                power_saving: 0,
                ..power
            }),
            Some(0),
        );
        assert!(matches!(&lean[1], Step::Write(p) if p == &vec![0x08, 0, 2, 0]));
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn refuses_same_version_and_dry_run_writes_nothing() {
        let dir = tmp("refuse");
        let mut package = synthetic_package(&dir, "1.05");
        let (mut mouse, writes) = crate::fake::scripted(crate::fake::default_script());
        assert!(prepare(&mut mouse, &package, false).is_ok());
        assert!(
            writes
                .lock()
                .unwrap()
                .iter()
                .all(|w| !matches!(w[0], 0x03 | 0x05 | 0x08 | 0x43 | 0x45 | 0x48))
        );
        package.version = "1.04".into();
        assert!(
            prepare(&mut mouse, &package, false)
                .unwrap_err()
                .to_string()
                .contains("already")
        );
        std::fs::remove_dir_all(dir).ok();
    }
}
