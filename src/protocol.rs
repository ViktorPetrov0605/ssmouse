//! Wire protocol of the SteelSeries Rival 3 Wireless (USB `1038:1830`).
//!
//! Recovered from SteelSeries GG's device descriptor and verified on hardware
//! (firmware 1.04). Commands are 64-byte output reports on HID interface 3
//! (usage page `0xFFC0`), opcode first; responses are 64-byte input reports.
//!
//! | Purpose              | Request                         | Response                                  |
//! |----------------------|---------------------------------|-------------------------------------------|
//! | Battery              | `AA 01`                         | u16 LE level %, charge byte (always 0)    |
//! | DPI presets          | `A0`                            | count, active, 5 x u16 LE sensor values   |
//! | Active DPI preset    | `96`                            | level 1..5                                |
//! | Polling rate         | `97`                            | level 0..3 = 1000/500/250/125 Hz          |
//! | Firmware             | `D0` receiver, `90` mouse       | major, minor                              |
//! | Link status          | `E8` (fallback `A8`)            | 4 = linked, 1 = asleep/off                |
//! | Power mode           | `AB`                            | saving, movement, u16 idle_light, u16 sleep_s |
//! | Debounce             | `9E`                            | make_ms, break_ms                         |
//!
//! Writes get no response: `17` polling, `20` DPI, `2B` power, `1E` debounce,
//! `13` live colour, `18` restart lighting, `19`/`1A` mappings, `09` save to
//! flash, `1B` factory reset, `71`/`73` pairing.

use std::fmt;

pub const VENDOR_ID: u16 = 0x1038;
pub const PID_RIVAL3_WIRELESS: u16 = 0x1830;
/// Related wireless family PIDs, detected but not verified.
pub const KNOWN_PIDS: [u16; 4] = [PID_RIVAL3_WIRELESS, 0x1832, 0x1834, 0x1836];
pub const MODEL_RIVAL3_WIRELESS: &str = "M-0000022";
/// Vendor command interface (interface 3).
pub const USAGE_PAGE_CONTROL: u16 = 0xFFC0;
/// Receiver "sync" interface that pushes events (interface 2).
pub const USAGE_PAGE_EVENTS: u16 = 0xFF00;
pub const REPORT_SIZE: usize = 64;
/// Minimum spacing between commands (vendor descriptor: 30 ms).
pub const COMMAND_INTERVAL_MS: u64 = 30;
pub const MAX_DPI_LEVELS: usize = 5;

pub fn product_name(pid: u16) -> String {
    if pid == PID_RIVAL3_WIRELESS {
        "SteelSeries Rival 3 Wireless".into()
    } else {
        format!("SteelSeries wireless mouse ({pid:#06x})")
    }
}

// ---------------------------------------------------------------------------
// CPI <-> sensor value (truemove-air table from the vendor descriptor)
// ---------------------------------------------------------------------------

