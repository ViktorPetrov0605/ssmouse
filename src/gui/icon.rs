//! Tray / window icons rendered in code: a rounded badge filled to the battery
//! level with the percentage on top.  No font files or image libraries needed.

/// Colour buckets, as in the window.
pub fn tier_color(level: Option<u8>, stale: bool) -> [u8; 3] {
    match (level, stale) {
        (_, true) | (None, _) => [0x6e, 0x77, 0x81],
        (Some(l), _) if l <= 10 => [0xda, 0x36, 0x33],
        (Some(l), _) if l <= 25 => [0xd2, 0x99, 0x22],
        _ => [0x2e, 0xa0, 0x43],
    }
}

/// 5x7 digits plus '?' and '-'.
const GLYPHS: [(char, [u8; 7]); 12] = [
    (
        '0',
        [
            0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110,
        ],
    ),
    (
        '1',
        [
            0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110,
        ],
    ),
    (
        '2',
        [
            0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111,
        ],
    ),
    (
        '3',
        [
            0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110,
        ],
    ),
    (
        '4',
        [
            0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010,
        ],
    ),
    (
        '5',
        [
            0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110,
        ],
    ),
    (
        '6',
        [
            0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110,
        ],
    ),
    (
        '7',
        [
            0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000,
        ],
    ),
    (
        '8',
        [
            0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110,
        ],
    ),
    (
        '9',
        [
            0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100,
        ],
    ),
    (
        '?',
        [
            0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b00000, 0b00100,
        ],
    ),
    (
        '-',
        [
            0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000,
        ],
    ),
];

const BASE: usize = 64;

/// RGBA pixels of the badge at `size` x `size` (rendered at 64 and box-filtered).
pub fn badge_rgba(level: Option<u8>, stale: bool, size: usize) -> Vec<u8> {
    let big = render(level, stale);
    if size == BASE {
        return big;
    }
    let mut out = vec![0u8; size * size * 4];
    for y in 0..size {
        for x in 0..size {
            let (x0, x1) = (
                x * BASE / size,
                ((x + 1) * BASE / size).max(x * BASE / size + 1),
            );
            let (y0, y1) = (
                y * BASE / size,
                ((y + 1) * BASE / size).max(y * BASE / size + 1),
            );
            let mut acc = [0u32; 4];
            let mut n = 0;
            for sy in y0..y1 {
                for sx in x0..x1 {
                    let i = (sy * BASE + sx) * 4;
                    let a = u32::from(big[i + 3]);
                    for c in 0..3 {
                        acc[c] += u32::from(big[i + c]) * a; // premultiplied
                    }
                    acc[3] += a;
                    n += 1;
                }
            }
            let o = (y * size + x) * 4;
            if let Some(alpha) = acc[3].checked_div(n) {
                for c in 0..3 {
                    out[o + c] = acc[c].checked_div(acc[3]).unwrap_or(0) as u8;
                }
                out[o + 3] = alpha as u8;
            }
        }
    }
    out
}

/// ARGB32 big-endian (StatusNotifierItem format).
pub fn badge_argb(level: Option<u8>, stale: bool, size: usize) -> Vec<u8> {
    let mut px = badge_rgba(level, stale, size);
    for p in px.as_chunks_mut::<4>().0 {
        p.rotate_right(1);
    }
    px
}

