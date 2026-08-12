
An EAC-compatible profile picture overlay mod which works with linux on wayland desktops :D
PS: Check out [Rocket Spoof](https://github.com/xplodingeggo/rocketspoof) on my GitHub! It let's you spoof your display name on linux for Rocket League to whatever you like ;) There's also a windows version.
--Only works for 1440p 16:9 at 75% Interface scale--. Works at 1440p at 50%, 75% and 100% interface scale. 75% is the most tried and tested and will work the best but 50% and 100% should work fine too. There is also support for other interface scales if you do ```rl-pfp start --ui-scale 69``` The number you put at the end should be the interface scale percentage in game.
 I plan to add support for different interface scales first and then add support for 1080p
<img width="2560" height="1440" alt="screenshot2" src="https://github.com/user-attachments/assets/5d8101b5-5bb9-472f-9f31-47d8ed038f5b" />

<img width="2560" height="1440" alt="screenshot3" src="https://github.com/user-attachments/assets/6fbba6c6-22a4-4437-a38f-f1d6d92b5520" />


## Usage

### Requirements

- A Wayland compositor with **`wlr-layer-shell` support** (Hyprland, Sway, and other wlroots-based compositors). I tested and made this for hyprland on arch linux, hopefully others should work fine but im not sure. Can probably work on KDE Plasma too.
  **Won't run under GNOME, or X11 sessions** — they don't implement this protocol. If you're on Ubuntu/Fedora and this doesn't work, it's probably because the default desktop is GNOME; installing Sway (packaged on most distros) or Hyprland alongside it will get you there. The overlay checks for this on startup and will tell you and exit instead of just crashing
- Python 3.10+
- System packages for the overlay (not pip-installable):

  ```bash
  # Arch
  sudo pacman -S gtk4 gtk4-layer-shell python-gobject
  # Debian/Ubuntu
  sudo apt install libgtk-4-dev gir1.2-gtk-4.0 python3-gi
  ```
- A controller with permission to read `/dev/input/eventX` (usually the `input` group):
  ```bash
  sudo usermod -aG input $USER   # then log out/in
  ```
- Optional: `hyprctl` (ships with Hyprland) is used to only show avatars while Rocket League is the focused window. This is soft — if it's missing (e.g. on Sway), the overlay just stays always-on instead of failing.

### Install

```bash
git clone https://github.com/xplodingeggo/rl-pfp-overlay.git
cd rl-pfp-overlay
pip install -e . --break-system-packages
```

`-e` (editable) means the `rl-pfp` command always runs directly from this checked-out folder — pull updates with `git pull`, no reinstall needed.

If `rl-pfp` isn't found afterward, its script directory likely isn't on your `$PATH`:
```bash
python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```
Add whatever that prints to your shell's `$PATH`.

#### Optional: separate venv for the bridge/controller

`PyGObject` (used by the overlay) usually needs to be system Python, since venvs dont let you install it. If you'd rather keep `aiohttp`/`evdev` isolated in their own venv instead of installing them system-wide, u can do it but u weird asl — see **Configuration** below (`bridge_venv_python`). Install `rl-pfp-overlay` itself under system Python either way.

### Configuration

```bash
rl-pfp config
```

Interactively sets `~/.config/rl-pfp-overlay/config.json`:

| Key                  | Required for                   | Notes                                                                                                                                                             |
| -------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `steam_api_key`      | Steam avatars                  | free at [steamcommunity.com/dev](https://steamcommunity.com/dev)                                                                                                  |
| `psn_npsso`          | PSN avatars                    | free, sign into playstation website then go here https://ca.account.sony.com/api/v1/ssocookie                                                                     |
| `xbox_api_key`       | Xbox avatars                   | free at [xbl.io](https://xbl.io)                                                                                                                                  |
| `bridge_venv_python` | only if splitting environments | path to a venv's `python`, used for the bridge + controller only (you should leave blank)                                                                         |
| scoreboard_button    | controller button toggle       | evdev button name (e.g. `BTN_SELECT` for Share/Select); defaults to `BTN_THUMBL` (L3) if blank. Run `python3 -m rlpfp.controller_listener --detect` to find yours |
| avatar_overrides     | custom pfps                    | set via `rl-pfp config` at the end — no code editing needed                                                                                                       |

Blank/omitted fields just skip that platform's avatars. Env vars `STEAM_API_KEY` / `PSN_NPSSO` / `XBOX_API_KEY` still work and take priority over the config file.

You'll also need the Stats API enabled in Rocket League itself. See the next section.

## Stats API Setup

Before you run `rl-pfp start`, you need to enable Rocket League's Stats API. This is what the overlay uses to know who's in the match and when goals are scored.

Since RL on Linux runs through a Wine/Proton prefix, the config file isn't in the game's install directory — it's inside your prefix, under whichever Windows "user" your setup uses. That varies by launcher:

- If you're running through Steam's Proton runtime, the user is usually `steamuser`.
- If you're running through Heroic, Lutris, or a custom Wine prefix, it might be `steamuser` too (if you set it up that way) or your actual Linux username, depending on how the prefix was created.

Example (mine, via Heroic):

```
/home/xplodingeggo/Games/Heroic/Prefixes/rocketleague/pfx/drive_c/users/steamuser/Documents/My Games/Rocket League/TAGame/Config/
```
I have the steam runtime enabled in heroic which is why it falls under 'steamuser' and not 'xplodingeggo'
The general shape is:

```
<your prefix>/drive_c/users/<user>/Documents/My Games/Rocket League/TAGame/Config/
```
If its not their you can have a look around until you find it. These prefix structures can get confusing.

Open (or create) `TAStatsAPI.ini` in that folder and paste this in:

ini

```ini
[TAGame.MatchStatsExporter_TA]
PacketSendRate=2
Port=49123
WebPort=49124
```

Then **restart Rocket League** — changes only take effect after a full restart.

There's also a `DefaultStatsAPI.ini` sitting in the actual game installation folder itself (not the prefix), e.g.:

```
/mnt/game/Games/rocket/rocketleague/TAGame/Config/
```

If the prefix config above doesn't seem to take effect, try checking/editing that one too. I'm honestly not 100% sure how the two interact, but both exist and both are worth knowing about.

The bridge looks for connections on `127.0.0.1:49123`, so if you use a different port, change `49123` in `rl_stats_bridge.py` too (but you probably don't need to).

If the bridge logs say "Stats API not reachable" or "connection refused," the most common reasons are:

- You didn't restart RL after editing the config
- You edited the config in the wrong prefix/user folder
- RL is running but the Stats API didn't actually start (check your RL logs)
***TL;DR If you have setup bakkesmod before its that same place just in the documents folder instead of AppData***
### Running

```bash
rl-pfp
```

Starts everything (equivalent to `rl-pfp start`): the Stats API bridge, the GTK overlay, and the controller listener, each as its own process. First run will prompt for any missing config keys.

```
rl-pfp start --debug        # overlay debug HUD + verbose bridge logs
rl-pfp start --no-prompt    # skip config prompts, just start with what's set
```

`Ctrl+C` stops all three cleanly. Logs are interleaved in the terminal and also written per-component to `~/.cache/rl-pfp-overlay/logs/`.

Check it from another terminal while it's running:
```bash
rl-pfp status
```

### Running components individually

Useful for debugging one piece in isolation (breakpoints, `strace`, etc.) without the others:

```bash
python3 -m rlpfp.rl_stats_bridge --verbose
python3 -m rlpfp.gtk4_overlay --debug
python3 -m rlpfp.controller_listener --list   # --list: print detected input devices and exit
```

### Calibration note (important)

Scoreboard and goal-replay avatar positions in `gtk4_overlay.py` are calibrated for **2560×1440 at 75% Rocket League UI scale**. I'm just going to hope thats what most of you play at anyways, otherwise I have no idea how to fix it. Maybe I'll do some work on it in a future update (never). On the bright side, im pretty sure the interface scale option scales linearly, so if we just find out the formula then we chillin

# Q&A

## Q: Is this free/open source
 A: Yes. I hate working on this though I have ptsd so i need all the help i can get. You can create forks or branches for your own distro/DE I think that would be good. I'm honestly new to github so i apologize if stuff is not structured right. You can ask me questions on github or discord or wherever and i might be able to help. I didn't code this but I did have to spend hours configuring the overlay to just align correctly

## Q: Which file does what?
A: 
1. rl_stats_bridge - the central glue which holds this vibecoded mess together. It pulls the info from the StatsAPI and allows the other scripts like the pfp resolver and gtk overlay to function properly
2. gtk4_overlay.py - This is probably the main hard part of this project, it renders the profiles 3.5s after a goal is scored or also on the scoreboard after sorting the players from highest to lowest on each team. This is the only way to make sure the positions are correct mid game since its the only thing rocket league uses to determine who is where on the scoreboard and lucky for us, its exposed by StatsAPI. Not so fun fact, if you use --debug when launching this overlay you will have another overlay in the top left which shows all the players in a lobby, how long ago the last goal was scored, and who in the lobby has a PFP and who doesnt
3. controller_listener - this helps us determine when the scoreboard is open. The scoreboard button is by default L3/LS, but its configurable in the config/first time setup.
   When its pressed it will tell the gtk overlay to render the profiles for the scoreboard 
4. pfp_resolver - kinda in the name. Simply uses the API keys you give it to fetch profiles based on the platform/display name exposed from stats API
## Q: I wanna use my own pfp! How do i do it?

**A:** Run `rl-pfp config`, and at the end it'll offer to add a custom avatar override. You need your platform + account ID — join a match first with `rl-pfp start` running, and check `~/.cache/rl-pfp-overlay/logs/bridge.log` for a line like:

```
PlayerJoined: YourName (Epic|61a21e5cbca9481e8b19b944f792d778/0)
```

Plug in the platform (`epic`) and ID, point it at your image, and it's saved to `config.json` — no code editing needed. I plan to add a CDN later at some point if this gets enough users/requests, probably using cloudflare R2 buckets so that you can see other's profiles
## Q: Why can't i see the profile of Switch users?
A: Short answer, Nintendo is gay
Long answer, Nintendo doesn't expose a public API to fetch player avatars from their accounts, unlike Steam (Steam Web API), PlayStation (PSN's profile endpoint), and Xbox (OpenXBL). Without a public endpoint, there's no way to fetch them programmatically. There used to be unofficial apis for nintendo but they are down now after nintendo made some server side changes
## Q: Why is the profiles all over the place at the start of a game?
A: I dont know, but by the time one or 2 of the players on a team get some score it should fix
I know one reason is because your profile can end up on another player and thats because both of your score is zero meaning it can't be compared to find your place on the scoreboard.
## Q: Does it work with avatar borders?
A: It works fine as long as they dont obstruct the main part of the box. Alot of them work fine but some might look weird

## Q: What is the best decal in Rocket League?
Interstellar.

# Whats next/Roadmap
1. Support for different interface scales
2. Support for 1080p
3. fix the orange team push down one row bug[https://github.com/xplodingeggo/rl-profiles-linux/issues/1] 
4. Profile pictures in main menu, after game end, tournament ready up screen - will most likely require new detection methods which i currently dont know yet maybe like text scanning
5. Public CDN for epic games profile pictures (you can see other epic game users people who use this mod)