pub const TRUEMOVE_AIR_TABLE: [(u8, u16); 180] = [
    (0x00, 100),
    (0x02, 200),
    (0x03, 300),
    (0x04, 400),
    (0x05, 500),
    (0x06, 600),
    (0x07, 700),
    (0x09, 800),
    (0x0A, 900),
    (0x0B, 1000),
    (0x0C, 1100),
    (0x0D, 1200),
    (0x0E, 1300),
    (0x10, 1400),
    (0x11, 1500),
    (0x12, 1600),
    (0x13, 1700),
    (0x14, 1800),
    (0x16, 1900),
    (0x17, 2000),
    (0x18, 2100),
    (0x19, 2200),
    (0x1A, 2300),
    (0x1B, 2400),
    (0x1D, 2500),
    (0x1E, 2600),
    (0x1F, 2700),
    (0x20, 2800),
    (0x21, 2900),
    (0x23, 3000),
    (0x25, 3100),
    (0x26, 3200),
    (0x27, 3300),
    (0x28, 3400),
    (0x29, 3500),
    (0x2A, 3600),
    (0x2C, 3700),
    (0x2D, 3800),
    (0x2E, 3900),
    (0x2F, 4000),
    (0x30, 4100),
    (0x32, 4200),
    (0x33, 4300),
    (0x34, 4400),
    (0x35, 4500),
    (0x36, 4600),
    (0x38, 4700),
    (0x39, 4800),
    (0x3A, 4900),
    (0x3B, 5000),
    (0x3C, 5100),
    (0x3E, 5200),
    (0x3F, 5300),
    (0x40, 5400),
    (0x41, 5500),
    (0x42, 5600),
    (0x44, 5700),
    (0x45, 5800),
    (0x46, 5900),
    (0x47, 6000),
    (0x48, 6100),
    (0x4A, 6200),
    (0x4B, 6300),
    (0x4C, 6400),
    (0x4D, 6500),
    (0x4E, 6600),
    (0x50, 6700),
    (0x51, 6800),
    (0x52, 6900),
    (0x53, 7000),
    (0x54, 7100),
    (0x56, 7200),
    (0x57, 7300),
    (0x58, 7400),
    (0x59, 7500),
    (0x5A, 7600),
    (0x5C, 7700),
    (0x5D, 7800),
    (0x5E, 7900),
    (0x5F, 8000),
    (0x60, 8100),
    (0x62, 8200),
    (0x63, 8300),
    (0x64, 8400),
    (0x65, 8500),
    (0x66, 8600),
    (0x68, 8700),
    (0x69, 8800),
    (0x6A, 8900),
    (0x6B, 9000),
    (0x6C, 9100),
    (0x6E, 9200),
    (0x6F, 9300),
    (0x70, 9400),
    (0x71, 9500),
    (0x72, 9600),
    (0x74, 9700),
    (0x75, 9800),
    (0x76, 9900),
    (0x77, 10000),
    (0x78, 10100),
    (0x7A, 10200),
    (0x7B, 10300),
    (0x7C, 10400),
    (0x7D, 10500),
    (0x7E, 10600),
    (0x80, 10700),
    (0x81, 10800),
    (0x82, 10900),
    (0x83, 11000),
    (0x84, 11100),
    (0x86, 11200),
    (0x87, 11300),
    (0x88, 11400),
    (0x89, 11500),
    (0x8A, 11600),
    (0x8C, 11700),
    (0x8D, 11800),
    (0x8E, 11900),
    (0x8F, 12000),
    (0x90, 12100),
    (0x92, 12200),
    (0x93, 12300),
    (0x94, 12400),
    (0x95, 12500),
    (0x96, 12600),
    (0x98, 12700),
    (0x99, 12800),
    (0x9A, 12900),
    (0x9B, 13000),
    (0x9C, 13100),
    (0x9E, 13200),
    (0x9F, 13300),
    (0xA0, 13400),
    (0xA1, 13500),
    (0xA2, 13600),
    (0xA4, 13700),
    (0xA5, 13800),
    (0xA6, 13900),
    (0xA7, 14000),
    (0xA8, 14100),
    (0xAA, 14200),
    (0xAB, 14300),
    (0xAC, 14400),
    (0xAD, 14500),
    (0xAE, 14600),
    (0xB0, 14700),
    (0xB1, 14800),
    (0xB2, 14900),
    (0xB3, 15000),
    (0xB4, 15100),
    (0xB5, 15200),
    (0xB6, 15300),
    (0xB7, 15400),
    (0xB8, 15500),
    (0xB9, 15600),
    (0xBA, 15700),
    (0xBB, 15800),
    (0xBC, 15900),
    (0xBD, 16000),
    (0xBF, 16100),
    (0xC0, 16200),
    (0xC2, 16300),
    (0xC3, 16400),
    (0xC4, 16500),
    (0xC5, 16600),
    (0xC6, 16700),
    (0xC7, 16800),
    (0xC9, 16900),
    (0xCA, 17000),
    (0xCB, 17100),
    (0xCC, 17200),
    (0xCD, 17300),
    (0xCF, 17400),
    (0xD0, 17500),
    (0xD1, 17600),
    (0xD2, 17700),
    (0xD3, 17800),
    (0xD5, 17900),
    (0xD6, 18000),
];

pub const MAX_SENSOR_VALUE: u16 = 0xD6;

/// CPI for a sensor value; values missing from the table use the vendor's
/// approximation `100 * floor(value * 0.8)`.
pub fn cpi_from_sensor(value: u16) -> Option<u16> {
    if value > MAX_SENSOR_VALUE {
        return None;
    }
    TRUEMOVE_AIR_TABLE
        .iter()
        .find(|(sensor, _)| u16::from(*sensor) == value)
        .map(|(_, cpi)| *cpi)
        .or(Some(100 * ((value as f32 * 0.8) as u16)))
}

