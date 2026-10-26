//! Device discovery, transport and the high-level [`Mouse`] API.

use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::error::{Error, Result};
use crate::protocol::{self as proto, DpiInfo, PowerMode};

/// One HID interface of a supported receiver.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Interface {
    pub path: String,
    pub vendor_id: u16,
    pub product_id: u16,
    pub interface_number: i32,
    pub usage_page: u16,
    pub product: String,
    pub serial: String,
}

/// Bytes in, bytes out.  Implemented by hidapi and by [`FakeTransport`].
pub trait Transport: Send {
    /// Send one command (opcode first, without report ID).
    fn write(&mut self, payload: &[u8]) -> Result<()>;
    /// One input report, or `None` after `timeout_ms`.
    fn read(&mut self, timeout_ms: i32) -> Result<Option<Vec<u8>>>;
}

pub fn enumerate() -> Result<Vec<Interface>> {
    let api = hidapi::HidApi::new().map_err(hid_error)?;
    let mut found: Vec<Interface> = api
        .device_list()
        .filter(|d| {
            d.vendor_id() == proto::VENDOR_ID && proto::KNOWN_PIDS.contains(&d.product_id())
        })
        .map(|d| Interface {
            path: d.path().to_string_lossy().into_owned(),
            vendor_id: d.vendor_id(),
            product_id: d.product_id(),
            interface_number: d.interface_number(),
            usage_page: d.usage_page(),
            product: d.product_string().unwrap_or_default().to_string(),
            serial: d.serial_number().unwrap_or_default().to_string(),
        })
        .collect();
    found.sort_by(|a, b| a.path.cmp(&b.path));
    found.dedup_by(|a, b| a.path == b.path && a.usage_page == b.usage_page);
    Ok(found)
}

/// The command interface: usage page 0xFFC0 (interface 3 on the Rival 3 Wireless).
pub fn control_interface(interfaces: &[Interface]) -> Option<&Interface> {
    interfaces
        .iter()
        .filter(|i| i.usage_page == proto::USAGE_PAGE_CONTROL || i.interface_number == 3)
        .min_by_key(|i| {
            (
                i.usage_page != proto::USAGE_PAGE_CONTROL,
                i.interface_number != 3,
            )
        })
}

/// The interface the receiver pushes events on (usage page 0xFF00, interface 2).
pub fn event_interface(interfaces: &[Interface]) -> Option<&Interface> {
    interfaces
        .iter()
        .filter(|i| i.usage_page == proto::USAGE_PAGE_EVENTS || i.interface_number == 2)
        .min_by_key(|i| {
            (
                i.usage_page != proto::USAGE_PAGE_EVENTS,
                i.interface_number != 2,
            )
        })
}

fn hid_error(error: hidapi::HidError) -> Error {
    let text = error.to_string();
    let lower = text.to_lowercase();
    if lower.contains("permission")
        || lower.contains("os error 13")
        || lower.contains("access is denied")
    {
        Error::Permission(text)
    } else {
        Error::Hid(text)
    }
}

pub struct HidTransport {
    device: hidapi::HidDevice,
}

impl HidTransport {
    pub fn open(interface: &Interface) -> Result<Self> {
        let api = hidapi::HidApi::new().map_err(hid_error)?;
        let path = CString::new(interface.path.clone())
            .map_err(|_| Error::Hid("bad device path".into()))?;
        let device = api.open_path(&path).map_err(hid_error)?;
        Ok(Self { device })
    }
}

impl Transport for HidTransport {
    fn write(&mut self, payload: &[u8]) -> Result<()> {
        // Unnumbered reports: report ID 0, then the 64-byte report.  Works with
        // Linux hidraw, Windows (65-byte output reports) and macOS alike.
        let mut report = [0u8; proto::REPORT_SIZE + 1];
        let len = payload.len().min(proto::REPORT_SIZE);
        report[1..=len].copy_from_slice(&payload[..len]);
        self.device.write(&report).map_err(hid_error)?;
        Ok(())
    }

    fn read(&mut self, timeout_ms: i32) -> Result<Option<Vec<u8>>> {
        let mut buf = [0u8; proto::REPORT_SIZE];
        let n = self
            .device
            .read_timeout(&mut buf, timeout_ms)
            .map_err(hid_error)?;
        Ok((n > 0).then(|| buf[..n].to_vec()))
    }
}

