"""System tests: real audio decoding, the real sleep lock and idle inhibitor,
logout/crash behaviour of a real app process, and the installer.
Skip with `pytest -m "not system"`."""
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

import bell_timer as bt
from conftest import REAL_DBUS, ROOT, start_broadway

pytestmark = pytest.mark.system
Gst = bt.Gst
CHILD = os.path.join(ROOT, "tests", "_child.py")


@pytest.fixture(autouse=True)
def own_display(monkeypatch):
    """Each app process gets its own Broadway display: one display server
    shared by several apps drops connections ("Unable to write to server")."""
    if os.environ.get("GDK_BACKEND") != "broadway" or not shutil.which("broadwayd"):
        yield
        return
    proc, disp = start_broadway(first=120 + os.getpid() % 40)
    monkeypatch.setenv("BROADWAY_DISPLAY", disp)
    yield
    proc.terminate()
    proc.wait()


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


def test_logout_with_end_dialog_open_exits_promptly(tmp_path):
    """Second review: the End dialog's own main loop kept the app alive after
    a logout signal until someone clicked it."""
    p = subprocess.Popen([sys.executable, CHILD, str(tmp_path), "dialog"], env=child_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: os.path.exists(tmp_path / "ready"), 30), "dialog never opened"
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=5) == 0
    recs = [json.loads(l) for l in open(tmp_path / "sessions.jsonl")]
    assert len(recs) == 1 and recs[0]["seconds"] >= 600


def test_real_app_ignores_terminal_hangup_and_quits_on_term(tmp_path):
    """Runs bell_timer.py itself (its main()) with a private home folder."""
    env = {**os.environ, "HOME": str(tmp_path)}      # no session bus: stays separate
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bell_timer.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=open(tmp_path / "stderr", "w"))
    data = tmp_path / ".local/share/bell-timer"
    assert wait_for(lambda: os.path.exists(data / ".lock"), 30), "app never started"
    time.sleep(1)
    p.send_signal(signal.SIGHUP)                      # the terminal it came from closed
    time.sleep(1)
    assert p.poll() is None, (tmp_path / "stderr").read_text()[-2000:]
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    assert oct(os.stat(data).st_mode & 0o777) == "0o700"


def test_hard_kill_then_carry_on(tmp_path):
    """Third review: a desktop crash used to end the meditation for good. A
    checkpoint from moments ago in this boot can be carried on with."""
    p, pid = _start_sitting(tmp_path)
    p.kill()
    p.wait(timeout=10)
    out = subprocess.run([sys.executable, CHILD, str(tmp_path), "resume"], env=child_env(),
                         capture_output=True, text=True, timeout=60)
    state = json.loads(out.stdout.strip().splitlines()[-1])
    assert state["running"] and state["elapsed"] >= 600
    assert state["status"].startswith("Carrying on")


# ---------------------------------------------------------------- installer

@pytest.mark.parametrize("folder", ["home", 'odd "home" $dir `x` \\ here'])
def test_installer_writes_launcher_and_command(tmp_path, folder):
    home = tmp_path / folder
    home.mkdir()
    apps = home / ".local/share/applications"
    apps.mkdir(parents=True)
    (apps / "bell-timer.desktop").write_text("old")          # from an earlier version
    env = {**os.environ, "HOME": str(home)}
    subprocess.run(["bash", os.path.join(ROOT, "install.sh")], env=env, check=True,
                   capture_output=True)
    cmd = home / ".local/bin/bell-timer"
    assert os.access(cmd, os.X_OK)
    # the launcher really runs this app's file, whatever the folder is called
    dry = cmd.read_text().replace("exec /usr/bin/python3", "printf '%s\\n'")
    out = subprocess.run(["bash", "-c", dry], capture_output=True, text=True)
    assert out.stdout.splitlines() == [os.path.join(ROOT, "bell_timer.py")]
    desktop = (apps / "io.github.adinotices.MeditationBell.desktop").read_text()
    assert not (apps / "bell-timer.desktop").exists()
    assert f"Icon={ROOT}/logo.png" in desktop and "StartupWMClass=bell-timer" in desktop
    exec_line = [l for l in desktop.splitlines() if l.startswith("Exec=")][0]
    # undo desktop-entry quoting and check it names the command
    assert exec_line.startswith('Exec="') and exec_line.endswith('"')
    assert re.sub(r"\\(.)", r"\1", exec_line[6:-1]) == str(cmd)


def test_installer_wont_replace_someone_elses_command(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    other = home / ".local/bin/bell-timer"
    other.write_text("#!/bin/sh\necho a different program\n")
    out = subprocess.run(["bash", os.path.join(ROOT, "install.sh")],
                         env={**os.environ, "HOME": str(home)}, capture_output=True, text=True)
    assert out.returncode == 1 and "not replacing it" in out.stderr
    assert "a different program" in other.read_text()


def test_installer_from_a_folder_with_a_backslash(tmp_path):
    repo = tmp_path / "my\\repo"
    repo.mkdir()
    shutil.copy(os.path.join(ROOT, "install.sh"), repo)
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["bash", str(repo / "install.sh")], env={**os.environ, "HOME": str(home)},
                   check=True, capture_output=True)
    desktop = (home / ".local/share/applications/io.github.adinotices.MeditationBell.desktop")
    icon = [l for l in desktop.read_text().splitlines() if l.startswith("Icon=")][0]
    assert icon == "Icon=" + str(repo).replace("\\", "\\\\") + "/logo.png"


def test_source_compiles_cleanly():
    out = subprocess.run([sys.executable, "-W", "error", "-m", "py_compile",
                          os.path.join(ROOT, "bell_timer.py")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