/// The sensor value whose CPI is nearest to `cpi` (100..18000 in 100 steps).
pub fn sensor_from_cpi(cpi: u16) -> u8 {
    TRUEMOVE_AIR_TABLE
        .iter()
        .min_by_key(|(sensor, table_cpi)| ((*table_cpi as i32 - cpi as i32).abs(), *sensor))
        .map(|(sensor, _)| *sensor)
        .unwrap_or(0)
}

pub fn snap_cpi(cpi: u16) -> u16 {
    cpi_from_sensor(sensor_from_cpi(cpi).into()).unwrap_or(cpi)
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DpiPreset {
    pub level: u8,
    pub sensor: u16,
    pub cpi: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DpiInfo {
    pub level_max: u8,
    pub active_level: u8,
    pub presets: Vec<DpiPreset>,
}

impl DpiInfo {
    pub fn current_cpi(&self) -> Option<u16> {
        self.presets
            .iter()
            .find(|p| p.level == self.active_level)
            .and_then(|p| p.cpi)
    }
    pub fn cpis(&self) -> Vec<u16> {
        self.presets.iter().map(|p| p.cpi.unwrap_or(0)).collect()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PowerMode {
    /// High-efficiency mode (forces 125 Hz).
    pub power_saving: u8,
    /// "Sensor rest"; GG writes 100 for on, 0 for off.
    pub movement_saving: u8,
    pub idle_lighting: u16,
    /// Sleep timer in seconds.
    pub idle_sleep: u16,
}

pub const POLLING_RATES: [(u8, u16); 4] = [(0, 1000), (1, 500), (2, 250), (3, 125)];

pub fn polling_hz(level: u8) -> Option<u16> {
    POLLING_RATES
        .iter()
        .find(|(l, _)| *l == level)
        .map(|(_, hz)| *hz)
}

pub fn polling_level(hz: u16) -> Option<u8> {
    POLLING_RATES
        .iter()
        .find(|(_, h)| *h == hz)
        .map(|(l, _)| *l)
}

/// Sleep-timer choices offered by SteelSeries GG (seconds, 1..20 minutes).
pub fn sleep_timer_choices() -> impl Iterator<Item = u16> {
    (1..=20).map(|minutes| minutes * 60)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError(pub String);

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ProtocolError {}

fn invalid(message: impl Into<String>) -> ProtocolError {
    ProtocolError(message.into())
}

// ---------------------------------------------------------------------------
// Read requests and parsers
// ---------------------------------------------------------------------------

pub const CMD_BATTERY: &[u8] = &[0xAA, 0x01];
pub const CMD_DPI: &[u8] = &[0xA0];
pub const CMD_DPI_LEVEL: &[u8] = &[0x96];
pub const CMD_POLLING: &[u8] = &[0x97];
pub const CMD_FW_RECEIVER: &[u8] = &[0xD0];
pub const CMD_FW_MOUSE: &[u8] = &[0x90];
pub const CMD_RF_STATUS: &[u8] = &[0xE8];
pub const CMD_RF_STATUS_FALLBACK: &[u8] = &[0xA8];
pub const CMD_POWER: &[u8] = &[0xAB];
pub const CMD_DEBOUNCE: &[u8] = &[0x9E];

/// Responses carry no opcode, so a late or duplicated report could be taken
/// for the answer to another command (seen on hardware while the link
/// flaps).  Every parser therefore accepts only its exact shape: the struct's
/// own bytes, all later bytes zero.
fn shape(data: &[u8], len: usize) -> Option<&[u8]> {
    (data.len() >= len && data[len..].iter().all(|b| *b == 0)).then(|| &data[..len])
}

pub fn parse_battery(data: &[u8]) -> Result<u8, ProtocolError> {
    let fields = shape(data, 3).ok_or_else(|| invalid("unexpected battery response"))?;
    let level = u16::from_le_bytes([fields[0], fields[1]]);
    if level > 100 || fields[2] != 0 {
        return Err(invalid(format!("battery level out of range: {level}")));
    }
    Ok(level as u8)
}

pub fn parse_battery_opt(data: &[u8]) -> Option<u8> {
    parse_battery(data).ok()
}

pub fn parse_dpi(data: &[u8]) -> Option<DpiInfo> {
    let data = shape(data, 12)?;
    let (level_max, active_level) = (data[0], data[1]);
    if !(1..=5).contains(&level_max) || !(1..=level_max).contains(&active_level) {
        return None;
    }
    let presets = (0..level_max as usize)
        .map(|i| {
            let sensor = u16::from_le_bytes([data[2 + 2 * i], data[3 + 2 * i]]);
            DpiPreset {
                level: i as u8 + 1,
                sensor,
                cpi: cpi_from_sensor(sensor),
            }
        })
        .collect::<Vec<DpiPreset>>();
    if presets.iter().any(|p| p.cpi.is_none())
        || data[2 + 2 * level_max as usize..].iter().any(|b| *b != 0)
    {
        return None;
    }
    Some(DpiInfo {
        level_max,
        active_level,
        presets,
    })
}

pub fn parse_dpi_level(data: &[u8]) -> Option<u8> {
    shape(data, 1)
        .map(|d| d[0])
        .filter(|level| (1..=5).contains(level))
}

pub fn parse_polling(data: &[u8]) -> Option<u16> {
    shape(data, 1).and_then(|d| polling_hz(d[0]))
}

pub fn parse_firmware(data: &[u8]) -> Option<String> {
    match shape(data, 2)? {
        [major @ 1..=9, minor] => Some(format!("{major}.{minor:02}")),
        _ => None,
    }
}

/// `Some(true)` when the receiver reports a linked mouse (status 4).
pub fn parse_rf_status(data: &[u8]) -> Option<bool> {
    match shape(data, 1)?[0] {
        4 => Some(true),
        0..=3 | 5 => Some(false),
        _ => None,
    }
}

pub fn parse_power(data: &[u8]) -> Option<PowerMode> {
    let d = shape(data, 6)?;
    let mode = PowerMode {
        power_saving: d[0],
        movement_saving: d[1],
        idle_lighting: u16::from_le_bytes([d[2], d[3]]),
        idle_sleep: u16::from_le_bytes([d[4], d[5]]),
    };
    // GG's ranges: on/off flag, 0..100, timers up to an hour, sleep >= 1 min.
    let plausible = mode.power_saving <= 1
        && mode.movement_saving <= 100
        && mode.idle_lighting <= 3600
        && (60..=3600).contains(&mode.idle_sleep);
    plausible.then_some(mode)
}

pub fn parse_debounce(data: &[u8]) -> Option<(u8, u8)> {
    match shape(data, 2)? {
        [make @ 1..=100, brk @ 1..=100] => Some((*make, *brk)),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Write commands
// ---------------------------------------------------------------------------

pub fn set_polling(hz: u16) -> Result<Vec<u8>, ProtocolError> {
    let level = polling_level(hz)
        .ok_or_else(|| invalid("polling rate must be 125, 250, 500 or 1000 Hz"))?;
    Ok(vec![0x17, level])
}

/// Batch DPI write: 1..5 presets (sensor values) and the active one (1-based).
pub fn set_dpi(sensors: &[u8], active: u8) -> Result<Vec<u8>, ProtocolError> {
    if sensors.is_empty() || sensors.len() > MAX_DPI_LEVELS {
        return Err(invalid("between 1 and 5 DPI presets are required"));
    }
    if active == 0 || active as usize > sensors.len() {
        return Err(invalid(format!(
            "active preset must be 1..{}",
            sensors.len()
        )));
    }
    if sensors
        .iter()
        .any(|value| u16::from(*value) > MAX_SENSOR_VALUE)
    {
        return Err(invalid("DPI out of range"));
    }
    let mut out = vec![0x20, sensors.len() as u8, active];
    for index in 0..MAX_DPI_LEVELS {
        out.extend_from_slice(&u16::from(sensors.get(index).copied().unwrap_or(0)).to_le_bytes());
    }
    Ok(out)
}

pub fn set_power(mode: PowerMode) -> Vec<u8> {
    let mut out = vec![0x2B, mode.power_saving, mode.movement_saving];
    out.extend_from_slice(&mode.idle_lighting.to_le_bytes());
    out.extend_from_slice(&mode.idle_sleep.to_le_bytes());
    out
}

pub fn set_debounce(make_ms: u8, break_ms: u8) -> Vec<u8> {
    vec![0x1E, make_ms, break_ms]
}

/// Show a colour immediately (not stored; restart-lighting undoes it).
pub fn live_color(rgb: [u8; 3]) -> Vec<u8> {
    vec![0x13, 0x01, rgb[0], rgb[1], rgb[2]]
}

pub const RESTART_LIGHTING: &[u8] = &[0x18];
pub const SAVE_TO_FLASH: &[u8] = &[0x09];
pub const FACTORY_RESET: &[u8] = &[0x1B];
pub const PAIR: &[u8] = &[0x71, 0x00];
/// Restart the receiver (GG sends this after firmware updates); the USB
/// device re-enumerates.  Recovers a receiver that lost the mouse.
pub const RESET_RECEIVER: &[u8] = &[0x41];
pub const RESET_WIRELESS: &[u8] = &[0x73];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpi_table() {
        assert_eq!(cpi_from_sensor(0x09), Some(800));
        assert_eq!(cpi_from_sensor(0xD6), Some(18000));
        assert_eq!(cpi_from_sensor(0xD7), None);
        assert_eq!(sensor_from_cpi(800), 0x09);
        assert_eq!(sensor_from_cpi(830), 0x09);
        assert_eq!(sensor_from_cpi(50), 0x00);
        assert_eq!(snap_cpi(1250), 1200);
    }

    #[test]
    fn parses_captured_responses() {
        assert_eq!(parse_battery(&[0x47, 0, 0]), Ok(71));
        assert!(parse_battery(&[0xFF, 0, 0]).is_err());
        let dpi = parse_dpi(&[5, 2, 4, 0, 9, 0, 0x0D, 0, 0x12, 0, 0x17, 0]).unwrap();
        assert_eq!(dpi.cpis(), vec![400, 800, 1200, 1600, 2000]);
        assert_eq!(dpi.current_cpi(), Some(800));
        assert_eq!(parse_dpi(&[0; 12]), None);
        assert_eq!(parse_polling(&[0]), Some(1000));
        assert_eq!(parse_polling(&[1]), Some(500));
        assert_eq!(parse_firmware(&[1, 4]), Some("1.04".into()));
        assert_eq!(parse_rf_status(&[4]), Some(true));
        assert_eq!(parse_rf_status(&[1]), Some(false));
        assert_eq!(
            parse_power(&[0, 0x32, 0xB0, 0x04, 0x2C, 0x01]),
            Some(PowerMode {
                power_saving: 0,
                movement_saving: 50,
                idle_lighting: 1200,
                idle_sleep: 300
            })
        );
        assert_eq!(parse_debounce(&[6, 20]), Some((6, 20)));
    }

    #[test]
    fn rejects_replies_meant_for_other_commands() {
        // Captured while the link flapped: link status "01" read as power mode
        // gave "high efficiency on, sleep 0 min".
        assert_eq!(parse_power(&[1, 0, 0, 0, 0, 0, 0, 0]), None);
        assert_eq!(parse_power(&[0x48, 0, 0, 0, 0, 0]), None);
        // A late battery reply ("48 00 00") is not a link status, polling rate or DPI.
        assert_eq!(parse_rf_status(&[0x48, 0, 0]), None);
        assert_eq!(parse_polling(&[0x48]), None);
        assert_eq!(parse_dpi(&[0x48, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]), None);
        // Firmware "01 04" is not a power mode or DPI; DPI is not a battery level.
        assert_eq!(parse_power(&[1, 4, 0, 0, 0, 0]), None);
        assert!(parse_battery(&[5, 2, 4, 0, 9, 0, 0x0D, 0, 0x12, 0, 0x17, 0]).is_err());
        assert_eq!(parse_polling(&[0, 0x32, 0xB0]), None);
        assert_eq!(parse_debounce(&[4, 0]), None);
    }

    #[test]
    fn writes_match_hardware_captures() {
        assert_eq!(
            set_dpi(&[4, 9, 0x0D, 0x12, 0x17], 3).unwrap(),
            vec![0x20, 5, 3, 4, 0, 9, 0, 0x0D, 0, 0x12, 0, 0x17, 0]
        );
        assert_eq!(
            set_dpi(&[9, 0x17], 1).unwrap()[..13],
            [0x20, 2, 1, 9, 0, 0x17, 0, 0, 0, 0, 0, 0, 0]
        );
        assert!(set_dpi(&[], 1).is_err());
        assert!(set_dpi(&[4; 6], 1).is_err());
        assert!(set_dpi(&[4, 9], 3).is_err());
        assert_eq!(set_polling(500).unwrap(), vec![0x17, 1]);
        assert!(set_polling(333).is_err());
        let mode = PowerMode {
            power_saving: 0,
            movement_saving: 50,
            idle_lighting: 1200,
            idle_sleep: 300,
        };
        assert_eq!(set_power(mode), vec![0x2B, 0, 0x32, 0xB0, 0x04, 0x2C, 0x01]);
        assert_eq!(set_debounce(6, 20), vec![0x1E, 6, 20]);
        assert_eq!(live_color([255, 0, 0]), vec![0x13, 1, 255, 0, 0]);
    }
}
