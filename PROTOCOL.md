# IPC360 / Puwell "PW" protocol — SD-card playback off the manager (mgr), not the camera

Reverse-engineered from `apk/native/libpwnativenetsdk.so` (5,229,992 B, ARM thumb,
md5 e30325c66ea013ea6f08a6d7a02d590e) + a live strace of the `com.ipc360` app on the tablet
(`frames/pw9.log`) + direct probes from this box. Implemented in `ipc360.py`.

**Works today**: `ipc360.py --list --day …` (day map), `--pull HH:MM-HH:MM` (chunks from the
P2P relay), `--play HH:MM-HH:MM` → `.mkv`/`.mp4`. Verified live 2026-10-31: 13:00–13:01 on
2026-10-30 → 6575 chunks / 5.25 MB / 40 s → `probes/play-H/seg.mp4` 50.25 s, 0 decode errors,
frames show the yard + white van with the burned-in OSD `2026-10-30 13:00:4x`.

---

## 1. Wire framing (all sockets)

Every message = **20-byte header + payload**, one `send()` (`sdk::utils::send_msg_tcp`,
impl 0x1ab720, PLT stub 0xf5010; always preceded by `utils::instance()` @0xf4134):

```
+0x00 u32le 0xFFEEDDCC        magic (on-wire bytes CC DD EE FF)
+0x04 u32le cmd               command number
+0x08 u32le 0x6912E4          SDK build rev  == 0x690000 | 4836
                            proven: sdk::ipc_send_proc sscanf("$Rev: 4836 $","$Rev: %d",&x)
                            then rev = x | 0x690000  (so the byte 0x12 never "hits" anything)
+0x0C u32le len(payload) + 20 packet length
+0x10 u32le 0                 spare slot (unused on this path)
+0x14 payload                fixed C struct, memcpy'd — never JSON
```

The **`rev` dword is repurposed as a tag by the P2P relay**: its 4 ASCII bytes show up as
`DEBU`, `NOTI`, `sock`, `nt32`, `37;4`, `(soc`, `..C.`, `..~.`, `lP..`, `mvoi` (see §UNRESOLVED).

Manager payloads are themselves enveloped:

```
[camId u32, user_id u32, inner_len u32, seq u32] + inner(inner_len bytes)
```

## 2. Two managers, one camera socket

| socket | who | cmd band | this box |
|---|---|---|---|
| NET mgr | `52.8.101.180:19000` | 10xxx (account/login) | login only |
| camera mgr (IOT) | `54.241.6.178:19001` (returned by login) | 20xxx (camera/day-map/playback) | everything below |
| camera P2P relay | varies per session, e.g. `147.135.38.160:19405` | 40xxx | chunk transport |

