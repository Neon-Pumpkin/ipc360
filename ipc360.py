#!/usr/bin/env python3
"""ipc360.py - talk to the IPC360/Puwell mgr directly: SD-card day map + recordings.

stdlib only (socket + struct + hashlib); ffmpeg is used for the mp4 mux.

  python3 ipc360.py --list --day 2026-10-30
  IPC360_PW=your-camera-password python3 ipc360.py --play 13:00-13:01 --day 2026-10-30 \
      --out probes/play/seg --seconds 120 --idle 15      # -> seg.mkv + seg.mp4
  python3 ipc360.py --from-wire probes/play-H/pull-20261030-1300-1301.wire   # no network needed
  python3 ipc360.py --from-wire A/pull-....wire,B/pull-....wire  # comma list = slot union of
                                                        # two pulls of the same range (fills
                                                        # datagrams the relay lost on one pass)

Wire framing (all managers): 20-byte header
  [0xFFEEDDCC, cmd, rev, tot=len(payload)+20, 0]  then payload (little-endian dwords).
Manager payload envelope: [camId, user_id, inner_len, seq] + inner.

Login rule (cracked, verified live): salt = 15 ASCII chars from cmd 10030;
  digest = md5( md5hex(password) + salt ) hex-lowercase
sent as cmd 10001, 332-byte payload: email[64] | digest@0x40 | tail (see TAIL).
"""
import hashlib, socket, struct, os, sys, time, datetime, argparse, json, re

SEC = os.environ.get("IPC360_CREDS_FILE", "")   # OPTIONAL "email / password" file you keep
                                       # outside the repo
REV = 0x6912E4
CAM_ID_DEFAULT = None   # no camera is assumed: --cam <camId>, IPC360_CAM_ID, or the settings file
FATHER_ID = 41                        # global_set::father_id on the wire
TAIL = bytes.fromhex("0000010001000900000030303000")   # verbatim from the capture

CFG = os.environ.get("IPC360_CONFIG", "ipc360-config.json")   # the ONE settings file


def cfg_read():
    """The settings file as a dict ({} when there is none). Written by --setup, mode 0600."""
    try:
        return json.loads(open(CFG).read())
    except Exception:
        return {}


def cfg_set(**kv):
    c = cfg_read()
    c.update({k: v for k, v in kv.items() if v not in (None, "")})
    try:
        open(CFG, "w").write(json.dumps(c, indent=1) + "\n")
        os.chmod(CFG, 0o600)                 # a no-op where unix modes do not exist
    except Exception:
        pass


def cfg_get(name, env, default=None):
    """One setting: the environment wins, then the settings file, then the default."""
    v = os.environ.get(env)
    if v not in (None, ""):
        return v
    return cfg_read().get(name, default)


def read_creds():
    """email + password come from IPC360_EMAIL / IPC360_PW, from the settings file
    (--setup writes it), from IPC360_CREDS_FILE, or they are ASKED FOR on the terminal.
    Nothing credential-shaped is ever stored in the code itself."""
    c = cfg_read()
    lines = [l.strip() for l in open(SEC).read().splitlines() if l.strip()] if SEC else []
    email = os.environ.get("IPC360_EMAIL") or c.get("email") or next((l for l in lines if "@" in l), "")
    pw = os.environ.get("IPC360_PW") or c.get("pw") or next((l for l in lines if "@" not in l), "")
    if not email:
        email = input("email the camera is enrolled in (the app's login): ").strip()
    if not pw:
        try:
            import getpass
            pw = getpass.getpass("that camera's password (not echoed): ").strip()
        except Exception:
            pw = input("that camera's password: ").strip()
    if not email or not pw:
        sys.exit("no email or no password: run ipc360.py --setup, or export "
                "IPC360_EMAIL / IPC360_PW")
    return email, pw


def frame(cmd, payload, rev=REV):
    return struct.pack("<IIIII", 0xFFEEDDCC, cmd, rev, len(payload) + 20, 0) + payload


def envelope(cam, user, inner, seq):
    return struct.pack("<IIII", cam, user, len(inner), seq & 0xFFFFFFFF) + inner


def recv_frame(sock, timeout=12.0):
    sock.settimeout(timeout)
    hdr = b""
    try:
        while len(hdr) < 20:
            d = sock.recv(20 - len(hdr))
            if not d:
                return None
            hdr += d
    except Exception:
        return None
    magic, cmd, rev, tot, spare = struct.unpack("<IIIII", hdr)
    n = max(0, tot - 20)
    pay = b""
    try:
        while len(pay) < n:
            d = sock.recv(n - len(pay))
            if not d:
                break
            pay += d
    except Exception:
        return None
    return (cmd, rev, pay)


def u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


class Mgr:
    def __init__(self, ip, port):
        self.ip, self.port = ip, port
        self.sock = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(12)
        s.connect((self.ip, self.port))
        self.sock = s
        return s

    def send(self, cmd, payload, rev=REV):
        self.sock.sendall(frame(cmd, payload, rev))

    def flushin(self):
        """drop pending replies (handshake acks) so the day-map scan starts clean"""
        self.sock.settimeout(3)
        try:
            while True:
                d = self.sock.recv(4096)
                if not d:
                    break
        except Exception:
            pass

    def recv(self, timeout=12.0):
        return recv_frame(self.sock, timeout)

    def drain(self, want, timeout=12.0, tries=1):
        """read frames until one has cmd == want (keep others in .extras)"""
        out = []
        for _ in range(tries):
            r = self.recv(timeout)
            if r is None:
                break
            out.append(r)
            if r[0] == want:
                return r, out
        return None, out


def login(mgr, email, pw, cam_id):
    """salt -> digest -> user_id/sessionid.  Returns dict or None."""
    s = mgr.open()
    mgr.send(10030, email.encode().ljust(64, b"\0") + b"\x01\x00")
    r = mgr.recv()
    if r is None:
        return None
    salt = r[2].rstrip(b"\0")
    md5pw = hashlib.md5(pw.encode()).hexdigest()
    digest = hashlib.md5(md5pw.encode() + salt).hexdigest()
    req = bytearray(332)
    req[0:64] = email.encode().ljust(64, b"\0")
    req[0x40:0x40 + 32] = digest.encode()
    req[0x80:0x80 + len(TAIL)] = TAIL
    mgr.send(10001, bytes(req))
    r = mgr.recv()
    if r is None:
        return None
    pay = r[2]
    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in pay)
    # verified live (payload offsets; matches sdk::login_data_return frame+0x14..):
    #   +0x00 u32 user_id   +0x04 ip packed as 4 bytes   +0x08 u32 port
    #   +0x0c u32 custom_id +0x10 u32 dev_id             +0x14 u32 0
    #   +0x18 char sessionid[128] (base64)
    uid = u32(pay, 0x00)
    mip = "%d.%d.%d.%d" % tuple(pay[4:8])
    port = u32(pay, 8)
    custom = u32(pay, 0x0C)
    sid = pay[0x18:0x18 + 128].rstrip(b"\0")
    info = dict(salt=salt.decode("latin1"), ip_packed=pay[4:8], uid=uid, mgr_ip=mip, mgr_port=port,
                custom=custom, sessionid=sid.decode("latin1"), raw=pay, asc=asc)
    info["ok"] = bool(sid) and uid != 0 and "not correct" not in asc
    return info


def mgr_sign_in(mgr, custom, owner, sessionid):
    p = bytearray(148)
    p[0:4] = struct.pack("<HH", 1, 1)
    p[4:8] = struct.pack("<I", custom)
    p[8:12] = struct.pack("<I", owner)
    p[20:20 + len(sessionid)] = sessionid[:128]
    mgr.send(20200, bytes(p))
    r, extra = mgr.drain(20200)
    return r, extra


def shm(day, y=None, h=0, mi=0, s=0):
    """ShmTime = 6 LE dwords [year, month, day, hour, min, sec] (year is literal:
    2026 == 0x7ea).  The mgr echoes it back as dayEpoch = that day's 00:00
    CAMERA-local (camera runs UTC-4)."""
    d = datetime.datetime.strptime(day, "%Y-%m-%d") if isinstance(day, str) else day
    return struct.pack("<IIIIII", y or d.year, d.month, d.day, h, mi, s)


