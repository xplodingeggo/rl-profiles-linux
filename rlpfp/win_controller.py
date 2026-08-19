#!/usr/bin/env python3
"""
Layer 6 (Windows): Controller input listener (button -> scoreboard visibility)

Windows equivalent of controller_listener.py. evdev/`/dev/input` don't
exist on Windows, so this polls the XInput API directly via ctypes
instead — no extra package needed for the XInput call itself (it's a
system DLL), just ctypes from the standard library.

XInput covers real Xbox controllers AND Steam Input's virtual Xbox-pad
remap (the same scenario controller_listener.py's docstring describes
for Linux) — Steam Input on Windows also emulates an XInput device, so
a non-Xbox pad (e.g. a DualSense or 8BitDo) remapped through Steam Input
shows up here the same way.

Listens for a configurable button and POSTs to the bridge's
/scoreboard-visible endpoint in real time, same as the Linux listener:
  - button pressed  -> {"visible": true}
  - button released -> {"visible": false}

Defaults to LEFT_THUMB (L3), same default as the Linux side's BTN_THUMBL.
Set "scoreboard_button_windows" in config.json (via `rl-pfp config`) to
any XInput button name — see BUTTON_NAMES below. To find yours:

    python -m rlpfp.win_controller --detect

...then press the button — it'll print its XInput name so you can copy
it into config.json.

Run (alongside rl_stats_bridge.py):
  python -m rlpfp.win_controller
"""

import ctypes
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

MAX_CONTROLLERS = 4  # XInput supports up to 4 user indices
POLL_INTERVAL_SECONDS = 0.02  # 50Hz — fast enough to feel instant on a button press
RECONNECT_DELAY_SECONDS = 5

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
    for i in range(MAX_CONTROLLERS):
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


def list_all_devices() -> None:
    """Debug helper: prints connected state for all 4 XInput slots."""
    for i in range(MAX_CONTROLLERS):
        state = get_state(i)
        if state is not None:
            log.info("User index %d: connected (buttons=0x%04x)", i, state.Gamepad.wButtons)
        else:
            log.info("User index %d: not connected", i)


def detect_button() -> None:
    """`--detect` mode: waits for the next button press on the first
    connected controller and prints its XInput name, so you can copy
    the right value into config.json's scoreboard_button_windows
    without guessing.

    A DirectInput-only pad (e.g. most non-Xbox controllers) only shows
    up here once Steam Input has bound it to a virtual XInput device —
    that binding can take a few seconds after Steam/the game starts, or
    after you enable Steam Input for it, so this retries every
    RECONNECT_DELAY_SECONDS instead of giving up on the first check
    (same patience listen_loop() already has for the main run loop)."""
    try:
        index = find_connected_controller()
        waited = False
        while index is None:
            if not waited:
                print(
                    "No XInput controller detected yet — if you're using "
                    "Steam Input to remap a non-Xbox pad, this can take a "
                    "few seconds to bind after Steam/the game starts. "
                    f"Retrying every {RECONNECT_DELAY_SECONDS}s (Ctrl+C to cancel)..."
                )
                waited = True
            time.sleep(RECONNECT_DELAY_SECONDS)
            index = find_connected_controller()

        print(f"Listening on XInput slot {index} — press the button you want to use "
              f"for the scoreboard toggle (Ctrl+C to cancel)...")
        prev_buttons = 0
        while True:
            state = get_state(index)
            if state is None:
                log.warning("Controller disconnected — waiting for it to reconnect...")
                index = None
                while index is None:
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    index = find_connected_controller()
                print(f"Reconnected on XInput slot {index}.")
                prev_buttons = 0
                continue
            buttons = state.Gamepad.wButtons
            newly_pressed = buttons & ~prev_buttons
            if newly_pressed:
                names = [name for mask, name in BUTTON_NAMES.items() if newly_pressed & mask]
                if names:
                    print(f"\nDetected: {' + '.join(names)}")
                    print(f'Set this in config.json (or via `rl-pfp config`): '
                          f'"scoreboard_button_windows": "{names[0]}"')
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


def listen_loop() -> None:
    config = load_config()
    button_mask, button_name = resolve_button_mask(
        config.get("scoreboard_button_windows") or DEFAULT_BUTTON_NAME
    )
    log.info("Scoreboard toggle button: %s", button_name)

    while True:
        index = find_connected_controller()
        if index is None:
            log.info("No controller connected — retrying in %ss...", RECONNECT_DELAY_SECONDS)
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        log.info("Selected controller at XInput slot %d", index)
        was_pressed = False
        try:
            while True:
                state = get_state(index)
                if state is None:
                    log.warning("Controller disconnected. Reconnecting...")
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    break

                is_pressed = bool(state.Gamepad.wButtons & button_mask)
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
