#!/usr/bin/env python3
"""
Layer 4: Overlay window (Hyprland + GTK4 layer-shell)

Renders player profile pictures on top of Rocket League:
  1. Goal-scored nameplate avatar — shown for a few seconds after
     the GoalScored event, at the "SCORED BY" nameplate position.
  2. Scoreboard avatars — shown next to each player row while the
     scoreboard is visible (currently driven by a manual toggle on
     the bridge; Layer 6 will wire this to real L3/Tab detection).

Reference layout below was measured at 2560x1440, 75% RL interface
scale, and is scaled at runtime to your actual RL window resolution +
configured rl_ui_scale (see _scale_slot / NOTES at the bottom) — that
scaling is unvalidated past the reference point, so check alignment
with --debug if you're on a different setup.

Requires:
  pip install PyGObject gtk4-layer-shell requests --break-system-packages
  System packages: libgtk-4-dev, gtk4-layer-shell (see project README)

Run (on your Hyprland desktop, with rl_stats_bridge.py already running):
  python3 gtk4_overlay.py
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk, GdkPixbuf, Gio

import json
import logging
import math
import sys
import threading
import time
import urllib.request
import urllib.error
import os
import argparse

from .config import load_config

DEBUG_MODE = False  # set by --debug CLI flag in main(), before app.run()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gtk4-overlay")

OVERLAY_VERSION = "2026-08-10-hide-all-first-v2"
log.info("=== gtk4_overlay.py VERSION: %s ===", OVERLAY_VERSION)

BRIDGE_URL = "http://127.0.0.1:9090"
POLL_INTERVAL_SECONDS = 0.05  # Poll every 50ms — Stats API updates at ~2Hz anyway
GOAL_NAMEPLATE_DURATION_SECONDS = 11  # how long to show it after GoalScored
GOAL_NAMEPLATE_DELAY_SECONDS = 3.5  # confirmed via real testing to match RL's replay timing

# --- Reference layout (2560x1440, 75% RL interface scale) --------------
# Measured directly from screenshots. See LAYER5 for scaling to other
# resolutions/UI scales.

REFERENCE_RESOLUTION = (2560, 1440)
REFERENCE_UI_SCALE = 0.75


def _load_ui_scale() -> float:
    """RL's 'Interface Scale' video setting can't be read from the OS —
    it's an in-game slider with no exposed file/API — so it's a config
    value (rl_ui_scale / $RL_UI_SCALE) the user sets to match. Falls
    back to REFERENCE_UI_SCALE (i.e. no UI-scale correction applied) if
    unset or invalid."""
    # $RL_UI_SCALE takes priority over config.json, same precedence as
    # the other env-var-backed settings (steam/psn/xbox keys) — this is
    # how `rl-pfp start --ui-scale N` overrides the saved config for a
    # single run without touching the file.
    raw = os.environ.get("RL_UI_SCALE") or load_config().get("rl_ui_scale")
    if not raw:
        return REFERENCE_UI_SCALE
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except (TypeError, ValueError):
        log.warning(
            "rl_ui_scale %r isn't a positive number — using default %.2f",
            raw, REFERENCE_UI_SCALE,
        )
        return REFERENCE_UI_SCALE
    return value


UI_SCALE = _load_ui_scale()

# RL repositions each team's section start position depending on team
# size (1v1/2v2 sit differently than 3v3/4v4). Each (team_size, team) pair
# has its OWN y value below — editing one will never affect any other
# team size, so it's safe to tune 1v1 without touching 2v2/3v3/4v4.
#
# team_size = max(players on blue, players on orange), i.e. 1 for 1v1,
# 2 for 2v2, 3 for 3v3, 4 for 4v4.
ROW_HEIGHT = 56  # vertical spacing between rows within a team's section
BOX_SIZE = 48  # size_px = 64 * UI_SCALE (linear, confirmed: 32@0.5, 48@0.75, 64@1.0)
SLOT_X = 714  # horizontal position — same for both teams, all sizes (probe-measured @ 0.75, 3v3)

# How many pixels to shrink every avatar picture on each side (0 = off).
AVATAR_INSET_PX = 0

# Per-(team_size, team) row0 y-coordinate. This is the ONLY thing you
# need to edit to fix a specific team size's vertical position — changing
# SCOREBOARD_LAYOUTS[1]["orange"], for example, affects ONLY 1v1's orange
# team, nothing else.
SCOREBOARD_LAYOUTS = {
    4: {"blue": 428, "orange": 756},
    # 3 used to be a straight copy of 4's values, but 3v3's block actually
    # sits about one ROW_HEIGHT lower on screen — confirmed by measuring
    # a 3v3 screenshot pixel-by-pixel (avatar was landing on the "BLUE"
    # header instead of the "vaelixz." row). Starting point below is
    # measured from that screenshot; nudge with grid_measure.py if it's
    # not pixel-perfect for you.
    3: {"blue": 490, "orange": 754},  # was 810, -1 ROW_HEIGHT: orange rendered a full row too low
    2: {"blue": 553, "orange": 756},  # blue re-measured with box_probe.py @ 0.75, was 550
    1: {"blue": 617, "orange": 761},
}
# (These values already have the old GLOBAL_Y_OFFSET of -36 baked in, so
# they should render in the same place as before this refactor — this
# change only affects how you EDIT them going forward, not the current
# on-screen position.)


def _scaled_row_height() -> float:
    """Row spacing at the current UI_SCALE — see ROW_HEIGHT_QUAD above,
    fitted directly from real measured row spacing, not derived from the
    icon-size curve (that shortcut was tried and didn't match measured
    data closely enough)."""
    return _quad(ROW_HEIGHT_QUAD, UI_SCALE)


def get_scoreboard_slots(team_size: int) -> list:
    """Build the (team, row_index, x, y, w, h) slot list for a given team
    size (1-4), using the calibrated layout closest to it, scaled to the
    current RL window geometry + UI scale (see _scale_slot)."""
    # Clamp to the range we have data for.
    team_size = max(1, min(4, team_size))
    layout = SCOREBOARD_LAYOUTS[team_size]
    blue_row0_y = layout["blue"]
    orange_row0_y = layout["orange"]
    row_height = _scaled_row_height()
    slots = []
    for row in range(team_size):
        x, y, w, h = _scale_slot(SLOT_X, blue_row0_y + row * row_height, BOX_SIZE, BOX_SIZE, SCOREBOARD_UI_QUAD)
        slots.append(("blue", row, x, y, w, h))
    for row in range(team_size):
        x, y, w, h = _scale_slot(SLOT_X, orange_row0_y + row * row_height, BOX_SIZE, BOX_SIZE, SCOREBOARD_UI_QUAD)
        slots.append(("orange", row, x, y, w, h))
    return slots

# Goal-scored nameplate avatar box.
# Reverted to your last confirmed-good manual calibration.
GOAL_NAMEPLATE_X_NUDGE = 3
GOAL_NAMEPLATE_Y_NUDGE = -35.25  # nudged back down 1px from prior -1px shift
_GOAL_NAMEPLATE_REFERENCE_SLOT = (
    1044 + GOAL_NAMEPLATE_X_NUDGE,
    1220 + GOAL_NAMEPLATE_Y_NUDGE,
    75,
    75,
)  # x, y, w, h — at REFERENCE_RESOLUTION / REFERENCE_UI_SCALE


def get_goal_nameplate_slot() -> tuple:
    return _scale_slot(*_GOAL_NAMEPLATE_REFERENCE_SLOT, NAMEPLATE_UI_QUAD)


class BridgeState:
    """Thread-safe holder for the latest bridge poll result."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "players": [],
            "scoreboard_visible": False,
            "last_goal": None,
        }

    def update(self, data: dict) -> None:
        with self._lock:
            self._data = data

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)


