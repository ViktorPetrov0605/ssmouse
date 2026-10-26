//! Button and scroll-wheel remapping (`19` buttons, `1A` wheel).
//!
//! Each binding is five bytes, GG's "newerFourByteSelector": a function code
//! and four key bytes.
//!
//! * `00` disabled, `01`-`06` mouse buttons, `30` DPI cycle, `31`/`32` scroll
//! * `51` keyboard: up to three modifier usages, then the key usage (HID
//!   keyboard page), e.g. Ctrl+C = `51 E0 06 00 00`
//! * `61` media: one consumer usage, e.g. volume up = `61 E9 00 00 00`
//!
//! Specs are short strings: `left`, `dpi`, `scroll-up`, `disabled`,
//! `key:ctrl+shift+t`, `media:play-pause`.

use std::collections::BTreeMap;

use crate::error::{Error, Result};

/// Control name -> binding spec; controls not listed use their default.
pub type Bindings = BTreeMap<String, String>;

pub const SIMPLE: [(&str, u8, &str); 10] = [
    ("disabled", 0x00, "Disabled"),
    ("left", 0x01, "Left click"),
    ("right", 0x02, "Right click"),
    ("middle", 0x03, "Middle click"),
    ("back", 0x04, "Back (button 4)"),
    ("forward", 0x05, "Forward (button 5)"),
    ("button6", 0x06, "Button 6"),
    ("dpi", 0x30, "Cycle DPI"),
    ("scroll-up", 0x31, "Scroll up"),
    ("scroll-down", 0x32, "Scroll down"),
];

pub const MEDIA: [(&str, u8, &str); 6] = [
    ("mute", 0xE2, "Mute"),
    ("next", 0xB5, "Next track"),
    ("play-pause", 0xCD, "Play / pause"),
    ("previous", 0xB6, "Previous track"),
    ("volume-up", 0xE9, "Volume up"),
    ("volume-down", 0xEA, "Volume down"),
];

pub const MODIFIERS: [(&str, u8); 8] = [
    ("ctrl", 0xE0),
    ("shift", 0xE1),
    ("alt", 0xE2),
    ("super", 0xE3),
    ("rctrl", 0xE4),
    ("rshift", 0xE5),
    ("ralt", 0xE6),
    ("rsuper", 0xE7),
];

const FN_KEY: u8 = 0x51;
const FN_MEDIA: u8 = 0x61;

/// Physical controls in mapping-table order, with GG's defaults.
pub const BUTTONS: [(&str, &str, &str); 6] = [
    ("left", "Left button", "left"),
    ("right", "Right button", "right"),
    ("middle", "Wheel click", "middle"),
    ("back", "Side button (back)", "back"),
    ("forward", "Side button (forward)", "forward"),
    ("dpi", "DPI button", "dpi"),
];
pub const WHEEL: [(&str, &str, &str); 2] = [
    ("wheel-up", "Wheel up", "scroll-up"),
    ("wheel-down", "Wheel down", "scroll-down"),
];
/// The button table has room for seven entries; the mouse has six buttons.
const BUTTON_SLOTS: usize = 7;

pub fn controls() -> impl Iterator<Item = (&'static str, &'static str, &'static str)> {
    BUTTONS.into_iter().chain(WHEEL)
}

pub fn default_for(control: &str) -> Option<&'static str> {
    controls()
        .find(|(name, ..)| *name == control)
        .map(|(.., default)| default)
}