Device: camId `0xCAMID` (CAMID) "PIR", the camera on **LAN <LAN ip>** (P2P LAN port 23456
`father_id` 41 (0x29 = `global_set::father_id`), user_id/custom <user_id>, devID <devID>,
devPara <devPara>, serial <serial>. Camera TZ = UTC-4, but the mgr's day-map now echoes
`dayEpoch` = that day's **UTC** midnight (recheck each run — `--list` prints the offset).

### UDP vs TCP — the trap that cost hours

The mgr is a byte stream (`SOCK_STREAM`). **The camera P2P relay is a DATAGRAM socket**
(`sendto`/`recvfrom` in the capture, `video_conn::make_bind_udp` / `send_msg_bind_udp` in the SDK).
Consequences, all confirmed:

* `recv(n)` with `n < datagram size` **truncates and silently drops the tail** — ask for 65536.
* 1846 of 4729 capture chunk datagrams end **2 B short** of their own header `tot`, and the next
  frame starts on the *next* datagram. So the relay delivers a **byte stream inside datagrams**:
  frames straddle datagrams. `ipc360.split_frames()` is MAGIC-delimited and keeps a `leftover`
  buffer; trusting `tot` alone loses/shifting payload.
* The relay sometimes **double-frames**: a 20-B wrapper whose `tot` equals the inner frame's,
  magic at +0 and +20 → wrapper yields an empty payload, inner parses normally.
* A read **timeout ≠ EOF**: `read_msgs()` returns `(raw, frames, leftover, eof)`.

## 3. Login (10xxx) — cracked, verified live

* cmd **10030**: `email[64] | 01 00` → reply = **salt**, 15 ASCII chars.
* digest = `md5( md5hex(password) + salt )` lowercase hex.
* cmd **10001**: 332-B payload `email[64] | digest@0x40 | tail@0x80`,
  `TAIL = 00 00 01 00 01 00 09 00 00 00 30 30 30 00` (verbatim from capture).
* reply dwords: `+0x00 user_id | +0x04 ip as 4 dotted bytes | +0x08 port | +0x0C custom_id
  | +0x10 dev_id | +0x14 0 | +0x18 sessionid[128] (base64)`.

## 4. Day map (mgr 20400) — LIST

Order that works (capture-faithful; the first five are the camera handshake the capture does
before any day query):

```
mgr 1     [user, 0]                          keepalive
mgr 20202 [cam, ip(4 bytes), 1, 10]
mgr 20357 [cam, user, 1]
mgr 20326 [cam, user]
mgr 20352 [cam, user, 0x7D]
mgr 20282 [cam, user, 0, 0]                  (16 B, capture fd64 @72.811)
mgr 20400 enveloped, inner = 68 B: [cam,user,user,0,0] + ShmTime(day) + 24 zeros
      -> mgr answers cnt x 20401:
         [cam,user,innerlen,seq+1, cam,user, dayEpoch, chunkCnt, idx, sublen, slot]
         idx 1 = index-version ASCII (sub 16); idx 2..9 = 180 minute flags, base=(idx-2)*180
      -> mgr 20402 index ack: envelope seq 90004, inner 220 B = [cam,user,1] + zeros
         mgr replies 20403 (seq 90005, body [cam,user,0,0,0,0,"000"])
```

`ShmTime` = 6 LE dwords `[year, month, day, hour, min, sec]` (year literal: 2026 = 0x7ea);
24 zero bytes = "empty range".

Result for 2026-10-30: **9 index chunks, 47 active minutes**
(02:20, 06:16, 07:34, 08:25, 08:47, 08:59, 10:58, 11:17, 11:32, 11:38–39, 12:31, 12:33, 12:37,
13:00–13:01, 13:27, 13:30, 13:35, 14:18, 15:43, 16:26, 16:53, 16:55, 17:10, 17:27, 17:30–31,
18:00, 18:26–27, 18:30, 18:33, 18:43, 19:26–27, 20:03, 20:13, 20:59, 21:21, 21:44, 21:57–58,
22:01, 22:11, 22:23), flag `0x82`.

**Flag bits (verified 2026-10-31 on the day still live): `0x82` = a CLOSED minute, `0x80` = the minute
still being WRITTEN on the card** (today's 16:38–16:39 while the camera recorded) — same range entry, the
low bit is the open/closed state. Pulling an open minute returns short, so the gallery draws 0x80 dashed
(`.cellopen`, "STILL OPEN on the card") and only makes closed minutes click-pullable.

## 5. Playback (mgr 20241) + the flow-control beat — DOWNLOAD

`sdk::base_conn::playback_send` (Ghidra 0x1cb974) sends cmd **0x4f11 = 20241**, payload
**0xbc = 188 B** = `SDK_REMOTE_FILE`, **not** enveloped:

```
+0   u32 camId        +120 u32 [112-char name tail]
+4   u32 user_id      +132 ShmTime start (24 B)
+8   u32 custom_id    +156 ShmTime end  (24 B)
+12  u32 device = 0   +180 u32 1
+16  u32 ctrl         +184 u32 tail  (0x23 for the "list probe"/release form)
```

ctrl comes from `playback_send`'s switch on its int arg: `0→2, 1→3, 2→0xa, 3→0xb (play), 4→4,
999→0 (else 0)`.

Capture order, reproduced by `ipc360.py pull()`:

```
1. mgr 20241 ctrl=0xb  EMPTY ShmTime, tail=0x23        (list probe)
2. mgr 20203 mgr_p2p_msg (sdk::mgr_conn::mgr_p2p_msg, 40 B)
      -> mgr 20204 (40 B): IP = payload+0x10 as 4 dotted bytes, PORT = u16 @+0x14 BYTE-SWAPPED
3. camera socket: 40001 x3 [cam,user,father] ; 40013 [user,father,0x10001,cam]
4. echo every 40002 verbatim (12 B [cam,user,father]);  40014 arrives x3 =
      [father, user, 0, cam, 1, 0]  (24 B): on #1 send mgr 20241 ctrl=0 (range, tail 0),
      on #2 ctrl=4, on #3 nothing.
5. THEN EVERYTHING FLOWS FROM THE BEAT:
   every ~1.6 s  ->  camera keepalive cmd=1 (8 B [user, cam] on the wire — capture ground
                     truth, even though video_conn::keep_alive builds the struct as
                     [camera_id, owner_client_id]) and mgr keepalive cmd=1,
                     plus mgr 20241 ctrl=0xb(range, tail=0x23)
   that 20241-per-beat IS the flow control: each beat releases ~250–550 further 40005 chunks
   (probes/batch.py over pw9.log: 20 ctrl=0xb beats, 249..552 chunks each, camera never idle
   >0.31 s; the SDK symbol `mgr_man::keep_send_beat` @ELF 0x1ce3f9 is the beat WATCHDOG — see §12 item 7)
6. ACK every 40005 with 40004 = [camId, ctr, user_id] (see below)
```

One minute of SD = ~5 MB, released in ~40 s, then the camera goes silent (that silence is the
end of the requested range — `--idle` stops the loop).

**The "few chunks then idle" symptom was our own code**: `mgr_log()` used `mgr.recv(6.0)` inside
each keepalive beat, so every beat ate 12–96 s and the mgr's 20241 releases never landed. Fix =
short recv timeouts everywhere (`mgr_log tmo=0.3`, camera recv `0.4`), beats then fire at 1.6 s
like `keep_send_beat`. Also confirmed: `--early` (firing the ctrl=0xb release right after 40013)
is **not needed** — the capture-faithful run got *more* chunks (6575 vs 6487).

## 6. The chunk: cmd 40005, two header layouts

```
layout A: [cam, father, net u16 | vt u16, fid u32, cnt u32, idx u16 | tot u16, dl u32, data…]
layout C: [cam, father, net|vt, fid, 3 ASCII digits + pad, idx u16 | tot u16, dl u32, data…]
```

* Discriminator (`tools/frparse.py`, `ipc360.parse_chunk`): **`dl == len(payload) - 28` ⇒ A wins on that
  test alone** (patched 2026-10-31). Evidence: a fresh, correctly-delimited minute is **6988/6988 A**, and
  in the old capture dump **all 2510 chunks satisfy the A size test** while only 2401 were labelled A —
  the 109 "C" rows are the *same* A header whose `cnt` dword happens to read as 3 ASCII digits. The old
  tiebreak `cnt == fid*tot + idx` is not a rule (`cnt` is a session-global chunk counter → it holds for
  232/6988 fresh chunks) and mis-keys ~4 % of chunks onto the wrong fid. Layout C stays as the fallback
  only for headers where A's size test fails (other firmware).
* Limits, from `sdk::ipc_conn::ipc_frame_data` (ELF 0x1c92ab): **chunkTotal ≤ 0x200**,
  **dataLen ≤ 0x3d0**, a group is "last chunk" when `dataLen == 0x3d0`.
* `ipc_frame_data` internals confirm the assembly rule: per-frame slot = 0x2424 B; received-count
  @+0x19c, tot @+0x1a0, total-bytes @+0x1a4, fid @+0x1a8; per-slot `dl` @+0x420+idx*0x10 and
  `cnt` @+0x42c; a 0x200-entry received-flag array @+0x3b7. **Complete when received-count == tot**
  (bytes never reach `want` because of the 2-B-short datagrams) — hence `ipc360.py` keys a dict by
  `idx` and fires on `len(slots) == tot`.
* `net == 3` bypasses reassembly (single chunk → callback straight away, `type 3` arg) — **so net=3
  chunks are a STREAM, not a group: the same fid legitimately repeats.** In one pass of the test
  minute: 1314 audio chunks over 1051 distinct fids, 263 fids sent **twice back-to-back with
  different payloads** (fid delta hist = `+1`×1041, `+0`×263, all payloads distinct). Keying audio by
  `(net,vt,fid)` therefore throws ~20 % of the audio away — `ipc360.py` now keys it `(3,vt,fid,occ)`
  (`occ_key()`) with occurrence = payload identity, and `merge_groups()` dedupes a repeat pass by
  payload equality rather than by fid.
* `net == 1` completion writes `fid+1` to a next-expected-fid field (so out-of-order fid frames
  get dropped by the SDK).
* `fid` is one **SD frame/group counter** (0…1199 for the test minute) shared by all nets: net=0 ∩
  net=1 = ∅ and their union is 1178 of 1200 slots, while net=3 fids *reuse* the same counter (1072 of
  its 1106 fids also exist in net=1). Measured on the union of 3 passes: net0 34 fids (0…1162, an IDR
  every ~34 frames), net1 1144, net3 1106, `fid` union 1179 over span 0…1199.
  **1200 fid slots in a 60 s SD minute ⇒ the SD path runs 20 fps, not the live 25 fps.** Confirmed by
  OSD: `probes/frames/L-n{0,147,294,588,882,1177}.png` read 12:59:59 → 13:00:05 → 13:00:14 → 13:00:2x
  → 13:00:44 → 13:00:59 — monotone, no time jumps, ~19.6 fps throughout, and ffmpeg's own raw-h264
  demuxer lands at 1178 frames = **58.90 s ⇒ 20 fps** (its `25 fps, 40 tbr` probe line is a guess).
  **Concat order = sort by (fid, net, occ).**
* `cnt` is a **session-global chunk counter across all nets** — one pass of the minute: 6988 chunks,
  cnt 0…5771, 5554 distinct ⇒ 218 chunk slots never arrived. That makes completeness *measurable*
  without guessing: audio cnt (union of 3 passes) = 49…1498, 1385 present of 1450 slots ⇒ **only 65
  audio chunks missing**, plus cnt 0…48 (≈2 s) that the camera never sends for this range. Audio and
  video cnt interleave (net0 cnt 0…5651, net1 140…5771). **For net=3 the cnt is the packet's ordinal
  inside the minute**: `cnt - cnt_min` places it on an absolute timeline (the same cnt seen in 2 passes
  IS the same packet, so `audio_track()` dedupes by cnt), which is what keeps audio in step with the
  picture — naive concatenation closes every lost packet as a 40 ms hole and drifts up to ~3.5 s by
  the end of a minute (see §12 item 9).

### What each net carries (this camera, verified on a fresh 4.3 MB stream)

| net | meaning | shape |
|---|---|---|
| 0 | big ~100 KB units, camera marks them **nal_unit_type 5** (the keyframe/IDR groups) | 34 groups, tot 97–160, dlsum 66 k–115 k |
| 1 | slice NALs (nal_unit_type 1) | 971 groups, tot 1–21, dlsum 82–87 for the small ones, 3 k–4 k for bigger |
| 3 | audio, **tot always 1, idx always 0, dl always exactly 320 B** | 1314 chunks/pass = 420,480 B (the old "640" in `wirechk` was two same-fid chunks summed per key, not a 640-B packet) |

Concatenating nets {0,1} in (fid,net) order gives an Annex-B stream ffmpeg parses:
`h264, yuv420p, 1920x1080, 25 fps, 40 tbr` (SPS found), 998–1250 frames, ~8 decode errors per
4.3 MB.

**Audio = AT&T a-law (G.711 / `pcm_alaw`), NOT linear u8 — 8 kHz mono, 320 B = 40 ms, 25 packets/s.**
The camera's OWN live RTSP SDP says so: ffprobe on
`rtsp://admin:<rtsp password>@<LAN ip>/cam/realmonitor?channel=1&subtype=1` decodes as
`Audio: pcm_alaw, 8000 Hz, mono, s16, 64 kb/s`, and net=3 is exactly 320 B × 25/s = **8000 B/s = 64 kb/s**.
Why the old u8 reading is wrong and *loud*: a-law decode magnitude/RMS matches the app clip, while
treating the same bytes as linear u8 gives ±32768 swings and E|Δ|/std = 0.645 vs the app's 0.237 —
that IS the "maximized over full volume + added noise" the viewer heard. `ALAW2S16` in `ipc360.py` is
ffmpeg's own 256-entry pcm_alaw table (generated by feeding bytes 0…255 through a `fmt 6` WAV →
pcm_s16le; ZERO_ALAW = 85 decodes to −8, so a-law silence is byte 0x55 not 0x80).
Per-minute totals for the test minute: **~1450 audio packets** exist (union of 5 passes = 1421 of 1500
slots = 56.8 s on a 59.95 s timeline; union of 7 passes = 1438 of 1500), `--passes 3` recovers 1388.

## 7. Mux (what `assemble()` does — 2026-10-31 rewrite)

```
seg.h264            nets {0,1} groups sorted by (fid,net,occ), slots sorted by idx
seg-audio.raw       net=3 on the ABSOLUTE timeline: slot = cnt-cnt_min (+ head), holes = a-law
                    silence (byte 0x55 × 320); head from --audio-anchor (default fid: fid_first×50 ms)
seg-audio-s16.raw   that timeline decoded a-law → signed 16-bit LE (the mux input)
seg.mkv             ffmpeg -f h264 -i seg.h264 -f s16le -ar 8000 -ac 1 -i seg-audio-s16.raw -c copy
                    (lossless: h264 + pcm_s16le 8 kHz — VLC/mpv)
seg.wav             the audio alone, lossless (plays everywhere)
seg.mp4             -c:v copy -movflags +faststart -strict -1 -ar 8000 -c:a mp3 -b:a 64k
seg.stats.json      the stats dict (groups/partial/decode_errors/audio_*) — camclips.py reads it
```
This ffmpeg build (6.1.1) has **no AAC encoder** (`aac` = decoder-only `D`, `-c:a avcodec_aac` →
"Encoder not found"), so the app's AAC-with-the-noise-stripped look can only be approximated by an
audio FILTER — `--denoise NAME` appends `-af NAME` (verified working names here: `anlmdn`,
`adynamicsmooth`, `afftdn`; `denoise` is NOT a filter, `afftdn=nf=2` → "Numerical result out of range").
Default '' = no filter (the a-law decode already removed the u8 amplitude/noise doubling). mp3 at
8000 Hz needs `-strict -1`; `-ar 44100` resampling is pointless at 8 kHz source and was dropped.
`assemble()` also decodes the raw h264 once (`-f null -`) and reports `decode_errors` honestly —
mux stderr is captured, so camclips.py's log regex can never see it.
Measured durations (test minute 2026-10-30 13:00–13:01): `probes/play-P5b` = 1199 groups / 59.95 s
video, 56.84 s audio, 2 PARTIAL, **1** decode error; `probes/play-P7b` (--passes 7) = 1199 groups /
60.00 s, 60.00 s audio (1438 pkts, 2.16 s of silence holes), 0 PARTIAL, **0** decode errors.

## 8. 40004 — the app's ACK (semantics only approximated)

`W cmd=40004 pw=12 dws: [camId, ctr, user_id]`, 1031 writes in the capture, ctr `0xb42 → 0xc69`
≈ 295 increments for 1031 ACKs (values repeat and occasionally skip, e.g. 0xb44 → 0xb46) ⇒
**a session-global counter, NOT the chunk index**; ~1 increment per 3.5 ACKs. `--ack seq` (global
+1 per ACK) and `--ack cnt` (echo the chunk's own `cnt`) both work; `--ack none` was not tested
end-to-end. Note **no 0x9c44 constant exists anywhere in the SDK** (grep of `probes/an-index.json`
CONSTS = 0 hits) — the cmd value is computed at runtime, so the sender is not findable via the
cmd-table pass.

## 9. SDK message senders (static, from probes/cmdtab.txt + probes/addr-dec2.log)

All camera-side senders live in `sdk::ipc_conn` and use `utils::send_msg_tcp(this, connIdx, cmd, buf, len)`:

| cmd | SDK fn | len |
|---|---|---|
| 40001 | `p2p_stun_msg` / `make_ipc_conn` | — |
| 40002 | `p2p_stun_ack_msg` | 12 |
| 40013 | `connect_video_msg` | 16 |
| 40015 | `ipc_stopplay` | 24 = `[cam, net_type=1, father_id, owner_client_id, video_type=1, 0]`; its own log line is "ipc_stopplay:camera_id %d,net_type %d,dst_peerid %d,src_peerid %d,video_type %d" |
| 40023 | `ipc_send_proc` (re-emits buffered bytes; builds the 20-B header with rev from `$Rev: 4836 $`) | — |
| 20241 | `base_conn::playback_send` | 188 |
| 20203 | `mgr_conn::mgr_p2p_msg` | 40 |

Keepalive senders: `ipc_conn::keep_alive` = cmd **1, 0-byte payload** (TCP); `video_conn::keep_alive`
= cmd 1, **8 B**, via `send_msg_bind_udp`. Received-only cmds (**40004 / 40005 / 40014 / 40087 are
never sent by the SDK** — camera→app only).

`CameraPlay::_publicPlayBack` (ELF 0x1add5d) is the whole playbook in one place:
`video_conn::make_bind_udp` → `mgr_p2p_msg` (wait 1000 ms) → `send_p2p_stun_msg` →
`send_connect_video_msg` (wait 3000 ms) → mgr callback @+0x68 with the `SDK_REMOTE_FILE` (the
20241) → `frame_deck::SetRealPlayCallBack`. Failure codes: `global_set::lasterror = 15000`
(connect-video timeout), `0x2ee3 = 12003` (send data failed).

## 10. Security gate — CLOSED, do not re-break

`SEI_PW` / `PW_PASS_STREAM_SECURITY` (DES) is **not applied for this camera**: the SEI u64 field
decodes to **0**, so chunks arrive in the clear. The DES ladder and the key-schedule attempts in
the older sessions are a dead end; keep that CLOSED.

## 11. Marker tags — CLOSED as an artifact

Old corpus `probes/t2-in.bin` (the pre-fix, truncated-chunk assembly) had 164 hits of
`00` + 3 ASCII decimal digits in 42 KB, values in the 270-value set {0..79, 100..109, 160..179,
200..279, 300..379}. The **fresh, correctly-assembled 4.3 MB stream contains exactly 1** such run
— random-H264 probability for 164 hits is ~0, so the old count was an artifact of the broken
assembly path, not a container field. No "strip the tags" edit is needed to make ffmpeg decode;
the only observed edit that moved ffmpeg MB 12→13 was the 4-tag-bytes→6-zeros experiment, i.e.
noise. If a pre-fix corpus is regenerated, re-check before repeating the claim.

## 12. UNRESOLVED (honest list)

1. **rev-TAG — server-side, unrecoverable from this binary**: the `rev` dword's 4 ASCII bytes in
   40xxx frames (`DEBU`, `NOTI`, `sock`, `nt32`, `37;4`, `(soc`, `..C.`, `..~.`, `lP..`, `mvoi`).
   Static evidence it is NOT the SDK's: `probes/string-vaddrs.txt` (the .so's string table) contains
   **zero** hits for `DEBU`/`NOTI`/`nt32`/`sock`, while the SDK's own outgoing frames always carry
   `rev = 0x6912E4` from `"$Rev: 4836 "$` (`ipc_send_proc`, §9). The frames that carry those tags are the
   ones the **mgr P2P relay** re-frames on our behalf, so the field is that server binary's own memory
   (log strings / stack text). The SDK never parses `rev` on receive (only writes it), so it is
   harmless to echo and there is nothing to implement.
2. **40004 `ctr` rule** (§8) — counter increments ~1 per 3.5 ACKs, values repeat/skip.
3. **A-vs-C layout trigger — CLOSED 2026-10-31** (see §6): there is **no second layout on this camera**;
   "C" rows are ambiguity of the same A header (`cnt` dword reading as ASCII digits), and the tiebreak is
   now simply "A if `dl == len(payload)-28`". Re-measured after the change: fresh wires unchanged (100 % A),
   `parse_chunk`/`wirechk`/`frparse` all patched to match.
4. **20351 — CLOSED 2026-10-31** (static, `probes/mt-dec2.log` + `probes/ghidra-dec4.log`; recheck the
   hex! 0x4f66/0x4f67 = **20326/20327**, 0x4f7e/0x4f7f = **20350/20351**):
   * **20350 (0x4f7e)** is sent by `sdk::base_conn::mgr_tfinfo_set` (ELF **0x1cc40c**, 16-B body, from the
     `client_mgr_set_device_tfcard` struct). `base_data_return`'s cmd switch lists `case 0x4f7e:` in the
     **fall-through-to-default group** (0x4f76/0x4f77/0x4f78/0x4f7a/0x4f7c/0x4f7e/0x4f80/0x4f82…) ⇒ the SDK
     has **no reply handler** for it: fire-and-forget.
   * **20351 (0x4f7f)** = the mgr's **error / "cmd rejected" notification**: `base_data_return case 0x4f7f`
     requires `payload == 0x24` (36 B ⇒ **inner 16 B**, exactly the `[cam,user,cmd,0]` we saw), sets
     `uVar15 = inner+0` (camId), `puVar24 = *(inner+8)` (the rejected-cmd dword read as a pointer), len 0,
     and calls the mgr_conn's **virtual slot +0xa4**. That slot is `mgr_error_process` (ELF **0x1cc396**,
     `d['calls']` scan = 0 static callers ⇒ bound, like `playback_send` at +0x68): it walks
     `global_set::mgr_center`, matches the conn by camId (`conn+0x8 → +4 == camId`) or via its virtual
     +0xa0 predicate, then **`conn+0x10 = -1`** + `SetEventEx(conn+0x27)` = tear this mgr conn down. That is
     why our pre-fix `--pull` (LAN TCP path) got 20351 with body `[cam,user,0x9c42,0]` = "cmd 40002 rejected"
     and then nothing.
     **MEASURED 2026-11-01 22:2x, so the LAN byte path is closed for both framings**: `--ep <LAN ip>:23456`
     over **UDP**, with the normal mgr 20241 release beats, answers **0 chunks** — mgr replies only **20312**
     ("camera P2P public 223.157.134.47:6126 / LAN <LAN ip>:23456", i.e. it restates both endpoints)
     once per beat, then `(camera idle 10.4 s -> stop)`; wire = 244 B, `probes/lan-test/`. So the chunk
     transport exists ONLY on the mgr-announced remote relay (`147.135.x / 148.72.x / 15.204.x :19xxx`).
   * Separate, and easy to confuse with the above: **20326 (0x4f66) = `base_conn::mgr_wifi_get`** (ELF **0x1cb494**,
     8-B body — this is the cmd our `day_map()` already sends) whose reply is **20327 (0x4f67)**: `case 0x4f67`
     wants `payload == 0x88` ⇒ inner **0x74 = 116 B at payload+0x14**, dispatch type **0xc**.
   * Neither 20350 nor 20351 occurs anywhere in the app capture (`grep 20351 probes/*.txt` = 0 hits) ⇒ the
     app never calls `mgr_tfinfo_set`; nothing to implement.
