# ssmouse: SteelSeries Rival 3 Wireless battery on Linux

Read the **battery level**, **DPI presets**, **polling rate**, **firmware** and
**connection state** of a SteelSeries Rival 3 Wireless (USB `1038:1830`)
without SteelSeries GG. It comes as a **system-tray app** and a **CLI with JSON
output**. Linux is the main target; Windows and macOS work too.

![ssmouse window](docs/screenshot.png)

* Tray icon showing the battery percentage, colour-coded (green / amber / red)
* Desktop notification when the battery drops to 20 % and again at 10 %
* Remembers the last reading while the mouse sleeps, and picks it back up within
  seconds once you move the mouse
* Optional start on login (XDG autostart)
* CLI for scripts and status bars: `ssmouse --json battery` → `{"level": 71, "charging": null}`

The protocol was reverse engineered from SteelSeries GG. No vendor code or data
files are included. See [Protocol reference](#protocol-reference).

---

## Install

### Linux

```console
git clone https://github.com/ViktorPetrov0605/ssmouse.git
cd ssmouse
./install.sh
```

The installer:

1. installs a udev rule (`udev/99-steelseries.rules` → `/etc/udev/rules.d/`,
   **needs sudo**) so your user can open `/dev/hidraw*`;
2. installs the package with the GUI (PySide6) using `pipx`, or into `./.venv`
   (via `uv` or `python3 -m venv`) if pipx is missing;
3. adds **ssmouse** to your application menu.

Options: `--no-gui` (CLI only), `--no-udev`, `--venv`, `--pipx`,
`--uninstall` (removes the udev rule). **Replug the receiver** after the first
install.

Manual install:

```console
sudo cp udev/99-steelseries.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=usb
pipx install '.[gui]'            # or: pip install '.[gui]' inside a venv
```

> Debian/Ubuntu may need `sudo apt install python3-venv` first.

### Windows / macOS

```console
pip install '.[gui]'
ssmouse-gui
```

Windows and macOS need no udev rule. The libusb `hidapi` backend is picked
automatically. On macOS, grant Input Monitoring if prompted.

---

## The tray app

```console
ssmouse-gui               # window + tray icon
ssmouse-gui --minimized   # start hidden in the tray (what autostart uses)
ssmouse-gui --fake        # demo with captured data, no mouse needed
```

* Left-click the tray icon to show or hide the window. Right-click for *Refresh*,
  *Start on login* and *Quit*. Closing the window keeps the app in the tray.
* While the mouse is connected it updates every 1 minute by default
  (configurable from 15 s to 5 min).
* While the mouse is asleep, only the receiver is asked for the link state, which
  costs nothing on the radio. That check runs every 5 s, so a fresh reading
  appears soon after you touch the mouse.
* Launching it a second time just brings up the existing window.
* On GNOME the tray icon needs the *AppIndicator and KStatusNotifierItem
  Support* extension. Without a tray the app runs as a normal window.

---

## Status

Every value below was verified on real hardware (receiver firmware 1.04):

| Data | Command | Example |
|---|---|---|
| Battery level | `AA 01` | `47 00 00` → 71 % |
| DPI presets + active level | `A0`, `96` | 400 / **800** / 1200 / 1600 / 2000 |
| Polling rate | `97` | 1000 Hz |
| Firmware (receiver) | `D0` (fallback `90`) | `01 04` → 1.04 |
| Link status | `E8` (fallback `A8`) | `04` = linked, `01` = mouse asleep/off |
| Power mode | `AB` | idle timers (raw; units unconfirmed) |
| Charging | n/a | not reported by this model, always `null` |

The receiver itself answers `D0` and `E8`. Everything else is relayed to the mouse
over 2.4 GHz, so it only answers while the mouse is awake. `ssmouse` checks
`E8` first and reports *asleep* immediately instead of timing out.

---

## Command line

```
$ ssmouse info
SteelSeries Rival 3 Wireless
  vendor    : 1038
  product   : 1830
  model     : M-0000022
  serial    : -
  firmware  : 1.04
  interface : /dev/hidraw6 (if 3, usage 0xFFC0)
  transport : hidapi-hidraw
  connected : yes
  battery   : 71% (charge status not reported by this device)
  dpi       : 800 CPI (level 2/5; presets: 400, 800, 1200, 1600, 2000)
  polling   : 1000 Hz
  power     : saving=0 movement=50 idle_light=1200 idle_sleep=300 (raw)
```

Captured byte sequences can be replayed, so the CLI, GUI and tests run on any
machine: `ssmouse --fake --json info`, `ssmouse-gui --fake`, or set
`SSMOUSE_FAKE=1` in the environment.

### Usage

```
ssmouse [global options] {info,battery,dpi}
```

| Command | Description |
|---|---|
| `info` | device metadata + battery + DPI + polling + firmware + connection |
| `battery` | battery level only |
| `dpi` | DPI presets and the active CPI |

Global options (accepted before or after the command):

| Option | Description |
|---|---|
| `--json` | JSON output (stable keys, good for scripting) |
| `--watch` | repeat until Ctrl-C (TUI-style ANSI redraw on a TTY) |
| `--interval SECONDS` | watch interval (default `2.0`) |
| `--debug` | hexdump every HID report to stderr |
| `--list` | list detected SteelSeries HID interfaces and exit |
| `--fake` | replay captured data instead of hardware |
| `--backend …` | `auto` (default), `hidapi-hidraw`, `hidapi-hid`, `ctypes-hidraw` |
| `--path /dev/hidrawN` | force a specific device node |
| `--interface N` | force a specific HID interface number |
| `--framing …` | `default`, `no-id`, `canonical`, `short-3`, `report-id-0` |
| `--retries N` | command retries (default `2`) |
| `--timeout-ms MS` | read timeout per attempt (default `400`) |
| `--version`, `-h/--help` | |

### Examples

```console
ssmouse info
ssmouse --json info | jq '.battery.level'
ssmouse --json dpi
ssmouse --watch --interval 5 battery
ssmouse --debug battery       # show TX/RX hexdumps on stderr
ssmouse --list                # which hidraw node is the control interface?
```

### JSON schemas

`info`:

```json
{
  "name": "SteelSeries Rival 3 Wireless",
  "vendor_id": "1038",
  "product_id": "1830",
  "model": "M-0000022",
  "manufacturer": "SteelSeries",
  "serial_number": null,
  "firmware": "1.04",
  "firmware_source": "dongle",
  "path": "/dev/hidraw6",
  "interface_number": 3,
  "usage_page": "0xFFC0",
  "backend": "hidapi-hidraw",
  "battery": {"level": 47, "charging": null},
  "dpi": {
    "level_max": 5,
    "active_level": 2,
    "presets": [
      {"level": 1, "sensor": 4,  "cpi": 400},
      {"level": 2, "sensor": 6,  "cpi": 600},
      {"level": 3, "sensor": 16, "cpi": 1400},
      {"level": 4, "sensor": 32, "cpi": 2800},
      {"level": 5, "sensor": 48, "cpi": 4100}
    ],
    "current_cpi": 600,
    "current_sensor": 6
  },
  "polling_rate_hz": 250,
  "connected": true,
  "power_mode": {"power_saving": 0, "movement_saving": 0,
                 "idle_lighting": 600, "idle_sleep": 900},
  "protocol": {"framing": "report_id=none size=64 pad=True", "source": "…"}
}
```

`firmware_source` is `"dongle"` when `D0` answered and `"mouse"` when only the
`90` fallback answered.  Any read that fails (device asleep, unsupported,
timeout) yields `null` for that field; `info` adds an `"errors"` array when the
battery read failed.

`battery` → `{"level": 47, "charging": null}`

`dpi` → `{"available": true, "level_max": 5, "active_level": 2,
"presets": […], "current_cpi": 600, "current_sensor": 6}`; when no valid
response arrives, `{"available": false, …, "reason": "…"}`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | I/O or protocol error (e.g. device asleep) |
| `2` | no supported SteelSeries device found |

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
constant `report_id = 0x00`. On Linux the kernel maps the first buffer byte to
the HID report `wValue`, so the working forms are:

```
verified   : [opcode, ...payload..., pad to 64]      # default
canonical  : [0x00, opcode, ...payload..., pad to 65] # --framing canonical
```

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
`ssmouse --list` to see the current mapping. Interface 2 (`0xFF00`) is the
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

**DPI conversion.** The DPI struct stores sensor values, not CPI. The full
`truemove-air` table (`0x00 → 100`, `0x02 → 200`, … `0xD6 → 18000`) is embedded
in `ssmouse/protocol.py`; values missing from the table use the vendor
approximation `100 * floor(sensor * 0.8)`, and values outside `0x00..0xD6` are
invalid (`cpi: null`).

**Charging.** The Rival 3 Wireless runs on AA batteries; its descriptor
declares the charge-status byte as a constant `0x00`. `ssmouse` therefore
reports `"charging": null`. `Protocol(charging_supported=True)` enables real
charge parsing for other models.

---

## Project layout

```
ssmouse/
├── transport.py   # hidapi (hidraw + libusb backends) and ctypes hidraw fallback
├── protocol.py    # framing, packet builders, parsers, CPI table
├── device.py      # VID/PID detection, interface ranking, failover, high-level reads
├── status.py      # one-shot "read everything" shared by CLI and GUI
├── fake.py        # captured/replay transport used by tests and --fake
├── cli.py         # info/battery/dpi, --json, --watch, --debug
└── gui/
    ├── model.py     # Qt-free state: polling, last-known battery, alerts
    ├── widgets.py   # battery gauge, tray icon renderer
    ├── app.py       # window, tray, worker thread, single instance
    └── autostart.py # XDG autostart entry
tests/             # pytest suite (no hardware needed)
tools/probe.py     # hardware probe: enumeration, descriptors, commands, raw hex, pairing
udev/99-steelseries.rules
data/ssmouse-gui.desktop
install.sh
```

### Architecture

`cli` / `gui` → `status` → `device.Mouse` → `protocol.Protocol` ↔ `transport.Transport`.

* `transport.py` only moves bytes. Three interchangeable backends:
  the `hidapi` package's **hidraw** backend (Linux, works with the udev rule),
  its **libusb** backend (Windows/macOS), and a pure-`ctypes` hidraw fallback
  used when `hidapi` cannot be installed.
* `protocol.py` knows every opcode and response layout and contains no I/O.
  New protocol details are a one-line change there.
* `device.py` handles enumeration, control-interface ranking, the 30 ms command
  pacing, retries, and failover to the next interface on `EPIPE`.
* `fake.py` replays captured bytes; the tests never need the mouse.

---

## Troubleshooting

**`mouse is not linked to the receiver (asleep or switched off)`**
Wireless mice sleep when idle. Move the mouse (or click a button) and retry.
The GUI shows the last known level in grey until the mouse wakes up.

**`no response within 400 ms` / `Battery: unavailable`**
The mouse dropped the link mid-read. Retry after moving it.
`--retries 5 --timeout-ms 800` can help the first command after wake-up.

**`connected: false` even after moving the mouse**
Usually the batteries are empty. Below a few percent the mouse cannot hold the
2.4 GHz link, and its scroll wheel blinks white while it searches. Replace the
batteries first. If it still won't link, it may be paired to another receiver.
Run the vendor pairing flow from the probe tool:

```console
.venv/bin/python tools/probe.py --pair
```

It writes `71` (pair_dongle), polls `E8` every 2 s for 20 s (success = `4`
twice), and sends `73` (reset_wireless) on timeout — the same sequence
SteelSeries GG uses. This is a **write** operation, which is why it lives in
`tools/probe.py` and not in the read-only `ssmouse` CLI. If pairing still
fails, check that the AA batteries are charged and the mouse is powered on.

**`no 1038:… device found`**
* Check `lsusb | grep 1038` and `ls -l /dev/hidraw*`.
* The mouse may be switched off, out of battery, or paired to another dongle.
* On Linux install the udev rule (`./install.sh`) and replug the device.
* Use `ssmouse --list` to see which interfaces are detected.

**`EPIPE` / `Broken pipe` on write**
The selected interface has no OUT endpoint. `ssmouse` switches to the next
candidate automatically, but you can force the known-good one:
`ssmouse --interface 3 info` or `ssmouse --path /dev/hidraw7 info`.

**Wrong interface on a different model**
Use `ssmouse --list`, then `--interface N` / `--path /dev/hidrawN`. The
`--framing canonical` or `--framing short-3` presets cover firmware variants
that include a report-ID byte or use a short report.

**`hidapi` will not install**
On Linux the pure-`ctypes` hidraw fallback needs no extra packages:
`ssmouse --backend ctypes-hidraw info`. (Windows/macOS require `hidapi`.)

**Permission denied on `/dev/hidrawN`**
The udev rule is missing or not applied:
```console
sudo cp udev/99-steelseries.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```
Then unplug/replug the mouse.

**WSL**
USB HID passthrough must be configured (`usbipd-win`); otherwise run the tool
on Windows directly.

---

## Development

```console
python3 -m venv .venv && .venv/bin/pip install -e '.[gui,dev]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
.venv/bin/ruff check ssmouse tests
```

The suite (`tests/`) covers battery parsing against the real capture, DPI
parsing/CPI conversion, polling/firmware/status parsers, all framing variants,
interface ranking and EPIPE failover, timeout behaviour, the CLI (including
`--watch` and `--debug`) and the packaged udev/installer/README assets. It
runs entirely on the fake transport. GUI widget tests are skipped when PySide6
is not installed.

Live verification against the hardware (when connected):

```console
.venv/bin/python tools/probe.py --list
.venv/bin/python tools/probe.py --commands battery,dpi,dpi-level,polling,firmware,rf-status,power-mode
.venv/bin/python tools/probe.py --framing canonical --commands battery
.venv/bin/python tools/probe.py --raw aa01
```

### Adding a model

1. Add the PID + name to `PRODUCT_NAMES` / `MODEL_NUMBERS` / `KNOWN_PIDS` in
   `ssmouse/protocol.py`.
2. If the firmware framing differs, add a `FRAMING_PRESETS` entry.
3. If the sensor has a different CPI table, add it next to
   `_TRUEMOVE_AIR_TABLE`.
4. Add a capture to `ssmouse/fake.py` and a parser test.

---

## Limitations

* Read-only by design: no DPI/polling/RGB/button **writes** are implemented.
* Charging state is unavailable on this AA-battery model (`null`).
* Serial number is empty on the tested unit; the field is passed through from
  HID enumeration when present.
* Firmware string format is `major.minor` (e.g. `1.04`) as declared by the
  descriptor.
* Pairing writes (`71`/`73`) are available only in `tools/probe.py`; the
  `ssmouse` CLI is read-only by design.
* Only the Rival 3 Wireless (`1038:1830`) is verified. Related PIDs
  (`0x1832`, `0x1834`, `0x1836`) are detected but not validated.
