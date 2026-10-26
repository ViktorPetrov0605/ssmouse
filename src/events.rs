//! Events the receiver pushes on its "sync" interface (usage page 0xFF00).
//!
//! Verified on hardware and matching GG's `translate_sync_data`:
//!
//! | Report  | Meaning                                    |
//! |---------|--------------------------------------------|
//! | `28 NN` | link changed: 01 = connected, 00 = lost    |
//! | `16 NN` | active DPI preset changed to NN (1..5)     |
//! | `2A NN` | battery level NN %                         |
//!
//! Listening costs nothing on the radio, so the tray reacts instantly and
//! only polls as a fallback.

use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::sleep;
use std::time::Duration;

use crate::device::{self, HidTransport, Transport};
use crate::protocol::MAX_DPI_LEVELS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    Link(bool),
    DpiLevel(u8),
    Battery(u8),
}

pub fn parse(data: &[u8]) -> Option<Event> {
    match data {
        [0x28, value, ..] => Some(Event::Link(*value == 1)),
        [0x16, level, ..] if (1..=MAX_DPI_LEVELS).contains(&usize::from(*level)) => {
            Some(Event::DpiLevel(*level))
        }
        [0x2A, level, ..] if *level <= 100 => Some(Event::Battery(*level)),
        _ => None,
    }
}

/// Deliver events until `stop` is set; survives receiver unplug/replug.
pub fn listen(mut on_event: impl FnMut(Event), stop: &AtomicBool) {
    while !stop.load(Ordering::Relaxed) {
        let transport = device::enumerate()
            .ok()
            .and_then(|all| device::event_interface(&all).cloned())
            .and_then(|interface| HidTransport::open(&interface).ok());
        let Some(mut transport) = transport else {
            sleep(Duration::from_secs(3));
            continue;
        };
        while !stop.load(Ordering::Relaxed) {
            match transport.read(500) {
                Ok(Some(report)) => {
                    if let Some(event) = parse(&report) {
                        on_event(event);
                    }
                }
                Ok(None) => {}
                Err(_) => break, // unplugged: rediscover
            }
        }
        sleep(Duration::from_secs(1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_captured_events() {
        assert_eq!(parse(&[0x28, 1, 0, 0]), Some(Event::Link(true)));
        assert_eq!(parse(&[0x28, 0, 1, 0]), Some(Event::Link(false)));
        assert_eq!(parse(&[0x16, 3]), Some(Event::DpiLevel(3)));
        assert_eq!(parse(&[0x2A, 0x46]), Some(Event::Battery(70)));
        for bad in [
            &[][..],
            &[0x28],
            &[0x16, 9],
            &[0x16, 0],
            &[0x2A, 0x90],
            &[0x99, 0],
        ] {
            assert_eq!(parse(bad), None);
        }
    }
}
