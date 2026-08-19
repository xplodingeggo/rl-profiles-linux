"""
Shared, OS-independent layout/calibration math for the overlay.

Split out of gtk4_overlay.py so the Windows overlay (win_overlay.py) can
reuse the exact same calibrated slot positions without importing GTK
(gi.repository isn't installable on Windows, and gtk4_overlay.py imports
it at module level). This module has zero GUI-toolkit dependencies —
just the pure math that turns "team size + row" into an (x, y, w, h) box
on screen.

If you ever recalibrate slot positions (via grid_measure/box_probe on
either OS), edit the numbers here — both overlays pick it up.
"""

from __future__ import annotations

import math
import os
import threading
import logging

from .config import load_config

log = logging.getLogger("rlpfp-layout")

BRIDGE_URL = "http://127.0.0.1:9090"
POLL_INTERVAL_SECONDS = 0.05  # Poll every 50ms — Stats API updates at ~2Hz anyway
GOAL_NAMEPLATE_DURATION_SECONDS = 11  # how long to show it after GoalScored
GOAL_NAMEPLATE_DELAY_SECONDS = 3.5  # confirmed via real testing to match RL's replay timing

# --- Reference layout (2560x1440, 75% RL interface scale) --------------
# Measured directly from screenshots on Linux. Rocket League's UI is the
# same game engine/assets regardless of OS, so these reference pixel
# positions are expected to transfer to Windows unchanged — only the
# window-geometry/UI-scale correction below needs an OS-specific input
# (win32gui window rect instead of hyprctl).

REFERENCE_RESOLUTION = (2560, 1440)
REFERENCE_UI_SCALE = 0.75


def _load_ui_scale() -> float:
    """RL's 'Interface Scale' video setting can't be read from the OS —
    it's an in-game slider with no exposed file/API — so it's a config
    value (rl_ui_scale / $RL_UI_SCALE) the user sets to match. Falls
    back to REFERENCE_UI_SCALE (i.e. no UI-scale correction applied) if
    unset or invalid."""
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

ROW_HEIGHT = 56  # vertical spacing between rows within a team's section
BOX_SIZE = 48  # size_px = 64 * UI_SCALE (linear, confirmed: 32@0.5, 48@0.75, 64@1.0)
SLOT_X = 714  # horizontal position — same for both teams, all sizes (probe-measured @ 0.75, 3v3)

# How many pixels to shrink every avatar picture on each side (0 = off).
AVATAR_INSET_PX = 0

SCOREBOARD_LAYOUTS = {
    # orange +ROW_HEIGHT (56) on Windows for 4/3/2: confirmed on real
    # hardware that orange row0 was rendering exactly one row spacing
    # too high in 2v2 and 3v3 (almost certainly 4v4 too) — same class
    # of bug the original Linux calibration already hit once for blue's
    # 3v3 value (see the historical "orange rendered a full row too
    # low" note this project's Linux history carries for that case).
    # 1v1 not reported broken, left untouched.
    # 4v4 orange: blind "+1 row spacing" guess (756->812) needed a
    # further +10/11px, same pattern as 3v3's orange fix — 823 (final
    # y=818). Blue not yet confirmed for 4v4.
    4: {"blue": 428, "orange": 823},  # was 756, then 812
    # 3v3: the blind "+1 row spacing" guess (754->810) landed close but
    # not exact — box_probe measured the real target as y=817 at s=1.0,
    # which needs base=822 here (not 810), a further +12 on top of the
    # row-spacing guess. Since 4v4 got the same kind of blind guess and
    # was never independently measured, treat its value as unconfirmed
    # too until checked the same way.
    # 3v3 blue: user confirmed it renders 16px too low — moved to 474
    # (was 490), then -2px to 472, -1px to 471, -1px more to 470 (final,
    # dialed in with box_probe). Orange in the same lobby size renders
    # correctly at every row with the current ROW_HEIGHT_QUAD, so this
    # is a row0 base-value fix only, not a spacing issue.
    3: {"blue": 470, "orange": 822},  # was 490, then 474, 472, 471, 470
    2: {"blue": 553, "orange": 812},  # was 756
    1: {"blue": 617, "orange": 761},
}