5. **mgr 20402 fields — CLOSED 2026-10-31** (same logs): **20402 = 0x4fb2** is the SDK's *generic mgr
   "getter" request*, sent by `base_conn::mgr_udsign_get` (ELF **0x1ccb94**, envelope **seq 0x15f91 = 90001**)
   and `base_conn::mgr_servertime_get` (ELF **0x1cd004**, envelope **seq 0x15f94 = 90004**) — body always
   **0xec = 236 B** = the standard envelope `[camId, user_id, inner_len 0xdc = 220, seq]` + a
   `memclr(0xd0)`-zeroed 220-B request struct whose **dword[2] = the getter's int arg** (our capture's
   `inner[8] = 1` = `param_2` of `mgr_servertime_get`). **Reply = 20403 = 0x4fb3**, and `base_data_return`
   dispatches mgr replies **by `seq`, not only by cmd**: a second switch has `case 0x15f95:` (= 90005 =
   servertime's reply, seq+1) → `memcpy 0xdc` from `payload+0x24` → callback **type 0x1b** (the same tag the
   getter passes to its own log slot), sibling cases fill the cloud/server structs (`aws_file_path`,
   `access key id`, `secret_access`, `lose time`, and the 3-ASCII-digit `"000"` we saw). So 20402/20403 =
   **server-time + cloud-storage getters** — irrelevant to chunk playback, kept only to be capture-faithful.
