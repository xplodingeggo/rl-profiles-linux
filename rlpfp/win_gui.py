"""
rlpfp/win_gui.py — native Windows control panel.

Launch: `rl-pfp gui`, `rl-pfp-gui.exe` (no console window, installed by
setup), or `python -m rlpfp.win_gui`.

Plain Tkinter + ttk, deliberately NOT Electron/webview — Tkinter ships
with Python (zero extra install), and with the 'vista' ttk theme
(available on Windows 7+) it renders through real Win32 common
controls, so it looks native and costs a few MB of RAM instead of a
bundled Chromium.

Tabs:
  Home       start/stop `rl-pfp start` as a child process, live log tail
  Setup      checks required packages are importable, installs missing ones
  Config     API keys / UI scale / bridge venv path -> config.json
  Controller scoreboard-button config + interactive detect, reusing
             win_controller.py's --detect / --detect-hid flows as a
             child process (their only "input" is a physical button
             press — no stdin prompts to worry about)
  Startup    Task Scheduler "run at log on" entry (see NOTE below)

NOTE on "Windows service": a real Windows Service (SCM) runs in
Session 0, isolated from the interactive desktop, so it CANNOT show
the overlay window at all (no desktop/window-station access — this is
a hard OS restriction, not a bug). The practical equivalent for a GUI
app like this is a Scheduled Task set to run at user logon, which runs
the app in your actual desktop session, just automatically — that's
what the Startup tab creates.
"""

from __future__ import annotations

import importlib.util
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from . import config

APP_TITLE = "RL PFP Overlay — Control Panel"
TASK_NAME = "RL PFP Overlay"

# (import name, pip package name, human label)
REQUIRED_PACKAGES = [
    ("aiohttp", "aiohttp", "aiohttp (stats bridge HTTP server)"),
    ("requests", "requests", "requests (avatar downloads)"),
    ("PIL", "Pillow", "Pillow (image resize/compositing)"),
    ("win32api", "pywin32", "pywin32 (overlay window + click-through)"),
    ("hid", "hidapi", "hidapi (raw-HID controller fallback, optional)"),
]

CONFIG_TAB_KEYS = [
    "steam_api_key", "psn_npsso", "xbox_api_key",
    "bridge_venv_python", "rl_ui_scale",
]
SECRET_KEYS = {"steam_api_key", "psn_npsso", "xbox_api_key"}
CONTROLLER_TAB_KEYS = [
    "scoreboard_button_windows", "scoreboard_button_dinput_index", "scoreboard_button_hid",
]

_FIELD_BY_KEY = {f[0]: f for f in config.FIELDS}

_CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW — avoid a flashing console


