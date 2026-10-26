//! The settings window (`ssmouse-gui --window`): status and every setting.
//!
//! Device I/O runs on a worker thread, one job at a time; each job opens the
//! device (taking the cross-process lock) and closes it again, so the tray
//! keeps working while the window is open.

use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant};

use eframe::egui::{self, Color32, RichText};

use crate::bindings;
use crate::device::Mouse;
use crate::firmware::{self, FirmwareStatus, Package};
use crate::lighting::{DEFAULT_COLOR, Effect, Mode};
use crate::profile::{self, Profile};
use crate::protocol::{self as proto, DpiInfo, PowerMode};
use crate::status::{self, Status};
use crate::{Error, autostart, updates};

use super::icon;
use super::monitor::{Link, Snapshot};
use super::settings::{INTERVALS, Settings};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Status,
    Performance,
    Lighting,
    Buttons,
    Power,
    Device,
}

#[derive(Debug, Clone, Default)]
struct DeviceConfig {
    dpi: Option<DpiInfo>,
    polling_hz: Option<u16>,
    power: Option<PowerMode>,
    debounce: Option<(u8, u8)>,
    firmware: FirmwareStatus,
}

enum Reply {
    Status(Box<Status>),
    Config(Box<DeviceConfig>),
    Done(String),
}

type Job = Box<dyn FnOnce(&mut Mouse) -> crate::Result<Reply> + Send>;

enum Net {
    Checked(Option<updates::GgRelease>, Option<updates::AppRelease>),
    Progress(u64, u64),
    Downloaded(Package),
    Failed(String),
}

struct Worker {
    jobs: Sender<(String, Job)>,
    replies: Receiver<(String, crate::Result<Reply>)>,
    pending: usize,
}

impl Worker {
    fn new(ctx: egui::Context) -> Self {
        let (jobs, job_rx) = mpsc::channel::<(String, Job)>();
        let (reply_tx, replies) = mpsc::channel();
        std::thread::spawn(move || {
            for (tag, job) in job_rx {
                let result = Mouse::open().and_then(|mut mouse| job(&mut mouse));
                let _ = reply_tx.send((tag, result));
                ctx.request_repaint();
            }
        });
        Self {
            jobs,
            replies,
            pending: 0,
        }
    }

    fn run(&mut self, tag: &str, job: Job) {
        self.pending += 1;
        let _ = self.jobs.send((tag.to_string(), job));
    }
}

#[derive(Clone)]
struct BindingEdit {
    control: &'static str,
    label: &'static str,
    spec: String,
    recording: bool,
}

pub struct App {
    tab: Tab,
    worker: Worker,
    net_tx: Sender<Net>,
    net_rx: Receiver<Net>,
    ctx: egui::Context,
    snap: Snapshot,
    config: Option<DeviceConfig>,
    profile: Profile,
    settings: Settings,
    autostart: bool,
    message: Option<(String, bool)>,
    next_status: Instant,
    // edits
    dpi_count: usize,
    dpi: [u16; 5],
    dpi_active: u8,
    polling: u16,
    effect: Effect,
    binds: Vec<BindingEdit>,
    sleep_s: u16,
    sensor_rest: bool,
    high_efficiency: bool,
    debounce: (u8, u8),
    // device tab
    profile_path: String,
    firmware_folder: String,
    online: String,
    gg_release: Option<updates::GgRelease>,
    app_release: Option<updates::AppRelease>,
    package: Option<Package>,
    download: Option<(u64, u64)>,
    confirm: Option<Confirm>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Confirm {
    FactoryReset,
    Firmware,
    Pair,
}

pub fn run() -> eframe::Result {
    let icon = egui::IconData {
        rgba: icon::badge_rgba(Some(100), false, 64),
        width: 64,
        height: 64,
    };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("ssmouse")
            .with_app_id("ssmouse")
            .with_inner_size([560.0, 640.0])
            .with_min_inner_size([480.0, 520.0])
            .with_icon(std::sync::Arc::new(icon)),
        ..Default::default()
    };
    eframe::run_native(
        "ssmouse",
        options,
        Box::new(|cc| Ok(Box::new(App::new(cc.egui_ctx.clone())))),
    )
}

impl App {
    fn new(ctx: egui::Context) -> Self {
        let profile = Profile::load().unwrap_or_default();
        let (net_tx, net_rx) = mpsc::channel();
        let effect = profile.lighting.clone().unwrap_or_default();
        let binds = bindings::controls()
            .map(|(control, label, default)| BindingEdit {
                control,
                label,
                spec: profile
                    .bindings
                    .get(control)
                    .cloned()
                    .unwrap_or_else(|| default.to_string()),
                recording: false,
            })
            .collect();
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_default();
        let mut app = Self {
            tab: Tab::Status,
            worker: Worker::new(ctx.clone()),
            net_tx,
            net_rx,
            ctx,
            snap: Snapshot::new(Link::Asleep),
            config: None,
            profile,
            settings: Settings::load(),
            autostart: autostart::is_enabled(),
            message: None,
            next_status: Instant::now(),
            dpi_count: 5,
            dpi: [400, 800, 1200, 1600, 2000],
            dpi_active: 1,
            polling: 1000,
            effect,
            binds,
            sleep_s: 300,
            sensor_rest: true,
            high_efficiency: false,
            debounce: (6, 20),
            profile_path: home.join("ssmouse-profile.json").to_string_lossy().into_owned(),
            firmware_folder: String::new(),
            online: "New firmware comes with SteelSeries GG releases; ssmouse checks SteelSeries' release feed for them.".into(),
            gg_release: None,
            app_release: None,
            package: updates::best_package(),
            download: None,
            confirm: None,
        };
        app.snap.detail = Some("Connecting\u{2026}".into());
        if let Some(level) = app.settings.last_battery {
            app.snap.battery = Some(level);
            app.snap.stale = true;
            app.snap.seen_at = app.settings.last_seen;
        }
        // Developer aid (screenshots): SSMOUSE_START_TAB=performance etc.
        app.tab = match std::env::var("SSMOUSE_START_TAB")
            .unwrap_or_default()
            .as_str()
        {
            "performance" => Tab::Performance,
            "lighting" => Tab::Lighting,
            "buttons" => Tab::Buttons,
            "power" => Tab::Power,
            "device" => Tab::Device,
            _ => Tab::Status,
        };
        app.load_config();
        app
    }

