A Hebnix plugin port of [rl-pfp-overlay](https://github.com/xplodingeggo/rl-profiles-linux) — shows Steam/Xbox/PSN profile pictures on Rocket League's real scoreboard, calibrated against the same layout math as the standalone Linux/Windows versions (see the `linux`/`windows` branches).

Runs inside [Hebnix](https://github.com/Hebbins/Hebnix-Public) — no game injection, no separate overlay window/process. Positioning, HTTP resolution, and rendering all happen through Hebnix's own Lua plugin API.

## Install

Drop `pfpoverlay/` into Hebnix's `plugins/` directory (next to `hebnix-app.exe`), then enable it from Hebnix's plugin list.

## Requirements

- A Hebnix build with `hebnix.http_download_async`, `hebnix.plugin_dir()`, `hebnix.http_get_no_redirect_async`, and the `on_http_download_response`/`on_http_redirect_response` callbacks — small patches on top of stock Hebnix needed for binary-safe avatar downloads, writing them to the plugin's own asset folder, and PSN's OAuth redirect exchange. See the comment block at the top of `main.lua` for details.
- Steam Web API key and/or an xbl.io API key, set from the plugin's settings panel, to resolve avatars for those platforms.
- For PSN: an NPSSO value (Settings > PSN in the plugin) — log into playstation.com in a browser, then visit `https://ca.account.sony.com/api/v1/ssocookie` in that same session to get one. One-time setup; the plugin refreshes its own PSN session automatically after that.
- Your Rocket League "Interface Scale" video setting entered in the plugin settings — positioning is calibrated relative to it.

## Status

- Steam, Xbox, and PSN avatar resolution: working.
- Nintendo Switch: not implemented — there's no accessible public API for it (unlike PSN's permissive profile lookup). Use the manual avatar override editor in settings for specific Switch players you care about.
- Bots: tracked (for correct scoreboard row placement) but never resolved to an avatar — RL doesn't expose a real platform account for them.
- Scoreboard positioning: ported from the standalone project's `layout.py`, avatars render at RL's real scoreboard slot coordinates while the configured scoreboard button is held.
- Goal-scored nameplate: shown during the goal replay camera, matching how long RL's own nameplate stays up; hides immediately if the replay is skipped.
- Avatar overrides: a real `overrides.json` file next to `main.lua`, editable via settings (platform dropdown + ID + image path) or directly.
