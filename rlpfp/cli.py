"""
rl-pfp — unified CLI entrypoint.

    rl-pfp                    same as `rl-pfp start`
    rl-pfp start [--debug]     run rl_stats_bridge + gtk4_overlay + controller_listener (foreground)
    rl-pfp status                query a running bridge, print state, exit
    rl-pfp config                  interactively view/edit config.json (steam/psn/xbox keys,
                                     bridge_venv_python — pfp_resolver.py bootstraps the actual
                                     PSN OAuth tokens automatically on first use)
    rl-pfp grid                    standalone pixel-grid calibration overlay — safe to run
                                     alongside `rl-pfp start`, doesn't touch avatar rendering

Each component also still runs completely standalone, unchanged, for
isolated debugging:

    python3 -m rlpfp.rl_stats_bridge --verbose
    python3 -m rlpfp.gtk4_overlay --debug
    python3 -m rlpfp.controller_listener --list
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
        debug=args.debug, verbose=args.debug,
        bridge_python=bridge_python, ui_scale=ui_scale,
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
    """Launch the standalone pixel-grid calibration overlay (grid_measure.py)
    as its own process, with the same LD_PRELOAD auto-detection `start`
    uses for the avatar overlay — runs independently, alongside or
    instead of `rl-pfp start`."""
    env = {**os.environ, **supervisor._overlay_env()}
    argv = [sys.executable, "-m", "rlpfp.grid_measure"]
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 0


def _cmd_probe(_args: argparse.Namespace) -> int:
    """Launch the standalone box-position probe overlay (box_probe.py) as
    its own process, with the same LD_PRELOAD auto-detection `start` uses
    — runs independently, alongside or instead of `rl-pfp start`."""
    env = {**os.environ, **supervisor._overlay_env()}
    argv = [sys.executable, "-m", "rlpfp.box_probe"]
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
        help="overlay debug HUD + bridge verbose access logs",
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
