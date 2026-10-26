//! Read everything about the device at once (CLI `info`, tray, settings window).

use serde::Serialize;

use crate::device::Mouse;
use crate::protocol::{DpiInfo, PowerMode};

#[derive(Debug, Clone, Default, Serialize)]
pub struct Status {
    pub name: String,
    pub model: Option<&'static str>,
    pub path: String,
    pub interface: i32,
    /// `None` when the receiver did not answer the link query.
    pub connected: Option<bool>,
    pub battery: Option<u8>,
    #[serde(skip)]
    pub dpi: Option<DpiInfo>,
    pub dpi_presets: Vec<u16>,
    pub dpi_active: Option<u8>,
    pub current_cpi: Option<u16>,
    pub polling_hz: Option<u16>,
    pub firmware: Option<String>,
    #[serde(skip)]
    pub power: Option<PowerMode>,
    pub sleep_s: Option<u16>,
    pub errors: Vec<String>,
}

/// Reads only what can answer: an asleep mouse gets just the receiver queries.
pub fn read(mouse: &mut Mouse) -> Status {
    let mut status = Status {
        name: mouse.info.name.clone(),
        model: mouse.info.model,
        path: mouse.info.path.clone(),
        interface: mouse.info.interface_number,
        ..Default::default()
    };
    status.connected = mouse.is_linked();
    if status.connected == Some(false) {
        status.errors.push(crate::Error::NotLinked.to_string());
    } else {
        match mouse.battery() {
            Ok(level) => status.battery = Some(level),
            Err(e) => {
                status.errors.push(format!("battery: {e}"));
                mouse.retries = 1; // a sleeping mouse won't answer the rest either
            }
        }
        status.dpi = mouse.dpi();
        status.polling_hz = mouse.polling_rate();
        status.power = mouse.power_mode();
    }
    if let Some(dpi) = &status.dpi {
        status.dpi_presets = dpi.cpis();
        status.dpi_active = Some(dpi.active_level);
        status.current_cpi = dpi.current_cpi();
    }
    status.sleep_s = status.power.map(|p| p.idle_sleep);
    status.firmware = mouse.receiver_firmware().or_else(|| mouse.mouse_firmware());
    status
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn asleep_mouse_only_gets_receiver_queries() {
        let (mut mouse, writes) = crate::fake::asleep();
        let status = read(&mut mouse);
        assert_eq!(status.connected, Some(false));
        assert_eq!(status.battery, None);
        assert_eq!(status.firmware.as_deref(), Some("1.04"));
        assert!(
            writes
                .lock()
                .unwrap()
                .iter()
                .all(|w| matches!(w[0], 0xE8 | 0xA8 | 0xD0 | 0x90))
        );
    }

    #[test]
    fn linked_mouse() {
        let status = read(&mut crate::fake::mouse());
        assert_eq!(status.battery, Some(47));
        assert_eq!(status.dpi_presets, vec![400, 600, 1400, 2800, 4100]);
        assert_eq!(status.current_cpi, Some(600));
        assert_eq!(status.polling_hz, Some(250));
        assert_eq!(status.sleep_s, Some(900));
    }
}