5. **20402 fields**: inner_len 220, `inner[8] = 1`, envelope `seq 90004` (and 20403's `seq 90005`,
   body `"000"`). Meaning unknown; verified **not required** for chunks but re-test before trusting.
6. **net=1 fid gaps** and ~284 missing chunks in 10 partial net=0 groups per minute: the gaps come
   in **contiguous idx runs** (e.g. fid=40 missing 16–52, fid=801 missing 87–159), with only 5
   duplicate-idx chunks → i.e. whole bursts of datagrams lost at the relay, not a parse bug.
7. ~~keep_send_beat body~~ → **READ** (`probes/addr-dec4.log`; real dynsym = **ELF 0x1ce3f9**, the
   old 0x1ce3e9 was wrong). `mgr_man::keep_send_beat` is NOT the chunk releaser — it is a **watchdog**:
   for each conn in `global_set::mgr_center` (slot _4_4_ / _0_4_ / _8_4_, alive == conn+0x14 == 3/4)
   it calls `mgr_man::_mgr_check_heartbeat(conn /*ELF 0x1ce459*/, 0x13888|0x13889)`:
   if `conn+0x110 == 2` then while `conn+0x1c` (beat counter) `< 5` call the conn's keepalive slot
   `+0x60` (EPIPE ⇒ 0x20 → tear down); once the counter hits **5** the mgr does
   `conn+0x14 = 0; mgr_conn::mgr_clean(conn)` + `pw_log_action("mgr disconnect please sign in again")`
   and reports error **0x13888 (80008)** / **0x13889 (80009)**. `LocalHeatProc` (0x1ce565) does the
   same for the local conn with limit **9**. So the mgr-side conn self-destructs after 5 idle beats —
   which is exactly why a pull must keep the beat cadence alive (and why our `--idle` stop is safe).
   The 20241-per-beat that releases chunks is reached through the mgr_conn **method slot +0x68**
   (`(**(code**)(*(int*)mgr_conn + 0x68))(mgr_conn, camId, SDK_REMOTE_FILE*)` in
   `CameraPlay::_publicPlayBack`/`publicPlayBackProc`); `base_conn::playback_send` (ELF 0x1bb975)
   has **no static caller** (`d['calls']` scan = 0 hits) — it is bound into that slot, not called
   directly. `mgr_error_process` (0x1bc396) sets slot+0x10 = −1 + fires the conn's event on such errors.
