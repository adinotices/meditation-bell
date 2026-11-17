"""Integration tests for the keep-awake decisions and the bell player, without
touching real power settings or playing sound."""
import os

import pytest
from gi.repository import GLib

import bell_timer as bt
from conftest import pump


@pytest.fixture
def power(tmp_path, monkeypatch):
    """A fake battery and COSMIC idle config."""
    idle = tmp_path / "idle"
    idle.mkdir()
    bat = tmp_path / "BAT0_status"
    monkeypatch.setattr(bt, "COSMIC_IDLE_DIR", str(idle))
    monkeypatch.setattr(bt.glob, "glob", lambda pattern: [str(bat)] if "BAT" in pattern
                        else [])

    def set_(battery, ac_suspend="None", bat_suspend="Some(1200000)"):
        bat.write_text("Discharging\n" if battery else "Charging\n")
        (idle / "suspend_on_ac_time").write_text(ac_suspend)
        (idle / "suspend_on_battery_time").write_text(bat_suspend)
    return set_


def test_on_mains_with_no_idle_suspend_screen_may_sleep(app, power):
    power(battery=False)
    app.audio_on_display = False
    assert bt.BellTimer.idle_would_stop_bells(app) is False


def test_on_mains_with_audio_on_monitor_idle_is_blocked(app, power):
    power(battery=False)
    app.audio_on_display = True
    assert bt.BellTimer.idle_would_stop_bells(app) is True


def test_on_battery_idle_is_blocked(app, power):
    power(battery=True)
    assert bt.BellTimer.idle_would_stop_bells(app) is True


def test_unreadable_idle_config_plays_safe(app, power, monkeypatch, tmp_path):
    power(battery=False)
    monkeypatch.setattr(bt, "COSMIC_IDLE_DIR", str(tmp_path / "missing"))
    assert bt.BellTimer.idle_would_stop_bells(app) is True


def test_block_idle_takes_and_releases_screensaver_inhibit(app, monkeypatch):
    calls = []

    def fake(method, args, reply=None, done=None):
        calls.append(method)
        if done:
            done(GLib.Variant("(u)", (77,)) if method == "Inhibit" else None)
    monkeypatch.setattr(app, "_screensaver", fake)
    block = bt.BellTimer.block_idle    # the real one (the fixture fakes it)
    block(app, True)
    block(app, True)                    # no double inhibit
    assert app.ss_cookie == 77 and calls == ["Inhibit"]
    block(app, False)
    assert app.ss_cookie is None and calls == ["Inhibit", "UnInhibit"]


def test_refresh_awake_follows_power_changes(app, power, monkeypatch, clock):
    decisions = []
    monkeypatch.setattr(app, "check_audio_route", lambda: None)
    monkeypatch.setattr(app, "block_idle", decisions.append)
    monkeypatch.setattr(app, "bells_due", lambda: True)
    monkeypatch.setattr(app, "idle_would_stop_bells",
                        lambda: bt.BellTimer.idle_would_stop_bells(app))
    app.audio_on_display = False
    power(battery=False)
    bt.BellTimer.refresh_awake(app)
    power(battery=True)                 # unplugged mid-meditation
    bt.BellTimer.refresh_awake(app)
    assert decisions == [False, True]
    assert app._next_awake_check == clock.t + bt.AWAKE_CHECK_EVERY


# ---------------------------------------------------------------- bell player

@pytest.fixture
def recorded_bell(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    played = []
    monkeypatch.setattr(bell, "_play_once", lambda uri, retry=True: played.append(uri) or uri)
    monkeypatch.setattr(bt, "STRIKE_GAP_MS", 5)
    return bell, played


def _spin_loop(seconds=0.3):
    import time
    end = time.time() + seconds
    while time.time() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)


def test_strike_three_times(recorded_bell):
    bell, played = recorded_bell
    bell.strike(3)
    _spin_loop()
    assert played == [bell.uri] * 3


def test_each_event_uses_its_own_bell(recorded_bell):
    bell, played = recorded_bell
    bell.strike_start()
    bell.strike(1)
    bell.strike_end()
    _spin_loop()
    assert sorted(played) == sorted([bell.start_uri, bell.uri, bell.end_uri])
    assert len(set(played)) == 3


def test_closing_bell_falls_back_to_three_strikes(recorded_bell, tmp_path):
    bell, played = recorded_bell
    bell.end_uri = None
    bell.strike_end()
    _spin_loop()
    assert played == [bell.uri] * bt.END_STRIKES


def test_failed_bell_retries_once_then_reports(tmp_path):
    """e.g. the audio device vanished: one fresh retry, then a visible error."""
    errors = []
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH, on_error=errors.append)
    bell.volume = 0.0
    bell._play_once(GLib.filename_to_uri(str(tmp_path / "gone.wav")))
    _spin_loop(3)
    assert len(errors) == 1 and errors[0].startswith("A bell couldn’t play")
    assert bell.players == []


def test_preview_cuts_off_previous_preview(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    bell.volume = 0.0
    bell.play_preview("end")
    first = bell.preview
    bell.play_preview("start")
    assert first not in bell.players and bell.preview is not first
    bell._cleanup(bell.preview)
    pump()
