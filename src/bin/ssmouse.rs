//! `ssmouse`: command line interface.

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

use clap::{Parser, Subcommand};
use serde_json::json;
use ssmouse::bindings::{self, Bindings};
use ssmouse::firmware;
use ssmouse::lighting::{self, Effect, Mode};
use ssmouse::profile::{self, Profile};
use ssmouse::{Error, Mouse, Result, events, status, updates};

#[derive(Parser)]
#[command(
    name = "ssmouse",
    version,
    about = "Read and configure a SteelSeries Rival 3 Wireless without SteelSeries GG"
)]
struct Cli {
    /// Machine-readable JSON output
    #[arg(long, global = true)]
    json: bool,
    /// Hexdump every HID report on stderr
    #[arg(long, global = true)]
    debug: bool,
    /// Use a simulated mouse (no hardware; same as SSMOUSE_FAKE=1)
    #[arg(long, global = true)]
    fake: bool,
    /// Do not store changes in the mouse's flash (lighting and buttons are lost at power-off)
    #[arg(long, global = true)]
    no_save: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Battery, DPI, polling rate, firmware and link state
    Info {
        /// Repeat every N seconds until Ctrl-C
        #[arg(long, value_name = "SECONDS")]
        watch: Option<f64>,
    },
    /// Battery level
    Battery {
        #[arg(long, value_name = "SECONDS")]
        watch: Option<f64>,
    },
    /// DPI presets and the active one
    Dpi,
    /// List detected SteelSeries HID interfaces
    List,
    /// Print receiver events (link, DPI button, battery) as they happen
    Events,
    /// Change DPI, polling rate, power and debounce settings
    Set {
        #[command(subcommand)]
        setting: Setting,
    },
    /// Wheel LED effect
    Light {
        /// steady, colorshift, breathe, reactive, off, or preview (show now, don't store)
        mode: String,
        /// Colours: rrggbb, r,g,b or names; several for colorshift/breathe
        colors: Option<String>,
        /// Cycle length in ms
        #[arg(long)]
        duration: Option<u32>,
        /// Reactive: colour shown on click (default white)
        #[arg(long)]
        flash: Option<String>,
        /// Reactive: fade-back time in ms (default 500)
        #[arg(long)]
        fade: Option<u32>,
    },
    /// Remap buttons and the wheel (no arguments: show the mapping)
    Bind {
        /// left, right, middle, back, forward, dpi, wheel-up, wheel-down
        control: Option<String>,
        /// left, right, middle, back, forward, button6, dpi, scroll-up, scroll-down, disabled,
        /// key:<combo> (e.g. key:ctrl+c), media:<mute|next|previous|play-pause|volume-up|volume-down>
        action: Option<String>,
        /// Restore the default (for one control, or all)
        #[arg(long)]
        reset: bool,
    },
    /// Show, apply, export or import the saved profile
    Profile {
        /// show, apply, path, export, import
        action: String,
        file: Option<PathBuf>,
    },
    /// Store the current settings in the mouse's flash
    Save,
    /// Restore factory settings
    Reset {
        #[arg(long)]
        yes: bool,
    },
    /// Pair the mouse with the receiver (switch the mouse off and on when asked)
    Pair,
    /// Restart the receiver (fixes a receiver that lost the mouse; like replugging it)
    ResetReceiver,
    /// Firmware versions, online check, download and update
    Firmware {
        /// status, check, download, update
        #[arg(default_value = "status")]
        action: String,
        /// update: a GG firmware/272111664 folder (default: newest download)
        folder: Option<PathBuf>,
        /// Validate and show the plan only
        #[arg(long)]
        dry_run: bool,
        /// Also flash when the installed firmware is the same or newer
        #[arg(long)]
        force: bool,
        #[arg(long)]
        yes: bool,
    },
}

#[derive(Subcommand)]
enum Setting {
    /// DPI presets, e.g. 400,800,1600
    Dpi {
        presets: String,
        /// Preset to select (1-based)
        #[arg(long)]
        active: Option<u8>,
    },
    /// Select a stored DPI preset
    DpiLevel { level: u8 },
    /// Report rate: 125, 250, 500 or 1000
    Polling { hz: u16 },
    /// Sleep timer, e.g. 5m or 300 (1-20 minutes)
    Sleep { duration: String },
    /// Lower sensor power while the mouse is still: on/off
    SensorRest { state: String },
    /// High-efficiency mode (125 Hz, longest battery life): on/off
    HighEfficiency { state: String },
    /// Button debounce in ms
    Debounce { make_ms: u8, break_ms: u8 },
}

