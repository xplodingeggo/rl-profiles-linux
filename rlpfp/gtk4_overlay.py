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
import sys
import threading
import time
import argparse

from .layout import (
    BRIDGE_URL,
    POLL_INTERVAL_SECONDS,
    GOAL_NAMEPLATE_DURATION_SECONDS,
    GOAL_NAMEPLATE_DELAY_SECONDS,
    REFERENCE_RESOLUTION,
    REFERENCE_UI_SCALE,
    UI_SCALE,
    AVATAR_INSET_PX,
    window_geometry_state,
    get_scoreboard_slots,
    get_goal_nameplate_slot,
    BridgeState,
    FocusState,
    poll_bridge_loop as _shared_poll_bridge_loop,
)

DEBUG_MODE = False  # set by --debug CLI flag in main(), before app.run()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gtk4-overlay")

OVERLAY_VERSION = "2026-08-10-hide-all-first-v2"
log.info("=== gtk4_overlay.py VERSION: %s ===", OVERLAY_VERSION)

# All slot-position calibration data (SCOREBOARD_LAYOUTS, the UI-scale
# quadratics, get_scoreboard_slots/get_goal_nameplate_slot, etc.) now
# lives in layout.py, shared with win_overlay.py — see NOTES at the
# bottom of this file for the calibration derivation, which still
# applies unchanged.

bridge_state = BridgeState()
focus_state = FocusState()


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
    """Runs in a background thread; never touches GTK directly. Thin
    wrapper around the shared implementation in layout.py, bound to this
    module's bridge_state instance."""
    _shared_poll_bridge_loop(bridge_state)


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
