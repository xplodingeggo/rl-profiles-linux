A Hebnix plugin port of [rl-profiles-linux](https://github.com/xplodingeggo/rl-profiles-linux/tree/windows) — shows Steam/Xbox/PSN profile pictures on Rocket League's real scoreboard, calibrated against the same layout math as the standalone Windows versions (see the `windows` branch).

Runs inside [Hebnix](https://github.com/Hebbins/Hebnix-Public) - everything uses lua calls with hebnix

## Install

Drop `pfpoverlay/` into Hebnix's `plugins/` directory (next to `hebnix-app.exe`), then enable it from Hebnix's plugin list.

## Requirements
- a pc, with at least a RTX 6090 Ti, Ryzen 420 68000X4D and 2 petabytes of ram
### <u>These are optional and only required if you want to fetch them manually without using the hebnix api</u>
- [Steam Web API key](https://steamcommunity.com/dev/apikey) and/or an [xbl.io](xbl.io) API key, set from the plugin's settings panel, to resolve avatars for those platforms.
- For PSN: an NPSSO value (Settings > PSN in the plugin) — log into [playstation.com](https://www.playstation.com) in a browser, then visit [this](https://ca.account.sony.com/api/v1/ssocookie) in that same session to get one. One-time setup; the plugin refreshes its own PSN session automatically after that as long as you use it at least every 2 months

## Notice
- Switch: not implemented — there's no accessible public API for it (unlike PSN's permissive profile lookup). Use the manual avatar override editor in settings for specific Switch players you care about
- Use the sliders to fix the alignment if it is off. The ones called row0 are the ones you should look for
