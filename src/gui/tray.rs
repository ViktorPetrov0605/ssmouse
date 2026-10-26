//! The tray daemon: battery badge, notifications, receiver events, update checks.
//!
//! The controller below is toolkit-free and runs on its own thread; small
//! adapters show its [`TrayView`] with ksni (Linux, StatusNotifierItem over
//! D-Bus, no GTK) or tray-icon + tao (Windows, macOS).  The settings window
//! is a separate process (`ssmouse-gui --window`).

use std::process::{Child, Command};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

use crate::events::{self, Event};
use crate::{autostart, updates};

use super::monitor::{self, Link, Snapshot, Tracker};
use super::settings::{Settings, now};

pub enum Cmd {
    OpenWindow,
    Refresh,
    ToggleAutostart,
    Quit,
    Receiver(Event),
    UpdatesChecked(Option<updates::GgRelease>, Option<updates::AppRelease>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct TrayView {
    pub level: Option<u8>,
    pub stale: bool,
    pub title: String,
    pub tooltip: String,
    pub autostart: bool,
}

impl TrayView {
    fn of(snap: &Snapshot) -> Self {
        let level = snap
            .battery
            .map(|b| format!("{b}%"))
            .unwrap_or_else(|| "--".into());
        let mut tooltip = format!("Battery: {level}");
        if let Some(cpi) = snap.current_cpi {
            tooltip.push_str(&format!("\nDPI: {cpi}"));
        }
        if snap.link != Link::Connected {
            tooltip.push('\n');
            tooltip.push_str(snap.link.text());
            if snap.stale {
                tooltip.push_str(&format!(
                    " (last reading {})",
                    monitor::describe_age(snap.seen_at, now())
                ));
            }
        }
        Self {
            level: snap.battery,
            stale: snap.stale,
            title: snap.name.clone(),
            tooltip,
            autostart: autostart::is_enabled(),
        }
    }
}

pub fn notify(summary: &str, body: &str) {
    let _ = notify_rust::Notification::new()
        .appname("ssmouse")
        .summary(summary)
        .body(body)
        .icon("input-mouse")
        .show();
}

/// Starts the settings window process unless one is already open.
struct WindowLauncher(Option<Child>);

impl WindowLauncher {
    fn open(&mut self) {
        if let Some(child) = &mut self.0
            && matches!(child.try_wait(), Ok(None))
        {
            return; // already open
        }
        self.0 = std::env::current_exe()
            .ok()
            .and_then(|exe| Command::new(exe).arg("--window").spawn().ok());
    }
}

const UPDATE_CHECK_EVERY: u64 = 24 * 3600;

/// The controller loop; `show` pushes a new view to the tray toolkit.
pub fn run_controller(
    rx: Receiver<Cmd>,
    tx: Sender<Cmd>,
    mut show: impl FnMut(TrayView),
    open_window_now: bool,
) {
    let settings = Settings::load();
    let mut tracker = Tracker::new(settings.last_battery, settings.last_seen);
    let mut launcher = WindowLauncher(None);
    if open_window_now {
        launcher.open();
    }
    let mut next_poll = Instant::now();
    let mut next_update_check = Instant::now() + Duration::from_secs(30);
    let mut last_view: Option<TrayView> = None;
    if tracker.last_battery.is_some() {
        let (snap, _) = tracker.update(Snapshot::new(Link::Asleep));
        publish(&mut last_view, &snap, &mut show);
    }

    loop {
        let wait = next_poll
            .saturating_duration_since(Instant::now())
            .min(next_update_check.saturating_duration_since(Instant::now()));
        let cmd = match rx.recv_timeout(wait) {
            Ok(cmd) => Some(cmd),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => return,
        };
        match cmd {
            Some(Cmd::Quit) => return,
            Some(Cmd::OpenWindow) => launcher.open(),
            Some(Cmd::Refresh) => next_poll = Instant::now(),
            Some(Cmd::ToggleAutostart) => {
                let _ = autostart::set_enabled(!autostart::is_enabled());
                if let Some(mut view) = last_view.clone() {
                    view.autostart = autostart::is_enabled();
                    last_view = Some(view.clone());
                    show(view);
                }
            }
            Some(Cmd::Receiver(Event::Battery(level))) => {
                let snap = Snapshot {
                    battery: Some(level),
                    seen_at: Some(now()),
                    ..Snapshot::new(Link::Connected)
                };
                let (merged, alert) = tracker.update(Snapshot {
                    name: last_view
                        .as_ref()
                        .map(|v| v.title.clone())
                        .unwrap_or(snap.name.clone()),
                    ..snap
                });
                publish(&mut last_view, &merged, &mut show);
                remember(&tracker);
                if let Some(threshold) = alert {
                    low_battery(level, threshold);
                }
            }
            // The link flaps briefly while the mouse (re)connects; settle first.
            Some(Cmd::Receiver(Event::Link(up))) => {
                next_poll = Instant::now() + Duration::from_millis(if up { 1500 } else { 800 })
            }
            Some(Cmd::Receiver(Event::DpiLevel(_))) => {
                next_poll = Instant::now() + Duration::from_millis(500)
            }
            Some(Cmd::UpdatesChecked(gg, app)) => {
                Settings::update(|s| s.last_update_check = now());
                let settings = Settings::load();
                if let Some(r) = gg.filter(|r| r.version != settings.notified_gg) {
                    Settings::update(|s| s.notified_gg = r.version.clone());
                    notify(
                        "New mouse firmware may be available",
                        &format!(
                            "SteelSeries GG {} is out. Open ssmouse \u{2192} Device to download it.",
                            r.version
                        ),
                    );
                }
                if let Some(r) = app.filter(|r| r.version != settings.notified_app) {
                    Settings::update(|s| s.notified_app = r.version.clone());
                    notify(
                        "ssmouse update",
                        &format!("ssmouse {} is available: {}", r.version, r.url),
                    );
                }
            }
            None => {}
        }
        if Instant::now() >= next_update_check {
            next_update_check = Instant::now() + Duration::from_secs(3600);
            let s = Settings::load();
            if s.auto_update_check
                && now().saturating_sub(s.last_update_check) >= UPDATE_CHECK_EVERY
                && !crate::fake::requested()
            {
                let tx = tx.clone();
                std::thread::spawn(move || {
                    let gg = updates::check_gg_release(None).ok().flatten();
                    let app = updates::check_app_release().ok().flatten();
                    let _ = tx.send(Cmd::UpdatesChecked(gg, app));
                });
            }
        }
        if Instant::now() >= next_poll {
            let snap = monitor::poll_once();
            if snap.detail.as_deref() == Some("busy") {
                next_poll = Instant::now() + Duration::from_secs(2);
                continue;
            }
            let link = snap.link;
            let (merged, alert) = tracker.update(snap);
            publish(&mut last_view, &merged, &mut show);
            remember(&tracker);
            if let (Some(threshold), Some(level)) = (alert, merged.battery) {
                low_battery(level, threshold);
            }
            next_poll = Instant::now()
                + Duration::from_secs(monitor::next_poll_delay(link, Settings::load().interval_s));
        }
    }
}

fn publish(last: &mut Option<TrayView>, snap: &Snapshot, show: &mut impl FnMut(TrayView)) {
    let view = TrayView::of(snap);
    if last.as_ref() != Some(&view) {
        *last = Some(view.clone());
        show(view);
    }
}

fn remember(tracker: &Tracker) {
    let (battery, seen) = (tracker.last_battery, tracker.last_seen);
    let s = Settings::load();
    if s.last_battery != battery || s.last_seen != seen {
        Settings::update(|s| {
            s.last_battery = battery;
            s.last_seen = seen;
        });
    }
}

fn low_battery(level: u8, threshold: u8) {
    notify(
        "Mouse battery low",
        &format!("The battery is at {level}% (below {threshold}%). Replace or recharge it soon."),
    );
}

/// Forward receiver events to the controller until `stop` is set.
pub fn spawn_event_listener(tx: Sender<Cmd>, stop: Arc<AtomicBool>) {
    if crate::fake::requested() {
        return;
    }
    std::thread::spawn(move || {
        events::listen(
            |e| {
                let _ = tx.send(Cmd::Receiver(e));
            },
            &stop,
        )
    });
}

// ---------------------------------------------------------------------------
// Linux: StatusNotifierItem via ksni
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
pub fn run(open_window: bool) {
    use ksni::blocking::TrayMethods;
    use ksni::menu::{CheckmarkItem, StandardItem};

    struct Tray {
        view: TrayView,
        tx: Sender<Cmd>,
    }

    impl ksni::Tray for Tray {
        fn id(&self) -> String {
            "ssmouse".into()
        }
        fn title(&self) -> String {
            format!("ssmouse - {}", self.view.title)
        }
        fn icon_pixmap(&self) -> Vec<ksni::Icon> {
            [22usize, 32, 64]
                .iter()
                .map(|s| ksni::Icon {
                    width: *s as i32,
                    height: *s as i32,
                    data: super::icon::badge_argb(self.view.level, self.view.stale, *s),
                })
                .collect()
        }
        fn tool_tip(&self) -> ksni::ToolTip {
            ksni::ToolTip {
                title: self.view.title.clone(),
                description: self.view.tooltip.clone(),
                ..Default::default()
            }
        }
        fn activate(&mut self, _x: i32, _y: i32) {
            let _ = self.tx.send(Cmd::OpenWindow);
        }
        fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
            vec![
                StandardItem {
                    label: "Open ssmouse".into(),
                    activate: Box::new(|t: &mut Self| {
                        let _ = t.tx.send(Cmd::OpenWindow);
                    }),
                    ..Default::default()
                }
                .into(),
                StandardItem {
                    label: "Refresh now".into(),
                    activate: Box::new(|t: &mut Self| {
                        let _ = t.tx.send(Cmd::Refresh);
                    }),
                    ..Default::default()
                }
                .into(),
                CheckmarkItem {
                    label: "Start on login".into(),
                    checked: self.view.autostart,
                    activate: Box::new(|t: &mut Self| {
                        let _ = t.tx.send(Cmd::ToggleAutostart);
                    }),
                    ..Default::default()
                }
                .into(),
                ksni::MenuItem::Separator,
                StandardItem {
                    label: "Quit".into(),
                    activate: Box::new(|t: &mut Self| {
                        let _ = t.tx.send(Cmd::Quit);
                    }),
                    ..Default::default()
                }
                .into(),
            ]
        }
    }

    let (tx, rx) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    spawn_event_listener(tx.clone(), stop.clone());
    let view = TrayView {
        level: None,
        stale: false,
        title: "SteelSeries mouse".into(),
        tooltip: "Starting\u{2026}".into(),
        autostart: autostart::is_enabled(),
    };
    let handle = Tray {
        view,
        tx: tx.clone(),
    }
    .spawn();
    let handle = match handle {
        Ok(h) => Some(h),
        Err(e) => {
            eprintln!("ssmouse: no system tray available ({e}); opening the window instead");
            None
        }
    };
    let tray_missing = handle.is_none();
    run_controller(
        rx,
        tx,
        |view| {
            if let Some(h) = &handle {
                h.update(|t| t.view = view);
            }
        },
        open_window || tray_missing,
    );
    stop.store(true, Ordering::Relaxed);
    if let Some(h) = handle {
        h.shutdown();
    }
}

// ---------------------------------------------------------------------------
// Windows / macOS: tray-icon + tao event loop
// ---------------------------------------------------------------------------

#[cfg(not(target_os = "linux"))]
pub fn run(open_window: bool) {
    use tao::event::Event as TaoEvent;
    use tao::event_loop::{ControlFlow, EventLoopBuilder};
    use tray_icon::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
    use tray_icon::{Icon, MouseButton, TrayIconBuilder, TrayIconEvent};

    enum UiEvent {
        View(TrayView),
        Exit,
    }

    let event_loop = EventLoopBuilder::<UiEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let (tx, rx) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    spawn_event_listener(tx.clone(), stop.clone());

    let open = MenuItem::new("Open ssmouse", true, None);
    let refresh = MenuItem::new("Refresh now", true, None);
    let auto = CheckMenuItem::new("Start on login", true, autostart::is_enabled(), None);
    let quit = MenuItem::new("Quit", true, None);
    let menu = Menu::new();
    let _ = menu.append_items(&[
        &open,
        &refresh,
        &auto,
        &PredefinedMenuItem::separator(),
        &quit,
    ]);
    let icon =
        |level, stale| Icon::from_rgba(super::icon::badge_rgba(level, stale, 32), 32, 32).ok();
    let mut tray = TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_menu_on_left_click(false)
        .with_tooltip("ssmouse")
        .with_icon(icon(None, false).expect("icon"))
        .build()
        .ok();

    let controller_tx = tx.clone();
    let controller_proxy = proxy.clone();
    std::thread::spawn(move || {
        run_controller(
            rx,
            controller_tx,
            |view| {
                let _ = controller_proxy.send_event(UiEvent::View(view));
            },
            open_window,
        );
        let _ = controller_proxy.send_event(UiEvent::Exit);
    });

    let (open_id, refresh_id, auto_id, quit_id) = (
        open.id().clone(),
        refresh.id().clone(),
        auto.id().clone(),
        quit.id().clone(),
    );
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::WaitUntil(Instant::now() + Duration::from_millis(100));
        while let Ok(e) = MenuEvent::receiver().try_recv() {
            let cmd = if e.id == open_id {
                Cmd::OpenWindow
            } else if e.id == refresh_id {
                Cmd::Refresh
            } else if e.id == auto_id {
                Cmd::ToggleAutostart
            } else if e.id == quit_id {
                Cmd::Quit
            } else {
                continue;
            };
            let _ = tx.send(cmd);
        }
        while let Ok(e) = TrayIconEvent::receiver().try_recv() {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                ..
            } = e
            {
                let _ = tx.send(Cmd::OpenWindow);
            }
        }
        match event {
            TaoEvent::UserEvent(UiEvent::View(view)) => {
                if let Some(t) = &mut tray {
                    let _ = t.set_icon(icon(view.level, view.stale));
                    let _ = t.set_tooltip(Some(format!("{}\n{}", view.title, view.tooltip)));
                }
                auto.set_checked(view.autostart);
            }
            TaoEvent::UserEvent(UiEvent::Exit) => {
                stop.store(true, Ordering::Relaxed);
                tray.take();
                *control_flow = ControlFlow::Exit;
            }
            _ => {}
        }
    });
}
