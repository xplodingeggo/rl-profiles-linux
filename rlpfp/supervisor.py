"""
Supervisor for `rl-pfp start`.

Runs bridge, overlay, and controller as three SEPARATE OS processes
(not merged into one) so each keeps its own memory space, its own
crash domain, and can be attached to individually with pdb/gdb/strace
without touching the other two — you can still run any one of them
completely standalone for debugging:

    python3 -m rlpfp.rl_stats_bridge --verbose
    python3 -m rlpfp.gtk4_overlay --debug
    python3 -m rlpfp.controller_listener --list

What this module adds on top of "three terminals":
  - starts bridge first and waits for it to actually be listening on
    127.0.0.1:9090 before starting overlay/controller, instead of
    relying on their own retry loops to paper over the startup race
  - interleaves all three processes' stdout/stderr into one prefixed
    stream ([bridge] / [overlay] / [controller]) AND writes each to
    its own file under ~/.cache/rl-pfp-overlay/logs/, so you can
    `tail -f` just one component if you're debugging it in isolation
  - one Ctrl+C (SIGINT) or SIGTERM stops all three in a sane order
    (controller + overlay first, since they're bridge *clients* and
    otherwise log noisy "connection refused" while bridge is still
    going down; bridge last), with a kill fallback if any hang
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

BRIDGE_URL = "http://127.0.0.1:9090"

# The overlay needs gtk4-layer-shell preloaded, same requirement your
# original gtk4_overlay.py always had — LD_PRELOAD is a shell-level env
# var, not something Python can set for its own already-running process,
# so this has to be injected before the overlay child even starts, not
# handled inside overlay.py itself.
#
# Common install locations across distros; first match wins. If yours
# isn't here, either add it, or export LD_PRELOAD yourself before
# running `rl-pfp start` (that still works — this only fills it in when
# it's not already set).
LAYER_SHELL_LIB_CANDIDATES = [
    "/usr/lib/libgtk4-layer-shell.so",                       # Arch
    "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so",       # Debian/Ubuntu
    "/usr/lib64/libgtk4-layer-shell.so",                      # Fedora/RHEL
    "/usr/local/lib/libgtk4-layer-shell.so",                  # built from source
]


def _find_layer_shell_lib() -> str | None:
    for candidate in LAYER_SHELL_LIB_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _overlay_env() -> dict[str, str]:
    """Extra env vars for the overlay child specifically (not bridge/
    controller, which don't need this)."""
    if os.environ.get("LD_PRELOAD"):
        # Already set (e.g. exported in the shell rl-pfp was launched
        # from) — respect it as-is, don't override.
        return {}

    lib_path = _find_layer_shell_lib()
    if lib_path:
        return {"LD_PRELOAD": lib_path}

    print(
        "[supervisor] Couldn't find libgtk4-layer-shell.so in the usual "
        "locations, and LD_PRELOAD isn't already set — the overlay will "
        "likely fail to init layer-shell. Either install gtk4-layer-shell "
        "for your distro, or run:\n"
        "  LD_PRELOAD=/path/to/libgtk4-layer-shell.so rl-pfp start",
        flush=True,
    )
    return {}
BRIDGE_READY_TIMEOUT_SECONDS = 15
BRIDGE_READY_POLL_INTERVAL = 0.25

TERMINATE_WAIT_SECONDS = 5

LOG_DIR = Path.home() / ".cache" / "rl-pfp-overlay" / "logs"

# Directory containing the rlpfp/ package itself. Injected as PYTHONPATH
# for any child spawned under a DIFFERENT interpreter than sys.executable
# (e.g. bridge/controller's venv) so that interpreter can `import rlpfp`
# without needing `pip install -e .` run a second time inside that venv —
# it only needs aiohttp/evdev installed, this package is found via path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Order matters for both startup and shutdown.
COMPONENTS = ["bridge", "overlay", "controller"]

# ANSI colors per component, for the interleaved terminal stream. Falls
# back gracefully — these are cosmetic only, log files never contain them.
_PREFIX_COLORS = {
    "bridge": "\033[36m",     # cyan
    "overlay": "\033[35m",    # magenta
    "controller": "\033[33m", # yellow
}
_RESET = "\033[0m"


def _foreign_interpreter_env(interpreter: str) -> dict[str, str]:
    """PYTHONPATH injection for a child running under a DIFFERENT
    interpreter than sys.executable (e.g. bridge_venv_python), so that
    interpreter can `import rlpfp` via path alone — no need to
    `pip install -e .` a second time inside that venv, it only needs
    its own deps (aiohttp, evdev) installed."""
    if interpreter == sys.executable:
        return {}
    existing = os.environ.get("PYTHONPATH", "")
    new_path = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    return {"PYTHONPATH": new_path}


class Child:
    def __init__(self, name: str, argv: list[str], extra_env: dict[str, str] | None = None):
        self.name = name
        self.argv = argv
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.log_path = LOG_DIR / f"{name}.log"
        self._reader_thread: threading.Thread | None = None

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **self.extra_env} if self.extra_env else None
        self.proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self._reader_thread = threading.Thread(
            target=self._pump_output, daemon=True
        )
        self._reader_thread.start()

    def _pump_output(self) -> None:
        color = _PREFIX_COLORS.get(self.name, "")
        prefix = f"{color}[{self.name}]{_RESET}"
        # 'a' not 'w': a component may be restarted within one supervisor
        # run in the future; don't clobber earlier lines from this session.
        with open(self.log_path, "a", buffering=1) as logf:
            logf.write(f"\n--- session start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            if self.proc and self.proc.stdout:
                for line in self.proc.stdout:
                    line = line.rstrip("\n")
                    print(f"{prefix} {line}", flush=True)
                    logf.write(line + "\n")

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def terminate(self) -> None:
        if self.proc and self.is_alive():
            self.proc.terminate()

    def kill(self) -> None:
        if self.proc and self.is_alive():
            self.proc.kill()

    def wait(self, timeout: float) -> bool:
        """Returns True if it exited within timeout."""
        if not self.proc:
            return True
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


def _wait_for_bridge_ready(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BRIDGE_URL}/current-lobby", timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(BRIDGE_READY_POLL_INTERVAL)
    return False


def run(*, debug: bool, verbose: bool, bridge_python: str | None = None) -> int:
    """
    Starts bridge, overlay, controller as child processes and blocks
    until interrupted. Returns a process exit code.

    bridge_python: interpreter to use for bridge + controller (their
    venv, if you split environments — e.g. aiohttp/evdev in a venv,
    separate from system Python). Defaults to sys.executable, i.e.
    whatever interpreter rl-pfp itself is running under. The overlay
    ALWAYS uses sys.executable specifically (not bridge_python), since
    PyGObject/gtk4-layer-shell typically has to be system Python — so
    rl-pfp itself should generally be installed/run under system Python
    too, with bridge_python pointing at a separate venv just for
    bridge/controller.
    """
    bridge_interpreter = bridge_python or sys.executable

    bridge_argv = [bridge_interpreter, "-m", "rlpfp.rl_stats_bridge"]
    if verbose:
        bridge_argv.append("--verbose")

    overlay_argv = [sys.executable, "-m", "rlpfp.gtk4_overlay"]
    if debug:
        overlay_argv.append("--debug")

    controller_argv = [bridge_interpreter, "-m", "rlpfp.controller_listener"]

    children: dict[str, Child] = {
        "bridge": Child("bridge", bridge_argv, extra_env=_foreign_interpreter_env(bridge_interpreter)),
        "overlay": Child("overlay", overlay_argv, extra_env=_overlay_env()),
        "controller": Child("controller", controller_argv, extra_env=_foreign_interpreter_env(bridge_interpreter)),
    }

    print(f"Logs: interleaved below, and per-component under {LOG_DIR}/", flush=True)

    children["bridge"].start()
    print("Waiting for bridge to come up on 127.0.0.1:9090 ...", flush=True)
    if not _wait_for_bridge_ready(BRIDGE_READY_TIMEOUT_SECONDS):
        print(
            f"[supervisor] bridge didn't come up within "
            f"{BRIDGE_READY_TIMEOUT_SECONDS}s — starting overlay/controller "
            f"anyway (they retry on their own), but something's likely wrong.",
            flush=True,
        )
    else:
        print("[supervisor] bridge is up.", flush=True)

    children["overlay"].start()
    children["controller"].start()

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        print(f"\n[supervisor] received signal {signum}, shutting down...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    exit_code = 0
    try:
        while not stop_event.is_set():
            # If any child dies on its own (not via our shutdown path),
            # treat it as fatal for the whole group rather than silently
            # running degraded (e.g. overlay dies, bridge and controller
            # keep running pointlessly).
            for name, child in children.items():
                if not child.is_alive():
                    code = child.proc.returncode if child.proc else None
                    print(
                        f"[supervisor] '{name}' exited unexpectedly "
                        f"(code {code}) — stopping the rest.",
                        flush=True,
                    )
                    stop_event.set()
                    exit_code = code or 1
                    break
            time.sleep(0.5)
    finally:
        # Shutdown order: controller + overlay (bridge clients) before
        # bridge itself, to avoid a burst of "connection refused" noise
        # from clients still polling a bridge that's already gone.
        for name in ["controller", "overlay", "bridge"]:
            children[name].terminate()
        for name in ["controller", "overlay", "bridge"]:
            if not children[name].wait(TERMINATE_WAIT_SECONDS):
                print(f"[supervisor] '{name}' didn't exit in time, killing.", flush=True)
                children[name].kill()
                children[name].wait(2)
        print("[supervisor] all components stopped.", flush=True)

    return exit_code
