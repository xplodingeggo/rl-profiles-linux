# Autostart via systemd (--user service)

Starts/stops the overlay automatically whenever Rocket League's window
appears/disappears in Hyprland — works no matter how RL gets launched
(Heroic, Steam, direct exe), since it watches the window itself instead
of hooking into a specific launcher.

Requires: Hyprland (uses `hyprctl clients -j`), `rl-pfp` already
installed and on PATH (`pip install -e .` from repo root, or however
you normally run it).

## Install

```bash
mkdir -p ~/.local/share/rl-pfp-overlay
cp systemd/rl-pfp-watcher.sh ~/.local/share/rl-pfp-overlay/
chmod +x ~/.local/share/rl-pfp-overlay/rl-pfp-watcher.sh

mkdir -p ~/.config/systemd/user
cp systemd/rl-pfp-watcher.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now rl-pfp-watcher.service
```

That's it — the service is now enabled for every future login, and
already running.

## What it does

`rl-pfp-watcher.sh` polls `hyprctl clients -j` every 5 seconds for a
window whose class/title matches "rocket league" (same match rule as
`RL_WINDOW_MATCH_CANDIDATES` in `rlpfp/gtk4_overlay.py`):

- RL window appears, no overlay running yet -> starts `rl-pfp start
  --no-prompt` in the background.
- RL window disappears, overlay still running -> kills it cleanly
  (SIGTERM, same shutdown path as Ctrl+C).

`--no-prompt` matters here: `rl-pfp start` normally prompts
interactively for any missing config field on first run, but a
systemd service has no TTY — `input()` hits EOF and crashes instead of
waiting for you to type. Run `rl-pfp config` once by hand beforehand
to fill in Steam/PSN/Xbox keys etc.; the service only ever runs
`--no-prompt`, so it will simply skip whatever isn't configured.

Order doesn't matter — start the service before or after RL is
already running, the very next 5-second poll picks up the current
state either way.

## Checking it's working

```bash
systemctl --user status rl-pfp-watcher.service   # service + its own memory use
pgrep -af "rl-pfp start"                          # is the overlay actually up?
tail -f /tmp/rl-pfp-overlay.log                   # bridge/overlay/controller logs
```

## Uninstall

```bash
systemctl --user disable --now rl-pfp-watcher.service
rm ~/.config/systemd/user/rl-pfp-watcher.service
rm ~/.local/share/rl-pfp-overlay/rl-pfp-watcher.sh
systemctl --user daemon-reload
```
