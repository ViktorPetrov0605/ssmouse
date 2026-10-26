use std::fmt;

pub type Result<T, E = Error> = std::result::Result<T, E>;

#[derive(Debug)]
pub enum Error {
    /// No supported receiver is plugged in.
    NotFound,
    /// The OS refused access (Linux: udev rule missing).
    Permission(String),
    /// The receiver is there but the mouse is asleep or switched off.
    NotLinked,
    /// Another ssmouse process is talking to the mouse.
    Busy,
    Timeout,
    Hid(String),
    Protocol(String),
    Invalid(String),
    Firmware(String),
    Update(String),
    Io(std::io::Error),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::NotFound => f.write_str("no SteelSeries Rival 3 Wireless receiver found (is it plugged in?)"),
            Error::Permission(e) => write!(f, "no permission to open the receiver ({e}); on Linux install the udev rule and replug it"),
            Error::NotLinked => f.write_str("mouse is not linked to the receiver (asleep or switched off) - move or click it and retry"),
            Error::Busy => f.write_str("another ssmouse process is using the mouse; try again"),
            Error::Timeout => f.write_str("the mouse did not answer (asleep?) - move it and retry"),
            Error::Hid(e) => write!(f, "HID error: {e}"),
            Error::Protocol(e) | Error::Invalid(e) | Error::Firmware(e) | Error::Update(e) => f.write_str(e),
            Error::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<crate::protocol::ProtocolError> for Error {
    fn from(e: crate::protocol::ProtocolError) -> Self {
        Error::Protocol(e.0)
    }
}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::Invalid(format!("invalid JSON: {e}"))
    }
}
