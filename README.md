A Hebnix plugin port of [rl-pfp-overlay](https://github.com/xplodingeggo/rl-profiles-linux) — shows Steam/Xbox/PSN profile pictures on Rocket League's real scoreboard, calibrated against the same layout math as the standalone Windows versions (see the `windows` branch).

Runs inside [Hebnix](https://github.com/Hebbins/Hebnix-Public) - everything uses lua calls with hebnix

## Install

Drop `pfpoverlay/` into Hebnix's `plugins/` directory (next to `hebnix-app.exe`), then enable it from Hebnix's plugin list.

## Requirements
- Your Rocket League "Interface Scale" video setting entered in the plugin settings — positioning is calibrated relative to it.
### <u>These are optional and only required if you want to fetch them manually without using tracker</u>
- [Steam Web API key](https://steamcommunity.com/dev/apikey) and/or an [xbl.io](xbl.io) API key, set from the plugin's settings panel, to resolve avatars for those platforms.
- For PSN: an NPSSO value (Settings > PSN in the plugin) — log into [playstation.com](https://www.playstation.com) in a browser, then visit [this](https://ca.account.sony.com/api/v1/ssocookie) in that same session to get one. One-time setup; the plugin refreshes its own PSN session automatically after that as long as you use it at least every 2 months

## Status

- Steam, Xbox, and PSN avatar resolution: working.
- Nintendo Switch: not implemented — there's no accessible public API for it (unlike PSN's permissive profile lookup). Use the manual avatar override editor in settings for specific Switch players you care about.
- Bots: tracked (for correct scoreboard row placement) but never resolved to an avatar — RL doesn't expose a real platform account for them.
- Scoreboard positioning: ported from the standalone project's `layout.py`, avatars render at RL's real scoreboard slot coordinates while the configured scoreboard button is held.
- Goal-scored nameplate: shown during the goal replay camera, matching how long RL's own nameplate stays up; hides immediately if the replay is skipped.
- Avatar overrides: a real `overrides.json` file next to `main.lua`, editable via settings (platform dropdown + ID + image path) or directly insdie the UI
