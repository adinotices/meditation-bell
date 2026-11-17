"""Shared test setup.

* GTK runs on a private Broadway display when `broadwayd` is installed, so no
  windows appear on your desktop (set BELL_TEST_DISPLAY=native to watch them).
* Every test gets its own temporary data folder; a guard fails the test if the
  app's paths ever point at the real journal.
* No session bus (see below), so tests can't reach the real desktop services.
* Time is faked: tests move a clock forward instead of waiting.
"""
import os
import shutil
import subprocess
import sys
import time
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# GTK 3 can register only one application per process on a session bus, and the
# tests build a fresh app each time, so they run without one. The real bus address
# is kept for the system tests, which start the app in a child process.
REAL_DBUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/bell-timer-tests"

def start_broadway(first=None):
    """Start broadwayd on a free display; returns (process, ":N"). It prints
    "Listening on ..." once ready (its socket is abstract: no file to wait for)."""
    import select
    first = first or 40 + os.getpid() % 40
    for n in range(first, first + 80):
        proc = subprocess.Popen(["broadwayd", f":{n}"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        ready, _, _ = select.select([proc.stdout], [], [], 5)
        line = proc.stdout.readline() if ready else ""
        if line.startswith("Listening"):
            return proc, f":{n}"
        proc.kill()      # display taken (or broken): try the next one
        proc.wait()
    sys.exit("tests: broadwayd would not start (set BELL_TEST_DISPLAY=native to use your display)")


def _start_broadway():
    import atexit
    proc, disp = start_broadway()
    atexit.register(proc.terminate)   # even if pytest is cut short
    os.environ["GDK_BACKEND"] = "broadway"
    os.environ["BROADWAY_DISPLAY"] = disp
    return proc


_broadway = None
if os.environ.get("BELL_TEST_DISPLAY") != "native":
    if shutil.which("broadwayd"):
        _broadway = _start_broadway()
    else:
        print("tests: broadwayd not found (Debian/Ubuntu: libgtk-3-bin), so test windows "
              "will open on your display", file=sys.stderr)

import pytest  # noqa: E402

import bell_timer as bt  # noqa: E402
from gi.repository import Gio, GLib, Gtk  # noqa: E402

bt.Gst.init(None)
REAL_DATA_DIR = os.path.expanduser("~/.local/share/bell-timer")


def pytest_unconfigure(config):
    if _broadway:
        _broadway.terminate()


class FakeClock:
    """Two clocks, like the real ones: `t` keeps counting through suspend
    (CLOCK_BOOTTIME), `awake_t` doesn't (CLOCK_MONOTONIC)."""

    def __init__(self):
        self.t = 10_000.0
        self.awake_t = 5_000.0

    def __call__(self):
        return self.t

    def awake(self):
        return self.awake_t

    def advance(self, seconds):
        """Time passes with the laptop awake."""
        self.t += seconds
        self.awake_t += seconds

    def sleep(self, seconds):
        """The laptop is suspended: nothing runs, only the boot clock moves."""
        self.t += seconds


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets a fresh data folder, and must never reach the real journal."""
    d = str(tmp_path / "data")
    os.makedirs(d)
    monkeypatch.setattr(bt, "DATA_DIR", d)
    monkeypatch.setattr(bt, "LOG_PATH", os.path.join(d, "sessions.jsonl"))
    monkeypatch.setattr(bt, "CURRENT_PATH", os.path.join(d, "current.json"))
    monkeypatch.setattr(bt, "CONFIG_PATH", os.path.join(d, "config.json"))
    real = os.path.realpath(REAL_DATA_DIR) + os.sep
    for name in ("DATA_DIR", "LOG_PATH", "CURRENT_PATH", "CONFIG_PATH"):
        assert not os.path.realpath(getattr(bt, name)).startswith(real)
    return d


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(bt, "now_clock", c)
    monkeypatch.setattr(bt, "now_awake", c.awake)
    return c


class _FixedDatetime(bt.dt.datetime):
    fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls.fixed if tz is None else cls.fixed.astimezone(tz)


class _FixedDate(bt.dt.date):
    @classmethod
    def today(cls):
        return _FixedDatetime.fixed.date()


@pytest.fixture
def wall(monkeypatch):
    """Pins the wall clock (dates in the journal, "today") to noon on a fixed day.
    Use wall.set(...) to move it."""
    import types
    fake = types.SimpleNamespace(**{k: getattr(bt.dt, k) for k in dir(bt.dt)
                                    if not k.startswith("__")})
    fake.datetime, fake.date = _FixedDatetime, _FixedDate

    class Wall:
        def set(self, when):
            _FixedDatetime.fixed = _FixedDatetime.fromisoformat(when.isoformat())
    w = Wall()
    w.set(bt.dt.datetime(2026, 11, 15, 12, 0))
    monkeypatch.setattr(bt, "dt", fake)
    return w


@pytest.fixture(autouse=True)
def fast_disk(monkeypatch, request):
    """Simulated hours checkpoint every 10 s; real fsyncs would make that slow.
    Tests marked `durable` keep them."""
    if "durable" not in request.keywords:
        monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture(autouse=True)
def fail_on_callback_errors(monkeypatch):
    """PyGObject prints and swallows exceptions raised in GTK callbacks (draw
    handlers, timers). Collect them and fail the test instead."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda typ, val, tb: errors.append(val))
    yield errors
    assert not errors, f"exception in a GTK callback: {errors[0]!r}"


class Dialogs:
    """Stands in for modal dialogs: records what was shown and answers from a queue."""

    def __init__(self):
        self.answers = []
        self.seen = []

    def answer(self, *responses):
        self.answers.extend(responses)

    def run(self, dlg):
        with warnings.catch_warnings():       # get_action_area is deprecated but fine here
            warnings.simplefilter("ignore", DeprecationWarning)
            area = dlg.get_action_area()
        info = {
            "title": dlg.get_property("text") if isinstance(dlg, Gtk.MessageDialog)
            else dlg.get_title(),
            "body": dlg.get_property("secondary-text") if isinstance(dlg, Gtk.MessageDialog)
            else None,
            "buttons": [b.get_label() for b in area.get_children()],
        }
        self.seen.append(info)
        assert self.answers, f"unexpected dialog: {info}"
        a = self.answers.pop(0)
        return a(dlg) if callable(a) else a

    @property
    def last(self):
        return self.seen[-1]


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    """Every test: a real modal dialog would block forever, so all of them are
    answered from this queue, and an unexpected one fails the test."""
    d = Dialogs()
    monkeypatch.setattr(Gtk.Dialog, "run", lambda self: d.run(self))
    return d


def pump():
    for _ in range(200):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


@pytest.fixture
def make_app(data_dir, clock, monkeypatch):
    """Builds a real app window on the test display. Bells, keep-awake and the
    sleep lock are recorded instead of performed (see test_system for the real ones)."""
    apps = []

    def make(stub_system=True):
        for old in apps:          # earlier apps in this test count as closed (or crashed)
            old.release_lock()
        app = bt.BellTimer()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        app.events = []
        if stub_system:
            # the decisions in refresh_awake run for real; only the system calls are faked
            def lock_on():
                app.events.append("inhibit_on")
                app.sleep_fd = -1

            def lock_off():
                if app.sleep_fd is not None:
                    app.events.append("lock_off")
                app.sleep_fd = None
            app.inhibit_on = lock_on
            app.inhibit_off_sleep = lock_off
            app.block_idle = lambda on: setattr(app, "idle_blocked", on)
            app.check_audio_route = lambda: None
            app.idle_would_stop_bells = lambda: True
            app.watch_shutdown = lambda: None
        app.register(None)
        app.activate()
        b = app.bell
        b.strike = lambda times=1: app.events.append("interval")
        b.strike_start = lambda: app.events.append("start")
        b.strike_end = lambda: app.events.append("end")
        b.play_preview = lambda which: app.events.append(f"preview:{which}")
        pump()
        apps.append(app)
        return app

    yield make
    for app in apps:
        if app.tick_id:
            GLib.source_remove(app.tick_id)
            app.tick_id = None
        if app.warm_id:
            GLib.source_remove(app.warm_id)
            app.warm_id = None
        app.running = app.warming = False
        app.release_lock()
        if app.win:
            app.win.destroy()
    pump()


@pytest.fixture
def app(make_app):
    return make_app()


# ---------------------------------------------------------------- helpers for tests

def configure(app, minutes=0, hours=0, infinite=False, interval_min=0, warmup=0,
              start_bell=True):
    app.sw_inf.set_active(infinite)
    app.sp_hours.set_value(hours)
    app.sp_mins.set_value(minutes)
    app.sp_int.set_value(interval_min)
    app.sp_warm.set_value(warmup)
    app.sw_start.set_active(start_bell)
    # a value outside a field's range is silently clamped: catch typos in tests
    for spin, want in ((app.sp_hours, hours), (app.sp_mins, minutes),
                       (app.sp_int, interval_min), (app.sp_warm, warmup)):
        assert spin.get_value() == want, f"setting {want} was clamped to {spin.get_value()}"


def begin(app, clock, **settings):
    configure(app, **settings)
    app.on_start_pause(None)
    clock.advance(2)          # step past the double-click guard
    if app.running:
        app._tick()


def run_for(app, clock, seconds, step=1.0):
    """Advance the fake clock (awake), ticking the timer as the 250 ms GLib timer would."""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.advance(min(step, end - clock.t))
        if app.warming:
            app.warm_tick()
        elif app.running:
            app._tick()


def journal(data_dir):
    import json
    path = os.path.join(data_dir, "sessions.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path) if line.strip()]
