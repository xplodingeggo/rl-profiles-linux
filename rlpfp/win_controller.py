#!/usr/bin/env python3
"""
Layer 6 (Windows): Controller input listener (button -> scoreboard visibility)

Reads controller state via three backends, tried in order until one
finds a device:

  1. XInput (xinput1_4/1_3/9_1_0.dll, ctypes, no package needed) — real
     Xbox controllers, and Steam Input when its output is set to Xbox
     360 Controller emulation. Buttons have real names (A/B/X/Y/...).

  2. Legacy Multimedia Joystick API (winmm.dll's joyGetPosEx/
     joyGetNumDevs, ctypes, no package needed) — a fallback for
     DirectInput-only pads. On modern Windows this API is implemented
     on top of DirectInput itself, so it sees the raw physical
     controller directly without Steam Input remapping anything. Not
     every controller gets registered here though (see #3).

  3. Raw HID reports (`pip install hidapi`) — the lowest-level fallback,
     for controllers neither of the above ever sees at all (confirmed
     with a real 8BitDo Ultimate 2 Wireless connected via its 2.4GHz
     dongle: empty `--list` for both #1 and #2, but it enumerates fine
     as a standard HID Gamepad). This is the lowest level that still
     works uniformly for any compliant device. HID reports have no
     standard button layout though — unlike XInput, there's no "byte N
     bit M is always A" — so finding your button requires an
     interactive baseline-diff: see hid_detect_button() / `--detect-hid`.

Across all three, Steam Input only remaps input for the specific game
process it's actively hooking. That's why a controller working "in
XInput mode" (native hardware switch, or Steam Input actively hooking
THIS process) doesn't mean it'll be visible here via #1 when Steam
Input isn't hooking rl-pfp itself — hence #2 and #3.

Listens for a configurable button and POSTs to the bridge's
/scoreboard-visible endpoint in real time:
  - button pressed  -> {"visible": true}
  - button released -> {"visible": false}

Config (via `rl-pfp config`):
  scoreboard_button_windows        XInput button name (e.g. "LEFT_THUMB",
                                    the default — L3). Only used when
                                    an XInput device is what's found.
  scoreboard_button_dinput_index   Legacy-joystick button index (an
                                    integer, e.g. 2). Only used when NO
                                    XInput device is found but a raw
                                    DirectInput one is. Not set by
                                    default — run --detect to find it.
  scoreboard_button_hid            "vid:pid:byte_offset:bitmask" (e.g.
                                    "0x2dc8:0x6012:14:0x10"). Only used
                                    when NEITHER of the above find a
                                    device but a raw HID gamepad exists.
                                    Not set by default — run
                                    --detect-hid to generate this value.

    python -m rlpfp.win_controller --detect          # XInput / DirectInput
    python -m rlpfp.win_controller --detect-hid       # raw HID fallback

...press the button — it'll print whichever config key + value to set.

Run (alongside rl_stats_bridge.py):
  python -m rlpfp.win_controller
"""

import ctypes
from ctypes import wintypes
from collections import Counter
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

DEFAULT_BUTTON_NAME = "LEFT_THUMB"  # L3

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


# --- Raw HID backend (fallback for pads NEITHER of the above ever see) ---
#
# Some controllers never get registered into Windows' legacy joystick
# list at all — confirmed with a real 8BitDo Ultimate 2 Wireless on its
# 2.4GHz dongle: `--list` came back completely empty for both XInput
# and the legacy DirectInput scan above, yet it enumerates perfectly
# fine as a standard HID Gamepad (Usage Page 0x01, Usage 0x05). Reading
# the raw HID input reports directly is the lowest level that still
# works uniformly for any compliant device, but HID reports have no
# standard button layout (unlike XInput's named bits), so there's no
# way to know which byte/bit is "your button" without an interactive
# baseline-diff: see
# hid_detect_button() / `--detect-hid`.
#
# Requires: pip install hidapi (the "hidapi" PyPI package specifically —
# it bundles a prebuilt native library. The similarly-named "hid"
# package is a thin wrapper that needs hidapi.dll installed separately
# and will ImportError without it). Entirely optional: everything above
# (XInput, legacy DirectInput) works without it — this only matters if
# both of those find nothing for your controller.

try:
    import hid as _hid_module
    HID_AVAILABLE = True
except ImportError:
    _hid_module = None
    HID_AVAILABLE = False

HID_USAGE_PAGE_GENERIC_DESKTOP = 0x01
HID_USAGE_JOYSTICK = 0x04
HID_USAGE_GAMEPAD = 0x05
HID_REPORT_SIZE = 64  # generous upper bound — read() just returns whatever's there
HID_BASELINE_SAMPLES = 40
HID_BASELINE_POLL_SECONDS = 0.01
HID_BASELINE_TIMEOUT_SECONDS = 2.0
HID_DETECT_TIMEOUT_SECONDS = 20


