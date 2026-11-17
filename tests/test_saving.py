"""Integration tests: ending a meditation, what goes into the journal, crash
recovery, closing the window, logout, and editing the journal."""
import glob
import json
import os

import pytest
from gi.repository import Gtk

import bell_timer as bt
from conftest import begin, journal, run_for

R = Gtk.ResponseType


# ---------------------------------------------------------------- ending early

def test_end_early_and_save(app, clock, dialogs):
    begin(app, clock, minutes=30, interval_min=10)
    run_for(app, clock, 600)
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "End _without saving", "End and _save"]
    assert dialogs.last["body"].startswith("You have meditated for 0:10:02")
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 602 and rec["planned"] == 1800 and rec["completed"] is False
    assert rec["interval"] == 600 and rec["bells"] == 1
    assert "extra_seconds" not in rec
    assert app.status.get_text() == "Meditation ended · 10m 2s added to your journal"
    assert not app.running and not os.path.exists(bt.CURRENT_PATH)


def test_end_early_without_saving(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 600)
    dialogs.answer(R.REJECT)
    app.on_stop_clicked(None)
    assert journal(bt.DATA_DIR) == [] and not app.running
    assert app.status.get_text() == "Meditation ended · not saved"
    assert not os.path.exists(bt.CURRENT_PATH)


def test_keep_meditating_changes_nothing(app, clock, dialogs):
    begin(app, clock, minutes=30)
    dialogs.answer(R.CANCEL)
    app.on_stop_clicked(None)
    assert app.running and journal(bt.DATA_DIR) == []


def test_escape_or_closing_the_dialog_keeps_meditating(app, clock, dialogs):
    begin(app, clock, minutes=30)
    dialogs.answer(R.DELETE_EVENT)
    app.on_stop_clicked(None)
    assert app.running


def test_under_a_minute_is_never_saved(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 20)
    dialogs.answer(R.REJECT)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "_End"]
    assert journal(bt.DATA_DIR) == [] and app.status.get_text() == ""


# ---------------------------------------------------------------- extra time

def _into_extra_time(app, clock, minutes=5, extra=150):
    begin(app, clock, minutes=minutes, interval_min=1)
    run_for(app, clock, minutes * 60 - 2 + extra)
    assert app.overtime


def test_extra_time_dialog_offers_planned_or_total(app, clock, dialogs):
    _into_extra_time(app, clock)
    dialogs.answer(R.CANCEL)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "Save _5m", "Save 7m 30s with _extra"]
    assert "Your 5m meditation is complete, and you continued for 2m 30s more" in dialogs.last["body"]
    assert app.running and app.overtime


def test_extra_time_option_1_saves_planned_length(app, clock, dialogs):
    _into_extra_time(app, clock)
    dialogs.answer(R.REJECT)
    app.on_stop_clicked(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 300 and rec["completed"] is True and "extra_seconds" not in rec
    assert app.status.get_text() == "Meditation complete · 5m added to your journal"


def test_extra_time_option_2_saves_planned_plus_extra(app, clock, dialogs):
    _into_extra_time(app, clock)
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 450 and rec["extra_seconds"] == 150 and rec["completed"] is True
    assert rec["planned"] == 300 and rec["bells"] == 4
    assert app.status.get_text() == ("Meditation complete · 7m 30s added to your journal "
                                     "(including 2m 30s extra)")


def test_extra_time_pause_is_not_counted(app, clock, dialogs):
    _into_extra_time(app, clock, extra=60)
    app.on_start_pause(None)
    run_for(app, clock, 900)
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 360


def test_closing_window_in_extra_time_asks_and_quits(app, clock, dialogs):
    _into_extra_time(app, clock)
    dialogs.answer(R.REJECT)
    assert app.on_close() is False
    assert dialogs.last["buttons"][1:] == ["Save _5m and quit", "Save 7m 30s with _extra and quit"]
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 300


def test_checkpoint_marks_completion_in_extra_time(app, clock):
    _into_extra_time(app, clock)
    assert json.load(open(bt.CURRENT_PATH))["completed"] is True


# ---------------------------------------------------------------- closing and logout

def test_close_when_idle_saves_settings(app, clock):
    app.sp_mins.set_value(42)
    app.sp_warm.set_value(20)
    assert app.on_close() is False
    cfg = json.load(open(bt.CONFIG_PATH))
    assert cfg["minutes"] == 42 and cfg["warmup_sec"] == 20


def test_close_mid_meditation_keep_stays_open(app, clock, dialogs):
    begin(app, clock, minutes=30)
    dialogs.answer(R.CANCEL)
    assert app.on_close() is True and app.running


def test_close_mid_meditation_save(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 120)
    dialogs.answer(R.OK)
    assert app.on_close() is False
    assert dialogs.last["buttons"][-1] == "End and _save and quit"
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 122


def test_logout_saves_everything_before_the_planned_end(app, clock):
    begin(app, clock, minutes=30)
    run_for(app, clock, 598)
    app.on_signal()
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 600 and rec["completed"] is False and not app.running


def test_logout_in_extra_time_saves_planned_and_notes_extra(app, clock):
    """Nobody to ask: save the planned length, keep the extra on record only."""
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 100)
    app.on_signal()
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 300 and rec["unsaved_extra_seconds"] == 100
    assert rec["completed"] is True and "extra_seconds" not in rec
    assert not app.running


