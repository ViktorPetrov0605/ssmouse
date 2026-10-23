"""Button and scroll-wheel remapping (``19`` / ``1A`` commands).

Each binding is five bytes: a function code and four key bytes, the format
SteelSeries GG calls ``newerFourByteSelector``:

* ``0x00`` disabled, ``0x01``-``0x06`` mouse buttons, ``0x30`` DPI cycle,
  ``0x31``/``0x32`` scroll up/down
* ``0x51`` keyboard: up to three modifier usages then the key usage (HID
  keyboard page), zero padded, e.g. Ctrl+C = ``51 E0 06 00 00``
* ``0x61`` media: one consumer-page usage, e.g. volume up = ``61 E9 00 00 00``

Binding specs are short strings: ``left``, ``dpi``, ``scroll-up``,
``disabled``, ``key:ctrl+shift+t``, ``media:play-pause``.
"""

from __future__ import annotations

from dataclasses import dataclass

FN_DISABLED = 0x00
FN_KEY = 0x51
FN_MEDIA = 0x61

#: Simple actions: spec -> function code.
SIMPLE_ACTIONS = {
    "disabled": FN_DISABLED,
    "left": 0x01,
    "right": 0x02,
    "middle": 0x03,
    "back": 0x04,
    "forward": 0x05,
    "button6": 0x06,
    "dpi": 0x30,
    "scroll-up": 0x31,
    "scroll-down": 0x32,
}
ACTION_LABELS = {
    "disabled": "Disabled",
    "left": "Left click",
    "right": "Right click",
    "middle": "Middle click",
    "back": "Back (button 4)",
    "forward": "Forward (button 5)",
    "button6": "Button 6",
    "dpi": "Cycle DPI",
    "scroll-up": "Scroll up",
    "scroll-down": "Scroll down",
}

#: Consumer-page usages GG offers for media actions.
MEDIA_KEYS = {
    "mute": 0xE2,
    "next": 0xB5,
    "play-pause": 0xCD,
    "previous": 0xB6,
    "volume-up": 0xE9,
    "volume-down": 0xEA,
}

MODIFIERS = {
    "ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2, "super": 0xE3,
    "rctrl": 0xE4, "rshift": 0xE5, "ralt": 0xE6, "rsuper": 0xE7,
}
_MODIFIER_ALIASES = {"control": "ctrl", "meta": "super", "win": "super", "cmd": "super",
                     "option": "alt", "altgr": "ralt"}
MAX_MODIFIERS = 3


def _keyboard_usages() -> dict[str, int]:
    keys = {chr(ord("a") + i): 0x04 + i for i in range(26)}
    keys.update({str((i + 1) % 10): 0x1E + i for i in range(10)})
    keys.update({"f%d" % (i + 1): 0x3A + i for i in range(12)})
    keys.update({"f%d" % (i + 13): 0x68 + i for i in range(12)})
    keys.update({
        "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
        "minus": 0x2D, "equal": 0x2E, "leftbrace": 0x2F, "rightbrace": 0x30,
        "backslash": 0x31, "semicolon": 0x33, "apostrophe": 0x34, "grave": 0x35,
        "comma": 0x36, "dot": 0x37, "slash": 0x38, "capslock": 0x39,
        "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49,
        "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "menu": 0x65,
    })
    return keys


KEYS = _keyboard_usages()
_KEY_ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
                "pgup": "pageup", "pgdn": "pagedown", "-": "minus", "=": "equal",
                "[": "leftbrace", "]": "rightbrace", "\\": "backslash", ";": "semicolon",
                "'": "apostrophe", "`": "grave", ",": "comma", ".": "dot", "/": "slash"}