def _hid_find_gamepads() -> list[dict]:
    """Enumerate connected HID devices that identify as a standard
    Joystick or Gamepad top-level collection (Usage Page 0x01, Usage
    0x04/0x05) — the same classification Windows' own HID class driver
    uses, so it works for any compliant device regardless of connection
    type (USB, Bluetooth, 2.4GHz dongle) or whether the legacy joystick
    API above ever saw it."""
    if not HID_AVAILABLE:
        return []
    return [
        d for d in _hid_module.enumerate()
        if d.get("usage_page") == HID_USAGE_PAGE_GENERIC_DESKTOP
        and d.get("usage") in (HID_USAGE_JOYSTICK, HID_USAGE_GAMEPAD)
    ]


def _hid_open(info: dict):
    dev = _hid_module.device()
    dev.open_path(info["path"])
    dev.set_nonblocking(True)
    return dev


def _resolve_hid_spec(raw) -> dict | None:
    """Parses the "vid:pid:byte_offset:bitmask" config string (each
    part accepts hex "0x.." or plain decimal) produced by --detect-hid.
    Returns None (and logs why) for anything malformed, so a typo
    degrades instead of crashing the listener."""
    if not raw:
        return None
    try:
        vid_s, pid_s, byte_s, mask_s = str(raw).split(":")
        return {
            "vid": int(vid_s, 0),
            "pid": int(pid_s, 0),
            "byte_offset": int(byte_s, 0),
            "bitmask": int(mask_s, 0),
        }
    except (ValueError, AttributeError):
        log.warning(
            "scoreboard_button_hid %r in config.json isn't in the expected "
            "'vid:pid:byte_offset:bitmask' format — ignoring it. Run "
            "`python -m rlpfp.win_controller --detect-hid` to generate a "
            "valid value.",
            raw,
        )
        return None


def hid_list_devices() -> None:
    if not HID_AVAILABLE:
        log.info("Raw HID fallback not installed — run: pip install hidapi")
        return
    matches = _hid_find_gamepads()
    if not matches:
        log.info("No HID joystick/gamepad devices found.")
        return
    for d in matches:
        log.info(
            "HID: vid=0x%04x pid=0x%04x product=%r path=%s",
            d["vendor_id"], d["product_id"], d.get("product_string"), d["path"],
        )


def _hid_capture_baseline(dev) -> tuple[list[int], set[int]] | None:
    """Reads a burst of idle reports and returns (baseline, stable_bytes):
    baseline[i] is the per-byte MODE (most common value) — the "at
    rest" fingerprint button presses get diffed against — and
    stable_bytes is the set of byte indices that read the SAME value on
    every single sample.

    Sticks/triggers/gyro drift constantly even at rest (confirmed: one
    axis byte wandered by ~8 across a couple seconds of doing nothing),
    so only stable_bytes are trustworthy candidates for "this is a
    digital button" during detection — a real button is either exactly
    baseline or exactly baseline-with-bits-set, never a slow wobble.

    Returns None if no reports arrived at all (device not actually
    streaming, e.g. asleep)."""
    samples = []
    deadline = time.time() + HID_BASELINE_TIMEOUT_SECONDS
    while len(samples) < HID_BASELINE_SAMPLES and time.time() < deadline:
        report = dev.read(HID_REPORT_SIZE)
        if report:
            samples.append(report)
        time.sleep(HID_BASELINE_POLL_SECONDS)
    if not samples:
        return None
    length = min(len(s) for s in samples)
    baseline = []
    stable_bytes = set()
    for i in range(length):
        values = Counter(s[i] for s in samples)
        baseline.append(values.most_common(1)[0][0])
        if len(values) == 1:
            stable_bytes.add(i)
    return baseline, stable_bytes


HID_RELEASE_TIMEOUT_SECONDS = 10
HID_RELEASE_DEBOUNCE_SAMPLES = 5


