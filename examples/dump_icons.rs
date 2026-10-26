//! Dev helper: writes raw RGBA tray badges for inspection.
fn main() {
    let out = std::env::args().nth(1).expect("output dir");
    for (name, level, stale) in [
        ("100", Some(100), false),
        ("71", Some(71), false),
        ("22", Some(22), false),
        ("7", Some(7), false),
        ("stale", Some(71), true),
        ("none", None, false),
    ] {
        for size in [22usize, 64] {
            std::fs::write(
                format!("{out}/{name}-{size}.rgba"),
                ssmouse::gui::icon::badge_rgba(level, stale, size),
            )
            .unwrap();
        }
    }
}
