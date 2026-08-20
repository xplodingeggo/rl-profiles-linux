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


def _load_use_linux_calibration() -> bool:
    """Experimental A/B flag (RL_LINUX_CALIBRATION=1) that swaps every
    calibrated constant below for the ORIGINAL, untouched Linux
    numbers — no Windows-specific row0/orange/nameplate corrections
    applied at all. Those were all measured at 75% and ported to other
    scales purely through the quad math; the theory being tested is
    that the quad math alone was always correct, and the growing pile
    of Windows-only patches was compensating for something else
    entirely (e.g. a measurement or window-geometry issue) rather than
    a real difference between the two OSes' rendering. Deliberately an
    env var, not a config.json field — this is a one-off comparison
    tool, not a setting anyone should leave on by accident."""
    raw = os.environ.get("RL_LINUX_CALIBRATION", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


USE_LINUX_CALIBRATION = _load_use_linux_calibration()
if USE_LINUX_CALIBRATION:
    log.info("RL_LINUX_CALIBRATION=1 — using the original Linux calibration constants, unmodified")

ROW_HEIGHT = 56  # vertical spacing between rows within a team's section
BOX_SIZE = 48  # size_px = 64 * UI_SCALE (linear, confirmed: 32@0.5, 48@0.75, 64@1.0)
SLOT_X = 714  # horizontal position — same for both teams, all sizes (probe-measured @ 0.75, 3v3)

# How many pixels to shrink every avatar picture on each side (0 = off).
AVATAR_INSET_PX = 0

# --- Original Linux calibration (measured at 75% UI scale, ported to
# other scales purely via the quad math below) — kept verbatim as the
# RL_LINUX_CALIBRATION=1 comparison baseline. See _load_use_linux_
# calibration()'s docstring for why this exists. -----------------------
_LINUX_ORIGINAL_SCOREBOARD_LAYOUTS = {
    4: {"blue": 428, "orange": 756},
    3: {"blue": 490, "orange": 754},
    2: {"blue": 553, "orange": 756},
    1: {"blue": 617, "orange": 761},
}
_LINUX_ORIGINAL_SCOREBOARD_UI_QUAD = {
    "x": (0.0, -756.0, 1281.0),
    "y": (4.0, -179.0, 685.0),
    "size": (0.0, 64.0, 0.0),
}
_LINUX_ORIGINAL_ROW_HEIGHT_QUAD = (88.0, -86.0, 72.0)
_LINUX_ORIGINAL_NAMEPLATE_UI_QUAD = {
    "x": (8.0, -322.0, 237.0),
    "y": (-16.0, -271.0, 1396.0),
    "size": (0.0, 100.0, 0.0),
}

_WINDOWS_SCOREBOARD_LAYOUTS = {
    # orange +ROW_HEIGHT (56) on Windows for 4/3/2: confirmed on real
    # hardware that orange row0 was rendering exactly one row spacing
    # too high in 2v2 and 3v3 (almost certainly 4v4 too) — same class
    # of bug the original Linux calibration already hit once for blue's
    # 3v3 value (see the historical "orange rendered a full row too
    # low" note this project's Linux history carries for that case).
    # 1v1 not reported broken, left untouched.
    #
    # IMPORTANT: every row0 base below EXCEPT 3v3 orange/blue and 1v1
    # blue (fixed) still has its s=1.0-only pixel correction baked
    # directly into this reference-scale (0.75) value — the same bug
    # that broke 3v3 orange / 1v1 blue at 75% scale (see those entries'
    # comments + EXTRA_Y_QUAD below for the fix and full explanation).
    # Everything else is only known-correct at s=1.0 right now; treat
    # it as broken at any other UI scale until it gets the same
    # two-point (0.75 + 1.0) re-measurement.
    # 4v4 orange: blind "+1 row spacing" guess (756->812) needed a
    # further +10/11px, same pattern as 3v3's orange fix — 823 (final
    # y=818). Blue: user confirmed it renders 38-40px too low (~39,
    # landed on the even-number candidate at their request) — moved to
    # 389 (final y=384), then -1px to 388 (final y=383), then -1px more
    # to 387 (final y=382).
    4: {"blue": 387, "orange": 823},  # blue was 428, then 389, 388; orange was 756, then 812
    # 3v3: the blind "+1 row spacing" guess (754->810) landed close but
    # not exact — box_probe measured the real target as y=817 at s=1.0,
    # which needed +12 on top of the row-spacing guess (810->822) IF
    # squeezed entirely into this base value. That's exactly what broke
    # 75% scale (reported later): baking a s=1.0-only correction into
    # this reference-scale (0.75) base shifts EVERY scale by the same
    # amount, since this value feeds straight through when s==0.75 (no
    # quad correction applies there). box_probe re-measured directly at
    # s=0.75: first pass gave y=795, corrected to y=789 (6px measurement
    # error), then +1px to y=790, then +2px more to y=792 (final) — so
    # 822 was simply wrong as a reference value; this is now 792 (the
    # real target at 0.75), and the extra needed specifically at s=1.0
    # now lives in EXTRA_Y_QUAD["orange"] below instead of here. See
    # that dict's comment for why orange needs its own extra curve on
    # top of the shared one.
    # 3v3 blue: was confirmed correct at s=1.0 (470, dialed in with
    # box_probe) — but that means it's suspect at every OTHER scale for
    # the exact same reason 3v3 orange was: an s=1.0-only measurement
    # baked into this reference-scale base. box_probe-measured target
    # at s=0.75: (716, 530), corrected 1px up-left to (715, 529), then
    # 1px up-left again to (714, 528) (final — x nudge now 0, exactly
    # SLOT_X) — base reset to 528 (see EXTRA_X_NUDGE/EXTRA_Y_QUAD
    # below), s=1.0 target (465) preserved.
    3: {"blue": 528, "orange": 792},  # blue was 490/470(s=1.0-only fudge)/530/529; orange was 490/822(fudge)/795(6px error)/789/790
    # 2v2: blue "confirmed correct as-is" was only ever checked at
    # s=1.0 (548, base 553 + shared dy(1.0)=-5) — same s=1.0-only
    # blind spot as everything else here. box_probe target at s=0.75:
    # (716, 593), corrected 1px up-left to (715, 592), then +1px more
    # up to (715, 591) (final) — base reset to 591, x nudge +2->+1 (see
    # EXTRA_X_NUDGE/EXTRA_Y_QUAD below), s=1.0 target (548) preserved.
    # Orange (blind "+1 row spacing" guess, never independently
    # measured at s=1.0 or s=0.75) confirmed 25px too low at s=0.75:
    # target is 822-25=797, then -2px to 795, then -1px more to 794
    # (final) — base reset to 794; EXTRA_Y_QUAD["orange"][2] keeps the
    # previously-confirmed s=1.0 target (817) exact.
    2: {"blue": 591, "orange": 794},  # blue was 553(s=1.0-only fudge)/535/593/592; orange was 756/812/821/822(s=1.0-only fudge)/797/795
    # 1v1: blue box_probe target at s=0.75 was (716, 656), corrected by
    # 1px up-left to (715, 655) — base reset to 655, x nudge 2->1 (see
    # EXTRA_X_NUDGE/EXTRA_Y_QUAD below), s=1.0 target (632) still exact.
    # Orange confirmed 24px too high at s=0.75 (previous base 822 was
    # the same s=1.0-only fudge as everywhere else): target is
    # 822-24=798. Base reset to 798; EXTRA_Y_QUAD["orange"][1] keeps
    # the previously-confirmed s=1.0 target (817) exact.
    1: {"blue": 655, "orange": 798},  # blue was 617/635/637/656; orange was 761/821/822(s=1.0-only fudge)
}

_WINDOWS_SCOREBOARD_UI_QUAD = {  # (c2, c1, c0) per axis, value = c2*s^2 + c1*s + c0
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
_WINDOWS_ROW_HEIGHT_QUAD = (88.0, -86.0, 72.0)  # c0 net +1 vs. original Linux calibration: +1, then +2 more (74) which turned out 2px too generous once measured row-by-row past row0 at 75% (rows drift increasingly imprecise the further down the block) — backed off by 2 (74->72). Uniform shift (c0 only) — applies the same -2px at every scale, same as every previous row-height adjustment here. row1/row2/row3 have no calibration point of their own (pure ROW_HEIGHT_QUAD math), so this is the only lever for their spacing.

# Neither team's row0 scales with UI scale exactly the way
# SCOREBOARD_UI_QUAD's shared y-curve predicts — it was fit from a
# single blue measurement, and both teams turn out to drift from it by
# their own team/size-specific amount (orange more visibly, but 1v1
# blue confirmed it too — likely each team/row-count combo has its own
# anchor, e.g. a "VS"/divider element, or vertical centering that
# depends on how many rows are in the block, neither of which scales
# linearly the same way row-to-row spacing does).
#
# EXTRA_Y_QUAD is an ADDITIONAL delta layered on top of the normal
# _scale_slot math (added to the reference y before it goes in), fit
# per (team, team_size) through s=0.5 (no data yet — assumed 0, same
# "trusted" placeholder default used elsewhere in this file for
# unmeasured points), s=0.75 (forced to 0 — SCOREBOARD_LAYOUTS' base IS
# the s=0.75 target directly now), and s=1.0 (forced to whatever gap
# the s=1.0 measurement leaves after the shared curve is applied).
#
# Confirmed so far (base @ s=0.75 -> target @ s=1.0, gap = the extra
# curve's value at s=1.0):
#   orange 3v3: base 792 -> 817 (25px gap)
#   orange 2v2: base 794 -> 817 (28px gap, after the -2px and -1px fixes)
#   orange 1v1: base 798 -> 817 (24px gap — all three orange team sizes
#               converge on ~24-25px regardless of base/team_size;
#               unconfirmed whether that's meaningful or coincidence,
#               but each is still fit independently)
#   blue   3v3: base 528 -> 465 (-58px gap, after two 1px up-left fixes)
#   blue   2v2: base 591 -> 548 (-38px gap, after two 1px up fixes)
#   blue   1v1: base 655 -> 632 (-18px gap, after the 1px up-left fix)
# Everything else still has its s=1.0-only calibration baked directly
# into SCOREBOARD_LAYOUTS (see that dict's comments) and needs the same
# two-point re-measurement before it can be trusted off 100% scale.
_WINDOWS_EXTRA_Y_QUAD = {
    "blue": {
        1: (-144.0, 180.0, -54.0),
        2: (-304.0, 380.0, -114.0),
        3: (-464.0, 580.0, -174.0),
    },
    "orange": {
        1: (192.0, -240.0, 72.0),
        2: (224.0, -280.0, 84.0),
        3: (240.0, -300.0, 90.0),
    },
}

# Same idea as EXTRA_Y_QUAD but for x, and flat (not scale-dependent) —
# only s=0.75 measurements exist so far (1v1 blue: x=715, 1px right of
# the shared SLOT_X-derived 714; 2v2 blue: x=715, also 1px right), not
# enough to fit a curve per team_size, so each applies uniformly at
# every scale until an s=1.0 measurement either confirms that or shows
# it also needs its own curve like EXTRA_Y_QUAD.
# 3v3 blue's x turned out to need no nudge at all (settled at 714 ==
# SLOT_X after two 1px-left corrections) — no entry needed.
_WINDOWS_EXTRA_X_NUDGE = {
    "blue": {
        1: 1,
        2: 1,
    },
}
_WINDOWS_NAMEPLATE_UI_QUAD = {
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

# --- Active set — RL_LINUX_CALIBRATION=1 swaps in the untouched Linux
# originals (with no orange-extra correction, since that's a
# Windows-specific patch with no Linux equivalent); otherwise the
# Windows-calibrated constants above. -----------------------------------
if USE_LINUX_CALIBRATION:
    SCOREBOARD_LAYOUTS = _LINUX_ORIGINAL_SCOREBOARD_LAYOUTS
    SCOREBOARD_UI_QUAD = _LINUX_ORIGINAL_SCOREBOARD_UI_QUAD
    ROW_HEIGHT_QUAD = _LINUX_ORIGINAL_ROW_HEIGHT_QUAD
    NAMEPLATE_UI_QUAD = _LINUX_ORIGINAL_NAMEPLATE_UI_QUAD
    EXTRA_Y_QUAD = {}
    EXTRA_X_NUDGE = {}
else:
    SCOREBOARD_LAYOUTS = _WINDOWS_SCOREBOARD_LAYOUTS
    SCOREBOARD_UI_QUAD = _WINDOWS_SCOREBOARD_UI_QUAD
    ROW_HEIGHT_QUAD = _WINDOWS_ROW_HEIGHT_QUAD
    NAMEPLATE_UI_QUAD = _WINDOWS_NAMEPLATE_UI_QUAD
    EXTRA_Y_QUAD = _WINDOWS_EXTRA_Y_QUAD
    EXTRA_X_NUDGE = _WINDOWS_EXTRA_X_NUDGE

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
    row_height = _scaled_row_height()
    slots = []
    for team in ("blue", "orange"):
        row0_x = SLOT_X + EXTRA_X_NUDGE.get(team, {}).get(team_size, 0)
        row0_y = layout[team]
        extra_y = EXTRA_Y_QUAD.get(team, {}).get(team_size)
        if extra_y is not None:
            row0_y += _quad(extra_y, UI_SCALE)
        for row in range(team_size):
            x, y, w, h = _scale_slot(row0_x, row0_y + row * row_height, BOX_SIZE, BOX_SIZE, SCOREBOARD_UI_QUAD)
            slots.append((team, row, x, y, w, h))
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