def _hid_confirm_release(dev, byte_index: int, baseline_value: int) -> bool:
    """Waits for byte_index to return to baseline_value and STAY there
    for HID_RELEASE_DEBOUNCE_SAMPLES straight reads. A real button
    press+release always does this; a one-off status/battery flag flip
    generally won't revert within the timeout — this is what actually
    separates the two, since a bare "value changed and held for a bit"
    check alone isn't enough (confirmed against a real 8BitDo pad: a
    status byte held its changed value across 5+ consecutive samples
    with nobody touching the controller)."""
    matched = 0
    deadline = time.time() + HID_RELEASE_TIMEOUT_SECONDS
    while time.time() < deadline:
        report = dev.read(HID_REPORT_SIZE)
        if not report:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if byte_index < len(report) and report[byte_index] == baseline_value:
            matched += 1
            if matched >= HID_RELEASE_DEBOUNCE_SAMPLES:
                return True
        else:
            matched = 0
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def hid_detect_button() -> None:
    """`--detect-hid` mode: opens the first HID joystick/gamepad found,
    captures an idle baseline, then waits for you to press a button and
    diffs incoming reports against that baseline. Prints a
    scoreboard_button_hid value to paste into config.json.

    Has to run interactively like this (unlike the XInput/DirectInput
    --detect path, which just waits for *any* known-shape button state
    to change) since HID reports have no standard layout to check
    against ahead of time — the diff against a just-captured baseline
    IS the detection."""
    if not HID_AVAILABLE:
        print("Raw HID fallback not installed — run: pip install hidapi")
        return

    matches = _hid_find_gamepads()
    if not matches:
        print("No HID joystick/gamepad devices found. Is the controller connected?")
        return

    info = matches[0]
    print(f"Opening {info.get('product_string')!r} (vid=0x{info['vendor_id']:04x} "
          f"pid=0x{info['product_id']:04x})...")
    dev = _hid_open(info)

    # Consecutive matching samples required before trusting a detected
    # change — filters out one-off noise spikes (a single stray report)
    # that a bare first-difference check would false-positive on.
    DEBOUNCE_SAMPLES = 5

    try:
        print("Capturing idle baseline — don't touch the controller for a moment...")
        result = _hid_capture_baseline(dev)
        if result is None:
            print(
                "No reports received from the device — it may be asleep or "
                "not actually streaming input. Move a stick or press "
                "anything once to wake it, then try again."
            )
            return
        baseline, stable_bytes = result
        if not stable_bytes:
            print(
                "Every byte in this device's report drifted during the idle "
                "baseline — couldn't find any stable candidate bytes. Try "
                "again (make sure the controller is sitting still), or this "
                "controller's digital buttons may not map cleanly this way."
            )
            return

        print(
            f"Baseline captured ({len(baseline)} bytes, {len(stable_bytes)} "
            f"stable). Now press and HOLD the button you want for the "
            f"scoreboard toggle — you have {HID_DETECT_TIMEOUT_SECONDS}s "
            f"(Ctrl+C to cancel)..."
        )

        pending = None  # (byte_index, changed_bits) awaiting debounce confirmation
        pending_count = 0
        deadline = time.time() + HID_DETECT_TIMEOUT_SECONDS
        while time.time() < deadline:
            report = dev.read(HID_REPORT_SIZE)
            if not report:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            changed = [
                (i, report[i] ^ baseline[i])
                for i in stable_bytes
                if i < len(report) and report[i] != baseline[i]
            ]
            # Exactly one changed stable byte is the clean case a real
            # button press produces. Multiple at once (e.g. Ctrl+C
            # timing, or an unrelated stable byte glitching) — restart
            # debounce rather than guessing which one is real.
            candidate = changed[0] if len(changed) == 1 else None

            if candidate is not None and candidate == pending:
                pending_count += 1
            else:
                pending, pending_count = candidate, 1 if candidate else 0

            if pending is not None and pending_count >= DEBOUNCE_SAMPLES:
                byte_index, changed_bits = pending
                print(f"\nHeld change detected at byte {byte_index}: "
                      f"{baseline[byte_index]} -> "
                      f"{baseline[byte_index] ^ changed_bits} "
                      f"(bitmask 0x{changed_bits:02x}) — now RELEASE it "
                      f"(confirming this is a real button, not e.g. a "
                      f"battery/status flag that just happened to flip)...")
                if _hid_confirm_release(dev, byte_index, baseline[byte_index]):
                    print("Release confirmed.")
                    print(
                        'Set this in config.json (or via `rl-pfp config`): '
                        f'"scoreboard_button_hid": "0x{info["vendor_id"]:04x}:'
                        f'0x{info["product_id"]:04x}:{byte_index}:0x{changed_bits:02x}"'
                    )
                else:
                    print(
                        f"\nByte {byte_index} never returned to its baseline "
                        f"value ({baseline[byte_index]}) — this probably "
                        f"WASN'T your button (likely a status/battery flag "
                        f"that changed on its own). Discarding this result — "
                        f"run --detect-hid again and press+hold firmly."
                    )
                return

            time.sleep(POLL_INTERVAL_SECONDS)
        print("\nTimed out without detecting a held change — try again and "
              "press+hold firmly right after the prompt.")
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        dev.close()


# --- Unified lookup (XInput -> legacy DirectInput -> raw HID) ------------