fn render(level: Option<u8>, stale: bool) -> Vec<u8> {
    let mut img = vec![0u8; BASE * BASE * 4];
    let color = tier_color(level, stale);
    let radius = 14.0f32;
    let fill_top = level.map(|l| BASE as f32 * (1.0 - f32::from(l.clamp(12, 100)) / 100.0));
    for y in 0..BASE {
        for x in 0..BASE {
            let coverage = rounded_rect_coverage(
                x as f32 + 0.5,
                y as f32 + 0.5,
                0.5,
                0.5,
                BASE as f32 - 0.5,
                BASE as f32 - 0.5,
                radius,
            );
            if coverage <= 0.0 {
                continue;
            }
            let inner = rounded_rect_coverage(
                x as f32 + 0.5,
                y as f32 + 0.5,
                3.5,
                3.5,
                BASE as f32 - 3.5,
                BASE as f32 - 3.5,
                radius - 3.0,
            );
            let (rgb, alpha) = if inner < 1.0 && coverage > 0.0 && inner <= 0.5 {
                // Border, slightly lighter than the fill colour.
                (
                    [
                        color[0].saturating_add(40),
                        color[1].saturating_add(40),
                        color[2].saturating_add(40),
                    ],
                    255.0,
                )
            } else if fill_top.is_some_and(|top| y as f32 >= top) {
                (color, 255.0)
            } else {
                ([0, 0, 0], 110.0)
            };
            let i = (y * BASE + x) * 4;
            img[i..i + 3].copy_from_slice(&rgb);
            img[i + 3] = (alpha * coverage) as u8;
        }
    }
    let text = match level {
        Some(l) => l.min(100).to_string(),
        None => "?".into(),
    };
    draw_text(&mut img, &text);
    img
}

fn rounded_rect_coverage(px: f32, py: f32, x0: f32, y0: f32, x1: f32, y1: f32, r: f32) -> f32 {
    let cx = px.clamp(x0 + r, x1 - r);
    let cy = py.clamp(y0 + r, y1 - r);
    let d = ((px - cx).powi(2) + (py - cy).powi(2)).sqrt() - r;
    let edge = (px - x0).min(x1 - px).min(py - y0).min(y1 - py);
    if edge < 0.0 {
        return 0.0;
    }
    (0.5 - d).clamp(0.0, 1.0)
}

fn draw_text(img: &mut [u8], text: &str) {
    let chars: Vec<[u8; 7]> = text
        .chars()
        .filter_map(|c| GLYPHS.iter().find(|(g, _)| *g == c).map(|(_, rows)| *rows))
        .collect();
    let n = chars.len().max(1);
    let scale = if n >= 3 { 3 } else { 5 };
    let gap = if n >= 3 { 2 } else { 3 };
    let width = n * 5 * scale + (n - 1) * gap;
    let height = 7 * scale;
    let x0 = (BASE - width) / 2;
    let y0 = (BASE - height) / 2;
    let mut mask = vec![false; BASE * BASE];
    for (ci, rows) in chars.iter().enumerate() {
        let cx = x0 + ci * (5 * scale + gap);
        for (ry, row) in rows.iter().enumerate() {
            for rx in 0..5 {
                if row & (0b10000 >> rx) != 0 {
                    for dy in 0..scale {
                        for dx in 0..scale {
                            mask[(y0 + ry * scale + dy) * BASE + cx + rx * scale + dx] = true;
                        }
                    }
                }
            }
        }
    }
    // Dark outline first, then white glyphs, so digits read on any fill.
    for y in 0..BASE {
        for x in 0..BASE {
            if mask[y * BASE + x] {
                continue;
            }
            let near = (y.saturating_sub(2)..(y + 3).min(BASE))
                .any(|ny| (x.saturating_sub(2)..(x + 3).min(BASE)).any(|nx| mask[ny * BASE + nx]));
            if near {
                let i = (y * BASE + x) * 4;
                img[i..i + 4].copy_from_slice(&[0, 0, 0, 200]);
            }
        }
    }
    for (p, on) in mask.iter().enumerate() {
        if *on {
            img[p * 4..p * 4 + 4].copy_from_slice(&[255, 255, 255, 255]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_all_states() {
        for (level, stale) in [
            (Some(100), false),
            (Some(71), false),
            (Some(7), false),
            (Some(50), true),
            (None, false),
        ] {
            for size in [16, 22, 32, 64] {
                let px = badge_rgba(level, stale, size);
                assert_eq!(px.len(), size * size * 4);
                assert!(
                    px.chunks(4)
                        .any(|p| p == [255, 255, 255, 255] || p[0] > 200),
                    "digits visible"
                );
            }
        }
        assert_eq!(tier_color(Some(5), false), [0xda, 0x36, 0x33]);
        assert_eq!(tier_color(Some(70), true), [0x6e, 0x77, 0x81]);
    }
}
