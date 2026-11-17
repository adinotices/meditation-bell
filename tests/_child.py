"""Runs the real app in a child process for test_system.py.

usage: _child.py DATA_DIR MODE
  awake    - take and release the real sleep lock and idle inhibitor, print JSON
  sit      - start a meditation (bells muted), checkpoint it at 10 minutes,
             write DATA_DIR/ready, then wait
  recover  - launch on DATA_DIR (recovering anything left behind), then quit
  dialog   - start a meditation, open the real End dialog, write DATA_DIR/ready
"""
import json
import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bell_timer as bt  # noqa: E402
import time  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

data, mode = sys.argv[1], sys.argv[2]
bt.DATA_DIR = data
bt.LOG_PATH = os.path.join(data, "sessions.jsonl")
bt.CURRENT_PATH = os.path.join(data, "current.json")
bt.CONFIG_PATH = os.path.join(data, "config.json")
bt.Gst.init(None)
app = bt.BellTimer()
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
signal.signal(signal.SIGHUP, signal.SIG_IGN)      # as main() does
for sig in (signal.SIGTERM, signal.SIGINT):
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, app.on_signal)


def inhibitors():
    out = subprocess.run(["systemd-inhibit", "--list", "--no-legend"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "Meditation Bell" in line]


def mine(lines):
    return any(str(os.getpid()) in line.split() for line in lines)


def wait_until(cond, timeout=10):
    """Run the main loop until cond() (the D-Bus calls answer asynchronously)."""
    end = time.time() + timeout
    ctx = GLib.MainContext.default()
    while not cond() and time.time() < end:
        ctx.iteration(False)
        time.sleep(0.01)
    return cond()


def awake():
    r = {}
    app.running, app.paused, app.overtime, app.target, app.interval = True, False, False, None, 60
    app.inhibit_on()
    wait_until(lambda: app.sleep_fd is not None)
    r["sleep_fd"] = app.sleep_fd
    r["listed_while_on"] = mine(inhibitors())
    app.block_idle(False)
    app.block_idle(True)
    r["screensaver_cookie"] = (app.ss_cookie if wait_until(lambda: app.ss_cookie is not None, 5)
                               else None)
    r["gtk_cookie"] = app.inhibit_cookie
    app.running = False
    app.inhibit_off()
    r["cookie_after"] = app.ss_cookie
    r["listed_after"] = mine(inhibitors())
    print(json.dumps(r), flush=True)
    app.quit()
    return False


def sit():
    for name in ("strike", "strike_start", "strike_end"):
        setattr(app.bell, name, lambda *a: None)
    app.sw_inf.set_active(True)
    app.sp_warm.set_value(0)
    app.on_start_pause(None)
    app.accum = 600                         # pretend ten minutes have passed
    app.checkpoint()
    wait_until(lambda: app.sleep_fd is not None)
    with open(os.path.join(data, "ready"), "w") as f:
        json.dump({"pid": os.getpid(), "sleep_lock": app.sleep_fd is not None,
                   "listed": mine(inhibitors())}, f)
    return False


def recover():
    app.quit()
    return False


def dialog():
    sit()
    os.remove(os.path.join(data, "ready"))

    def opened():
        if app._dialogs:
            open(os.path.join(data, "ready"), "w").write("{}")
            return False
        return True
    GLib.timeout_add(100, opened)
    app.on_stop_clicked(None)       # blocks in the dialog's own main loop
    return False


GLib.timeout_add(300, {"awake": awake, "sit": sit, "recover": recover,
                       "dialog": dialog}[mode])
sys.exit(app.run([]))
