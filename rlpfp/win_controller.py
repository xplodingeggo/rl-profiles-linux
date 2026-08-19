#!/usr/bin/env python3
"""
Layer 6 (Windows): Controller input listener (button -> scoreboard visibility)

Windows equivalent of controller_listener.py. evdev/`/dev/input` don't
exist on Windows, so this reads controller state via two ctypes-only
backends — no extra package needed for either, just system DLLs:

  1. XInput (xinput1_4/1_3/9_1_0.dll) — real Xbox controllers, and
     Steam Input when its output is set to Xbox 360 Controller
     emulation. Buttons have real names (A/B/X/Y/BACK/START/...).

  2. Legacy Multimedia Joystick API (winmm.dll's joyGetPosEx/
     joyGetNumDevs) — a fallback for DirectInput-only pads. On modern
     Windows this API is implemented on top of DirectInput itself, so
     it sees the RAW physical controller directly, without needing
     Steam Input to remap/expose it as XInput at all. This matters
     because Steam Input on Windows only remaps input for the specific
     game process it's actively hooking — unlike Linux, where Steam's
     uinput-based remap exposes a virtual Xbox pad system-wide, so any
     evdev reader picks it up regardless of what's running. Buttons
     here are just numbered (no semantic names) — see
     scoreboard_button_dinput_index below.

find_any_controller() tries XInput first (semantic names, so prefer it
when available), then falls back to the legacy joystick API if nothing
showed up there — covering both "Steam Input set to Xbox emulation" and
"raw DirectInput pad, Steam Input not remapping it at all" setups
without the user needing to pick a mode.

Listens for a configurable button and POSTs to the bridge's
/scoreboard-visible endpoint in real time, same as the Linux listener:
  - button pressed  -> {"visible": true}
  - button released -> {"visible": false}

Config (via `rl-pfp config`):
  scoreboard_button_windows        XInput button name (e.g. "LEFT_THUMB",
                                    the default — L3, matching Linux's
                                    BTN_THUMBL default). Only used when
                                    an XInput device is what's found.
  scoreboard_button_dinput_index   Legacy-joystick button index (an
                                    integer, e.g. 2). Only used when NO
                                    XInput device is found but a raw
                                    DirectInput one is. Not set by
                                    default — run --detect to find it.

    python -m rlpfp.win_controller --detect

...press the button — it'll print whichever config key + value to set,
depending on which backend actually found your controller.

Run (alongside rl_stats_bridge.py):
  python -m rlpfp.win_controller
"""

import ctypes
from ctypes import wintypes
import json
import logging
import time
import urllib.error
import urllib.request

from .config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("win-controller")

BRIDGE_URL = "http://127.0.0.1:9090"

DEFAULT_BUTTON_NAME = "LEFT_THUMB"  # L3 — same default as Linux's BTN_THUMBL

POLL_INTERVAL_SECONDS = 0.02  # 50Hz — fast enough to feel instant on a button press
RECONNECT_DELAY_SECONDS = 5

# --- XInput backend ------------------------------------------------------

MAX_XINPUT_CONTROLLERS = 4  # XInput supports up to 4 user indices

# XInput button bitmask -> name. XINPUT_GAMEPAD_BACK is what Xbox One/
# Series controllers' "View" button reports as, and XINPUT_GAMEPAD_START
# is "Menu" — XInput itself never got renamed past the Xbox 360 names.
BUTTON_NAMES = {
    0x0001: "DPAD_UP",
    0x0002: "DPAD_DOWN",
    0x0004: "DPAD_LEFT",
    0x0008: "DPAD_RIGHT",
    0x0010: "START",
    0x0020: "BACK",
    0x0040: "LEFT_THUMB",
    0x0080: "RIGHT_THUMB",
    0x0100: "LEFT_SHOULDER",
    0x0200: "RIGHT_SHOULDER",
    0x1000: "A",
    0x2000: "B",
    0x4000: "X",
    0x8000: "Y",
}
NAME_TO_MASK = {name: mask for mask, name in BUTTON_NAMES.items()}


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


def _load_xinput():
    """XInput's DLL name varies by Windows version/SDK — try newest
    first, fall back to the ones guaranteed present on older Windows 10/
    any Windows 11 install."""
    for dll_name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(dll_name)
        except OSError:
            continue
    raise OSError(
        "Couldn't load any XInput DLL (xinput1_4/1_3/9_1_0) — this "
        "should ship with every Windows 10/11 install. Is this really "
        "Windows?"
    )


