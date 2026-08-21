#!/usr/bin/env python3
"""
Windows overlay window — Tkinter + Win32, no GTK/Wayland involved.

Same job as gtk4_overlay.py, ported: renders player profile pictures on
top of Rocket League while it's the focused window.
  1. Goal-scored nameplate avatar — shown for a few seconds after
     the GoalScored event, at the "SCORED BY" nameplate position.
  2. Scoreboard avatars — shown next to each player row while the
     scoreboard is visible (driven by win_controller.py's button press).

Slot positions come from layout.py — the SAME calibration data
gtk4_overlay.py uses, measured on Linux at 2560x1440/75% UI scale. RL is
the same game engine/UI on both OSes, so those reference pixel positions
should transfer directly; only the window-geometry input differs (win32
GetWindowRect here vs hyprctl there). If alignment looks off on your
setup, use `rl-pfp probe` / `rl-pfp grid` (win32 versions) to check.

How the window works, since Tkinter alone can't do this:
  - Borderless, always-on-top, sized to the virtual screen.
  - `-transparentcolor` (a Windows-only Tk attribute) makes one exact
    RGB color fully transparent — we pick an color so unlikely to occur
    in a real avatar that a false-transparent pixel is a non-issue
    (TRANSPARENT_KEY below), and composite every avatar image onto a
    background of that color before displaying it (see _load_avatar).
  - WS_EX_LAYERED | WS_EX_TRANSPARENT (set via ctypes after the window
    exists) makes the ENTIRE window click-through, so mouse input always
    reaches Rocket League underneath — same effect as the empty
    cairo.Region() input-region trick on Linux.

Requires:
  pip install pywin32 Pillow requests
  (Pillow: for RGBA compositing avatar images onto the transparent-color
  background; pywin32: win32gui/win32con for the click-through + focus/
  geometry win32 calls; Tkinter itself ships with the standard Windows
  Python installer.)

Run (with rl_stats_bridge.py already running):
  python -m rlpfp.win_overlay
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageSequence, ImageTk

import win32api
import win32con
import win32gui

from .layout import (
    GOAL_NAMEPLATE_DURATION_SECONDS,
    GOAL_NAMEPLATE_DELAY_SECONDS,
    AVATAR_INSET_PX,
    window_geometry_state,
    get_scoreboard_slots,
    get_goal_nameplate_slot,
    BridgeState,
    FocusState,
    poll_bridge_loop,
    UI_SCALE,
)

DEBUG_MODE = False  # set by --debug CLI flag in main(), before mainloop()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("win-overlay")

# A color vanishingly unlikely to appear in a real avatar photo (pure,
# fully-saturated magenta) — every pixel this exact color becomes
# invisible AND click-through. Avatar images are composited onto a
# background of this color (see _load_avatar) so their transparent/
# rounded-corner regions vanish the same way GTK's alpha compositing did
# on Linux, just via a color-key instead of true per-pixel alpha.
TRANSPARENT_KEY = "#ff00fe"

# Some GIFs declare a 0ms (or otherwise absurdly short) frame duration —
# floor it so an animated avatar can't peg a CPU core redrawing every tick.
MIN_GIF_FRAME_MS = 20

bridge_state = BridgeState()
focus_state = FocusState()

# Substrings to match against the foreground window's title (case-
# insensitive). Rocket League's window title varies slightly by how
# it's launched (Steam/Epic) — if focus detection isn't working, check
# what win32gui.GetWindowText(win32gui.GetForegroundWindow()) reports
# while RL is focused and add that string here.
RL_WINDOW_MATCH_CANDIDATES = ["rocket league"]


def poll_focus_loop() -> None:
    """Runs in a background thread; polls the Win32 foreground window
    and updates focus_state + window_geometry_state. Never touches
    Tkinter directly (Tk isn't thread-safe — the main thread's
    after()-driven tick reads these instead)."""
    logged_unmatched = set()
    while True:
        try:
            hwnd = win32gui.GetForegroundWindow()
            title = (win32gui.GetWindowText(hwnd) or "").lower() if hwnd else ""
            is_rl = any(candidate in title for candidate in RL_WINDOW_MATCH_CANDIDATES)
            focus_state.set(is_rl)

            if is_rl:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                w, h = right - left, bottom - top
                if w > 0 and h > 0:
                    geometry = (left, top, w, h)
                    if window_geometry_state.get() != geometry:
                        log.info("RL window geometry: %s", geometry)
                        window_geometry_state.set(geometry)

            if not is_rl and title not in logged_unmatched:
                log.info("Foreground window (not matched as RL): title=%r", title)
                logged_unmatched.add(title)
        except Exception:
            log.exception("Error polling foreground window")
        time.sleep(1.0)


def _make_click_through(hwnd: int) -> None:
    """Add WS_EX_LAYERED | WS_EX_TRANSPARENT so the whole window ignores
    mouse input — clicks always fall through to Rocket League or
    whatever's underneath, regardless of focus state. Equivalent to the
    empty cairo.Region() trick on the GTK/Wayland side.

    Tkinter's own `-transparentcolor` already sets WS_EX_LAYERED and a
    colorkey via SetLayeredWindowAttributes. SetWindowLong touching
    GWL_EXSTYLE resets that colorkey as a side effect (a Windows quirk,
    not documented but reliably reproducible) — without re-applying it
    below, the window silently reverts to fully opaque and paints solid
    black over everything instead of being transparent."""
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    win32gui.SetWindowLong(
        hwnd, win32con.GWL_EXSTYLE,
        ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT,
    )
    r = int(TRANSPARENT_KEY[1:3], 16)
    g = int(TRANSPARENT_KEY[3:5], 16)
    b = int(TRANSPARENT_KEY[5:7], 16)
    win32gui.SetLayeredWindowAttributes(hwnd, win32api.RGB(r, g, b), 0, win32con.LWA_COLORKEY)
    # Force DWM to recomposite with the colorkey immediately — without
    # this, the window can render one (or more) fully opaque black
    # frames before the compositor catches up, which is a race, not
    # deterministic: it can appear to "stick" black depending on timing.
    win32gui.RedrawWindow(
        hwnd, None, None,
        win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW | win32con.RDW_ALLCHILDREN,
    )
    log.info("Click-through enabled (WS_EX_LAYERED | WS_EX_TRANSPARENT).")


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the full virtual screen, spanning all monitors —
    matches gtk4_overlay's full-anchor layer-shell surface, so slots
    computed from RL's window geometry (which may not be at (0,0) on a
    multi-monitor setup) land in the right place."""
    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return x, y, w, h


class Overlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        # cache_key ("path|WxH") -> list of (PhotoImage, duration_ms) —
        # a static image is just a 1-frame list, so the rest of the
        # overlay doesn't need to know or care which it's showing.
        self._frames_cache: dict[str, list[tuple[ImageTk.PhotoImage, int]]] = {}
        self._avatar_labels: dict[str, tk.Label] = {}
        # widget_id -> {"key": cache_key, "frame": int, "next_at": float}
        # — drives per-widget GIF playback independently of each other
        # (goal nameplate and each scoreboard slot can be on different
        # frames/GIFs at once) and independently of the 50ms bridge-poll
        # tick, since GIF frame durations rarely line up with that.
        self._anim_state: dict[str, dict] = {}
        self._last_logged_positions: dict[str, tuple] = {}
        self._last_logged_team_size = None

        screen_x, screen_y, screen_w, screen_h = _virtual_screen_rect()
        self._origin = (screen_x, screen_y)

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=TRANSPARENT_KEY)
        root.attributes("-transparentcolor", TRANSPARENT_KEY)
        root.geometry(f"{screen_w}x{screen_h}+{screen_x}+{screen_y}")

        self.debug_label = None
        if DEBUG_MODE:
            self.debug_label = tk.Label(
                root, text="waiting for bridge...", justify="left",
                anchor="nw", bg="black", fg="#00ff00",
                font=("Consolas", 10), padx=8, pady=8,
            )
            self.debug_label.place(x=10, y=10)

        # Click-through must be applied after the window's first real
        # paint, not just after it exists — a 0ms `after()` fires before
        # Tk's own -transparentcolor setup and initial compositing have
        # actually landed, which raced against our win32 calls and could
        # leave the window stuck fully opaque black. 150ms is enough
        # margin for that first paint to land before we touch styles.
        root.after(150, self._enable_click_through)
        root.after(50, self._tick)

    def _enable_click_through(self) -> None:
        self.root.update()  # full update, not just idletasks — flush the initial paint
        hwnd = self.root.winfo_id()
        # winfo_id() on a Tk toplevel returns the window's own HWND on
        # Windows (unlike X11, no separate "frame" reparenting to chase).
        try:
            _make_click_through(hwnd)
        except Exception:
            log.exception("Could not enable click-through")

    def _process_frame(self, frame: Image.Image, w: int, h: int) -> Image.Image:
        """Composite one raw frame onto a TRANSPARENT_KEY background so any
        alpha/rounded-corner regions vanish through the color-key instead
        of showing a black/white box. Shared by static images and every
        frame of an animated GIF."""
        src = frame.convert("RGBA")
        inset = AVATAR_INSET_PX
        inner_w, inner_h = max(1, w - 2 * inset), max(1, h - 2 * inset)
        # COVER fit: scale to fill, center-crop the overflow — matches
        # Gtk.ContentFit.COVER used on the Linux side.
        src_ratio = src.width / src.height
        dst_ratio = inner_w / inner_h
        if src_ratio > dst_ratio:
            scale_h = inner_h
            scale_w = round(inner_h * src_ratio)
        else:
            scale_w = inner_w
            scale_h = round(inner_w / src_ratio)
        resized = src.resize((max(1, scale_w), max(1, scale_h)), Image.LANCZOS)
        left = (resized.width - inner_w) // 2
        top = (resized.height - inner_h) // 2
        cropped = resized.crop((left, top, left + inner_w, top + inner_h))

        # -transparentcolor can only key out an EXACT pixel match, not
        # partial transparency. Some avatars (mostly PSN ones, and GIFs)
        # have real anti-aliased alpha edges — alpha_composite would blend
        # those semi-transparent edge pixels with TRANSPARENT_KEY,
        # producing a pixel that's neither fully opaque nor an exact
        # key match, which shows up as a pink fringe/outline around
        # the avatar instead of vanishing. Binarizing the alpha first
        # guarantees every pixel is either fully avatar or fully key.
        r, g, b, a = cropped.split()
        a = a.point(lambda v: 255 if v >= 128 else 0)
        cropped = Image.merge("RGBA", (r, g, b, a))

        bg = Image.new("RGBA", (inner_w, inner_h), TRANSPARENT_KEY)
        bg.alpha_composite(cropped)
        return bg.convert("RGB")

    def _load_avatar_frames(self, path: str, w: int, h: int) -> list[tuple[ImageTk.PhotoImage, int]] | None:
        """Load + cache an avatar's frames at the given size. A static
        image (or a GIF with only one frame) comes back as a 1-element
        list; an animated GIF comes back with one entry per frame, each
        paired with that frame's display duration in ms. Cached by
        (path, w, h) — re-composited whenever the requested slot size
        changes (e.g. UI scale)."""
        cache_key = f"{path}|{w}x{h}"
        if cache_key in self._frames_cache:
            return self._frames_cache[cache_key]
        try:
            src = Image.open(path)
            frames: list[tuple[ImageTk.PhotoImage, int]] = []
            if getattr(src, "is_animated", False) and src.n_frames > 1:
                for frame in ImageSequence.Iterator(src):
                    duration = max(MIN_GIF_FRAME_MS, frame.info.get("duration", 100))
                    processed = self._process_frame(frame, w, h)
                    frames.append((ImageTk.PhotoImage(processed), duration))
            else:
                processed = self._process_frame(src, w, h)
                frames.append((ImageTk.PhotoImage(processed), 0))
            self._frames_cache[cache_key] = frames
            return frames
        except Exception as e:
            log.warning("Failed to load avatar image %s: %s", path, e)
            return None

    def _place_avatar(self, widget_id: str, avatar_path: str, x: int, y: int, w: int, h: int) -> None:
        cache_key = f"{avatar_path}|{w}x{h}"

        label = self._avatar_labels.get(widget_id)
        if label is None:
            label = tk.Label(self.root, bd=0, highlightthickness=0, bg=TRANSPARENT_KEY)
            self._avatar_labels[widget_id] = label

        state = self._anim_state.get(widget_id)
        if state is None or state["key"] != cache_key:
            # New image (or first time shown, or slot resized) for this
            # widget — (re)load its frames and start animation over from
            # frame 0, rather than mid-cycle, so a re-placed GIF doesn't
            # jump around.
            frames = self._load_avatar_frames(avatar_path, w, h)
            if not frames:
                self._hide_avatar(widget_id)
                return
            photo, duration = frames[0]
            state = {
                "key": cache_key,
                "frame": 0,
                "next_at": time.time() + duration / 1000.0 if duration else None,
            }
            self._anim_state[widget_id] = state
            label.configure(image=photo)
            label.image = photo  # keep a reference; Tk drops GC'd PhotoImages

        inset = AVATAR_INSET_PX
        rel_x = x - self._origin[0] + inset
        rel_y = y - self._origin[1] + inset
        label.place(x=rel_x, y=rel_y, width=w - 2 * inset, height=h - 2 * inset)

    def _hide_avatar(self, widget_id: str) -> None:
        label = self._avatar_labels.get(widget_id)
        if label is not None:
            label.place_forget()
        self._anim_state.pop(widget_id, None)

    def _advance_animations(self) -> None:
        """Step every currently-placed GIF avatar forward to whichever
        frame its duration says should be showing now. Runs every tick
        (50ms) independently of the bridge-poll data, since GIF frame
        durations rarely divide evenly into that."""
        now = time.time()
        for widget_id, state in self._anim_state.items():
            if state["next_at"] is None or now < state["next_at"]:
                continue  # static image (duration 0), or not due yet
            frames = self._frames_cache.get(state["key"])
            if not frames or len(frames) <= 1:
                continue
            label = self._avatar_labels.get(widget_id)
            if label is None or not label.winfo_ismapped():
                continue
            state["frame"] = (state["frame"] + 1) % len(frames)
            photo, duration = frames[state["frame"]]
            label.configure(image=photo)
            label.image = photo
            state["next_at"] = now + duration / 1000.0

    def _tick(self) -> None:
        state = bridge_state.get()
        players = state.get("players", [])
        scoreboard_visible = state.get("scoreboard_visible", False)
        last_goal = state.get("last_goal")
        is_replay = state.get("is_replay", False)

        if self.debug_label is not None:
            avatar_summary = ", ".join(
                f"{p.get('name')}={'PFP' if p.get('avatar_path') else 'none'}"
                for p in players
            ) or "(no players)"
            goal_age = f"{time.time() - last_goal['timestamp']:.1f}s ago" if last_goal else "none"
            geometry = window_geometry_state.get()
            self.debug_label.configure(text=(
                f"players: {len(players)} | scoreboard_visible: {scoreboard_visible}\n"
                f"last_goal: {goal_age}\n"
                f"{avatar_summary}\n"
                f"RL focused: {focus_state.get()}\n"
                f"RL window: {geometry or '(not detected — using reference res, no scaling)'} | "
                f"ui_scale: {UI_SCALE}"
            ))

        if not focus_state.get():
            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    self._hide_avatar(f"scoreboard_{team_name}_{row_idx}")
            self._hide_avatar("goal_nameplate")
            self.root.after(50, self._tick)
            return

        if scoreboard_visible:
            blue = sorted(
                [p for p in players if p.get("team_num") == 0],
                key=lambda p: p.get("score", 0), reverse=True,
            )
            orange = sorted(
                [p for p in players if p.get("team_num") == 1],
                key=lambda p: p.get("score", 0), reverse=True,
            )

            team_size = max(len(blue), len(orange), 1)
            slots = get_scoreboard_slots(team_size)

            if self._last_logged_team_size != team_size:
                log.info(
                    "team_size=%d (blue=%d, orange=%d) -> slots=%s",
                    team_size, len(blue), len(orange), slots,
                )
                self._last_logged_team_size = team_size

            # Only widget_ids actually re-placed below stay in
            # _anim_state — anything not in this set (player left,
            # team shrank, no avatar this tick) gets hidden after, which
            # is also what resets its animation to frame 0 next time it
            # reappears. Everything still occupied is left alone here so
            # _place_avatar's same-key fast path can keep its GIF timer
            # running instead of restarting it 20x/sec.
            occupied_ids = set()
            for team_name, team_players in (("blue", blue), ("orange", orange)):
                for slot in slots:
                    slot_team, row_idx, x, y, w, h = slot
                    if slot_team != team_name:
                        continue
                    widget_id = f"scoreboard_{team_name}_{row_idx}"
                    if row_idx < len(team_players):
                        p = team_players[row_idx]
                        avatar_path = p.get("avatar_path")
                        if avatar_path:
                            occupied_ids.add(widget_id)
                            prev = self._last_logged_positions.get(widget_id)
                            if prev != (p.get("name"), x, y):
                                log.info(
                                    "PLACE %s: %s -> x=%d y=%d w=%d h=%d",
                                    widget_id, p.get("name"), x, y, w, h,
                                )
                                self._last_logged_positions[widget_id] = (p.get("name"), x, y)
                            self._place_avatar(widget_id, avatar_path, x, y, w, h)

            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    widget_id = f"scoreboard_{team_name}_{row_idx}"
                    if widget_id not in occupied_ids:
                        self._hide_avatar(widget_id)
        else:
            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    self._hide_avatar(f"scoreboard_{team_name}_{row_idx}")

        if last_goal:
            elapsed = time.time() - last_goal.get("timestamp", 0)
        else:
            elapsed = None
        in_delay_window = (
            elapsed is not None
            and GOAL_NAMEPLATE_DELAY_SECONDS <= elapsed < (GOAL_NAMEPLATE_DELAY_SECONDS + GOAL_NAMEPLATE_DURATION_SECONDS)
        )
        if in_delay_window and is_replay:
            scorer_key = last_goal.get("scorer_key")
            scorer = next(
                (p for p in players
                 if p.get("platform") + "|" + p.get("uid") + "|" + str(p.get("splitscreen")) == scorer_key),
                None,
            )
            avatar_path = scorer.get("avatar_path") if scorer else None
            x, y, w, h = get_goal_nameplate_slot()
            if avatar_path:
                self._place_avatar("goal_nameplate", avatar_path, x, y, w, h)
            else:
                self._hide_avatar("goal_nameplate")
        else:
            self._hide_avatar("goal_nameplate")

        self._advance_animations()
        self.root.after(50, self._tick)


def main():
    global DEBUG_MODE
    parser = argparse.ArgumentParser(description="RL PFP overlay (Windows)")
    parser.add_argument(
        "--debug", action="store_true",
        help="Show the debug HUD (player list, last goal, focus state) in the top-left corner",
    )
    args = parser.parse_args()
    DEBUG_MODE = args.debug

    # DPI awareness so win32 coordinates/geometry match real pixels, not
    # a Windows-scaled logical size (avoids double-scaling on top of our
    # own rl_ui_scale correction on a >100% Windows display scale).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            log.warning("Could not set DPI awareness — positions may be off on a scaled display.")

    poll_thread = threading.Thread(target=poll_bridge_loop, args=(bridge_state,), daemon=True)
    poll_thread.start()

    focus_thread = threading.Thread(target=poll_focus_loop, daemon=True)
    focus_thread.start()

    root = tk.Tk()
    Overlay(root)
    root.mainloop()


if __name__ == "__main__":
    main()