def day_map(mgr, cam, user, ip_packed, day=None, seq=20373, ack20402=True):
    """camera handshake then cmd 20400 (enveloped, inner 68 B ShmTime range))
    -> the mgr answers cnt x 20401, each:
      [cam,user,innerlen,seq+1, cam,user, dayEpoch, chunkCnt, idx, sublen, slot]
    idx 1 = index-version ASCII (sub 16); idx 2..9 = 180 minute flags, base=(idx-2)*180.
    """
    mgr.send(1, struct.pack("<II", user, 0))
    mgr.send(20202, struct.pack("<I", cam) + ip_packed + struct.pack("<II", 1, 10))
    mgr.send(20357, struct.pack("<III", cam, user, 1))
    mgr.send(20326, struct.pack("<II", cam, user))
    mgr.send(20352, struct.pack("<III", cam, user, 0x7D))
    mgr.send(20282, struct.pack("<IIII", cam, user, 0, 0))   # capture fd64 @72.811, 16 B
    mgr.flushin()
    inner = (struct.pack("<IIIII", cam, user, user, 0, 0) + shm(day) + b"\x00" * 24)[:68]
    mgr.send(20400, envelope(cam, user, inner, seq))
    chunks = {}
    version = None
    mins = [0] * 1440
    cnt = 0
    for _ in range(120):
        r = mgr.recv()
        if r is None:
            break
        if r[0] != 20401:
            continue
        pay = r[2]
        idx, cnt, sub = u32(pay, 32), u32(pay, 28), u32(pay, 36)
        slot = pay[40:40 + sub]
        chunks[idx] = (u32(pay, 24), cnt, sub, slot)
        if idx == 1:
            version = slot.split(b"\0")[0].decode("latin1")
        elif idx >= 2:
            base = (idx - 2) * 180
            for i, f in enumerate(slot[:180]):
                if f:
                    mins[base + i] = f
        if cnt and len(chunks) >= cnt:
            break
    if ack20402:
        # capture fd64 @73.5: the app acks the index with 20402 (inner_len 220 =
        # [cam,user,1]+zeros, envelope seq 90004) and the mgr answers 20403 (seq 90005,
        # body [cam,user,0,0,0,0,"000"]).  The capture's playback release only happens
        # after this beat closes, so mirror it.
        mgr.send(20402, envelope(cam, user, struct.pack("<III", cam, user, 1) + b"\x00" * 208, 90004))
        for _ in range(3):
            r = mgr.recv(6.0)
            if r is None:
                break
    return mins, version, chunks


def occ_key(groups, k, data):
    """Key an audio chunk with an OCCURRENCE suffix: (3, vt, fid) -> (3, vt, fid, occ).

    net=3 is NOT reassembled by the SDK (ipc_frame_data: `net == 3` bypasses the slot book-keeping
    and calls the callback straight away, tot is always 1), and the same fid legitimately repeats
    in ONE pass: probes/play-K p3 = 1314 audio chunks in 1051 distinct fids, 263 of them sent TWICE
    back-to-back with DIFFERENT payloads (fid delta hist = +1 x1041, +0 x263).  Collapsing by fid
    throws those chunks away (= 84 KB = 10.5 s of audio in that pass), which is why the muxed audio
    ran ~14 s shorter than the video.  Dedupe by payload identity instead of by fid, so a repeat
    pass can never double-count the same datagram.
    """
    o = 0
    while True:
        kk = k + (o,)
        d = groups.get(kk)
        if d is None or d["slots"].get(0) == data:
            return kk          # empty slot = a new occurrence; identical data = the SAME datagram
        o += 1                 # occupied by a DIFFERENT payload -> this one is a later occurrence


def merge_groups(dst, src):
    """Union slot dicts of two pulls of the SAME range (relay datagram loss differs per pass).
    net=3 keys carry an occurrence suffix: drop a pass-B chunk into the first occurrence slot for
    its fid whose data DIFFERS (identical data at the same occ == the same datagram, skip it)."""
    for k, d in src.items():
        kk = k
        if k[0] == 3:
            o = k[3]
            mine = d["slots"].get(0)
            while True:
                cand = k[:3] + (o,)
                g0 = dst.get(cand)
                if g0 is None or g0["slots"].get(0) == mine:
                    break
                o += 1
            kk = k[:3] + (o,)
        g0 = dst.setdefault(kk, {"tot": d["tot"], "slots": dict(d["slots"]), "fmt": d["fmt"],
                                 "cnt": d.get("cnt")})
        g0["slots"].update(d["slots"])
        g0["tot"] = d["tot"]
    return dst


ALAW2S16 = (
    -5504, -5248, -6016, -5760, -4480, -4224, -4992, -4736,
    -7552, -7296, -8064, -7808, -6528, -6272, -7040, -6784,
    -2752, -2624, -3008, -2880, -2240, -2112, -2496, -2368,
    -3776, -3648, -4032, -3904, -3264, -3136, -3520, -3392,
    -22016, -20992, -24064, -23040, -17920, -16896, -19968, -18944,
    -30208, -29184, -32256, -31232, -26112, -25088, -28160, -27136,
    -11008, -10496, -12032, -11520, -8960, -8448, -9984, -9472,
    -15104, -14592, -16128, -15616, -13056, -12544, -14080, -13568,
    -344, -328, -376, -360, -280, -264, -312, -296,
    -472, -456, -504, -488, -408, -392, -440, -424,
    -88, -72, -120, -104, -24, -8, -56, -40,
    -216, -200, -248, -232, -152, -136, -184, -168,
    -1376, -1312, -1504, -1440, -1120, -1056, -1248, -1184,
    -1888, -1824, -2016, -1952, -1632, -1568, -1760, -1696,
    -688, -656, -752, -720, -560, -528, -624, -592,
    -944, -912, -1008, -976, -816, -784, -880, -848,
    5504, 5248, 6016, 5760, 4480, 4224, 4992, 4736,
    7552, 7296, 8064, 7808, 6528, 6272, 7040, 6784,
    2752, 2624, 3008, 2880, 2240, 2112, 2496, 2368,
    3776, 3648, 4032, 3904, 3264, 3136, 3520, 3392,
    22016, 20992, 24064, 23040, 17920, 16896, 19968, 18944,
    30208, 29184, 32256, 31232, 26112, 25088, 28160, 27136,
    11008, 10496, 12032, 11520, 8960, 8448, 9984, 9472,
    15104, 14592, 16128, 15616, 13056, 12544, 14080, 13568,
    344, 328, 376, 360, 280, 264, 312, 296,
    472, 456, 504, 488, 408, 392, 440, 424,
    88, 72, 120, 104, 24, 8, 56, 40,
    216, 200, 248, 232, 152, 136, 184, 168,
    1376, 1312, 1504, 1440, 1120, 1056, 1248, 1184,
    1888, 1824, 2016, 1952, 1632, 1568, 1760, 1696,
    688, 656, 752, 720, 560, 528, 624, 592,
    944, 912, 1008, 976, 816, 784, 880, 848,
)
ZERO_ALAW = 85            # the a-law byte whose decode is closest to 0 (|value| = 8)

