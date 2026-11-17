"""Tests for the fixes from the fourth review."""
import os
import subprocess
import time

import pytest
from gi.repository import GLib, Gtk

import bell_timer as bt
from conftest import begin, journal, pump, run_for

R = Gtk.ResponseType


def _crashed(make_app, clock, seconds, **settings):
    app = make_app()
    begin(app, clock, **settings)
    run_for(app, clock, seconds - 2)
    app.checkpoint()
    app.running = False             # the process dies here
    return app


def test_time_spent_answering_carry_on_counts(make_app, clock, dialogs):
    """Probe: answering after 120 s gave 362 s instead of 482."""
    _crashed(make_app, clock, 240, hours=1)
    clock.advance(120)

    def slow_answer(_dlg):
        clock.advance(120)
        return R.OK
    dialogs.answer(slow_answer)
    again = make_app()
    pump()
    assert again.running and again.elapsed() == pytest.approx(480)


def test_carry_on_offers_to_save_the_length_it_reached(make_app, clock, dialogs):
    _crashed(make_app, clock, 600, hours=1)
    clock.advance(60)
    dialogs.answer(R.REJECT)
    make_app()
    pump()
    assert "_Save 10m to journal" in dialogs.last["buttons"]
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 600


def test_no_repeat_bell_after_carrying_on(make_app, clock, dialogs):
    """Probe: a crash within 10 s after a bell rang it again on carrying on."""
    app = make_app()
    begin(app, clock, hours=1, interval_min=10)
    run_for(app, clock, 600)                    # the 10-minute bell rings
    assert app.events.count("interval") == 1
    run_for(app, clock, 3)                      # crash 3 s later, before a timed checkpoint
    app.running = False
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    run_for(again, clock, 5)
    assert again.events.count("interval") == 0 and again.bells_rung == 1


def test_carry_on_after_resuming_past_the_extra_time_stop(make_app, clock, dialogs):
    """Probe: carrying on re-applied the 4-hour stop and rolled time back."""
    app = make_app()
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + bt.EXTRA_MAX + 10, step=5)
    app.on_start_pause(None)                    # Resume past the stop
    run_for(app, clock, 1800, step=5)
    app.checkpoint()
    el = app.elapsed()
    app.running = False
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    run_for(again, clock, 5)
    assert not again.paused and again.elapsed() == pytest.approx(el + 5, abs=1)


def test_already_saved_checkpoint_isnt_offered(make_app, clock, dialogs):
    app = _crashed(make_app, clock, 600, hours=1)
    rec = bt.load_json(bt.CURRENT_PATH, None)
    bt.append_session({k: v for k, v in rec.items() if not k.startswith("_")})
    make_app()
    pump()
    assert dialogs.seen == [] and len(journal(bt.DATA_DIR)) == 1
    assert not os.path.exists(bt.CURRENT_PATH)


def test_carrying_on_paused_doesnt_keep_the_laptop_awake(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, hours=1, interval_min=10)
    run_for(app, clock, 100)
    app.on_start_pause(None)                    # paused
    app.running = False
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert again.paused and again.sleep_fd is None and not again.idle_blocked


@pytest.mark.parametrize("start", ["9999-12-31T23:00:00-05:00", "0001-01-01T00:30:00",
                                   "1969-12-31T23:00:00+00:00"])
def test_implausible_years_are_kept_but_ignored(app, start):
    with open(bt.LOG_PATH, "w") as f:
        f.write(f'{{"start": "{start}", "seconds": 600}}\n')
        f.write('{"start": "2026-11-14T07:00:00-05:00", "seconds": 300}\n')
    sessions, junk = bt.load_sessions()
    assert [s["seconds"] for s in sessions] == [300] and len(junk) == 1
    app.refresh_stats()
    assert app.stat_labels["total"][0].get_text() == "5m"


def test_add_by_hand_refuses_one_that_ends_in_the_future(app, dialogs, wall):
    from test_saving import _fill_manual
    dialogs.answer(_fill_manual("2026-11-15", "11:30", 1, 0), R.CANCEL)   # ends 12:30
    app.on_add_manual(None)
    assert journal(bt.DATA_DIR) == []


def test_stat_tiles_are_not_read_twice(app):
    bt.append_session({"start": bt.dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "seconds": 3600})
    app.refresh_stats()
    lbl, title = app.stat_labels["today"]
    assert lbl.get_accessible().get_name() == "1 hour"


def test_sleep_note_never_exceeds_the_time_counted(app, clock, dialogs):
    begin(app, clock, minutes=1)
    run_for(app, clock, 58 + 30)
    clock.sleep(8 * 3600)                       # asleep long past the 4 h stop
    app._tick()
    dialogs.answer(R.CANCEL)
    app.on_stop_clicked(None)
    body = dialogs.last["body"]
    assert "slept for 8h" not in body


@pytest.mark.system
def test_refusal_sound_plays_to_the_end():
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    bell.beep(sink="fakesink sync=true")
    assert len(bell.beeps) == 1
    ctx = GLib.MainContext.default()
    end = time.time() + 5
    while bell.beeps and time.time() < end:
        ctx.iteration(False)
        time.sleep(0.01)
    assert bell.beeps == []                    # played (about 0.25 s) and cleaned up


@pytest.mark.system
def test_installer_refuses_a_folder_with_a_line_break(tmp_path):
    from conftest import ROOT
    import shutil
    repo = tmp_path / "a\nb"
    repo.mkdir()
    shutil.copy(os.path.join(ROOT, "install.sh"), repo)
    home = tmp_path / "home"
    home.mkdir()
    out = subprocess.run(["bash", str(repo / "install.sh")], env={**os.environ, "HOME": str(home)},
                         capture_output=True, text=True)
    assert out.returncode == 1 and "line break" in out.stderr
    assert not (home / ".local/bin/bell-timer").exists()


@pytest.mark.system
def test_installer_leaves_someone_elses_old_desktop_file(tmp_path):
    from conftest import ROOT
    home = tmp_path / "home"
    apps = home / ".local/share/applications"
    apps.mkdir(parents=True)
    (apps / "bell-timer.desktop").write_text("[Desktop Entry]\nName=Another bell app\n")
    subprocess.run(["bash", os.path.join(ROOT, "install.sh")],
                   env={**os.environ, "HOME": str(home)}, check=True, capture_output=True)
    assert (apps / "bell-timer.desktop").exists()