def find_any_controller() -> tuple[str, object] | tuple[None, None]:
    """Returns ("xinput", index), ("dinput", index), ("hid", device_info
    dict), or (None, None). XInput is checked first — it gives semantic
    button names, so it's preferred whenever available (a real Xbox pad,
    or Steam Input set to Xbox 360 emulation). Falls back to the legacy
    joystick/DirectInput scan next, then to raw HID enumeration last —
    each level catches controllers the previous one simply cannot see."""
    index = find_connected_controller()
    if index is not None:
        return "xinput", index
    index = _dinput_find_connected()
    if index is not None:
        return "dinput", index
    hid_matches = _hid_find_gamepads()
    if hid_matches:
        return "hid", hid_matches[0]
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
    hid_list_devices()


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
        while backend is None or backend == "hid":
            if backend == "hid":
                print(
                    f"Found {index.get('product_string')!r} via raw HID only — "
                    "neither XInput nor the legacy DirectInput API can see it, "
                    "so this --detect flow (which watches a known button-state "
                    "shape change) doesn't apply. Use the HID-specific detector "
                    "instead:\n  python -m rlpfp.win_controller --detect-hid"
                )
                return
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


def _run_hid_listen(info: dict, hid_spec: dict) -> None:
    """Inner loop for the raw-HID backend — needs an open device handle
    kept alive across polls (unlike XInput/DirectInput, which are
    stateless index lookups each tick), so it's structured differently
    from the xinput/dinput branch below."""
    if (info["vendor_id"], info["product_id"]) != (hid_spec["vid"], hid_spec["pid"]):
        log.warning(
            "Found a different HID controller (vid=0x%04x pid=0x%04x) than "
            "configured in scoreboard_button_hid (vid=0x%04x pid=0x%04x) — "
            "skipping. Retrying in %ss...",
            info["vendor_id"], info["product_id"],
            hid_spec["vid"], hid_spec["pid"], RECONNECT_DELAY_SECONDS,
        )
        time.sleep(RECONNECT_DELAY_SECONDS)
        return

    try:
        dev = _hid_open(info)
    except Exception:
        log.exception("Failed to open HID device %r", info.get("product_string"))
        time.sleep(RECONNECT_DELAY_SECONDS)
        return

    log.info("Selected controller: backend=hid product=%r", info.get("product_string"))
    was_pressed = False
    last_report = None
    byte_offset, bitmask = hid_spec["byte_offset"], hid_spec["bitmask"]
    try:
        while True:
            report = dev.read(HID_REPORT_SIZE)
            if report:
                last_report = report
            elif last_report is None:
                # No report yet since opening — device may just be slow to
                # start streaming; keep waiting rather than declaring it
                # disconnected immediately.
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            is_pressed = (
                last_report is not None
                and byte_offset < len(last_report)
                and bool(last_report[byte_offset] & bitmask)
            )
            if is_pressed != was_pressed:
                post_scoreboard_visible(is_pressed)
                was_pressed = is_pressed

            time.sleep(POLL_INTERVAL_SECONDS)
    except Exception:
        log.exception("Unexpected error in HID listen loop")
    finally:
        dev.close()
    time.sleep(RECONNECT_DELAY_SECONDS)


def listen_loop() -> None:
    config = load_config()
    xinput_mask, xinput_name = resolve_button_mask(
        config.get("scoreboard_button_windows") or DEFAULT_BUTTON_NAME
    )
    dinput_index = _resolve_dinput_index(config.get("scoreboard_button_dinput_index"))
    hid_spec = _resolve_hid_spec(config.get("scoreboard_button_hid"))
    log.info(
        "Scoreboard toggle button: XInput=%s, DirectInput index=%s, HID spec=%s",
        xinput_name,
        dinput_index if dinput_index is not None else "(not set)",
        hid_spec if hid_spec is not None else "(not set)",
    )

    while True:
        backend, index = find_any_controller()
        if backend is None:
            log.info(
                "No controller connected (checked XInput, legacy "
                "DirectInput, and raw HID) — retrying in %ss...",
                RECONNECT_DELAY_SECONDS,
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

        if backend == "hid" and hid_spec is None:
            log.warning(
                "Found a raw HID controller (%r) — not visible via XInput or "
                "legacy DirectInput — but scoreboard_button_hid isn't set in "
                "config.json. Run `python -m rlpfp.win_controller "
                "--detect-hid`, then set it via `rl-pfp config`. Retrying "
                "in %ss...",
                index.get("product_string"), RECONNECT_DELAY_SECONDS,
            )
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        if backend == "hid":
            _run_hid_listen(index, hid_spec)
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

    if "--detect-hid" in sys.argv:
        hid_detect_button()
        sys.exit(0)

    if "--detect" in sys.argv:
        detect_button()
        sys.exit(0)

    log.info("Starting controller listener (Ctrl+C to stop)...")
    try:
        listen_loop()
    except KeyboardInterrupt:
        pass