#: Physical controls in mapping-table order, with GG's defaults.
BUTTONS = ("left", "right", "middle", "back", "forward", "dpi")
WHEEL = ("wheel-up", "wheel-down")
TRIGGERS = BUTTONS + WHEEL
TRIGGER_LABELS = {
    "left": "Left button", "right": "Right button", "middle": "Wheel click",
    "back": "Side button (back)", "forward": "Side button (forward)",
    "dpi": "DPI button", "wheel-up": "Wheel up", "wheel-down": "Wheel down",
}
DEFAULT_BINDINGS = {
    "left": "left", "right": "right", "middle": "middle", "back": "back",
    "forward": "forward", "dpi": "dpi", "wheel-up": "scroll-up", "wheel-down": "scroll-down",
}
#: The button table has room for seven entries; the mouse has six buttons.
BUTTON_SLOTS = 7


@dataclass(frozen=True)
class Binding:
    function: int
    keys: tuple[int, int, int, int] = (0, 0, 0, 0)

    def encode(self) -> bytes:
        return bytes([self.function, *self.keys])


def _canonical_key(name: str) -> str:
    name = name.strip().lower()
    return _KEY_ALIASES.get(name, name)


def parse_binding(spec: str) -> Binding:
    """Turn a binding spec (see module docs) into a :class:`Binding`."""
    text = spec.strip().lower()
    if text in SIMPLE_ACTIONS:
        return Binding(SIMPLE_ACTIONS[text])
    kind, _, arg = text.partition(":")
    if kind == "media":
        if arg not in MEDIA_KEYS:
            raise ValueError("unknown media key %r (choose: %s)" % (arg, ", ".join(MEDIA_KEYS)))
        return Binding(FN_MEDIA, (MEDIA_KEYS[arg], 0, 0, 0))
    if kind == "key":
        parts = [part for part in arg.replace(" ", "").split("+") if part]
        if not parts:
            raise ValueError("key binding needs a key, e.g. key:ctrl+c")
        *mods, key = parts
        usages = []
        for mod in mods:
            mod = _MODIFIER_ALIASES.get(mod, mod)
            if mod not in MODIFIERS:
                raise ValueError("unknown modifier %r" % mod)
            usages.append(MODIFIERS[mod])
        if len(usages) > MAX_MODIFIERS:
            raise ValueError("at most %d modifiers are supported" % MAX_MODIFIERS)
        key = _MODIFIER_ALIASES.get(_canonical_key(key), _canonical_key(key))
        if key in MODIFIERS:
            usages.append(MODIFIERS[key])
        elif key in KEYS:
            usages.append(KEYS[key])
        else:
            raise ValueError("unknown key %r" % key)
        return Binding(FN_KEY, tuple(usages + [0] * (4 - len(usages))))  # type: ignore[arg-type]
    raise ValueError(
        "invalid binding %r (use %s, key:<combo> or media:<key>)" % (spec, ", ".join(SIMPLE_ACTIONS))
    )


def describe_binding(spec: str) -> str:
    text = spec.strip().lower()
    if text in ACTION_LABELS:
        return ACTION_LABELS[text]
    kind, _, arg = text.partition(":")
    if kind == "media":
        return "Media: " + arg.replace("-", " ").title()
    if kind == "key":
        return "Keys: " + "+".join(part.capitalize() for part in arg.split("+"))
    return spec


def mapping_payloads(bindings: dict[str, str]) -> list[bytes]:
    """``19`` (buttons) and ``1A`` (wheel) payloads for a trigger -> spec map."""
    merged = {**DEFAULT_BINDINGS, **bindings}
    unknown = set(bindings) - set(TRIGGERS)
    if unknown:
        raise ValueError("unknown control(s): %s" % ", ".join(sorted(unknown)))
    buttons = b"".join(parse_binding(merged[name]).encode() for name in BUTTONS)
    buttons += Binding(FN_DISABLED).encode() * (BUTTON_SLOTS - len(BUTTONS))
    wheel = b"".join(parse_binding(merged[name]).encode() for name in WHEEL)
    return [bytes([0x19]) + buttons, bytes([0x1A]) + wheel]
