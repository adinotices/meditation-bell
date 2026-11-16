# Meditation Bell

A quiet desktop meditation timer for Linux, built for long sits: set any length
(or none), ring a bell at a regular interval as a posture-change reminder, and
keep a journal of how much you have meditated.

![Meditation Bell](screenshot.png)

## Features

- **Any length, or ∞.** Hours and minutes, or no end at all: interval bells keep ringing until you stop.
- **Interval bells** at any number of minutes, plus separate opening and closing bells.
- **Warm up.** A short countdown after Begin (15 seconds by default) so you can settle before the opening bell.
- **Journal.** Today, last 7 and 30 days, all time, day streak, a 14-day chart and a list of meditations. Ending early lets you choose whether to save.
- **Built for long sits.** The clock counts through pauses correctly, keeps the laptop from idle-suspending when that would stop the bells, tells you if the laptop slept mid-meditation, and recovers an unfinished meditation after a crash or power loss.
- **Calm to look at.** Light and dark themes (follows the system, with a toggle), a dimmed view after 30 seconds, and colour contrast that meets WCAG AA.

## Requirements

Python 3 with GTK 3 (PyGObject) and GStreamer. On Debian, Ubuntu or Pop!_OS:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good
```

Developed on Pop!_OS with the COSMIC desktop. It should run on any GTK 3 desktop;
theme-following and the keep-awake logic are written for COSMIC and fall back to
generic behaviour elsewhere.

## Run

```sh
python3 bell_timer.py
```

or `./install.sh` to add a launcher entry and a `bell-timer` command.

## Bells

| File | Used for | Source |
| --- | --- | --- |
| `start_bell.wav` | Opening bell | ["bell-meditation_cleaned.wav" by JetRye](https://freesound.org/s/140128/), CC0 |
| `end_bell.wav` | Closing bell | ["temple_bell_002.wav" by tec_studio](https://freesound.org/s/668647/), CC0 |
| `interval_bell.wav` | Interval bell | ["Bright Tibetan Bell Ding B Note - cleaner" by steaq](https://freesound.org/s/346328/), CC0 |

To use your own sounds, replace any of these files with one of the same name.

## Your data

Everything stays on your machine, in `~/.local/share/bell-timer/`:
`sessions.jsonl` (one line per meditation), `config.json` (last settings) and
`current.json` (a checkpoint while a meditation is running). The app makes no
network connections.

## Licence

Code: MIT (see `LICENSE`). Bell recordings: CC0, by their authors above.