fn on_off(text: &str) -> Result<bool> {
    match text.to_lowercase().as_str() {
        "on" | "yes" | "true" | "1" | "enable" | "enabled" => Ok(true),
        "off" | "no" | "false" | "0" | "disable" | "disabled" => Ok(false),
        _ => Err(Error::Invalid(format!("expected on/off, got {text:?}"))),
    }
}

fn duration_s(text: &str) -> Result<u16> {
    let t = text.trim().to_lowercase();
    let (number, minutes) = match t.strip_suffix("min").or_else(|| t.strip_suffix('m')) {
        Some(n) => (n, true),
        None => (t.strip_suffix('s').unwrap_or(&t), false),
    };
    let value: u16 = number
        .trim()
        .parse()
        .map_err(|_| Error::Invalid("use seconds or minutes, e.g. 300 or 5m".into()))?;
    Ok(if minutes { value * 60 } else { value })
}

struct Ctx {
    json: bool,
    debug: bool,
    persist: bool,
}

impl Ctx {
    fn open(&self) -> Result<Mouse> {
        let mut mouse = Mouse::open()?;
        mouse.debug = self.debug;
        Ok(mouse)
    }

    fn linked(&self) -> Result<Mouse> {
        let mut mouse = self.open()?;
        mouse.require_link()?;
        Ok(mouse)
    }

    fn finish(&self, mouse: &mut Mouse, message: String) -> Result<()> {
        if self.persist {
            mouse.save_to_flash()?;
        }
        if self.json {
            println!("{}", json!({"ok": true, "message": message}));
        } else {
            println!("{message}");
        }
        Ok(())
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    if cli.fake {
        // SAFETY: set before any other thread exists.
        unsafe { std::env::set_var(ssmouse::fake::ENV_FAKE, "1") };
    }
    let ctx = Ctx {
        json: cli.json,
        debug: cli.debug,
        persist: !cli.no_save,
    };
    match run(cli.command, &ctx) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ssmouse: {e}");
            ExitCode::from(if matches!(e, Error::NotFound) { 2 } else { 1 })
        }
    }
}

fn run(command: Command, ctx: &Ctx) -> Result<()> {
    match command {
        Command::Info { watch } => repeat(watch, || info(ctx)),
        Command::Battery { watch } => repeat(watch, || battery(ctx)),
        Command::Dpi => dpi(ctx),
        Command::List => list(ctx),
        Command::Events => {
            eprintln!("listening for receiver events (Ctrl-C to stop)");
            let stop = AtomicBool::new(false);
            events::listen(
                |event| {
                    if ctx.json {
                        let (kind, value) = match event {
                            events::Event::Link(on) => ("link", u32::from(on)),
                            events::Event::DpiLevel(l) => ("dpi_level", l.into()),
                            events::Event::Battery(b) => ("battery", b.into()),
                        };
                        println!("{}", json!({"event": kind, "value": value}));
                    } else {
                        println!("{event:?}");
                    }
                    std::io::stdout().flush().ok();
                },
                &stop,
            );
            Ok(())
        }
        Command::Set { setting } => set(ctx, setting),
        Command::Light {
            mode,
            colors,
            duration,
            flash,
            fade,
        } => light(ctx, &mode, colors, duration, flash, fade),
        Command::Bind {
            control,
            action,
            reset,
        } => bind(ctx, control, action, reset),
        Command::Profile { action, file } => profile_cmd(ctx, &action, file),
        Command::Save => {
            let mut mouse = ctx.linked()?;
            mouse.save_to_flash()?;
            println!("settings saved to the mouse's flash");
            Ok(())
        }
        Command::Reset { yes } => {
            if !yes && !confirm("Restore the mouse's factory settings? [y/N] ", "y")? {
                println!("cancelled");
                return Ok(());
            }
            let mut mouse = ctx.linked()?;
            mouse.factory_reset()?;
            Profile::default().save()?;
            println!("factory settings restored");
            Ok(())
        }
        Command::Pair => {
            let mut mouse = ctx.open()?;
            println!("pairing... switch the mouse off and on again in 2.4 GHz mode now");
            println!(
                "{}",
                if mouse.pair()? {
                    "paired"
                } else {
                    "pairing timed out (receiver reset)"
                }
            );
            Ok(())
        }
        Command::ResetReceiver => {
            let mut mouse = ctx.open()?;
            mouse.reset_receiver()?;
            drop(mouse);
            std::thread::sleep(Duration::from_secs(4));
            let mut mouse = ctx.open()?;
            let linked = mouse.wait_for_stable_link(Duration::from_secs(6), 2);
            println!(
                "receiver restarted; mouse {}",
                if linked {
                    "linked"
                } else {
                    "not linked yet (move it)"
                }
            );
            Ok(())
        }
        Command::Firmware {
            action,
            folder,
            dry_run,
            force,
            yes,
        } => firmware_cmd(ctx, &action, folder, dry_run, force, yes),
    }
}

