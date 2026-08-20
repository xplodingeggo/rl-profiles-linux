"""
rl-pfp — unified CLI entrypoint.

    rl-pfp                    same as `rl-pfp start`
    rl-pfp start [--debug] [--verbose] [--linux-calibration]
                                    run rl_stats_bridge + win_overlay + win_controller
                                    (foreground). --debug: overlay HUD only. --verbose:
                                    bridge per-request access logs (noisy, off by default —
                                    was previously (and confusingly) tied to --debug, which
                                    buried real status lines like the controller's "no
                                    controller connected, retrying" under a wall of HTTP logs).
                                    --linux-calibration: experimental A/B flag, use the original
                                    unmodified Linux calibration constants for this run
    rl-pfp status                query a running bridge, print state, exit
    rl-pfp config                  interactively view/edit config.json (steam/psn/xbox keys,
                                     bridge_venv_python — pfp_resolver.py bootstraps the actual
                                     PSN OAuth tokens automatically on first use)
    rl-pfp grid                    standalone pixel-grid calibration overlay — safe to run
                                     alongside `rl-pfp start`, doesn't touch avatar rendering
    rl-pfp gui                     native control panel — install/configure/start, controller
                                     button detect, Windows-startup toggle (or use rl-pfp-gui.exe,
                                     which opens with no console window)

Each component also still runs completely standalone, unchanged, for
isolated debugging:

    python -m rlpfp.rl_stats_bridge --verbose
    python -m rlpfp.win_overlay --debug
    python -m rlpfp.win_controller --list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import config, supervisor

BRIDGE_URL = "http://127.0.0.1:9090"


def _cmd_start(args: argparse.Namespace) -> int:
    effective_config = config.ensure_config(interactive=not args.no_prompt)
    bridge_python = effective_config.get("bridge_venv_python") or None
    if bridge_python:
        bridge_python = str(Path(bridge_python).expanduser())
        if not Path(bridge_python).exists():
            print(
                f"Warning: bridge_venv_python is set to {bridge_python!r} in "
                f"config.json, but that path doesn't exist. Falling back to "
                f"{sys.executable} for bridge/controller — fix the path with "
                f"`rl-pfp config`.",
                file=sys.stderr,
            )
            bridge_python = None

    ui_scale = args.ui_scale
    if ui_scale is not None and ui_scale > 1:
        ui_scale = ui_scale / 100  # "--ui-scale 75" -> 0.75

    return supervisor.run(
        debug=args.debug, verbose=args.verbose,
        bridge_python=bridge_python, ui_scale=ui_scale,
        linux_calibration=args.linux_calibration,
    )


def _cmd_status(_args: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/current-lobby", timeout=2) as resp:
            lobby = json.loads(resp.read())
        with urllib.request.urlopen(f"{BRIDGE_URL}/pfp-cache-status", timeout=2) as resp:
            cache = json.loads(resp.read())
    except (urllib.error.URLError, OSError):
        print(f"Not reachable at {BRIDGE_URL} — is `rl-pfp start` running?")
        return 1

    players = lobby.get("players", [])
    print(f"Bridge:      {BRIDGE_URL}")
    print(f"Match GUID:  {lobby.get('match_guid') or '(none)'}")
    print(f"Players:     {len(players)}")
    for p in players:
        avatar = "PFP" if p.get("avatar_path") else "none"
        print(f"  - {p.get('name'):<20} {p.get('platform'):<8} avatar={avatar}")
    print(f"Scoreboard visible: {lobby.get('scoreboard_visible')}")
    print(f"Replay:             {lobby.get('is_replay')}")
    last_goal = lobby.get("last_goal")
    print(f"Last goal:          {last_goal.get('scorer_name') if last_goal else '(none)'}")
    print(f"PFP cache:          {cache.get('cached_count')} files at {cache.get('cache_dir')}")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    config.edit_interactive()
    return 0


def _cmd_grid(_args: argparse.Namespace) -> int:
    """Launch the standalone pixel-grid calibration overlay (win_grid_measure.py,
    Tkinter/win32) as its own process. Runs independently, alongside or
    instead of `rl-pfp start`."""
    env = {**os.environ, **supervisor._overlay_env()}
    argv = [sys.executable, "-m", "rlpfp.win_grid_measure"]
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 0


def _cmd_gui(_args: argparse.Namespace) -> int:
    """Launch the native control panel (win_gui.py, Tkinter/ttk). Runs
    in-process (not a subprocess) since it has no other work to do
    first — unlike grid/probe, which run alongside `rl-pfp start`."""
    from . import win_gui
    return win_gui.main()


def _cmd_probe(_args: argparse.Namespace) -> int:
    """Launch the standalone box-position probe overlay (win_box_probe.py,
    Tkinter/win32) as its own process. Runs independently, alongside or
    instead of `rl-pfp start`."""
    env = {**os.environ, **supervisor._overlay_env()}
    argv = [sys.executable, "-m", "rlpfp.win_box_probe"]
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rl-pfp",
        description="RL PFP Overlay — unified CLI",
    )
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="run bridge + overlay + controller (foreground)")
    p_start.add_argument(
        "--debug", action="store_true",
        help="overlay debug HUD (does NOT enable bridge access logs — see --verbose)",
    )
    p_start.add_argument(
        "--verbose", action="store_true",
        help="bridge verbose access logs (one line per HTTP request the overlay makes — "
             "very noisy, ~20/sec; separate from --debug so it doesn't drown out "
             "controller/bridge status messages in the terminal)",
    )
    p_start.add_argument(
        "--no-prompt", action="store_true",
        help="don't prompt for missing config keys, just start with what's there",
    )
    p_start.add_argument(
        "--ui-scale", type=float, default=None,
        help="override rl_ui_scale for this run (e.g. 75 or 0.75 both mean 75%%), "
             "passed to the overlay only — doesn't touch config.json",
    )
    p_start.add_argument(
        "--linux-calibration", action="store_true",
        help="experimental: use the ORIGINAL, unmodified Linux calibration "
             "constants instead of the Windows-patched ones, for this run only "
             "(A/B testing whether the Windows-specific row0/orange/nameplate "
             "patches were actually needed — see layout.py's "
             "_load_use_linux_calibration() docstring)",
    )
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="query a running bridge and print state")
    p_status.set_defaults(func=_cmd_status)

    p_config = sub.add_parser("config", help="view/edit config.json interactively")
    p_config.set_defaults(func=_cmd_config)

    p_grid = sub.add_parser(
        "grid",
        help="run the standalone pixel-grid calibration overlay (safe alongside `start`)",
    )
    p_grid.set_defaults(func=_cmd_grid)

    p_gui = sub.add_parser("gui", help="launch the native control panel")
    p_gui.set_defaults(func=_cmd_gui)

    p_probe = sub.add_parser(
        "probe",
        help="interactive box-position probe — nudge x/y/w/h live and read off pixel values (safe alongside `start`)",
    )
    p_probe.set_defaults(func=_cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # Bare `rl-pfp` with no subcommand = `rl-pfp start`, per the earlier
    # discussion — the common case shouldn't need typing "start" every time.
    if not argv:
        argv = ["start"]

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