_xinput = _load_xinput()
_xinput.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_STATE)]
_xinput.XInputGetState.restype = ctypes.c_uint


def get_state(user_index: int) -> XINPUT_STATE | None:
    """Returns the XINPUT_STATE for a connected controller at this user
    index (0-3), or None if nothing's connected there."""
    state = XINPUT_STATE()
    result = _xinput.XInputGetState(user_index, ctypes.byref(state))
    if result == 0:  # ERROR_SUCCESS
        return state
    return None  # ERROR_DEVICE_NOT_CONNECTED (or any other failure)


def find_connected_controller() -> int | None:
    """Returns the first connected XInput user index (0-3), or None."""
    for i in range(MAX_XINPUT_CONTROLLERS):
        if get_state(i) is not None:
            return i
    return None


def resolve_button_mask(button_name: str) -> tuple[int, str]:
    """Look up an XInput button mask by name. Falls back to
    DEFAULT_BUTTON_NAME with a warning if the configured name isn't
    recognized, so a typo in config.json degrades instead of crashing
    the whole listener."""
    mask = NAME_TO_MASK.get(button_name.upper())
    if mask is None:
        log.warning(
            "scoreboard_button_windows %r in config.json isn't a "
            "recognized XInput button name — falling back to %s (L3). "
            "Run `python -m rlpfp.win_controller --detect` and press "
            "your button to find the correct name.",
            button_name, DEFAULT_BUTTON_NAME,
        )
        return NAME_TO_MASK[DEFAULT_BUTTON_NAME], DEFAULT_BUTTON_NAME
    return mask, button_name.upper()


# --- Legacy Multimedia Joystick (DirectInput) backend ---------------------
#
# joyGetPosEx/joyGetNumDevs from winmm.dll. This is the same joystick API
# Windows has shipped since the 90s; on Vista+ it's a thin shim over
# DirectInput itself, so it sees any DirectInput-recognized HID gamepad
# directly — no Steam Input remap required. Up to 16 legacy joystick
# slots; buttons come back as a plain 32-bit bitmask with no semantic
# names (unlike XInput), so scoreboard_button_dinput_index is just a
# button index (0-31) rather than a name like "A" or "LEFT_THUMB".

MAX_LEGACY_JOYSTICKS = 16
JOYERR_NOERROR = 0
JOY_RETURNBUTTONS = 0x00000080
JOY_RETURNCENTERED = 0x00000400
JOY_RETURNALL = 0x000000FF | JOY_RETURNCENTERED
MAXPNAMELEN = 32

_winmm = ctypes.WinDLL("winmm")


class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwXpos", wintypes.DWORD),
        ("dwYpos", wintypes.DWORD),
        ("dwZpos", wintypes.DWORD),
        ("dwRpos", wintypes.DWORD),
        ("dwUpos", wintypes.DWORD),
        ("dwVpos", wintypes.DWORD),
        ("dwButtons", wintypes.DWORD),
        ("dwButtonNumber", wintypes.DWORD),
        ("dwPOV", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("dwReserved2", wintypes.DWORD),
    ]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("szPname", ctypes.c_wchar * MAXPNAMELEN),
        ("wXmin", wintypes.UINT),
        ("wXmax", wintypes.UINT),
        ("wYmin", wintypes.UINT),
        ("wYmax", wintypes.UINT),
        ("wZmin", wintypes.UINT),
        ("wZmax", wintypes.UINT),
        ("wNumButtons", wintypes.UINT),
        ("wPeriodMin", wintypes.UINT),
        ("wPeriodMax", wintypes.UINT),
        ("wRmin", wintypes.UINT),
        ("wRmax", wintypes.UINT),
        ("wUmin", wintypes.UINT),
        ("wUmax", wintypes.UINT),
        ("wVmin", wintypes.UINT),
        ("wVmax", wintypes.UINT),
        ("wCaps", wintypes.UINT),
        ("wMaxAxes", wintypes.UINT),
        ("wNumAxes", wintypes.UINT),
        ("wMaxButtons", wintypes.UINT),
        ("szRegKey", ctypes.c_wchar * MAXPNAMELEN),
        ("szOEMVxD", ctypes.c_wchar * 260),
    ]


_winmm.joyGetNumDevs.restype = wintypes.UINT
_winmm.joyGetPosEx.argtypes = [wintypes.UINT, ctypes.POINTER(JOYINFOEX)]
_winmm.joyGetPosEx.restype = ctypes.c_uint
_winmm.joyGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(JOYCAPSW), wintypes.UINT]
_winmm.joyGetDevCapsW.restype = ctypes.c_uint