8. **`--passes N` — MEASURED convergence** (each pass re-pulls the same range from a DIFFERENT
   relay endpoint and slots are unioned; `--from-wire A,B[,C]` does the same offline):
   | passes | partial groups | missing chunks | frames | decode errors | container dur |
   |---|---|---|---|---|---|
   | 1 | 11 | 285 | 1005 | 8 | 50.25 s |
   | 2 | 6 | 106 | 1112 | **3** | 55.60 s |
   | 3 | 5 | 99 | 1178 | **1** | 58.90 s |
   | 5 | 2 (fid 562 131/139, fid 719 106/118) | — | 1199 | 1 | 59.95 s |
   | 7 | **0** | — | 1199 | **0** | 60.00 s |
   So the loss really is per-datagram relay loss (different endpoints per pass) and the curve keeps
   improving past 3: `--passes 5` = 1199 of 1200 frames, `--passes 7` measured **0 partial groups /
   0 decode errors** on this minute (`probes/play-P7b`). Nothing is ever truly complete — a few
   chunks never leave the camera for a range (fid=40 stays 88/100). **Recommend `--passes 5`
   (~6 min wall/minute) for anything you watch, `--passes 7` when a minute came back with PARTIAL
   groups and you want it glitch-free** (~6.7 min wall/minute — see item 12's PASS_S).
   **Cheapest glitch-free recipe (measured 2026-10-31 17:xx): pull the SAME minute TWICE at 7 passes,
   strictly SEQUENTIALLY, then merge all 14 wires OFFLINE with `--from-wire` (no network, ~20 s). On 13:27–13:28
   neither run alone was clean (`play-1327d` 1197/1 PARTIAL/1 err, `play-1327e` 1188/1 PARTIAL/0 err — one of
   its passes hit the dead relay `148.72.167.213:19005` and returned 0 chunks) while the 14-wire union
   `play-1327m` = 1197 groups / **0 PARTIAL / 0 errors**. On 12:33–12:34 the 8-wire union `play-1233m` = 1204
   groups / **1 PARTIAL (net=0 fid=401 have 99/110) / 0 errors** — that frame group's last 11 chunks never
   left the camera, so that minute can never reach 0 PARTIAL by re-pulling; judge a minute by `decode_errors`,
   not by PARTIAL alone.
9. **net=3 audio = a-law + absolute timeline — CLOSED 2026-10-31** (the three complaints about
   `2026-10-30_1300-1301.mp4`: "audio maxed over full volume + heavy noise", "audio out of sync",
   "glitches"):
   * codec: **a-law** not linear u8 — see §6 (the camera's own RTSP SDP names `pcm_alaw`/8000 and net=3
     is exactly 8000 B/s; u8 gives the ±32768 swings and E|Δ|/std 0.645 vs the app's 0.237 = the noise).
   * placement: net=3 `cnt` is the packet's ordinal **inside the minute** ⇒ `audio_track()` lays packets
     on an absolute timeline (holes = a-law silence) and the head comes from `--audio-anchor`.
     **`fid` is the right anchor**: RMS-profile windows (0.2 s, ours vs the viewer's app clip on the share)
     pick a best shift of exactly **2.00 s** = the burned-in OSD difference (our frame 0 = 12:59:59,
     app frame 0 = 13:00:01); the `end` anchor gives 16.4 s = wrong. Re-verified with
     `tools/audiosync.py probes/play-P7b/seg.wav <app clip>` → best shift −2.00 s, mean |diff|/maxRMS 0.0099.
   * glitches: chunk loss, not a parse bug — see item 8's table (`--passes 5` = 2 PARTIAL / 1 decode
     error, `--passes 7` = 0 / 0 on this minute). The published clip on the gallery is now `play-P7b`.
   The app's own clip (`2026-10-30-13-04-07-fromIPC360app.mp4`, put on the share by the viewer) stays as
   the reference: 27.65 s, h264 1920×1080 20.04 fps, **AAC (LC) 8000 Hz mono 29 kb/s** (ffmpeg re-encode,
   `Lavf58.20.100`) — ffmpeg warns `TNS filter order 20 is greater than maximum 12` and rejects 1 packet
   when decoding it, which is the app's own doing, not our mux.
10. **ONE mgr session at a time — the pull-lock guard (added 2026-10-31)**: the mgr connection and the
     camera P2P relay are single shared state, so a `--pull`/`--play` started while another one runs destroys
     IT: it dies with `BrokenPipeError` inside `mgr.send` and its day map comes back "0 chunks / no mgr 20204
     reply" (hit twice that day: the two 7-pass re-pulls died while the viewer's page grab held the mgr, and the
     user's page grab of 14:25–14:28 came back 0 chunks while the CLI pull held it — the mgr conn also
     self-destructs after ~5 idle beats). `ipc360.py` now enforces it: `grab_lock()` refuses with rc 1
     ("REFUSED: grab … has been running N min") while `camclips-share/grab.lock` is younger than
     `LOCK_TIMEOUT_S` = 1200 s, touches that lock + a `grab-running.json` sidecar for the duration of its own
     pull, and `grab_release()` unlinks both. Locks older than 1200 s are taken over (the page's own rule).
     `--force` and env `IPC360_LOCK_OK=1` bypass the check — camclips passes `IPC360_LOCK_OK=1` to ITS OWN
     ipc360 child so it is not refused by the lock the page just touched; `--grab-lock -` = don't touch locks.
     `--list` is guarded too: it REFUSES (rc 1) while the lock is held but never TAKES it (a pull would
     otherwise refuse its own lock seconds later), and camclips' `daymap()` refuses to spawn `--list`
     while the lock is alive — it serves the cached `daymap-<day>.json` + a note instead. That is exactly
     what broke the 17:1x run of 2026-10-31: the `/refresh?day=2026-10-31` at 17:12 spawned a SECOND ipc360
     on the same uid, so the running 7-pass pull died in pass 2 (BrokenPipeError, day map "0 chunks /
     no mgr 20204 reply"). Consequence: pulls of different minutes are SEQUENTIAL
     (`probes/pullbatch3.sh`, `pullbatch4.sh`), never concurrent — and no day-map pokes while a pull runs.
11. **the SD does NOT always record at 20 fps — measured 2026-10-31**: `fid` is a continuous FRAME
     index over the pulled range, but the rate differs per minute:
     * 2026-10-30 13:00–13:01 → fid 0…1198 over 60.00 s = **20 fps** (`ffprobe -show_entries stream=r_frame_rate`
       = `20/1`).
     * 2026-10-31 00:42–00:45 → fid 0…1799 over 180.04 s (3 minutes) = **10 fps** (`r_frame_rate 10/1`,
       1798 decodable frames / 179.8 s).
     So **fid→seconds needs that minute's own rate**: `s = fid / fps`, never `fid * 0.05`. Fixed the
     consequence in `assemble()`: the old `vspan_s = (fid_max+1) * 0.05` and the `--audio-anchor fid`
     head (`fid_min_a * 0.05 * rate / 320`) silently assumed 20 fps, i.e. on a 10-fps minute the reported
     span came back HALF (90.0 s for a 180 s clip) and the fid anchor put the audio **half as late as it
     should**. `fps` is now derived from the wire itself, no flag and no assumption — net=3 sends 25
     packets/s (320 B @ 8 kHz = 64 kb/s) and its `cnt` is that packet counter, so
     `fps = 25 * (fid slots) / (cnt slots)`, snapped to the nearest of 10/12.5/15/20/25/30 so a 20-fps
     minute stays bit-identical to the old math (`probes/play-P7` re-assembled offline: 20.0 fps, head
     0.32 s, span 59.95 s — unchanged). `assemble()` publishes `video_fps` in `seg.stats.json` and prints
     `video: N groups, fid A..B = 10.0 fps (…s span)`; camclips' card shows the real rate + the 180.00 s
     timeline. `thumbs()` / the ruler use `ffprobe r_frame_rate` (the container truth) — same answer.
12. **wall-clock per pull + `--seconds` is a per-PASS cap (both measured 2026-10-31 17:5x)**:
     * `PASS_S = 57` s **per PASS per MINUTE** of SD (the wire mtimes inside one grab are exactly 57 s
       apart on three different minutes: p1 17:52:05 → p2 17:53:51 → p3 17:55:36 → p4 17:57:22 → p5
       17:59:08, i.e. ≈ realtime plus a couple of seconds of relay overhead). So 5 passes of a 3-minute
       range ≈ 14 min, `--passes 7` of one minute ≈ 6.7 min. The old "~6 min per minute of SD" wording
       already folded the passes in — it double-stated the cost, which is why the page looked slow but
       was not.
     * `--seconds` caps **ONE pass**, not the whole pull (`pull()` restarts its clock at the top of every
       pass), so a fixed `--seconds 150` silently truncates any range longer than ~2.6 min. camclips'
       `run_grab()` now sizes it to the range — `seconds = max(150, PASS_S * mins * 1.8)` and
       `kill_after = max(LOCK_TIMEOUT, PASS_S*passes*mins*1.6 + 300)` — and `--help` says "cap ON ONE
       PASS of a --pull/--play".
     * Related trap found the same day: page-published clips used to have THIN sidecars (no `audio_codec`,
       no `video_fid_*`, no `partial_list`, `decode_errors` from the log regex fallback). Root cause was
       a call-signature slip, not a race: `seg_stats(segdir)` wants the DIRECTORY holding `seg.*` but
       `run_grab()` passed `work / "seg"` (the PREFIX), so it looked for `grab-X/seg/seg.stats.json` →
       `FileNotFoundError` → the `except` swallowed it and returned `{}`. Confirmed: `seg_stats(work)` =
       34 keys, `seg_stats(work/"seg")` = 0. Fixed by calling `seg_stats(work)`, accepting either
       spelling inside `seg_stats()`, and PRINTING when the file is genuinely missing — a thin card can
       never be silent again. Clips already published thin were repaired by re-publishing from their own
       grab dir: `camclips.py --import camclips-share/grab-<NAME>/seg --name <NAME> …`.

## 13b. Gallery calendar truth rules (added 2026-10-31 22:2x)

* **Opening `/day?day=X` = one mgr day-map query for X** (`render_day(fresh=True)` → `daymap(X)`),
  ~8-12 s wall-clock (login + query). That is what makes "check the day I care about" one click, and
  it re-reads even a CLOSED past day, because a day map can be a partial/failed read and the card
  itself can recycle. `fresh=False` (cache only) is used by `/pull`'s own page render.
* **The calendar NEVER queries.** Cell bars come from `daymap-<day>.json` caches only: green `.cmin`
  = minutes the card holds, dashed `.copen` (flag 0x80) = minute still being written, amber `.cclip`
  = minutes already pulled (from the clip sidecars' `smin/emin`). `no bar` (date never opened) ≠
  `card had nothing` (opened, card empty) — deliberately different, and the /help text says so.
* **A day-map read that did not answer is NOT cached**: `daymap()` requires the mgr's own
  `day YYYY-MM-DD: N chunks, M active minutes` line; no line ⇒ `out["failed"]=True`, nothing written
  to `daymap-<day>.json`, sweep logs `NO DAY MAP from mgr (not cached)`. Caching a silent mgr as an
  EMPTY day would make the calendar lie (a date that has recordings would render "card had nothing").
* **ipc360.py's `--list` checks the grab lock too** (`ipc360.py:1056`, `grab_busy()`), so a sweep that
  holds `grab.lock` gets **REFUSED for every date** unless camclips passes `IPC360_LOCK_OK=1` into the
  child — done in `daymap(force=True)`. Symptom of forgetting it: every date fails in ~0.1 s and
  nothing is cached (a month "swept" in seconds instead of ~4 min).
* Sweep cost, measured 2026-10-31 22:29: **~7.5 s per date**, whole 2026-10 = 31 dates ≈ 4 min; the
  card holds recordings for the WHOLE month (5-67 active minutes/day, 2026-10-11 = 0 = genuinely
  empty), so the October calendar filled with bars for every date. 9 chunks/day every time.

### 13b.1 The cache rule for closed days (added 2026-11-01 00:1x) — a closed day must NOT re-query

* `day_closed(day)` = `day < date.today()`; `card_complete(day, d)` = cached map exists, is not `failed`,
  has an `at`, the date is past, AND `d["at"] >= local end of that date` (`mktime(date)+86400`). All four
  parts matter: "past" alone would trust a map that was read WHILE the day was still open.
* `render_day(day, pull=None, check=None, clock="")` decides with that rule: `check=None` → query unless
  `card_complete`; `check=True` (`/day?day=X&recheck=1`) → always query; `check=False` (`&cache=1`) → never;
  a `pull` page → never (the pull owns the mgr session, and `daymap()` would be REFUSED by ipc360.py's own
  lock check). The header states which happened: `SD card read HH:MM:SS` (fresh) vs
  `SD card read HH:MM:SS · that day is over, cached map used (no camera)`.
* `check this day` chip is drawn ONLY on pages that served the cache — on a page that already queried,
  offering a re-read would be a lie (nothing was skipped).
* Why "read AFTER the day ended" and not "is in the past": 2026-10-31's map was read at 22:39 ON 2026-10-31,
  so minutes 22:40-23:59 were unknown to it — `card_complete` is False and that date still queries on Nov 1.
  Once it is re-read after the day ended, every later open is free (measured: `/day?day=2026-10-28` = 200 in
  ~10 ms, no child process; `/day?day=2026-10-28&recheck=1` = 11.06 s wall-clock = login + one mgr query).
* The card can still recycle an old day (the mgr has done it before on other cameras), which is exactly why
  the opt-in `check this day` exists instead of trusting the cache forever.

### 13b.2 Day-page / calendar UI facts (verified)

* **Whole-cell calendar click**: `<div class="cday"><a href="/day?day=…" title="…">DD<div class="cbar">…marks…</div><div class="cnote">…note…</div></a></div>`
  + `.cday a{display:block;position:relative;width:100%…}`. `.cbar` stays `position:relative` so the
  `.cmin/.copen/.cclip` marks keep anchoring to the bar, not to the cell.
* **PM hours** = per-page opt-in, afternoon row only: `strip_rows(day, d, clips, pm=True)` puts
  `data-pm="1"` on the 720-1439 row, `hlab()` writes `12 PM, 1 PM…` for its hour ticks, `hlabf()` writes the
  full `12:00 PM – 11:59 PM` for the row's own `.srlab`, and MAPJS `fmt(m,pm,full)` mirrors both (JS reads
  `data-pm`, so the zoomed label can't fall back to 24 h). URL `/day?day=X&clock=pm`, nav chips toggle
  `pm hours` ⇄ `24 h`. The morning row is never relabelled — the two rows still can't be confused at noon.
* **months chips** = `.chips` row of `month_step(today, -k)` for k=0..5 plus every month that has a day-map
  cache or a clip; `‹ previous month` and `next month ›` still walk beyond it. Never render a month list
  from data only — a month nobody has data for yet must still be reachable.
* **No `calendar for …` header line**: the wordmark + the camera line + `.nav` chips carry the page, all prose
  lives on /help. `card last read HH:MM` was dropped from the header (the chips row already prints
  `N dates checked`).
* **The pull countdown is measured, and the setup phase is disclosed** — see PROGRESS.md "Gallery UI round"
  and the /help cost paragraph: finished passes drop `pull-*.wire` files in `grab-<clip>/`, their mtime
  spacing = the real pass rate (48-56 s per pass per minute of SD), while `PASS_S = 57` survives only as the
  fallback before any wire exists. A pull also spends minutes BEFORE pass 1 (login, mgr queries, `--idle 15`
  waits: their 2026-10-31 22:45 pull waited ~21 min), so any ETA printed before the first wire is a lie —
  the bar says `setting up / pass 1 streaming (setup + idle waits come first)` instead.
* /help's cost paragraph had a live `%d` in the served HTML (a %-arity slip: that string element has no
  `% (...)` tail). The rule for these page strings: either no `%` conversions at all, or count the args
  exactly — and grep the SERVED page for literal `%d`/`%s`, not the source. Same class of slip on the day
  page: `~%d s wall-clock per minute per pass` fed `PASS_S // 60 or 1` = **1** → the page claimed "~1 s per
  minute per pass" (60x optimistic). Now plain prose: one pass ≈ 50 s per minute of SD, a 5-pass one-minute
  pull ≈ 4 min + the setup phase.
* **Check the month NUMBER, not only its shape.** `re.match(r"^\d{4}-\d{2}$", "2026-13")` succeeds, `month_dates("2026-13")`
  then raises `calendar.IllegalMonthError`, and in the `/sweep` route that happens **after** `grab.lock` was touched
  (the route does `lock.touch()` before building `total = len(month_dates(m))`) → the page 500'd (0 bytes) AND a
  stale lock stayed for the next 20 min. New helper `month_ok(s)` = shape + `1 <= int(s[5:7]) <= 12`, used in
  `render_home`, `render_month`, the `/sweep` route and CLI `--sweep`. Verified `/month?m=2026-13`, `/month?m=2027-01`
  (future month → `month_dates` = [] → weekday row only, no crash), `/month?m=bad` → all 200, falling back to the
  current month, and the journal shows zero handler exceptions.
* **`status_frag()`'s newest-clip chip globs `*.json`**, so its filter must skip `daymap-`, `grab-` **and `sweep-`**;
  forgetting `sweep-` printed `newest clip: sweep-running · landed 19:00 · 0 s · 0 B` the moment a sweep state file
  became the newest json. (`clips_on_disk()` is safe — it globs `*.mp4`.)
* **A restart of `camclips.service` mid-sweep/mid-pull kills the thread but leaves `grab.lock` + `sweep-running.json`
  on the share**, so the LIVE bar keeps counting a dead walk for up to `LOCK_TIMEOUT` (1200 s). After such a restart,
  delete both files (and any half-written `grab-<name>/`); day-map caches the mgr already answered are honest reads
  and may stay.
* **A day map for a month the card knows nothing about is still a VALID read**: `2025-12-01 -> 9 chunks, 0 active
  minutes` (the mgr answers arbitrary months, and its `mgr-day` TZ offset can differ per month — `+0h` in October,
  `-1h` in December, so recheck it per run). That is why `daymap()` caches it (`card had nothing`) and refuses to cache
  only a mgr that never answered.


## 13. How to run (this box, c)

```
cd . && IPC360_PW=your-camera-password python3 ipc360.py --list --day 2026-10-30
cd . && IPC360_PW=your-camera-password python3 ipc360.py --play 13:00-13:01 --day 2026-10-30 \
    --out probes/play-P5b/seg --passes 5 --seconds 150 --idle 15      # -> seg.{mkv,mp4,wav} + stats json
# re-assemble WITHOUT touching the network (fast, use it to compare passes settings):
python3 ipc360.py --from-wire probes/play-P5/pull-20261030-1300-1301-p1.wire,...,p5.wire \
    --out probes/play-P5b/seg
# publish into the LAN gallery (faststart + poster + 9 stills + sidecar json):
camclips.py --import ipc360/probes/play-P5b/seg --name 2026-10-30_1300-1301 \
    --day 2026-10-30 --range 13:00-13:01
# or let the gallery pull it:  camclips.py --grab 2026-10-30 13:00-13:01 --passes 5
# repair a clip published with a thin sidecar (offline, from its own grab dir):
camclips.py --import camclips-share/grab-2026-10-31_0042-0045/seg \
    --name 2026-10-31_0042-0045 --day 2026-10-31 --range 00:42-00:45
# sweep the whole month into the calendar (one card query per date, ~7.5 s each, holds grab.lock):
camclips.py --sweep 2026-10
# audio sync truth test vs the app's clip:
python3 tools/audiosync.py probes/play-P7b/seg.wav camclips-share/2026-10-30-13-04-07-fromIPC360app.mp4
```

The gallery (`camclips.service`, :8795, `camclips.py --serve`) — **the landing page is a CALENDAR,
one month at a time** (`calendar_html()`), so a date can never be "missing": the grid comes from the
REAL dates of the month (`month_dates()`, capped at the box's today), not from what the card said.
Clicking a date goes to `/day?day=…`, **which reads the card for that date** (see below); the calendar
itself never queries anything. ALL the long prose lives on `/help`; pages are labels + chips only:
```
/                       calendar for the CURRENT month (S M T W T F S grid, Sunday first) + LIVE bar
/month?m=2026-09        the calendar for any month (‹ previous month / next month › chips)
/sweep?m=2026-10        ask the card for EVERY date of that month in one walk (thread, holds grab.lock,
                        progress in the LIVE bar + sweep-running.json + sweep.log; ~7.5 s per date)
/day?day=2026-10-30     that day: READS THE CARD on open (fresh=True) + the SD day map as TWO full-width
                        rows (00:00-11:59 above, 12:00-23:59 below = noon never cut) + one row per clip
/clip?name=<NAME>       ONE video (autoplay muted, then unmuted 600 ms later = "starts right away")
/help                   the long text: how the calendar works, colours, wheel zoom, what a pull costs,
                        how to read the card facts (fps varies, a-law audio, holes), two-step delete,
                        repair recipe, CLI
/dl/<NAME>.mp4          save-as download (Content-Disposition), Range-served 206
/delete?name=<NAME>     two-step: shows the files + size, /delete?name=…&confirm=1 removes them
                        (gallery files only — CLI probes/seg.* untouched; the SD minute stays on the card)
/statusfrag /status.json live-bar target (the bar re-polls every 3 s) / machine-readable
/status                 which pull/sweep holds the lock + the newest grab (or sweep) log tail
/refresh?day=…          303 back after refetching day maps (&to=day returns the DAY page; plain returns
                        /). Kept for scripts/repair — no page still links it as a "re-read" button
/pull?day=…&range=…     browser-facing HTML that starts a pull (same as clicking a day-map cell);
                        it stays on that day page and PULLJS walks it to /clip?name=… when the clip lands
/api/daymap?day=        raw day map JSON (programmatic only — never hand the viewer this)
```

Day-map internals (the only part that is not plain HTML): `strip_rows()` emits one
`<div class="sdr" data-lo="0|720" data-span="720">` per 12 h row and puts every card range in the row
that holds its start minute, CLAMPED to that row (a range crossing noon draws in both rows). Cells +
ticks also carry `data-s` / `data-e` / `data-f` / `data-min` and the server-rendered layout is the
WHOLE row — that is the no-JS fallback. `MAPJS` then re-lays those children for its own zoom window
(wheel over a row = zoom that row, double-click = a 30-min window, span clamped 15…720 min inside
that row's own 12 h), forces a **minimum cell width of 1.4 %** so a 1-minute minute you can see is
also clickable, turns `tkb` (15-min ticks) on under 5 h and `tkq` (5-min) under 2 h through the row
classes `b15`/`b5`, and rewrites that row's `.srlab` to the window drawn. Classes JS touches, keep
them: `.tl input.scrub .tmark #st #pullnote .sdr .cell .srlab`.

Restart ONLY `sudo -n systemctl restart camclips.service` — and never while
`camclips-share/grab.lock` is alive (stopping the unit kills the pull's cgroup).

Secrets: the account's email + the camera's password, set as `IPC360_EMAIL` / `IPC360_PW` in your
environment (or `IPC360_CREDS_FILE`, a two-line file you keep OUTSIDE the repo), or written into
`ipc360-config.json` by `ipc360.py --setup`. Nothing in the shipped files identifies your
account, your camera, or your LAN.
Login mgr default `52.8.101.180:19000`; the camera mgr comes back from login.
