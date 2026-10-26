# ssmouse: an open-source SteelSeries GG for the Rival 3 Wireless

Configure and monitor a SteelSeries Rival 3 Wireless (USB `1038:1830`) without
SteelSeries GG, on **Linux, Windows and macOS**. It's a tray app with a
settings window, plus a scriptable CLI. ssmouse is written in Rust and ships as
small self-contained binaries: no Python, runtime or installer needed.

<img src="docs/screenshot.png" width="420" alt="ssmouse settings window">

| Feature | GG | ssmouse |
|---|---|---|
| Battery level, tray icon, low-battery notifications | ✓ | ✓ |
| DPI presets (1–5, 100–18000 CPI) and active preset | ✓ | ✓ |
| Polling rate (125 / 250 / 500 / 1000 Hz) | ✓ | ✓ |
| Wheel LED: steady, colour shift, breathe, reactive, off | ✓ | ✓ |
| Button and wheel remapping: mouse buttons, DPI cycle, scroll, key combos, media keys, disable | ✓ | ✓ |
| Sleep timer, sensor rest, high-efficiency mode | ✓ | ✓ |
| Button debounce (in GG's device descriptor, not shown in its UI) | – | ✓ |
| Firmware update check, download and install | ✓ | ✓ (straight from SteelSeries) |
| Pairing, factory reset, receiver restart | ✓ | ✓ |
| Profiles: export / import | cloud | JSON file |
| Macros, acceleration / angle snapping, Prism Sync, GameSense | ✓ | – (implemented in GG's Windows driver, not in the mouse) |

Every setting is written to the mouse and **saved in its flash memory**, so it
keeps working on any computer, even one without ssmouse. All of the above has
been verified on real hardware (firmware 1.04).

The protocol was reverse engineered from SteelSeries GG. No SteelSeries code,
data or firmware is included. See [Protocol reference](#protocol-reference).

---

## Install

Download the archive for your system from the
[releases page](https://github.com/ViktorPetrov0605/ssmouse/releases):

| System | Contents |
|---|---|
| Linux x86_64 | `ssmouse` (fully static), `ssmouse-gui`, `install.sh`, udev rule |
| Windows x86_64 | `ssmouse.exe`, `ssmouse-gui.exe` (no install needed) |
| macOS (Apple Silicon + Intel) | `ssmouse`, `ssmouse.app` |

**Linux:** unpack and run `./install.sh`. It copies both binaries to
`~/.local/bin`, adds **ssmouse** to the application menu, and installs a udev
rule (needs `sudo`) so your user can open `/dev/hidraw*`. Replug the receiver
afterwards. `./install.sh --uninstall` removes everything. You can also skip the
installer: the binaries run from anywhere once the udev rule is in place.

**Windows:** unzip anywhere and run `ssmouse-gui.exe`. Quit SteelSeries GG
first, because two programs changing the same mouse fight each other.

**macOS:** unzip and open `ssmouse.app`. It isn't signed, so the first time
right-click it and choose *Open*. If macOS asks, grant Input Monitoring.

**From source** (any OS with Rust ≥ 1.89): `cargo build --release`, or on Linux
`./install.sh` in a checkout, which builds and installs.

---

## The app

```console
ssmouse-gui               # tray icon + settings window
ssmouse-gui --minimized   # tray only (what start-on-login uses)
ssmouse-gui --fake        # demo with a simulated mouse
```

* **Tray:** the badge shows the battery percentage, colour-coded. It turns
  grey with the last known value while the mouse sleeps. You get a
  notification at 20 % and again at 10 %. Click the badge to open the settings
  window; right-click for *Refresh*, *Start on login* and *Quit*.
* **Instant updates:** the receiver pushes events when the mouse connects or
  disconnects, when you press the DPI button, and when the battery level
  changes, and ssmouse listens for them. A regular poll (15 s by default,
  adjustable from 5 s to 5 min) is only a safety net. While the mouse sleeps,
  only the receiver is asked, which costs nothing on the radio.
* **Settings window:** tabs for **Status**, **Performance** (DPI presets,
  polling rate), **Lighting**, **Buttons** (click and press a shortcut to bind
  it), **Power** (sleep timer, sensor rest, high-efficiency mode, debounce) and
  **Device** (pairing, receiver restart, profiles, factory reset, firmware).
  Settings apply when the mouse is awake.
* The tray and the window are separate processes that share the device safely
  (a lock keeps their commands from interleaving). Closing the window leaves
  the tray running.
* On GNOME the tray needs the *AppIndicator and KStatusNotifierItem Support*
  extension. Without a tray, the window opens instead.

---

## Command line

```
$ ssmouse info
SteelSeries Rival 3 Wireless
  model     : M-0000022
  firmware  : 1.04
  device    : /dev/hidraw7 (interface 3)
  connected : yes
  battery   : 63%
  dpi       : 800 CPI (preset 2/5: 400, 800, 1200, 1600, 2000)
  polling   : 1000 Hz
  power     : sleep after 5 min, sensor rest on, high efficiency off
```

```console
ssmouse info [--watch 5]                        # everything; --json for scripts
ssmouse battery                                 # --json: {"level": 63, "charging": null}
ssmouse dpi
ssmouse list                                    # HID interfaces and which one is used
ssmouse events                                  # live link / DPI / battery events

ssmouse set dpi 400,800,1600,3200 --active 2    # 1-5 presets, snapped to 100 CPI
ssmouse set dpi-level 3
ssmouse set polling 1000                        # 125 | 250 | 500 | 1000
ssmouse set sleep 10m                           # 1-20 minutes
ssmouse set sensor-rest on
ssmouse set high-efficiency off                 # on = 125 Hz, longest battery life
ssmouse set debounce 6 20                       # press / release in ms

ssmouse light steady ff5500
ssmouse light colorshift red,green,blue --duration 3000
ssmouse light breathe cyan --duration 4000
ssmouse light reactive 0000ff --flash ffffff --fade 800
ssmouse light off
ssmouse light preview ff0000                    # show now, don't store

ssmouse bind                                    # show the mapping
ssmouse bind back key:ctrl+c
ssmouse bind forward media:volume-up            # mute, next, previous, play-pause, volume-up/down
ssmouse bind dpi disabled
ssmouse bind --reset

ssmouse profile show | apply | path | export FILE | import FILE
ssmouse pair                                    # pair a new receiver
ssmouse reset-receiver                          # restart the receiver (like replugging it)
ssmouse reset                                   # factory settings
```

Each change is saved to the mouse's flash. Add `--no-save` to experiment:
lighting and button changes made that way are lost when the mouse is switched
off. Changes are also recorded in a profile file (`ssmouse profile path`). Use
`--debug` to hexdump every HID report, and `--fake` to try everything without a
mouse.

---

## Firmware updates

SteelSeries has no separate firmware feed: new firmware ships inside
SteelSeries GG releases, and GG compares the firmware bundled with it against
the device. ssmouse does the same, but downloads straight from SteelSeries
instead of redistributing anything:

```console
ssmouse firmware              # installed versions vs. newest known
ssmouse firmware check        # ask SteelSeries for a newer GG release (and GitHub for ssmouse)
ssmouse firmware download     # fetch that GG installer from SteelSeries, keep only the firmware
ssmouse firmware update       # install the newest downloaded firmware
ssmouse firmware update PATH  # ...or the firmware/272111664 folder of an existing GG install
```

How it works:

1. `GET https://steelseries.com/api/v2/releases/gg/check-version?platform=windows&current_version=X`
   answers **204** when GG X is current, or **200** with `version`, `installer_url`
   and an MD5 `checksum`. The previous release gets a small patch installer
   without firmware, so ssmouse resolves the **full** installer.
2. The installer (about 430 MB) comes from SteelSeries' CDN and is MD5-verified.
   7-Zip extracts only `apps/engine/firmware/272111664/`, and then the installer
   is deleted. You need 7-Zip installed (`p7zip`/`7zip` on Linux,
   `brew install sevenzip` on macOS, 7-zip.org on Windows).
3. The images are validated (sizes, ARM vector tables, `version.json`). The
   update then runs GG's exact sequence: it flashes the mouse over 2.4 GHz,
   then the receiver's radio and USB chips, which takes about a minute.

The protocol has no checksum, so ssmouse refuses unless the firmware is newer
than what's installed, the mouse is awake and the battery is at least 30 %.
**Do not switch off the mouse or unplug the receiver while it runs.**
`--dry-run` shows the plan without writing anything. SteelSeries GG 122 ships
1.04, which is what current mice already run.

In the app, *Device → Firmware and updates* does the same with buttons. With
*Check for firmware and ssmouse updates daily* on (the default), the app makes
two small requests a day, to SteelSeries' version check and to GitHub's
releases API. It notifies you once per new version and never downloads or
flashes anything by itself.

---

## Protocol reference

Recovered by static analysis of SteelSeries GG 122 (its per-device descriptors),
then confirmed against the hardware. Only the facts needed for interoperability
are documented here.

**Interface.** USB interface **3**, usage page `0xFFC0`, usage `0x0001`
(the `/dev/hidrawN` number varies). Interface 2 (usage page
`0xFF00`) is enumerated first but has **no working OUT
endpoint** on this model: writes fail with `EPIPE`. `ssmouse` ranks interface
3 first and automatically fails over.

**Framing.** The descriptor declares a 65-byte report whose byte 0 is the
constant `report_id = 0x00`. ssmouse writes `[0x00, opcode, payload…]` padded
to 65 bytes, which works with Linux hidraw, Windows and macOS alike.

Input reports are unnumbered 64-byte interrupt-IN transfers; on Linux they
arrive without a report-ID prefix, so `data[0]` is the first struct field.

**Opcode masks.** Plain reads are `0x80 | op`; dongle reads are
`0xC0 | op` (`read-mask = 0x80`, `dongle-read-op-mask = 0x80|0x40 = 0xC0`).
The v1 Rival 3 Wireless descriptor therefore declares `D0` for firmware and
`E8` for RF status, while the observed engine traffic used the `0x80` variants
`90`/`A8`; `ssmouse` sends the descriptor variant first and falls back.

**Interface selection.** The command interface is identified by
**usage page `0xFFC0` / usage `0x0001`** (interface 3 on this device). The
`/dev/hidrawN` node number is *not* stable: it changed from hidraw7 to hidraw6
after a receiver replug. `ssmouse` ranks interfaces by usage page from sysfs
(`HID_PHYS=…/inputN` + report descriptor), not by node name; use
`ssmouse list` to see the current mapping. Interface 2 (`0xFF00`) is the
async sync/event interface and has no usable OUT endpoint (writes → `EPIPE`).

**Timing.** At least ~30 ms between commands, 300-800 ms read timeout, 2
retries (all vendor constraints, implemented in `Protocol`/`Mouse`).

**Commands.**

```
battery      TX: AA 01                       RX: level uint16 LE @0, charge @2
dpi          TX: A0 (read-op 0x20)           RX: level_max @0, active_level @1,
                                                 5 x uint16 LE sensor @2..12
dpi-level    TX: 96 (read-op 0x16)           RX: active level 1..5 @0
polling      TX: 97 (read-op 0x17)           RX: level 0..3 -> 1000/500/250/125
firmware     TX: D0 (dongle-read 0x10)       RX: fw_major @0, fw_minor @1
                fallback: 90 (read-op 0x10)
rf-status    TX: E8 (dongle-read 0xA8)       RX: 4 = connected @0
                fallback: A8 (read-op 0xA8)
power-mode   TX: AB (read-op 0x2B)           RX: u8 power_saving, u8
                                                 movement_saving, u16 LE
                                                 idle_lighting, u16 LE idle_sleep
```

**Write commands.** The mouse does not answer writes; read the value back
to verify.

```
polling      17 level                         0..3 = 1000/500/250/125 Hz
dpi          20 count active s1..s5           u16 LE sensor values
power        2B saving movement u16 idle_light u16 idle_sleep_s
debounce     1E make_ms break_ms              read with 9E
live colour  13 01 r g b                      until 18 (restart lighting)
buttons      19 + 7 x (fn, k1, k2, k3, k4)    L, R, M, back, forward, DPI, unused
wheel        1A + 2 x (fn, k1..k4)            up, down
             fn: 00 off, 01-06 mouse buttons, 30 DPI, 31/32 scroll,
                 51 keys (modifier usages then key usage), 61 consumer usage
lighting     03 00 off16 len16 data           48-byte chunks of a 92-byte effect
             05 00 10FF 00000000 len16        push to effect 0xFF10
             1C 00 0000 0000 00               bind the LED to effect 0
save         09                               store everything in flash
factory      1B
pair         71 00, poll E8 for 4, 73 on timeout
firmware     (08|m) erase, (03|m) buffer, (05|m) push; m = 00 mouse, 40 receiver
```

The 92-byte effect is `u32 duration_ms, i32 wave, i16 x, i16 y, 4 x u8 scale,
u8 overflow + 3 pad, u32 trigger mask, u8 sections, rgb initial, 16 x (rgb,
u8 share of the cycle in 1/255)`; see `src/lighting.rs`.

**Events.** The receiver pushes 64-byte input reports on interface 2 (usage
page `0xFF00`): `28 01`/`28 00` mouse linked/lost, `16 NN` active DPI preset
changed, `2A NN` battery NN %. ssmouse listens there (`src/events.rs`).

**DPI conversion.** The DPI struct stores sensor values, not CPI. The full
`truemove-air` table (`0x00 → 100`, `0x02 → 200`, … `0xD6 → 18000`) is embedded
in `src/protocol.rs`; values missing from the table use the vendor
approximation `100 * floor(sensor * 0.8)`, and values outside `0x00..0xD6` are
invalid (`cpi: null`).

**Charging.** The Rival 3 Wireless runs on AA batteries; its descriptor
declares the charge-status byte as a constant `0x00`, so there is no charging state.

**Matching replies.** Replies carry no opcode. A late or duplicated report
(seen on hardware while the radio link flaps) could be read as the answer to
another command, so every parser accepts only its exact shape, and power
settings are only rewritten after two identical reads.

---

## Troubleshooting

**"mouse is not linked to the receiver (asleep or switched off)"**
Wireless mice sleep when idle. Move or click the mouse and retry. The tray
shows the last known level in grey until it wakes.

**The cursor stops moving after a settings change**
The receiver can lose the mouse if a command arrives while the radio link is
re-negotiating. Run `ssmouse reset-receiver` (or *Device → Restart receiver*),
or unplug and replug the receiver. Settings are not affected.

**Still not linked after moving the mouse**
Usually the batteries are empty. Below a few percent the mouse can't hold the
2.4 GHz link, and its scroll wheel blinks white while it searches. Replace
them. If it still won't link, it may be paired to another receiver: run
`ssmouse pair` and switch the mouse off and on within 20 seconds.

**"no permission to open the receiver"** (Linux)
The udev rule is missing. Run `./install.sh`, or copy
`udev/99-steelseries.rules` to `/etc/udev/rules.d/`, run
`sudo udevadm control --reload-rules && sudo udevadm trigger`, then replug
the receiver.

**"another ssmouse process is using the mouse"**
The tray and the window take turns; retry after a second.

**No tray icon on GNOME**
Install the *AppIndicator and KStatusNotifierItem Support* extension.

---

## Development

```console
cargo test                         # protocol, lighting, bindings, firmware, updates, GUI logic
cargo clippy --all-targets
cargo run --bin ssmouse -- --fake info
cargo run --bin ssmouse-gui -- --fake
cargo build --release --no-default-features --bin ssmouse   # CLI only (static with musl)
```

```
src/
├── protocol.rs   # opcodes, reply parsers (shape-checked), write commands, CPI table
├── device.rs     # HID discovery, transport, cross-process lock, Mouse API
├── lighting.rs   # wheel LED effects (GG's libsteel format)
├── bindings.rs   # button / wheel mapping encoder, key and media names
├── profile.rs    # JSON profile, apply to mouse
├── firmware.rs   # version check, package validation, GG's update sequence
├── updates.rs    # SteelSeries release feed, installer download, ssmouse releases
├── events.rs     # receiver push events
├── status.rs     # read everything at once
├── autostart.rs  # XDG autostart / Windows Run key / macOS LaunchAgent
├── fake.rs       # simulated mouse for tests and --fake
├── gui/          # tray (ksni on Linux, tray-icon elsewhere), settings window (egui), icons
└── bin/          # ssmouse (CLI), ssmouse-gui
```

Portability choices: hidapi's pure-Rust Linux backend (no libudev), a D-Bus
tray without GTK on Linux, rustls instead of OpenSSL, and an icon renderer
built in code. The release CLI for Linux is fully static (musl).

---

## Limitations

* Only the Rival 3 Wireless (`1038:1830`, firmware 1.04) is verified. Related
  PIDs are detected but not validated.
* The mouse cannot report its lighting or button mappings, so ssmouse shows
  what it last applied (stored in the profile).
* Bluetooth mode is not supported. GG's own descriptor disables every setting
  in Bluetooth mode; the vendor protocol needs the 2.4 GHz receiver.
* Macros, acceleration/deceleration, angle snapping, Prism Sync and GameSense
  are implemented by SteelSeries GG's Windows driver, not by the mouse.
* The firmware update runs GG's exact sequence but has not been exercised on
  hardware, since no firmware newer than 1.04 exists to install.
* The Windows and macOS builds pass CI but have not been tried with a real
  mouse yet. Reports are welcome.