fn repeat(watch: Option<f64>, mut f: impl FnMut() -> Result<()>) -> Result<()> {
    let Some(seconds) = watch else { return f() };
    loop {
        if let Err(e) = f() {
            eprintln!("ssmouse: {e}");
        }
        std::thread::sleep(Duration::from_secs_f64(seconds.max(0.5)));
    }
}

fn confirm(prompt: &str, expected: &str) -> Result<bool> {
    print!("{prompt}");
    std::io::stdout().flush()?;
    let mut answer = String::new();
    std::io::stdin().read_line(&mut answer)?;
    Ok(answer.trim().eq_ignore_ascii_case(expected)
        || (expected == "y" && answer.trim().eq_ignore_ascii_case("yes")))
}

fn info(ctx: &Ctx) -> Result<()> {
    let mut mouse = ctx.open()?;
    let s = status::read(&mut mouse);
    if ctx.json {
        println!("{}", serde_json::to_string(&s)?);
        return Ok(());
    }
    let opt = |v: Option<String>, none: &str| v.unwrap_or_else(|| none.to_string());
    println!("{}", s.name);
    println!("  model     : {}", s.model.unwrap_or("-"));
    println!("  firmware  : {}", opt(s.firmware.clone(), "unknown"));
    println!("  device    : {} (interface {})", s.path, s.interface);
    println!(
        "  connected : {}",
        match s.connected {
            Some(true) => "yes",
            Some(false) => "no (asleep or off)",
            None => "unknown",
        }
    );
    println!(
        "  battery   : {}",
        opt(s.battery.map(|b| format!("{b}%")), "unavailable")
    );
    if let Some(dpi) = &s.dpi {
        let presets: Vec<String> = dpi.cpis().iter().map(u16::to_string).collect();
        println!(
            "  dpi       : {} CPI (preset {}/{}: {})",
            dpi.current_cpi().unwrap_or(0),
            dpi.active_level,
            dpi.level_max,
            presets.join(", ")
        );
    } else {
        println!("  dpi       : unavailable");
    }
    println!(
        "  polling   : {}",
        opt(s.polling_hz.map(|hz| format!("{hz} Hz")), "unknown")
    );
    if let Some(p) = s.power {
        println!(
            "  power     : sleep after {} min, sensor rest {}, high efficiency {}",
            p.idle_sleep / 60,
            if p.movement_saving > 0 { "on" } else { "off" },
            if p.power_saving > 0 { "on" } else { "off" }
        );
    }
    for e in &s.errors {
        eprintln!("warning: {e}");
    }
    Ok(())
}

fn battery(ctx: &Ctx) -> Result<()> {
    let mut mouse = ctx.linked()?;
    let level = mouse.battery()?;
    if ctx.json {
        println!("{}", json!({"level": level, "charging": null}));
    } else {
        println!("Battery: {level}%");
    }
    Ok(())
}