SCOREBOARD_UI_QUAD = {  # (c2, c1, c0) per axis, value = c2*s^2 + c1*s + c0
    # x/y refit on Windows: team_size=2 blue row0 target at s=1.0 is
    # x=526, y=548 (final +1px y nudge on top of the prior 526/547
    # pass). Refit through the SAME trusted s=0.5/s=0.75 points from
    # the original Linux calibration, only replacing the s=1.0 target
    # each time. Original (pre-Windows) curves: x=(0.0,-756.0,1281.0),
    # y=(4.0,-179.0,685.0).
    "x": (8.0, -766.0, 1284.0),
    "y": (308.0, -559.0, 799.0),
    "size": (0.0, 64.0, 0.0),
}
ROW_HEIGHT_QUAD = (88.0, -86.0, 74.0)  # c0 +3 total: +1 from original Linux calibration, +2 more on Windows — row1/row2/row3 have no calibration point of their own (pure ROW_HEIGHT_QUAD math), and were rendering too close together at every scale; same uniform-shift approach as the original +1
NAMEPLATE_UI_QUAD = {
    # x/y refit on Windows: box_probe-measured target at s=1.0 is
    # x=969, y=1150 (final -1px x nudge on top of the prior 970/1150
    # pass — dead on target now). Refit through the SAME trusted
    # s=0.5/s=0.75 points from the original Linux calibration, only
    # replacing the s=1.0 target each time. Original (pre-Windows)
    # curves: x=(8.0,-322.0,237.0), y=(-16.0,-271.0,1396.0).
    "x": (0.0, -312.0, 234.0),
    "y": (304.0, -671.0, 1516.0),
    "size": (0.0, 100.0, 0.0),
}

GOAL_NAMEPLATE_X_NUDGE = 3
GOAL_NAMEPLATE_Y_NUDGE = -35.25
_GOAL_NAMEPLATE_REFERENCE_SLOT = (
    1044 + GOAL_NAMEPLATE_X_NUDGE,
    1220 + GOAL_NAMEPLATE_Y_NUDGE,
    75,
    75,
)  # x, y, w, h — at REFERENCE_RESOLUTION / REFERENCE_UI_SCALE


def _quad(coefs: tuple, s: float) -> float:
    c2, c1, c0 = coefs
    return c2 * s * s + c1 * s + c0


def _round_up(value: float) -> int:
    """Any fraction above .0 rounds UP (not Python's round-half-to-even) —
    e.g. 39.08 -> 40. Matches an in-game rounding quirk found while
    calibrating the scoreboard size at 65% UI scale."""
    return math.ceil(value)


class WindowGeometryState:
    """Thread-safe holder for RL's actual window geometry (x, y, w, h) in
    real pixels — lets scoreboard/nameplate positions scale to whatever
    resolution RL is rendering at instead of assuming the hardcoded
    REFERENCE_RESOLUTION. On Linux this is fed by hyprctl; on Windows by
    win32gui. Keeps the last known value between polls."""

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


def _scale_slot(x: int, y: int, w: int, h: int, ui_quad: dict) -> tuple:
    """Scale a reference-layout (x, y, w, h) slot — measured at
    REFERENCE_RESOLUTION / REFERENCE_UI_SCALE — to RL's actual current
    window geometry and configured rl_ui_scale. See NOTES in
    gtk4_overlay.py for the full derivation of the two corrections
    applied here (UI-scale quadratic, then resolution)."""
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


def _scaled_row_height() -> float:
    return _quad(ROW_HEIGHT_QUAD, UI_SCALE)


def get_scoreboard_slots(team_size: int) -> list:
    """Build the (team, row_index, x, y, w, h) slot list for a given team
    size (1-4), using the calibrated layout closest to it, scaled to the
    current RL window geometry + UI scale (see _scale_slot)."""
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


class FocusState:
    """Thread-safe holder for whether Rocket League is currently the
    focused/active window."""

    def __init__(self):
        self._lock = threading.Lock()
        self._focused = True  # assume focused until we know otherwise

    def set(self, focused: bool) -> None:
        with self._lock:
            self._focused = focused

    def get(self) -> bool:
        with self._lock:
            return self._focused


def poll_bridge_loop(bridge_state: "BridgeState") -> None:
    """Runs in a background thread; never touches any GUI toolkit
    directly — just updates the shared BridgeState."""
    import json
    import time
    import urllib.request
    import urllib.error

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