    fn linked(&self) -> bool {
        self.snap.link == Link::Connected
    }

    fn say(&mut self, text: impl Into<String>, error: bool) {
        self.message = Some((text.into(), error));
    }

    fn refresh_status(&mut self) {
        self.next_status = Instant::now() + Duration::from_secs(5);
        self.worker.run(
            "status",
            Box::new(|mouse| Ok(Reply::Status(Box::new(status::read(mouse))))),
        );
    }

    fn load_config(&mut self) {
        self.worker.run(
            "config",
            Box::new(|mouse| {
                mouse.require_link()?;
                Ok(Reply::Config(Box::new(DeviceConfig {
                    dpi: mouse.dpi(),
                    polling_hz: mouse.polling_rate(),
                    power: mouse.power_mode_confirmed(),
                    debounce: mouse.debounce(),
                    firmware: FirmwareStatus::read(mouse),
                })))
            }),
        );
    }

    /// Run a change, save it to flash, then re-read the device.
    fn apply(
        &mut self,
        what: &str,
        job: impl FnOnce(&mut Mouse) -> crate::Result<String> + Send + 'static,
    ) {
        self.say(format!("{what}\u{2026}"), false);
        self.worker.run(
            "apply",
            Box::new(move |mouse| {
                mouse.require_link()?;
                let message = job(mouse)?;
                mouse.save_to_flash()?;
                Ok(Reply::Done(message))
            }),
        );
        if let Err(e) = self.profile.save() {
            self.say(format!("Could not save the profile: {e}"), true);
        }
    }

    fn init_edits(&mut self, config: &DeviceConfig) {
        if let Some(dpi) = &config.dpi {
            self.dpi_count = dpi.presets.len();
            for (slot, cpi) in self.dpi.iter_mut().zip(dpi.cpis()) {
                *slot = cpi;
            }
            self.dpi_active = dpi.active_level;
        }
        if let Some(hz) = config.polling_hz {
            self.polling = hz;
        }
        if let Some(p) = config.power {
            self.sleep_s = p.idle_sleep;
            self.sensor_rest = p.movement_saving > 0;
            self.high_efficiency = p.power_saving > 0;
        }
        if let Some(d) = config.debounce {
            self.debounce = d;
        }
    }

    fn poll_replies(&mut self) {
        while let Ok((tag, result)) = self.worker.replies.try_recv() {
            self.worker.pending = self.worker.pending.saturating_sub(1);
            match (tag.as_str(), result) {
                (_, Ok(Reply::Status(s))) => {
                    let mut snap = Snapshot::from_status(&s);
                    if snap.battery.is_none() && self.snap.battery.is_some() {
                        snap.battery = self.snap.battery;
                        snap.stale = true;
                        snap.seen_at = self.snap.seen_at;
                    }
                    let became_linked =
                        snap.link == Link::Connected && self.snap.link != Link::Connected;
                    self.snap = snap;
                    if became_linked && self.config.is_none() {
                        self.load_config();
                    }
                }
                (_, Ok(Reply::Config(c))) => {
                    self.init_edits(&c);
                    self.config = Some(*c);
                }
                (_, Ok(Reply::Done(m))) => {
                    self.say(m, false);
                    self.load_config();
                    self.refresh_status();
                }
                ("status", Err(e)) => {
                    let mut snap = Snapshot::from_error(&e);
                    if matches!(e, Error::Busy) {
                        continue;
                    }
                    snap.battery = self.snap.battery;
                    snap.stale = snap.battery.is_some();
                    snap.seen_at = self.snap.seen_at;
                    self.snap = snap;
                }
                ("config", Err(_)) => {}
                (_, Err(e)) => self.say(format!("Failed: {e}"), true),
            }
        }
        while let Ok(n) = self.net_rx.try_recv() {
            match n {
                Net::Checked(gg, app) => {
                    let stamp = Settings::update(|s| s.last_update_check = super::settings::now());
                    self.settings = stamp;
                    self.online = match &gg {
                        None => "No newer SteelSeries GG release, so no new firmware.".into(),
                        Some(r) => format!(
                            "SteelSeries GG {} is out and may contain new firmware.",
                            r.version
                        ),
                    };
                    if gg.is_none() && app.is_none() {
                        self.say("Everything is up to date", false);
                    }
                    self.gg_release = gg;
                    self.app_release = app;
                }
                Net::Progress(done, total) => self.download = Some((done, total)),
                Net::Downloaded(p) => {
                    self.download = None;
                    self.gg_release = None;
                    self.say(format!("Firmware {} downloaded", p.version), false);
                    self.package = Some(p);
                }
                Net::Failed(e) => {
                    self.download = None;
                    self.say(e, true);
                }
            }
        }
    }