fn dpi(ctx: &Ctx) -> Result<()> {
    let mut mouse = ctx.linked()?;
    let info = mouse.dpi().ok_or(Error::Timeout)?;
    if ctx.json {
        println!(
            "{}",
            json!({"presets": info.cpis(), "active_level": info.active_level, "current_cpi": info.current_cpi()})
        );
    } else {
        let presets: Vec<String> = info.cpis().iter().map(u16::to_string).collect();
        println!(
            "DPI: {} CPI (preset {}/{}: {})",
            info.current_cpi().unwrap_or(0),
            info.active_level,
            info.level_max,
            presets.join(", ")
        );
    }
    Ok(())
}

fn list(ctx: &Ctx) -> Result<()> {
    let all = ssmouse::device::enumerate()?;
    if ctx.json {
        let rows: Vec<_> = all.iter().map(|i| json!({"path": i.path, "interface": i.interface_number, "usage_page": i.usage_page, "product_id": i.product_id})).collect();
        println!("{}", serde_json::Value::Array(rows));
        return Ok(());
    }
    if all.is_empty() {
        return Err(Error::NotFound);
    }
    let control = ssmouse::device::control_interface(&all).cloned();
    let event = ssmouse::device::event_interface(&all).cloned();
    for i in &all {
        let role = if Some(i) == control.as_ref() {
            "  <- commands"
        } else if Some(i) == event.as_ref() {
            "  <- events"
        } else {
            ""
        };
        println!(
            "{:<24} if={} usage={:#06x} {:04x}:{:04x} {}{role}",
            i.path, i.interface_number, i.usage_page, i.vendor_id, i.product_id, i.product
        );
    }
    Ok(())
}