def _dinput_get_state(index: int) -> JOYINFOEX | None:
    """Returns the JOYINFOEX for a connected legacy joystick slot
    (0-15), or None if nothing's connected there."""
    info = JOYINFOEX()
    info.dwSize = ctypes.sizeof(JOYINFOEX)
    info.dwFlags = JOY_RETURNALL
    result = _winmm.joyGetPosEx(index, ctypes.byref(info))
    return info if result == JOYERR_NOERROR else None


def _dinput_device_name(index: int) -> str:
    caps = JOYCAPSW()
    result = _winmm.joyGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
    return caps.szPname if result == JOYERR_NOERROR else "(unknown)"


# Windows keeps a permanent legacy compatibility placeholder bound to
# unpopulated joystick slots, always named exactly this. It reports as
# "connected" via joyGetPosEx — and its dwButtons/dwButtonNumber fields
# come back as uninitialized garbage that changes on every single call
# (confirmed: values like 0, 1, 2 buttons and random button bits,
# flapping between consecutive reads with nothing plugged in) — so
# neither "buttons present" nor "button state" can distinguish it from
# a real controller. Its NAME is stable across calls, though, so that's
# what has to be filtered on: a real controller reports its actual
# product name (e.g. "Xbox 360 Controller", "8BitDo Ultimate 2C"), never
# this placeholder string.
_PHANTOM_DEVICE_NAME = "microsoft pc-joystick driver"


def _dinput_find_connected() -> int | None:
    """Returns the first connected legacy joystick slot (0-15) that's a
    real device (not the phantom placeholder above), or None."""
    for i in range(MAX_LEGACY_JOYSTICKS):
        info = _dinput_get_state(i)
        if info is None:
            continue
        if _dinput_device_name(i).strip().lower() == _PHANTOM_DEVICE_NAME:
            continue
        return i
    return None


# --- Unified lookup (tries XInput, falls back to legacy DirectInput) -----

def find_any_controller() -> tuple[str, int] | tuple[None, None]:
    """Returns ("xinput", index), ("dinput", index), or (None, None).
    XInput is checked first — it gives semantic button names, so it's
    preferred whenever available (a real Xbox pad, or Steam Input set to
    Xbox 360 emulation). Falls back to the legacy joystick/DirectInput
    scan for pads Steam Input isn't remapping (or isn't running at all
    for), which the XInput API simply can't see."""
    index = find_connected_controller()
    if index is not None:
        return "xinput", index
    index = _dinput_find_connected()
    if index is not None:
        return "dinput", index
    return None, None


def list_all_devices() -> None:
    """Debug helper: prints connected state for all XInput slots and all
    legacy DirectInput joystick slots."""
    for i in range(MAX_XINPUT_CONTROLLERS):
        state = get_state(i)
        if state is not None:
            log.info("XInput slot %d: connected (buttons=0x%04x)", i, state.Gamepad.wButtons)
        else:
            log.info("XInput slot %d: not connected", i)
    for i in range(MAX_LEGACY_JOYSTICKS):
        info = _dinput_get_state(i)
        if info is None:
            log.info("DirectInput slot %d: not connected", i)
            continue
        name = _dinput_device_name(i)
        if name.strip().lower() == _PHANTOM_DEVICE_NAME:
            log.info(
                "DirectInput slot %d: unpopulated (Windows' legacy compatibility "
                "placeholder, not a real controller — ignore this one)", i,
            )
            continue
        log.info(
            "DirectInput slot %d: connected, name=%r buttons=0x%08x numButtons=%d",
            i, name, info.dwButtons, info.dwButtonNumber,
        )


