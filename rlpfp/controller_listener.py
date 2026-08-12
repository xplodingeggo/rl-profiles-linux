#!/usr/bin/env python3
"""
Layer 6: Controller input listener (button -> scoreboard visibility)

Listens for a configurable button on the controller device and POSTs
to the bridge's /scoreboard-visible endpoint in real time:
  - button pressed  -> {"visible": true}
  - button released -> {"visible": false}

Defaults to BTN_THUMBL (L3), but most people actually use Share/Select
instead — set "scoreboard_button" in config.json (via `rl-pfp config`)
to whatever evdev calls your button. Since the exact name varies by
controller and remap (Share, Select, Back, View... all mean different
things depending on hardware and whether Steam Input is remapping to
a virtual Xbox pad), the easiest way to find yours is:

    python3 -m rlpfp.controller_listener --detect

...then just press the button — it'll print its evdev name(s) so you
can copy the right one into config.json.

Since you're using Steam Input to remap the 8BitDo Ultimate 2 (dinput)
to Xbox controls, the button event actually appears on the VIRTUAL
Xbox-mapped device Steam creates, not the raw 8BitDo device. This
script auto-detects that device by name at startup.

Requires:
  pip install evdev requests --break-system-packages

Permissions:
  Reading /dev/input/eventX usually requires being in the 'input'
  group (or running as root, not recommended). Check with:
    groups $USER
  If 'input' isn't listed:
    sudo usermod -aG input $USER
  Then log out/in (or reboot) for the group change to take effect.

Run (alongside rl_stats_bridge.py):
  python3 -m rlpfp.controller_listener
"""

import logging
import time
import urllib.request
import urllib.error
import json

from evdev import InputDevice, categorize, ecodes, list_devices

from .config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("controller-listener")

BRIDGE_URL = "http://127.0.0.1:9090"

DEFAULT_BUTTON_NAME = "BTN_THUMBL"  # L3 — the original default


def resolve_button_code(button_name: str) -> tuple[int, str]:
    """Look up an evdev code by name (e.g. "BTN_SELECT"). Falls back to
    DEFAULT_BUTTON_NAME with a warning if the configured name isn't a
    real evdev code, so a typo in config.json degrades instead of
    crashing the whole listener."""
    code = getattr(ecodes, button_name, None)
    if code is None:
        log.warning(
            "scoreboard_button %r in config.json isn't a real evdev "
            "button name — falling back to %s (L3). Run "
            "`python3 -m rlpfp.controller_listener --detect` and press "
            "your button to find the correct name.",
            button_name, DEFAULT_BUTTON_NAME,
        )
        return getattr(ecodes, DEFAULT_BUTTON_NAME), DEFAULT_BUTTON_NAME
    return code, button_name


def _code_name(code: int) -> str:
    """Reverse lookup for --detect output — evdev.ecodes.keys can map a
    code to a single name or a tuple of aliases; normalize to a string."""
    names = ecodes.keys.get(code, str(code))
    if isinstance(names, tuple):
        return " / ".join(names)
    return names


# Candidate name substrings for the device to listen on, checked in
# order. Steam Input's virtual pad usually shows up as something like
# "Microsoft X-Box 360 pad" or "Steam Virtual Gamepad" — adjust this
# list if auto-detect picks the wrong device (see list_all_devices()).
DEVICE_NAME_CANDIDATES = [
    "Xbox 360",
    "X-Box 360",
    "Steam Virtual",
    "Microsoft X-Box",
    "8BitDo",  # fallback: raw device, in case Steam Input isn't active
]

RECONNECT_DELAY_SECONDS = 5


def list_all_devices() -> list[InputDevice]:
    """Debug helper: prints every input device evdev can see."""
    devices = [InputDevice(path) for path in list_devices()]
    for d in devices:
        log.info("Found device: %s at %s", d.name, d.path)
    return devices


def find_controller_device() -> InputDevice | None:
    devices = [InputDevice(path) for path in list_devices()]
    for candidate in DEVICE_NAME_CANDIDATES:
        for d in devices:
            if candidate.lower() in d.name.lower():
                log.info("Selected device: %s (%s)", d.name, d.path)
                return d
    log.warning(
        "No matching controller device found. Available devices:"
    )
    for d in devices:
        log.warning("  - %s (%s)", d.name, d.path)
    return None


def detect_button() -> None:
    """`--detect` mode: waits for the next button press on the
    auto-detected controller and prints its evdev name(s), so you can
    copy the right value into config.json's scoreboard_button without
    guessing what your controller/remap calls it."""
    device = find_controller_device()
    if device is None:
        log.error("No controller device found — can't detect a button press.")
        return

    print(f"Listening on {device.name} — press the button you want to use "
          f"for the scoreboard toggle (Ctrl+C to cancel)...")
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1:  # press, not release
                name = _code_name(event.code)
                print(f"\nDetected: {name}")
                print(f'Set this in config.json (or via `rl-pfp config`): "scoreboard_button": "{name if " / " not in name else name.split(" / ")[0]}"')
                return
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
    button_code, button_name = resolve_button_code(
        config.get("scoreboard_button") or DEFAULT_BUTTON_NAME
    )
    log.info("Scoreboard toggle button: %s", button_name)

    while True:
        device = find_controller_device()
        if device is None:
            log.info("Retrying device detection in %ss...", RECONNECT_DELAY_SECONDS)
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        try:
            for event in device.read_loop():
                if event.type == ecodes.EV_KEY and event.code == button_code:
                    # value: 1 = pressed, 0 = released, 2 = autorepeat (ignore)
                    if event.value == 1:
                        post_scoreboard_visible(True)
                    elif event.value == 0:
                        post_scoreboard_visible(False)
        except OSError as e:
            # Device disconnected (e.g. controller turned off/Steam
            # Input restarted). Reconnect.
            log.warning("Device disconnected: %s. Reconnecting...", e)
            time.sleep(RECONNECT_DELAY_SECONDS)
        except Exception:
            log.exception("Unexpected error in listen loop")
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    import sys

    if "--list" in sys.argv:
        # Debug mode: just list all devices and exit, so you can see
        # what evdev picks up and adjust DEVICE_NAME_CANDIDATES if needed.
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