    fn check_updates(&mut self) {
        let tx = self.net_tx.clone();
        let ctx = self.ctx.clone();
        self.online = "Checking\u{2026}".into();
        std::thread::spawn(move || {
            let reply = match updates::check_gg_release(None) {
                Ok(gg) => Net::Checked(gg, updates::check_app_release().ok().flatten()),
                Err(e) => Net::Failed(format!("Update check failed: {e}")),
            };
            let _ = tx.send(reply);
            ctx.request_repaint();
        });
    }

    fn download_firmware(&mut self) {
        let tx = self.net_tx.clone();
        let ctx = self.ctx.clone();
        self.download = Some((0, 0));
        std::thread::spawn(move || {
            let progress_tx = tx.clone();
            let progress_ctx = ctx.clone();
            let mut last = 0;
            let result = updates::fetch_firmware(None, |done, total| {
                if done - last > 1 << 20 || done == total {
                    last = done;
                    let _ = progress_tx.send(Net::Progress(done, total));
                    progress_ctx.request_repaint();
                }
            });
            let _ = tx.send(match result {
                Ok(Some((_, package))) => Net::Downloaded(package),
                Ok(None) => Net::Failed("No newer SteelSeries GG release to download".into()),
                Err(e) => Net::Failed(format!("Firmware download failed: {e}")),
            });
            ctx.request_repaint();
        });
    }
}

impl eframe::App for App {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll_replies();
        if Instant::now() >= self.next_status && self.worker.pending == 0 {
            self.refresh_status();
        }
        ctx.request_repaint_after(Duration::from_millis(500));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        egui::CentralPanel::default().show(ui, |ui| {
            self.header(ui);
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                for (tab, name) in [
                    (Tab::Status, "Status"),
                    (Tab::Performance, "Performance"),
                    (Tab::Lighting, "Lighting"),
                    (Tab::Buttons, "Buttons"),
                    (Tab::Power, "Power"),
                    (Tab::Device, "Device"),
                ] {
                    ui.selectable_value(&mut self.tab, tab, name);
                }
            });
            ui.separator();
            egui::ScrollArea::vertical()
                .auto_shrink([false, true])
                .max_height(ui.available_height() - 28.0)
                .show(ui, |ui| match self.tab {
                    Tab::Status => self.status_tab(ui),
                    Tab::Performance => self.performance_tab(ui),
                    Tab::Lighting => self.lighting_tab(ui),
                    Tab::Buttons => self.buttons_tab(ui),
                    Tab::Power => self.power_tab(ui),
                    Tab::Device => self.device_tab(ui),
                });
            ui.separator();
            if let Some((text, error)) = &self.message {
                let color = if *error {
                    Color32::from_rgb(0xda, 0x36, 0x33)
                } else {
                    ui.visuals().text_color()
                };
                ui.label(RichText::new(text).color(color));
            }
        });
        if let Some(c) = self.confirm {
            self.confirm_dialog(ui.ctx(), c);
        }
    }
}

fn chip(ui: &mut egui::Ui, text: &str, fill: Color32) {
    egui::Frame::new()
        .fill(fill)
        .corner_radius(9.0)
        .inner_margin(egui::Margin::symmetric(10, 3))
        .show(ui, |ui| {
            ui.label(RichText::new(text).color(Color32::WHITE).strong());
        });
}

fn link_color(link: Link) -> Color32 {
    match link {
        Link::Connected => Color32::from_rgb(0x2e, 0xa0, 0x43),
        Link::Asleep => Color32::from_rgb(0xd2, 0x99, 0x22),
        Link::NoReceiver => Color32::from_rgb(0x6e, 0x77, 0x81),
        _ => Color32::from_rgb(0xda, 0x36, 0x33),
    }
}