def detect_button() -> None:
    """`--detect` mode: waits for the next button press on the first
    connected controller (XInput first, falling back to legacy
    DirectInput) and prints whichever config key + value to set — an
    XInput button name, or a DirectInput button index — so you can copy
    it into config.json without guessing.

    A DirectInput-only pad Steam Input isn't remapping to XInput won't
    show up on the first check right after Steam/the game starts (or
    ever, via XInput — it'll be picked up via the DirectInput fallback
    instead), so this retries every RECONNECT_DELAY_SECONDS instead of
    giving up immediately (same patience listen_loop() has)."""
    try:
        backend, index = find_any_controller()
        waited = False
        while backend is None:
            if not waited:
                print(
                    "No controller detected yet (checked XInput and legacy "
                    "DirectInput) — if you're using Steam Input, this can "
                    "take a few seconds to bind after Steam/the game starts. "
                    f"Retrying every {RECONNECT_DELAY_SECONDS}s (Ctrl+C to cancel)..."
                )
                waited = True
            time.sleep(RECONNECT_DELAY_SECONDS)
            backend, index = find_any_controller()

        print(f"Listening on {backend} slot {index} — press the button you want "
              f"to use for the scoreboard toggle (Ctrl+C to cancel)...")

        prev_buttons = 0
        while True:
            if backend == "xinput":
                state = get_state(index)
                buttons = state.Gamepad.wButtons if state is not None else None
            else:
                info = _dinput_get_state(index)
                buttons = info.dwButtons if info is not None else None

            if buttons is None:
                log.warning("Controller disconnected — waiting for it to reconnect...")
                backend, index = None, None
                while backend is None:
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    backend, index = find_any_controller()
                print(f"Reconnected on {backend} slot {index}.")
                prev_buttons = 0
                continue

            newly_pressed = buttons & ~prev_buttons
            if newly_pressed:
                if backend == "xinput":
                    names = [name for mask, name in BUTTON_NAMES.items() if newly_pressed & mask]
                    if names:
                        print(f"\nDetected: {' + '.join(names)}")
                        print(f'Set this in config.json (or via `rl-pfp config`): '
                              f'"scoreboard_button_windows": "{names[0]}"')
                        return
                else:
                    for bit in range(32):
                        if newly_pressed & (1 << bit):
                            print(f"\nDetected: DirectInput button index {bit}")
                            print(f'Set this in config.json (or via `rl-pfp config`): '
                                  f'"scoreboard_button_dinput_index": {bit}')
                            return
            prev_buttons = buttons
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nCancelled.")


def post_scoreboard_visible(visible: bool) -> None:
    body = json.dumps({"visible": visible}).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}/scoreboard-visible",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
        log.info("scoreboard_visible -> %s", visible)
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError) as e:
        log.warning("Couldn't reach bridge: %s", e)


def _resolve_dinput_index(raw) -> int | None:
    """config.json / `rl-pfp config` store this as plain text (the
    generic interactive prompt has no notion of "this field is an
    int") — parse defensively so a stray non-numeric value degrades to
    "not set" instead of crashing the listener."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            "scoreboard_button_dinput_index %r in config.json isn't a "
            "valid integer — ignoring it. Run `python -m rlpfp.win_controller "
            "--detect` to find the right value.",
            raw,
        )
        return None


def listen_loop() -> None:
    config = load_config()
    xinput_mask, xinput_name = resolve_button_mask(
        config.get("scoreboard_button_windows") or DEFAULT_BUTTON_NAME
    )
    dinput_index = _resolve_dinput_index(config.get("scoreboard_button_dinput_index"))
    log.info(
        "Scoreboard toggle button: XInput=%s, DirectInput index=%s",
        xinput_name, dinput_index if dinput_index is not None else "(not set)",
    )

    while True:
        backend, index = find_any_controller()
        if backend is None:
            log.info(
                "No controller connected (checked XInput and legacy "
                "DirectInput) — retrying in %ss...", RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        if backend == "dinput" and dinput_index is None:
            log.warning(
                "Found a DirectInput controller (not XInput / not remapped "
                "by Steam Input), but scoreboard_button_dinput_index isn't "
                "set in config.json. Run `python -m rlpfp.win_controller "
                "--detect` and press your button to find the index, then "
                "set it via `rl-pfp config`. Retrying in %ss...",
                RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        log.info("Selected controller: backend=%s slot=%d", backend, index)
        was_pressed = False
        try:
            while True:
                if backend == "xinput":
                    state = get_state(index)
                    connected = state is not None
                    is_pressed = connected and bool(state.Gamepad.wButtons & xinput_mask)
                else:
                    info = _dinput_get_state(index)
                    connected = info is not None
                    is_pressed = connected and bool(info.dwButtons & (1 << dinput_index))

                if not connected:
                    log.warning("Controller disconnected. Reconnecting...")
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    break

                if is_pressed != was_pressed:
                    post_scoreboard_visible(is_pressed)
                    was_pressed = is_pressed

                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception:
            log.exception("Unexpected error in listen loop")
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    import sys

    if "--list" in sys.argv:
        list_all_devices()
        sys.exit(0)

    if "--detect" in sys.argv:
        detect_button()
        sys.exit(0)

    log.info("Starting controller listener (Ctrl+C to stop)...")
    try:
        listen_loop()
    except KeyboardInterrupt:
        pass
