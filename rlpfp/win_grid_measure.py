#!/usr/bin/env python3
"""
Windows: standalone pixel-grid calibration overlay.

Windows port of grid_measure.py — same purpose, Tkinter + Win32 instead
of GTK4 + layer-shell. Draws a labeled coordinate grid (green minor
lines every 20px, red major lines + yellow "x,y" labels every 100px) as
a click-through, always-on-top window — independent of win_overlay.py,
so it can run alongside `rl-pfp start` without affecting avatar
rendering.

Use: screenshot RL with this running, zoom in, read the coordinate of
whatever you're measuring (e.g. a scoreboard box corner) off the nearest
label + minor gridlines. The calibration data in layout.py was measured
on Linux — this tool exists so you can confirm/re-derive it if RL's UI
happens to render even a pixel different on Windows.

Run:
  rl-pfp grid
  (or: python -m rlpfp.win_grid_measure)
"""

import ctypes
import logging
import tkinter as tk

import win32api
import win32con
import win32gui

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("win-grid-measure")

MINOR_SPACING_PX = 20
MAJOR_SPACING_PX = 100
TRANSPARENT_KEY = "#ff00fe"


def _virtual_screen_rect():
    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return x, y, w, h


def _make_click_through(hwnd: int) -> None:
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    win32gui.SetWindowLong(
        hwnd, win32con.GWL_EXSTYLE,
        ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT,
    )


def _draw_grid(canvas: tk.Canvas, width: int, height: int) -> None:
    canvas.delete("all")
    for x in range(0, width, MINOR_SPACING_PX):
        canvas.create_line(x, 0, x, height, fill="#00ff00", stipple="gray25")
    for y in range(0, height, MINOR_SPACING_PX):
        canvas.create_line(0, y, width, y, fill="#00ff00", stipple="gray25")

    for x in range(0, width, MAJOR_SPACING_PX):
        canvas.create_line(x, 0, x, height, fill="#ff0000")
    for y in range(0, height, MAJOR_SPACING_PX):
        canvas.create_line(0, y, width, y, fill="#ff0000")

    for x in range(0, width, MAJOR_SPACING_PX):
        for y in range(0, height, MAJOR_SPACING_PX):
            canvas.create_text(
                x + 2, y + 8, text=f"{x},{y}", fill="#ffff00",
                font=("Consolas", 8, "bold"), anchor="w",
            )


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    screen_x, screen_y, screen_w, screen_h = _virtual_screen_rect()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=TRANSPARENT_KEY)
    root.attributes("-transparentcolor", TRANSPARENT_KEY)
    root.geometry(f"{screen_w}x{screen_h}+{screen_x}+{screen_y}")

    canvas = tk.Canvas(root, width=screen_w, height=screen_h, bg=TRANSPARENT_KEY,
                        highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    _draw_grid(canvas, screen_w, screen_h)

    def enable_click_through():
        root.update_idletasks()
        try:
            _make_click_through(root.winfo_id())
            log.info("Click-through enabled.")
        except Exception:
            log.exception("Could not enable click-through")

    root.after(0, enable_click_through)
    root.mainloop()


if __name__ == "__main__":
    main()
