#!/bin/bash
# Watches for Rocket League's actual game process and starts/stops
# `rl-pfp start` to match — run as a systemd --user service so it's
# always alive in the background, independent of Heroic/Steam/however
# RL gets launched, and independent of Hyprland (no hyprctl call here —
# gtk4_overlay.py still uses hyprctl on its own for window geometry,
# that's unrelated). Matches the real Proton process name
# "RocketLeague.exe", not the launcher/wrapper processes around it
# (Launcher.exe, steam.exe, python's waitforexitandrun, etc).

PFP_PID=""

is_rl_running() {
    pgrep -f "RocketLeague\.exe" >/dev/null 2>&1
}

while true; do
    if is_rl_running; then
        if [ -z "$PFP_PID" ] || ! kill -0 "$PFP_PID" 2>/dev/null; then
            rl-pfp start --no-prompt </dev/null >/tmp/rl-pfp-overlay.log 2>&1 &
            PFP_PID=$!
        fi
    else
        if [ -n "$PFP_PID" ] && kill -0 "$PFP_PID" 2>/dev/null; then
            kill "$PFP_PID" 2>/dev/null
            wait "$PFP_PID" 2>/dev/null
            PFP_PID=""
        fi
    fi
    sleep 5
done
