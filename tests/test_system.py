"""System tests: real audio decoding, the real sleep lock and idle inhibitor,
logout/crash behaviour of a real app process, and the installer.
Skip with `pytest -m "not system"`."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

import bell_timer as bt
from conftest import REAL_DBUS, ROOT

pytestmark = pytest.mark.system
Gst = bt.Gst
CHILD = os.path.join(ROOT, "tests", "_child.py")


def child_env():
    env = dict(os.environ)
    if REAL_DBUS:
        env["DBUS_SESSION_BUS_ADDRESS"] = REAL_DBUS
    return env


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------- audio

@pytest.mark.parametrize("path", [bt.START_BELL_PATH, bt.BELL_PATH, bt.END_BELL_PATH])
def test_bell_file_decodes_to_the_end(path):
    pipe = Gst.parse_launch(
        f'filesrc location="{path}" ! decodebin ! audioconvert ! audioresample ! fakesink sync=false')
    pipe.set_state(Gst.State.PLAYING)
    msg = pipe.get_bus().timed_pop_filtered(20 * Gst.SECOND,
                                            Gst.MessageType.EOS | Gst.MessageType.ERROR)
    ok, duration = pipe.query_duration(Gst.Format.TIME)
    pipe.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS
    assert duration > 2 * Gst.SECOND


def test_bell_player_reaches_end_through_playbin():
    """Plays the interval bell through the app's own player at zero volume."""
    errors = []
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH, on_error=errors.append)
    bell.volume = 0.0
    bell._play_once(bell.uri)
    ctx = bt.GLib.MainContext.default()
    assert bell.players
    end = time.time() + 30
    while bell.players and time.time() < end:
        ctx.iteration(False)
        time.sleep(0.02)
    assert not bell.players and not errors   # finished and cleaned up


# ---------------------------------------------------------------- staying awake

@pytest.mark.skipif(not shutil.which("systemd-inhibit"), reason="needs systemd-inhibit")
def test_real_sleep_lock_and_idle_inhibit(tmp_path):
    out = subprocess.run([sys.executable, CHILD, str(tmp_path), "awake"], env=child_env(),
                         capture_output=True, text=True, timeout=60)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["sleep_fd"] is not None and r["listed_while_on"] and not r["listed_after"]
    assert r["cookie_after"] is None
    if r["screensaver_cookie"] is None:
        pytest.skip("no org.freedesktop.ScreenSaver service on this desktop")


# ---------------------------------------------------------------- real process lifecycle

def _start_sitting(tmp_path):
    p = subprocess.Popen([sys.executable, CHILD, str(tmp_path), "sit"], env=child_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: os.path.exists(tmp_path / "ready"), 30), "child never started"
    time.sleep(0.3)
    info = json.load(open(tmp_path / "ready"))
    assert info["sleep_lock"] and info["listed"], "no sleep lock while meditating"
    return p, info["pid"]


def lock_listed(pid):
    out = subprocess.run(["systemd-inhibit", "--list", "--no-legend"],
                         capture_output=True, text=True).stdout
    return any(str(pid) in line.split() for line in out.splitlines()
               if "Meditation Bell" in line)


def test_logout_saves_meditation_and_releases_lock(tmp_path):
    p, pid = _start_sitting(tmp_path)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=15) == 0
    recs = [json.loads(l) for l in open(tmp_path / "sessions.jsonl")]
    assert len(recs) == 1 and recs[0]["seconds"] >= 600 and recs[0]["planned"] is None
    assert not os.path.exists(tmp_path / "current.json")
    assert wait_for(lambda: not lock_listed(pid), 5)


def test_hard_kill_releases_lock_and_is_recovered_on_next_launch(tmp_path):
    p, pid = _start_sitting(tmp_path)
    p.kill()
    p.wait(timeout=10)
    assert wait_for(lambda: not lock_listed(pid), 5), "sleep lock outlived the app"
    assert json.load(open(tmp_path / "current.json"))["seconds"] >= 600
    out = subprocess.run([sys.executable, CHILD, str(tmp_path), "recover"], env=child_env(),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    recs = [json.loads(l) for l in open(tmp_path / "sessions.jsonl")]
    assert len(recs) == 1 and recs[0]["seconds"] >= 600 and recs[0]["recovered"] is True
    assert not os.path.exists(tmp_path / "current.json")


# ---------------------------------------------------------------- installer

def test_installer_writes_launcher_and_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(["bash", os.path.join(ROOT, "install.sh")], env=env, check=True,
                   capture_output=True)
    cmd = home / ".local/bin/bell-timer"
    desktop = (home / ".local/share/applications/bell-timer.desktop").read_text()
    assert os.access(cmd, os.X_OK) and os.path.join(ROOT, "bell_timer.py") in cmd.read_text()
    assert f'Exec="{cmd}"' in desktop and f"Icon={ROOT}/logo.png" in desktop
    assert "StartupWMClass=bell-timer" in desktop


def test_source_compiles_cleanly():
    out = subprocess.run([sys.executable, "-W", "error", "-m", "py_compile",
                          os.path.join(ROOT, "bell_timer.py")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