/// HID keyboard usage for a key name.
pub fn key_usage(name: &str) -> Option<u8> {
    let name = match name {
        "return" => "enter",
        "escape" => "esc",
        "del" => "delete",
        "ins" => "insert",
        "pgup" => "pageup",
        "pgdn" | "pgdown" => "pagedown",
        "print" => "printscreen",
        "-" => "minus",
        "=" => "equal",
        "[" => "leftbrace",
        "]" => "rightbrace",
        "\\" => "backslash",
        ";" => "semicolon",
        "'" => "apostrophe",
        "`" => "grave",
        "," => "comma",
        "." => "dot",
        "/" => "slash",
        other => other,
    };
    let bytes = name.as_bytes();
    if bytes.len() == 1 && bytes[0].is_ascii_lowercase() {
        return Some(0x04 + bytes[0] - b'a');
    }
    if bytes.len() == 1 && bytes[0].is_ascii_digit() {
        return Some(if bytes[0] == b'0' {
            0x27
        } else {
            0x1E + bytes[0] - b'1'
        });
    }
    if let Some(n) = name.strip_prefix('f').and_then(|n| n.parse::<u8>().ok()) {
        return match n {
            1..=12 => Some(0x3A + n - 1),
            13..=24 => Some(0x68 + n - 13),
            _ => None,
        };
    }
    Some(match name {
        "enter" => 0x28,
        "esc" => 0x29,
        "backspace" => 0x2A,
        "tab" => 0x2B,
        "space" => 0x2C,
        "minus" => 0x2D,
        "equal" => 0x2E,
        "leftbrace" => 0x2F,
        "rightbrace" => 0x30,
        "backslash" => 0x31,
        "semicolon" => 0x33,
        "apostrophe" => 0x34,
        "grave" => 0x35,
        "comma" => 0x36,
        "dot" => 0x37,
        "slash" => 0x38,
        "capslock" => 0x39,
        "printscreen" => 0x46,
        "scrolllock" => 0x47,
        "pause" => 0x48,
        "insert" => 0x49,
        "home" => 0x4A,
        "pageup" => 0x4B,
        "delete" => 0x4C,
        "end" => 0x4D,
        "pagedown" => 0x4E,
        "right" => 0x4F,
        "left" => 0x50,
        "down" => 0x51,
        "up" => 0x52,
        "menu" => 0x65,
        _ => return None,
    })
}

fn modifier_usage(name: &str) -> Option<u8> {
    let name = match name {
        "control" => "ctrl",
        "meta" | "win" | "cmd" => "super",
        "option" => "alt",
        "altgr" => "ralt",
        other => other,
    };
    MODIFIERS
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, usage)| *usage)
}

/// Encode a binding spec as its five bytes.
pub fn encode(spec: &str) -> Result<[u8; 5]> {
    let text = spec.trim().to_lowercase();
    if let Some((_, code, _)) = SIMPLE.iter().find(|(name, ..)| *name == text) {
        return Ok([*code, 0, 0, 0, 0]);
    }
    if let Some(media) = text.strip_prefix("media:") {
        let (_, usage, _) = MEDIA.iter().find(|(name, ..)| *name == media).ok_or_else(|| {
            Error::Invalid(format!("unknown media key {media:?} (mute, next, previous, play-pause, volume-up, volume-down)"))
        })?;
        return Ok([FN_MEDIA, *usage, 0, 0, 0]);
    }
    if let Some(combo) = text.strip_prefix("key:") {
        let parts: Vec<&str> = combo
            .split('+')
            .map(str::trim)
            .filter(|p| !p.is_empty())
            .collect();
        let (key, mods) = parts
            .split_last()
            .ok_or_else(|| Error::Invalid("key binding needs a key, e.g. key:ctrl+c".into()))?;
        if mods.len() > 3 {
            return Err(Error::Invalid("at most 3 modifiers are supported".into()));
        }
        let mut out = [FN_KEY, 0, 0, 0, 0];
        for (i, m) in mods.iter().enumerate() {
            out[1 + i] = modifier_usage(m)
                .ok_or_else(|| Error::Invalid(format!("unknown modifier {m:?}")))?;
        }
        out[1 + mods.len()] = modifier_usage(key)
            .or_else(|| key_usage(key))
            .ok_or_else(|| Error::Invalid(format!("unknown key {key:?}")))?;
        return Ok(out);
    }
    Err(Error::Invalid(format!(
        "invalid binding {spec:?} (use left, right, middle, back, forward, dpi, scroll-up, scroll-down, disabled, key:<combo> or media:<key>)"
    )))
}

