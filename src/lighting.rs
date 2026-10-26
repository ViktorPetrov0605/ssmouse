//! Wheel LED effects in SteelSeries' "libsteel" format.
//!
//! Port of `get-libsteel-effect-bytes` from GG's `libsteel_lighting_engine.lisp`.
//! An effect is uploaded in three steps:
//!
//! 1. `03 00 <offset u16> <len u16> <data>`: flash-buffer writes, 48-byte chunks
//! 2. `05 00 10 FF <offset u32> <len u16>`: push the buffer to effect 0xFF10
//! 3. `1C 00 <x i16> <y i16> 00`: bind the LED to that effect
//!
//! Effect layout (92 bytes): `u32 duration_ms, i32 wave, i16 x, i16 y,
//! 4 x u8 scale, u8 overflow + 3 pad, u32 trigger mask, u8 sections,
//! rgb initial colour, 16 x (rgb, u8 share of the cycle in 1/255)`.

use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::error::{Error, Result};

pub type Rgb = [u8; 3];

const EFFECT_ID: u16 = 0xFF10;
const CHUNK: usize = 0x30;
const SECTIONS: usize = 16;
const WRAP_AROUND: u8 = 0;
const HOLD: u8 = 1;
const ALL_BUTTONS: u32 = 0xFFFF_FFFF;

pub const DEFAULT_COLOR: Rgb = [255, 85, 0];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    #[default]
    Steady,
    Colorshift,
    Breathe,
    Reactive,
    Off,
}