def audio_track(groups, audio_rate=8000, codec="alaw", head=0):
    """Lay the net=3 packets on an ABSOLUTE timeline -> (bytes, stats).

    Two things the naive concatenation got wrong, both audible:
      * a lost audio packet used to silently CLOSE a 40 ms hole, so everything after it
        drifts (up to ~3.5 s by the end of a minute) - audio and video then disagree.
        The net=3 chunk's own `cnt` is a per-net packet counter (audio cnt = 49..1498 for
        the ~1450 packets a minute sends), so `cnt - cnt_min` IS the packet's ordinal:
        holes become silence and the picture stays in step.
      * the head: the camera never sends the first ~50 audio packets for a range, so the
        audio must start LATE, not at 0.
    Anchor = `--audio-anchor`: fid (DEFAULT - first packet at fid_first x the 50 ms video
    frame time; PROVEN on 2026-10-30 13:00-13:01: the RMS-profile best shift vs the viewer's
    app clip came out at exactly the 2.00 s burned-in-OSD difference) or end (last packet at
    the minute end - gave 16.4 s = wrong) or start (no head).
    Returns (raw_codec_bytes, s16le_bytes, stats).
    """
    aud = []
    for k, d in groups.items():
        if k[0] != 3:
            continue
        data = b"".join(d["slots"][i] for i in sorted(d["slots"]))
        if data:
            aud.append((d.get("cnt"), k[2], k[3] if len(k) > 3 else 0, data))
    if not aud:
        return b"", b"", {"packets": 0}
    all_cnt = all(a[0] is not None for a in aud)
    if all_cnt:
        cmin = min(a[0] for a in aud)
        slot = {}
        for a in aud:
            slot.setdefault(a[0] - cmin, a[3])       # the same cnt from two passes IS the same packet
        if max(slot) > 3 * len(slot):                # cnts that didn't line up: not an ordinal
            all_cnt = False
    if not all_cnt:
        aud.sort(key=lambda a: (a[1], a[2]))          # fid order, holes invisible (last resort)
        slot = {i: a[3] for i, a in enumerate(aud)}
    span = max(slot) + 1 + max(head, 0)
    hole_byte = bytes([ZERO_ALAW]) * 320 if codec == "alaw" else b"\x00" * 320
    buf = [None] * span                              # one slot per packet ordinal
    for n, data in slot.items():
        m = n + max(head, 0)
        if 0 <= m < span:
            buf[m] = data
    raw = b"".join(d if d else hole_byte for d in buf)
    nfill = len(slot)
    if codec == "alaw":
        s16 = bytearray(2 * len(raw))
        for i, b_ in enumerate(raw):
            v = ALAW2S16[b_]
            s16[2 * i] = v & 0xFF
            s16[2 * i + 1] = (v >> 8) & 0xFF
    elif codec == "u8":
        s16 = bytearray(2 * len(raw))
        for i, b_ in enumerate(raw):
            v = (b_ - 128) << 8
            s16[2 * i] = v & 0xFF
            s16[2 * i + 1] = (v >> 8) & 0xFF
    else:                                             # s8
        s16 = bytearray(2 * len(raw))
        for i, b_ in enumerate(raw):
            v = (b_ - 256 if b_ > 127 else b_) << 8
            s16[2 * i] = v & 0xFF
            s16[2 * i + 1] = (v >> 8) & 0xFF
    st = {"packets": nfill, "packets_expected": span, "mode": "cnt" if all_cnt else "fid-ordinal",
          "hole_packets": span - nfill - max(head, 0),
          "hole_seconds": round((span - nfill - max(head, 0)) * 320.0 / audio_rate, 2),
          "seconds": round(len(raw) / float(audio_rate), 2),
          "codec": codec, "cnt_min": (min(a[0] for a in aud) if all_cnt else None),
          "fid_min": min(a[1] for a in aud)}
    return raw, bytes(s16), st