impl App {
    fn header(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading(RichText::new(&self.snap.name).strong());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let text = self.snap.link.text().split(" - ").next().unwrap_or("");
                chip(ui, text, link_color(self.snap.link));
            });
        });
    }

    fn battery_gauge(&self, ui: &mut egui::Ui) {
        let width = ui.available_width().min(420.0);
        let (rect, _) = ui.allocate_exact_size(egui::vec2(width, 90.0), egui::Sense::hover());
        let painter = ui.painter_at(rect);
        let body = egui::Rect::from_min_max(
            rect.min + egui::vec2(4.0, 6.0),
            rect.max - egui::vec2(16.0, 6.0),
        );
        let fg = ui.visuals().text_color().gamma_multiply(0.6);
        painter.rect_stroke(
            body,
            12.0,
            egui::Stroke::new(3.0, fg),
            egui::StrokeKind::Inside,
        );
        let nub = egui::Rect::from_center_size(
            egui::pos2(body.max.x + 5.0, body.center().y),
            egui::vec2(8.0, body.height() * 0.36),
        );
        painter.rect_filled(nub, 3.0, fg);
        let [r, g, b] = icon::tier_color(self.snap.battery, self.snap.stale);
        if let Some(level) = self.snap.battery {
            let inner = body.shrink(8.0);
            let fill = egui::Rect::from_min_size(
                inner.min,
                egui::vec2(
                    inner.width() * f32::from(level.min(100)) / 100.0,
                    inner.height(),
                ),
            );
            painter.rect_filled(fill, 7.0, Color32::from_rgb(r, g, b));
        }
        let text = self
            .snap
            .battery
            .map(|b| format!("{b}%"))
            .unwrap_or_else(|| "--".into());
        let font = egui::FontId::proportional(body.height() * 0.45);
        painter.text(
            body.center() + egui::vec2(1.5, 1.5),
            egui::Align2::CENTER_CENTER,
            &text,
            font.clone(),
            Color32::from_black_alpha(140),
        );
        painter.text(
            body.center(),
            egui::Align2::CENTER_CENTER,
            &text,
            font,
            Color32::WHITE,
        );
    }

    fn status_tab(&mut self, ui: &mut egui::Ui) {
        ui.vertical_centered(|ui| self.battery_gauge(ui));
        let note = if self.snap.link == Link::Connected && !self.snap.stale {
            "Live".to_string()
        } else if self.snap.battery.is_some() {
            format!(
                "{}. Last reading {}.",
                self.snap.link.text(),
                super::monitor::describe_age(self.snap.seen_at, super::settings::now())
            )
        } else {
            self.snap.link.text().to_string()
        };
        ui.vertical_centered(|ui| ui.label(note));
        if self.snap.link == Link::NoPermission {
            ui.label("Install the udev rule (see README) and replug the receiver.");
        }
        ui.add_space(8.0);
        ui.strong("DPI presets");
        ui.horizontal_wrapped(|ui| {
            for (i, cpi) in self.snap.presets.iter().enumerate() {
                let active = self.snap.active == Some(i as u8 + 1);
                if active {
                    chip(ui, &cpi.to_string(), ui.visuals().selection.bg_fill);
                } else {
                    ui.label(format!(" {cpi} "));
                }
            }
            if self.snap.presets.is_empty() {
                ui.label("-");
            }
        });
        ui.add_space(6.0);
        egui::Grid::new("details")
            .num_columns(2)
            .spacing([16.0, 6.0])
            .show(ui, |ui| {
                ui.label("Polling rate:");
                ui.label(
                    self.snap
                        .polling_hz
                        .map(|h| format!("{h} Hz"))
                        .unwrap_or_else(|| "-".into()),
                );
                ui.end_row();
                ui.label("Receiver firmware:");
                ui.label(self.snap.firmware.clone().unwrap_or_else(|| "-".into()));
                ui.end_row();
            });
        ui.add_space(10.0);
        ui.separator();
        ui.horizontal(|ui| {
            ui.label("Tray fallback refresh:");
            let current = INTERVALS
                .iter()
                .find(|(s, _)| *s == self.settings.interval_s)
                .map(|(_, l)| *l)
                .unwrap_or("15 seconds");
            egui::ComboBox::from_id_salt("interval")
                .selected_text(current)
                .show_ui(ui, |ui| {
                    for (secs, label) in INTERVALS {
                        if ui
                            .selectable_label(self.settings.interval_s == secs, label)
                            .clicked()
                        {
                            self.settings = Settings::update(|s| s.interval_s = secs);
                        }
                    }
                });
        });
        ui.label(
            RichText::new(
                "Link, DPI and battery changes show up instantly; this is only a safety net.",
            )
            .small()
            .weak(),
        );
        if ui.checkbox(&mut self.autostart, "Start on login").changed()
            && let Err(e) = autostart::set_enabled(self.autostart)
        {
            self.say(format!("Could not change autostart: {e}"), true);
        }
    }

    fn footer(&mut self, ui: &mut egui::Ui, hint: &str) -> bool {
        ui.add_space(8.0);
        ui.separator();
        let mut apply = false;
        ui.horizontal(|ui| {
            ui.label(RichText::new(hint).small().weak());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let enabled = self.linked() && self.worker.pending == 0;
                apply = ui
                    .add_enabled(enabled, egui::Button::new("Apply"))
                    .on_disabled_hover_text("Wake the mouse first")
                    .clicked();
            });
        });
        apply
    }

    fn performance_tab(&mut self, ui: &mut egui::Ui) {
        ui.strong("DPI presets (the DPI button cycles through them)");
        ui.horizontal(|ui| {
            ui.label("Number of presets:");
            ui.add(egui::DragValue::new(&mut self.dpi_count).range(1..=5));
        });
        self.dpi_active = self.dpi_active.clamp(1, self.dpi_count as u8);
        egui::Grid::new("dpi")
            .num_columns(3)
            .spacing([12.0, 6.0])
            .show(ui, |ui| {
                for i in 0..self.dpi_count {
                    ui.label(format!("Preset {}:", i + 1));
                    ui.add(
                        egui::DragValue::new(&mut self.dpi[i])
                            .range(100..=18000)
                            .speed(10.0)
                            .suffix(" CPI"),
                    );
                    ui.radio_value(&mut self.dpi_active, i as u8 + 1, "active");
                    ui.end_row();
                }
            });
        ui.add_space(8.0);
        ui.strong("Polling rate");
        ui.horizontal(|ui| {
            for hz in [1000, 500, 250, 125] {
                ui.radio_value(&mut self.polling, hz, format!("{hz} Hz"));
            }
        });
        if self.footer(ui, "Values snap to the sensor's 100 CPI steps.") {
            let presets: Vec<u16> = self.dpi[..self.dpi_count]
                .iter()
                .map(|c| proto::snap_cpi(*c))
                .collect();
            let (active, hz) = (self.dpi_active, self.polling);
            let high_efficiency = self.profile.power.high_efficiency == Some(true);
            self.profile.dpi = Some(profile::Dpi {
                presets: presets.clone(),
                active,
            });
            self.profile.polling_hz = Some(hz);
            self.apply("Saving DPI and polling rate", move |mouse| {
                let used = mouse.set_dpi(&presets, active)?;
                let mut message = format!(
                    "DPI {} (active {} CPI)",
                    used.iter()
                        .map(u16::to_string)
                        .collect::<Vec<_>>()
                        .join("/"),
                    used[active as usize - 1]
                );
                if !high_efficiency && mouse.polling_rate() != Some(hz) {
                    mouse.set_polling_rate(hz)?;
                    message.push_str(&format!(", {hz} Hz"));
                }
                Ok(message)
            });
        }
    }

    fn lighting_tab(&mut self, ui: &mut egui::Ui) {
        ui.strong("Scroll-wheel LED");
        egui::ComboBox::from_id_salt("mode")
            .selected_text(self.effect.mode.label())
            .show_ui(ui, |ui| {
                for mode in Mode::ALL {
                    ui.selectable_value(&mut self.effect.mode, mode, mode.label());
                }
            });
        let mode = self.effect.mode;
        if mode != Mode::Off {
            let max = mode.max_colors().max(1);
            if self.effect.colors.is_empty() {
                self.effect.colors.push(DEFAULT_COLOR);
            }
            ui.horizontal_wrapped(|ui| {
                ui.label(if mode == Mode::Reactive {
                    "Base colour:"
                } else {
                    "Colours:"
                });
                let shown = self.effect.colors.len().min(max);
                for color in self.effect.colors.iter_mut().take(shown) {
                    ui.color_edit_button_srgb(color);
                }
                if max > 1 {
                    if ui
                        .add_enabled(self.effect.colors.len() < max, egui::Button::new("+"))
                        .clicked()
                    {
                        self.effect.colors.push(DEFAULT_COLOR);
                    }
                    if ui
                        .add_enabled(self.effect.colors.len() > 1, egui::Button::new("\u{2212}"))
                        .clicked()
                    {
                        self.effect.colors.pop();
                    }
                }
            });
        }
        if matches!(mode, Mode::Colorshift | Mode::Breathe) {
            let mut secs = self.effect.duration_ms as f32 / 1000.0;
            ui.add(egui::Slider::new(&mut secs, 0.5..=30.0).text("cycle length (s)"));
            self.effect.duration_ms = (secs * 1000.0) as u32;
        }
        if mode == Mode::Reactive {
            ui.horizontal(|ui| {
                ui.label("Flash colour:");
                ui.color_edit_button_srgb(&mut self.effect.react_color);
            });
            ui.add(egui::Slider::new(&mut self.effect.react_ms, 50..=5000).text("fade back (ms)"));
        }
        if self.footer(
            ui,
            "The mouse cannot report its lighting; this is the last effect ssmouse applied.",
        ) {
            let mut effect = self.effect.clone();
            if matches!(effect.mode, Mode::Steady | Mode::Reactive) {
                effect.colors.truncate(1);
            }
            if let Err(e) = effect.validate() {
                self.say(e.to_string(), true);
                return;
            }
            self.profile.lighting = Some(effect.clone());
            self.apply("Saving lighting", move |mouse| {
                mouse.set_lighting(&effect)?;
                Ok(format!("Lighting: {}", effect.mode.label()))
            });
        }
    }

    fn buttons_tab(&mut self, ui: &mut egui::Ui) {
        ui.strong("Button assignments");
        let recorded = ui.ctx().input(|i| {
            i.events.iter().find_map(|e| match e {
                egui::Event::Key {
                    key,
                    pressed: true,
                    modifiers,
                    ..
                } => Some((*key, *modifiers)),
                _ => None,
            })
        });
        egui::Grid::new("binds")
            .num_columns(2)
            .spacing([12.0, 6.0])
            .show(ui, |ui| {
                for edit in &mut self.binds {
                    ui.label(format!("{}:", edit.label));
                    ui.horizontal(|ui| {
                        let current = if edit.spec.starts_with("key:") {
                            "Keyboard shortcut".to_string()
                        } else {
                            bindings::describe(&edit.spec)
                        };
                        egui::ComboBox::from_id_salt(edit.control)
                            .width(170.0)
                            .selected_text(current)
                            .show_ui(ui, |ui| {
                                for (spec, _, label) in bindings::SIMPLE {
                                    ui.selectable_value(&mut edit.spec, spec.to_string(), label);
                                }
                                ui.separator();
                                for (name, _, label) in bindings::MEDIA {
                                    ui.selectable_value(
                                        &mut edit.spec,
                                        format!("media:{name}"),
                                        format!("Media: {label}"),
                                    );
                                }
                                ui.separator();
                                if ui
                                    .selectable_label(
                                        edit.spec.starts_with("key:"),
                                        "Keyboard shortcut\u{2026}",
                                    )
                                    .clicked()
                                {
                                    edit.spec = "key:".into();
                                    edit.recording = true;
                                }
                            });
                        if edit.spec.starts_with("key:") {
                            let text = if edit.recording {
                                "Press a shortcut\u{2026}".to_string()
                            } else if edit.spec == "key:" {
                                "Click to record".to_string()
                            } else {
                                bindings::describe(&edit.spec)
                                    .trim_start_matches("Keys: ")
                                    .to_string()
                            };
                            if ui.button(text).clicked() {
                                edit.recording = true;
                            }
                            if edit.recording
                                && let Some((key, mods)) = recorded
                                && let Some(spec) = key_to_spec(key, mods)
                            {
                                edit.spec = spec;
                                edit.recording = false;
                            }
                        }
                    });
                    ui.end_row();
                }
            });
        if ui.button("Restore defaults").clicked() {
            for edit in &mut self.binds {
                edit.spec = bindings::default_for(edit.control)
                    .unwrap_or("disabled")
                    .to_string();
                edit.recording = false;
            }
        }
        if self.footer(
            ui,
            "Changes are stored in the mouse, so they work on any computer.",
        ) {
            let map: bindings::Bindings = self
                .binds
                .iter()
                .map(|b| (b.control.to_string(), b.spec.clone()))
                .collect();
            if let Some(bad) = map.values().find(|s| bindings::encode(s).is_err()) {
                self.say(
                    format!("Invalid assignment {bad:?}: record a shortcut first"),
                    true,
                );
                return;
            }
            if !map.values().any(|s| s == "left") {
                self.say("Keep at least one control on \"Left click\", or you will not be able to click.", true);
                return;
            }
            self.profile.bindings = map.clone();
            self.profile.normalize();
            self.apply("Saving button assignments", move |mouse| {
                mouse.set_bindings(&map)?;
                Ok("Button assignments saved".into())
            });
        }
    }

    fn power_tab(&mut self, ui: &mut egui::Ui) {
        ui.strong("Battery");
        ui.horizontal(|ui| {
            ui.label("Sleep after idle:");
            egui::ComboBox::from_id_salt("sleep")
                .selected_text(format!("{} min", self.sleep_s / 60))
                .show_ui(ui, |ui| {
                    for s in proto::sleep_timer_choices() {
                        ui.selectable_value(&mut self.sleep_s, s, format!("{} min", s / 60));
                    }
                });
        });
        ui.checkbox(
            &mut self.sensor_rest,
            "Sensor rest: lower sensor power while the mouse is still",
        );
        ui.checkbox(
            &mut self.high_efficiency,
            "High-efficiency mode: 125 Hz polling for maximum battery life",
        );
        ui.add_space(8.0);
        ui.strong("Button debounce (advanced)");
        ui.horizontal(|ui| {
            ui.label("Press:");
            ui.add(
                egui::DragValue::new(&mut self.debounce.0)
                    .range(1..=100)
                    .suffix(" ms"),
            );
            ui.label("Release:");
            ui.add(
                egui::DragValue::new(&mut self.debounce.1)
                    .range(1..=100)
                    .suffix(" ms"),
            );
        });
        if self.footer(
            ui,
            "Factory values: 5 min, sensor rest on, debounce 6/20 ms.",
        ) {
            let power = profile::Power {
                sleep_s: Some(self.sleep_s),
                sensor_rest: Some(self.sensor_rest),
                high_efficiency: Some(self.high_efficiency),
            };
            let debounce = self.debounce;
            let polling = self.profile.polling_hz.unwrap_or(1000);
            self.profile.power = power.clone();
            self.profile.debounce = Some(profile::Debounce {
                make_ms: debounce.0,
                break_ms: debounce.1,
            });
            self.apply("Saving power settings", move |mouse| {
                profile::apply_power(mouse, &power)?;
                let target = if power.high_efficiency == Some(true) {
                    profile::HIGH_EFFICIENCY_HZ
                } else {
                    polling
                };
                if mouse.polling_rate() != Some(target) {
                    mouse.set_polling_rate(target)?;
                }
                if mouse.debounce() != Some(debounce) {
                    mouse.set_debounce(debounce.0, debounce.1)?;
                }
                Ok("Power settings saved".into())
            });
        }
    }

    fn device_tab(&mut self, ui: &mut egui::Ui) {
        let linked = self.linked() && self.worker.pending == 0;
        ui.strong("Wireless");
        ui.label("Pair after replacing the receiver, or if the wheel keeps blinking white with fresh batteries.");
        ui.horizontal(|ui| {
            if ui.button("Pair mouse with receiver\u{2026}").clicked() {
                self.confirm = Some(Confirm::Pair);
            }
            if ui
                .button("Restart receiver")
                .on_hover_text("Like unplugging the receiver; fixes a receiver that lost the mouse")
                .clicked()
            {
                self.say("Restarting the receiver\u{2026}", false);
                self.worker.run(
                    "apply",
                    Box::new(|mouse| {
                        mouse.reset_receiver()?;
                        Ok(Reply::Done("Receiver restarted".into()))
                    }),
                );
            }
        });
        ui.add_space(8.0);
        ui.strong("Profile");
        ui.horizontal(|ui| {
            ui.label("File:");
            ui.add(egui::TextEdit::singleline(&mut self.profile_path).desired_width(280.0));
        });
        ui.horizontal(|ui| {
            if ui
                .add_enabled(linked, egui::Button::new("Write saved profile to mouse"))
                .clicked()
            {
                let p = self.profile.clone();
                self.apply("Writing saved profile", move |mouse| {
                    let applied = profile::apply(mouse, &p, &profile::ALL_SECTIONS, false)?;
                    Ok(format!(
                        "Applied: {}",
                        if applied.is_empty() {
                            "nothing configured".into()
                        } else {
                            applied.join(", ")
                        }
                    ))
                });
            }
            if ui.button("Export").clicked() {
                let path = PathBuf::from(&self.profile_path);
                match self.profile.save_to(&path) {
                    Ok(()) => self.say(format!("Exported to {}", path.display()), false),
                    Err(e) => self.say(format!("Export failed: {e}"), true),
                }
            }
            if ui
                .add_enabled(linked, egui::Button::new("Import"))
                .clicked()
            {
                match Profile::load_from(&PathBuf::from(&self.profile_path)) {
                    Ok(p) => {
                        self.profile = p.clone();
                        self.effect = p.lighting.clone().unwrap_or_default();
                        for edit in &mut self.binds {
                            edit.spec =
                                p.bindings.get(edit.control).cloned().unwrap_or_else(|| {
                                    bindings::default_for(edit.control).unwrap().into()
                                });
                        }
                        self.apply("Applying imported profile", move |mouse| {
                            let applied = profile::apply(mouse, &p, &profile::ALL_SECTIONS, false)?;
                            Ok(format!("Imported: {}", applied.join(", ")))
                        });
                    }
                    Err(e) => self.say(format!("Could not import: {e}"), true),
                }
            }
        });
        ui.add_space(8.0);
        ui.strong("Factory reset");
        if ui
            .add_enabled(
                linked,
                egui::Button::new("Restore factory settings\u{2026}"),
            )
            .clicked()
        {
            self.confirm = Some(Confirm::FactoryReset);
        }
        ui.add_space(8.0);
        ui.strong("Firmware and updates");
        let installed = self
            .config
            .as_ref()
            .map(|c| c.firmware.clone())
            .unwrap_or_default();
        let newest = self
            .package
            .as_ref()
            .map(|p| p.version.clone())
            .unwrap_or_else(|| firmware::LATEST_KNOWN.into());
        let outdated = installed.up_to_date(&newest) == Some(false);
        ui.label(format!(
            "Installed: mouse {}, receiver {}. Newest available: {newest} \u{2014} {}.",
            installed.mouse.as_deref().unwrap_or("?"),
            installed.receiver.as_deref().unwrap_or("?"),
            if outdated {
                "update available"
            } else {
                "up to date"
            }
        ));
        ui.label(&self.online);
        if let Some(app) = &self.app_release {
            ui.hyperlink_to(format!("ssmouse {} is available", app.version), &app.url);
        }
        if let Some((done, total)) = self.download {
            let fraction = if total > 0 {
                done as f32 / total as f32
            } else {
                0.0
            };
            ui.add(egui::ProgressBar::new(fraction).text(format!(
                "Downloading SteelSeries GG: {} of {} MB",
                done >> 20,
                total >> 20
            )));
        }
        ui.horizontal(|ui| {
            if ui.add_enabled(self.download.is_none(), egui::Button::new("Check for updates")).clicked() {
                self.check_updates();
            }
            if self.gg_release.is_some() && ui.add_enabled(self.download.is_none(), egui::Button::new("Download firmware"))
                .on_hover_text("Downloads the SteelSeries GG installer (about 430 MB) and keeps only the firmware").clicked() {
                self.download_firmware();
            }
            if outdated && self.package.is_some() && ui.add_enabled(linked, egui::Button::new(format!("Install firmware {newest}"))).clicked() {
                self.confirm = Some(Confirm::Firmware);
            }
        });
        ui.horizontal(|ui| {
            ui.label("Or a GG firmware folder:");
            ui.add(
                egui::TextEdit::singleline(&mut self.firmware_folder)
                    .hint_text("\u{2026}/apps/engine/firmware/272111664")
                    .desired_width(240.0),
            );
            if ui
                .add_enabled(
                    linked && !self.firmware_folder.is_empty(),
                    egui::Button::new("Load"),
                )
                .clicked()
            {
                match firmware::load_package(&PathBuf::from(&self.firmware_folder)) {
                    Ok(p) => {
                        self.say(format!("Loaded firmware {}", p.version), false);
                        self.package = Some(p);
                        self.confirm = Some(Confirm::Firmware);
                    }
                    Err(e) => self.say(e.to_string(), true),
                }
            }
        });
        let mut auto = self.settings.auto_update_check;
        if ui
            .checkbox(&mut auto, "Check for firmware and ssmouse updates daily")
            .changed()
        {
            self.settings = Settings::update(|s| s.auto_update_check = auto);
        }
    }

    fn confirm_dialog(&mut self, ctx: &egui::Context, which: Confirm) {
        let (title, text, button) = match which {
            Confirm::FactoryReset => ("Restore factory settings", "Reset DPI, polling rate, power, lighting and buttons to factory defaults?".to_string(), "Reset"),
            Confirm::Pair => ("Pair mouse", "After you press Start, switch the mouse off and on again in 2.4 GHz mode within 20 seconds.".to_string(), "Start"),
            Confirm::Firmware => ("Install firmware",
                format!("Install firmware {}? This takes about a minute. Do not switch the mouse off or unplug the receiver until it finishes; an interrupted update can make them unusable.",
                        self.package.as_ref().map(|p| p.version.as_str()).unwrap_or("?")), "Install"),
        };
        let mut close = false;
        let mut go = false;
        egui::Window::new(title)
            .collapsible(false)
            .resizable(false)
            .anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
            .show(ctx, |ui| {
                ui.label(text);
                ui.horizontal(|ui| {
                    go = ui.button(button).clicked();
                    close = ui.button("Cancel").clicked();
                });
            });
        if close {
            self.confirm = None;
        }
        if go {
            self.confirm = None;
            match which {
                Confirm::FactoryReset => {
                    self.profile = Profile::default();
                    self.effect = Effect::default();
                    for edit in &mut self.binds {
                        edit.spec = bindings::default_for(edit.control).unwrap().into();
                    }
                    self.apply("Restoring factory settings", |mouse| {
                        mouse.factory_reset()?;
                        Ok("Factory settings restored".into())
                    });
                }
                Confirm::Pair => {
                    self.say("Pairing: switch the mouse off and on now\u{2026}", false);
                    self.worker.run(
                        "apply",
                        Box::new(|mouse| {
                            Ok(Reply::Done(if mouse.pair()? {
                                "Mouse paired".into()
                            } else {
                                "Pairing timed out".into()
                            }))
                        }),
                    );
                }
                Confirm::Firmware => {
                    if let Some(package) = self.package.clone() {
                        self.say(
                            format!(
                                "Installing firmware {} \u{2014} do not switch off the mouse",
                                package.version
                            ),
                            false,
                        );
                        self.worker.run(
                            "apply",
                            Box::new(move |mouse| {
                                firmware::update(mouse, &package, false, |_| {})?;
                                Ok(Reply::Done(format!(
                                    "Firmware {} installed; the mouse and receiver restart now",
                                    package.version
                                )))
                            }),
                        );
                    }
                }
            }
        }
    }
}

