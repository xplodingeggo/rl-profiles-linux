#!/usr/bin/env python3
"""
Standalone pixel-grid calibration overlay.

Draws a labeled coordinate grid (green minor lines every 20px, red major
lines + yellow "x,y" labels every 100px) as its own click-through
layer-shell window — independent of gtk4_overlay.py, so it can run
alongside `rl-pfp start` (or on its own) without affecting avatar
rendering at all.

Use: screenshot RL with this running, zoom in, read the coordinate of
whatever you're measuring (e.g. a scoreboard box corner) off the nearest
label + minor gridlines. Repeat at different resolutions / RL "Interface
Scale" settings to gather real calibration data points.

Run:
  LD_PRELOAD=/path/to/libgtk4-layer-shell.so python3 -m rlpfp.grid_measure
  (or `rl-pfp grid`, which finds LD_PRELOAD for you the same way
  `rl-pfp start` does)
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import Gtk, Gtk4LayerShell, Gdk

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("grid-measure")

MINOR_SPACING_PX = 20
MAJOR_SPACING_PX = 100


def _draw_grid(area, cr, width, height):
    import cairo

    cr.set_line_width(1)
    cr.set_source_rgba(0, 1, 0, 0.25)
    for x in range(0, width, MINOR_SPACING_PX):
        cr.move_to(x + 0.5, 0)
        cr.line_to(x + 0.5, height)
    for y in range(0, height, MINOR_SPACING_PX):
        cr.move_to(0, y + 0.5)
        cr.line_to(width, y + 0.5)
    cr.stroke()

    cr.set_source_rgba(1, 0, 0, 0.7)
    for x in range(0, width, MAJOR_SPACING_PX):
        cr.move_to(x + 0.5, 0)
        cr.line_to(x + 0.5, height)
    for y in range(0, height, MAJOR_SPACING_PX):
        cr.move_to(0, y + 0.5)
        cr.line_to(width, y + 0.5)
    cr.stroke()

    cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(11)
    cr.set_source_rgba(1, 1, 0, 0.95)
    for x in range(0, width, MAJOR_SPACING_PX):
        for y in range(0, height, MAJOR_SPACING_PX):
            cr.move_to(x + 2, y + 12)
            cr.show_text(f"{x},{y}")


def _make_click_through(widget):
    try:
        import cairo
        surface = widget.get_surface()
        surface.set_input_region(cairo.Region())
    except Exception as e:
        log.warning("Could not set click-through input region: %s", e)


class GridApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.local.rl-pfp-grid-measure")

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)

        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b".transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        window.set_css_classes(["transparent"])

        if not Gtk4LayerShell.is_supported():
            log.error(
                "Your Wayland compositor doesn't support wlr-layer-shell "
                "(needed by this tool, same as gtk4_overlay.py). See the README."
            )
            sys.exit(1)

        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.NONE)
        for edge in (
            Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT,
            Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
        ):
            Gtk4LayerShell.set_anchor(window, edge, True)
            Gtk4LayerShell.set_margin(window, edge, 0)

        area = Gtk.DrawingArea()
        area.set_hexpand(True)
        area.set_vexpand(True)
        area.set_draw_func(_draw_grid)
        window.set_child(area)

        window.connect("realize", _make_click_through)
        window.present()


def main():
    app = GridApp()
    app.run([])


if __name__ == "__main__":
    main()