bridge_state = BridgeState()


class FocusState:
    """Thread-safe holder for whether Rocket League is currently the
    focused/active window (per Hyprland)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._focused = True  # assume focused until we know otherwise

    def set(self, focused: bool) -> None:
        with self._lock:
            self._focused = focused

    def get(self) -> bool:
        with self._lock:
            return self._focused


focus_state = FocusState()


class WindowGeometryState:
    """Thread-safe holder for RL's actual window geometry (x, y, w, h) in
    real pixels, per Hyprland — lets scoreboard/nameplate positions scale
    to whatever resolution RL is rendering at instead of assuming the
    hardcoded REFERENCE_RESOLUTION. Updated by poll_focus_loop whenever
    RL is the focused window; keeps the last known value the rest of the
    time (so avatars still position correctly in the moment right after
    RL regains focus, before that thread's next 1s poll lands)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._geometry = None  # (x, y, w, h) or None if never detected

    def set(self, geometry: tuple) -> None:
        with self._lock:
            self._geometry = geometry

    def get(self):
        with self._lock:
            return self._geometry


window_geometry_state = WindowGeometryState()


# UI-scale correction, per HUD element group, as a QUADRATIC in UI_SCALE:
#   value(s) = c2*s^2 + c1*s + c0
# Fitted exactly through 3 real measured screenshot points per element
# (s = 0.5, 0.75, 1.0) — pixel-detected icon borders via grid_measure.py +
# numpy, not eyeballed. A straight line (2-point) model was tried first
# and was WRONG: it predicted the scoreboard icon at x=522 for s=1.0, the
# real measured position was x=423 — off by ~100px, because the true
# relationship curves. The two HUD groups also scale completely
# differently from each other (scoreboard moves hard in x, nameplate
# moves hard in y AND grows much bigger at s=1.0), so each axis of each
# element gets its own independently-fitted quadratic — no shared/global
# formula.
#
# Calibration data points (2560x1440, RL window at (0,0), x/y/size = icon
# top-left + square side length):
#   scoreboard (row0 icon, team_size=2 — every real screenshot measured
#   this session turned out to be a 2v2-with-bots lobby, players:4 per
#   the debug HUD, even the ones that looked like "1v1" at a glance):
#     x: s=0.50 -> 892, s=0.75 -> 712, s=1.00 -> 423   [measured]
#     size: s=0.50 -> 50, s=0.75 -> 51, s=1.00 -> 68   [measured]
#     y: s=0.50 -> 622, s=0.75 -> 550 (= SCOREBOARD_LAYOUTS[2]["blue"],
#        trusted/unchanged — "75% works perfectly"), s=1.00 -> 545
#        [measured, from a screenshot with the PFP overlay OFF for a
#        teammate (Heater) so the real un-covered RL icon was directly
#        visible — the y-curve was previously fit assuming a y=617
#        reference (team_size=1's row0, which no screenshot this session
#        actually used), so at s=1.0 it was predicting row0 68px too
#        high on screen; refit against this cleaner ground truth fixes
#        it without touching SCOREBOARD_LAYOUTS or any other global]
#   goal nameplate icon:
#     s=0.50 -> (1118, 1251, 61)
#     s=0.75 -> (1044, 1182, 78)
#     s=1.00 -> (957, 1139, 124)  [measured]
#
# This is now the FULL 50-100% range (RL's entire Interface Scale slider),
# so no more extrapolation needed for ui_scale alone.
SCOREBOARD_UI_QUAD = {  # (c2, c1, c0) per axis, value = c2*s^2 + c1*s + c0
    # x/y fit from box_probe.py (interactive live-align tool, not
    # screenshot pixel-counting), all 3 points now real, 2v2 row0:
    #   x: 0.5 -> 903, 0.75 -> 714, 1.0 -> 525  (0.5 nudged -1 left in-game)
    #   y: 0.5 -> 596.5, 0.75 -> 553, 1.0 -> 510 (0.5 nudged -1 up in-game)
    "x": (0.0, -756.0, 1281.0),
    "y": (4.0, -179.0, 685.0),
    # size: 32@0.5, 48@0.75 (BOX_SIZE), 64@1.0 — exactly linear.
    "size": (0.0, 64.0, 0.0),
}
# ROW_HEIGHT (spacing between scoreboard rows) fitted the same way, from
# a 2-row screenshot at each of s=0.5 and s=1.0 (row1's top minus row0's
# top), plus the trusted s=0.75 baseline (ROW_HEIGHT below). NOT the same
# curve as icon "size" above — tried reusing that curve as a shortcut
# first, it predicted 55px at s=0.5 vs the real measured ~50px, off by
# enough to bother fitting ROW_HEIGHT on its own real data instead.
#   s=0.50 -> 50px   [measured]
#   s=0.75 -> 56px   (existing baseline)
#   s=1.00 -> 73px   [measured]
ROW_HEIGHT_QUAD = (88.0, -86.0, 72.0)  # c0 +1: uniform +1px row spacing, all scales
NAMEPLATE_UI_QUAD = {
    # x (delta form, zero at ref=0.75): 1.0 -> -77 (970 vs 1047 ref),
    # 0.5 -> +78 (nudged +1 right in-game, so target abs = 1125).
    "x": (8.0, -322.0, 237.0),
    # y: uniform -1px shift applied to every scale (whole curve rendered
    # 1px too low, found via 65% check). 0.75 -> 1183.75, 1.0 -> 1109,
    # 0.5 -> 1256.5 — quadratic, absolute form.
    "y": (-16.0, -271.0, 1396.0),
    # size is linear: size_px = 100 * UI_SCALE at REFERENCE_RESOLUTION.
    # Confirmed: 0.75 -> 75px, 1.0 -> 100px (both measured exactly).
    "size": (0.0, 100.0, 0.0),
}


def _quad(coefs: tuple, s: float) -> float:
    c2, c1, c0 = coefs
    return c2 * s * s + c1 * s + c0


def _round_up(value: float) -> int:
    """Any fraction above .0 rounds UP (not Python's round-half-to-even) —
    e.g. 39.08 -> 40. Matches an in-game rounding quirk found while
    calibrating the scoreboard size at 65% UI scale."""
    return math.ceil(value)


def _scale_slot(x: int, y: int, w: int, h: int, ui_quad: dict) -> tuple:
    """Scale a reference-layout (x, y, w, h) slot — measured at
    REFERENCE_RESOLUTION / REFERENCE_UI_SCALE — to RL's actual current
    window geometry and configured rl_ui_scale.

    Two independent corrections, applied in order:
      1. UI-scale: shift/resize by (quad(UI_SCALE) - quad(REFERENCE_UI_SCALE))
         per axis, where quad() is the fitted curve for this HUD group (see
         SCOREBOARD_UI_QUAD / NAMEPLATE_UI_QUAD above). The quad passes
         exactly through the reference point, so this delta is 0 at
         UI_SCALE == REFERENCE_UI_SCALE (identity) — same as the old
         straight-line model, just curved instead of linear, and applied
         as an OFFSET from the caller's x/y/w/h so per-team-size/per-team
         row0 offsets (SCOREBOARD_LAYOUTS) and ROW_HEIGHT still apply on
         top, unaffected.
      2. Resolution: multiply by (current RL window size / REFERENCE_RESOLUTION),
         from the window's top-left corner. UNVALIDATED — no resolution other
         than 2560x1440 has been tested yet, this is still a guess.
    """
    s = UI_SCALE
    dx = _quad(ui_quad["x"], s) - _quad(ui_quad["x"], REFERENCE_UI_SCALE)
    dy = _quad(ui_quad["y"], s) - _quad(ui_quad["y"], REFERENCE_UI_SCALE)
    dsize = _quad(ui_quad["size"], s) - _quad(ui_quad["size"], REFERENCE_UI_SCALE)
    x = x + dx
    y = y + dy
    w = max(1.0, w + dsize)
    h = max(1.0, h + dsize)

    geometry = window_geometry_state.get()
    if geometry is None:
        # RL's window geometry was never detected (hyprctl unavailable,
        # or RL hasn't been seen as the focused window yet) — skip the
        # resolution correction, just apply the UI-scale correction above.
        return _round_up(x), _round_up(y), _round_up(w), _round_up(h)
    win_x, win_y, win_w, win_h = geometry

    res_scale_x = win_w / REFERENCE_RESOLUTION[0]
    res_scale_y = win_h / REFERENCE_RESOLUTION[1]

    return (
        win_x + _round_up(x * res_scale_x),
        win_y + _round_up(y * res_scale_y),
        max(1, _round_up(w * res_scale_x)),
        max(1, _round_up(h * res_scale_y)),
    )


# Substrings to match against Hyprland's activewindow "class" or "title"
# (case-insensitive). Rocket League's window class varies by how it's
# launched (Steam/Epic/Heroic/Proton) — if focus detection isn't working,
# run `hyprctl activewindow` while RL is focused and check what "class"
# and "title" actually report, then add that string here.
RL_WINDOW_MATCH_CANDIDATES = ["rocketleague", "rocket league"]


def poll_focus_loop():
    """Runs in a background thread; polls Hyprland for the active window
    and updates focus_state. Never touches GTK directly."""
    import subprocess

    logged_unmatched = set()
    while True:
        try:
            result = subprocess.run(
                ["hyprctl", "activewindow", "-j"],
                capture_output=True, text=True, timeout=2,
            )
            info = json.loads(result.stdout)
            window_class = (info.get("class") or "").lower()
            window_title = (info.get("title") or "").lower()
            is_rl = any(
                candidate in window_class or candidate in window_title
                for candidate in RL_WINDOW_MATCH_CANDIDATES
            )
            focus_state.set(is_rl)

            if is_rl:
                at = info.get("at")
                size = info.get("size")
                if (
                    isinstance(at, list) and len(at) == 2
                    and isinstance(size, list) and len(size) == 2
                    and size[0] > 0 and size[1] > 0
                ):
                    geometry = (at[0], at[1], size[0], size[1])
                    if window_geometry_state.get() != geometry:
                        log.info("RL window geometry: %s", geometry)
                        window_geometry_state.set(geometry)

            # Log unmatched window identifiers once each, so you can find
            # the right string to add to RL_WINDOW_MATCH_CANDIDATES if
            # detection isn't working.
            ident = f"{window_class}|{window_title}"
            if not is_rl and ident not in logged_unmatched:
                log.info("Active window (not matched as RL): class=%r title=%r",
                          window_class, window_title)
                logged_unmatched.add(ident)
        except FileNotFoundError:
            log.warning("hyprctl not found — focus detection disabled, overlay always active")
            focus_state.set(True)
            time.sleep(10)
            continue
        except Exception:
            log.exception("Error polling Hyprland focus state")
        time.sleep(1.0)


def apply_transparency_css():
    """Must be called after a Gdk.Display exists (i.e. inside do_activate,
    not at module import time)."""
    css_provider = Gtk.CssProvider()
    css_provider.load_from_data(b"""
        .transparent {
            background: transparent;
            background-color: transparent;
        }
    """)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def poll_bridge_loop():
    """Runs in a background thread; never touches GTK directly."""
    while True:
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/current-lobby", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                bridge_state.update(data)
        except (urllib.error.URLError, TimeoutError, ConnectionRefusedError) as e:
            log.warning("Bridge unreachable: %s", e)
        except Exception:
            log.exception("Error polling bridge")
        time.sleep(POLL_INTERVAL_SECONDS)


class Overlay(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.local.rl-pfp-overlay")
        self.window = None
        self.fixed = None
        # Cache of loaded GdkTexture per avatar_path, so we don't reload
        # from disk every redraw.
        self._texture_cache: dict[str, Gdk.Texture] = {}
        # Widgets we've placed, keyed by a stable id, so we can move/hide
        # them instead of destroying and recreating every tick.
        self._avatar_widgets: dict[str, Gtk.Picture] = {}
        self._last_logged_positions: dict[str, tuple] = {}

    def do_activate(self):
        apply_transparency_css()

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_decorated(False)
        self.window.set_default_size(*REFERENCE_RESOLUTION)
        self.window.set_css_classes(["transparent"])

        # gtk4-layer-shell's own docs warn that calling init_for_window()
        # on a compositor that doesn't support wlr-layer-shell can hard-
        # crash at the GTK/GLib level in a way Python's try/except can't
        # catch — so check support first and fail with a clear message
        # instead, rather than wrapping init_for_window() itself.
        if not Gtk4LayerShell.is_supported():
            log.error(
                "Your Wayland compositor doesn't support the layer-shell "
                "protocol (wlr-layer-shell), which this overlay requires. "
                "This is expected on GNOME, KDE, and X11 sessions. "
                "Compositors that DO support it include Hyprland, Sway, "
                "and other wlroots-based compositors. See the README."
            )
            sys.exit(1)

        Gtk4LayerShell.init_for_window(self.window)
        Gtk4LayerShell.set_layer(self.window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (
            Gtk4LayerShell.Edge.LEFT,
            Gtk4LayerShell.Edge.RIGHT,
            Gtk4LayerShell.Edge.TOP,
            Gtk4LayerShell.Edge.BOTTOM,
        ):
            Gtk4LayerShell.set_anchor(self.window, edge, True)
            Gtk4LayerShell.set_margin(self.window, edge, 0)

        self.fixed = Gtk.Fixed()
        self.window.set_child(self.fixed)

        # Click-through: give the surface an empty input region so mouse
        # clicks always pass through to whatever's underneath (RL, your
        # desktop, other windows) regardless of focus state. The window
        # still renders on top — it just never intercepts the pointer.
        self.window.connect("realize", self._make_click_through)

        # Debug HUD — top-left corner, shows raw bridge state so you can
        # confirm things are working without alt-tabbing to a terminal.
        # Only created when launched with --debug; otherwise self.debug_label
        # stays None and _tick skips updating it.
        self.debug_label = None
        if DEBUG_MODE:
            self.debug_label = Gtk.Label(label="waiting for bridge...")
            self.debug_label.set_css_classes(["debug-hud"])
            debug_css = Gtk.CssProvider()
            debug_css.load_from_data(b"""
                .debug-hud {
                    background-color: rgba(0, 0, 0, 0.75);
                    color: #00ff00;
                    font-family: monospace;
                    font-size: 14px;
                    padding: 8px;
                }
            """)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                debug_css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self.fixed.put(self.debug_label, 10, 10)


        self.window.present()

        # Redraw tick — reads bridge_state (updated by the polling
        # thread) and repositions/shows/hides avatar widgets.
        GLib.timeout_add(50, self._tick)

    def _get_texture(self, path: str) -> Gdk.Texture | None:
        if path in self._texture_cache:
            return self._texture_cache[path]
        try:
            texture = Gdk.Texture.new_from_filename(path)
            self._texture_cache[path] = texture
            return texture
        except Exception as e:
            log.warning("Failed to load avatar image %s: %s", path, e)
            return None

    def _place_avatar(self, widget_id: str, avatar_path: str, x: int, y: int, w: int, h: int):
        """Show (or move) an avatar Picture widget at the given position.

        The picture is shrunk and centered within (x, y, w, h) by
        AVATAR_INSET_PX on each side, so RL's own white outline border
        (drawn right at the edge of the real icon slot) stays visible
        around our overlay instead of being fully covered by it.
        """
        texture = self._get_texture(avatar_path)
        if not texture:
            self._hide_avatar(widget_id)
            return

        inset = AVATAR_INSET_PX
        inset_x = x + inset
        inset_y = y + inset
        inset_w = max(1, w - 2 * inset)
        inset_h = max(1, h - 2 * inset)

        picture = self._avatar_widgets.get(widget_id)
        if picture is None:
            picture = Gtk.Picture()
            picture.set_can_shrink(True)
            picture.set_content_fit(Gtk.ContentFit.COVER)
            self.fixed.put(picture, inset_x, inset_y)
            self._avatar_widgets[widget_id] = picture
        else:
            self.fixed.move(picture, inset_x, inset_y)

        picture.set_paintable(texture)
        picture.set_size_request(inset_w, inset_h)
        picture.set_visible(True)
        # Force immediate visual update on position/content change
        self.fixed.queue_draw()

    def _hide_avatar(self, widget_id: str):
        picture = self._avatar_widgets.get(widget_id)
        if picture:
            picture.set_visible(False)

    def _make_click_through(self, widget):
        try:
            import cairo
            surface = widget.get_surface()
            surface.set_input_region(cairo.Region())
            log.info("Click-through enabled (empty input region set).")
        except Exception as e:
            # If this fails, the overlay window may still capture clicks —
            # test by clicking on something underneath it. Some GTK4/cairo
            # binding combos expose this API slightly differently.
            log.warning("Could not set click-through input region: %s", e)

    def _tick(self) -> bool:
        state = bridge_state.get()
        players = state.get("players", [])
        scoreboard_visible = state.get("scoreboard_visible", False)
        last_goal = state.get("last_goal")
        is_replay = state.get("is_replay", False)

        # --- Debug HUD ---
        if self.debug_label is not None:
            avatar_summary = ", ".join(
                f"{p.get('name')}={'PFP' if p.get('avatar_path') else 'none'}"
                for p in players
            ) or "(no players)"
            goal_age = f"{time.time() - last_goal['timestamp']:.1f}s ago" if last_goal else "none"
            geometry = window_geometry_state.get()
            self.debug_label.set_label(
                f"players: {len(players)} | scoreboard_visible: {scoreboard_visible}\n"
                f"last_goal: {goal_age}\n"
                f"{avatar_summary}\n"
                f"RL focused: {focus_state.get()}\n"
                f"RL window: {geometry or '(not detected — using reference res, no scaling)'} | "
                f"ui_scale: {UI_SCALE}"
            )

        # --- Focus gate ---
        # Only show avatar overlays when Rocket League is actually the
        # focused/active window (per Hyprland). This keeps the overlay out
        # of the way while alt-tabbed to other apps, browsing, etc. The
        # debug HUD stays visible regardless, for troubleshooting.
        if not focus_state.get():
            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    self._hide_avatar(f"scoreboard_{team_name}_{row_idx}")
            self._hide_avatar("goal_nameplate")
            return True  # keep the timeout running, skip rendering below

        # --- Scoreboard avatars ---
        if scoreboard_visible:
            # Unconditionally hide ALL scoreboard widgets first. This
            # guarantees no stale widget from a previous match's team size
            # can linger on screen (e.g. testing a 2v2 then switching to a
            # 1v1 without restarting the overlay) — we only re-show the
            # ones that are actually correct for the current state below.
            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    self._hide_avatar(f"scoreboard_{team_name}_{row_idx}")

            # Split players by team and sort by score (descending) so
            # highest-scoring players occupy the top slots, matching RL's
            # actual scoreboard display order.
            blue = sorted(
                [p for p in players if p.get("team_num") == 0],
                key=lambda p: p.get("score", 0),
                reverse=True,
            )
            orange = sorted(
                [p for p in players if p.get("team_num") == 1],
                key=lambda p: p.get("score", 0),
                reverse=True,
            )

            # RL repositions the whole scoreboard block based on team size
            # (1v1/2v2 sit lower on screen than 3v3/4v4) — see
            # SCOREBOARD_LAYOUTS. Use the larger team's size as the match
            # size (they're normally equal; max() is just a safe default
            # if a team is mid-fill during connect/disconnect).
            team_size = max(len(blue), len(orange), 1)
            slots = get_scoreboard_slots(team_size)

            # Log team_size only when it changes, so this doesn't spam the
            # terminal at the 50ms tick rate.
            if getattr(self, "_last_logged_team_size", None) != team_size:
                log.info(
                    "team_size=%d (blue=%d, orange=%d) -> slots=%s",
                    team_size, len(blue), len(orange), slots,
                )
                self._last_logged_team_size = team_size

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
                            # Log only when this widget's target position
                            # actually changes, to avoid spamming every tick.
                            prev = self._last_logged_positions.get(widget_id)
                            if prev != (p.get("name"), x, y):
                                log.info(
                                    "PLACE %s: %s -> x=%d y=%d w=%d h=%d",
                                    widget_id, p.get("name"), x, y, w, h,
                                )
                                self._last_logged_positions[widget_id] = (p.get("name"), x, y)
                            self._place_avatar(widget_id, avatar_path, x, y, w, h)
                    # (no else needed — already hidden above)
        else:
            for team_name in ("blue", "orange"):
                for row_idx in range(4):
                    self._hide_avatar(f"scoreboard_{team_name}_{row_idx}")

        # --- Goal-scored nameplate avatar ---
        # Wait GOAL_NAMEPLATE_DELAY_SECONDS after the goal before showing
        # (to roughly match RL's own slow-mo replay timing), then keep it
        # visible for GOAL_NAMEPLATE_DURATION_SECONDS after that — UNLESS
        # the replay ends first (naturally, or someone presses A to skip),
        # in which case is_replay flips to False and we hide immediately.
        # This is far more reliable than trying to detect the skip
        # keypress ourselves, which the Stats API doesn't expose anyway.
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
            scorer = next((p for p in players if p.get("platform") + "|" + p.get("uid") + "|" + str(p.get("splitscreen")) == scorer_key), None)
            avatar_path = scorer.get("avatar_path") if scorer else None
            x, y, w, h = get_goal_nameplate_slot()
            if avatar_path:
                self._place_avatar("goal_nameplate", avatar_path, x, y, w, h)
            else:
                self._hide_avatar("goal_nameplate")
        else:
            self._hide_avatar("goal_nameplate")

        return True  # keep the timeout running


def main():
    global DEBUG_MODE
    parser = argparse.ArgumentParser(description="RL PFP overlay")
    parser.add_argument(
        "--debug", action="store_true",
        help="Show the debug HUD (player list, last goal, focus state) in the top-left corner",
    )
    args = parser.parse_args()
    DEBUG_MODE = args.debug

    poll_thread = threading.Thread(target=poll_bridge_loop, daemon=True)
    poll_thread.start()

    focus_thread = threading.Thread(target=poll_focus_loop, daemon=True)
    focus_thread.start()

    app = Overlay()
    app.run([])  # empty list, not None — we already parsed our own args above;
    # passing None would let GTK try to parse sys.argv itself and choke on --debug


if __name__ == "__main__":
    main()

# --- NOTES ---------------------------------------------------------------
#
# Recalibrating for a different resolution or UI scale:
#   SCOREBOARD_LAYOUTS and _GOAL_NAMEPLATE_REFERENCE_SLOT are measured at
#   REFERENCE_RESOLUTION + REFERENCE_UI_SCALE. _scale_slot() converts
#   those into real on-screen coordinates using two corrections:
#     1. UI-scale (SCOREBOARD_UI_QUAD / NAMEPLATE_UI_QUAD): a quadratic
#        curve per axis (x, y, size), fitted EXACTLY through 3 real
#        measured screenshot points each (ui_scale = 0.5, 0.75, 1.0 — the
#        full range of RL's Interface Scale slider), pixel-detected via
#        grid_measure.py + numpy, not eyeballed. A straight-line (2-point)
#        model was tried first and was wrong by ~100px once the 1.0 point
#        came in — the real relationship curves, it doesn't extrapolate
#        linearly. The two HUD groups scale completely differently from
#        each other (scoreboard moves hard horizontally, nameplate moves
#        hard vertically and grows much bigger near 100%), confirmed by
#        measurement, not assumed — each axis of each element has its own
#        independently-fitted quadratic.
#     2. Resolution (still an unvalidated guess): RL's actual window
#        geometry from `hyprctl activewindow -j` (poll_focus_loop ->
#        window_geometry_state), scaled multiplicatively from the
#        window's top-left corner. No resolution besides 2560x1440 has
#        been tested yet.
#   ui_scale is now fully covered — every curve (SCOREBOARD_UI_QUAD,
#   NAMEPLATE_UI_QUAD, ROW_HEIGHT_QUAD) is fitted through 3 real
#   screenshot-measured points spanning the full 50-100% range and
#   reproduces all of them exactly. ROW_HEIGHT_QUAD is fitted from its
#   own real 2-row measurements (50px @ 0.5, 73px @ 1.0) — an earlier
#   shortcut that reused the icon-size curve for row spacing was tried
#   and rejected once the 0.5 data point came in (predicted 55px, real
#   was 50px). Use `rl-pfp grid` (or `python3 -m rlpfp.grid_measure`)
#   alongside `rl-pfp start` to gather further data points the same way
#   if positions drift for other team sizes/rows not yet spot-checked.
#
# Click-through:
#   The cairo.Region() approach above is the standard way to make a
#   GTK4/Wayland surface click-through, but layer-shell + input-region
#   behavior can vary by compositor version. If clicks are still being
#   captured by the overlay in-game, that's the first thing to debug.
#
# Scoreboard row ordering:
#   We're assuming RL lists players within a team in join order, which
#   matched the test screenshots. If it doesn't reliably (e.g. it might
#   sort by score), the avatar-to-name mapping on the real scoreboard
#   could be off — cross check visually once you've got this running.
