#!/usr/bin/env python3
"""
Windows: standalone box-position probe overlay.

Windows port of box_probe.py — Tkinter + Win32 instead of GTK4 +
layer-shell. Draws a single outlined box at an (x, y, w, h) you control
live from a control-panel window, on top of Rocket League — so you can
nudge numbers until the box lines up with the real scoreboard/nameplate
icon, then read off the exact pixel values to update layout.py.
Independent of win_overlay.py — safe to run alongside `rl-pfp start`.

Run:
  rl-pfp probe
  (or: python -m rlpfp.win_box_probe)
"""

import ctypes
import logging
import tkinter as tk

import win32api
import win32con
import win32gui

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("win-box-probe")

TRANSPARENT_KEY = "#ff00fe"


def _virtual_screen_rect():
    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return x, y, w, h


def _make_click_through(hwnd: int) -> None:
    """See win_overlay.py's _make_click_through docstring — SetWindowLong
    here resets the -transparentcolor colorkey as a side effect, so it
    must be re-applied or the window paints solid black instead of
    being transparent."""
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
    # frames before the compositor catches up (a race, not deterministic).
    win32gui.RedrawWindow(
        hwnd, None, None,
        win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW | win32con.RDW_ALLCHILDREN,
    )


class BoxState:
    def __init__(self):
        self.x = 712.0
        self.y = 550.0
        self.w = 45.0
        self.h = 45.0


class ProbeApp:
    def __init__(self):
        self.state = BoxState()

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        self.screen_x, self.screen_y, self.screen_w, self.screen_h = _virtual_screen_rect()

        self.overlay = tk.Tk()
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.configure(bg=TRANSPARENT_KEY)
        self.overlay.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.overlay.geometry(f"{self.screen_w}x{self.screen_h}+{self.screen_x}+{self.screen_y}")

        self.canvas = tk.Canvas(
            self.overlay, width=self.screen_w, height=self.screen_h,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # 150ms delay so Tk's own -transparentcolor setup lands before
        # we touch the window's extended styles — see win_overlay.py's
        # _make_click_through docstring for why this matters.
        self.overlay.after(150, self._enable_click_through)

        self._build_control_window()
        self._redraw()

    def _enable_click_through(self):
        self.overlay.update()  # full update, not just idletasks — flush the initial paint
        try:
            _make_click_through(self.overlay.winfo_id())
        except Exception:
            log.exception("Could not enable click-through")

    def _redraw(self):
        s = self.state
        self.canvas.delete("all")
        self.canvas.create_rectangle(
            s.x, s.y, s.x + s.w, s.y + s.h, outline="#ff0000", width=2,
        )
        self.canvas.create_line(s.x - 8, s.y, s.x + 8, s.y, fill="#00ffff")
        self.canvas.create_line(s.x, s.y - 8, s.x, s.y + 8, fill="#00ffff")
        self.canvas.create_text(
            s.x, max(0, s.y - 12),
            text=f"x={s.x:g} y={s.y:g} w={s.w:g} h={s.h:g}",
            fill="#ffff00", font=("Consolas", 10, "bold"), anchor="w",
        )
        self._force_recomposite()

    def _force_recomposite(self):
        """canvas.delete("all") + recreate updates Tk's own internal
        state fine, but a WS_EX_LAYERED|WS_EX_TRANSPARENT window (see
        _make_click_through) doesn't reliably recomposite the actual
        on-screen surface on its own afterward — the old box's pixels
        stay visually "burned in" until forced, the same underlying
        quirk win_overlay.py's black-screen fix worked around at
        startup. Here it has to run on every redraw, not just once."""
        self.overlay.update_idletasks()
        try:
            hwnd = self.overlay.winfo_id()
            win32gui.RedrawWindow(
                hwnd, None, None,
                win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW | win32con.RDW_ALLCHILDREN,
            )
        except Exception:
            log.exception("Could not force recomposite")

    def _build_control_window(self):
        window = tk.Toplevel()
        window.title("box probe")
        window.geometry("260x220")
        window.attributes("-topmost", True)

        def make_row(label_text, initial, row, attr):
            tk.Label(window, text=label_text).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            var = tk.DoubleVar(value=initial)

            def on_change(*_args):
                try:
                    setattr(self.state, attr, var.get())
                    self._redraw()
                except tk.TclError:
                    pass  # mid-typing invalid float; ignore until it resolves

            var.trace_add("write", on_change)
            spin = tk.Spinbox(
                window, from_=-2000, to=4000, increment=1, textvariable=var, width=10,
            )
            spin.grid(row=row, column=1, padx=8, pady=4)

        make_row("x", self.state.x, 0, "x")
        make_row("y", self.state.y, 1, "y")
        make_row("w", self.state.w, 2, "w")
        make_row("h", self.state.h, 3, "h")

        tk.Label(
            window,
            text="Nudge until the box+crosshair lines up\n"
                 "with the real icon's top-left corner\n"
                 "in Rocket League, then read the values off.",
            justify="center",
        ).grid(row=4, column=0, columnspan=2, padx=8, pady=10)

        window.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.overlay.destroy()

    def run(self):
        self.overlay.mainloop()


def main():
    app = ProbeApp()
    app.run()


if __name__ == "__main__":
    main()
