//! ssmouse: read and configure a SteelSeries Rival 3 Wireless without SteelSeries GG.
//!
//! ```no_run
//! let mut mouse = ssmouse::Mouse::open()?;
//! println!("battery: {}%", mouse.battery()?);
//! # Ok::<(), ssmouse::Error>(())
//! ```

pub mod autostart;
pub mod bindings;
pub mod device;
pub mod error;
pub mod events;
pub mod fake;
pub mod firmware;
pub mod lighting;
pub mod paths;
pub mod profile;
pub mod protocol;
pub mod status;
pub mod updates;

#[cfg(feature = "gui")]
pub mod gui;

pub use device::Mouse;
pub use error::{Error, Result};