// ---------------------------------------------------------------------------
// Cross-process lock: the tray and the settings window must not interleave
// commands on the same interface (every reader sees every response).
// ---------------------------------------------------------------------------

pub struct DeviceLock {
    _file: File,
}

impl DeviceLock {
    pub fn acquire(timeout: Duration) -> Result<Self> {
        let user = std::env::var("USER")
            .or_else(|_| std::env::var("USERNAME"))
            .unwrap_or_default();
        let path = std::env::temp_dir().join(format!("ssmouse-{user}.lock"));
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(&path)?;
        let deadline = Instant::now() + timeout;
        loop {
            match file.try_lock() {
                Ok(()) => return Ok(Self { _file: file }),
                Err(std::fs::TryLockError::WouldBlock) if Instant::now() < deadline => {
                    sleep(Duration::from_millis(20))
                }
                Err(std::fs::TryLockError::WouldBlock) => return Err(Error::Busy),
                Err(std::fs::TryLockError::Error(e)) => return Err(e.into()),
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Mouse
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceInfo {
    pub name: String,
    pub product_id: u16,
    pub model: Option<&'static str>,
    pub path: String,
    pub interface_number: i32,
}

pub struct Mouse {
    transport: Box<dyn Transport>,
    pub info: DeviceInfo,
    last_command: Option<Instant>,
    pub timeout_ms: i32,
    pub retries: u32,
    /// Minimum spacing between commands (firmware updates use 12 ms).
    pub interval: Duration,
    /// Hexdump every report on stderr (`--debug`).
    pub debug: bool,
    _lock: Option<DeviceLock>,
}

impl Mouse {
    /// Find the receiver and open its command interface.
    pub fn open() -> Result<Self> {
        if crate::fake::requested() {
            return Ok(crate::fake::mouse());
        }
        let interfaces = enumerate()?;
        let interface = control_interface(&interfaces)
            .ok_or(Error::NotFound)?
            .clone();
        let lock = DeviceLock::acquire(Duration::from_secs(5))?;
        let transport = HidTransport::open(&interface)?;
        let mut mouse = Self::with_transport(Box::new(transport), info_for(&interface));
        mouse._lock = Some(lock);
        Ok(mouse)
    }

    pub fn with_transport(transport: Box<dyn Transport>, info: DeviceInfo) -> Self {
        Self {
            transport,
            info,
            last_command: None,
            timeout_ms: 400,
            retries: 2,
            interval: Duration::from_millis(proto::COMMAND_INTERVAL_MS),
            debug: false,
            _lock: None,
        }
    }

    fn pace(&mut self) {
        if let Some(last) = self.last_command {
            let elapsed = last.elapsed();
            if elapsed < self.interval {
                sleep(self.interval - elapsed);
            }
        }
        self.last_command = Some(Instant::now());
    }

    /// Send a command without waiting for a response (writes are not acknowledged).
    pub fn write(&mut self, payload: &[u8]) -> Result<()> {
        self.pace();
        if self.debug {
            eprintln!("TX {}", hex(payload));
        }
        self.transport.write(payload)
    }

    pub fn write_all<I: IntoIterator<Item = Vec<u8>>>(&mut self, payloads: I) -> Result<()> {
        payloads
            .into_iter()
            .try_for_each(|payload| self.write(&payload))
    }

    /// Send a read command and return the first report `parse` accepts.
    ///
    /// Reports that do not fit (late or duplicated answers to earlier
    /// commands) are skipped instead of being misread.
    pub fn query<T>(&mut self, request: &[u8], parse: fn(&[u8]) -> Option<T>) -> Result<T> {
        for attempt in 0..self.retries.max(1) {
            while self.transport.read(0)?.is_some() {}
            self.write(request)?;
            let deadline = Instant::now() + Duration::from_millis(self.timeout_ms.max(0) as u64);
            while let Some(left) = deadline.checked_duration_since(Instant::now()) {
                let Some(report) = self.transport.read(left.as_millis().max(1) as i32)? else {
                    break;
                };
                if self.debug {
                    eprintln!("RX {}", hex(&report[..report.len().min(16)]));
                }
                if let Some(value) = parse(&report) {
                    return Ok(value);
                }
                if self.debug {
                    eprintln!("   (ignored: not an answer to {})", hex(request));
                }
            }
            if attempt + 1 < self.retries {
                sleep(Duration::from_millis(50));
            }
        }
        Err(Error::Timeout)
    }

    /// Raw variant: the first report of any shape.
    pub fn transact(&mut self, request: &[u8]) -> Result<Vec<u8>> {
        self.query(request, |r| Some(r.to_vec()))
    }

    fn read_opt<T>(&mut self, request: &[u8], parse: fn(&[u8]) -> Option<T>) -> Option<T> {
        self.query(request, parse).ok()
    }

    /// True once the link has been reported up `times` times in a row.
    pub fn wait_for_stable_link(&mut self, timeout: Duration, times: u32) -> bool {
        let deadline = Instant::now() + timeout;
        let mut streak = 0;
        while Instant::now() < deadline {
            if self.is_linked() == Some(true) {
                streak += 1;
                if streak >= times {
                    return true;
                }
            } else {
                streak = 0;
            }
            sleep(Duration::from_millis(300));
        }
        false
    }

    // -- reads --------------------------------------------------------------

    /// Whether the receiver has a radio link to the mouse (it answers itself).
    pub fn is_linked(&mut self) -> Option<bool> {
        self.read_opt(proto::CMD_RF_STATUS, proto::parse_rf_status)
            .or_else(|| self.read_opt(proto::CMD_RF_STATUS_FALLBACK, proto::parse_rf_status))
    }

    /// Error unless the mouse is linked (asleep mice never answer).
    pub fn require_link(&mut self) -> Result<()> {
        match self.is_linked() {
            Some(false) => Err(Error::NotLinked),
            _ => Ok(()),
        }
    }

    pub fn battery(&mut self) -> Result<u8> {
        self.query(proto::CMD_BATTERY, proto::parse_battery_opt)
    }

    /// Power mode read twice with identical results (guards read-modify-write).
    pub fn power_mode_confirmed(&mut self) -> Option<PowerMode> {
        let first = self.power_mode()?;
        (self.power_mode()? == first).then_some(first)
    }

    pub fn dpi(&mut self) -> Option<DpiInfo> {
        self.read_opt(proto::CMD_DPI, proto::parse_dpi)
    }

    pub fn dpi_level(&mut self) -> Option<u8> {
        self.read_opt(proto::CMD_DPI_LEVEL, proto::parse_dpi_level)
    }

    pub fn polling_rate(&mut self) -> Option<u16> {
        self.read_opt(proto::CMD_POLLING, proto::parse_polling)
    }

    pub fn receiver_firmware(&mut self) -> Option<String> {
        self.read_opt(proto::CMD_FW_RECEIVER, proto::parse_firmware)
    }

    pub fn mouse_firmware(&mut self) -> Option<String> {
        self.read_opt(proto::CMD_FW_MOUSE, proto::parse_firmware)
    }

    pub fn power_mode(&mut self) -> Option<PowerMode> {
        self.read_opt(proto::CMD_POWER, proto::parse_power)
    }

    pub fn debounce(&mut self) -> Option<(u8, u8)> {
        self.read_opt(proto::CMD_DEBOUNCE, proto::parse_debounce)
    }

    // -- writes -------------------------------------------------------------

    /// Store 1..5 presets and select one; returns the CPI actually used.
    pub fn set_dpi(&mut self, cpis: &[u16], active: u8) -> Result<Vec<u16>> {
        let sensors: Vec<u8> = cpis
            .iter()
            .map(|cpi| proto::sensor_from_cpi(*cpi))
            .collect();
        self.write(&proto::set_dpi(&sensors, active)?)?;
        Ok(cpis.iter().map(|cpi| proto::snap_cpi(*cpi)).collect())
    }

    pub fn set_dpi_level(&mut self, level: u8) -> Result<()> {
        let info = self.dpi().ok_or(Error::Timeout)?;
        let sensors: Vec<u8> = info.presets.iter().map(|p| p.sensor as u8).collect();
        self.write(&proto::set_dpi(&sensors, level)?)
    }

    /// Change the report rate.  The mouse drops the radio link (sometimes
    /// twice) while it switches; wait until the link is stable again.
    pub fn set_polling_rate(&mut self, hz: u16) -> Result<()> {
        let command = proto::set_polling(hz)?;
        self.require_link()?;
        self.write(&command)?;
        sleep(Duration::from_millis(1500));
        if !self.wait_for_stable_link(Duration::from_secs(8), 3) {
            return Err(Error::NotLinked);
        }
        Ok(())
    }

    /// Restart the receiver (USB re-enumerates; reopen the device afterwards).
    pub fn reset_receiver(&mut self) -> Result<()> {
        self.write(proto::RESET_RECEIVER)
    }

    pub fn set_power_mode(&mut self, mode: PowerMode) -> Result<()> {
        self.write(&proto::set_power(mode))
    }

    pub fn set_debounce(&mut self, make_ms: u8, break_ms: u8) -> Result<()> {
        self.write(&proto::set_debounce(make_ms, break_ms))
    }

    pub fn set_live_color(&mut self, rgb: [u8; 3]) -> Result<()> {
        self.write(&proto::live_color(rgb))
    }

    pub fn set_lighting(&mut self, effect: &crate::lighting::Effect) -> Result<()> {
        self.write_all(crate::lighting::payloads(effect))
    }

    /// Remap buttons and wheel; controls not listed keep their default action.
    pub fn set_bindings(&mut self, bindings: &crate::bindings::Bindings) -> Result<()> {
        self.write_all(crate::bindings::payloads(bindings)?)
    }

    /// Persist settings in flash.  Lighting and button mappings are lost at
    /// power-off without this (verified on hardware).
    pub fn save_to_flash(&mut self) -> Result<()> {
        self.write(proto::SAVE_TO_FLASH)
    }

    /// Restore factory settings and store them.
    pub fn factory_reset(&mut self) -> Result<()> {
        self.write(proto::FACTORY_RESET)?;
        sleep(Duration::from_millis(500));
        self.save_to_flash()
    }

    /// GG's pairing flow: `71 00`, poll the link until it holds, `73` on timeout.
    pub fn pair(&mut self) -> Result<bool> {
        self.write(proto::PAIR)?;
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut linked = 0;
        while Instant::now() < deadline {
            sleep(Duration::from_secs(2));
            if self.is_linked() == Some(true) {
                linked += 1;
                if linked >= 2 {
                    return Ok(true);
                }
            } else {
                linked = 0;
            }
        }
        self.write(proto::RESET_WIRELESS)?;
        Ok(false)
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|b| format!("{b:02X}"))
        .collect::<Vec<_>>()
        .join(" ")
}

fn info_for(interface: &Interface) -> DeviceInfo {
    DeviceInfo {
        name: if interface.product.is_empty() {
            proto::product_name(interface.product_id)
        } else {
            interface.product.clone()
        },
        product_id: interface.product_id,
        model: (interface.product_id == proto::PID_RIVAL3_WIRELESS)
            .then_some(proto::MODEL_RIVAL3_WIRELESS),
        path: interface.path.clone(),
        interface_number: interface.interface_number,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn iface(number: i32, page: u16) -> Interface {
        Interface {
            path: format!("/dev/hidraw{number}"),
            vendor_id: proto::VENDOR_ID,
            product_id: proto::PID_RIVAL3_WIRELESS,
            interface_number: number,
            usage_page: page,
            product: String::new(),
            serial: String::new(),
        }
    }

    #[test]
    fn picks_interfaces() {
        let all = vec![iface(0, 1), iface(1, 1), iface(2, 0xFF00), iface(3, 0xFFC0)];
        assert_eq!(control_interface(&all).unwrap().interface_number, 3);
        assert_eq!(event_interface(&all).unwrap().interface_number, 2);
        // No usage pages reported: fall back to interface numbers.
        let bare = vec![iface(2, 0), iface(3, 0)];
        assert_eq!(control_interface(&bare).unwrap().interface_number, 3);
        assert!(control_interface(&[iface(0, 1)]).is_none());
    }

    #[test]
    fn transact_drains_and_retries() {
        let (mut mouse, writes) = crate::fake::scripted(vec![]);
        assert!(matches!(mouse.transact(&[0xAA, 0x01]), Err(Error::Timeout)));
        assert_eq!(writes.lock().unwrap().len(), 2); // two attempts
    }

    #[test]
    fn reads_via_fake() {
        let mut mouse = crate::fake::mouse();
        assert_eq!(mouse.battery().unwrap(), 47);
        assert_eq!(
            mouse.dpi().unwrap().cpis(),
            vec![400, 600, 1400, 2800, 4100]
        );
        assert_eq!(mouse.polling_rate(), Some(250));
        assert_eq!(mouse.is_linked(), Some(true));
        assert_eq!(mouse.receiver_firmware().as_deref(), Some("1.04"));
    }
}
