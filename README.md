An EAC-compatible profile picture overlay mod for Rocket League — Windows 11 port :D
PS: Check out [Rocket Spoof](https://github.com/xplodingeggo/rocketspoof) on my GitHub! It let's you spoof your display name for Rocket League to whatever you like ;) There's a Windows version there too.
Works on any 16:9 display with any interface scale. I plan to add support for ultrawide soon.

### Calibration note (important)

For the interface scale, if you use any other scale other than 75%, make sure you start the tool like this:
```rl-pfp start --ui-scale 67``` The number at the end should be your interface scale percentage in game
<img width="2560" height="1440" alt="screenshot2" src="https://github.com/user-attachments/assets/5d8101b5-5bb9-472f-9f31-47d8ed038f5b" />

<img width="2560" height="1440" alt="screenshot3" src="https://github.com/user-attachments/assets/6fbba6c6-22a4-4437-a38f-f1d6d92b5520" />


## Usage

Renders profile pictures on top of Rocket League using a transparent, click-through overlay window (Tkinter + Win32) — no game injection, EAC-safe, just a window drawn on top. Controller input (for the scoreboard toggle) is read via XInput.

### Requirements

- Windows 10/11, Python 3.10+ (from [python.org](https://www.python.org/) — check "Add python.exe to PATH" during install)
- An XInput-compatible controller for the scoreboard-toggle button (a real Xbox pad, or anything remapped to an XInput virtual pad — e.g. via Steam Input). DirectInput-only controllers with no XInput remap aren't supported yet.
- No admin rights, no system packages — `pywin32` and `Pillow` install via pip like anything else.

### Install

```powershell
git clone https://github.com/xplodingeggo/rl-pfp-overlay.git
cd rl-pfp-overlay
pip install -e .
```

`-e` (editable) means the `rl-pfp` command always runs directly from this checked-out folder — pull updates with `git pull`, no reinstall needed.

If `rl-pfp` isn't found afterward, its script directory likely isn't on your `PATH`:
```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```
Add whatever that prints to your `PATH`. (The python.org installer's "Add python.exe to PATH" checkbox handles this for you on a fresh install.)

### Configuration

```powershell
rl-pfp config
```

Interactively sets `%APPDATA%\rl-pfp-overlay\config.json`:

| Key                  | Required for                   | Notes                                                                                                                                                             |
| -------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `steam_api_key`      | Steam avatars                  | free at [steamcommunity.com/dev](https://steamcommunity.com/dev)                                                                                                  |
| `psn_npsso`          | PSN avatars                    | free, sign into playstation website then go here https://ca.account.sony.com/api/v1/ssocookie                                                                     |
| `xbox_api_key`       | Xbox avatars                   | free at [xbl.io](https://xbl.io)                                                                                                                                  |
| `scoreboard_button_windows` | controller button toggle (XInput) | XInput button name — e.g. `LEFT_THUMB` (L3, the default), `BACK` (View/Share), `A`/`B`/`X`/`Y`, `START`, `LEFT_SHOULDER`/`RIGHT_SHOULDER`, `DPAD_UP`/`DOWN`/`LEFT`/`RIGHT`. Used when your controller is found via XInput (a real Xbox pad, or Steam Input set to Xbox emulation) |
| `scoreboard_button_dinput_index` | controller button toggle (DirectInput fallback) | a plain number, e.g. `2`. Used when your controller is NOT found via XInput — e.g. Steam Input only remaps input for the specific game it's actively hooking, not system-wide, so a raw DirectInput pad it isn't remapping falls back to this. Not set by default |
| `scoreboard_button_hid` | controller button toggle (raw HID fallback) | `"vid:pid:byte_offset:bitmask"`, e.g. `"0x2dc8:0x6012:14:0x10"`. Used when your controller is found by NEITHER of the above — some wireless pads (2.4GHz dongle or Bluetooth) never register with XInput or the legacy DirectInput API at all. Requires `pip install hidapi`. Not set by default — run `python -m rlpfp.win_controller --detect-hid` (interactive: it'll ask you to press+hold the button) to generate this value |
| `rl_ui_scale`        | anyone not on 75% interface scale | same thing as `--ui-scale` above but saved so you don't have to type it every launch. Must match RL's Options > Video "Interface Scale" exactly, e.g. `0.75`      |
| `epic_placeholder_disabled` | Epic players you'd rather show RL's default pic | Epic has no public avatar API so it shows a placeholder image by default — set this to skip it and let RL's built-in default show instead. Toggle it from `rl-pfp config` |
| avatar_overrides     | custom pfps                    | set via `rl-pfp config` at the end — no code editing needed                                                                                                       |

Blank/omitted fields just skip that platform's avatars. Env vars `STEAM_API_KEY` / `PSN_NPSSO` / `XBOX_API_KEY` still work and take priority over the config file.

You'll also need the Stats API enabled in Rocket League itself. See the next section.

## Stats API Setup

Before you run `rl-pfp start`, you need to enable Rocket League's Stats API. This is what the overlay uses to know who's in the match and when goals are scored.
Find the `DefaultStatsAPI.ini` which is in the actual game installation folder, e.g.:

```
# Steam
C:\Program Files (x86)\Steam\steamapps\common\rocketleague\TAGame\Config\

# Epic Games
C:\Program Files\Epic Games\rocketleague\TAGame\Config\
```
change it to this

```ini
[TAGame.MatchStatsExporter_TA]
PacketSendRate=2
Port=49123
WebPort=49124
```

Then **restart Rocket League** — changes only take effect after a full restart.

The bridge looks for connections on `127.0.0.1:49123`, so if you use a different port, change `49123` in `rl_stats_bridge.py` too (but you probably don't need to).

If the bridge logs say "Stats API not reachable" or "connection refused," the most common reasons are:

- You didn't restart RL after editing the config
- RL is running but the Stats API didn't actually start (check your RL logs)

### Running

```powershell
rl-pfp
```

Starts everything (equivalent to `rl-pfp start`): the Stats API bridge, the overlay, and the controller listener, each as its own process. First run will prompt for any missing config keys.

```
rl-pfp start --debug        # overlay debug HUD + verbose bridge logs
rl-pfp start --no-prompt    # skip config prompts, just start with what's set
```

`Ctrl+C` stops all three cleanly. Logs are interleaved in the terminal and also written per-component to `%LOCALAPPDATA%\rl-pfp-overlay\cache\logs\`.

Check it from another terminal while it's running:
```powershell
rl-pfp status
```

### Running components individually

Useful for debugging one piece in isolation without the others:

```powershell
python -m rlpfp.rl_stats_bridge --verbose
python -m rlpfp.win_overlay --debug
python -m rlpfp.win_controller --list     # --list: print connected XInput controllers and exit
python -m rlpfp.win_controller --detect   # --detect: press a button, prints its XInput name
```

### Calibration tools

`rl-pfp grid` and `rl-pfp probe` run standalone overlays for re-checking/nudging slot positions if alignment ever looks off on your setup — safe to run alongside `rl-pfp start`, they don't touch avatar rendering. Slot positions live in `rlpfp/layout.py`.

# Q&A

## Q: Is this free/open source
 A: Yes. You can create forks or branches for your own setup, I think that would be good. I'm honestly new to github so i apologize if stuff is not structured right. You can ask me questions on github or discord or wherever and i might be able to help. I didn't code this but I did have to spend hours configuring the overlay to just align correctly and understanding how StatsAPI works

## Q: Which file does what?
A: 
1. rl_stats_bridge - the central glue which holds this vibecoded mess together. It pulls the info from the StatsAPI and allows the other scripts like the pfp resolver and overlay to function properly
2. win_overlay.py - This is probably the main hard part of this project, it renders the profiles 3.5s after a goal is scored or also on the scoreboard after sorting the players from highest to lowest on each team. This is the only way to make sure the positions are correct mid game since its the only thing rocket league uses to determine who is where on the scoreboard and lucky for us, its exposed by StatsAPI. Not so fun fact, if you use --debug when launching this overlay you will have another overlay in the top left which shows all the players in a lobby, how long ago the last goal was scored, and who in the lobby has a PFP and who doesnt
3. win_controller.py - this helps us determine when the scoreboard is open. Tries three backends in order until one finds your controller: XInput (real Xbox pads, or Steam Input set to Xbox emulation) -> Windows' legacy DirectInput-based joystick API (sees raw controllers directly, no Steam Input remap needed) -> raw HID reports (for pads that don't register with either of the above at all — confirmed necessary for some wireless controllers on their 2.4GHz dongle, e.g. an 8BitDo Ultimate 2 Wireless; needs `pip install hidapi`). Steam Input only remaps input for the specific game it's actively hooking, not system-wide, which is why this needed 3 fallback layers instead of 1. The scoreboard button is by default L3/LS, but its configurable in the config/first time setup.
   When its pressed it will tell the overlay to render the profiles for the scoreboard 
4. pfp_resolver - kinda in the name. Simply uses the API keys you give it to fetch profiles based on the platform/display name exposed from stats API
## Q: I wanna use my own pfp! How do i do it?

**A:** Run `rl-pfp config`, and at the end it'll offer to add a custom avatar override. You need your platform + account ID — join a match first with `rl-pfp start` running, and check `%LOCALAPPDATA%\rl-pfp-overlay\cache\logs\bridge.log` for a line like:

```
PlayerJoined: YourName (Epic|61a21e5cbca9481e8b19b944f792d778/0)
```

Plug in the platform (`epic`) and ID, point it at your image, and it's saved to `config.json` — no code editing needed. I plan to add a CDN later at some point if this gets enough users/requests, probably using cloudflare R2 buckets so that you can see other's profiles
## Q: Why can't i see the profile of Switch users?
A: Short answer, Nintendo is bitch
Long answer, Nintendo doesn't expose a public API to fetch player avatars from their accounts, unlike Steam (Steam Web API), PlayStation (PSN's profile endpoint), and Xbox (OpenXBL). Without a public endpoint, there's no way to fetch them programmatically. There used to be unofficial apis for nintendo but they are down now after nintendo made some server side changes
## Q: Why is the profiles all over the place at the start of a game?
A: I dont know, but by the time one or 2 of the players on a team get some score it should fix
I know one reason is because your profile can end up on another player and thats because both of your score is zero meaning it can't be compared to find your place on the scoreboard.
## Q: Does it work with avatar borders?
A: It works fine as long as they dont obstruct the main part of the box. Alot of them work fine but some might look weird

## Q: What is the best decal in Rocket League?
Interstellar.

# Whats next/Roadmap

1. Profile pictures in main menu, after game end, tournament ready up screen - will most likely require new detection methods which i currently dont know yet maybe like text scanning
2. Public CDN for epic games profile pictures (you can see other epic game users people who use this mod)