/// egui key + modifiers -> `key:ctrl+shift+t` (None for keys the mouse cannot send).
pub fn key_to_spec(key: egui::Key, mods: egui::Modifiers) -> Option<String> {
    use egui::Key as K;
    let name = match key {
        K::Enter => "enter",
        K::Escape => "esc",
        K::Backspace => "backspace",
        K::Tab => "tab",
        K::Space => "space",
        K::Minus => "minus",
        K::Equals => "equal",
        K::OpenBracket => "leftbrace",
        K::CloseBracket => "rightbrace",
        K::Backslash => "backslash",
        K::Semicolon => "semicolon",
        K::Quote => "apostrophe",
        K::Backtick => "grave",
        K::Comma => "comma",
        K::Period => "dot",
        K::Slash => "slash",
        K::Insert => "insert",
        K::Delete => "delete",
        K::Home => "home",
        K::End => "end",
        K::PageUp => "pageup",
        K::PageDown => "pagedown",
        K::ArrowRight => "right",
        K::ArrowLeft => "left",
        K::ArrowDown => "down",
        K::ArrowUp => "up",
        other => {
            let n = other.name();
            return single_key(n).map(|k| combine(mods, &k));
        }
    };
    Some(combine(mods, name))
}

fn single_key(name: &str) -> Option<String> {
    let lower = name.to_lowercase();
    let ok = (lower.len() == 1 && lower.chars().all(|c| c.is_ascii_alphanumeric()))
        || (lower.starts_with('f')
            && lower[1..]
                .parse::<u8>()
                .is_ok_and(|n| (1..=24).contains(&n)));
    ok.then_some(lower)
}

