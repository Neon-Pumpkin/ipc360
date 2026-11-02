#!/usr/bin/env python3
r"""camclips.py — LAN gallery for the ipc360 camera clips.

Page lives on the share (`camclips-share/`), so it is reachable two ways:
  * browser  http://localhost:8795/            (play + seek + download + grab more)
  * <the share opened as a Windows folder>             (just copy the .mp4s off the box)

Serve:   camclips.py --serve            (default --port 8795)
Grab:    camclips.py --grab 2026-10-30 13:00-13:01 [--passes 5]
Import:  camclips.py --import probes/play-P5b/seg --name NAME --day D --range R
Thumbs:  camclips.py --thumbs NAME [--reference "from the IPC360 app"]  (stills + sidecar)
Daymap:  camclips.py --daymap 2026-10-30            (cache what the card holds)

What the page does
  /                  gallery. Per clip: inline <video> + a TIMELINE you can click anywhere
                     on (click seeks the video in place; the <input type=range> slider below
                     it scrubs too) + tick labels every 5 s + a row of `#t=` jump links for
                     browsers without JS + a thumbnail strip (every still is a link to that
                     moment) + card text = codec/anchor/audio holes/partial groups/decode
                     errors/relays/passes + its own download links.
                     Plus a per-day SD day-map strip: green cells = what the card holds,
                     clicking a cell grabs that minute; a day index of every clip with sizes.
  /api/daymap?day=   the day map as raw JSON (programmatic use; also re-reads the card)
  /refresh?day=      re-read the SD day map(s), then 303 back to / (the BROWSER refresh — no
                     raw JSON for the viewer; the page's "refresh" link points here)
  /sweep?m=YYYY-MM   ask the card for EVERY date of that month in one walk (it holds grab.lock so
                     nothing can pull during it; the LIVE bar counts it). Opening /day?day=X also
                     READS THE CARD for X — one query per page open — which is how a date that was
                     never checked gets its timeline. The calendar itself never queries anything:
                     its 24-hour hairline bars come from the daymap-<day>.json caches only.
  /grab?day=&range=  runs ipc360.py --play in the background (writes clips/<name>.mp4)
  /pull?day=&range=  same, but it STAYS on that day's page: the sticky top bar counts the pull
                 down in place and the page opens /clip?name=… itself when the clip lands
  /dl/<name>.mp4     the same bytes with Content-Disposition: attachment ("save as")
  /status            grab lock + which grab holds it + live log tail
  /*.mp4 / *.jpg / *.json / *.log    plain file serving WITH Range support (seek = no re-fetch)

One grab runs at a time — the mgr session AND the camera P2P relay are shared state, so two
pulls at once tear each other down.  This page holds `grab.lock` while a grab runs, and ipc360.py
refuses a CLI --pull/--play while that lock is alive (its own child gets IPC360_LOCK_OK=1).
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import html as _html
import json
import os
import re
import subprocess
import shutil
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from urllib.parse import urlparse, parse_qs
from pathlib import Path

CFG = Path(os.environ.get("IPC360_CONFIG", "ipc360-config.json"))   # what ipc360.py --setup wrote


def cfg():
    """The settings file as a dict: email, pw, cam, cam_ip, share … written by ipc360.py --setup."""
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {}


SHARE = Path(os.environ.get("CAMCLIPS_SHARE") or cfg().get("share") or "camclips-share")
                                          # WHERE clips + day maps + prefs live: env, or the settings file
SRC = Path(".")
PWFILE = None   # creds come from the environment, not a file
CAM = os.environ.get("IPC360_CAM_ID") or str(cfg().get("cam", ""))   # the camId as digits
LOCK_TIMEOUT = 1200                     # wall-clock guard for one grab (s)
PASS_S = 57                             # MEASURED wall-clock per PASS per MINUTE of SD (2026-10-31: the
                                        # p1..p7 wire files of three different minutes land 57 s apart, every pass). So
                                        # one pass streams a minute of SD slightly FASTER than realtime; a 5-pass pull
                                        # of a 3-minute range is ~14 min, not 30.
RUNNING = SHARE / "grab-running.json"   # which grab holds grab.lock (day/range/started) -> /status
SWEEP = SHARE / "sweep-running.json"    # a MONTH SWEEP holds grab.lock too (month/total/done) -> /status
PREFS = SHARE / "prefs.json"            # the viewer's settings, shared by every page: clock ("pm"/"") + the look effects ON

# ipc360.py's own stdout lines, kept as a fallback for clips published before
# seg.stats.json existed (the stats sidecar in the seg dir is the primary source now).
PLAY_RE = re.compile(r"play: (\d+) video groups \((\d+) B h264\) \+ (\d+) audio groups \((\d+) B\), (\d+) partial")
PASS_RE = re.compile(r"pull (\d)/(\d): (\d+) chunks, (\d+) complete frames, (\d+) groups\s+ep=\('?([^']*')?, ?(\d+)\)")
ERR_RE = re.compile(r"error while decoding")


def clip_name(day: str, rng: str) -> str:
    """2026-10-30 13:00-13:01 -> 2026-10-30_1300-1301."""
    return "%s_%s" % (day, rng.replace(":", ""))


def ffprobe_dur(path: Path, stream: str | None = None) -> float | None:
    """Duration in seconds of the container (or one stream) via ffprobe."""
    args = ["ffprobe", "-v", "error", "-show_entries", "format=duration"]
    if stream:
        args = ["ffprobe", "-v", "error", "-select_streams", stream + ":0",
                "-show_entries", "stream=duration"]
    args += ["-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    r = subprocess.run(args, capture_output=True, text=True)
    for ln in (r.stdout or "").splitlines():        # stdout only: stderr has "264 kb/s" noise
        m = re.match(r"\s*(\d+(?:\.\d+)?)", ln)
        if m:
            return float(m.group(1))
    return None


def ffprobe_fps(path: Path, default: float = 20.0) -> float:
    """The video stream's REAL frame rate (ffprobe r_frame_rate "10/1" -> 10.0). The SD does
    not always record at 20 fps: quiet minutes come off at 10 fps (00:42-00:45 of 2026-10-31
    is 1799 frames / 179.8 s = 10 fps), so anything that counts frames must use this."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=r_frame_rate",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True)
    for ln in (r.stdout or "").splitlines():
        m = re.match(r"\s*(\d+)\s*/\s*(\d+)", ln)
        if m and int(m.group(2)):
            return float(m.group(1)) / float(m.group(2))
    return default

def faststart(src: Path, dst: Path) -> bool:
    """Lossless re-mux so browsers can seek (moov atom first) + a poster frame."""
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-c", "copy", "-movflags", "+faststart",
                        "-strict", "-1", str(dst)],   # -strict -1: mp3 at 8 kHz is "not standard"
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(dst),
                    "-frames:v", "1", "-vf", "scale=480:-1", str(dst.with_suffix(".jpg"))],
                   capture_output=True, text=True)
    return True


def seg_stats(segdir: Path, prefix: str = "seg") -> dict:
    """ipc360.py's stats sidecar (written by assemble() next to seg.*).

    `segdir` = the DIRECTORY that holds seg.* (…/grab-X, probes/play-P7b).  Handing it the
    seg PREFIX instead (…/grab-X/seg) looks for …/grab-X/seg/seg.stats.json = nothing, and
    that silent {} is exactly why the 2026-10-31 PAGE-pulled clips' sidecars lacked audio_codec
    / video_fid_* / partial_list (root cause, confirmed: seg_stats(work) = 34 keys, seg_stats(work/'seg')
    = 0).  Accept either spelling now, and PRINT when the file is genuinely missing so a thin
    card can never be silent again.
    """
    p = Path(segdir)
    f = p / (prefix + ".stats.json")
    if not f.exists() and p.name == prefix:            # caller handed the PREFIX path
        f = p.parent / (prefix + ".stats.json")
    try:
        return json.loads(f.read_text())
    except Exception as e:
        print("camclips: no stats sidecar at %s (%s) - this clip's card will be thin" % (f, e))
        return {}


def hms(sec: float) -> str:
    """51.5 -> '0:51' (timeline tick labels)."""
    s = int(round(sec))
    return "%d:%02d" % (s // 60, s % 60)


def plural(n: int) -> str:
    """1 -> '1 clip', 4 -> '4 clips' (the pages read like an app, not a form field)."""
    return "%d clip%s" % (n, "" if n == 1 else "s")


def atlabel(ts) -> str:
    """A share-timestamp as the wall clock saw it, WITH the zone abbreviation.
    Nov 1 2026 is exactly the day this matters: the clock went 02:00 EDT -> 01:00 EST, so the
    01:xx hour happened TWICE and a bare `01:47:47` reads as if it were in the FUTURE . `%Z` (EDT/EST) breaks the tie."""
    return time.strftime("%H:%M:%S %Z", time.localtime(float(ts)))


def clock_pref() -> str:
    """The saved clock mode for EVERY day page: 'pm' or '' (24 h). Set ONCE by clicking the chip
    on any day ."""
    try:
        p = json.loads(PREFS.read_text())
    except Exception:
        return ""
    return "pm" if (p.get("clock") == "pm") else ""


def fx_pref() -> list:
    """Which FX blocks are ON for EVERY page. Saved in prefs.json next to the clock setting
    . No `fx` key yet =
    the whole look on, which is what the gallery had before the selector existed."""
    try:
        p = json.loads(PREFS.read_text())
    except Exception:
        return list(FX_ORDER)
    v = p.get("fx") if isinstance(p, dict) else None
    if v is None:
        return list(FX_ORDER)
    return [b for b in FX_ORDER if b in set(v)]


def set_fx_pref(names) -> None:
    prefs_save(fx=sorted(set(names)))


def set_clock_pref(mode: str) -> None:
    prefs_save(clock=("pm" if mode == "pm" else ""))


def prefs_read() -> dict:
    try:
        p = json.loads(PREFS.read_text())
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def prefs_save(**kv) -> None:
    """prefs.json holds EVERY viewer setting (clock, fx) - merge, never rewrite the file,
    or setting the clock would silently drop the look and vice versa."""
    p = prefs_read()
    p.update(kv)
    try:
        PREFS.write_text(json.dumps(p, indent=1))
    except Exception as e:
        print("camclips: pref %s: %s" % (kv, e))


def day_closed(day: str) -> bool:
    """True when that date is entirely in the past on the box's clock (its recordings cannot grow)."""
    return day < datetime.date.today().isoformat()


def card_complete(day: str, d: dict | None) -> bool:
    """True when the CACHED day map is the whole truth, so opening that day needs NO camera work:
    the date is over AND our read of it happened AFTER that date ended (every minute of it was
    already closed on the card when we read it) AND the read actually answered. The card recycling
    an old day is the rare exception, covered by the `check this day` button (and by
    /day?day=X&recheck=1), not by asking the camera every time a page is opened."""
    if not d or d.get("failed") or not d.get("at") or not day_closed(day):
        return False
    y, mo, dd = (int(x) for x in day.split("-"))
    # NOT +86400: on a DST-change date the local day is 23 or 25 hours long (Nov 1 2026 is 25 h -
    # the 01:00-02:00 hour repeats), so a read at 23:30 would look "after the day ended" while
    # minutes 23:31-23:59 were still open. mktime() of the NEXT date applies the right DST rule.
    end = time.mktime((datetime.date(y, mo, dd) + datetime.timedelta(days=1)).timetuple())
    return d["at"] >= end


def mb(n) -> str:
    """5246968 -> '5.2 MB' (the pages print sizes everywhere, so keep them tidy)."""
    return ("%.1f MB" % (n / 1e6) if n >= 1e6 else
            "%.0f KB" % (n / 1e3) if n >= 1e3 else "%d B" % n)


def thumbs(mp4: Path, n: int = 9, width: int = 300) -> list:
    """n evenly spaced stills across the clip -> NAME-th<k>.jpg in the share.

    The frame numbers come from ffmpeg's `select` filter on the DECODED stream, so each
    still's label is exact — but the frame count needs the clip's REAL fps (ffprobe r_frame_rate), since some minutes record at 10, not 20).
    Returns [[clip_seconds, "NAME-th<k>.jpg"], …] for the sidecar json.
    """
    name = mp4.name[:-4]
    dur = ffprobe_dur(mp4) or 0.0
    if dur <= 0.2:
        return []
    fps = ffprobe_fps(mp4)                        # the SD's own rate for THIS clip
    nf = int(round(dur * fps))                    # frames in the clip at that rate
    if nf < 2:
        return []
    ns = [min(nf - 1, max(0, int(round((i + 0.5) * nf / float(n))))) for i in range(n)]
    sel = "+".join("eq(n\\,%d)" % k for k in ns)
    pat = str(SHARE / ("%s-th%%02d.jpg" % name))      # ffmpeg writes th00.jpg … th08.jpg
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4),
                        "-vf", "select='%s',scale=%d:-2" % (sel, width),
                        "-vsync", "0", "-start_number", "0", pat], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for i, k in enumerate(ns):
        fn = "%s-th%02d.jpg" % (name, i)
        if (SHARE / fn).exists():
            out.append([round(k / fps, 2), fn])
    return out