fn set(ctx: &Ctx, setting: Setting) -> Result<()> {
    let mut profile = Profile::load()?;
    let mut mouse = ctx.linked()?;
    let message = match setting {
        Setting::Dpi { presets, active } => {
            let cpis: Vec<u16> = presets
                .split(',')
                .map(|p| {
                    p.trim()
                        .parse()
                        .map_err(|_| Error::Invalid(format!("bad CPI {p:?}")))
                })
                .collect::<Result<_>>()?;
            let active = active.unwrap_or_else(|| {
                profile
                    .dpi
                    .as_ref()
                    .map(|d| d.active)
                    .unwrap_or(1)
                    .min(cpis.len() as u8)
            });
            let used = mouse.set_dpi(&cpis, active)?;
            profile.dpi = Some(profile::Dpi {
                presets: used.clone(),
                active,
            });
            format!(
                "DPI presets {}, active {active} ({} CPI)",
                used.iter()
                    .map(u16::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                used[active as usize - 1]
            )
        }
        Setting::DpiLevel { level } => {
            mouse.set_dpi_level(level)?;
            if let Some(d) = profile.dpi.as_mut() {
                d.active = level;
            }
            format!("DPI preset {level} selected")
        }
        Setting::Polling { hz } => {
            mouse.set_polling_rate(hz)?;
            profile.polling_hz = Some(hz);
            format!("polling rate {hz} Hz")
        }
        Setting::Debounce { make_ms, break_ms } => {
            mouse.set_debounce(make_ms, break_ms)?;
            profile.debounce = Some(profile::Debounce { make_ms, break_ms });
            format!("debounce: press {make_ms} ms, release {break_ms} ms")
        }
        Setting::Sleep { duration } => {
            let s = duration_s(&duration)?;
            if !(60..=1200).contains(&s) {
                return Err(Error::Invalid("sleep timer must be 1..20 minutes".into()));
            }
            profile.power.sleep_s = Some(s);
            profile::apply_power(&mut mouse, &profile.power)?;
            format!("sleep after {} min idle", s / 60)
        }
        Setting::SensorRest { state } => {
            profile.power.sensor_rest = Some(on_off(&state)?);
            profile::apply_power(&mut mouse, &profile.power)?;
            format!(
                "sensor rest {}",
                if profile.power.sensor_rest == Some(true) {
                    "on"
                } else {
                    "off"
                }
            )
        }
        Setting::HighEfficiency { state } => {
            let on = on_off(&state)?;
            profile.power.high_efficiency = Some(on);
            profile::apply(&mut mouse, &profile, &["power", "polling"], false)?;
            format!(
                "high-efficiency mode {}",
                if on { "on (125 Hz)" } else { "off" }
            )
        }
    };
    profile.save()?;
    ctx.finish(&mut mouse, message)
}

fn light(
    ctx: &Ctx,
    mode: &str,
    colors: Option<String>,
    duration: Option<u32>,
    flash: Option<String>,
    fade: Option<u32>,
) -> Result<()> {
    let colors = colors.as_deref().map(lighting::parse_colors).transpose()?;
    if mode == "preview" {
        let color = colors
            .and_then(|c| c.first().copied())
            .ok_or_else(|| Error::Invalid("preview needs a colour".into()))?;
        let mut mouse = ctx.linked()?;
        mouse.set_live_color(color)?;
        println!(
            "showing {} (not stored; `ssmouse profile apply` restores the effect)",
            lighting::format_color(color)
        );
        return Ok(());
    }
    let mut profile = Profile::load()?;
    let mode = Mode::parse(mode)?;
    let previous = profile.lighting.clone().unwrap_or_default();
    let mut effect = Effect { mode, ..previous };
    if let Some(c) = colors {
        effect.colors = c;
    }
    if matches!(mode, Mode::Steady | Mode::Reactive) {
        effect.colors.truncate(1);
    }
    if let Some(d) = duration {
        effect.duration_ms = d;
    }
    if let Some(f) = flash {
        effect.react_color = lighting::parse_color(&f)?;
    }
    if let Some(f) = fade {
        effect.react_ms = f;
    }
    effect.validate()?;
    let mut mouse = ctx.linked()?;
    mouse.set_lighting(&effect)?;
    let described = format!(
        "lighting: {} {}",
        mode.label(),
        effect
            .colors
            .iter()
            .map(|c| lighting::format_color(*c))
            .collect::<Vec<_>>()
            .join(" ")
    );
    profile.lighting = Some(effect);
    profile.save()?;
    ctx.finish(&mut mouse, described)
}

fn bind(ctx: &Ctx, control: Option<String>, action: Option<String>, reset: bool) -> Result<()> {
    let mut profile = Profile::load()?;
    let Some(control) = control else {
        if reset {
            profile.bindings = Bindings::new();
        } else {
            for (name, label, default) in bindings::controls() {
                let spec = profile
                    .bindings
                    .get(name)
                    .map(String::as_str)
                    .unwrap_or(default);
                println!("{name:<11} {:<22} {}", label, bindings::describe(spec));
            }
            return Ok(());
        }
        let mut mouse = ctx.linked()?;
        mouse.set_bindings(&profile.bindings)?;
        profile.save()?;
        return ctx.finish(&mut mouse, "all buttons restored to defaults".into());
    };
    let default = bindings::default_for(&control)
        .ok_or_else(|| Error::Invalid(format!("unknown control {control:?}")))?;
    if reset {
        profile.bindings.remove(&control);
    } else {
        let action = action
            .ok_or_else(|| Error::Invalid("bind needs an action (or --reset)".into()))?
            .to_lowercase();
        bindings::encode(&action)?;
        profile.bindings.insert(control.clone(), action);
    }
    let mut mouse = ctx.linked()?;
    mouse.set_bindings(&profile.bindings)?;
    profile.save()?;
    let spec = profile
        .bindings
        .get(&control)
        .map(String::as_str)
        .unwrap_or(default);
    ctx.finish(
        &mut mouse,
        format!("{control} -> {}", bindings::describe(spec)),
    )
}

fn profile_cmd(ctx: &Ctx, action: &str, file: Option<PathBuf>) -> Result<()> {
    let need_file = || {
        file.clone()
            .ok_or_else(|| Error::Invalid(format!("profile {action} needs a file name")))
    };
    match action {
        "path" => println!("{}", profile::default_path().display()),
        "show" => println!("{}", serde_json::to_string_pretty(&Profile::load()?)?),
        "export" => {
            let path = need_file()?;
            Profile::load()?.save_to(&path)?;
            println!("exported to {}", path.display());
        }
        "apply" | "import" => {
            let profile = if action == "import" {
                Profile::load_from(&need_file()?)?
            } else {
                Profile::load()?
            };
            let mut mouse = ctx.linked()?;
            let applied = profile::apply(&mut mouse, &profile, &profile::ALL_SECTIONS, false)?;
            if action == "import" {
                profile.save()?;
            }
            let text = if applied.is_empty() {
                "nothing configured".to_string()
            } else {
                applied.join(", ")
            };
            return ctx.finish(&mut mouse, format!("applied: {text}"));
        }
        other => {
            return Err(Error::Invalid(format!(
                "unknown profile action {other:?} (show, apply, path, export, import)"
            )));
        }
    }
    Ok(())
}

fn firmware_cmd(
    ctx: &Ctx,
    action: &str,
    folder: Option<PathBuf>,
    dry_run: bool,
    force: bool,
    yes: bool,
) -> Result<()> {
    match action {
        "status" => {
            let mut mouse = ctx.open()?;
            let s = firmware::FirmwareStatus::read(&mut mouse);
            let latest = updates::best_package()
                .map(|p| p.version)
                .unwrap_or_else(|| firmware::LATEST_KNOWN.into());
            let verdict = match s.up_to_date(&latest) {
                Some(true) => "up to date",
                Some(false) => "update available",
                None => "unknown",
            };
            if ctx.json {
                println!(
                    "{}",
                    json!({"mouse": s.mouse, "receiver": s.receiver, "latest": latest, "up_to_date": s.up_to_date(&latest)})
                );
            } else {
                println!(
                    "mouse     : {}",
                    s.mouse.as_deref().unwrap_or("unknown (asleep?)")
                );
                println!("receiver  : {}", s.receiver.as_deref().unwrap_or("unknown"));
                println!("latest    : {latest}");
                println!("status    : {verdict}");
            }
            Ok(())
        }
        "check" => {
            let gg = updates::check_gg_release(None)?;
            let app = updates::check_app_release();
            match &gg {
                None => println!(
                    "firmware : no newer SteelSeries GG than {}, so no new firmware",
                    updates::last_checked_gg()
                ),
                Some(r) => println!(
                    "firmware : SteelSeries GG {} is out and may contain new firmware; run `ssmouse firmware download` (about 430 MB)",
                    r.version
                ),
            }
            match app {
                Ok(Some(r)) => println!("ssmouse  : version {} is available: {}", r.version, r.url),
                Ok(None) => println!("ssmouse  : up to date ({})", updates::APP_VERSION),
                Err(e) => println!("ssmouse  : could not check ({e})"),
            }
            Ok(())
        }
        "download" => {
            let mut last = u64::MAX;
            let result = updates::fetch_firmware(None, |done, total| {
                let pct = (done * 100).checked_div(total).unwrap_or(0);
                if pct != last {
                    last = pct;
                    eprint!("\r  downloading SteelSeries GG: {pct:3}%");
                }
            })?;
            match result {
                None => println!(
                    "nothing to download: SteelSeries GG {} is the newest release",
                    updates::last_checked_gg()
                ),
                Some((release, package)) => {
                    println!(
                        "\nSteelSeries GG {} contains Rival 3 Wireless firmware {} (saved in {})",
                        release.version,
                        package.version,
                        package.source.display()
                    );
                    println!("install it with: ssmouse firmware update");
                }
            }
            Ok(())
        }
        "update" => {
            let package = match folder {
                Some(f) => firmware::load_package(&f)?,
                None => updates::best_package().ok_or_else(|| Error::Invalid(
                    "no firmware downloaded yet: run `ssmouse firmware download`, or pass a GG firmware/272111664 folder".into()))?,
            };
            let mut mouse = ctx.open()?;
            let steps = firmware::prepare(&mut mouse, &package, force)?;
            let (writes, time) = firmware::estimate(&steps);
            println!(
                "firmware {} from {}: {writes} commands, about {} s",
                package.version,
                package.source.display(),
                time.as_secs()
            );
            if dry_run {
                return Ok(());
            }
            if !yes {
                println!("Do NOT switch off the mouse or unplug the receiver while updating.");
                if !confirm(
                    &format!("Type 'flash' to install firmware {}: ", package.version),
                    "flash",
                )? {
                    println!("cancelled");
                    return Ok(());
                }
            }
            firmware::update(&mut mouse, &package, force, |p| {
                eprint!("\r  {p:3}%");
            })?;
            println!(
                "\nupdate sent; the mouse and receiver restart now. Check with `ssmouse firmware` in a few seconds."
            );
            Ok(())
        }
        other => Err(Error::Invalid(format!(
            "unknown firmware action {other:?} (status, check, download, update)"
        ))),
    }
}
