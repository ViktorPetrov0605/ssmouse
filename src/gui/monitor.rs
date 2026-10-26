//! Toolkit-free state for the tray: polling, last-known battery, alerts.

use crate::Error;
use crate::device::Mouse;
use crate::status;

use super::settings::now;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Link {
    Connected,
    /// Receiver present, mouse asleep or off.
    Asleep,
    NoReceiver,
    NoPermission,
    Error,
}

impl Link {
    pub fn text(self) -> &'static str {
        match self {
            Link::Connected => "Connected",
            Link::Asleep => "Asleep - move the mouse to wake it",
            Link::NoReceiver => "Receiver not found",
            Link::NoPermission => "No permission - install the udev rule",
            Link::Error => "Error",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub link: Link,
    pub name: String,
    pub battery: Option<u8>,
    /// `battery` is a remembered value (mouse asleep right now).
    pub stale: bool,
    pub seen_at: Option<u64>,
    pub presets: Vec<u16>,
    pub active: Option<u8>,
    pub current_cpi: Option<u16>,
    pub polling_hz: Option<u16>,
    pub firmware: Option<String>,
    pub detail: Option<String>,
}

impl Snapshot {
    pub fn new(link: Link) -> Self {
        Self {
            link,
            name: "SteelSeries mouse".into(),
            battery: None,
            stale: false,
            seen_at: None,
            presets: vec![],
            active: None,
            current_cpi: None,
            polling_hz: None,
            firmware: None,
            detail: None,
        }
    }

    pub fn from_status(s: &status::Status) -> Self {
        let link = match (s.connected, s.battery) {
            (Some(false), _) | (None, None) => Link::Asleep,
            _ => Link::Connected,
        };
        Self {
            link,
            name: s.name.clone(),
            battery: s.battery,
            stale: false,
            seen_at: s.battery.map(|_| now()),
            presets: s.dpi_presets.clone(),
            active: s.dpi_active,
            current_cpi: s.current_cpi,
            polling_hz: s.polling_hz,
            firmware: s.firmware.clone(),
            detail: (!s.errors.is_empty()).then(|| s.errors.join("; ")),
        }
    }

    pub fn from_error(e: &Error) -> Self {
        let link = match e {
            Error::NotFound => Link::NoReceiver,
            Error::Permission(_) => Link::NoPermission,
            Error::NotLinked | Error::Timeout => Link::Asleep,
            _ => Link::Error,
        };
        Self {
            detail: Some(e.to_string()),
            ..Self::new(link)
        }
    }
}

/// Open the device, read everything, close it again (never fails).
pub fn poll_once() -> Snapshot {
    match Mouse::open() {
        Ok(mut mouse) => Snapshot::from_status(&status::read(&mut mouse)),
        // Another ssmouse process holds the device; try again next time.
        Err(Error::Busy) => Snapshot {
            detail: Some("busy".into()),
            ..Snapshot::new(Link::Connected)
        },
        Err(e) => Snapshot::from_error(&e),
    }
}

/// Percentages that trigger a notification when crossed downwards.
pub const ALERT_THRESHOLDS: [u8; 2] = [20, 10];
/// An alert re-arms once the level is this far above its threshold.
pub const REARM_MARGIN: u8 = 5;

pub struct Tracker {
    pub last_battery: Option<u8>,
    pub last_seen: Option<u64>,
    armed: [bool; 2],
}

impl Tracker {
    pub fn new(last_battery: Option<u8>, last_seen: Option<u64>) -> Self {
        let mut t = Self {
            last_battery,
            last_seen,
            armed: [true; 2],
        };
        if let Some(level) = last_battery {
            t.alerts(level); // don't re-alert for a known low level
        }
        t
    }

    /// Merge with remembered state; returns the merged snapshot and a newly
    /// crossed low-battery threshold, if any.
    pub fn update(&mut self, mut snap: Snapshot) -> (Snapshot, Option<u8>) {
        if let Some(level) = snap.battery {
            self.last_battery = Some(level);
            self.last_seen = snap.seen_at.or(Some(now()));
            let alert = self.alerts(level);
            return (snap, alert);
        }
        if self.last_battery.is_some() {
            snap.battery = self.last_battery;
            snap.stale = true;
            snap.seen_at = self.last_seen;
        }
        (snap, None)
    }

    fn alerts(&mut self, level: u8) -> Option<u8> {
        let mut fired = None;
        for (i, threshold) in ALERT_THRESHOLDS.iter().enumerate() {
            if level > threshold + REARM_MARGIN {
                self.armed[i] = true;
            } else if level <= *threshold && self.armed[i] {
                self.armed[i] = false;
                fired = Some(*threshold);
            }
        }
        fired
    }
}

/// While asleep only the receiver is asked (no radio traffic): poll faster.
pub fn next_poll_delay(link: Link, interval_s: u64) -> u64 {
    match link {
        Link::Asleep | Link::NoReceiver => interval_s.min(5),
        _ => interval_s,
    }
}

pub fn describe_age(seen_at: Option<u64>, now: u64) -> String {
    let Some(seen) = seen_at else {
        return "never".into();
    };
    let s = now.saturating_sub(seen);
    match s {
        0..60 => "just now".into(),
        60..3600 => format!("{} min ago", s / 60),
        3600..86400 => format!("{} h ago", s / 3600),
        _ => format!("{} days ago", s / 86400),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connected(level: u8) -> Snapshot {
        Snapshot {
            battery: Some(level),
            ..Snapshot::new(Link::Connected)
        }
    }

    #[test]
    fn remembers_battery_while_asleep() {
        let mut t = Tracker::new(None, None);
        t.update(Snapshot {
            seen_at: Some(100),
            ..connected(70)
        });
        let (merged, alert) = t.update(Snapshot::new(Link::Asleep));
        assert_eq!(
            (merged.battery, merged.stale, merged.seen_at, alert),
            (Some(70), true, Some(100), None)
        );
    }

    #[test]
    fn alerts_once_per_threshold_and_rearms() {
        let mut t = Tracker::new(None, None);
        let alerts: Vec<Option<u8>> = [30, 21, 20, 19, 20, 11, 10, 9, 10]
            .iter()
            .map(|l| t.update(connected(*l)).1)
            .collect();
        assert_eq!(
            alerts,
            vec![None, None, Some(20), None, None, None, Some(10), None, None]
        );
        assert_eq!(t.update(connected(100)).1, None);
        assert_eq!(t.update(connected(20)).1, Some(20));
        let mut known_low = Tracker::new(Some(15), Some(0));
        assert_eq!(known_low.update(connected(15)).1, None);
        assert_eq!(known_low.update(connected(9)).1, Some(10));
    }

    #[test]
    fn snapshots() {
        let s = Snapshot::from_status(&status::read(&mut crate::fake::mouse()));
        assert_eq!(
            (s.link, s.battery, s.current_cpi),
            (Link::Connected, Some(47), Some(600))
        );
        let s = Snapshot::from_status(&status::read(&mut crate::fake::asleep().0));
        assert_eq!(s.link, Link::Asleep);
        assert_eq!(
            Snapshot::from_error(&Error::NotFound).link,
            Link::NoReceiver
        );
        assert_eq!(
            Snapshot::from_error(&Error::Permission("x".into())).link,
            Link::NoPermission
        );
        assert_eq!(next_poll_delay(Link::Asleep, 60), 5);
        assert_eq!(next_poll_delay(Link::Connected, 60), 60);
        assert_eq!(describe_age(Some(1000), 1030), "just now");
        assert_eq!(describe_age(Some(1000), 1300), "5 min ago");
    }
}