def run_grab(day: str, rng: str, passes: int = 5, seconds: int | None = None) -> dict:
    """ipc360.py --play into the share; returns the metadata dict (None fields on failure).

    The pull's stdout is streamed line-by-line into `grab-<name>.log` (python3 -u) so
    /status shows live progress while the thread runs, instead of an empty tail.
    """
    name = clip_name(day, rng)
    work = SHARE / ("grab-" + name)
    work.mkdir(exist_ok=True)
    mins = max(1, mins_of(name, rng)[1] - mins_of(name, rng)[0])   # minutes of SD in this range
    if seconds is None:
        # ipc360.py's --seconds is a WALL-CLOCK CAP PER PASS (pull() starts its clock at the
        # top of every pass), so a fixed 150 s silently truncates ranges longer than ~2.6 min.
        # One pass costs ~PASS_S s per minute of SD ⇒ size the cap to the range, with slack.
        seconds = int(max(150, PASS_S * mins * 1.8))
    kill_after = max(LOCK_TIMEOUT, int(PASS_S * passes * mins * 1.6) + 300)
    env = dict(os.environ, IPC360_LOCK_OK="1")   # IPC360_EMAIL / IPC360_PW / IPC360_CAM_ID are inherited
    cmd = ["python3", "-u", str(SRC / "ipc360.py"), "--cam", str(CAM), "--play", rng, "--day", day,
           "--out", str(work / "seg"), "--passes", str(passes),
           "--seconds", str(seconds), "--idle", "15"]
    print("camclips: running", " ".join(cmd))
    logpath = work / ("grab-" + name + ".log")
    t0 = time.time()
    out = []
    with open(logpath, "w") as lf:
        lf.write("cmd: %s\n" % " ".join(cmd))
        lf.flush()
        p = subprocess.Popen(cmd, cwd=str(SRC), env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        while True:
            ln = p.stdout.readline()
            if ln:
                out.append(ln)
                lf.write(ln)
                lf.flush()
                continue
            if p.poll() is not None:
                break
            if time.time() - t0 > kill_after:
                p.kill()
                out.append("camclips: killed after %d s (kill_after, scaled to the range)\n" % kill_after)
                lf.write(out[-1])
                break
    log = "".join(out)
    meta = {"name": name, "day": day, "range": rng, "at": int(t0), "passes": passes}
    meta["smin"], meta["emin"] = mins_of(name, rng)
    m = PLAY_RE.search(log)
    if m:
        meta.update(video_groups=int(m.group(1)), video_bytes=int(m.group(2)),
                    audio_groups=int(m.group(3)), audio_bytes=int(m.group(4)), partial=int(m.group(5)))
    meta["pulls"] = [{"pass": int(g[0]), "chunks": int(g[2]), "frames": int(g[3]),
                      "groups": int(g[4]), "relay": "%s:%s" % (g[5].strip("'"), g[6])}
                     for g in PASS_RE.findall(log)]
    st = seg_stats(work)         # work = the DIR holding seg.* (prefix defaults to "seg")
    for k, v in st.items():
        if k not in ("video", "audio", "audio_s16", "mp4", "mkv", "wav"):
            meta[k] = v
    meta["decode_errors"] = st.get("decode_errors", len(ERR_RE.findall(log)))
    meta["grab_rc"] = p.poll()
    if not meta["pulls"] and not meta.get("video_groups"):
        meta["grab_failed"] = True                      # day map empty / mgr conn torn down: no clip
    src_mp4 = work / "seg.mp4"
    dst = SHARE / (name + ".mp4")
    if src_mp4.exists():
        meta["faststart"] = faststart(src_mp4, dst)
        if not meta["faststart"]:
            src_mp4.rename(dst)
        meta["bytes"] = dst.stat().st_size if dst.exists() else 0
        meta["dur_video"] = ffprobe_dur(dst)
        meta["dur_audio"] = ffprobe_dur(dst, "a")
        meta["video_fps"] = meta.get("video_fps") or ffprobe_fps(dst)   # container truth (10 or 20)
        meta["thumbs"] = thumbs(dst)
    (SHARE / (name + ".json")).write_text(json.dumps(meta, indent=1, sort_keys=True))
    return meta


def daymap(day: str, force: bool = False) -> dict:
    """ipc360.py --list -> {'ranges': [[start,end,flag],…], 'summary': str} cached to disk.
    While a pull holds `grab.lock` we must NOT spawn --list: the mgr keys its session by uid,
    so a second ipc360 on the same uid tears the running pull down (BrokenPipeError in mgr.send
    and its day map comes back "0 chunks / no mgr 20204 reply"). Serve the cache instead."""
    cache = SHARE / ("daymap-%s.json" % day)
    lock = SHARE / "grab.lock"
    if not force and lock.exists() and time.time() - lock.stat().st_mtime < LOCK_TIMEOUT:
        busy = ("a month sweep is walking the card" if SWEEP.exists() and
                time.time() - SWEEP.stat().st_mtime < LOCK_TIMEOUT else "a pull is running")
        try:
            out = json.loads(cache.read_text())
            out["note"] = ("%s (grab.lock) - the mgr session is NOT ours, showing the "
                           "cached day map; it is re-read when the walk ends" % busy)
        except Exception:
            out = {"day": day, "summary": "day map not readable while a pull runs (mgr session busy)",
                   "ranges": [], "at": int(time.time()),
                   "note": "one mgr session at a time - see /status"}
        return out
    env = dict(os.environ)
    if force:
        # WE own grab.lock here (a month sweep): ipc360's own --list guard must not refuse our queries
        env["IPC360_LOCK_OK"] = "1"
    p = subprocess.run(["python3", str(SRC / "ipc360.py"), "--list", "--day", day],
                       cwd=str(SRC), env=env, capture_output=True, text=True, timeout=120)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    summary = next((ln for ln in log.splitlines() if ln.startswith("day ")), "")
    ranges = []
    for ln in log.splitlines():
        m = re.match(r"\s+(\d\d):(\d\d)-(\d\d):(\d\d)\s+flag=0x([0-9a-f]+)", ln)
        if m:
            h0, m0, h1, m1, f = m.groups()
            ranges.append([int(h0) * 60 + int(m0), int(h1) * 60 + int(m1), int(f, 16)])
    out = {"day": day, "summary": summary, "ranges": ranges, "at": int(time.time()), "log": log}
    if not summary:
        # NO "day YYYY-MM-DD: …" line came back = the mgr did not answer (torn-down session, refused
        # login, dead camera). Caching that as an EMPTY day would make the calendar LIE - a date that
        # really has recordings would render as "card had nothing" - so nothing is written here and the
        # caller is told the read failed (and the stale cache, if any, is what the pages keep showing).
        out["failed"] = True
        out["note"] = "the card did not answer for this date; nothing was cached"
        return out
    cache.write_text(json.dumps(out, indent=1))
    return out


DAYMAP_RE = re.compile(r"^day (\d{4}-\d\d-\d\d): (\d+) chunks, (\d+) active minutes")


def daymaps_on_disk() -> dict:
    out = {}
    for f in SHARE.glob("daymap-*.json"):
        try:
            out[f.name[len("daymap-"):-len(".json")]] = json.loads(f.read_text())
        except Exception:
            pass
    return out


def month_dates(month: str) -> list:
    """Every REAL date of a YYYY-MM month, capped at the box's today: ['2026-10-01', …].
    The camera cannot hold a date that has not happened yet, so the calendar never draws one."""
    y, mo = int(month[:4]), int(month[5:7])
    today = datetime.date.today()
    return [datetime.date(y, mo, d1).isoformat()
            for d1 in range(1, calendar.monthrange(y, mo)[1] + 1)
            if datetime.date(y, mo, d1) <= today]


def month_ok(s: str) -> bool:
    """True only for a REAL month string: the regex alone lets 2026-13 through, and that is not
    cosmetic - month_dates() would raise IllegalMonthError (which also poisoned the /sweep route,
    leaving grab.lock behind). Check the month NUMBER too."""
    return bool(re.match(r"^\d{4}-\d{2}$", s or "")) and 1 <= int(s[5:7]) <= 12


def month_step(month: str, step: int) -> str:
    """YYYY-MM shifted by step months (the calendar's « previous month » / 'next month' chips)."""
    y, mo = int(month[:4]), int(month[5:7]) + step
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return "%04d-%02d" % (y, mo)


def cal_bar(lo: int, hi: int, cls: str) -> str:
    """One hairline mark inside a calendar cell: minutes-of-day -> % of the 24-hour bar."""
    left = lo / 14.4                       # 1440 minutes = 100 %
    w = max(0.45, (hi - lo + 1) / 14.4)
    if left + w > 100:
        w = max(0.45, 100 - left)
    return '<i class="%s" style="left:%.2f%%;width:%.2f%%"></i>' % (cls, left, w)


def calendar_html(month: str, dm: dict, by_day: dict) -> str:
    """The home page's calendar: a 7-column grid, Sunday first, one cell per real date of the month.
    A cell = the date (click -> that day's page, WHICH READS THE CARD for that date) + a 24-hour
    hairline bar of what the card holds for it, taken from that date's CACHED day map only (green =
    minutes the card holds, dashed = minute still open on the card, amber = minutes already pulled
    into this gallery). Nothing here touches the camera, so a date nobody asked about is a bare date,
    NOT an empty day - the two look different on the page on purpose."""
    y, mo = int(month[:4]), int(month[5:7])
    lead = (datetime.date(y, mo, 1).weekday() + 1) % 7          # Sunday-first
    cells = ['<div class="cwd">%s</div>' % w for w in "SMTWTFS"]
    cells += ['<div class="cblank"></div>'] * lead
    for day in month_dates(month):
        d = dm.get(day)
        clips = by_day.get(day) or []
        marks = [cal_bar(min(a, b), max(a, b), "copen" if fl == 0x80 else "cmin")
                 for a, b, fl in ((d or {}).get("ranges") or [])]
        for nm, meta in clips:
            lo, hi = meta.get("smin") or 0, meta.get("emin") or 0
            if hi >= lo:
                marks.append(cal_bar(lo, hi, "cclip"))
        cardm = sum(r[1] - r[0] + 1 for r in d["ranges"]) if d and d.get("ranges") else None
        read = (time.strftime("%H:%M %Z", time.localtime(d["at"])) if d and d.get("at") else "")
        note = ("%d clip%s" % (len(clips), "" if len(clips) == 1 else "s")) if clips else (
            "card had nothing" if (d and not d.get("ranges")) else "")
        tip = "%s \u00b7 %d clip%s" % (day, len(clips), "" if len(clips) == 1 else "s")
        if cardm is not None:
            tip += " \u00b7 %d min on card, read %s" % (cardm, read)
        elif d:
            tip += " \u00b7 card checked %s, holds nothing" % read
        cells.append('<div class="cday"><a href="/day?day=%s" title="%s">%s<div class="cbar">%s</div>%s</a></div>'
                     % (esc(day), esc(tip), esc(day[-2:]), "".join(marks),
                        ('<div class="cnote">%s</div>' % esc(note)) if note else ""))
    return '<div class="cal">%s</div>' % "".join(cells)


def sweep_running():
    """The live sweep's state dict, or None. Kept separate so the month page can HIDE the
    `check this month` chip while a sweep walks (a second click would only be refused anyway)."""
    try:
        sw = json.loads(SWEEP.read_text())
    except Exception:
        return None
    if not sw or not SWEEP.exists() or time.time() - SWEEP.stat().st_mtime > LOCK_TIMEOUT:
        return None
    return sw


def sweep_note() -> str:
    """A panel the month page draws WHILE a sweep walks the card, so the viewer sees why dates are
    arriving on their own. Driven by sweep-running.json, which the sweep rewrites every date."""
    sw = sweep_running()
    if not sw:
        return ""
    return ('<div class="panel"><div class="k"><b>CHECKING THE CARD for %s</b> \u00b7 %s/%s dates \u00b7 '
            'one card query per date, each date lands in the calendar as it is done \u00b7 nothing can be '
            'pulled while this walks <a class="help" href="/help">?</a></div></div>'
            % (esc(sw.get("month") or "?"), sw.get("done") or 0, sw.get("total") or 0))


def sweep_month(month: str, days: list | None = None) -> dict:
    """Ask the card for EVERY date of a month, one mgr query each, caching each day map so the
    calendar fills itself. Holds grab.lock for the whole walk (a pull cannot collide with it) and
    re-touches it every date so the lock never looks stale; progress goes to sweep-running.json +
    sweep.log so the LIVE bar and /status can count it. force=True because WE own the lock here."""
    days = days if days is not None else month_dates(month)
    lock, log = SHARE / "grab.lock", SHARE / "sweep.log"
    started = int(time.time())
    got = 0
    with open(log, "w") as lf:
        lf.write("sweep %s: %d dates, one card query each\n" % (month, len(days)))
        lf.flush()
        for i, dd in enumerate(days, 1):
            try:
                d = daymap(dd, force=True)
                if d.get("failed"):
                    lf.write("%s -> NO DAY MAP from mgr (not cached)\n" % dd)
                else:
                    lf.write("%s -> %s\n" % (dd, (d.get("summary") or "no day map")))
                    got += 1
            except Exception as e:
                lf.write("%s -> FAILED %s\n" % (dd, e))
            lf.flush()
            SWEEP.write_text(json.dumps({"month": month, "started": started, "done": i,
                                         "total": len(days), "last": dd}, indent=1))
            lock.touch()
    return {"month": month, "dates": len(days), "maps": got, "at": started}


def clips_on_disk() -> list:
    out = []
    for f in sorted(SHARE.glob("*.mp4")):
        name = f.name[:-4]
        try:
            meta = json.loads((SHARE / (name + ".json")).read_text())
        except Exception:
            meta = {"name": name, "bytes": f.stat().st_size, "at": int(f.stat().st_mtime)}
        meta.setdefault("name", name)
        m = re.match(r"(\d{4}-\d\d-\d\d)", name)      # a clip published without a sidecar still
        if m and not meta.get("day"):
            meta["day"] = m.group(1)                  # belongs under its name's date
        t = re.match(r"\d{4}-\d\d-\d\d-(\d\d)-(\d\d)-\d\d", name)   # <tsv time>-style names
        if t and not meta.get("range"):
            hh, mm = int(t.group(1)), int(t.group(2))
            meta["range"] = "%02d:%02d-%02d:%02d" % (hh, mm, hh, mm + 1)
        if not meta.get("smin") and not meta.get("emin"):
            meta["smin"], meta["emin"] = mins_of(name, meta.get("range"))
        if ":" not in (meta.get("range") or "") and (meta.get("smin") or 0) < (meta.get("emin") or 0):
            # a sidecar whose range came in garbage (e.g. "24" from a clipped --import arg) or was
            # never given: the name's own minute span is authoritative, so rebuild the label.
            meta["range"] = "%02d:%02d-%02d:%02d" % (meta["smin"] // 60, meta["smin"] % 60,
                                                     meta["emin"] // 60, meta["emin"] % 60)
        if "dur_video" not in meta:
            meta["dur_video"] = ffprobe_dur(f)
        out.append((name, meta))
    return out


def esc(s) -> str:
    return _html.escape(str(s), quote=True)


MIME = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".jpg": "image/jpeg",
        ".json": "application/json", ".log": "text/plain; charset=utf-8",
        ".h264": "video/x-h264", ".raw": "audio/vnd.wave"}


def mins_of(name: str, rng: str | None = None) -> tuple:
    """(start_min, end_min) for the day-map strip marker: from '13:00-13:01' or the clip name."""
    s = rng or ""
    m = re.search(r"(\d\d):(\d\d)-(\d\d):(\d\d)", s) or re.search(r"_(\d\d)(\d\d)-(\d\d)(\d\d)", name)
    if not m:
        return (0, 0)
    a, b, c, d = (int(x) for x in m.groups())
    return (a * 60 + b, c * 60 + d)



def card_bits(name: str, meta: dict) -> str:
    """The card's fact line, SHORT — the long "why is it like this" prose lives on /help."""
    b = []
    if meta.get("reference"):
        b.append("REFERENCE")
    if meta.get("dur_video"):
        b.append("video %.1f s" % meta["dur_video"])
    if meta.get("video_groups"):
        fps = meta.get("video_fps") or (
            round(meta["video_groups"] / meta["dur_video"], 2) if meta.get("dur_video") else None)
        b.append("%d video groups (fid %s\u2026%s = %s fps)"
                 % (meta["video_groups"], meta.get("video_fid_min", "?"),
                    meta.get("video_fid_max", "?"), fps if fps else "?"))
        CODEC = {"alaw": "a-law", "u8": "u8 linear (old, wrong)", "s8": "s8 linear"}
        b.append("audio = %s @ %s Hz mono"
                 % (CODEC.get(meta["audio_codec"], meta.get("audio_codec") or "?"),
                    meta.get("audio_rate") or "?"))
        b.append("anchor fid %s \u00b7 head %s s" % (meta.get("audio_anchor", "?"),
                                                     meta.get("audio_head_seconds", "?")))
    if meta.get("audio_seconds"):
        extra = ""
        if meta.get("audio_hole_seconds"):
            extra = " (%.2f s silence in %d lost)" % (meta["audio_hole_seconds"],
                                                      meta.get("audio_hole_packets", 0))
        n_pkt = meta.get("audio_packets") or meta.get("audio_groups") or 0
        b.append("%d audio pkts / %.2f s on %.2f s%s"
                 % (n_pkt, meta["audio_seconds"],
                    meta.get("audio_video_span_seconds") or meta.get("dur_video") or 0, extra))
    if meta.get("partial"):
        b.append("%d PARTIAL (%s)" % (meta["partial"], "; ".join(meta.get("partial_list") or [])))
    if meta.get("decode_errors") is not None:
        b.append("%d decode err" % meta["decode_errors"])
    if meta.get("passes"):
        relays = [p["relay"] for p in (meta.get("pulls") or [])]
        shown = ", ".join(relays[:3]) + ((" (+%d more)" % (len(relays) - 3)) if len(relays) > 3 else "")
        b.append("%d passes / relays %s" % (meta["passes"], shown or "?"))
    return " &nbsp; ".join(esc(x) for x in b)



def timeline_html(name: str, meta: dict) -> str:
    """The interactive ruler under a clip: hairline ticks, click-anywhere-to-seek, a slider,
    JS-free `#t=` jump links, and a filmstrip of stills (every still links to that moment)."""
    dur = meta.get("dur_video") or 0.0
    if dur <= 0:
        return ""
    LAB = 15 if dur > 120 else 5      # label every 5 s on a short clip, every 15 s on a long one
    out = ['<div class="tl" id="tl-%s" data-v="v-%s" title="click anywhere to jump the video here">'
           % (name, name)]
    t = 0
    while t <= int(round(dur)):
        pc = t / dur * 100.0
        lab = ('<i class="tkl" style="transform:translateX(-100%%)">%s</i>' % hms(t)
               if pc >= 96.0 else '<i class="tkl">%s</i>' % hms(t))
        out.append('<span class="tk" style="left:%.2f%%">%s</span>'
                   % (pc, lab if t % LAB == 0 else ""))
        t += 5
    out.append('<span class="tend" style="left:%.2f%%"><i style="transform:translateX(-100%%)">%s</i></span>'
               % (100.0, hms(dur)))
    out.append('<div class="tmark" style="left:0%"></div>')
    out.append("</div>")
    out.append('<input class="scrub" type="range" min="0" max="1000" value="0" '
               'data-v="v-%s" data-tl="tl-%s" title="drag to scrub">' % (name, name))
    jumps, t = [], 5.0
    while t < dur:
        jumps.append('<a href="%s.mp4#t=%.1f">%s</a>' % (esc(name), t, hms(t)))
        t += 5.0 if dur <= 120 else 15.0
    out.append('<div class="k2">jump: %s</div>' % " ".join(jumps))
    th = []
    for sec, fn in (meta.get("thumbs") or []):
        th.append('<a class="th" href="%s.mp4#t=%.1f" title="%s into the clip">'
                  '<img src="%s" width="110"><i class="cap">%s</i></a>'
                  % (esc(name), sec, hms(sec), esc(fn), hms(sec)))
    if th:
        out.append('<div class="stills">%s</div>' % "".join(th))
    return "".join(out)



def card_html(name: str, meta: dict, pre: str = "metadata", auto: bool = False) -> str:
    """ONE clip card: video + ruler + facts + its own buttons. Facts come from card_bits, the
    prose explaining them lives on /help."""
    poster = name + ".jpg"
    stamp = meta.get("at") or int((SHARE / (name + ".mp4")).stat().st_mtime)
    rng = meta.get("range") or ""
    sd = ("SD %s" % rng.replace("-", "\u2013")) if rng else ""
    head = ('<video id="v-%s" controls%s preload="%s" poster="%s" width="760">\n'
            '  <source src="%s.mp4" type="video/mp4">can\'t play mp4 here \u2014 '
            '<a href="%s.mp4">open the file</a></video>\n'
            % (esc(name), " autoplay muted" if auto else "", pre,
               esc(poster) if (SHARE / poster).exists() else "", esc(name), esc(name)))
    # a browser only lets a script start an UN-muted video on a page the viewer has already played
    # on, so the tag starts muted (allowed) and this script unmutes 600 ms in.
    js = ""
    if auto:
        js = ('<script>(function(){var v=document.getElementById("v-%s");if(!v)return;'
              'try{v.play();}catch(e){}'
              'window.setTimeout(function(){try{v.muted=false;}catch(e){}},600);})();</script>'
              % esc(name))
    lossless = ""
    if meta.get("mkv") or meta.get("wav"):
        lossless = ('<div class="k2">lossless on the box: %s .mkv + %s .wav (un-re-encoded)'
                    % (esc(meta.get("mkv") or ""), esc(meta.get("wav") or "")))
    return "".join(['<div class="card" id="clip-%s">' % esc(name),
                    '<div class="k"><b>%s</b> &nbsp; %s &nbsp; %s &nbsp; %s</div>'
                    % (esc(name), esc(time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(stamp))),
                       mb(meta.get("bytes") or (SHARE / (name + ".mp4")).stat().st_size), esc(sd)),
                    head,
                    timeline_html(name, meta),
                    js,
                    '<div class="facts">%s</div>' % card_bits(name, meta),
                    '<div class="acts">'
                    '<a class="btn" href="/clip?name=%s">replay</a> '
                    '<a class="btn" href="/dl/%s.mp4" download="%s.mp4">save</a> '
                    '<a class="btn" href="/delete?name=%s&day=%s">delete</a> '
                    '<a href="%s.json">metadata</a></div>'
                    % (esc(name), esc(name), esc(name), esc(name), esc(meta.get("day") or ""),
                       esc(name)),
                    "</div>", lossless])