fn combine(mods: egui::Modifiers, key: &str) -> String {
    let mut parts = Vec::new();
    // `command` is Cmd on macOS and Ctrl elsewhere; map Cmd to super.
    if mods.ctrl || (mods.command && !cfg!(target_os = "macos")) {
        parts.push("ctrl");
    }
    if mods.shift {
        parts.push("shift");
    }
    if mods.alt {
        parts.push("alt");
    }
    if mods.mac_cmd {
        parts.push("super");
    }
    parts.push(key);
    format!("key:{}", parts.join("+"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_capture() {
        let shift = egui::Modifiers {
            shift: true,
            ..Default::default()
        };
        assert_eq!(
            key_to_spec(egui::Key::F13, shift).as_deref(),
            Some("key:shift+f13")
        );
        let ctrl = egui::Modifiers {
            ctrl: true,
            command: !cfg!(target_os = "macos"),
            ..Default::default()
        };
        assert_eq!(
            key_to_spec(egui::Key::Z, ctrl).as_deref(),
            Some("key:ctrl+z")
        );
        assert_eq!(
            key_to_spec(egui::Key::PageDown, egui::Modifiers::ALT).as_deref(),
            Some("key:alt+pagedown")
        );
        for (_, _, _) in bindings::controls() {}
        assert!(
            bindings::encode(&key_to_spec(egui::Key::Num0, egui::Modifiers::NONE).unwrap()).is_ok()
        );
    }
}