impl Mode {
    pub const ALL: [Mode; 5] = [
        Mode::Steady,
        Mode::Colorshift,
        Mode::Breathe,
        Mode::Reactive,
        Mode::Off,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Mode::Steady => "Steady colour",
            Mode::Colorshift => "Colour shift",
            Mode::Breathe => "Breathe",
            Mode::Reactive => "Reactive (flash on click)",
            Mode::Off => "Off",
        }
    }

    /// Colour stops the section table can hold.
    pub fn max_colors(self) -> usize {
        match self {
            Mode::Colorshift => 14,
            Mode::Breathe => 7,
            Mode::Off => 0,
            _ => 1,
        }
    }

    pub fn parse(text: &str) -> Result<Self> {
        serde_json::from_value(serde_json::Value::String(text.to_lowercase())).map_err(|_| {
            Error::Invalid(format!(
                "unknown effect {text:?} (steady, colorshift, breathe, reactive, off)"
            ))
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Effect {
    pub mode: Mode,
    #[serde(with = "hex_list", default = "default_colors")]
    pub colors: Vec<Rgb>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub positions: Option<Vec<u8>>,
    #[serde(default = "default_duration")]
    pub duration_ms: u32,
    #[serde(with = "hex", default = "white")]
    pub react_color: Rgb,
    #[serde(default = "default_react")]
    pub react_ms: u32,
}

fn default_colors() -> Vec<Rgb> {
    vec![DEFAULT_COLOR]
}
fn default_duration() -> u32 {
    5000
}
fn white() -> Rgb {
    [255, 255, 255]
}
fn default_react() -> u32 {
    500
}

impl Default for Effect {
    fn default() -> Self {
        Self::new(Mode::Steady, vec![DEFAULT_COLOR])
    }
}

impl Effect {
    pub fn new(mode: Mode, colors: Vec<Rgb>) -> Self {
        Self {
            mode,
            colors,
            positions: None,
            duration_ms: 5000,
            react_color: white(),
            react_ms: 500,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.colors.is_empty() && self.mode != Mode::Off {
            return Err(Error::Invalid("at least one colour is required".into()));
        }
        if matches!(self.mode, Mode::Colorshift | Mode::Breathe)
            && self.colors.len() > self.mode.max_colors()
        {
            return Err(Error::Invalid(format!(
                "{:?} supports at most {} colours",
                self.mode,
                self.mode.max_colors()
            )));
        }
        if let Some(pos) = &self.positions
            && (pos.len() != self.colors.len() || pos.iter().any(|p| *p > 100) || !pos.is_sorted())
        {
            return Err(Error::Invalid(
                "positions must be ascending 0..100, one per colour".into(),
            ));
        }
        if self.duration_ms == 0 || self.duration_ms > 600_000 {
            return Err(Error::Invalid("duration must be 1 ms .. 10 min".into()));
        }
        if self.react_ms > 20_000 {
            return Err(Error::Invalid("reactive fade must be 0..20000 ms".into()));
        }
        Ok(())
    }

    fn stops(&self) -> Vec<i32> {
        match &self.positions {
            Some(p) => p.iter().map(|v| i32::from(*v)).collect(),
            None => (0..self.colors.len())
                .map(|i| (i * 100 / self.colors.len()) as i32)
                .collect(),
        }
    }

    /// The 92-byte effect written to flash buffer 0xFF10.
    pub fn encode(&self) -> Vec<u8> {
        let share = |delta: i32| (delta as f64 * 2.55).clamp(0.0, 255.0) as u8;
        let mut overflow = WRAP_AROUND;
        let mut trigger_mask = 0u32;
        let mut duration = self.duration_ms;
        let (initial, sections): (Rgb, Vec<(Rgb, u8)>) = match self.mode {
            Mode::Steady | Mode::Off => {
                overflow = HOLD;
                let color = if self.mode == Mode::Off {
                    [0, 0, 0]
                } else {
                    self.colors[0]
                };
                (color, vec![(color, 0)])
            }
            Mode::Reactive => {
                // Flash react_color, fade back to the base colour over react_ms.
                overflow = HOLD;
                trigger_mask = ALL_BUTTONS;
                duration = self.react_ms.max(10);
                (
                    self.react_color,
                    vec![(self.react_color, 0), (self.colors[0], 255)],
                )
            }
            Mode::Colorshift => {
                let pos = self.stops();
                let mut sections: Vec<(Rgb, u8)> = self
                    .colors
                    .iter()
                    .enumerate()
                    .map(|(i, c)| (*c, share(pos[i] - if i > 0 { pos[i - 1] } else { 0 })))
                    .collect();
                sections.push((self.colors[0], share(100 - pos[pos.len() - 1])));
                duration = duration.max(sections.len() as u32 * 330);
                (self.colors[0], sections)
            }
            Mode::Breathe => {
                // Every colour fades in and out through black.
                let pos = self.stops();
                let n = self.colors.len();
                let mut sections = Vec::new();
                for (i, color) in self.colors.iter().enumerate() {
                    let prev = if i > 0 { pos[i - 1] } else { 0 };
                    let next = if i + 1 < n { pos[i + 1] } else { 100 };
                    sections.push((*color, share(pos[i] - (prev + (pos[i] - prev) / 2))));
                    sections.push(([0, 0, 0], share((pos[i] + (next - pos[i]) / 2) - pos[i])));
                }
                let last = pos[n - 1];
                sections.push((self.colors[0], share(100 - (last + (100 - last) / 2))));
                duration = duration.max(2 * n as u32 * 330);
                (self.colors[0], sections)
            }
        };
        duration -= duration % 10;
        let mut out = Vec::with_capacity(92);
        out.extend_from_slice(&duration.to_le_bytes());
        out.extend_from_slice(&[0; 12]); // no wave, focal point 0/0, no scaling
        out.extend_from_slice(&[overflow, 0, 0, 0]);
        out.extend_from_slice(&trigger_mask.to_le_bytes());
        out.push(sections.len() as u8);
        out.extend_from_slice(&initial);
        for index in 0..SECTIONS {
            match sections.get(index) {
                Some((color, share)) => {
                    out.extend_from_slice(color);
                    out.push(*share);
                }
                None => out.extend_from_slice(&[0; 4]),
            }
        }
        out
    }
}

/// Command payloads that upload `effect` to the wheel LED.
pub fn payloads(effect: &Effect) -> Vec<Vec<u8>> {
    let blob = effect.encode();
    let mut out = Vec::new();
    for (index, chunk) in blob.chunks(CHUNK).enumerate() {
        let mut p = vec![0x03, 0x00];
        p.extend_from_slice(&((index * CHUNK) as u16).to_le_bytes());
        p.extend_from_slice(&(chunk.len() as u16).to_le_bytes());
        p.extend_from_slice(chunk);
        out.push(p);
    }
    let mut push = vec![0x05, 0x00];
    push.extend_from_slice(&EFFECT_ID.to_le_bytes());
    push.extend_from_slice(&0u32.to_le_bytes());
    push.extend_from_slice(&(blob.len() as u16).to_le_bytes());
    out.push(push);
    out.push(vec![0x1C, 0x00, 0, 0, 0, 0, 0]);
    out
}

// ---------------------------------------------------------------------------
// Colours
// ---------------------------------------------------------------------------

const NAMED: [(&str, Rgb); 11] = [
    ("black", [0, 0, 0]),
    ("white", [255, 255, 255]),
    ("red", [255, 0, 0]),
    ("green", [0, 255, 0]),
    ("blue", [0, 0, 255]),
    ("yellow", [255, 255, 0]),
    ("cyan", [0, 255, 255]),
    ("magenta", [255, 0, 255]),
    ("orange", [255, 85, 0]),
    ("purple", [128, 0, 255]),
    ("pink", [255, 64, 160]),
];

/// `ff5500`, `#ff5500`, `255,85,0` or a colour name.
pub fn parse_color(text: &str) -> Result<Rgb> {
    let value = text.trim().to_lowercase();
    if let Some((_, rgb)) = NAMED.iter().find(|(name, _)| *name == value) {
        return Ok(*rgb);
    }
    let hex = value.trim_start_matches('#');
    if hex.len() == 6 && hex.chars().all(|c| c.is_ascii_hexdigit()) {
        let raw = u32::from_str_radix(hex, 16).unwrap();
        return Ok([(raw >> 16) as u8, (raw >> 8) as u8, raw as u8]);
    }
    let parts: Vec<&str> = value.split(',').map(str::trim).collect();
    if parts.len() == 3
        && let (Ok(r), Ok(g), Ok(b)) = (parts[0].parse(), parts[1].parse(), parts[2].parse())
    {
        return Ok([r, g, b]);
    }
    Err(Error::Invalid(format!(
        "invalid colour {text:?} (use rrggbb, r,g,b or a colour name)"
    )))
}

/// Colour list: `red,blue`, `ff0000 0000ff`, or a single `255,0,0` triple.
pub fn parse_colors(text: &str) -> Result<Vec<Rgb>> {
    let parts: Vec<&str> = text
        .split([',', ' ', ';', '/'])
        .filter(|p| !p.is_empty())
        .collect();
    if parts.len() == 3
        && parts
            .iter()
            .all(|p| p.len() <= 3 && p.chars().all(|c| c.is_ascii_digit()))
    {
        return Ok(vec![parse_color(&parts.join(","))?]);
    }
    parts.iter().map(|p| parse_color(p)).collect()
}

pub fn format_color(rgb: Rgb) -> String {
    format!("{:02x}{:02x}{:02x}", rgb[0], rgb[1], rgb[2])
}

mod hex {
    use super::*;
    pub fn serialize<S: Serializer>(rgb: &Rgb, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&format_color(*rgb))
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Rgb, D::Error> {
        let text = String::deserialize(d)?;
        parse_color(&text).map_err(serde::de::Error::custom)
    }
}

mod hex_list {
    use super::*;
    pub fn serialize<S: Serializer>(list: &[Rgb], s: S) -> std::result::Result<S::Ok, S::Error> {
        s.collect_seq(list.iter().map(|c| format_color(*c)))
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Vec<Rgb>, D::Error> {
        let list = Vec::<String>::deserialize(d)?;
        list.iter()
            .map(|t| parse_color(t).map_err(serde::de::Error::custom))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steady_matches_hardware_capture() {
        let p = payloads(&Effect::new(Mode::Steady, vec![[255, 0, 0]]));
        assert_eq!(p.len(), 4);
        assert_eq!(&p[0][..6], &[0x03, 0, 0, 0, 0x30, 0]);
        assert_eq!(
            &p[0][6..35],
            &[
                0x88, 0x13, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1,
                0xFF, 0, 0, 0xFF
            ]
        );
        assert_eq!(&p[1][..6], &[0x03, 0, 0x30, 0, 0x2C, 0]);
        assert_eq!(p[2], vec![0x05, 0, 0x10, 0xFF, 0, 0, 0, 0, 0x5C, 0]);
        assert_eq!(p[3], vec![0x1C, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn colorshift_breathe_reactive_sections() {
        let mut e = Effect::new(
            Mode::Colorshift,
            vec![[255, 0, 0], [0, 255, 0], [0, 0, 255]],
        );
        e.duration_ms = 3000;
        let b = e.encode();
        assert_eq!(b.len(), 92);
        assert_eq!(u32::from_le_bytes(b[0..4].try_into().unwrap()), 3000);
        assert_eq!(b[16], 0);
        assert_eq!(b[24], 4);
        assert_eq!(
            &b[28..44],
            &[255, 0, 0, 0, 0, 255, 0, 84, 0, 0, 255, 84, 255, 0, 0, 86]
        );

        let mut e = Effect::new(Mode::Breathe, vec![[0, 255, 255]]);
        e.duration_ms = 4000;
        let b = e.encode();
        assert_eq!(b[24], 3);
        assert_eq!(
            &b[28..40],
            &[0, 255, 255, 0, 0, 0, 0, 127, 0, 255, 255, 127]
        );

        let mut e = Effect::new(Mode::Reactive, vec![[0, 0, 255]]);
        e.react_ms = 800;
        let b = e.encode();
        assert_eq!(u32::from_le_bytes(b[0..4].try_into().unwrap()), 800);
        assert_eq!(b[16], 1);
        assert_eq!(&b[20..28], &[0xFF, 0xFF, 0xFF, 0xFF, 2, 255, 255, 255]);
        assert_eq!(&b[28..36], &[255, 255, 255, 0, 0, 0, 255, 255]);
    }

    #[test]
    fn colors_and_json() {
        assert_eq!(parse_color("#FF5500").unwrap(), [255, 85, 0]);
        assert_eq!(parse_color("10,20,30").unwrap(), [10, 20, 30]);
        assert!(parse_color("nope").is_err());
        assert_eq!(
            parse_colors("ff0000,0000ff").unwrap(),
            vec![[255, 0, 0], [0, 0, 255]]
        );
        assert_eq!(parse_colors("255,0,0").unwrap(), vec![[255, 0, 0]]);
        // Profiles written by the Python version load unchanged.
        let e: Effect = serde_json::from_str(r#"{"mode":"reactive","colors":["0000ff"],"duration_ms":5000,"react_color":"ffffff","react_ms":800}"#).unwrap();
        assert_eq!(e.react_ms, 800);
        let back: Effect = serde_json::from_str(&serde_json::to_string(&e).unwrap()).unwrap();
        assert_eq!(back, e);
        assert!(
            Effect::new(Mode::Breathe, vec![[1, 2, 3]; 8])
                .validate()
                .is_err()
        );
    }
}