def strip_rows(day: str, d: dict, clips: list, pm: bool = False) -> str:
    """The SD day map as TWO full-width rows: 00:00-11:59 above, 12:00-23:59 below.

    A 1-minute cell drawn across the whole 24 h is under 1 px, so the strip used to be unreadable
    (and noon was cut in half); two rows double the room and noon always starts row 2. MAPJS then
    wheel-zooms whichever row your cursor is over. Every cell/tick ALSO carries data-s/data-e/data-
    min and the server layout is the whole 12 h row, so with JS off the page still reads (it just
    can't zoom). Green = the card holds that minute (click = pull it), dashed = flag 0x80, the
    minute still being written, amber = a clip already in the gallery."""
    rows = []
    for lo, hi in ((0, 719), (720, 1439)):
        span = float(hi - lo + 1)

        def pc(minute, _lo=lo, _span=span):
            return max(0.0, min(100.0, (minute - _lo) / _span * 100.0))

        cells = []
        for s0, e0, f in d["ranges"]:
            s, e = max(s0, lo), min(e0, hi)
            if s > e:
                continue                          # that range lives on the other row
            t0, t1 = "%02d:%02d" % (s // 60, s % 60), "%02d:%02d" % (e // 60, e % 60)
            pcx, wdx = pc(s), (e - s + 1) / span * 100.0
            base = "%s\u2013%s \u00b7 %d min \u00b7 flag 0x%02x" % (t0, t1, e - s + 1, f)
            have = [c for c in clips if c[1].get("day") == day
                    and not (c[1].get("emin", 0) <= s or c[1].get("smin", 0) > e)]
            if have:
                cells.append('<a class="cell" data-s="%d" data-e="%d" style="left:%.3f%%;width:%.3f%%" '
                             'title="%s \u00b7 already pulled \u2014 opens that video" '
                             'href="/clip?name=%s"></a>'
                             % (s, e, pcx, wdx, esc(base), esc(have[0][0])))
            elif f == 0x80:
                cells.append('<div class="cellopen" data-s="%d" data-e="%d" style="left:%.3f%%;width:%.3f%%" '
                             'title="%s \u00b7 still open on the card"></div>'
                             % (s, e, pcx, wdx, esc(base)))
            elif e - s <= 2:
                rng = "%02d:%02d-%02d:%02d" % (s // 60, s % 60, (e + 1) // 60, (e + 1) % 60)
                cells.append('<a class="cell" data-s="%d" data-e="%d" data-f="%d" style="left:%.3f%%;width:%.3f%%" '
                             'title="%s \u00b7 click = pull" href="/pull?day=%s&range=%s&passes=5"></a>'
                             % (s, e, f, pcx, wdx, esc(base), esc(day), rng.replace(":", "%3A")))
            else:
                cells.append('<div class="cell" data-s="%d" data-e="%d" data-f="%d" style="left:%.3f%%;width:%.3f%%" '
                             'title="%s \u00b7 %d min, too big to click-pull"></div>'
                             % (s, e, f, pcx, wdx, esc(base), e - s + 1))
        marks = []
        for name, meta in clips:
            if meta.get("day") != day:
                continue
            s, e = meta.get("smin", 0), meta.get("emin", 0)
            if not (lo <= s <= hi):
                continue
            marks.append('<a class="clip" data-s="%d" data-e="%d" style="left:%.3f%%;width:%.3f%%" '
                         'title="%s \u00b7 in the gallery" href="/clip?name=%s"></a>'
                         % (s, e, pc(s), max(e - s, 2) / span * 100.0,
                            esc("%s %s" % (day, meta.get("range") or name)), esc(name)))
        ticks = []

        def hlab(m, _pm=(pm and lo == 720)):
            """Hour label: 24 h by default; in the `pm` option the afternoon row reads 12 PM, 1 PM…"""
            h, mm = m // 60, m % 60
            if _pm:
                h12 = 12 if h == 12 else h - 12
                return ("%d PM" % h12) if mm == 0 else ("%d:%02d PM" % (h12, mm))
            return "%02d:%02d" % (h, mm)

        for m in range(lo, hi + 1, 60):
            tl = hlab(m)
            lab = ('<i style="transform:translateX(-100%%)">%s</i>' % tl) if pc(m) >= 92.0 \
                  else ('<i>%s</i>' % tl)
            ticks.append('<span class="tka" data-min="%d" style="left:%.2f%%">%s</span>' % (m, pc(m), lab))
        for m in range(lo, hi + 1, 15):
            if m % 60:
                ticks.append('<span class="tkb" data-min="%d" style="left:%.2f%%"></span>' % (m, pc(m)))
        for m in range(lo, hi + 1, 5):
            if m % 15:
                ticks.append('<span class="tkq" data-min="%d" style="left:%.2f%%"></span>' % (m, pc(m)))
        rows.append('<div class="sdr" data-lo="%d" data-span="%d"%s>%s%s%s</div>'
                    % (lo, int(span), ' data-pm="1"' if (pm and lo == 720) else "",
                       "".join(ticks), "".join(cells), "".join(marks)))
        # NO row label "00:00 – 11:59" / "12:00 – 23:59": the hour labels under the bar already
        # say it ( MAPJS puts a .srlab in ONLY once you zoom into a
        # window narrower than that row, where the hour ticks alone can't tell you the window.
    leg = ('<div class="legend">'
           '<span class="lg-ok">\u25ae on card \u00b7 click = pull it</span>'
           '<span class="lg-open">\u25a1 still open on the card</span>'
           '<span class="lg-clip">\u25ae pulled (amber)</span>'
           '<span class="lg-zoom">wheel over a row zooms it \u00b7 double-click = jump</span></div>')
    return '<div class="smap">%s%s</div>' % ("".join(rows), leg)



CSS = """
<style>
 :root{
  --bg:#0f1216; --panel:#151a20; --panel2:#1a2128; --line:#2b343e; --line2:#3c4a58;
  --tx:#e7ebf0; --tx2:#adbac7; --tx3:#7d8a97; --acc:#75b5dd; --warn:#ffb951;
  --ok:#4ca366; --ok2:#246b3c; --open:#4d6f85; --clip:#e8a62a; --bad:#d46a6a;
 }
 *{box-sizing:border-box}
 html{color-scheme:dark}
 body{background:var(--bg);color:var(--tx);margin:0;font-variant-numeric:tabular-nums;
      font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
 pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--tx2)}
 code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--tx2);
      background:var(--panel2);padding:1px 4px;border-radius:4px}
 video{width:100%;max-width:760px;display:block;margin:0 auto 6px;background:#000;border-radius:6px}
 img{display:inline-block;vertical-align:middle;border:1px solid var(--line);border-radius:3px}
 a{color:var(--acc);text-decoration:none}
 a:hover{color:#d4ebf9;text-decoration:underline}
 h1{font-size:20px;font-weight:600;letter-spacing:.4px;margin:10px 0 10px}
 h2{font-size:15px;font-weight:600;letter-spacing:.3px;color:var(--tx2);margin:12px 0 6px}
 .wrap{max-width:1180px;margin:0 auto;padding:16px 20px 26px}
 .hdr{display:flex;flex-wrap:wrap;align-items:center;gap:16px;padding:0 0 10px;
      border-bottom:1px solid var(--line)}
 .logo{font-size:16px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
 .logo a{color:var(--tx);text-decoration:none}
 .logo a:hover{color:var(--acc);text-decoration:none}
 .mark{border-radius:7px}
 .by{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--tx3);letter-spacing:.2px}
 .by a{color:var(--tx2);text-decoration:none;font-weight:600}
 .by a:hover{color:var(--acc);text-decoration:none}
 .by .mark{border-radius:6px}
 .hdr a:hover{text-decoration:none;filter:brightness(1.12)}
 .nav{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:auto}
 .chips{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 4px}
 .chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line2);border-radius:14px;
       padding:3px 11px;font-size:11px;font-weight:600;letter-spacing:.4px;color:var(--tx2);
       background:var(--panel2);white-space:nowrap}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
 .k{color:var(--tx2);font-size:13px} .k2{color:var(--tx3);font-size:12px}
 .facts{color:var(--tx3);font-size:11px;line-height:1.7}
 .dl{color:var(--tx2)}
 .btn{display:inline-flex;align-items:center;padding:4px 10px;border:1px solid var(--line2);
      border-radius:6px;font-size:11px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;
      color:var(--tx2);background:var(--panel2);cursor:pointer;white-space:nowrap;text-decoration:none}
 .btn:hover{background:var(--acc);color:#0e1217;border-color:var(--acc);text-decoration:none}
 .help{display:inline-block;width:18px;height:18px;line-height:18px;text-align:center;border-radius:9px;
       border:1px solid var(--line2);font-size:11px;font-weight:700;color:var(--tx2);
       background:var(--panel2);cursor:pointer;text-decoration:none}
 .help:hover{background:var(--acc);color:#0e1217;border-color:var(--acc);text-decoration:none}
 .dlist{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:10px 0}
 .drow{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:7px 2px;
       border-bottom:1px solid var(--line)}
 .drow:last-child{border-bottom:none}
 .drow:hover{background:var(--panel2)}
 .drow b,.dname{font-weight:600}
 .q-ok{color:var(--ok);font-size:11px;font-weight:600;letter-spacing:.5px}
 .q-part{color:var(--warn);font-size:11px;font-weight:600;letter-spacing:.5px}
 .q-bad{color:var(--bad);font-size:11px;font-weight:600;letter-spacing:.5px}
 .q-ref{color:var(--clip);font-size:11px;font-weight:700;letter-spacing:.6px}
 .dacts{margin-left:auto;display:flex;flex-wrap:wrap;gap:6px}
 .sw{position:relative;display:inline-block;width:170px;height:30px;border-radius:15px;
     border:1px solid var(--line2);background:var(--panel2);color:var(--tx3);cursor:pointer;
     text-decoration:none;overflow:hidden;vertical-align:middle}
 .sw .swl{position:absolute;top:7px;width:46px;height:16px;line-height:16px;text-align:center;
     font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--tx3)}
 .sw .swlo{left:12px} .sw .swlon{right:12px}
 .sw i{position:absolute;top:9px;left:60px;width:14px;height:14px;border-radius:7px;
     background:var(--tx3);font-style:normal}
 .sw-on .swlon{background:var(--ok);color:#0e1217;border-radius:8px}
 .sw-on i{left:96px;background:var(--ok)}
 .sw-off .swlo{background:var(--tx2);color:#0e1217}
 .sw:hover{border-color:var(--acc)}
 #pullnote{background:var(--panel2);border:1px solid var(--line2);border-radius:8px;
           padding:9px 13px;font-size:13px;color:var(--tx)}
 #pullnote b{color:var(--warn)} #pullnote a{color:var(--acc)}
 .st{position:sticky;top:0;left:0;right:0;z-index:9;background:var(--panel);
     border-bottom:1px solid var(--line2);padding:7px 20px;font-size:12px;color:var(--tx2);
     white-space:nowrap;letter-spacing:.2px}
 .st b{color:var(--warn)} .st a{color:var(--acc)}
 .stlive{color:var(--warn);font-weight:600} .stidle{color:var(--tx3)} .st2{color:var(--tx3);font-size:11px}
 .st i.live{display:inline-block;width:8px;height:8px;border-radius:4px;background:var(--tx3);
            font-style:normal}
 .stlive i.live{background:var(--warn);animation:pulse 1.5s ease-in-out infinite}
 @keyframes pulse{0%{opacity:.95}50%{opacity:.25}100%{opacity:.95}}
 .tl{position:relative;height:44px;background:var(--panel2);border:1px solid var(--line2);
     border-radius:6px;margin:6px 0 6px}
 .tk{position:absolute;top:0;bottom:0;width:1px;background:var(--line2)}
 .tkl{position:absolute;top:26px;left:3px;font-size:10px;color:var(--tx3);white-space:nowrap;
      pointer-events:none;font-style:normal}
 .tend{position:absolute;top:26px;font-size:10px;color:var(--tx3);pointer-events:none}
 .tend i{font-style:normal}
 .tmark{position:absolute;top:0;bottom:0;width:3px;background:var(--clip);border-radius:2px;
        box-shadow:0 0 8px rgba(232,166,42,.75)}
 .scrub{width:240px;height:22px;margin:4px 0 0;accent-color:var(--acc)}
 .jumps{color:var(--tx3);font-size:11px}
 .stills{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start;margin:10px 0}
 .th{display:inline-block;max-width:118px;text-align:center;transition:transform .12s ease}
 .th img{display:block;margin:0 auto}
 .th:hover{transform:scale(1.04)}
 .cap{display:block;font-size:10px;color:var(--tx3);margin-top:4px;font-style:normal}
 .smap{margin:0}
 .sdr{position:relative;height:34px;background:var(--panel2);border:1px solid var(--line2);
      border-radius:4px;margin:34px 0 0;overflow:visible;
      background-image:linear-gradient(90deg,var(--line2) 1px,transparent 1px);
      background-size:8.3333% 100%}
 .cell{position:absolute;top:3px;bottom:3px;background:var(--ok2);border:1px solid var(--ok);
       border-radius:2px;cursor:pointer}
 .cell:hover{background:var(--ok);border-color:var(--ok)}
 .cellopen{position:absolute;top:3px;bottom:3px;background:var(--panel2);border:1px dashed var(--open);
           border-radius:2px}
 .clip{position:absolute;top:0;bottom:0;background:var(--clip);border-radius:2px;min-width:6px;
       opacity:.95;box-shadow:0 0 7px rgba(232,166,42,.5)}
 .clip:hover{opacity:1;background:#ffc86a}
 .tka{position:absolute;top:0;bottom:0;width:1px;background:var(--line2);pointer-events:none}
 .tka i{position:absolute;top:37px;left:3px;font-size:10px;color:var(--tx3);white-space:nowrap;
        pointer-events:none;font-style:normal}
 .tka i[style]{font-style:normal}
 .tkb{position:absolute;top:9px;bottom:0;width:1px;background:var(--line2);opacity:0;pointer-events:none}
 .tkq{position:absolute;top:16px;bottom:0;width:1px;background:var(--line);opacity:0;pointer-events:none}
 .sdr.b15 .tkb{opacity:.75}
 .sdr.b5 .tkq{opacity:.5}
 .srlab{position:absolute;top:-13px;left:0;font-size:11px;color:var(--tx3);letter-spacing:.5px;
        pointer-events:none}
 .legend{display:flex;flex-wrap:wrap;gap:18px;font-size:11px;margin:22px 0 2px}
 .lg-ok{color:var(--ok)} .lg-open{color:var(--open)} .lg-clip{color:var(--clip)} .lg-zoom{color:var(--tx2)}
 .cal{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;align-items:stretch;margin:12px 0 6px}
 .cwd{font-size:10px;color:var(--tx3);letter-spacing:.6px;text-align:center;min-height:16px;
      background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:2px 0}
 .cblank{visibility:hidden;min-height:52px}
 .cday{position:relative;min-height:52px;background:var(--panel);border:1px solid var(--line);
       border-radius:6px;padding:4px 6px 3px;font-size:11px}
 .cday:hover{background:var(--panel2);border-color:var(--line2)}
 .cday a{display:block;position:relative;width:100%;color:var(--tx);font-size:12px;font-weight:600;
         text-decoration:none}
 .cday a:hover{color:var(--acc);text-decoration:none}
 .cbar{position:relative;height:15px;margin:2px 0 0;background:var(--panel2);border-radius:2px}
 .cmin{position:absolute;top:2px;bottom:2px;background:var(--ok2);border-radius:1px}
 .copen{position:absolute;top:2px;bottom:2px;background:var(--panel2);border:1px dashed var(--open)}
 .cclip{position:absolute;top:10px;bottom:0;background:var(--clip);border-radius:1px}
 .cnote{font-size:9px;color:var(--tx3);margin-top:1px}

"""

# ── SELECTABLE LOOK  ──
# The FX layer is ONE CSS text cut into named blocks by `@@name` marker lines. page()
# pastes back ONLY the enabled blocks, right before </style>, so they still win over the
# base design system exactly as before. Nothing here touches HTML, class names or MAPJS.
FX_ALL = """
@@logo
 /* wordmark: "IPC360" in caps, BIGGER than " clips" (which stays lowercase) */
 .logo{font-size:14px}
 .logo b{text-transform:uppercase;font-size:23px;line-height:1.1;letter-spacing:.6px;
         text-shadow:0 0 12px rgba(117,181,221,.45)}

@@aurora
 /* the backdrop: a slow aurora haze that drifts */
 body::before{content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
   background:radial-gradient(900px 520px at 10% -10%,rgba(117,181,221,.15),transparent 62%),
              radial-gradient(700px 460px at 95% 10%,rgba(232,166,42,.09),transparent 60%),
              radial-gradient(1200px 700px at 50% 112%,rgba(76,163,102,.09),transparent 66%);
   animation:fxdrift 54s ease-in-out infinite alternate}
 @keyframes fxdrift{0%{transform:translate3d(0,0,0) scale(1)}
                    100%{transform:translate3d(-3%,2%,0) scale(1.09)}}
@@scan
 /* the scanline veil over the whole page */
 body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
   background:repeating-linear-gradient(0deg,rgba(150,180,210,.030) 0 1px,transparent 1px 3px)}

@@glass
 /* frosted glass panels + LIVE bar */
 .panel,.card,.dlist,#pullnote{background:linear-gradient(165deg,rgba(28,35,43,.86),rgba(21,26,32,.72));
   backdrop-filter:blur(9px) saturate(1.3);-webkit-backdrop-filter:blur(9px) saturate(1.3);
   border:1px solid var(--line2);
   box-shadow:inset 0 1px 0 rgba(160,190,220,.10),0 8px 20px rgba(0,0,0,.55)}
 .st{background:rgba(21,26,32,.80);backdrop-filter:blur(14px) saturate(1.35);
     -webkit-backdrop-filter:blur(14px) saturate(1.35);
     border-bottom:1px solid rgba(60,74,88,.9);box-shadow:0 8px 18px rgba(0,0,0,.5)}

@@chrome
 /* metal-gradient titles; the inline k2/k spans keep their own flat colour */
 h1{background:linear-gradient(178deg,#f4f8fd,#b9d0e2 46%,#72899c);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
    text-shadow:0 0 12px rgba(117,181,221,.35)}
 h1 .k,h1 .k2{-webkit-text-fill-color:var(--tx3);text-shadow:none}
 .logo{text-shadow:0 0 10px rgba(117,181,221,.45)}

@@glow
 /* rim light inside chips/buttons + a bloom on hover */
 .chip{background:linear-gradient(180deg,rgba(28,35,43,.95),rgba(20,25,31,.9));
       box-shadow:inset 0 1px 1px rgba(117,181,221,.14),0 2px 6px rgba(0,0,0,.45)}
 .chip:hover{border-color:var(--acc);box-shadow:0 0 14px rgba(117,181,221,.35)}
 .btn{background:linear-gradient(180deg,rgba(34,42,50,.95),rgba(22,27,33,.95));
      box-shadow:inset 0 1px 1px rgba(160,190,220,.12),0 2px 5px rgba(0,0,0,.4);
      transition:background .15s ease,color .15s ease,border-color .15s ease,box-shadow .15s ease}
 .btn:hover{background:linear-gradient(180deg,#a8d4f2,var(--acc));
      box-shadow:0 0 16px rgba(117,181,221,.55),inset 0 1px 1px rgba(255,255,255,.35);text-shadow:none}
 .help:hover{box-shadow:0 0 14px rgba(117,181,221,.5)}

@@dims
 /* the calendar as a real 3D plane: date cells lift toward the cursor */
 .cal{position:relative;perspective:820px;perspective-origin:50% 165%}
 .cday{transform:translateZ(0);
       box-shadow:0 2px 5px rgba(0,0,0,.5);
       transition:transform .18s cubic-bezier(.3,1.4,.4,1),box-shadow .18s ease,
                   background .18s ease,border-color .18s ease}
 .cday:hover{transform:translateZ(30px) rotateX(11deg) scale(1.06);
       background:linear-gradient(160deg,var(--panel2),var(--panel));
       border-color:var(--acc);
       box-shadow:0 14px 24px rgba(0,0,0,.55),0 0 16px rgba(117,181,221,.40)}
@@sheen
 /* a light that sweeps across the calendar */
 .cal::after{content:"";position:absolute;inset:-6px;z-index:1;pointer-events:none;
       background:linear-gradient(112deg,transparent 42%,rgba(117,181,221,.13) 50%,transparent 58%);
       background-size:260% 100%;mix-blend-mode:screen;animation:fxsheen 14s linear infinite}
 @keyframes fxsheen{0%{background-position:-40% 0}100%{background-position:160% 0}}

@@glow
 /* glow on the day-map strip, the ruler, the video; stills tilt on hover */
 .cell:hover{box-shadow:0 0 10px rgba(76,163,102,.6)}
 .clip{box-shadow:0 0 9px rgba(232,166,42,.55)}
 .clip:hover{box-shadow:0 0 16px rgba(255,200,106,.8)}
 .tmark{box-shadow:0 0 10px rgba(232,166,42,.9)}
 .th:hover{transform:perspective(520px) rotateY(-7deg) scale(1.05)}
 video{box-shadow:0 0 30px rgba(0,0,0,.7),0 0 2px rgba(117,181,221,.35)}

@@always
 /* whatever is ON, a viewer who asked for no motion still gets none */
 @media (prefers-reduced-motion:reduce){
   body::before{animation:none} .cal::after{animation:none}
   .cday{transition:none;transform:none} .cday:hover{transform:none}
 }
"""


def fx_split(alltext: str) -> dict:
    """`@@name` marker lines cut FX_ALL into {name: css}. A name may appear twice (glow does)
    and its pieces stay in FILE ORDER, so pasting the blocks back keeps the cascade order the
    base design system was written against."""
    out, cur = {}, None
    for line in alltext.splitlines():
        s = line.strip()
        if s.startswith("@@"):
            cur = s[2:].strip()
            continue
        if cur is None:
            continue
        out[cur] = out[cur] + "\n" + line if cur in out else line
    return {k: v.strip() for k, v in out.items()}


FX_BLOCKS = fx_split(FX_ALL)
FX_ORDER = ("logo", "chrome", "glass", "glow", "dims", "aurora", "scan", "sheen")
FX_LABEL = {"logo": "big caps wordmark",
            "chrome": "metal titles",
            "glass": "frosted glass panels",
            "glow": "neon edges + glow",
            "dims": "3D calendar (dates lift toward the cursor)",
            "aurora": "slow coloured haze behind the page",
            "scan": "scanline texture",
            "sheen": "light sweeping the calendar"}
FX_MOTION = ("aurora", "scan", "sheen", "dims")     # the ones that move
FX_PRESETS = {"off": (), "calm": ("logo", "chrome", "glass", "glow"), "full": FX_ORDER}
LOOK_BACK = {"/", "/month", "/day", "/clip", "/help", "/status", "/status.json",
             "/api/daymap", "/look"}      # /look may 303 back to these ONLY - never /pull, /grab,
                                          # /delete, /sweep, /refresh (a look change must not re-pull)
LOOK_NAMES = {"/": "home \u00b7 this month", "/month": "this month", "/day": "that day",
              "/clip": "that clip", "/help": "how it works", "/status": "what it is doing",
              "/look": "here"}          # what a `go back to X` chip on /look calls each page


def fx_names(spec: str = "", cur=None) -> list:
    """Resolve a user's `fx=` spec against what is ON now. Accepted forms:
    off | calm | full (presets) · a comma list of block names (absolute) ·
    +name / -name (toggles) · a bare name = turn it on."""
    cur = set(cur if cur is not None else fx_pref())
    s = (spec or "").strip().lower()
    if not s:
        return [b for b in FX_ORDER if b in cur]
    if s in FX_PRESETS:
        return [b for b in FX_ORDER if b in FX_PRESETS[s]]
    for tok in re.split(r"[\s,]+", s):
        if not tok:
            continue
        if tok in FX_ORDER:                        # absolute list wins over toggles
            cur = set(t for t in re.split(r"[\s,]+", s) if t in FX_ORDER)
            break
        name, sign = (tok[1:], tok[0]) if tok[0] in "+-" else (tok, "+")
        if name in FX_ORDER:
            cur.add(name) if sign == "+" else cur.discard(name)
    return [b for b in FX_ORDER if b in cur]


def fx_css(names) -> str:
    """The ENABLED blocks in file order, then the always-on reduced-motion tail."""
    on = set(names)
    parts = [FX_BLOCKS[b] for b in FX_ORDER if b in on and b in FX_BLOCKS]
    parts += [FX_BLOCKS["always"]] if "always" in FX_BLOCKS else []
    return "\n".join(parts)


STATUSJS = """<script>
(function(){
  var box = document.getElementById("st");
  if(!box) return;
  var busy = 0;
  function tick(){
    var x = new XMLHttpRequest();
    x.open("GET", "/statusfrag?t=" + (busy++));
    x.onload  = function(){ box.innerHTML = x.responseText; window.setTimeout(tick, 3000); };
    x.onerror = function(){ window.setTimeout(tick, 5000); };
    x.send();
  }
  tick();
})();
</script>"""


PAGE_TMPL = """<!doctype html><html><meta charset="utf-8"><title>%s \u2014 ipc360 clips</title>
%s
<div class="st" id="st">%s</div>
<div class="wrap">
%s
</div>
%s"""


PULLJS = """<script>
(function(){
  var note = document.getElementById("pullnote");
  var name = "%s";
  var tries = %d;                                  /* ~2x the ETA, in 3 s polls */
  function fail(){
    if (note) note.innerHTML = 'PULL FOR ' + name + ' ENDED WITH NO CLIP - <a href="/status">live log</a> or click the minute again';
  }
  function again(){
    if (tries <= 0){ fail(); return; }
    tries = tries - 1;
    window.setTimeout(wait, 3000);
  }
  function wait(){
    var s = new XMLHttpRequest();
    s.open("GET", "/status.json?t=" + tries);
    s.onload = function(){
      var running = true;                          /* if /status.json is unreadable, assume running */
      try { running = (JSON.parse(s.responseText).running === true); } catch (e) { running = true; }
      if (running){ again(); return; }             /* still pulling \u2014 the top bar already counts down */
      var x = new XMLHttpRequest();
      x.open("GET", "/" + name + ".json");         /* 200 = the clip landed */
      x.onload = function(){
        if (x.status != 200){ again(); return; }
        var m = null;
        try { m = JSON.parse(x.responseText); } catch (e) { m = null; }
        if (m && !m.grab_failed && (m.bytes || 0) > 0){ window.location.href = "/clip?name=" + name; return; }
        fail();                                    /* the pull ran and produced nothing playable */
      };
      x.onerror = again;
      x.send();
    };
    s.onerror = again;
    s.send();
  }
  wait();
})();
</script>"""



def page(title: str, body: str, nav=None) -> str:
    """Every page = the design system + ONLY the FX blocks the viewer switched on, then the
    LIVE bar (STATUSJS keeps it current), then the body inside .wrap, then the scripts:
    status poll, clip ruler/slider, day-map wheel zoom. The look is read from prefs at render
    time, so changing it on /look changes every page from then on. `nav` is that page's header
    chips; a renderer that forgot one still gets default_nav(), so NO page can exist without a
    way home (asked three times in a row on 2026-11-01: the delete result, /help, a 404)."""
    if 'class="hdr"' not in body:
        body = page_hdr(nav if nav is not None else default_nav()) + body
    css = CSS + "\n" + fx_css(fx_pref()) + "\n</style>"
    return (PAGE_TMPL % (esc(title), css, status_frag(), body, STATUSJS + SCRUBJS + MAPJS))


def fx_switch(b: str, on: bool, back_enc: str) -> str:
    """One effect's SLIDE SWITCH (2026-11-01, asked for: "make the off and on buttons show which
    are selected or make a slide toggle so you can see which way it is toggled"). The knob sits
    over the word the effect is on, the whole pill is one click (it flips the effect), and the
    hrefs are the same +name / -name fx= URLs as everywhere else, so a look can still be shared."""
    act = ("-%s" % b) if on else ("+%s" % b)
    return ('<a class="sw %s" href="/look?fx=%s&to=%s" title="click: switch %s %s">'
            '<span class="swl swlo">off</span><span class="swl swlon">on</span><i></i></a>'
            % ("sw-on" if on else "sw-off", urllib.parse.quote(act, safe=""), back_enc,
               "off" if on else "on", esc(FX_LABEL[b])))


def look_chip(to: str) -> str:
    """The nav chip that opens the LOOK MENU and comes back to THIS page after a choice."""
    return ('<a class="btn" href="/look?to=%s" title="pick the look: no effects, calm, full, '
            'or switch every effect on and off \u00b7 remembered for every page">change look</a>'
            % urllib.parse.quote(to, safe=""))


COFFEE = "https://buymeacoffee.com/neonpumpkin"   # the author's tip jar (repo: .github/FUNDING.yml)
PROFILE = "https://github.com/Neon-Pumpkin"       # their GitHub profile (the handle HAS a hyphen)

MARK = "data:image/jpeg;base64," + (
    "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"
)   # a 120 px JPEG of the author's mark, inlined (no route, no path)


def sponsor_chip() -> str:
    """The tip-jar chip in the nav row of EVERY page, labelled just `support` (a long chip name eats
    the nav row): the LAN page should be as attributable as the repo page. It links OUT of the LAN,
    so it is inert to the gallery (asks the camera nothing, starts nothing)."""
    return ('<a class="btn" href="%s" rel="noopener nofollow" title="buymeacoffee \u2014 if this saved '
            'you an afternoon">support \u2615</a>' % COFFEE)


def page_hdr(nav) -> str:
    """ONE header for EVERY page (2026-11-01: /help and /status had none at all, and the viewer
    could not get out). The row is the wordmark (links home), then the byline **by Neon-Pumpkin** with
    a thumbnail of their mark (links to their GitHub profile), then that page's nav chips - asked for:
    "after the title/hero ipc360 clips, followed by 'by Neon Pumpkin' with a thumbnail of the graphic,
    and a buymeacoffee link under the word support". The mark is a 120 px JPEG inlined as a data URI
    (no route, no file beside the code, no extra request), so a copy of the gallery anywhere still
    says who wrote it."""
    if isinstance(nav, list):
        nav = "".join(nav)
    return ('<div class="hdr">'
            '<a class="logo" href="/"><b>ipc360</b> clips</a>'
            '<span class="by">by <a href="%s" rel="me noopener" title="GitHub">%s'
            '</a><img class="mark" src="%s" width="26" height="26" alt="the Neon-Pumpkin mark \u2014 '
            'the same one on the GitHub profile, the repo and the tip jar"/></span>'
            '<div class="nav">%s</div></div>'
            % (PROFILE, "Neon-Pumpkin", MARK, nav))



def day_chip(day: str) -> str:
    """The `that day` nav chip — nothing to link to when the page has no day, so an empty chip."""
    return ('<a class="btn" href="/day?day=%s">that day</a>' % esc(day)) if day else ""


def day_for(name: str) -> str:
    """The day a clip belongs to, for the nav chips of the /delete pages: those URLs carry ONLY
    the name (/delete?name=X from a card or a day-map row). The sidecar answers first (it says
    `day`), then a name that opens with a date (a CLI import, the app's own clip). "" when neither
    knows — day_chip() then just omits the chip instead of linking /day?day= (empty = today, WRONG)."""
    d = ""
    try:
        d = json.loads((SHARE / (name + ".json")).read_text()).get("day") or ""
    except Exception:
        pass
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
        d = m.group(1) if m else ""
    return d


def default_nav() -> str:
    """The header a page gets when its renderer did not build one (page()'s safety net): home,
    the help page, the tip jar."""
    return page_hdr(['<a class="btn" href="/">home \u2014 this month</a>',
                     '<a class="btn" href="/help">how it works</a>',
                     sponsor_chip()])


def render_look(to: str = "") -> str:
    """THE LOOK MENU . Every effect = its own on/off switch
    plus three presets (off / calm / full); a choice is saved in prefs.json and applies to every
    page from then on, so this page always shows what is currently on. The page carries its own
    nav row (verified)."""
    back = to if to in LOOK_BACK else "/look"
    enc = urllib.parse.quote(back, safe="")
    cur = set(fx_pref())
    rows = []
    for b in FX_ORDER:
        on = b in cur
        mot = " \u00b7 it moves" if b in FX_MOTION else ""
        rows.append('<div class="drow">%s<b class="dname">%s</b>'
                    '<span class="k">%s%s</span></div>'
                    % (fx_switch(b, on, enc), b, esc(FX_LABEL[b]), mot))
    presets = []
    for p in ("off", "calm", "full"):
        name = ("no effects at all" if p == "off" else
                ("calm \u2014 nothing moves" if p == "calm" else "full \u2014 everything"))
        if set(FX_PRESETS[p]) == cur:
            name = "%s \u00b7 this one" % name
        presets.append('<a class="btn" href="/look?fx=%s&to=%s">%s</a>' % (p, enc, name))
    onnow = "".join('<span class="chip">%s</span>' % esc(FX_LABEL[b]) for b in FX_ORDER if b in cur)
    nav = ['<a class="btn" href="/" title="the month calendar">home \u2014 this month</a>']
    if back != "/look":
        lbl = LOOK_NAMES.get(urlparse(back).path)
        nav.append('<a class="btn" href="%s" title="the look is saved for every page, so leaving '
                   'here changes how that page draws">go back to %s</a>' % (enc, lbl) if lbl else
                   '<a class="btn" href="%s">go back</a>' % enc)
    nav.append('<a class="btn" href="/help">how it works</a>')
    nav.append(sponsor_chip())
    body = [page_hdr(nav),
            '<h1>the look \u2014 switch any effect on or off</h1>',
            '<div class="dlist">%s</div>' % "".join(rows),
            '<div class="chips">%s</div>' % "".join(presets),
            '<div class="panel"><h2>Now on</h2><div class="k2">%s</div></div>'
            % (onnow if onnow else "nothing \u2014 the plain gallery"),
            '<div class="panel"><h2>Link a look straight</h2><div class="k">Every page takes '
            '<code>?fx=</code>, so a look can be shared as a URL without visiting this menu: '
            '<code>/day?day=2026-10-30&fx=off</code> \u00b7 <code>?fx=+aurora</code> adds one effect '
            'to what is on now \u00b7 <code>?fx=-scan</code> removes one \u00b7 '
            '<code>?fx=glass,glow</code> is an exact list \u00b7 <code>?fx=off</code> / '
            '<code>?fx=calm</code> / <code>?fx=full</code> are the presets. The choice is remembered '
            'for EVERY page (one file on the share, <code>prefs.json</code>, like the pm-hours '
            'setting) \u2014 reload the page you are on and it is already applied. With <b>off</b> you '
            'get exactly the plain gallery: no haze, no glass, no 3D, same words, same buttons, same '
            'day map. Whatever is on, a browser that asks for no motion (<code>prefers-reduced-motion</code>) '
            'still never sees the moving parts.</div></div>']
    return page("the look \u2014 pick your effects", "".join(body))


def clip_files(name: str) -> list:
    """Everything one clip owns on the share: .mp4 .jpg(poster) .json(sidecar) + its 9 stills
    + (if the page pulled it) its work dir. Nothing else — name must be a plain file name."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not (SHARE / (name + ".mp4")).exists():
        return []
    out = [SHARE / (n + ext) for ext in (".mp4", ".jpg", ".json")
           for n in (name,) if (SHARE / (n + ext)).exists()]
    out += sorted(SHARE.glob(name + "-th*.jpg"))
    work = SHARE / ("grab-" + name)
    if work.is_dir():
        out.append(work)
    return out


def delete_clip(name: str) -> tuple:
    """Remove a clip from the gallery (two-step from the page: /delete?name=… asks, /delete?…&confirm=1
    does it). The pulled minute can be re-pulled at any time from the card; if the clip came from a
    CLI run its seg.* files stay under probes, so 'delete' is a gallery action, not
    a data loss."""
    files = clip_files(name)
    gone, bytes_ = 0, 0
    for f in files:
        try:
            if f.is_dir():
                shutil.rmtree(f)
                gone += 1
                continue
            bytes_ += f.stat().st_size
            f.unlink()
            gone += 1
        except Exception as e:
            print("camclips: delete %s: %s" % (f, e))
    return gone, bytes_

def status_frag() -> str:
    """HTML fragment for the sticky LIVE bar (top of every page). STATUSJS re-fetches
    /statusfrag every 3 s and swaps this bar's innerHTML in place, so the bar counts a pull
    down without the viewer reloading, and there is no separate status page to keep open."""
    lock = SHARE / "grab.lock"
    running = lock.exists() and time.time() - lock.stat().st_mtime <= LOCK_TIMEOUT
    try:
        sw = json.loads(SWEEP.read_text())
    except Exception:
        sw = None
    out = []
    if running and sw:
        el = int(time.time() - (sw.get("started") or time.time()))
        dn, tot = int(sw.get("done") or 0), int(sw.get("total") or 0)
        eta = int(el / max(1, dn) * max(0, tot - dn)) if dn else 0
        out.append('<span class="stlive"><i class="live"></i> CHECKING THE CARD %s &nbsp; %d/%d dates '
                   '&nbsp; %d s in &nbsp; ~%d s left &nbsp; <a href="/status">log</a></span>'
                   % (esc(sw.get("month") or "?"), dn, tot, el, eta))
    elif running:
        txt = "PULL RUNNING"
        try:
            r = json.loads(RUNNING.read_text())
            passes = max(1, int(r.get("passes") or 1))
            t0 = float(r.get("started") or time.time())
            el = int(time.time() - t0)
            # MEASURED countdown, not the fixed PASS_S ETA: every finished pass drops its own
            # pull-*.wire inside grab-<clip>/, so their mtimes say how fast the passes really run
            # (measured 48-49 s per pass per minute of SD, 2026-10-31). A pull also spends time
            # BEFORE pass 1 - login, the mgr queries, the --idle waits - which is why the bar counts
            # PASSES and says "setting up" until the first wire lands.
            # ONLY wires newer than THIS pull's start count: grab-<name>/ is reused when a minute
            # is re-pulled, and the previous run's p1..p5 made a fresh pull read "pass 5/5 done -
            # ~0 s left" two seconds after it started (measured 2026-11-01, 21:28-21:29).
            gd = SHARE / ("grab-" + clip_name(r.get("day") or "", r.get("range") or ""))
            wires = sorted([f for f in gd.glob("pull-*.wire")
                            if f.stat().st_mtime >= t0 - 2], key=lambda f: f.stat().st_mtime)
            ws = [f.stat().st_mtime for f in wires]
            done = len(ws)
            # Cost scales with the MINUTES of SD in the range: one pass re-streams the whole range,
            # so 15:44-15:46 (2 min of SD) really is ~93 s per pass = the same 46-49 s per MINUTE
            # measured 2026-10-31. Say the minutes out loud or a healthy 2-minute pull looks stuck.
            mins = max(1, mins_of("", r.get("range") or "")[1] - mins_of("", r.get("range") or "")[0])
            rate = PASS_S * mins
            if done >= 2:
                rate = (ws[-1] - ws[0]) / (done - 1)
            txt = "PULL RUNNING %s %s \u00b7 %d min of SD \u00b7 pass %d/%d done \u00b7 %d s in" % (
                r.get("day") or "?", r.get("range") or "?", mins, min(done, passes), passes, el)
            if done >= 2:
                txt += " \u00b7 last pass %d s \u00b7 avg %d s" % (int(ws[-1] - ws[-2]), int(rate))
            if done and done < passes:
                # HONEST countdown : `avg x passes_left` sat at one number for a
                # whole pass, which read like a stalled clock. Credit the pass IN FLIGHT, so the estimate
                # falls on every poll: (passes-1-done) untouched passes + what is LEFT of this one.
                infl = time.time() - ws[-1]
                left = rate * (passes - done - 1) + max(0.0, rate - infl)
                txt += " \u00b7 pass %d in flight %d s \u00b7 ~%d s left" % (done + 1, int(infl), int(left))
            elif done >= passes:
                txt += " \u00b7 all passes in, muxing + publishing"
            else:
                txt += " \u00b7 setting up / pass 1 streaming (setup + idle waits come first)"
        except Exception:
            pass
        out.append('<span class="stlive"><i class="live"></i> LIVE &nbsp; %s &nbsp; '
                   '<a href="/status">log</a></span>' % esc(txt))
    else:
        out.append('<span class="stidle"><i class="live"></i> IDLE &nbsp; a green day-map minute = '
                   'pull it &nbsp; <a href="/help">?</a></span>')
    newest = sorted(SHARE.glob("*.json"), key=lambda f: f.stat().st_mtime)
    newest = [f for f in newest if not f.name.startswith(("daymap-", "grab-", "sweep-"))
              and f.name != "prefs.json"]      # the clock preference is not a clip
    if newest:
        try:
            m = json.loads(newest[-1].read_text())
            out.append('<span class="st2">newest clip: %s \u00b7 landed %s \u00b7 %.0f s \u00b7 %s</span>'
                       % (m.get("name") or newest[-1].stem,
                          atlabel(m.get("at") or 0),
                          m.get("dur_video") or 0, mb(m.get("bytes") or 0)))
        except Exception:
            pass
    return " &nbsp; ".join(out)



def render_home(month: str | None = None) -> str:
    """Home = a CALENDAR for ONE month at a time (default: the current month; « previous month »
    walks back). Every REAL date of that month is a cell — nothing is discovered, typed, or added
    by hand, and a date can never be missing from the calendar because the calendar is built from
    the dates, not from what the camera said. Clicking a date opens that day's page, WHICH READS
    THE CARD for that date and draws its timeline. A cell's 24-hour hairline bar shows what the
    card holds for that date from its CACHED day map only (green = minutes on the card, dashed =
    minute still open on the card, amber = minutes already pulled into this gallery); a date nobody
    checked has NO bar, deliberately different from a date the card says is empty. `check this
    month` asks the card for every date of the month in one walk. A WHOLE cell is the link, not just
    the number, and a date that is closed with a complete cached map opens WITHOUT touching the
    camera (see card_complete). The months chips at the bottom walk back 6 months from today. Long
    prose lives on /help."""
    m = (month or datetime.date.today().strftime("%Y-%m"))
    if not month_ok(m):
        m = datetime.date.today().strftime("%Y-%m")
    this_m = datetime.date.today().strftime("%Y-%m")
    dm = daymaps_on_disk()
    clips = clips_on_disk()
    by_day = {}
    for name, meta in clips:
        by_day.setdefault(meta.get("day") or "", []).append((name, meta))
    cardm = sum(sum(r[1] - r[0] + 1 for r in d["ranges"])
                for d in dm.values() if d.get("ranges"))
    checked = len([d for d in dm.values() if d.get("ranges")])
    months = sorted({k[:7] for k in list(dm) + list(by_day) if k[:7]}, reverse=True)
    # the months row: the last 6 months back from today, PLUS any month that has data, so a month
    # nobody visited is still reachable without typing a URL.
    show = [month_step(this_m, -k) for k in range(6)]
    show += [mo for mo in months if mo not in show]
    recent = sorted([c for c in clips if c[1].get("at")], key=lambda c: c[1]["at"], reverse=True)
    last = ("%s \u2014 landed %s, %.0f s, %s"
            % (recent[0][0], atlabel(recent[0][1]["at"]),
               recent[0][1].get("dur_video") or 0, mb(recent[0][1].get("bytes") or 0))
            if recent else "nothing yet")
    nav = ['<a class="btn" href="/month?m=%s">\u2039 previous month</a>' % esc(month_step(m, -1))]
    if m < this_m:
        nav.append('<a class="btn" href="/month?m=%s">next month \u203a</a>' % esc(month_step(m, 1)))
    nav.append(look_chip("/month?m=%s" % m))
    nav.append('<a class="btn" href="/help">how it works</a>')
    nav.append(sponsor_chip())
    # NO side-effect chip in the nav row . Nav = navigation + help + the LOOK menu ONLY -
    # `change look` asks the camera for nothing and starts nothing; the sweep chip moves
    # below the calendar, into the stats row, and says what it does and how big it is.
    ndates = len(month_dates(m))
    sw = sweep_running()
    sweep_chip = ("" if (sw or not ndates) else
                  '<a class="btn" href="/sweep?m=%s" title="asks the card for every date of %s, ~8 s each '
                  '\u00b7 nothing can be pulled while it walks">check this month (%d card %s)</a>'
                  % (esc(m), esc(m), ndates, "query" if ndates == 1 else "queries"))
    rows = []
    for dd in sorted(by_day, reverse=True):
        d = dm.get(dd)
        cm = (sum(r[1] - r[0] + 1 for r in d["ranges"])
              if d and d.get("ranges") else None)
        rows.append('<div class="drow"><a href="/day?day=%s"><b>%s</b></a>'
                    '<span class="k2">%s \u00b7 %s min on card</span></div>'
                    % (esc(dd), esc(dd), plural(len(by_day[dd])),
                       ("%d" % cm) if cm is not None else "?"))
    body = [page_hdr(nav),
            sweep_note(),
            '<h1>%s <span class="k2">the camera \u00b7 SD recordings</span></h1>' % esc(m),
            calendar_html(m, dm, by_day),
            '<div class="legend"><span class="lg-ok">green = card holds those minutes</span>'
            '<span class="lg-open">dashed = recording still open on the card</span>'
            '<span class="lg-clip">amber = already in this gallery</span>'
            '<span class="k">no bar = that date was never checked</span></div>',
            '<div class="chips"><span class="chip">%s</span>'
            '<span class="chip">%d dates checked</span>'
            '<span class="chip">~%d min on card</span>'
            '<span class="chip">newest clip: %s</span>%s</div>'
            % (plural(len(clips)), checked, cardm, esc(last), sweep_chip),
            '<div class="dlist">%s</div>' % "".join(rows) if rows else
            '<div class="panel"><div class="k2">nothing pulled yet \u00b7 click a date, then a green '
            'minute to pull it <a class="help" href="/help">?</a></div></div>',
            '<div class="chips">%s</div>'
            % "".join('<span class="chip"><a href="/month?m=%s">%s</a></span>'
                      % (esc(mo), esc(mo)) for mo in show)]
    return page("ipc360 clips", "".join(body))


def render_month(month: str) -> str:
    """Any month of the calendar (YYYY-MM), not only months that happen to have data."""
    return render_home(month if month_ok(month) else None)



def render_help() -> str:
    """The long text, one page: the gallery pages stay visual. Everything the buttons and the
    day map do, what the colours mean, and what a pull costs."""
    body = [page_hdr(['<a class="btn" href="/">home \u2014 this month</a>',
                     look_chip("/help"), sponsor_chip()]),
            '<h1>how this gallery works</h1>',
            '<div class="panel"><h2>Getting around</h2>'
            '<div class="k">The wordmark <b>ipc360 clips</b> at the top-left IS the home button - it is '
            'on every page, so you never need the browser\'s back button. Each page also carries buttons: '
            '<b>home</b>, <b>that month</b>, <b>&lt;&lt; previous month</b>, <b>next month &gt;&gt;</b> and '
            '<b>how it works</b> - the top row is NAVIGATION AND HELP ONLY, nothing in it asks the camera. '
            'The action that does walk the card, <b>check this month</b>, sits <b>below the calendar</b> in the '
            'stats row and says how many card queries it will make, because it is easy to hit by accident from the '
            'navigation row (2026-11-01: on the current month there is no "next month" chip, so an action chip '
            'there lands exactly where the eye expects a navigation chip). It is hidden while a sweep already walks. '
            'The DAY page adds <b>raw day map</b>, <b>check this day</b> (one card query) and <b>pm hours</b> '
            '⇄ <b>24 h</b> (saved for every day).</div>'
            '<div class="k"><b>The home page is a calendar, one month at a time</b> - this month by '
            'default, <b>&lt;&lt; previous month</b> walks back and <b>next month &gt;&gt;</b> walks back '
            'forward. Every REAL date of that month is a cell, so a date can never be "missing": the '
            'calendar is built from the dates, not from what the camera happened to say. <b>The whole '
            'cell is the link</b>, not just the number, and clicking a date opens that day\'s page with '
            'its minute-by-minute timeline. A date that is OVER, whose card read already covered the whole '
            'day, opens from that cached map with <b>no camera work at all</b> - a closed day cannot gain '
            'minutes. Today, a date never opened, or one whose read happened while the day was still on the '
            'card, asks the card again. When a page served the cache it also offers <b>check this day</b> to '
            'force one re-read - the rare case where the card recycled an old day - and that is why there is '
            'no re-read button to hunt for. The <b>months</b> chips at the bottom walk back 6 months from '
            'today; <b>&lt;&lt; previous month</b> walks further.</div>'
            '<div class="k">A cell\'s own <b>24-hour hairline bar</b> is drawn from that date\'s CACHED card '
            'read only - <b>the home page never asks the camera anything</b>. Green = the card holds those '
            'minutes, dashed = a minute the camera is still writing, amber = minutes already pulled into '
            'this gallery. A date nobody opened has <b>no bar</b>, which is deliberately NOT the same as a '
            'date the card says is empty (that one says <b>card had nothing</b>). <b>check this month</b> '
            'asks the card for every date of the month in one walk - it holds the pull lock, so nothing can '
            'pull while it runs, and the top bar counts it. One query per date, a few seconds each, so a '
            'whole month is a couple of minutes.</div></div>',
            '<div class="panel"><h2>The day map: two rows, wheel zoom</h2>'
            '<div class="k">The day map is what the camera\'s SD card holds for that day, minute by '
            'minute, as reported by the manager (<code>ipc360.py --list</code>, cached as '
            '<code>daymap-&lt;day&gt;.json</code> on the share). It is drawn as <b>two full-width '
            'rows</b>: 00:00-11:59 on top, 12:00-23:59 below. A single 24 h strip puts a 1-minute '
            'cell under 1 pixel, so this is both wider and never cuts noon in half.</div>'
            '<div class="k"><b>Put the mouse wheel over a row and it zooms THAT row</b> (the row '
            'under your cursor, like a map). <b>Double-click</b> jumps to a 30-minute window centred '
            'on the cursor. One-minute minutes are widened to a minimum clickable width, so if you '
            'can see it you can click it. Hours get hairlines + labels always; 15-minute ticks appear '
            'under 5 h of window, 5-minute ticks under 2 h. Zoom in and a small label appears above '
            'that row naming the window currently drawn (e.g. 12:25-13:05) - at full row width there is '
            'no label, the hour labels under the bar already say it. The afternoon row can be relabelled '
            '<b>12 PM, 1 PM … 11 PM</b> by the <b>pm hours</b> chip on the day page '
            '(<code>/day?day=X&clock=pm</code>), which is easier to read when the camera burns a 12-hour '
            'clock into the picture; tap <b>24 h</b> to go back. <b>That choice is remembered for EVERY '
            'day</b> (one file on the share, <code>prefs.json</code>) - set it once, every day page opens '
            'that way. Caveat: while the cursor sits on a day-map '
            'row the wheel zooms instead of scrolling the page - move off the strip to scroll.</div>'
            '<div class="k">Colours: <b style="color:var(--ok)">green</b> = the card holds those '
            'minutes, clicking pulls them; <b style="color:var(--open)">dashed blue</b> = day-map flag '
            '0x80, that minute is still being written on the card (grab it after it closes); '
            '<b style="color:var(--clip)">amber</b> = a clip already in this gallery, clicking opens it. '
            'A cell\'s tooltip gives exact minutes, minutes-count, and the day-map flag. Green cells '
            'wider than 3 minutes are shown but not clickable - that would be a huge pull.</div></div>',
            '<div class="panel"><h2>Clicking a minute = pull it</h2>'
            '<div class="k">Clicking a green cell opens <code>/pull?day=&amp;range=&amp;passes=5</code>: '
            'that day\'s page with a <code>PULLING \u2026</code> note. You are NOT sent to a '
            '"grab started" screen you have to come back from - the sticky LIVE bar counts the pull '
            'down every 3 s, and the page walks itself to the clip page (which autoplays) the moment '
            'the clip lands. Nothing can play BEFORE its minute lands: a minute that is not on the '
            'gallery does not exist yet.</div>'
            '<div class="k"><b>Cost, measured:</b> once a pass is streaming it covers one minute of SD in '
            '<b>~48-49 s of wall-clock</b> (measured 2026-10-31 on a 1-minute range; the script keeps the '
            'older <code>PASS_S</code> = 57 s constant only as the fallback estimate). BEFORE pass 1 there is '
            'a setup phase - login, the manager queries, the <code>--idle 15</code> waits - so a pull can sit '
            'with no pass finished for many minutes (their 2026-10-31 22:45 pull: ~21 min from start before '
            'pass 1 landed, then 48-49 s per pass, clip published 23:09). That is why the LIVE bar counts '
            '<b>passes</b>, measured from the pull\'s own <code>pull-*.wire</code> files: '
            '<code>2 min of SD · pass 2/5 done · 214 s in · last pass 93 s · avg 93 s · pass 3 in flight 41 s · '
            '~238 s left</code>, and while a pass is running it is counted out loud (<code>pass 3 in flight 41 s</code>) '
            'so the number moves every 3 s. <b>The countdown credits the pass in flight</b> - it is '
            '(passes-1-done) whole passes plus whatever is left of THIS one - so it falls every poll instead of '
            'sitting at one number for a whole pass . '
            '<b>The rate is per MINUTE of SD</b> - a pass re-streams the whole range - so 15:44-15:46 (2 min of SD) '
            'really is ~93 s per pass and ~8 min for 5 passes, while a 1-minute pull is ~4-5 min. The old '
            '<code>PASS_S</code> = 57 s constant is only the fallback estimate before any wire lands.</div>'
            '<div class="k"><b>One pull at a time.</b> The manager session AND the camera\'s P2P relay '
            'are shared state, so two pulls at once tear each other down; the page holds '
            '<code>grab.lock</code> while one runs and the CLI refuses a <code>--pull</code>/<code>--play</code> '
            'while that lock is alive. While a pull runs, opening a day page serves the CACHED day map '
            'instead of a fresh read (the mgr session belongs to the pull) - the page says so in its '
            'header. Re-check the day after the pull lands by reloading the day page, or use '
            '<code>/refresh?day=YYYY-MM-DD</code>.</div></div>',
            '<div class="panel"><h2>Playing</h2>'
            '<div class="k">A clip page holds ONE video and it starts playing right away: browsers only '
            'let a script start an untouched video with sound on a page the viewer has already played on, '
            'so the tag starts MUTED and the page unmutes it 600 ms in. That is why you hear the audio '
            'a beat after the picture starts.</div>'
            '<div class="k">Seek it by <b>clicking anywhere on the ruler</b> under the video (the video '
            'jumps there, the amber playhead follows), by <b>dragging the slider</b> below it, or - with '
            'no JS at all - by the <code>#t=</code> jump links and the 9 stills (each still links to '
            'that moment of the file).</div></div>',
            '<div class="panel"><h2>Delete is two steps, and only touches the gallery</h2>'
            '<div class="k"><code>delete</code> first shows the list of files that would go '
            '(.mp4, poster, the 9 stills, the sidecar .json, the pull dir) and asks; '
            'the confirm link removes them. The minute stays ON THE CARD, so you can pull it again at '
            'any time. A clip you imported from a CLI run keeps its <code>seg.*</code> files under '
            '<code>probes</code> - deleting on the page never erases those.</div></div>',
            '<div class="panel"><h2>Reading the card facts</h2>'
            '<div class="k"><b>video N groups (fid a\u2026b = fps)</b> - one group = one minute of H.264 '
            'as the camera recorded it; <code>fid</code> is a CONTINUOUS frame index inside the range, '
            'and the SD\'s own rate varies per minute (10 fps or 20 fps measured), which is why the '
            'frame index is converted with that minute\'s rate and not a fixed 20.</div>'
            '<div class="k"><b>audio = a-law @ 8000 Hz mono</b> - the camera\'s own RTSP SDP line says '
            'pcm_alaw/8000, and 25 packets of 320 B per second = 8000 B/s = 64 kb/s, so the bytes are '
            'AT&T a-law (G.711), NOT linear u8; they are expanded to pcm_s16le with no re-scale. '
            '<b>anchor fid / head s</b> is where the audio is pinned on the video timeline, so the '
            'burned-in clock on the picture and the audio agree to ~2 s. <b>audio holes</b> = datagrams '
            'that never arrived = real silence, kept as silence so the clip stays on an absolute '
            'timeline (concatenating what arrived drifts the sound tens of seconds).</div>'
            '<div class="k"><b>PARTIAL groups / decode errors</b> = minutes whose chunks did not all '
            'land on that pass (thin card minutes, or a relay that dropped packets mid-group). More '
            'passes, or a second run merged offline, usually closes them.</div></div>',
            '<div class="panel"><h2>Thin card / repair recipe</h2>'
            '<div class="k">If a clip on the share came out thin (no facts line, tiny card), the sidecar '
            'was written from an empty stats file - the usual cause is an old call that passed a file '
            'prefix instead of the seg DIRECTORY. Repair it offline from the pull you already have on '
            'the box, without touching the camera:'
            '<pre>cd .\nIPC360_PW=your-camera-password python3 ipc360.py --play 13:00-13:01 --day 2026-10-30 --out probes/p-rerun/seg --passes 5\ncamclips.py --import probes/p-rerun/seg --name 2026-10-30_1300-1301 --day 2026-10-30 --range 13:00-13:01'
            '</pre>'
            'or just re-still an existing file: <pre>camclips.py --thumbs 2026-10-30_1300-1301</pre></div></div>',
            '<div class="panel"><h2>Plain files and CLI</h2>'
            '<div class="k">The share is also a Windows folder: <pre>the same folder, opened over Windows shares</pre> '
            'copy the .mp4s off it if you prefer. On the box the same code is the CLI:'
            '<pre>cd . &amp;&amp; IPC360_PW=your-camera-password python3 ipc360.py --play 13:00-13:01 --day 2026-10-30 --out probes/play-Z/seg --passes 5\ncamclips.py --import probes/play-Z/seg --name NAME --day D --range R</pre></div></div>',
            '<div class="panel"><h2>The look (effects) is selectable</h2>'
            '<div class="k">The <b>change look</b> chip on every page opens <code>/look</code>: '
            'eight effects - <b>big caps wordmark</b>, <b>metal titles</b>, <b>frosted glass '
            'panels</b>, <b>neon edges + glow</b>, <b>3D calendar</b> (date cells lift toward the '
            'cursor), <b>slow coloured haze</b>, <b>scanline texture</b>, <b>light sweeping the '
            'calendar</b> - each with its own on/off switch, plus three presets: '
            '<b>no effects at all</b>, <b>calm</b> (the good-looking ones, nothing moves), '
            '<b>full</b> (everything). The choice is saved in <code>prefs.json</code> and applies '
            'to EVERY page, so reload the page you are on and it is already there. A look can also '
            'be put in a URL: <code>?fx=off</code>, <code>?fx=+aurora</code>, <code>?fx=-scan</code>, '
            '<code>?fx=glass,glow</code>. Switching effects never touches the camera and starts '
            'no pull - it is CSS only, the words, buttons and the day map stay identical.</div></div>',
            '<div class="panel"><h2>Endpoints</h2>'
            '<div class="k2"><code>/</code> days \u00b7 <code>/month?m=YYYY-MM</code> \u00b7 '
            '<code>/day?day=YYYY-MM-DD</code> \u00b7 <code>/clip?name=</code> one video \u00b7 '
            '<code>/clip?day=&amp;range=</code> the clip that covers a minute \u00b7 '
            '<code>/pull</code> start a pull and stay on the day page \u00b7 <code>/grab</code> same, JSON reply '
            '(for scripts) \u00b7 <code>/dl/NAME.mp4</code> save-as \u00b7 <code>/delete?name=X</code> '
            '(+ <code>&amp;confirm=1</code>) \u00b7 <code>/status</code> log tail \u00b7 '
            '<code>/status.json</code> machine-readable \u00b7 <code>/api/daymap?day=</code> raw day map \u00b7 '
            '<code>/refresh?day=</code> re-read the card \u00b7 <code>/sweep?m=YYYY-MM</code> ask the card for '
            'every date of a month \u00b7 <code>/look</code> the effects menu (add <code>?fx=</code> to any page) '
            '\u00b7 <code>/help</code> this page. '
            'Static <code>/*.mp4 *.jpg *.json *.log</code> are served with Range support, so seeking '
            'does not re-fetch the file.</div></div>']
    return page("how the ipc360 gallery works", "".join(body))


def render_day(day: str, pull: tuple = None, check=None, clock: str | None = None) -> str:
    """One day: the two-row SD day map + one compact row per clip of that day.

    `pull=(clip_name, eta_s)` = the /pull route: a pull for THIS page was just started, so the
    user stays here. #pullnote says what is happening, the LIVE bar counts it down, and PULLJS
    walks the page to /clip?name=\u2026 (which autoplays) the instant the clip lands - or rewrites
    #pullnote if the pull produced nothing playable.

    `check` = None -> THE CACHE RULE decides whether to ask the camera: a date entirely in the past
    whose cached read happened AFTER that date ended cannot gain minutes, so it opens from the cache
    with ZERO camera work (card_complete); today, a never-checked date, or a read taken while the day
    was still open, still queries. True = force one card query (/day?day=X&recheck=1, the `check this
    day` chip - it covers the rare case where the card recycled an old day), False = serve the cache
    (/day?day=X&cache=1, for scripts). `clock="pm"` labels the afternoon row 12 PM, 1 PM ... PM only
    ever applies to the 12:00-23:59 row (/day?day=X&clock=pm)."""
    clips = [c for c in clips_on_disk() if c[1].get("day") == day]
    if clock is None:
        clock = clock_pref()          # None = not asked = use the ONE saved setting
    cached = daymaps_on_disk().get(day)
    if pull:
        want = False                      # a pull owns the mgr session: never fight it for the read
    elif check is not None:
        want = bool(check)                # &recheck=1 forces a read, &cache=1 forbids one
    else:
        want = not card_complete(day, cached)
    fresh_read = False
    if want:
        # OPENING A DATE THE CACHE CANNOT ANSWER = CHECKING THAT DATE ON THE CARD: one mgr query,
        # a few seconds. daymap() refuses to spawn --list while a pull/sweep holds grab.lock and
        # raises, so the honest fallback below serves the cached map with a note instead.
        try:
            d = daymap(day)
            fresh_read = True
        except Exception:
            d = cached
            if d:
                d = dict(d, note="the card could not be read right now - showing the cached day map")
    else:
        d = cached
    cardlab = (("SD card read %s \u00b7 %d min ago"
                % (atlabel(d["at"]), max(0, int((time.time() - d["at"]) / 60))))
               if d and d.get("at") else "no day map yet")
    if d and d.get("at") and not fresh_read and card_complete(day, d):
        cardlab += " \u00b7 that day is over, cached map used (no camera)"
    if d and d.get("note"):
        cardlab += " \u00b7 " + d["note"]
    nav = ['<a class="btn" href="/">home</a>',
           '<a class="btn" href="/month?m=%s">that month</a>' % esc(day[:7]),
           '<a class="btn" href="/api/daymap?day=%s">raw day map</a>' % esc(day)]
    if not want:
        nav.append('<a class="btn" href="/day?day=%s&recheck=1">check this day</a>' % esc(day))
    nav.append('<a class="btn" href="/day?day=%s&clock=%s">%s</a>'
               % (esc(day), ("24h" if clock == "pm" else "pm"),
                  ("24 h" if clock == "pm" else "pm hours")))
    if not pull:                          # a pull page stays put: no bouncing anywhere
        nav.append(look_chip("/day?day=%s" % esc(day)))
    nav.append('<a class="btn" href="/help">how it works</a>')
    nav.append(sponsor_chip())
    hdr = page_hdr(nav)
    # the SD-card line is NOT a third header row  - it sits next to the
    # day title; the wordmark and the nav chips now share ONE row.
    body = [hdr, '<h1>%s \u2014 SD day map + clips <span class="k2">%s</span></h1>'
            % (esc(day), esc(cardlab))]
    if pull:
        pname, peta = pull
        eta = max(0, int(peta))
        body.insert(1, '<div id="pullnote"><b>PULLING %s</b> \u00b7 ETA %d:%02d \u00b7 '
                    'this page opens the video when it lands <a class="help" href="/help">?</a></div>'
                    % (esc(pname), eta // 60, eta % 60))
    if d and d.get("ranges"):
        body.append('<div class="panel">%s</div>' % strip_rows(day, d, clips, pm=(clock == "pm")))
    elif d and not d.get("failed"):
        body.append('<div class="panel"><div class="k2">the card was checked for %s (at %s) and holds '
                    'no recording for it \u00b7 <a class="help" href="/help">?</a></div></div>'
                    % (esc(day), atlabel(d.get("at") or time.time())))
    else:
        body.append('<div class="panel"><div class="k2">no day map for %s \u00b7 the card was not read '
                    '(or another read holds it) \u00b7 <a href="/refresh?day=%s&to=day">check this day again</a></div></div>'
                    % (esc(day), esc(day)))
    cardm = sum(r[1] - r[0] + 1 for r in d["ranges"]) if d and d.get("ranges") else 0
    body.append('<div class="chips"><span class="chip">%s on this day</span>'
                '<span class="chip">~%d min on card</span></div>' % (plural(len(clips)), cardm))
    rows = []
    for name, meta in reversed(clips):
        partial = meta.get("partial") or 0
        errs = meta.get("decode_errors") or 0
        if meta.get("grab_failed") or (meta.get("bytes") or 0) <= 0:
            q, qtxt = "bad", "FAILED"
        elif partial or errs:
            q, qtxt = "part", "PARTIAL"
        else:
            q, qtxt = "ok", "OK"
        head = ('<span class="q-ref">REFERENCE \u2014 the app\'s own clip</span>'
                if meta.get("reference") else '<span class="q-%s">%s</span>' % (q, qtxt))
        rows.append('<div class="drow">%s <b class="dname">%s</b> '
                    '<span class="k2">SD %s \u00b7 %s \u00b7 %.1f s \u00b7 %s partial / %s err</span>'
                    '<span class="dacts">'
                    '<a class="btn" href="/clip?name=%s">watch</a> '
                    '<a class="btn" href="/dl/%s.mp4" download="%s.mp4">save</a> '
                    '<a class="btn" href="/delete?name=%s&day=%s">delete</a></span></div>'
                    % (head, esc(name), esc((meta.get("range") or "").replace("-", "\u2013")),
                       mb(meta.get("bytes") or 0), meta.get("dur_video") or 0, partial, errs,
                       esc(name), esc(name), esc(name), esc(name), esc(day)))
    body.append('<div class="dlist">%s</div>' % "".join(rows) if rows else
                '<div class="panel"><div class="k2">nothing pulled for this day yet \u00b7 click a green '
                'day-map minute (one pass streams one minute of SD in ~50 s of wall-clock, so a 5-pass '
                'pull of one minute is ~4 min plus the setup phase, one pull at a time) or use '
                '<a href="/grab?day=%s&range=13:00-13:01&passes=5">/grab</a></div></div>'
                % esc(day))
    body.append('<div class="k2"><a href="/">home</a> \u00b7 '
                '<a href="/month?m=%s">%s</a></div>' % (esc(day[:7]), esc(day[:7])))
    if pull:
        body.append(PULLJS % (esc(pname), 2 * (int(peta) // 3 + 10)))
    return page(day, "".join(body))



def render_clip(name: str) -> str:
    """ONE video on the page (autoplay muted then unmuted by the inline script in card_html -
    browsers refuse to start an untouched video with sound), ruler + slider seek it in place."""
    jp = SHARE / (name + ".json")
    if not (SHARE / (name + ".mp4")).exists() or not jp.exists():
        return page(name, ('<h1>%s \u2014 not on the share</h1>'
                    '<div class="k">that clip is not on the share \u2014 the minute is still on the '
                    'card, so the day map can pull it again.</div>')
                    % esc(name),
                    ['<a class="btn" href="/">home \u2014 this month</a>',
                     '<a class="btn" href="/help">how it works</a>', sponsor_chip()])
    meta = json.loads(jp.read_text())
    d0 = meta.get("day") or ""
    body = [page_hdr(['<a class="btn" href="/day?day=%s">that day</a>' % esc(d0),
                     '<a class="btn" href="/">home</a>',
                     '<a class="btn" href="/help">how it works</a>',
                     look_chip("/clip?name=%s" % name), sponsor_chip()]),
            card_html(name, meta, pre="auto", auto=True)]
    return page(name, "".join(body))



def render_clip_by_range(day: str, rng: str) -> str:
    """App-like selection: /clip?day=&range= opens the clip that covers that minute, or offers the
    pull if the minute is not in the gallery yet."""
    want = clip_name(day, rng)
    if (SHARE / (want + ".mp4")).exists():
        return render_clip(want)
    mins = max(1, mins_of("", rng)[1] - mins_of("", rng)[0])
    return page("not pulled", '<h1>%s %s \u2014 not in the gallery</h1>'
                '<div class="panel"><div class="k">pull it: <a class="btn" '
                'href="/pull?day=%s&range=%s&passes=5">pull this minute</a> (~%d min, one pull at a '
                'time \u2014 it opens itself when it lands) \u00b7 or that day\'s page '
                '<a href="/day?day=%s">%s</a></div></div>'
                % (esc(day), esc(rng), esc(day), esc(rng).replace(":", "%3A"),
                   (PASS_S * 5 * mins) // 60, esc(day), esc(day)),
                ['<a class="btn" href="/">home \u2014 this month</a>', day_chip(day),
                 '<a class="btn" href="/help">how it works</a>', sponsor_chip()])



SCRUBJS = """<script>
(function(){
 function tlFor(el){ return document.getElementById(el.getAttribute('data-tl')); }
 function vid(el){ return document.getElementById(el.getAttribute('data-v')); }
 function seek(v,el,f){
   if(!v || !isFinite(v.duration) || v.duration<=0) return;
   if(f<0)f=0; if(f>1)f=1;
   v.currentTime = f*v.duration;
   try{ v.play(); }catch(e){}
   var m = el ? el.querySelector('.tmark') : null;
   if(m) m.style.left = (f*100)+'%';
 }
 var tls = document.querySelectorAll('.tl');
 for(var i=0;i<tls.length;i++){
   (function(el){
     var v = vid(el);
     if(!v) return;
     el.addEventListener('mousedown', function(e){
       var r = el.getBoundingClientRect();
       seek(v, el, (e.clientX-r.left)/r.width);
       e.preventDefault();
     });
   })(tls[i]);
 }
 var ins = document.querySelectorAll('input.scrub');
 for(var j=0;j<ins.length;j++){
   (function(s){
     var v = vid(s), el = tlFor(s);
     s.addEventListener('input', function(){ seek(v, el, (s.value-0)/1000.0); });
     s.addEventListener('change', function(){ seek(v, el, (s.value-0)/1000.0); });
   })(ins[j]);
 }
})();
</script>"""


MAPJS = """<script>
(function(){
  var rows;
  try { rows = document.querySelectorAll(".sdr"); } catch(e){ return; }
  function fmt(m,pm,full){
    m = Math.round(m); if(m < 0) m = 0; if(m > 1439) m = 1439;
    var h = Math.floor(m/60), mm = m - h*60;
    if(pm && h >= 12){ var h12 = (h === 12) ? 12 : h - 12;
      if(full || mm) return h12 + ":" + (mm<10?"0":"") + mm + " PM";
      return h12 + " PM"; }
    return (h<10?"0":"") + h + ":" + (mm<10?"0":"") + mm;
  }
  function setup(r){
   try{
    var lo0 = parseFloat(r.getAttribute("data-lo")), span0 = parseFloat(r.getAttribute("data-span"));
    if(!(lo0 >= 0) || !(span0 > 0)) return;
    var pm = r.getAttribute("data-pm") ? 1 : 0;
    var st = { lo: lo0, span: span0 };
    function relayout(){
      var kids = r.childNodes, i, el, m, s, e, x, w, mid;
      for(i = 0; i < kids.length; i++){
        el = kids[i];
        m = el.getAttribute("data-min");
        if(m !== null){
          m = parseFloat(m);
          if(m < st.lo || m > st.lo + st.span){ el.style.display = "none"; continue; }
          el.style.left = ((m - st.lo) / st.span * 100.0) + "%";
          el.style.display = "";
          continue;
        }
        s = el.getAttribute("data-s");
        if(s === null) continue;                    /* the row's own label: nothing to place */
        s = parseFloat(s); e = parseFloat(el.getAttribute("data-e"));
        if(e + 1 <= st.lo || s >= st.lo + st.span){ el.style.display = "none"; continue; }
        x = (s - st.lo) / st.span * 100.0;
        w = (e + 1 - s) / st.span * 100.0;
        if(w < 1.4){                                /* one-minute minutes stay CLICKABLE */
          mid = ((s + e + 1) / 2.0 - st.lo) / st.span * 100.0;
          x = mid - 0.7;
          w = 1.4;
        }
        if(x > 98.6) x = 98.6;
        if(x < 0) x = 0;
        if(w > 100.0 - x) w = 100.0 - x;
        el.style.left = x + "%";
        el.style.width = w + "%";
        el.style.display = "";
      }
      r.style.backgroundSize = (60.0 / st.span * 100.0) + "% 100%";
      r.className = "sdr" + (st.span <= 120 ? " b5 b15" : (st.span <= 300 ? " b15" : ""));
      var lab = r.querySelector(".srlab");
      if(st.span >= span0){            /* the whole row: the hour labels under the bar already say it */
        if(lab) r.removeChild(lab);
      } else {
        if(!lab){                      /* zoomed in: name the window, nothing else does */
          lab = document.createElement("div");
          lab.className = "srlab";
          r.appendChild(lab);
        }
        lab.textContent = fmt(st.lo, pm, 1) + " \u2013 " + fmt(st.lo + st.span - 1, pm, 1);
      }
    }
    function zoom(ev){
      var b = r.getBoundingClientRect();
      if(!b.width) return;
      var at = st.lo + (ev.clientX - b.left) / b.width * st.span;
      var ns = (ev.type == "dblclick") ? 30.0 : st.span * ((ev.deltaY > 0) ? 1.25 : 0.8);
      if(ns > 720.0) ns = 720.0;
      if(ns < 15.0) ns = 15.0;                      /* never zoom out past that row's own 12 h */
      st.lo = at - (at - st.lo) / st.span * ns;
      st.span = ns;
      if(st.lo < lo0) st.lo = lo0;
      if(st.lo > lo0 + span0 - ns) st.lo = lo0 + span0 - ns;
      relayout();
      ev.preventDefault();                          /* the wheel zooms, it does not scroll here */
    }
    relayout();
    r.addEventListener("wheel", function(ev){ try{ zoom(ev); }catch(e){} });
    r.addEventListener("dblclick", function(ev){ try{ zoom(ev); }catch(e){} });
   }catch(e){}
  }
  for(var i = 0; i < rows.length; i++) setup(rows[i]);
})();
</script>"""


class H(SimpleHTTPRequestHandler):
    directory = str(SHARE)               # class attr: SimpleHTTPRequestHandler honours it

    def translate_path(self, path):
        """Rooted at the share dir (own implementation so cwd can't matter; '..'/hidden
        components are dropped by the same filter SimpleHTTPRequestHandler uses)."""
        path = path.split("?", 1)[0].split("#", 1)[0]
        path = urllib.parse.unquote(path)
        words = [w for w in path.split("/") if w and w not in (os.curdir, os.pardir)]
        return os.path.join(str(SHARE), *words)

    def send_html(self, s, status=200):
        """Every dynamic page (LIVE bar included) is regenerated per request: a browser that
        caches one may keep a bar frozen at its first render , so say no-store on all of them. Static files stay cacheable.
        `status` lets the server's OWN pages (a 404) answer with the right code and still be HTML."""
        body = s.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error404(self, message=None):
        """A wrong URL used to be the browser's bare page — no wordmark, no way out, exactly the
        complaint that kept coming back. Python 3.12's send_error() builds its OWN body and never
        routes through error404, so serve_file() calls this method directly — same header as every
        other page, so even a mistyped address says where to go next."""
        self.send_html(page("not found",
                            '<h1>no such page on the gallery</h1>'
                            '<div class="k">a mistyped address \u00b7 the calendar, the month chips and '
                            'the chips on every page reach everything that exists.</div>',
                            default_nav()), 404)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if q.get("fx"):                    # a LOOK choice on ANY page: /day?day=X&fx=-scan,
            set_fx_pref(fx_names(q["fx"][0], fx_pref()))   # /month?m=2026-10&fx=off, ...
            # prefs.json is global, so this page (and every page after it) renders with that look.
            # A look is CSS only - it asks the camera for nothing and starts nothing.
        if u.path == "/look":
            # the LOOK MENU. With fx= it also RETURNS to the page that sent the viewer here
            # (`to=`, percent-encoded, whitelisted to LOOK_BACK), so picking "no effects" on a day
            # page lands back on that day page - never on /pull, /grab, /delete, /sweep, /refresh.
            to = urllib.parse.unquote((q.get("to") or [""])[0])
            if (q.get("fx") or [""])[0] and to and urlparse(to).path in LOOK_BACK:
                self.send_response(303)
                self.send_header("Location", to)
                self.end_headers()
                return
            self.send_html(render_look((q.get("to") or [""])[0]))
            return
        if u.path == "/":
            self.send_html(render_home())
            return
        if u.path == "/month":
            self.send_html(render_month((q.get("m") or [""])[0]))
            return
        if u.path == "/day":
            clk = (q.get("clock") or [None])[0]
            if clk is not None:                     # a chip was clicked -> remember it for EVERY day
                set_clock_pref(clk)
            self.send_html(render_day((q.get("day") or [datetime.date.today().isoformat()])[0],
                                      check=(True if q.get("recheck") else
                                             False if q.get("cache") else None),
                                      clock=clk))
            return
        if u.path == "/clip":
            if q.get("name"):
                self.send_html(render_clip(q["name"][0]))
            else:
                self.send_html(render_clip_by_range((q.get("day") or [""])[0],
                                                    (q.get("range") or [""])[0]))
            return
        if u.path == "/statusfrag":            # the LIVE bar's poll target (HTML fragment)
            self.send_html(status_frag())
            return
        if u.path == "/help":                  # the long text lives here, not on the gallery pages
            self.send_html(render_help())
            return
        if u.path == "/status.json":           # machine-readable for scripts/CLI
            lock = SHARE / "grab.lock"
            running = lock.exists() and time.time() - lock.stat().st_mtime <= LOCK_TIMEOUT
            obj = {"running": running}
            if running:
                # the sidecar is read ONLY while a lock is ALIVE. A grab that died (killed unit, a
                # rebooted box, a crashed child) leaves grab-running.json behind, and merging it
                # every time made a script see day/range/started for a grab that is NOT running
                # (2026-11-01: {"running": false, "day": "cli", "range": "13:13-13:14"} read as
                # "a grab is stuck here" when nothing was). idle = just say idle.
                try:
                    obj.update(json.loads(RUNNING.read_text()))
                except Exception:
                    pass
            self.send_json(obj)
            return
        if u.path == "/delete":               # two-step: /delete?name=X asks, &confirm=1 does
            nm = (q.get("name") or [""])[0]
            d0 = (q.get("day") or [""])[0] or day_for(nm)
            nav = ['<a class="btn" href="/">home \u2014 this month</a>', day_chip(d0),
                   '<a class="btn" href="/help">how it works</a>', sponsor_chip()]
            files = clip_files(nm)
            if not files:
                self.send_html(page(nm, ('<h1>%s \u2014 not on the share</h1>'
                            '<div class="k">nothing by that name on the share \u2014 the minute is '
                            'still on the card, so that day\'s day map can pull it again.</div>')
                            % esc(nm), nav))
                return
            if not (q.get("confirm") or [""])[0]:
                self.send_html(page(nm,
                    '<h1>delete %s from the gallery?</h1>'
                    '<div class="panel"><div class="k">%d files (~%s)</div>'
                    '<pre>%s</pre>'
                    '<div class="k">Only gallery files go: the .mp4, the poster, the 9 stills, the '
                    'sidecar, the pull dir. The minute is still ON THE CARD, so you can pull it '
                    'again at any time; a clip imported from a CLI run keeps its seg.* files under '
                    'probes.</div>'
                    '<a class="btn" href="/delete?name=%s&confirm=1">yes, delete it</a></div>'
                    % (esc(nm), len(files), mb(sum(f.stat().st_size for f in files if f.is_file())),
                       esc(" ".join(str(f).replace(str(SHARE), "") for f in files)), esc(nm)),
                       nav))
                return
            gone, nbytes = delete_clip(nm)
            self.send_html(page(nm, '<h1>%s deleted</h1>'
                        '<div class="k">%d files removed (~%s) \u00b7 the minute stays ON THE CARD '
                        '\u2014 pull it again from that day\'s page whenever you want</div>'
                        % (esc(nm), gone, mb(nbytes)), nav))
            return
        if u.path == "/api/daymap":
            day = (q.get("day") or [datetime.date.today().isoformat()])[0]
            try:
                out = daymap(day)
            except Exception as e:
                out = {"day": day, "error": str(e)}
            self.send_json(out)
            return
        if u.path == "/grab" or u.path == "/pull":
            day = (q.get("day") or [datetime.date.today().isoformat()])[0]
            rng = (q.get("range") or ["12:00-12:01"])[0]
            passes = int((q.get("passes") or ["5"])[0])
            lock = SHARE / "grab.lock"
            if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_TIMEOUT:
                lock.unlink(missing_ok=True)
            busy = lock.exists()
            if busy:
                why = "a grab is already running — see /status"
                try:                                    # say WHICH one, and how long it has taken
                    r = json.loads(RUNNING.read_text())
                    why = ("grab %s %s (%d pass(es)) has been running %d min — "
                           "wait for it or check /status" %
                           (r["day"], r["range"], r["passes"],
                            int((time.time() - r["started"]) / 60)))
                except Exception:
                    pass
            else:
                lock.touch()
                RUNNING.write_text(json.dumps({"day": day, "range": rng, "passes": passes,
                                               "started": int(time.time())}))
                threading.Thread(target=lambda: (run_grab(day, rng, passes),
                                                 lock.unlink(missing_ok=True),
                                                 RUNNING.unlink(missing_ok=True)),
                                 name="grab").start()
            if u.path == "/pull":                        # browser-friendly wrapper for day-map clicks
                mins = max(1, mins_of("", rng)[1] - mins_of("", rng)[0])
                eta = PASS_S * passes * mins
                self.send_html(render_day(day, (clip_name(day, rng), eta))
                               if not busy else render_day(day))
                return
            self.send_json({"started": not busy, "clip": clip_name(day, rng), "why": why,
                            "note": "~%d s wall-clock PER PASS per MINUTE of SD (measured); %d passes x %d min of SD = ~%.1f min" % (PASS_S, passes, mins, eta / 60)})
            return
        
        if u.path == "/status":
            lock = SHARE / "grab.lock"
            running = lock.exists()
            info = ""
            if running:
                info = "lock: RUNNING"
                try:
                    r = json.loads(RUNNING.read_text())
                    info += "  \u2014 %s %s (%s pass(es), started %s, %d min so far)" % (
                        r["day"], r["range"], r["passes"],
                        atlabel(r["started"]),
                        int((time.time() - r["started"]) / 60))
                except Exception:
                    pass
            try:
                s = json.loads(SWEEP.read_text())
                info += ("  \u2014 MONTH SWEEP for %s: %d/%d dates done (last %s, started %s)" %
                         (s["month"], s["done"], s["total"], s.get("last") or "?",
                          atlabel(s["started"])))
            except Exception:
                pass
            logs = sorted(SHARE.glob("grab-*/grab-*.log"), key=lambda f: f.stat().st_mtime)
            # the log lives INSIDE each grab-<name>/ dir (SHARE/grab-X/grab-X.log) - the old flat
            # glob("grab-*.log") matched nothing, which is why the /status tail looked empty.
            if (SHARE / "sweep.log").exists():
                logs = sorted(logs + [SHARE / "sweep.log"], key=lambda f: f.stat().st_mtime)
            tail = ""
            if logs:
                tail = logs[-1].read_text()[-4000:]
            self.send_html(page("status",
                                page_hdr(['<a class="btn" href="/">home \u2014 this month</a>',
                                          '<a class="btn" href="/help">how it works</a>',
                                          sponsor_chip()]) +
                                '<pre>%s\nlast grab log (tail):\n%s</pre>'
                                % (esc(info or "lock: idle"), esc(tail))))
            return
        if u.path == "/sweep":
            m = (q.get("m") or [datetime.date.today().strftime("%Y-%m")])[0]
            if not month_ok(m):
                m = datetime.date.today().strftime("%Y-%m")
            lock = SHARE / "grab.lock"
            if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_TIMEOUT:
                lock.unlink(missing_ok=True)
            busy = bool(lock.exists() or (SWEEP.exists() and
                                          time.time() - SWEEP.stat().st_mtime < LOCK_TIMEOUT))
            if not busy:
                lock.touch()
                SWEEP.write_text(json.dumps({"month": m, "started": int(time.time()), "done": 0,
                                             "total": len(month_dates(m))}, indent=1))
                threading.Thread(target=lambda: (sweep_month(m),
                                                 lock.unlink(missing_ok=True),
                                                 SWEEP.unlink(missing_ok=True)),
                                 name="sweep").start()
            self.send_html(render_month(m))
            return
        if u.path == "/refresh":
            days = (q.get("day") or [datetime.date.today().isoformat()])
            for dd in days:
                try:
                    daymap(dd)
                except Exception:
                    pass
            # re-read the day maps, then go back to WHERE you came from: /refresh?day=X&to=day returns
            # that day's page (the day page's re-read chip), plain /refresh returns the index.
            back = "/" if (q.get("to") or [""])[0] != "day" or len(days) != 1 \
                   else "/day?day=%s" % days[0]
            self.send_response(303)
            self.send_header("Location", back)
            self.end_headers()
            return
        if u.path.startswith("/dl/"):
            self.path = u.path[len("/dl"):]        # -> /NAME.mp4, served as an attachment
            self.serve_file(attach=True)
            return
        self.serve_file()

    def serve_file(self, attach=False):
        """Static file with Range support (so <video> seeking doesn't re-fetch 5 MB)."""
        fp = self.translate_path(self.path)
        if not os.path.isfile(fp):
            self.error404()
            return
        blob = open(fp, "rb").read()
        off, size = 0, len(blob)
        rng = self.headers.get("Range") or self.headers.get("range")
        if rng:
            m = re.search(r"bytes\s*=\s*(\d+)\s*-\s*(\d*)", rng)
            if m:
                off = min(int(m.group(1)), len(blob))
                size = min(int(m.group(2) or len(blob) - 1), len(blob) - 1) - off + 1
                size = max(size, 0)
        ext = os.path.splitext(fp)[1].lower()
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        if attach:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(fp))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (off, off + size - 1, len(blob)))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(blob[off:off + size])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--grab", nargs=2, metavar=("DAY", "RANGE"))
    ap.add_argument("--import", dest="imp", metavar="SEGDIR",
                    help="publish an existing ipc360 output (e.g. probes/play-P5b/seg)")
    ap.add_argument("--name", help="clip name for --import (default: <dir>-<file>)")
    ap.add_argument("--range", dest="range", help="HH:MM-HH:MM for --import (day-map marker position)")
    ap.add_argument("--day", dest="daymap_note", default="", help="day for --import card")
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--thumbs", metavar="NAME", help="make stills + sidecar for a clip on the share")
    ap.add_argument("--reference", metavar="NOTE",
                    help="with --thumbs: label the clip as someone else's file (e.g. 'from the IPC360 app')")
    ap.add_argument("--daymap", metavar="DAY")
    ap.add_argument("--sweep", metavar="MONTH",
                    help="ask the card for every date of YYYY-MM and cache each day map")
    a = ap.parse_args()
    SHARE.mkdir(parents=True, exist_ok=True)
    if a.thumbs:
        name = a.thumbs
        mp4 = SHARE / (name + ".mp4")
        if not mp4.exists():
            print("no such clip on the share:", mp4)
            return
        try:
            meta = json.loads((SHARE / (name + ".json")).read_text())
        except Exception:
            meta = {"name": name, "bytes": mp4.stat().st_size, "at": int(mp4.stat().st_mtime)}
        meta["name"] = name
        meta["dur_video"] = ffprobe_dur(mp4) or meta.get("dur_video")
        meta["dur_audio"] = ffprobe_dur(mp4, "a") or meta.get("dur_audio")
        meta["video_fps"] = meta.get("video_fps") or ffprobe_fps(mp4)   # container truth (10 or 20)
        meta["bytes"] = mp4.stat().st_size
        if a.reference:
            meta["reference"] = a.reference
        meta.setdefault("smin", 0)
        meta.setdefault("emin", 0)
        th = thumbs(mp4)
        if th:
            meta["thumbs"] = th
        if not (SHARE / (name + ".jpg")).exists():          # poster for clips published elsewhere
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(mp4),
                            "-frames:v", "1", "-vf", "scale=480:-1",
                            str(SHARE / (name + ".jpg"))], capture_output=True, text=True)
        (SHARE / (name + ".json")).write_text(json.dumps(meta, indent=1, sort_keys=True))
        print("thumbs:", len(meta.get("thumbs") or []), "json:", (SHARE / (name + ".json")))
        return
    if a.imp:
        seg = Path(a.imp)
        seg = seg if seg.suffix == ".mp4" else (seg.parent / (seg.name + ".mp4"))
        name = a.name or (seg.parent.name + "-" + seg.name[:-4])   # probes/play-P5b/seg -> play-P5b-seg
        day = a.daymap_note
        src = seg
        dst = SHARE / (name + ".mp4")
        ok = faststart(src, dst)
        if not ok:
            print("faststart failed; publishing the file as-is")
            src.rename(dst)
        rng = a.range or ""
        vmeta = {"name": name, "day": day, "range": rng,
                 "bytes": dst.stat().st_size if dst.exists() else 0,
                 "dur_video": ffprobe_dur(dst), "dur_audio": ffprobe_dur(dst, "a"),
                 "smin": mins_of(name, rng)[0], "emin": mins_of(name, rng)[1],
                 "src": str(src), "faststart": ok, "at": int(time.time())}
        st = seg_stats(seg.parent)
        for k, v in st.items():
            if k not in ("video", "audio", "audio_s16", "mp4", "mkv", "wav"):
                vmeta[k] = v
        if st:
            vmeta["decode_errors"] = st.get("decode_errors")
            vmeta.setdefault("audio_codec", st.get("audio_codec", "alaw"))
        vmeta["audio_seconds"] = st.get("audio_seconds") or (
            (seg.parent / "seg-audio.raw").stat().st_size / 8000.0
            if (seg.parent / "seg-audio.raw").exists() else None)
        vmeta["thumbs"] = thumbs(dst)
        (SHARE / (name + ".json")).write_text(json.dumps(vmeta, indent=1, sort_keys=True))
        print("published", dst, dst.stat().st_size, "thumbs", len(vmeta["thumbs"]))
        return
    if a.grab:
        print(json.dumps(run_grab(a.grab[0], a.grab[1], a.passes), indent=1, sort_keys=True))
        return
    if a.daymap:
        print(json.dumps(daymap(a.daymap), indent=1)[:1200])
        return
    if a.sweep:
        print(json.dumps(sweep_month(a.sweep if month_ok(a.sweep)
                              else datetime.date.today().strftime("%Y-%m")), indent=1))
        return
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), H)
    print("camclips on http://localhost:%d/ (Ctrl-C to stop)" % a.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
