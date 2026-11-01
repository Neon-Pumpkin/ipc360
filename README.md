# ipc360 — list, download and watch the SD recordings of an IPC360 / Puwell camera, without its app

**☕ [Buy me a coffee](https://buymeacoffee.com/neonpumpkin)** — hobby project, one camera, several weeks
of reverse engineering. If this saved you an afternoon, that is worth a coffee. The `NeonPumpkin` mark
([`assets/neonpumpkin.jpg`](assets/neonpumpkin.jpg)) is the same one on the GitHub profile and on the tip
jar, so the code, the profile and the coffee all read as one person — and the green **Sponsor** button
beside this repository points at the same link (`.github/FUNDING.yml`).

<img src="assets/neonpumpkin.jpg" align="right" width="96" height="96"
     alt="the NeonPumpkin mark — the same on the GitHub profile and buymeacoffee"/>

Two tools, one protocol:

| file | what it is |
|---|---|
| `ipc360.py` | the CLI: read the SD **day map**, **pull** a minute range from the cloud manager, **play** it into a clean `.mp4` (H.264 + audio) |
| `camclips.py` | a LAN **gallery** for what you pulled: a month **calendar**, a zoomable **day map**, one video per page with ruler-still seek, autoplay, save-as, two-step delete, a **measured** pull countdown, and **selectable effects** (`/look`) |
| `PROTOCOL.md` | the reverse-engineered wire protocol: framing, command numbers, the day-map answer, the audio format, every trap that cost hours |

Why this exists: the camera's own app will not show you *what is on the card* for a
day you did not open, will not give you an mp4 you can keep, and cannot be scripted.
Everything here talks to the **cloud managers** the app uses — the camera itself is
never reconfigured, and nothing is written to the SD card.

---

## Is it your camera?

The code speaks the **IPC360 / Puwell "PW" native net SDK** protocol
(`libpwnativenetsdk.so`, the `$Rev: 4836` build stamped into every packet as `rev = 0x6912E4`).
It was verified on **one** camera — a Puwell-SDK camera enrolled in the `com.ipc360` app — but
nothing in it is model-specific: the login is your **account**, day maps and chunk pulls are
answered by the **managers** the app uses, and the manager/relay addresses are discovered
per session, never hardcoded.

So, realistically:

* **works** — other cameras of the same family (the same PW SDK, the same app / account),
  each with its own `--cam <camId>`
* **probably works** — same brand, different SKU, if its SDK build stamp matches
  (`--login-mgr` and `--cam` are the only per-camera things in the CLI)
* **will not work** — cameras that are not in that ecosystem (a plain ONVIF / RTSP camera,
  a camera with its own app family). There is no ONVIF/RTSP path here at all.

One `--list --day <yesterday>` answers it for any candidate camera in ~10 s.

## Requirements

**Python 3** (stdlib only — `socket`, `struct`, `hashlib`, `re`, `argparse`) plus
**`ffmpeg` / `ffprobe`**, and only for the mux and the stills — `--no-ffmpeg` writes the raw
`.h264` / `.raw` and the day map needs neither.

Getting them, if the machine has neither:

| | Python 3 | ffmpeg |
|---|---|---|
| Windows | python.org/downloads/windows (one `.exe`, no admin rights needed) | the static build at ffmpeg.org/download.html — one `.7z`, unpack it, call it by its full path, or put its folder on `PATH` |
| macOS | already there (`python3`) | `brew install ffmpeg` |
| Debian / Ubuntu / Raspberry OS | already there | `sudo apt install ffmpeg` |
| Arch | already there | `sudo pacman -S ffmpeg` |
| Fedora / RHEL | already there | `sudo dnf install ffmpeg` |

Nothing here downloads a binary for you, and that is deliberate: an ffmpeg build is ~90 MB,
platform-specific, and the wrong one leaves a `PATH` that looks broken — a table you can read in
30 s beats a script that can fail in 10 ways. `ipc360.py --setup` says which of the two it can
find, so a first run tells you what to install before it fails inside a pull.

## Getting started (the five-minute version)

```sh
cd <this folder>
python3 ipc360.py --setup          # asks: email, password, camId, LAN ip, where clips live
python3 ipc360.py --list --day <yesterday>     # what the SD card holds: one mgr query
python3 camclips.py --serve        # the gallery on http://localhost:8795/
```

`--setup` writes **`ipc360-config.json`** next to the code (mode `0600`, listed in `.gitignore`
so it can never land in a commit). It holds everything a first run needs:

```json
{ "email": "the email the camera is enrolled in", "pw": "that camera password",
  "cam": 1234567890, "cam_ip": "10.20.30.40", "share": "camclips-share",
  "login_mgr": "52.8.101.180:19000", "cam_port": 23456 }
```

Nothing is asked for again, and every setting can be overridden per run without touching the
file: the environment wins over the file, and the flags win over both —
`ipc360.py --cam <id> --day …`, `IPC360_PW=… camclips.py --serve --port 9000`.

### "But how do I run this?"

* **Clone and run the python** — `git clone https://github.com/<you>/ipc360.git` then
  `python3 ipc360.py …`. There is nothing to compile and nothing to install beyond **ffmpeg**
  (used for the mux and the stills) and Python 3 — the rest is stdlib (`socket`, `struct`,
  `hashlib`, `http.server`). Downloading the four files in a zip and unzipping works just as
  well; the repo has no dependencies and no package to build.
* **It hosts its own webserver.** `camclips.py --serve` is `http.server.ThreadingHTTPServer`
  on a port (default **8795**), so the gallery is reachable from any device on the same LAN —
  that is how a phone or a tablet opens it. There is **no HTTPS and no login**: anyone on that
  network can watch, download, and start a pull. If that matters, run it on an internal
  network, or port-forward it to one machine (`ssh -L 8795:localhost:8795 otherbox`).
  `--port` is yours to change; the folder it serves is the settings file's `share`.
* **It is not a daemon by default** — `--serve` runs in the foreground and Ctrl-C stops it.
  `nohup`, `screen`, or the systemd unit below keep it up on Linux.
* **Windows / macOS work** as far as the code is concerned: no Linux-only system call is used,
  paths come from the settings file, and the mode-`0600` chmod is skipped where unix modes
  don't exist. What Linux gives you and Windows does not is (a) the systemd unit, (b) `tcpdump`
  to read your `camId` off a login (on Windows use a browser dev-tool or a small `netcat`
  capture instead), and (c) the SMB share path in the help page, which is just a Linux mount
  convention. On Windows, open the `share` folder directly — clips are ordinary `.mp4`s.
* **The prompts.** Missing creds are asked for on the terminal (password not echoed when the
  terminal supports it); a missing `camId` is asked for by `--setup` and refused with a clear
  message by every other command. Nothing is ever looked up for you: this tool will not "find
  your camera" — the manager only answers for a camera that is already in the app.

## Credentials — never in the code

```sh
export IPC360_EMAIL='the email the camera is enrolled in'
export IPC360_PW='that camera password'
export IPC360_CAM_ID=0            # your camera's camId (u32) - see below
# or keep a two-line file OUTSIDE the repo:
export IPC360_CREDS_FILE=~/private/camera-creds.txt
```

## Finding your `camId`

`camId` is a 32-bit number the manager puts in every envelope. You get it the honest way:
run the app once while `tcpdump -i any -nn -s0 -X 'tcp and port 19000'` (or `strace -f -e signal`)
records a login — the first dword of the payload is your `camId`, the account id is the second.
The number is also visible in the app's own device list. It does **not** change when you
move the camera to another LAN.

Or let the tool test candidates instead of guessing:

```sh
python3 ipc360.py --find-cam 1003637840 1234567890     # test these numbers, one mgr query each
python3 ipc360.py --find-cam --scan app-log.txt        # harvest candidates from a capture
```

Each candidate gets **one day-map query (~1.5 s)** on a fresh manager session: an enrolled id
answers with minutes, anything else answers with nothing (no error, no side effect — nothing is
pulled, nothing is written, the card is not touched). `--scan` reads the ~10-digit id out of a
tcpdump hex dump, the app's own log, or a strings dump of the app, most-likely first.

## CLI

```sh
IPC360_CAM_ID=1234567890 python3 ipc360.py --list --day 2026-10-30

python3 ipc360.py --play 13:00-13:01 --day 2026-10-30 --cam 1234567890 \
    --out probes/seg --seconds 120 --idle 15 --passes 5

# no network at all - merge two pulls of the SAME minute, offline:
python3 ipc360.py --from-wire probes/a/pull-*.wire,probes/b/pull-*.wire --out probes/segm/seg
```

`--play` writes `seg.mkv`, `seg.wav` (lossless audio alone), `seg.mp4` (`-c:v copy -movflags
+faststart -ar 8000 -c:a mp3 -b:a 64k`) and `seg.stats.json` (what the gallery prints).

**Passes are the whole point.** One pass re-streams the same range through a different
relay each time and the chunk slots are unioned; a single pass comes back with partial
groups. Measured on a 1-minute range:

| passes | partial groups | frames | decode errors |
|---|---|---|---|
| 1 | 11 | 1005 | 8 |
| 3 | 5 | 1178 | 1 |
| 5 | 2 | 1199 | 1 |
| 7 | **0** | **1199** | **0** |

≈6 min of wall-clock per minute of SD at `--passes 5` (≈9 min at 7). Cheapest glitch-free
recipe: pull the **same minute twice at 7 passes, strictly sequentially** (one manager
session at a time), then merge all 14 `.wire` files **offline** with `--from-wire` (~20 s,
no network). Judge a minute by its `decode errors`, not by its partial count — some chunks
never leave the camera at all.

## The gallery

```sh
python3 camclips.py --serve                      # default port 8795
CAMCLIPS_SHARE=~/public/camclips-share python3 camclips.py --serve --port 8795
```

* `/` — a **calendar for one month**: every real date is a cell, so a date can never be
  "missing"; the whole cell is the link. Each cell carries a 24-hour hairline bar drawn
  from that date's **cached** day map only — the calendar asks the camera nothing.
* `/day?day=YYYY-MM-DD` — that day's SD map as **two rows** (00:00–11:59, 12:00–23:59),
  mouse-wheel over a row to zoom that row, double-click = a 30-minute window, 1-minute
  minutes are widened to stay clickable, and clicking a green minute pulls it.
* A day that is **over** and whose cached read already covered the whole day opens with
  **zero camera work** — that is the point of the cache rule, and the header says which
  happened. `&recheck=1` forces one read for the rare case where the card recycled a day.
* `/clip?name=` — one video, starts playing right away, seek by clicking the ruler under
  it, dragging the slider, or (no JS) the `#t=` jump links and the 9 stills.
* `/pull` starts a pull and **stays on the day page**: the sticky top bar counts it down
  every 3 s from the pull's own `.wire` mtimes, and the page walks itself to the clip page
  when the clip lands.
* `/look` — **switch every visual effect on or off**: each effect has its own **slide switch**
  (the knob sits over the word it is on, the whole pill is one click), then the presets **no
  effects at all** / **calm** / **full**, and a `Now on` row. `/look` carries its own nav row —
  `home — this month` and `go back to that day` — so leaving it is as easy as entering it. Saved
  in `prefs.json`, applies to every page. Any page also takes `?fx=`: `?fx=off`, `?fx=+aurora`,
  `?fx=-scan`, `?fx=glass,glow`. Effects are CSS only — they never ask the camera for anything.
  `frosted glass panels`, `neon edges + glow`, `3D calendar`, `slow coloured haze`,
  `scanline texture`, `light sweeping the calendar`, plus the presets **no effects at all** /
  **calm** / **full**. Saved in `prefs.json`, applies to every page. Any page also takes
  `?fx=`: `?fx=off`, `?fx=+aurora`, `?fx=-scan`, `?fx=glass,glow`. Effects are CSS only —
  they never ask the camera for anything.
* `/delete?name=X` is **two steps**: it lists exactly which files go, then
  `&confirm=1` removes them. The minute stays on the card, so you can pull it again.
* `/sweep?m=YYYY-MM` asks the card for **every date of that month** in one walk (~8 s each).
* `/status`, `/status.json`, `/api/daymap?day=`, `/refresh?day=`, `/dl/NAME.mp4`.

One pull at a time: the manager session **and** the camera's P2P relay are shared state, so
two pulls tear each other down. `camclips.py` holds `grab.lock` while one runs and `ipc360.py`
refuses a CLI pull while that lock is alive (`IPC360_LOCK_OK=1` is what it passes its own child).

## Where clips live — a setting, not a hard-coded path

The `share` line in `ipc360-config.json` (set by `--setup`) is the folder everything sits in:
the clips (`.mp4`), the 9 stills per clip, the poster, the sidecar `.json` with the card facts,
the cached day maps (`daymap-<day>.json`), `prefs.json` (clock mode + the look), the pull logs
and `grab.lock`. `CAMCLIPS_SHARE=/srv/camera/clips python3 camclips.py --serve` overrides it for
one run; `IPC360_CONFIG=other-config.json` points at a different settings file (so one box can
host several cameras). Change it and the gallery simply grows in the new folder — old clips
stay where they were, and `--import` will publish an existing `.mp4` from anywhere.

| variable | default | meaning |
|---|---|---|
| `CAMCLIPS_SHARE` | `camclips-share` | where clips, day-map caches, prefs and the lock live |
| `IPC360_DIR` | `.` | where `ipc360.py` lives (the gallery runs it) |
| `IPC360_LOCK` / `IPC360_RUNNING` | `camclips-share/grab.lock` / `grab-running.json` | must match `CAMCLIPS_SHARE` + `/grab.lock` if you move the share |
| `IPC360_CAM_ID` | none | your camera's camId |
| `IPC360_CAM_IP` / `IPC360_CAM_PORT` | empty / `23456` | only the direct-LAN fallback |
| `IPC360_LOGIN_MGR` | `52.8.101.180:19000` | the manager that serves the account commands; override it if your app region differs |

```sh
python3 ipc360.py --login-mgr 52.8.101.180:19000 --list --day 2026-10-30 --cam 1234567890
```

## Example systemd unit (how the gallery runs on its own box)

```ini
# /etc/systemd/system/camclips.service
[Unit]
Service=camclips

[Service]
ExecStart=/usr/bin/python3 /srv/camera/camclips.py --serve --port 8795
Environment=CAMCLIPS_SHARE=/srv/camera/camclips-share IPC360_DIR=/srv/camera
# put the credentials in a 0600 drop-in, never in this file's history:
#   sudo systemctl edit camclips.service   ->  Environment=IPC360_EMAIL=... IPC360_PW=...
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Never restart the unit while a pull runs — stopping the unit kills the pull's cgroup and
leaves a live `grab.lock` that the status bar keeps counting for 20 min.

## What the protocol notes are worth reading

`PROTOCOL.md` documents the traps, and they are the reason this took weeks rather than
hours: the 20-byte frame header, the `rev` dword repurposed as a tag by the relay, the
manager payload envelope, **TCP for the managers vs DATAGRAM for the P2P relay** (a
short `recv()` silently truncates), keep-alive beats, `dayEpoch` vs the box clock, the
audio being **a-law (G.711) at 8 kHz** and not linear u8, `fid` being a continuous frame
index whose rate varies per minute, and why concatenating arrived packets drifts the
sound tens of seconds.

## Honest limits

* Verified on one camera, one month of cards. Other SKUs are likely, not proven.
* The protocol was reverse-engineered from the app's own SDK library; a newer app / SDK
  build may change the `rev` stamp or command numbers, so this can break with an update.
* Some chunks never leave the camera, so a minute can be permanently a few frames short —
  the gallery says `x partial / y err` rather than pretending the clip is perfect.
* **Unofficial.** Not affiliated with, endorsed by, or approved by IPC360, Puwell, or any
  camera vendor. Use it on a camera that is yours, in your own account.

## Reporting a problem

Open an issue and paste the exact command you ran — the answer depends on flags that are easy to
forget. Enough = the command, its full output, the `x partial / y err` line the gallery prints for
that clip (or the `seg.stats.json`), and whether the day map itself looked right. "it plays jagged
around 14:25" is fixable, "it doesn't work" is not.

## License

MIT — see `LICENSE`. Keep the disclaimer about it being unofficial.
