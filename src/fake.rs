//! A scripted receiver for tests and `--fake` demos (no hardware needed).
//!
//! The battery answer is a real capture; the rest follow the descriptor layouts.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use crate::device::{DeviceInfo, Mouse, Transport};
use crate::error::Result;
use crate::protocol as proto;

pub const ENV_FAKE: &str = "SSMOUSE_FAKE";

pub fn requested() -> bool {
    matches!(
        std::env::var(ENV_FAKE)
            .as_deref()
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Ok("1" | "true" | "yes" | "on")
    )
}

pub type Writes = Arc<Mutex<Vec<Vec<u8>>>>;

/// Request prefix -> response.
pub fn default_script() -> Vec<(Vec<u8>, Vec<u8>)> {
    vec![
        (vec![0xAA, 0x01], vec![0x2F, 0x00, 0x00]),
        // 400 / 600 / 1400 / 2800 / 4100 CPI, preset 2 active.
        (
            vec![0xA0],
            vec![5, 2, 0x04, 0, 0x06, 0, 0x10, 0, 0x20, 0, 0x30, 0],
        ),
        (vec![0x96], vec![2]),
        (vec![0x97], vec![2]),
        (vec![0xD0], vec![1, 4]),
        (vec![0x90], vec![1, 4]),
        (vec![0xE8], vec![4]),
        (vec![0xA8], vec![4]),
        (vec![0xAB], vec![0, 0, 0x58, 0x02, 0x84, 0x03]),
        (vec![0x9E], vec![6, 20]),
    ]
}

pub struct FakeTransport {
    script: Vec<(Vec<u8>, Vec<u8>)>,
    queue: VecDeque<Vec<u8>>,
    pub writes: Writes,
}

impl Transport for FakeTransport {
    fn write(&mut self, payload: &[u8]) -> Result<()> {
        self.writes.lock().unwrap().push(payload.to_vec());
        if let Some((_, response)) = self
            .script
            .iter()
            .find(|(prefix, _)| payload.starts_with(prefix))
        {
            let mut report = response.clone();
            report.resize(proto::REPORT_SIZE, 0);
            self.queue.push_back(report);
        }
        Ok(())
    }

    fn read(&mut self, _timeout_ms: i32) -> Result<Option<Vec<u8>>> {
        Ok(self.queue.pop_front())
    }
}

pub fn info() -> DeviceInfo {
    DeviceInfo {
        name: "SteelSeries Rival 3 Wireless".into(),
        product_id: proto::PID_RIVAL3_WIRELESS,
        model: Some(proto::MODEL_RIVAL3_WIRELESS),
        path: "fake".into(),
        interface_number: 3,
    }
}

/// A mouse answering from `script`; also returns the log of written commands.
pub fn scripted(script: Vec<(Vec<u8>, Vec<u8>)>) -> (Mouse, Writes) {
    let writes = Writes::default();
    let transport = FakeTransport {
        script,
        queue: VecDeque::new(),
        writes: writes.clone(),
    };
    let mut mouse = Mouse::with_transport(Box::new(transport), info());
    mouse.interval = std::time::Duration::ZERO;
    (mouse, writes)
}

pub fn mouse() -> Mouse {
    scripted(default_script()).0
}

/// The receiver is there but the mouse is asleep (link status 1).
pub fn asleep() -> (Mouse, Writes) {
    scripted(vec![(vec![0xE8], vec![1]), (vec![0xD0], vec![1, 4])])
}