def assemble(groups, outdir, prefix="seg", audio_rate=8000, audio_codec="alaw",
             anchor="fid", denoise="", ffmpeg=True, frame_files=False):
    """Turn a dict of (net,vt,fid[,occ]) -> {tot,slots,fmt,cnt} into files under outdir:
      prefix.h264            nets 0+1, groups sorted by (fid, net) -> Annex-B H264
      prefix-audio.raw       net=3 on the ABSOLUTE timeline (codec bytes, holes = silence)
      prefix-audio-s16.raw   that timeline decoded to signed 16-bit at audio_rate (the mux input)
      prefix.mkv             -c copy  (h264 + pcm_s16le 8 kHz: lossless, VLC/mpv)
      prefix.wav             the audio alone, lossless
      prefix.mp4             -c:v copy + audio -> mp3 8 kHz `-strict -1` (browser/Windows);
                             this ffmpeg build has NO AAC encoder, so the app's AAC look can
                             only be approximated - `--denoise NAME` appends `-af NAME` to the
                             mp4 audio (verified-working names here: anlmdn, adynamicsmooth,
                             afftdn; `denoise` is NOT a filter).  Default '' = no filter: the
                             a-law decode already removed the u8 amplitude/noise doubling.
      prefix.stats.json      that stats dict (camclips.py reads it when publishing a clip)
    PARTIAL video groups are written with the slots we have; on the test minute `--passes 5`
    left 2 PARTIAL groups and 1 'error while decoding' line (measured by decoding the raw
    h264 here, st["decode_errors"]); 3 passes left 5 PARTIAL + a 2.2 s frame gap. See the
    PROGRESS.md passes table.  Returns a stats dict.  """
    import subprocess
    os.makedirs(outdir, exist_ok=True)
    vp = os.path.join(outdir, prefix + ".h264")
    ap_ = os.path.join(outdir, prefix + "-audio.raw")
    sp_ = os.path.join(outdir, prefix + "-audio-s16.raw")
    order = sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][3] if len(kv[0]) > 3 else 0))
    nv = 0
    partial = []
    with open(vp, "wb") as f:
        for k, d in order:
            if k[0] not in (0, 1):
                continue
            if len(d["slots"]) != d["tot"]:
                partial.append((k, len(d["slots"]), d["tot"]))
            nv += 1
            for i in sorted(d["slots"]):
                f.write(d["slots"][i])
    vids = [k for k in groups if k[0] in (0, 1)]
    fid_min_v = min((k[2] for k in vids), default=0)
    fid_max = max((k[2] for k in vids), default=0)
    n_fid = max(1, fid_max - fid_min_v + 1)   # fid is a CONTINUOUS frame index over the range
    fid_min_a = min((k[2] for k in groups if k[0] == 3), default=0)
    # NOT every minute records at 20 fps: 2026-10-31 00:42-00:45 is 10 fps (fid 0..1799 over
    # 180.04 s; ffprobe r_frame_rate on the muxed file says 10/1), 2026-10-30 13:00-13:01 is 20
    # (fid 0..1199 over 60 s).  net=3 sends 25 packets/s (320 B @ 8 kHz = 64 kb/s) and its `cnt`
    # is that packet counter, so 25 * (fid slots / cnt slots) = that minute's own fps - no CLI
    # flag needed, and it reproduces both measured minutes exactly (25*1200/1500 = 20.0,
    # 25*1800/4500 = 10.0).  Snapping to a common rate keeps the 20-fps minutes bit-identical
    # to the old math; a 10-fps minute used to get its `fid` head HALF too small (= the
    # audio starting a few seconds early).
    acnts = [d.get("cnt") for k, d in groups.items() if k[0] == 3 and d.get("cnt") is not None]
    fps = 20.0                                   # the old assumption, used when audio is absent
    if acnts:
        n_cnt = max(acnts) - min(acnts) + 1
        est = 25.0 * n_fid / float(n_cnt)
        fps = min((10.0, 12.5, 15.0, 20.0, 25.0, 30.0), key=lambda r: abs(r - est))
    fid_s = 1.0 / fps                            # one fid, in seconds, at THAT rate
    vspan_s = n_fid * fid_s                      # the range's true span at that rate
    na = sum(1 for k in groups if k[0] == 3)
    adur_s = na * 320.0 / audio_rate
    if anchor == "fid":
        head = int(round(fid_min_a * fid_s * audio_rate / 320.0))
    elif anchor == "start":
        head = 0
    else:                                     # "end": line the last packet up with the minute end
        head = max(0, int(round((vspan_s - adur_s) * audio_rate / 320.0)))
    raw, s16, ast = audio_track(groups, audio_rate, audio_codec, head=head)
    ast["anchor"] = anchor
    ast["head_seconds"] = round(head * 320.0 / audio_rate, 2)
    ast["video_span_seconds"] = round(vspan_s, 2)
    with open(ap_, "wb") as f:
        f.write(raw)
    with open(sp_, "wb") as f:
        f.write(s16)
    st = dict(video=vp, video_groups=nv, video_bytes=os.path.getsize(vp),
              video_fid_min=fid_min_v, video_fid_max=fid_max, video_fps=fps,
              audio=ap_, audio_s16=sp_, audio_codec=audio_codec, partial=len(partial),
              audio_rate=audio_rate)
    st.update({"audio_" + k: v for k, v in ast.items()})
    st["audio_groups"] = ast.get("packets", 0)          # name camclips.py's parser expects
    st["audio_bytes"] = os.path.getsize(ap_)
    if ast.get("packets_expected"):
        print("  audio: %d pkts / span %d pkts | %.2f s audio vs %.2f s video | head %.2f s "
              "| holes %d pkts = %.2f s | anchor=%s codec=%s @ %d Hz (mode=%s cnt_min=%s fid_min=%s)"
              % (ast["packets"], ast["packets_expected"], ast["seconds"],
                 ast["video_span_seconds"], ast["head_seconds"], ast["hole_packets"],
                 ast["hole_seconds"], anchor, audio_codec, audio_rate,
                 ast.get("mode"), ast.get("cnt_min"), ast.get("fid_min")))
    print("  video: %d groups, fid %d..%d = %.1f fps (%.2f s span) - the SD's own rate for "
          "that range, net=3 counter ratio not an assumption"
          % (nv, fid_min_v, fid_max, fps, vspan_s))
    for k, have, want in partial:
        print("  PARTIAL net=%d vt=%d fid=%d have=%d/%d" % (k[0], k[1], k[2], have, want))
    st["partial_list"] = ["net=%d fid=%d have %d/%d" % (k[0], k[2], have, want)
                          for k, have, want in partial]
    # honest decode count: a PARTIAL group leaves one broken macroblock in the stream; the mux
    # logs are captured so camclips.py's log regex can never see it - decode the raw h264 here.
    dr = subprocess.run(["ffmpeg", "-y", "-loglevel", "info", "-f", "h264", "-i", vp,
                         "-f", "null", "-"], capture_output=True, text=True)
    st["decode_errors"] = (dr.stderr or "").count("error while decoding")
    print("  decode: %d 'error while decoding' lines in the raw h264 over %d frames"
          % (st["decode_errors"], nv))

    def save_stats(st_=None):
        with open(os.path.join(outdir, prefix + ".stats.json"), "w") as f:
            json.dump(st_, f, indent=1, sort_keys=True)

    save_stats(st)
    if not ffmpeg:
        return st
    dur = {}
    mp4a = ["-c:v", "copy", "-movflags", "+faststart", "-strict", "-1",
            "-ar", str(audio_rate), "-c:a", "mp3", "-b:a", "64k"]
    if denoise:
        mp4a += ["-af", denoise]
    jobs = [("mkv", ["-c", "copy"]), ("mp4", mp4a), ("wav", ["-c", "copy"])]
    for name, args in jobs:
        outp = os.path.join(outdir, prefix + "." + name)
        aopts = ["-f", "s16le", "-ar", str(audio_rate), "-ac", "1", "-i", sp_]
        if name == "wav":
            cmd = ["ffmpeg", "-y", "-loglevel", "error"] + aopts + args + [outp]
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "h264", "-i", vp] + aopts + args + [outp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            st[name + "-err"] = (r.stderr or "")[-300:]
            continue
        pr = subprocess.run(["ffprobe", "-v", "info", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", "-i", outp],
                            capture_output=True, text=True)
        st[name] = outp
        st[name + "-bytes"] = os.path.getsize(outp)
        for ln in (pr.stdout or "").splitlines():
            if ln.strip()[:1].isdigit():
                dur[name] = ln.strip()
                break
        st[name + "-dur"] = dur.get(name)
        print("  %s -> %s  (%s B, dur %s)" % (name, outp, os.path.getsize(outp), dur.get(name, "?")))
    save_stats(st)
    return st


def wire_groups(paths):
    """Re-parse saved .wire blobs -> groups dict (same shape pull builds).
    Accepts a comma-separated list: unioning two pulls of the SAME range fills the idx runs the
    relay lost on one pass (probes/wirechk.py shows those gaps are contiguous, so a second pass
    usually completes the group). Later files win on a duplicate idx."""
    g = {}
    for path in [p for p in paths.split(",") if p]:
        raw = open(path, "rb").read()
        frames, leftover = split_frames(raw)
        for cmd, rev, pay in frames:
            if cmd != 40005:
                continue
            c = parse_chunk(pay)
            if c is None:
                continue
            k = (c["net"], c["vt"], c["fid"])
            if c["net"] == 3:
                k = occ_key(g, k, c["data"])
            d = g.setdefault(k, {"tot": c["tot"], "slots": {}, "fmt": c["fmt"], "cnt": c["cnt"]})
            d["slots"][c["idx"]] = c["data"]
            d["tot"] = c["tot"]
    return g


def shm_t(dt):
    """ShmTime = 6 LE dwords [year, month, day, hour, min, sec] (year literal).
    None -> 24 zero bytes (the capture's first 20241 has an EMPTY range)."""
    if dt is None:
        return b"\x00" * 24
    return struct.pack("<IIIIII", dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def remote_file(cam, user, ctrl, start, end, tail=0, custom=None, name=b""):
    """SDK_REMOTE_FILE, 188 B, NOT enveloped.  Verbatim from the capture
    (fd64 cmd 20241, pw=188): [cam, user, custom, device=0, ctrl, name[112],
    ShmTime start@+132, ShmTime end@+156, u32 1, u32 tail].
    ctrl comes from playback_send's switch: 2/3/10/11/4/0 (0xb = play).
    Capture order: ctrl=0xb with EMPTY ShmTime + tail=0x23 (list probe), then per
    camera 40014: ctrl=0 (range, tail 0), ctrl=4 (range, tail 0), ctrl=0xb (range, tail 0x23).
    start/end may be None -> 24 zero bytes."""
    """SDK_REMOTE_FILE, 188 B, NOT enveloped.  Verbatim from the capture
    (fd64 cmd 20241, pw=188): [cam, user, custom, device=0, ctrl, name[112],
    ShmTime start@+132, ShmTime end@+156, u32 1, u32 tail].
    ctrl comes from playback_send's switch: 2/3/10/11/4/0 (0xb = play)."""
    p = bytearray(188)
    p[0:4] = struct.pack("<I", cam)
    p[4:8] = struct.pack("<I", user)
    p[8:12] = struct.pack("<I", user if custom is None else custom)
    p[16:20] = struct.pack("<I", ctrl)
    p[20:20 + len(name)] = name[:112]
    p[132:156] = shm_t(start)
    p[156:180] = shm_t(end)
    p[180:184] = struct.pack("<I", 1)
    p[184:188] = struct.pack("<I", tail)
    return bytes(p)


def parse_chunk(p):
    """40005 msg header.  TWO layouts occur in the capture (tools/frparse.py proved it):
      A (+8 net|vt, +12 fid, +16 cnt, +20 idx|tot, +24 dl)          dl == len(p)-28
      C (+8 net|vt, +12 fid, +16 = 3 ASCII digits + pad, +22 idx|tot, +24 dl) ditto
    The A/C test is `dl == len(p)-28`. **A now wins whenever A's size test passes**: on a fresh
    correctly-delimited minute ALL 6988 chunks satisfy A (`dl == len(p)-28`), and in the old capture
    dump all 2510 satisfy it too — the 109 "C" rows there are the SAME A header whose `cnt`
    dword happens to read as 3 ASCII digits. The old tiebreak `cnt == fid*tot+idx` is not a rule at
    all: `cnt` is a session-GLOBAL chunk counter (0..5771 across nets), so it holds for only 232/6988
    chunks and would mis-key ~4 % of them onto the wrong fid. C is kept as the fallback for headers
    where A's size test fails.
    chunkTotal <= 0x200, dataLen <= 0x3d0."""
    u = lambda o, n=4: int.from_bytes(p[o:o + n], "little")
    if len(p) < 28:
        return None
    tot_data = len(p) - 28
    a_cnt, a_idx, a_tot, a_dl = u(16), u(20, 2), u(22, 2), u(24)
    c_asc = p[16:20]
    c_ok = all(0x30 <= x <= 0x39 for x in c_asc[:3])
    c_idx, c_tot, c_dl = u(22, 2), u(24, 2), u(26)
    A = a_dl == tot_data
    C = c_ok and c_dl == tot_data
    if A and not C:
        fmt = "A"
    elif C and not A:
        fmt = "C"
    elif A and C:
        fmt = "A"          # cnt == fid*tot+idx is NOT a rule (cnt is a global chunk counter)
    else:
        fmt = "A" if a_dl else "C"
    if fmt == "A":
        return dict(fmt="A", net=u(8, 2), vt=u(10, 2), fid=u(12), cnt=a_cnt, idx=a_idx, tot=a_tot,
                    dl=a_dl, data=p[28:28 + tot_data])
    return dict(fmt="C", net=u(8, 2), vt=u(10, 2), fid=u(12),
                cnt=int(c_asc[:3].decode()) if c_ok else None, idx=c_idx, tot=c_tot, dl=c_dl,
                data=p[28:28 + tot_data])


MAGIC = 0xFFEEDDCC


def split_frames(b):
    """Split a received blob -> (frames, leftover_bytes), MAGIC-delimited.
    Why magic-delimited and not pure `tot` slicing (both facts come from the capture,
    probes/mgrseq.py + probes/p2p-seq.txt of frames/pw9.log):
      * 1846 of 4729 camera chunk datagrams are 2 B short of their header `tot` and the
        next frame starts on the NEXT datagram (the capture's own tool printed "!! resync"
        there).  Frames therefore live in a byte stream that happens to be delivered in
        datagrams: buffer the tail and delimit by magic, trusting `tot` only when it fits.
      * the mgr/P2P relay sometimes DOUBLE-FRAMES: a 20-B wrapper header (magic at +0)
        whose own `tot` equals the INNER frame's, magic at +20 -> the wrapper yields an
        empty payload and the inner frame parses normally.
    """
    starts, o = [], 0
    while True:
        n = b.find(b"\xcc\xdd\xee\xff", o)
        if n < 0:
            break
        starts.append(n)
        o = n + 4
    out, cut = [], None
    for k, p in enumerate(starts):
        if p + 20 > len(b):
            cut = p
            break
        cmd, rev, tot = (int.from_bytes(b[p + 4 + q * 4:p + 8 + q * 4], "little") for q in range(3))
        nxt = starts[k + 1] if k + 1 < len(starts) else len(b)
        if tot < 20 or tot > 300000:                      # garbage / stale bytes
            continue
        if p + tot <= nxt:
            end = p + tot                                 # complete frame
        elif k + 1 < len(starts):
            end = nxt                                     # header lies: data ends at next magic
        else:
            cut = p                                       # may continue in the next datagram
            break
        out.append((cmd, rev, b[p + 20:end]))
    return out, (b[cut:] if cut is not None else b"")


def read_msgs(sock, udp, timeout=0.4, buf=b""):
    """One read from the camera socket -> (raw_bytes, frames, leftover, eof).
    A SOCK_DGRAM read MUST ask for the whole datagram: Linux truncates recv(n) to n bytes
    and silently drops the tail of the datagram (that tail is the end of many a 40005
    chunk payload -- the cause of the short chunks).  `buf` is the previous leftover.
    The timeout is SHORT on purpose: the app's keep_send_beat fires every ~1.6 s whether
    or not data is inbound, so a blocking 12 s recv would starve the mgr flow control."""
    sock.settimeout(timeout)
    if udp:
        try:
            d = sock.recv(65536)
        except Exception:
            return None, [], buf, False          # timeout, not EOF
        if not d:
            return None, [], buf, True           # EOF
        raw = buf + d
        fr, left = split_frames(raw)
        return raw, fr, left, False
    r = recv_frame(sock, timeout)
    if r is None:
        return None, [], buf, False
    raw = struct.pack("<IIIII", MAGIC, r[0], r[1], 20 + len(r[2]), 0) + r[2]
    return raw, [r], b"", False


def mgr_p2p_msg(mgr, cam, user, father, mgr_ip_packed, param_3=0x5553,
                local_ip=0x0000A00F, port_hts=0xB465, tag=b"004\x08"):
    """sdk::mgr_conn::mgr_p2p_msg -> mgr cmd 20203, 40 B (Ghidra Gh 0x1dd0f8).
    Stack layout from &local_50: [u16 param_3, u16 1, u32 cam(param_1), u32 param_2(father),
    u32 owner_client_id, u32 local_public_ip, u32 local_ip, u16 htons(local_udp_port),
    u16 (same/lo), u32 mgr_ip].  Verbatim capture bytes for the local_* fields (the app's
    own bind socket params) -- only cam/user/mgr_ip are substituted here.
    The mgr answers 20204 (40 B) whose +0x10 = P2P relay IP (4 dotted bytes) and +0x14
    = relay port BYTE-SWAPPED (0x4b 0x69 -> 19305).  Returns that reply."""
    p = bytearray(40)
    p[0:2] = struct.pack("<H", param_3)
    p[2:4] = struct.pack("<H", 1)
    p[4:8] = struct.pack("<I", cam)
    p[8:12] = struct.pack("<I", father)
    p[12:16] = struct.pack("<I", user)
    p[20:24] = struct.pack("<I", local_ip)
    p[24:28] = tag
    p[28:30] = struct.pack("<H", port_hts)
    p[30:32] = struct.pack("<H", local_ip & 0xFFFF)
    p[32:36] = b"\x00\x0000"
    p[36:40] = mgr_ip_packed[:4]      # dotted bytes, same packing as the login reply
    mgr.send(20203, bytes(p))
    r, extra = mgr.drain(20204, timeout=8.0, tries=24)
    return r, extra


def mgr_log(mgr, out, n=10, verbose=True, tmo=0.3):
    """Drain up to n mgr replies, record them in out['mgr_hist'], print cmd/pw,
    and pull any 20312 endpoint announce.  20312 payload dws = [0, cam, 0, user, pub_ip(4 bytes),
    pub_port(swapped), lan_ip, lan_port, 0,0,0,8,8,8,8] -- the camera's OWN P2P endpoints (public + LAN).
    tmo is SHORT because this runs inside the keepalive beat: a 6 s idle recv would eat the beat."""
    eps = []
    hist = out.setdefault("mgr_hist", {})
    for _ in range(n):
        r = mgr.recv(tmo)
        if r is None:
            break
        cmd, rev, pay = r
        hist[cmd] = hist.get(cmd, 0) + 1
        out["raw"] += struct.pack("<IIIII", MAGIC, cmd, rev, 20 + len(pay), 0) + pay
        if cmd == 20312 and len(pay) >= 0x20:
            ip = "%d.%d.%d.%d" % tuple(pay[0x10:0x14])
            port = int.from_bytes(pay[0x14:0x16], "big")
            lan = "%d.%d.%d.%d" % tuple(pay[0x18:0x1C])
            lanp = int.from_bytes(pay[0x1C:0x1E], "little")
            eps.append((ip, port, lan, lanp))
            if verbose:
                print("  mgr 20312: camera P2P public %s:%d / LAN %s:%d (payload %s)" %
                      (ip, port, lan, lanp, pay[:56].hex(" ")))
        elif verbose and cmd not in (1, 3, 20401):
            print("  mgr cmd=%d pw=%d %s" % (cmd, len(pay), pay[:24].hex(" ")))
    return eps


def p2p_endpoint(pay):
    """mgr 20204 reply payload -> (ip_string, port) of the P2P relay the camera P2P
    frames must go to (sdk::video_conn::send_p2p_stun_msg reads +0x10 IP / +0x14 port
    byte-swapped and stores them at video_conn+0x14/+0x18, used by send_msg_bind_udp)."""
    b = pay[0x10:0x14]
    ip = "%d.%d.%d.%d" % tuple(b)
    port = int.from_bytes(pay[0x14:0x16], "big")
    return ip, port


def pull(mgr, cam, user, father, start, end, outdir, mgr_ip_packed=b"", wiretag="",
         cam_ip=None, cam_port=None, tail=0x23, keep=1.6, seconds=90, verbose=True,
         udp=True, announce=True, force_ep=False, ack="seq", release_on_beat=True, idle=12.0,
         early=False, frame_files=True):
    """Full playback path, in CAPTURE order (fd64/fd138 of frames/pw9.log 20:53:05-07):
      1. mgr 20241 ctrl=0xb, EMPTY ShmTime, tail=0x23   (list probe)
      2. mgr 20203 mgr_p2p_msg -> mgr 20204 = P2P relay endpoint (147.135.38.160, 19305)
      3. camera socket -> 40001 x3 + 40013             (sdk::video_conn::send_p2p_stun_msg)
      4. echo every camera 40002 VERBATIM; on 40014 #1 send mgr 20241 ctrl=0 (range),
         on 40014 #2 ctrl=4 (range), on 40014 #3 nothing (capture: 40014 x3, 20241 x2);
         keepalive cmd=1 on both sockets every ~1.6 s AND, on each beat after the handshake,
         mgr 20241 ctrl=0xb (range, tail=0x23) == the flow control that releases ~250-550
         chunks per beat (probes/batch.py of pw9.log: 20 beats -> 268/285/262/... chunks);
         ACK every 40005 with 40004 [cam, ctr, user] (capture: ctr ~= the chunk's own cnt).
    The camera channel is a **datagram** socket in the capture (sendto/recvfrom, one frame
    per datagram) -- udp=True uses SOCK_DGRAM.  Returns out dict incl. "ep".
    """
    import time
    s = None
    st = time.time()
    out = {"frames": {}, "chunks": 0, "frames_done": 0, "raw": b"", "groups": {}}
    rawp = os.path.join(outdir, "pull-%s-%02d%02d-%02d%02d%s.wire" %
                        (start.strftime("%Y%m%d"), start.hour, start.minute, end.hour, end.minute,
                         wiretag)) if outdir else None
    if rawp:
        os.makedirs(os.path.dirname(rawp), exist_ok=True)
    g = {}
    ep = None
    if mgr:
        mgr.send(20241, remote_file(cam, user, 0xb, None, None, tail=tail))
        eps = mgr_log(mgr, out, 12)
        if eps and not announce:
            out["ep2"] = eps[0]
    if mgr and announce:
        r4, extra = mgr_p2p_msg(mgr, cam, user, father, mgr_ip_packed)
        if r4:
            ep = p2p_endpoint(r4[2])
            out["ep"] = ep
            if verbose:
                print("  mgr 20204 -> P2P relay %s:%d (payload %s)" %
                      (ep[0], ep[1], r4[2][:40].hex(" ")))
        elif verbose:
            print("  no mgr 20204 reply")
    ip, port = ep if ep else (cam_ip, cam_port)
    if cam_ip and (not ep or force_ep):
        ip, port = cam_ip, cam_port
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.settimeout(12)
    s.connect((ip, int(port)))
    if verbose:
        print("  camera P2P %s:%s (%s)" % (ip, port, "UDP" if udp else "TCP"))
    for _ in range(3):
        s.sendall(frame(40001, struct.pack("<III", cam, user, father)))
    s.sendall(frame(40013, struct.pack("<IIII", user, father, 0x10001, cam)))
    if mgr and early:
        mgr.send(20241, remote_file(cam, user, 0xb, start, end, tail=tail))
    last_keep = time.time()
    n40014 = 0
    ackctr = 0
    nbeat = 0
    last_chunk = time.time()
    beat_chunks = 0
    buf = b""
    while True:
        if time.time() - st > seconds:
            break
        prev = len(buf)
        raw, msgs, buf, eof = read_msgs(s, udp, 0.4, buf)
        if eof:
            if verbose:
                print("  (camera socket EOF)")
            break
        if raw is None:
            msgs = ()
        else:
            out["raw"] += raw[prev:]  # only the new datagram bytes (tags survive)
        for cmd, rev, pay in msgs:
            tag = "".join(chr(c) if 32 <= c < 127 else "." for c in rev.to_bytes(4, "little"))
            if cmd == 40005:
                c = parse_chunk(pay)
                if c is None:
                    out["short"] = out.get("short", 0) + 1
                    if verbose:
                        print("  short/odd 40005 pw=%d %s" % (len(pay), pay[:24].hex(" ")))
                    continue
                k = (c["net"], c["vt"], c["fid"], c["fmt"])
                out["chunks"] += 1
                ackctr += 1
                last_chunk = time.time()
                beat_chunks += 1
                if ack != "none":
                    ctr = (ackctr - 1) if ack == "seq" else c["cnt"]
                    s.sendall(frame(40004, struct.pack("<III", cam, ctr, user)))
                d = g.setdefault(k, {"tot": c["tot"], "slots": {}, "bytes": 0, "dl": {}})
                d["slots"][c["idx"]] = c["data"]
                d["bytes"] += len(c["data"])
                d["dl"][c["idx"]] = c["dl"]
                gk = k[:3]
                if k[0] == 3:
                    gk = occ_key(out["groups"], k[:3], c["data"])
                gd = out["groups"].setdefault(gk, {"tot": c["tot"], "slots": {}, "fmt": c["fmt"],
                                                   "cnt": c["cnt"]})
                gd["slots"][c["idx"]] = c["data"]
                gd["tot"] = c["tot"]
                want = sum(d["dl"].values())
                if verbose and (c["idx"] % 8 == 0 or c["idx"] + 1 == c["tot"]):
                    print("  chunk net=%03d vt=%d fid=%d fmt=%s idx=%d/%d dl=%d/%d cnt=%d" %
                          (k[0], k[1], k[2], c["fmt"], c["idx"], c["tot"], len(c["data"]),
                           c["dl"], c["cnt"]))
                if len(d["slots"]) == c["tot"] and want:
                    blob = b"".join(d["slots"][i] for i in range(c["tot"]))
                    out["frames"][k] = blob
                    out["frames_done"] += 1
                    if verbose:
                        print("  frame net=%03d vt=%d fid=%d fmt=%s chunks=%d %d/%d B (short=%d)" %
                              (k[0], k[1], k[2], c["fmt"], c["tot"], len(blob), want, want - len(blob)))
                    if rawp and frame_files:
                        open(os.path.join(outdir, "f-%03d-%d-%010d-%s.bin" % (k[0], k[1], k[2],
                                                                           c["fmt"])), "wb").write(blob)
            elif cmd == 40002:
                s.sendall(frame(40002, pay))    # echo verbatim (real_data_return)
            elif cmd == 40014:
                n40014 += 1
                ctrl = {1: 0, 2: 4}.get(n40014)        # capture: ctrl=0 then ctrl=4, nothing on #3
                if mgr and ctrl is not None:
                    mgr.send(20241, remote_file(cam, user, ctrl, start, end, tail=0))
                mgr_log(mgr, out, 12, tmo=1.0)
                if verbose:
                    print("  40014 #%d tag=%r %s -> mgr 20241 ctrl=%s" %
                          (n40014, tag, pay[:40].hex(" "), ctrl if ctrl is not None else "-"))
            elif cmd == 40015:
                if verbose:
                    print("  40015 close-by-video-server (payload %s)" % pay.hex(" "))
                break
            elif verbose and cmd not in (3, 1):
                print("  ? cmd=%d tag=%r pw=%d %s" % (cmd, tag, len(pay), pay[:32].hex(" ")))
        now = time.time()
        if now - last_keep >= keep:
            last_keep = now
            nbeat += 1
            s.sendall(frame(1, struct.pack("<II", user, cam)))
            if mgr:
                mgr.send(1, struct.pack("<II", user, 0))
                if n40014 >= 1:                       # release beat, like the capture's keep_send_beat
                    mgr.send(20241, remote_file(cam, user, 0xb, start, end, tail=tail))
            mgr_log(mgr, out, 16, verbose=False)
            if verbose:
                print("  beat %d t=%.1f chunks+= %d total=%d mgr_replies=%s" %
                      (nbeat, now - st, beat_chunks, out["chunks"],
                      " ".join("%d:%d" % (k, v) for k, v in sorted((out.get("mgr_hist") or {}).items()))))
                beat_chunks = 0
        if time.time() - last_chunk > idle:
            if verbose:
                print("  (camera idle %.1f s -> stop)" % (time.time() - last_chunk))
            break
    if rawp:
        open(rawp, "wb").write(out["raw"])
        print("wrote %s (%d B)" % (rawp, len(out["raw"])))
    return out


# One mgr session per process: the mgr + the camera P2P relay are SHARED state, so two
# pulls at once tear each other down (the other one dies with BrokenPipeError in mgr.send
# and its day map comes back "0 chunks / no mgr 20204 reply").  The camclips page (systemd
# camclips.service, :8795) holds `grab.lock` while it runs a grab; refuse to start a CLI
# pull while that lock is alive (stale locks > LOCK_TIMEOUT_S get taken over, same rule the
# page uses).  camclips passes IPC360_LOCK_OK=1 to ITS OWN ipc360 child, which must not be
# refused by the lock the page just touched.
GRAB_LOCK_DEFAULT = os.environ.get("IPC360_LOCK", "camclips-share/grab.lock")
GRAB_LOCK_RUNNING = os.environ.get("IPC360_RUNNING", "camclips-share/grab-running.json")
LOCK_TIMEOUT_S = 1200


def cam_candidates(path: str, limit: int = 40) -> list:
    """camId candidates out of a capture — what a stranger can bring instead of knowing the id.
    Puwell device ids are ~10-digit u32s, so: every 4-byte-aligned LE dword in [1e7, 2^31) that is
    neither an ASCII run nor epoch-seconds of this decade, plus every 9-10 digit run in the text
    (the app prints the id in decimal, weighted 5 — a decimal run is a far stronger hint than a
    random dword). Over-inclusive on purpose: find_cam() TESTS each candidate with one mgr query.
    The first 6 MB of the file get scanned."""
    blob = open(path, "rb").read(6 << 20)
    counts = {}

    def add(v, w=1):
        if not (9_999_999 < v < 2 ** 31) or 1_600_000_000 <= v < 1_900_000_000:
            return
        if (10_000_000 <= v < 100_000_000 and 1900 <= v // 10000 <= 2100
            and 1 <= (v // 100) % 100 <= 12 and 1 <= v % 100 <= 31):   # an 8-digit date
            return
        b = v.to_bytes(4, "little")
        if all(32 <= x < 127 for x in b):          # an ASCII run, not an id
            return
        counts[v] = counts.get(v, 0) + w

    for o in range(0, len(blob) - 3, 4):
        add(int.from_bytes(blob[o:o + 4], "little"))
    for m in re.finditer(rb"(?<!\d)\d{9,10}(?!\d)", blob):
        add(int(m.group()), 5)          # the app's own log prints the id in decimal
    for m in re.finditer(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])", blob):
        add(int(m.group(), 16), 5)      # a hexdump of a login shows it as 8 hex digits
    return [v for v, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def find_cam(a, li) -> int:
    """--find-cam — which camId is this? One day-map query per candidate, each on a FRESH mgr
    session, and the answer is decisive: an id enrolled in the account answers with minutes, any
    other id answers with nothing (verified live: 0 chunks, index None — the mgr does not error,
    it just has no card for it). Nothing is pulled, nothing is written, ~1.5 s per candidate."""
    cands = list(dict.fromkeys(a.find_cam))
    day = a.day
    if day == datetime.date.today().isoformat():
        day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        print("(no --day given: testing yesterday %s — a day the card has finished writing)" % day)
    for p in ((a.scan or "").split(",") if a.scan else []):
        p = p.strip()
        if not p:
            continue
        try:
            found = cam_candidates(p)
        except OSError as e:
            print("scan %s: %s" % (p, e))
            continue
        print("scan %s: %d candidate(s) out of the first 6 MB (most-likely first)" % (p, len(found)))
        cands += [c for c in found if c not in cands]
    if not cands:
        print("no candidates: give numbers (--find-cam CAMID [more...]) or point at a capture "
              "(--find-cam --scan <file>): a tcpdump of one login, the app's own log, or a strings "
              "dump of the app all hold the id. The app also shows it in its device list.")
        return 1
    show = cands[:12]
    if len(cands) > len(show):
        print("testing the first %d of %d candidates (--day works as usual)" % (len(show), len(cands)))
    ip, port = a.mgr.split(":") if a.mgr else (li["mgr_ip"], str(li["mgr_port"]))
    hits = 0
    for c in show:
        m = Mgr(ip, int(port))
        m.open()
        mgr_sign_in(m, li["custom"], li["uid"], li["sessionid"].encode())
        mins, ver, chunks = day_map(m, c, li["uid"], li["ip_packed"], day, ack20402=True)
        act = [i for i, f in enumerate(mins) if f]
        if chunks:
            hits += 1
            print("HIT  %d (0x%08x): the card answered %d chunks / %d minutes on %s, index=%s"
                  % (c, c, len(chunks), len(act), day, ver))
            print("     that camera: --cam %d \u00b7 or \"cam\": %d in ipc360-config.json" % (c, c))
        else:
            print("miss %d (0x%08x): the mgr answered nothing on %s \u2192 not enrolled"
                  % (c, c, day))
    if not hits:
        print("none of these is yours. The app shows the id in its device list (Settings / device "
              "info), and one tcpdump of a login holds it as a 10-digit decimal. If your card is "
              "empty everywhere, add --day <a day it did record>.")
    return 0 if hits else 1


def grab_busy(path) -> str:
    """'' = nobody holds the pull lock; non-'' = reason (lock younger than LOCK_TIMEOUT_S).
    A lock older than that is a crashed/abandoned grab: take it over, same rule the page uses."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ""
    if (time.time() - st.st_mtime) <= LOCK_TIMEOUT_S:
        why = "another pull is running on this mgr session (%s) - refusing" % path
        try:                                    # say WHICH one, and how long it has taken
            r = json.loads(open(GRAB_LOCK_RUNNING).read())
            why = ("grab %s %s (%s pass(es)) has been running %d min on the same mgr session - "
                   "wait for it or check http://localhost:8795/status" %
                   (r["day"], r["range"], r["passes"], int((time.time() - r["started"]) / 60)))
        except Exception:
            pass
        return why
    try:
        os.unlink(path)
    except Exception:
        pass
    return ""


def grab_lock(path, force=False, what="", passes=0) -> str:
    """'' = free (and the lock is now OURS: touch it + write the sidecar); non-'' = reason to
    refuse.  `force` / env IPC360_LOCK_OK bypass it (camclips hands that to ITS OWN ipc360
    child, which must not be refused by the lock the page just touched)."""
    if force or os.environ.get("IPC360_LOCK_OK"):
        return ""
    why = grab_busy(path)
    if why:
        return why
    try:
        open(path, "w").close()
        open(GRAB_LOCK_RUNNING, "w").write(json.dumps(
            {"day": "cli", "range": what, "passes": passes, "started": int(time.time())}))
    except Exception as e:
        print("grab lock: %s (%s) - continuing anyway" % (path, e))
    return ""


def grab_release(path):
    for p in (path, GRAB_LOCK_RUNNING):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        except Exception as e:
            print("grab lock release: %s (%s)" % (p, e))


def fmt_ranges(mins):
    out = []
    start = None
    for m in range(1441):
        f = mins[m] if m < 1440 else 0
        if f and start is None:
            start = m
        if not f and start is not None:
            out.append((start, m - 1, mins[start]))
            start = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mgr", default=None, help="override the mgr the login returns (20xxx camera cmds)")
    ap.add_argument("--login-mgr", default=os.environ.get("IPC360_LOGIN_MGR", "52.8.101.180:19000"), help="mgr that serves 10xxx account cmds")
    ap.add_argument("--cam", type=int, default=(int(cfg_get("cam", "IPC360_CAM_ID"))
                                 if cfg_get("cam", "IPC360_CAM_ID") not in (None, "") else None))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--setup", action="store_true",
                    help="ask once: email, password, camId, LAN ip, where clips live")
    ap.add_argument("--find-cam", nargs="*", default=None, type=int, metavar="CANDIDATE",
                    help="which camId is yours: each candidate gets ONE day-map query (an enrolled "
                         "id answers with minutes, anything else answers with nothing). No numbers "
                         "= take the candidates out of a --scan capture")
    ap.add_argument("--scan", default=None, metavar="FILE",
                    help="with --find-cam: a capture to take candidates from "
                         "(a tcpdump of one login, the app's own log, a strings dump)")
    ap.add_argument("--day", default=datetime.date.today().isoformat())
    ap.add_argument("--play", default=None, help="HH:MM-HH:MM")
    ap.add_argument("--pull", default=None, help="HH:MM-HH:MM straight off the camera P2P")
    ap.add_argument("--cam-ip", default="<LAN ip>")
    ap.add_argument("--cam-port", type=int, default=23456)
    ap.add_argument("--seconds", type=int, default=90, help="wall-clock cap ON ONE PASS of a --pull/--play (pull() starts its clock at the top of every pass, so a 3-minute range needs ~60 s x minutes per pass; a fixed 150 s truncates it)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--ep", default=None, help="force the camera P2P endpoint ip:port")
    ap.add_argument("--tcp", action="store_true", help="camera P2P as a TCP stream (default UDP)")
    ap.add_argument("--no-announce", action="store_true", help="skip mgr 20203 (LAN-endpoint path)")
    ap.add_argument("--ack", default="seq", choices=("seq", "cnt", "none"),
                    help="40004 counter: session-global +1 per ack (seq), echo chunk cnt, or none")
    ap.add_argument("--early", action="store_true",
                    help="also send the ctrl=0xb(range) release right after 40013 (pre-capture-order test)")
    ap.add_argument("--no-index-ack", action="store_true", help="skip the mgr 20402 index ack")
    ap.add_argument("--idle", type=float, default=12.0, help="stop a --pull after this many s without a 40005")
    ap.add_argument("--from-wire", default=None, help="re-assemble a saved .wire (no network)")
    ap.add_argument("--no-ffmpeg", action="store_true", help="write .h264/.raw only, skip the mux")
    ap.add_argument("--audio-rate", type=int, default=8000,
                    help="net=3 audio rate in Hz (the camera's own RTSP SDP says 8000; 320 B "
                         "packets at 8 kHz = exactly 8000 B/s = 64 kb/s, i.e. 40 ms per packet)")
    ap.add_argument("--audio-codec", default="alaw", choices=("alaw", "u8", "s8"),
                    help="net=3 audio codec: alaw = AT&T a-law (the camera's OWN live RTSP SDP "
                         "line says pcm_alaw/8000 and net=3 is exactly 8000 B/s = 64 kb/s); "
                         "u8/s8 = the old linear interpretation (loud, noisy, wrong)")
    ap.add_argument("--audio-anchor", default="fid", choices=("end", "fid", "start"),
                    help="where the audio timeline starts: fid = first packet at fid_first x 50 ms "
                         "(DEFAULT, matches the app's OSD to 2.00 s), end = last packet at the "
                         "minute end (drifted 16.4 s on the test minute), start = no head")
    ap.add_argument("--denoise", default="",
                    help="ffmpeg audio filter NAME for the mp4's mp3 (appended as `-af NAME`); "
                         "verified here: anlmdn, adynamicsmooth, afftdn.  '' = off (default: "
                         "a-law decode already fixed the amplitude/noise)")
    ap.add_argument("--passes", type=int, default=1,
                    help="pull the same range N times and union slots (the relay loses different "
                         "datagram runs each pass; 2 passes -> ~1/3 fewer missing chunks)")
    ap.add_argument("--force", action="store_true",
                    help="start the pull even if the camclips grab.lock says one is already running"
                         " (you WILL break the other pull: one mgr session at a time)")
    ap.add_argument("--grab-lock", default=GRAB_LOCK_DEFAULT,
                    help="refuse --pull/--play while this lock file is alive (the :8795 page's "
                         "grab.lock; '-' = no lock)")
    a = ap.parse_args()
    if a.setup:                              # the five-minute first run
        email, pw = read_creds()
        cam = a.cam
        if cam is None:
            print("(no camId yet: --find-cam can TEST numbers you have, "
                  "--find-cam --scan <capture> takes them out of a capture)")
            c = input("camId (u32 - the app's device list, or one tcpdump of a login): ").strip()
            cam = int(c, 0) if c else None
        ip = input("the camera's LAN ip, for the direct-LAN fallback [Enter = skip]: ").strip()
        share = input("where clips + day maps are kept [camclips-share]: ").strip() or "camclips-share"
        cfg_set(email=email, pw=pw, cam=cam, cam_ip=ip, share=share,
                login_mgr=a.login_mgr, cam_port=a.cam_port)
        print("wrote %s (mode 0600, kept OUT of any git tree): %s"
              % (CFG, share))
        import shutil
        for tool in ("ffmpeg", "ffprobe"):
            p = shutil.which(tool)
            print("  %-8s %s" % (tool, p if p else
                                 "NOT on PATH \u2014 --list and --pull still work; "
                                 "--play, the mux and the stills need it (see Requirements)"))
        print("now:  python3 ipc360.py --list --day <yesterday>"
              "   and   python3 camclips.py --serve")
        return 0

    def outdir_prefix(o):
        o = o or "probes/play/seg"
        if o.endswith((".mp4", ".mkv", ".h264", ".raw")):
            o = os.path.splitext(o)[0]
        p = os.path.dirname(o)
        return (p or "probes"), os.path.basename(o)

    if a.from_wire:
        outdir, prefix = outdir_prefix(a.out)
        st = assemble(wire_groups(a.from_wire), outdir, prefix,
                      audio_rate=a.audio_rate, audio_codec=a.audio_codec,
                      anchor=a.audio_anchor, denoise=a.denoise,
                      ffmpeg=not a.no_ffmpeg)
        print("wire %s: %d video groups (%d B) + %d audio groups (%d B), %d partial  %s"
              % (a.from_wire, st["video_groups"], st["video_bytes"], st["audio_groups"],
                 st["audio_bytes"], st["partial"], st["video"]))
        return 0

    lockfile = a.grab_lock if (a.pull or a.play) else ""
    if lockfile and lockfile != "-":
        why = grab_lock(lockfile, force=a.force, what=a.pull or a.play,
                        passes=max(1, a.passes))
        if why:
            print("REFUSED: %s" % why)
            return 1
    elif (a.list or a.find_cam is not None) and a.grab_lock and a.grab_lock != "-" and not a.force:
        # a --list is a mgr query too: it must NOT take the lock (it would block the page for
        # seconds, and the next CLI pull would then refuse its own lock) - only refuse if held.
        why = grab_busy(a.grab_lock) if not os.environ.get("IPC360_LOCK_OK") else ""
        if why:
            print("REFUSED: %s (a --list uses the same mgr session)" % why)
            return 1

    email, pw = read_creds()
    lip, lport = a.login_mgr.split(":")
    lmgr = Mgr(lip, int(lport))
    if a.cam is None:
        sys.exit("this camera's id is not set: add --cam <camId>, export IPC360_CAM_ID, or"
                " run ipc360.py --setup")
    li = login(lmgr, email, pw, a.cam)
    if not li or not li["ok"]:
        print("LOGIN FAILED:", (li or {}).get("asc", "")[:120])
        return 1
    print("login ok: uid=%d custom=%d camera-mgr=%s:%d sessionid=%d chars"
          % (li["uid"], li["custom"], li["mgr_ip"], li["mgr_port"], len(li["sessionid"])))

    if a.find_cam is not None:
        return find_cam(a, li)

    if a.mgr:
        ip, port = a.mgr.split(":")
    else:
        ip, port = li["mgr_ip"], str(li["mgr_port"])
    mgr = Mgr(ip, int(port))
    mgr.open()
    r, extra = mgr_sign_in(mgr, li["custom"], li["uid"], li["sessionid"].encode())
    if r:
        print("sign-in resp:", "".join(chr(c) if 32 <= c < 127 else "." for c in r[2])[:60])
    if not a.list and not a.pull and not a.play:
        return 0

    d = datetime.datetime.strptime(a.day, "%Y-%m-%d")
    day_epoch = int(d.timestamp())
    mins, ver, chunks = day_map(mgr, a.cam, li["uid"], li["ip_packed"], a.day, ack20402=not a.no_index_ack)
    rep = sorted({v[0] for v in chunks.values() if v[0]})
    rep_s = ", ".join((datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=e)).strftime("%Y-%m-%d %H:%M")
                      for e in rep)
    act = [m for m, f in enumerate(mins) if f]
    print("day %s: %d chunks, %d active minutes, index=%s  mgr-day=%s (camera TZ offset %+.0fh)"
          % (a.day, len(chunks), len(act), ver, rep_s,
             (rep[0] - day_epoch) / 3600 if rep else 0))
    for s, e, f in fmt_ranges(mins):
        print("  %02d:%02d-%02d:%02d  flag=0x%02x" % (s // 60, s % 60, e // 60, e % 60, f))

    if a.pull or a.play:
        spec = a.pull or a.play
        h0, m0, h1, m1 = [int(x) for x in spec.replace("-", ":").split(":")]
        day = datetime.date(d.year, d.month, d.day)
        start = datetime.datetime(day.year, day.month, day.day, h0, m0, 0)
        end = datetime.datetime(day.year, day.month, day.day, h1, m1, 0)
        mgr.flushin()
        cip, capp = a.cam_ip, a.cam_port
        if a.ep:
            cip, capp = a.ep.split(":")
            capp = int(capp)
        outdir, prefix = outdir_prefix(a.out)
        merged = {}
        for p in range(max(1, a.passes)):
            tag = ("-p%d" % (p + 1)) if a.passes > 1 else ""
            res = pull(mgr, a.cam, li["uid"], FATHER_ID, start, end, outdir,
                       mgr_ip_packed=li["ip_packed"], wiretag=tag, cam_ip=cip, cam_port=capp,
                       force_ep=bool(a.ep), tail=0x23, keep=1.6, seconds=a.seconds,
                       udp=not a.tcp, announce=not a.no_announce, ack=a.ack, idle=a.idle,
                       early=a.early, frame_files=bool(a.pull))
            print("pull %d/%d: %d chunks, %d complete frames, %d groups  ep=%s"
                  % (p + 1, max(1, a.passes), res["chunks"], res["frames_done"], len(res["groups"]),
                     res.get("ep") or (cip, capp)))
            merged = merge_groups(merged, res["groups"])
            if p + 1 < max(1, a.passes):
                mgr.flushin()
        if a.play:
            st = assemble(merged, outdir, prefix, audio_rate=a.audio_rate,
                          audio_codec=a.audio_codec, anchor=a.audio_anchor,
                          denoise=a.denoise,
                          ffmpeg=not a.no_ffmpeg)
            print("play: %d video groups (%d B h264) + %d audio groups (%d B), %d partial"
                  % (st["video_groups"], st["video_bytes"], st["audio_groups"],
                     st["audio_bytes"], st["partial"]))
        if lockfile and lockfile != "-":
            grab_release(lockfile)
        return 0
    if lockfile and lockfile != "-":
        grab_release(lockfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