def test_logout_while_end_dialog_is_open_saves_once(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)

    def logout_meanwhile(_dlg):
        app.on_signal()
        return Gtk.ResponseType.OK
    dialogs.answer(logout_meanwhile)
    app.on_stop_clicked(None)
    assert len(journal(bt.DATA_DIR)) == 1


def test_finish_twice_is_harmless(app, clock):
    begin(app, clock, minutes=5)
    run_for(app, clock, 100)
    app.finish("save")
    app.finish("save")
    assert len(journal(bt.DATA_DIR)) == 1


# ---------------------------------------------------------------- crash safety

def test_checkpoint_written_every_30_seconds(app, clock):
    begin(app, clock, infinite=True)
    run_for(app, clock, 65)
    cp = json.load(open(bt.CURRENT_PATH))
    assert 60 <= cp["seconds"] <= 67 and cp["completed"] is False


def test_pause_writes_checkpoint(app, clock):
    begin(app, clock, infinite=True)
    run_for(app, clock, 20)
    app.on_start_pause(None)
    assert json.load(open(bt.CURRENT_PATH))["seconds"] == 22


def test_recovers_unfinished_meditation_on_launch(make_app):
    bt.atomic_write_json(bt.CURRENT_PATH, {"start": "2026-11-15T10:00:00-05:00",
                                           "seconds": 1234, "planned": 3600})
    app = make_app()
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 1234 and rec["recovered"] is True
    assert "Recovered an unfinished meditation (21m)" in app.status.get_text()
    assert not os.path.exists(bt.CURRENT_PATH)


def test_recovery_does_not_duplicate(make_app):
    rec = {"start": "2026-11-15T10:00:00-05:00", "seconds": 1234, "planned": 3600}
    bt.append_session(rec)
    bt.atomic_write_json(bt.CURRENT_PATH, rec)
    make_app()
    assert len(journal(bt.DATA_DIR)) == 1 and not os.path.exists(bt.CURRENT_PATH)


def test_recovery_keeps_a_broken_checkpoint_as_bad(make_app):
    open(bt.CURRENT_PATH, "w").write("{not json")
    app = make_app()
    assert journal(bt.DATA_DIR) == [] and not os.path.exists(bt.CURRENT_PATH)
    assert open(bt.CURRENT_PATH + ".bad").read() == "{not json"
    assert app.status.get_text().startswith("Note: 1 unreadable saved meditation file")


def test_recovery_drops_a_short_checkpoint(make_app):
    bt.atomic_write_json(bt.CURRENT_PATH, {"start": "2026-11-15T10:00:00-05:00", "seconds": 20})
    make_app()
    assert journal(bt.DATA_DIR) == [] and not os.path.exists(bt.CURRENT_PATH)


def test_recovery_never_deletes_a_file_it_cannot_read(make_app):
    """Second review, probe: an unreadable parked file was deleted."""
    path = os.path.join(bt.DATA_DIR, "unsaved-20261115100000.json")
    bt.atomic_write_json(path, {"start": "2026-11-15T10:00:00-05:00", "seconds": 5400})
    os.chmod(path, 0)
    make_app()
    assert os.path.exists(path) and journal(bt.DATA_DIR) == []
    os.chmod(path, 0o600)
    make_app()                      # readable again: added, then removed
    assert [r["seconds"] for r in journal(bt.DATA_DIR)] == [5400]
    assert not os.path.exists(path)


def test_save_failure_is_parked_and_survives_another_meditation(app, clock, dialogs):
    """Reviewer's probe M: a failed save used to be overwritten by the next Begin."""
    begin(app, clock, minutes=30)
    run_for(app, clock, 1200)
    os.makedirs(bt.LOG_PATH)            # appending now fails
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert app.status.get_text().startswith("Error: Couldn’t save this meditation")
    parked = glob.glob(os.path.join(bt.DATA_DIR, "unsaved-*.json"))
    assert len(parked) == 1 and json.load(open(parked[0]))["seconds"] == 1202
    # still failing: a new meditation must not replace it
    begin(app, clock, minutes=30)
    run_for(app, clock, 120)
    assert json.load(open(parked[0]))["seconds"] == 1202
    # disk fixed: the next Begin adds it first
    dialogs.answer(R.REJECT)
    app.on_stop_clicked(None)
    os.rmdir(bt.LOG_PATH)
    begin(app, clock, minutes=30)
    assert [r["seconds"] for r in journal(bt.DATA_DIR)] == [1202]
    assert glob.glob(os.path.join(bt.DATA_DIR, "unsaved-*.json")) == []
    assert "Recovered an unfinished meditation (20m)" in app.status.get_text()


