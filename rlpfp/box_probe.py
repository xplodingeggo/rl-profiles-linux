#!/usr/bin/env python3
"""
Standalone box-position probe overlay.

Draws a single outlined box at an (x, y, w, h) you control live from a
control-panel window, on top of Rocket League — so you can nudge numbers
until the box lines up with the real scoreboard/nameplate icon, then read
off the exact pixel values. Independent of gtk4_overlay.py, doesn't touch
avatar rendering at all — safe to run alongside `rl-pfp start`.

Run:
  rl-pfp probe
  (or: LD_PRELOAD=/path/to/libgtk4-layer-shell.so python3 -m rlpfp.box_probe)
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import Gtk, Gtk4LayerShell, Gdk

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("box-probe")


class BoxState:
    def __init__(self):
        self.x = 712.0
        self.y = 550.0
        self.w = 45.0
        self.h = 45.0


def _draw_box(state, area, cr, width, height):
    import cairo

    cr.set_line_width(2)
    cr.set_source_rgba(1, 0, 0, 0.9)
    cr.rectangle(state.x + 0.5, state.y + 0.5, state.w, state.h)
    cr.stroke()

    # Crosshair at the top-left corner — the point you're actually lining
    # up against when measuring "top-left corner" coordinates.
    cr.set_line_width(1)
    cr.set_source_rgba(0, 1, 1, 0.9)
    cr.move_to(state.x - 8, state.y)
    cr.line_to(state.x + 8, state.y)
    cr.move_to(state.x, state.y - 8)
    cr.line_to(state.x, state.y + 8)
    cr.stroke()

    cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(13)
    cr.set_source_rgba(1, 1, 0, 0.95)
    cr.move_to(state.x, max(0, state.y - 10))
    cr.show_text(f"x={state.x:g} y={state.y:g} w={state.w:g} h={state.h:g}")


def _make_click_through(widget):
    try:
        import cairo
        surface = widget.get_surface()
        surface.set_input_region(cairo.Region())
    except Exception as e:
        log.warning("Could not set click-through input region: %s", e)


class ProbeApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.local.rl-pfp-box-probe")
        self.state = BoxState()
        self.area = None

    def do_activate(self):
        if not Gtk4LayerShell.is_supported():
            log.error(
                "Your Wayland compositor doesn't support wlr-layer-shell "
                "(needed by this tool, same as gtk4_overlay.py). See the README."
            )
            sys.exit(1)

        self._build_overlay_window()
        self._build_control_window()

    def _build_overlay_window(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)

        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b".transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        window.set_css_classes(["transparent"])

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
        area.set_draw_func(lambda a, cr, w, h: _draw_box(self.state, a, cr, w, h))
        window.set_child(area)
        self.area = area

        window.connect("realize", _make_click_through)
        window.present()

    def _build_control_window(self):
        window = Gtk.ApplicationWindow(application=self, title="box probe")
        window.set_default_size(260, 180)

        grid = Gtk.Grid(row_spacing=6, column_spacing=8, margin_top=10,
                         margin_bottom=10, margin_start=10, margin_end=10)
        window.set_child(grid)

        def make_spin(label_text, initial, row, attr):
            label = Gtk.Label(label=label_text, halign=Gtk.Align.START)
            adjustment = Gtk.Adjustment(
                value=initial, lower=-2000, upper=4000, step_increment=1, page_increment=10,
            )
            spin = Gtk.SpinButton(adjustment=adjustment, digits=1, climb_rate=1)
            spin.set_value(initial)

            def on_change(sb):
                setattr(self.state, attr, sb.get_value())
                if self.area is not None:
                    self.area.queue_draw()

            spin.connect("value-changed", on_change)
            grid.attach(label, 0, row, 1, 1)
            grid.attach(spin, 1, row, 1, 1)

        make_spin("x", self.state.x, 0, "x")
        make_spin("y", self.state.y, 1, "y")
        make_spin("w", self.state.w, 2, "w")
        make_spin("h", self.state.h, 3, "h")

        hint = Gtk.Label(
            label="Nudge until the box+crosshair lines up\nwith the real icon's top-left corner\nin Rocket League, then read the values off.",
            justify=Gtk.Justification.CENTER, margin_top=8,
        )
        grid.attach(hint, 0, 4, 2, 1)

        window.present()


def main():
    app = ProbeApp()
    app.run([])


if __name__ == "__main__":
    main()
