"""
Config discovery/init.

Reuses the exact same file this project already has:
  ~/.config/rl-pfp-overlay/config.json — steam_api_key, psn_npsso,
  xbox_api_key, bridge_venv_python, avatar_overrides,
  epic_placeholder_disabled
Env vars STEAM_API_KEY / PSN_NPSSO / XBOX_API_KEY still take priority,
unchanged (see pfp_resolver.py) — this module doesn't alter that
precedence, it just helps get the file populated in the first place.

`rl-pfp start` calls ensure_config() once before spawning children. If
config.json is missing or a key is unset (and the equivalent env var
isn't set either), it prompts for that key interactively — same spirit
as rl_name_spoof.py's Q&A, but only for what's actually missing, so a
fully-configured setup starts with zero prompts. avatar_overrides isn't
part of that first-run prompt (it's optional and a dict, not a single
value) — it's only offered at the end of `rl-pfp config`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Linux: XDG-style ~/.config and ~/.cache, matching the original tool.
# Windows: %APPDATA% (Roaming) for config, %LOCALAPPDATA% for cache — the
# conventional split (config should roam with the user profile, cache
# shouldn't). Both fall back to Path.home() if the env var is somehow
# unset (rare, but cheaper than crashing over it).
if sys.platform == "win32":
    _APPDATA = Path(os.environ.get("APPDATA") or Path.home())
    _LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    CONFIG_DIR = _APPDATA / "rl-pfp-overlay"
    CACHE_DIR = _LOCALAPPDATA / "rl-pfp-overlay" / "cache"
else:
    CONFIG_DIR = Path.home() / ".config" / "rl-pfp-overlay"
    CACHE_DIR = Path.home() / ".cache" / "rl-pfp-overlay"

CONFIG_PATH = CONFIG_DIR / "config.json"

# (config key, env var, human label, one-line help, prompt on first run?)
# The last flag distinguishes fields worth interrupting a first `rl-pfp
# start` for (API keys — the whole point of running this) from ones
# that already have a sane default and just add a wall of prompts
# every fresh install — those are still fully editable, just via
# `rl-pfp config` instead of nagging on first run.
_FIELDS = [
    (
        "steam_api_key", "STEAM_API_KEY", "Steam Web API key",
        "free at https://steamcommunity.com/dev — leave blank to skip Steam avatars",
        True,
    ),
    (
        "psn_npsso", "PSN_NPSSO", "PSN NPSSO",
        "one-time only; leave blank to skip PSN avatars. resolver.py "
        "auto-bootstraps the actual OAuth tokens from this the first time "
        "it needs a PSN avatar, so nothing further to run here",
        True,
    ),
    (
        "xbox_api_key", "XBOX_API_KEY", "Xbox (OpenXBL / xbl.io) API key",
        "free at https://xbl.io — leave blank to skip Xbox avatars",
        True,
    ),
    (
        "bridge_venv_python", None, "Path to bridge/controller's venv Python",
        "e.g. /home/you/venvs/rlpfp/bin/python — leave blank to use the same "
        "interpreter rl-pfp itself is running under (fine if you don't split "
        "environments). The overlay always uses that same interpreter "
        "rl-pfp runs under too, since PyGObject/gtk4-layer-shell usually "
        "needs system Python, not a venv",
        False,
    ),
    (
        "scoreboard_button", None, "Controller button for scoreboard toggle (Linux)",
        "e.g. BTN_SELECT (Share/Select) — defaults to BTN_THUMBL (L3) if "
        "left blank. Run `python3 -m rlpfp.controller_listener --detect` "
        "and press the button you want to find its exact name first",
        False,
    ),
    (
        "scoreboard_button_windows", None, "Controller button for scoreboard toggle (Windows, XInput)",
        "XInput button name, e.g. BACK (View/Share) — defaults to LEFT_THUMB "
        "(L3) if left blank. Only used when your controller is found via "
        "XInput (a real Xbox pad, or Steam Input set to Xbox emulation). "
        "Run `python -m rlpfp.win_controller --detect` and press the "
        "button you want to find its exact name first",
        False,
    ),
    (
        "scoreboard_button_dinput_index", None, "Controller button index (Windows, DirectInput fallback)",
        "a plain number, e.g. 2 — only used when your controller is NOT "
        "found via XInput (a raw DirectInput/dinput pad, e.g. many "
        "controllers when Steam Input isn't remapping them to Xbox "
        "emulation). Not set by default. Run `python -m rlpfp.win_controller "
        "--detect` and press the button you want to find its index first",
        False,
    ),
    (
        "scoreboard_button_hid", None, "Controller button spec (Windows, raw HID fallback)",
        "'vid:pid:byte_offset:bitmask', e.g. 0x2dc8:0x6012:14:0x10 — only "
        "used when your controller is found by NEITHER XInput nor the "
        "DirectInput fallback (some wireless pads never register with "
        "either). Requires `pip install hidapi`. Not set by default. Run "
        "`python -m rlpfp.win_controller --detect-hid` (interactive — "
        "press+hold the button when prompted) to generate this value",
        False,
    ),
    (
        "rl_ui_scale", "RL_UI_SCALE", "Rocket League 'Interface Scale' video setting",
        "e.g. 0.75 for 75% — must match RL's own setting exactly (Options > "
        "Video), used to scale overlay positions to your resolution. "
        "Leave blank to assume 0.75 (the calibrated default)",
        False,
    ),
]


def _getenv(env: str | None) -> str | None:
    """os.getenv() that tolerates env=None (fields with no env-var
    equivalent, e.g. bridge_venv_python)."""
    return os.getenv(env) if env else None


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def ensure_config(*, interactive: bool = True) -> dict:
    """
    Returns the effective config dict. If interactive, prompts for any
    field that's missing from BOTH the env var and the config file, and
    saves answers back to config.json. Blank input = skip that platform,
    same as leaving the field out entirely.
    """
    config = load_config()
    missing = [
        (key, env, label, help_text)
        for key, env, label, help_text, first_run_prompt in _FIELDS
        if first_run_prompt and not _getenv(env) and not config.get(key)
    ]

    if not missing:
        return config

    if not interactive:
        return config

    print("First-time setup — a few keys aren't configured yet.")
    print(f"(saved to {CONFIG_PATH}; press Enter to skip any of these)\n")

    changed = False
    for key, env, label, help_text in missing:
        value = input(f"{label} ({help_text}): ").strip()
        if value:
            config[key] = value
            changed = True

    if changed:
        _save_config(config)
        print(f"\nSaved to {CONFIG_PATH}. Edit that file directly any time, "
              f"or re-run `rl-pfp config` to update it interactively.\n")
    else:
        print()

    return config


def edit_interactive() -> None:
    """Backing implementation for `rl-pfp config` — always prompts for
    every field (showing current value if set), regardless of what's
    already configured, so it doubles as a way to change existing keys."""
    config = load_config()
    print(f"Editing {CONFIG_PATH}")
    print("(press Enter to keep the current value, or type a new one)\n")

    changed = False
    for key, env, label, help_text, _first_run_prompt in _FIELDS:
        current = config.get(key)
        env_override = _getenv(env)
        if env_override:
            print(f"{label}: currently set via ${env} env var (takes priority; "
                  f"leaving this blank in config.json won't change that)")
        shown = f"[set]" if current else "[not set]"
        value = input(f"{label} {shown} ({help_text}): ").strip()
        if value:
            config[key] = value
            changed = True

    if changed:
        _save_config(config)
        print(f"\nSaved to {CONFIG_PATH}.")
    else:
        print("\nNo changes.")

    _edit_epic_placeholder_interactive(config)
    _edit_avatar_overrides_interactive(config)


def _edit_epic_placeholder_interactive(config: dict) -> None:
    """Epic has no public avatar API, so by default Epic players show a
    generic placeholder image instead of blank. Some people would rather
    just let RL's own built-in default picture show for Epic players —
    this toggles that off/on. Boolean, so it doesn't fit the _FIELDS
    text-input loop above."""
    current = bool(config.get("epic_placeholder_disabled"))
    print("\n--- Epic placeholder image ---")
    print(
        "Epic has no public avatar API, so Epic players show a generic "
        "placeholder image by default. Disable this to fall back to RL's "
        "own built-in default picture instead."
    )
    if current:
        answer = input("Currently disabled (RL default picture used). Re-enable placeholder? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            config["epic_placeholder_disabled"] = False
            _save_config(config)
            print("Saved: Epic placeholder re-enabled.")
    else:
        answer = input("Currently enabled (placeholder shown). Disable placeholder? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            config["epic_placeholder_disabled"] = True
            _save_config(config)
            print("Saved: Epic placeholder disabled.")


def _edit_avatar_overrides_interactive(config: dict) -> None:
    """Custom avatar overrides are a growing dict, not a single value,
    so they don't fit the _FIELDS loop above — handled as their own
    add/remove step instead. See `rl-pfp config` (this is its last
    section) and pfp_resolver.py's docstring for the key format."""
    overrides = config.get("avatar_overrides")
    overrides = dict(overrides) if isinstance(overrides, dict) else {}

    print("\n--- Custom avatar overrides ---")
    print(
        "Force a specific image for a specific platform + account ID "
        "(mainly useful for Epic, which has no public avatar API — but "
        "works for any platform)."
    )
    if overrides:
        print("Currently configured:")
        for key, path in overrides.items():
            print(f"  {key} -> {path}")
    else:
        print("None configured yet.")

    print(
        "\nTo find your platform + account ID: run `rl-pfp start`, join a "
        "match, then check the bridge log (~/.cache/rl-pfp-overlay/logs/"
        "bridge.log) for a line like:\n"
        "  PlayerJoined: YourName (Epic|61a21e5cbca9481e8b19b944f792d778|0)\n"
        "That's platform|account_id|splitscreen — you want the first two, "
        "lowercase the platform.\n"
    )

    changed = False
    while True:
        add = input("Add an override? [y/N]: ").strip().lower()
        if add not in ("y", "yes"):
            break

        platform = input("  Platform (e.g. epic): ").strip().lower()
        if not platform:
            print("  Skipped (no platform entered).")
            continue
        account_id = input("  Account ID: ").strip()
        if not account_id:
            print("  Skipped (no account ID entered).")
            continue
        image_path = input("  Path to image (~/ is fine): ").strip()
        if not image_path:
            print("  Skipped (no path entered).")
            continue

        resolved = Path(image_path).expanduser()
        if not resolved.exists():
            confirm = input(
                f"  Warning: {resolved} doesn't exist (yet). Save anyway? [y/N]: "
            ).strip().lower()
            if confirm not in ("y", "yes"):
                print("  Skipped.")
                continue

        key = f"{platform}|{account_id}"
        overrides[key] = image_path
        changed = True
        print(f"  Added: {key} -> {image_path}")

    if changed:
        config["avatar_overrides"] = overrides
        _save_config(config)
        print(f"\nSaved to {CONFIG_PATH}.")