pub fn describe(spec: &str) -> String {
    let text = spec.trim().to_lowercase();
    if let Some((.., label)) = SIMPLE.iter().find(|(name, ..)| *name == text) {
        return label.to_string();
    }
    if let Some(media) = text.strip_prefix("media:")
        && let Some((.., label)) = MEDIA.iter().find(|(name, ..)| *name == media)
    {
        return format!("Media: {label}");
    }
    if let Some(combo) = text.strip_prefix("key:") {
        let pretty: Vec<String> = combo
            .split('+')
            .map(|p| {
                let mut c = p.chars();
                c.next()
                    .map(|f| f.to_uppercase().chain(c).collect())
                    .unwrap_or_default()
            })
            .collect();
        return format!("Keys: {}", pretty.join("+"));
    }
    spec.to_string()
}

/// `19` (buttons) and `1A` (wheel) payloads.
pub fn payloads(bindings: &Bindings) -> Result<Vec<Vec<u8>>> {
    if let Some(unknown) = bindings.keys().find(|k| default_for(k).is_none()) {
        return Err(Error::Invalid(format!("unknown control {unknown:?}")));
    }
    let spec = |name: &str| {
        bindings
            .get(name)
            .map(String::as_str)
            .unwrap_or_else(|| default_for(name).unwrap())
    };
    let mut buttons = vec![0x19];
    for (name, ..) in BUTTONS {
        buttons.extend_from_slice(&encode(spec(name))?);
    }
    buttons.extend(std::iter::repeat_n(0, 5 * (BUTTON_SLOTS - BUTTONS.len())));
    let mut wheel = vec![0x1A];
    for (name, ..) in WHEEL {
        wheel.extend_from_slice(&encode(spec(name))?);
    }
    Ok(vec![buttons, wheel])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encoding_matches_hardware() {
        // Verified: Shift+F13 arrives as keyboard report 01 02 00 68.
        assert_eq!(encode("key:shift+f13").unwrap(), [0x51, 0xE1, 0x68, 0, 0]);
        assert_eq!(
            encode("key:ctrl+alt+delete").unwrap(),
            [0x51, 0xE0, 0xE2, 0x4C, 0]
        );
        assert_eq!(encode("key:ctrl+0").unwrap(), [0x51, 0xE0, 0x27, 0, 0]);
        assert_eq!(encode("media:volume-up").unwrap(), [0x61, 0xE9, 0, 0, 0]);
        assert_eq!(encode("dpi").unwrap(), [0x30, 0, 0, 0, 0]);
        for bad in [
            "key:",
            "key:ctrl+alt+shift+super+x",
            "media:louder",
            "jump",
            "key:hyper+x",
        ] {
            assert!(encode(bad).is_err(), "{bad}");
        }
        assert_eq!(describe("key:ctrl+shift+t"), "Keys: Ctrl+Shift+T");
    }

    #[test]
    fn mapping_payloads() {
        let p = payloads(&Bindings::new()).unwrap();
        assert_eq!(
            p[0],
            [
                &[0x19][..],
                &[
                    1, 0, 0, 0, 0, 2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 4, 0, 0, 0, 0, 5, 0, 0, 0, 0,
                    0x30, 0, 0, 0, 0
                ],
                &[0; 5]
            ]
            .concat()
        );
        assert_eq!(p[1], vec![0x1A, 0x31, 0, 0, 0, 0, 0x32, 0, 0, 0, 0]);
        let mut custom = Bindings::new();
        custom.insert("back".into(), "key:shift+f13".into());
        custom.insert("wheel-up".into(), "media:volume-up".into());
        let p = payloads(&custom).unwrap();
        assert_eq!(&p[0][16..21], &[0x51, 0xE1, 0x68, 0, 0]);
        assert_eq!(&p[1][1..6], &[0x61, 0xE9, 0, 0, 0]);
        custom.insert("thumb".into(), "left".into());
        assert!(payloads(&custom).is_err());
    }
}