def _pythonw() -> str:
    """Prefer pythonw.exe (no console flash) if it sits next to
    sys.executable, e.g. for launching detached/background processes."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


class ScrollText(ttk.Frame):
    """Read-only auto-scrolling text log, themed to match ttk."""

    def __init__(self, parent, height=14):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap="word", state="disabled",
                             bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                             relief="flat", borderwidth=0, font=("Consolas", 9))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def append(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class ChildProcess:
    """Wraps a subprocess.Popen and pumps its combined stdout/stderr
    line-by-line into a queue.Queue, read from the Tk main thread via
    .after() polling (Tk isn't thread-safe, so widgets are never
    touched from the reader thread itself)."""

    def __init__(self, argv: list[str], env: dict | None = None):
        self.argv = argv
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)  # sentinel: process exited

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.is_alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(640, 480)

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        self.overlay_proc: ChildProcess | None = None
        self.detect_proc: ChildProcess | None = None
        self.config_vars: dict[str, tk.StringVar] = {}
        self.controller_vars: dict[str, tk.StringVar] = {}
        self.epic_placeholder_var = tk.BooleanVar()
        self.debug_var = tk.BooleanVar()
        self.show_secrets_var = tk.BooleanVar(value=False)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.home_tab = ttk.Frame(notebook)
        self.setup_tab = ttk.Frame(notebook)
        self.config_tab = ttk.Frame(notebook)
        self.controller_tab = ttk.Frame(notebook)
        self.startup_tab = ttk.Frame(notebook)

        notebook.add(self.home_tab, text="Home")
        notebook.add(self.setup_tab, text="Setup")
        notebook.add(self.config_tab, text="Config")
        notebook.add(self.controller_tab, text="Controller")
        notebook.add(self.startup_tab, text="Startup")

        self._build_home_tab()
        self._build_setup_tab()
        self._build_config_tab()
        self._build_controller_tab()
        self._build_startup_tab()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queues)
        self._refresh_deps()
        self._refresh_startup_status()

    # ------------------------------------------------------------ Home ---

    def _build_home_tab(self) -> None:
        top = ttk.Frame(self.home_tab)
        top.pack(fill="x", padx=10, pady=10)

        self.status_var = tk.StringVar(value="● Stopped")
        self.status_label = ttk.Label(top, textvariable=self.status_var,
                                       font=("Segoe UI", 11, "bold"), foreground="#a00")
        self.status_label.pack(side="left")

        btns = ttk.Frame(top)
        btns.pack(side="right")
        ttk.Checkbutton(btns, text="Debug HUD", variable=self.debug_var).pack(side="left", padx=(0, 12))
        self.start_btn = ttk.Button(btns, text="Start", command=self._start_overlay)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop_overlay, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        mid = ttk.Frame(self.home_tab)
        mid.pack(fill="x", padx=10)
        ttk.Button(mid, text="Open config folder", command=self._open_config_folder).pack(side="left")
        ttk.Button(mid, text="Open logs folder", command=self._open_logs_folder).pack(side="left", padx=6)

        ttk.Label(self.home_tab, text="Log output:").pack(anchor="w", padx=10, pady=(10, 0))
        self.home_log = ScrollText(self.home_tab, height=20)
        self.home_log.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    def _start_overlay(self) -> None:
        if self.overlay_proc and self.overlay_proc.is_alive():
            return
        argv = [sys.executable, "-m", "rlpfp.cli", "start", "--no-prompt"]
        if self.debug_var.get():
            argv.append("--debug")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self.home_log.clear()
        self.overlay_proc = ChildProcess(argv, env=env)
        self.status_var.set("● Running")
        self.status_label.configure(foreground="#0a0")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def _stop_overlay(self) -> None:
        if self.overlay_proc:
            self.overlay_proc.stop()
        self.status_var.set("● Stopped")
        self.status_label.configure(foreground="#a00")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _open_config_folder(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(config.CONFIG_DIR)

    def _open_logs_folder(self) -> None:
        log_dir = config.CACHE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(log_dir)

    # ----------------------------------------------------------- Setup ---

    def _build_setup_tab(self) -> None:
        ttk.Label(self.setup_tab, text="Required packages:", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 4))

        self.dep_rows: dict[str, tuple[ttk.Label, tuple]] = {}
        rows_frame = ttk.Frame(self.setup_tab)
        rows_frame.pack(fill="x", padx=10)
        for mod, pip_name, label in REQUIRED_PACKAGES:
            row = ttk.Frame(rows_frame)
            row.pack(fill="x", pady=1)
            status_lbl = ttk.Label(row, text="?", width=3, font=("Segoe UI", 10, "bold"))
            status_lbl.pack(side="left")
            ttk.Label(row, text=label).pack(side="left")
            self.dep_rows[mod] = (status_lbl, (mod, pip_name, label))

        btns = ttk.Frame(self.setup_tab)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Check again", command=self._refresh_deps).pack(side="left")
        self.install_btn = ttk.Button(btns, text="Install missing", command=self._install_missing_deps)
        self.install_btn.pack(side="left", padx=6)

        ttk.Label(self.setup_tab, text="Install output:").pack(anchor="w", padx=10)
        self.setup_log = ScrollText(self.setup_tab, height=14)
        self.setup_log.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    def _refresh_deps(self) -> None:
        self._missing_packages = []
        for mod, (lbl, (mod_name, pip_name, _label)) in self.dep_rows.items():
            found = importlib.util.find_spec(mod_name) is not None
            if found:
                lbl.configure(text="OK", foreground="#0a0")
            else:
                lbl.configure(text="X", foreground="#a00")
                self._missing_packages.append(pip_name)

    def _install_missing_deps(self) -> None:
        if not self._missing_packages:
            messagebox.showinfo(APP_TITLE, "Nothing to install — all packages already found.")
            return
        if self.detect_proc and self.detect_proc.is_alive():
            messagebox.showwarning(APP_TITLE, "Another background task is already running.")
            return
        pkgs = sorted(set(self._missing_packages))
        self.setup_log.clear()
        self.setup_log.append(f"Installing: {', '.join(pkgs)}\n\n")
        self.install_btn.configure(state="disabled")
        argv = [sys.executable, "-m", "pip", "install", *pkgs]
        self._install_proc = ChildProcess(argv)

    # ---------------------------------------------------------- Config ---

    def _build_config_tab(self) -> None:
        canvas_frame = ttk.Frame(self.config_tab)
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Checkbutton(canvas_frame, text="Show API keys", variable=self.show_secrets_var,
                         command=self._apply_secret_visibility).pack(anchor="w", pady=(0, 8))

        self.secret_entries: list[ttk.Entry] = []
        for key in CONFIG_TAB_KEYS:
            _key, _env, label, help_text, _prompt = _FIELD_BY_KEY[key]
            var = tk.StringVar()
            self.config_vars[key] = var

            ttk.Label(canvas_frame, text=label, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            entry = ttk.Entry(canvas_frame, textvariable=var, width=70)
            if key in SECRET_KEYS:
                entry.configure(show="*")
                self.secret_entries.append(entry)
            entry.pack(anchor="w", fill="x", pady=(1, 1))
            ttk.Label(canvas_frame, text=help_text, foreground="#666",
                      wraplength=680, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        ttk.Checkbutton(canvas_frame, text="Disable Epic placeholder image (use RL's default picture instead)",
                         variable=self.epic_placeholder_var).pack(anchor="w", pady=(4, 8))

        btn_row = ttk.Frame(canvas_frame)
        btn_row.pack(anchor="w", pady=6)
        ttk.Button(btn_row, text="Reload", command=self._load_config_into_form).pack(side="left")
        ttk.Button(btn_row, text="Save", command=self._save_config_from_form).pack(side="left", padx=6)
        self.config_save_status = ttk.Label(btn_row, text="")
        self.config_save_status.pack(side="left", padx=6)

        self._load_config_into_form()

    def _apply_secret_visibility(self) -> None:
        show = "" if self.show_secrets_var.get() else "*"
        for entry in self.secret_entries:
            entry.configure(show=show)

    def _load_config_into_form(self) -> None:
        cfg = config.load_config()
        for key, var in self.config_vars.items():
            var.set(str(cfg.get(key, "") or ""))
        for key, var in self.controller_vars.items():
            var.set(str(cfg.get(key, "") or ""))
        self.epic_placeholder_var.set(bool(cfg.get("epic_placeholder_disabled")))

    def _save_config_from_form(self) -> None:
        cfg = config.load_config()
        for key, var in self.config_vars.items():
            value = var.get().strip()
            if value:
                cfg[key] = value
            else:
                cfg.pop(key, None)
        for key, var in self.controller_vars.items():
            value = var.get().strip()
            if value:
                cfg[key] = value
            else:
                cfg.pop(key, None)
        cfg["epic_placeholder_disabled"] = self.epic_placeholder_var.get()
        config.save_config(cfg)
        self.config_save_status.configure(text=f"Saved to {config.CONFIG_PATH}")
        self.after(3000, lambda: self.config_save_status.configure(text=""))

    # ------------------------------------------------------- Controller --

    def _build_controller_tab(self) -> None:
        top = ttk.Frame(self.controller_tab)
        top.pack(fill="x", padx=10, pady=10)

        for key in CONTROLLER_TAB_KEYS:
            _key, _env, label, help_text, _prompt = _FIELD_BY_KEY[key]
            var = tk.StringVar()
            self.controller_vars[key] = var

            ttk.Label(top, text=label, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Entry(top, textvariable=var, width=70).pack(anchor="w", fill="x", pady=(1, 1))
            ttk.Label(top, text=help_text, foreground="#666",
                      wraplength=680, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        btn_row = ttk.Frame(self.controller_tab)
        btn_row.pack(fill="x", padx=10, pady=4)
        ttk.Button(btn_row, text="List devices", command=lambda: self._run_detect(["--list"])).pack(side="left")
        ttk.Button(btn_row, text="Detect (XInput/DirectInput)",
                   command=lambda: self._run_detect(["--detect"])).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Detect (Raw HID)",
                   command=lambda: self._run_detect(["--detect-hid"])).pack(side="left")
        self.detect_cancel_btn = ttk.Button(btn_row, text="Cancel", command=self._cancel_detect, state="disabled")
        self.detect_cancel_btn.pack(side="left", padx=6)
        ttk.Button(btn_row, text="Save", command=self._save_config_from_form).pack(side="right")

        ttk.Label(self.controller_tab, text="Press the button on your controller when prompted below:").pack(
            anchor="w", padx=10)
        self.controller_log = ScrollText(self.controller_tab, height=12)
        self.controller_log.pack(fill="both", expand=True, padx=10, pady=(2, 10))

    def _run_detect(self, extra_args: list[str]) -> None:
        if self.detect_proc and self.detect_proc.is_alive():
            messagebox.showwarning(APP_TITLE, "A detect/list operation is already running.")
            return
        self.controller_log.clear()
        argv = [sys.executable, "-m", "rlpfp.win_controller", *extra_args]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self.detect_proc = ChildProcess(argv, env=env)
        self.detect_cancel_btn.configure(state="normal")

    def _cancel_detect(self) -> None:
        if self.detect_proc:
            self.detect_proc.stop()
        self.detect_cancel_btn.configure(state="disabled")

    _DETECT_RESULT_RE = re.compile(r'Set this in config\.json.*?:\s*"(\w+)":\s*(.+?)\s*$')

    def _handle_detect_line(self, line: str) -> None:
        match = self._DETECT_RESULT_RE.search(line)
        if not match:
            return
        key, raw_value = match.group(1), match.group(2).strip()
        value = raw_value[1:-1] if raw_value.startswith('"') and raw_value.endswith('"') else raw_value
        if key in self.controller_vars:
            self.controller_vars[key].set(value)
            self._save_config_from_form()
            self.controller_log.append(f"\n>>> Saved {key} = {value} to config.json\n")

    # --------------------------------------------------------- Startup ---

    def _build_startup_tab(self) -> None:
        frame = ttk.Frame(self.startup_tab)
        frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(
            frame,
            text=(
                "Adds a Task Scheduler entry that starts RL PFP Overlay "
                "automatically when you log in — runs in your normal "
                "desktop session, minimized to no visible console.\n\n"
                "(Not a literal Windows Service: a real Service runs in an "
                "isolated session with no desktop access, so it couldn't "
                "show the overlay at all. This achieves the same "
                "\"starts automatically\" result in a way that actually "
                "works for a GUI app.)"
            ),
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.startup_status_var = tk.StringVar(value="Checking...")
        ttk.Label(frame, textvariable=self.startup_status_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")

        btns = ttk.Frame(frame)
        btns.pack(anchor="w", pady=10)
        ttk.Button(btns, text="Add to Windows startup", command=self._add_startup).pack(side="left")
        ttk.Button(btns, text="Remove from Windows startup", command=self._remove_startup).pack(side="left", padx=6)
        ttk.Button(btns, text="Refresh status", command=self._refresh_startup_status).pack(side="left")

    def _refresh_startup_status(self) -> None:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
        )
        enabled = result.returncode == 0
        self.startup_status_var.set("Currently: Enabled (runs at log on)" if enabled else "Currently: Disabled")

    def _add_startup(self) -> None:
        exe = _pythonw()
        cmd = f'"{exe}" -m rlpfp.cli start --no-prompt'
        result = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd, "/sc", "onlogon", "/rl", "limited", "/f"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            messagebox.showerror(APP_TITLE, f"Couldn't create the scheduled task:\n{result.stderr or result.stdout}")
        self._refresh_startup_status()

    def _remove_startup(self) -> None:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
        )
        self._refresh_startup_status()

    # ----------------------------------------------------------- misc ---

    def _poll_queues(self) -> None:
        if self.overlay_proc:
            try:
                while True:
                    line = self.overlay_proc.lines.get_nowait()
                    if line is None:
                        self._stop_overlay()
                        break
                    self.home_log.append(line)
            except queue.Empty:
                pass

        if hasattr(self, "_install_proc") and self._install_proc:
            try:
                while True:
                    line = self._install_proc.lines.get_nowait()
                    if line is None:
                        self.install_btn.configure(state="normal")
                        self._refresh_deps()
                        self._install_proc = None
                        break
                    self.setup_log.append(line)
            except queue.Empty:
                pass

        if self.detect_proc:
            try:
                while True:
                    line = self.detect_proc.lines.get_nowait()
                    if line is None:
                        self.detect_cancel_btn.configure(state="disabled")
                        self.detect_proc = None
                        break
                    self.controller_log.append(line)
                    self._handle_detect_line(line)
            except queue.Empty:
                pass

        self.after(100, self._poll_queues)

    def _on_close(self) -> None:
        if self.overlay_proc and self.overlay_proc.is_alive():
            if not messagebox.askyesno(APP_TITLE, "The overlay is still running — stop it before closing?"):
                self.destroy()
                return
            self.overlay_proc.stop()
        if self.detect_proc and self.detect_proc.is_alive():
            self.detect_proc.stop()
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
