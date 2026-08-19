#!/bin/bash
# Watches Hyprland for Rocket League's window and starts/stops
# `rl-pfp start` to match — run as a systemd --user service so it's
# always alive in the background, independent of Heroic/Steam/however
# RL gets launched. Matches the same class/title substrings as
# RL_WINDOW_MATCH_CANDIDATES in rlpfp/gtk4_overlay.py.

PFP_PID=""

is_rl_running() {
    hyprctl clients -j 2>/dev/null \
        | grep -qiE '"class": ?"[^"]*rocket ?league[^"]*"|"title": ?"[^"]*rocket ?league[^"]*"'
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
