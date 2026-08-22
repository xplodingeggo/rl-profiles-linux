A Hebnix plugin port of [rl-pfp-overlay](https://github.com/xplodingeggo/rl-profiles-linux) — shows Steam/Xbox (PSN pending) profile pictures on Rocket League's real scoreboard, calibrated against the same layout math as the standalone Linux/Windows versions (see the `linux`/`windows` branches).

Runs inside [Hebnix](https://github.com/Hebbins/Hebnix-Public) — no game injection, no separate overlay window/process. Positioning, HTTP resolution, and rendering all happen through Hebnix's own Lua plugin API.

## Install

Drop `pfpoverlay/` into Hebnix's `plugins/` directory (next to `hebnix-app.exe`), then enable it from Hebnix's plugin list.

## Requirements

- A Hebnix build with `hebnix.http_download_async`, `hebnix.plugin_dir()`, and the `on_http_download_response` callback — these are small patches on top of stock Hebnix needed for binary-safe avatar downloads and writing them to the plugin's own asset folder. See the comment block at the top of `main.lua` for details.
- Steam Web API key and/or an xbl.io API key, set from the plugin's settings panel, to resolve avatars for those platforms.
- Your Rocket League "Interface Scale" video setting entered in the plugin settings — positioning is calibrated relative to it.

## Status

- Steam and Xbox avatar resolution: working.
- PSN: not implemented — pending a decision on handling the OAuth redirect `Location` header.
- Bots: tracked (for correct scoreboard row placement) but never resolved to an avatar — RL doesn't expose a real platform account for them.
- Scoreboard positioning: ported from the standalone project's `layout.py`, avatars render at RL's real scoreboard slot coordinates while the configured scoreboard button is held.