def test_failed_recovery_moves_checkpoint_aside(make_app):
    bt.atomic_write_json(bt.CURRENT_PATH, {"start": "2026-11-15T10:00:00-05:00",
                                           "seconds": 1234, "planned": 3600})
    os.makedirs(bt.LOG_PATH)
    make_app()
    assert not os.path.exists(bt.CURRENT_PATH)
    assert len(glob.glob(os.path.join(bt.DATA_DIR, "unsaved-*.json"))) == 1


def test_crash_in_extra_time_recovers_planned_length(make_app, clock):
    app = make_app()
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 9 * 60)
    app.checkpoint()
    app.running = False                 # simulate the process dying here
    make_app()
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 300 and rec["unsaved_extra_seconds"] == 540 and rec["recovered"]


def test_extra_time_dialog_saves_shown_amount_even_if_left_open(app, clock, dialogs):
    """Reviewer's probe B: the button said 7m 30s but 17m 30s was saved."""
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 150)

    def leave_open(_dlg):
        run_for(app, clock, 600)
        return R.OK
    dialogs.answer(leave_open)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"][-1] == "Save 7m 30s with _extra"
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 450


# ---------------------------------------------------------------- journal page

def _fill_manual(date, time_, hours, minutes):
    """Fills the Add dialog through its labelled fields, as a keyboard user would."""
    def answer(dlg):
        fields = {}

        def walk(w):
            if isinstance(w, Gtk.Label) and w.get_mnemonic_widget():
                fields[w.get_text()] = w.get_mnemonic_widget()
            if isinstance(w, Gtk.Container):
                for c in w.get_children():
                    walk(c)
        walk(dlg.get_content_area())
        fields["Date (YYYY-MM-DD)"].set_text(date)
        fields["Start time (HH:MM)"].set_text(time_)
        hours_spin = fields["Length"]
        hours_spin.set_value(hours)
        minutes_spin = [w for w in hours_spin.get_parent().get_children()
                        if isinstance(w, Gtk.Box)][0].get_children()[0]
        minutes_spin.set_value(minutes)
        return R.OK
    return answer


def test_add_meditation_by_hand(app, dialogs):
    dialogs.answer(_fill_manual("2026-11-14", "07:15", 1, 30))
    app.on_add_manual(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 5400 and rec["manual"] is True
    assert rec["start"].startswith("2026-11-14T07:15:00")
    assert [tuple(r)[1:3] for r in app.store] == [("1h 30m", "added")]


def test_add_by_hand_rejects_bad_date_then_accepts_fix(app, dialogs):
    dialogs.answer(_fill_manual("14/11/2026", "07:15", 0, 30),
                   _fill_manual("2026-11-14", "07:15", 0, 30))
    app.on_add_manual(None)
    assert len(dialogs.seen) == 2 and len(journal(bt.DATA_DIR)) == 1


def test_add_by_hand_rejects_zero_length(app, dialogs):
    dialogs.answer(_fill_manual("2026-11-14", "07:15", 0, 0), R.CANCEL)
    app.on_add_manual(None)
    assert journal(bt.DATA_DIR) == []


def test_delete_meditation_keeps_backup_and_junk(app, dialogs):
    bt.append_session({"start": "2026-11-14T07:00:00-05:00", "seconds": 600})
    with open(bt.LOG_PATH, "a") as f:
        f.write("a hand-written line\n")
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 900})
    app.refresh_stats()
    app.tree.get_selection().select_path(Gtk.TreePath(0))      # newest first
    dialogs.answer(R.OK)
    app.on_delete_selected(None)
    assert [s["seconds"] for s in bt.load_sessions()[0]] == [600]
    assert "a hand-written line" in open(bt.LOG_PATH).read()
    assert os.path.exists(bt.LOG_PATH + ".bak")


def test_delete_cancel_keeps_everything(app, dialogs):
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 900})
    app.refresh_stats()
    app.tree.get_selection().select_path(Gtk.TreePath(0))
    dialogs.answer(R.CANCEL)
    app.on_delete_selected(None)
    assert len(journal(bt.DATA_DIR)) == 1
