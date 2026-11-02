# IPC360 / Puwell “PW” protocol

Reverse-engineered protocol notes for reading SD-card recordings from an IPC360 / Puwell camera without using the mobile app.

> **Scope and confidence**
>
> This document describes the protocol implemented by [`ipc360.py`](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py) and the observations recorded during reverse engineering of the `com.ipc360` Android app and one working Puwell-SDK camera. It is not an official protocol specification. The protocol was verified against one camera in the `com.ipc360` ecosystem; details such as packet layouts, rates, and flag meanings may differ on other firmware.
>
> Three kinds of statements are distinguished throughout:
>
> - **Wire format** — a field layout or value visible in captured packets and implemented by the client.
> - **Observed behavior** — behavior measured on the test camera or in a particular capture. Treat measurements as examples, not universal limits.
> - **Interpretation** — a likely meaning inferred from SDK symbols, disassembly, or repeated captures. Where the evidence is inconclusive, the uncertainty is stated explicitly.
>
> The repository contains the client source and this document, but it does not contain every research artifact referenced below. In particular, many `probes/`, `frames/`, and `tools/` paths refer to the author's local reverse-engineering workspace, not files that can be obtained from the public repository.

## 1. Overview

The protocol has two distinct network roles:

1. **Cloud managers provide the control plane.** They authenticate the account, identify the camera, return the camera-manager address, answer requests for the SD-card day map, and coordinate a playback session.
2. **A P2P relay provides the recording-data path.** During playback, the manager returns a relay IP address and port. The client opens a separate camera/P2P socket to that endpoint and receives the recording chunks there.

The manager connection and the P2P data connection are therefore not interchangeable. A successful login or day-map response does not mean that the recording data is being transferred over the manager TCP connection.

At a high level, the client performs these operations:

1. Connect to the NET manager and authenticate with the enrolled account.
2. Read the account response to obtain the user ID, camera-manager address, custom ID, device ID, and session ID.
3. Connect to the camera manager and issue the camera-registration/handshake messages needed for a day-map request.
4. Send command `20400` and collect the manager's `20401` index chunks. These chunks describe which minutes are present on the SD card.
5. To download a time range, send a `20241` playback request and ask the manager for a P2P endpoint with `20203`. The manager answers with `20204`.
6. Open the P2P socket, perform the camera-side handshake, and process `40005` recording chunks. Echo `40002` messages, acknowledge chunks with `40004`, and keep both sides alive.
7. Periodically send another `20241` request with control value `0x0b`. On the tested camera, these requests act as a playback-release/flow-control signal and cause more chunks to arrive.
8. Reassemble video groups, put audio packets on a timeline, and write playable files.

The camera is not reconfigured by this process. The tool reads recordings through the same account and cloud services used by the app; it does not write to the camera's settings or SD card.

## 2. Terminology

| Term | Meaning |
|---|---|
| **NET manager** | The first cloud endpoint. It handles the account login exchange. |
| **Camera manager / IOT manager** | The manager endpoint returned by login. It handles camera-session messages and day-map/playback coordination. |
| **P2P relay** | The per-session network endpoint returned by command `20204`. The client sends the P2P handshake to this endpoint and receives recording chunks through it. |
| **Control plane** | Manager traffic used for authentication, discovery, day-map requests, and playback coordination. |
| **Data plane** | The P2P socket carrying the recording chunks. |
| **Frame** | A protocol message consisting of a 20-byte outer header and a payload. Do not confuse a protocol frame with a decoded video frame. |
| **Envelope** | A 16-byte manager-payload prefix containing camera ID, user ID, inner-payload length, and sequence number. Only some manager commands use it. |
| **ShmTime** | A 24-byte structure containing six little-endian 32-bit integers: year, month, day, hour, minute, and second. |
| **Day map / index** | The manager's description of the minutes recorded on a particular SD-card date. |
| **Chunk** | A `40005` message carrying one fragment of a video group or one audio packet. |
| **Group** | A set of chunks identified by fields such as `net`, `vt`, and `fid`. Video groups are reassembled from their indexed chunks. |
| **`fid`** | Frame/group identifier used by the camera's SD playback stream. It is not, by itself, a reliable timestamp. |
| **`cnt`** | A counter carried in the chunk header. Its relationship to the stream timeline is not fully reconciled across all observations; see [Chunk counters and timing](#chunk-counters-and-timing). |
| **`net`** | A stream identifier. On the tested camera, `0` and `1` carry H.264 video and `3` carries audio. |
| **`vt`** | A 16-bit field that is preserved in group keys. Its meaning has not been established here. |
| **`idx` / `tot`** | Chunk index and total chunk count for a reassembled group. |
| **`dl`** | Data-length field in a chunk header. |
| **IDR** | Instantaneous Decoder Refresh frame; an H.264 keyframe. |
| **NAL / NAL unit** | Network Abstraction Layer unit in an H.264 elementary stream. |
| **Annex B** | H.264 byte-stream format with start codes between NAL units. |
| **G.711 A-law** | Companded audio encoding used by the tested camera's `net=3` stream (`pcm_alaw`). |
| **SDK** | Software development kit. Here, the reference is Puwell's native networking library, `libpwnativenetsdk.so`. |
| **LE / little-endian** | Multi-byte integer encoding in which the least-significant byte is stored first. |
| **EOF** | End of stream. A socket timeout is not necessarily EOF. |

## 3. Network endpoints and roles

The endpoints below were observed during testing. The relay address is dynamic and must be obtained from the manager; do not hard-code the example relay.

| Connection | Endpoint observed | Transport | Purpose |
|---|---|---|---|
| NET manager | `52.8.101.180:19000` | TCP | Account login (`10030`, `10001`). |
| Camera manager (IOT) | `54.241.6.178:19001` in the test session | TCP | Camera registration, day-map requests, and playback coordination. The address is returned by login. |
| P2P relay | Examples from different captures: `147.135.38.160:19305` and `147.135.38.160:19405` | UDP datagrams in the tested path | P2P handshake and `40005` recording data. The address and port vary by session. |

The test camera was a Puwell-SDK “PIR” camera on a private LAN, using P2P LAN port `23456`. The device used `father_id = 41` (`0x29`, identified with `global_set::father_id`). Camera ID, user ID, device ID, serial, and local address are installation-specific and are not constants of the protocol.

The login response contains the camera-manager address. The client then asks the camera manager for a P2P endpoint. The manager's `20204` response contains the relay address at payload offset `+0x10` and the relay port at `+0x14`; the two port bytes are interpreted in network byte order (the original notes call this byte-swapped relative to the surrounding little-endian fields).

On the tested camera, a direct attempt to use the advertised LAN endpoint (`<LAN IP>:23456`) did not return recording chunks, even when tried over UDP with the normal release requests. The manager answered with `20312` and restated public and LAN endpoints, but the chunk stream still came only from the manager-announced remote relay. This is a result for the tested camera/session, not proof that every Puwell device rejects direct LAN playback.

## 4. Outer frame format

All observed manager and P2P messages use the same 20-byte outer header. The payload format depends on the command; it is not generally JSON.

```text
Offset  Size  Type     Meaning
------  ----  -------  ----------------------------------------------
0x00    4     u32 LE   Magic: 0xFFEEDDCC (wire bytes CC DD EE FF)
0x04    4     u32 LE   Command number
0x08    4     u32 LE   Revision / relay tag
0x0C    4     u32 LE   Total frame length, including the 20-byte header
0x10    4     u32 LE   Reserved field; zero in the client implementation
0x14    ...            Command payload
```

The client builds the header with:

```python
struct.pack("<IIIII", 0xFFEEDDCC, cmd, rev, len(payload) + 20, 0) + payload
```

The normal SDK revision value is `0x6912E4`, derived from the embedded SDK string `$Rev: 4836 $` as `0x690000 | 4836`. In a standard little-endian header, that value is encoded as bytes `E4 12 69 00`.

### 4.1 The revision field on relay traffic

On ordinary outgoing SDK frames, the revision field contains the SDK revision. Some received P2P frames instead contain four bytes that look like ASCII tags when the 32-bit value is displayed as little-endian bytes. Observed examples include `DEBU`, `NOTI`, `sock`, `nt32`, `37;4`, `(soc`, `..C.`, `..~.`, `lP..`, and `mvoi`.

The current evidence points to these values being introduced by the manager/P2P relay when it wraps or forwards traffic, rather than by the client SDK. The SDK's string table reportedly has no matching strings, and the receive path does not appear to interpret the field. The exact server-side meaning is unknown. The client logs the value for diagnosis but does not need to decode it.

### 4.2 Manager envelope

Many manager commands place an additional 16-byte envelope at the beginning of the outer frame's payload:

```text
Offset  Size  Type     Meaning
------  ----  -------  ----------------------------------------------
0x00    4     u32 LE   Camera ID
0x04    4     u32 LE   User ID
0x08    4     u32 LE   Length of the inner payload
0x0C    4     u32 LE   Sequence number
0x10    ...            Inner payload
```

The client constructs it as:

```python
struct.pack("<IIII", cam, user, len(inner), seq & 0xFFFFFFFF) + inner
```

Not every manager command is enveloped. In particular, the `20241` playback request described below is an un-enveloped 188-byte payload.

## 5. Account login (NET manager)

The login exchange uses command `10030` to obtain a salt and command `10001` to submit the password digest.

### 5.1 Salt request — command 10030

Payload:

```text
email[64] | 01 00
```

The email is UTF-8 encoded, zero-padded to 64 bytes. The two trailing bytes are the literal `01 00`.

The reply contains a 15-character ASCII salt. The current client strips trailing NUL bytes from the reply before using it.

### 5.2 Login request — command 10001

The password digest is calculated in two steps:

```text
md5_password = lowercase_hex(MD5(password_bytes))
digest       = lowercase_hex(MD5(ASCII(md5_password) + salt_bytes))
```

The command `10001` payload is 332 bytes:

```text
Offset  Size  Meaning
------  ----  -----------------------------------------------------
0x00    64    Email, NUL-padded
0x40    32    Lowercase hexadecimal digest, ASCII
0x60    32    Unused / zero in this request
0x80    14    Fixed tail bytes
0x8E    190   Zero-filled remainder
```

The fixed tail, copied verbatim from the capture, is:

```text
00 00 01 00 01 00 09 00 00 00 30 30 30 00
```

### 5.3 Login reply

The reply fields used by the client are:

| Payload offset | Size | Type | Meaning |
|---:|---:|---|---|
| `+0x00` | 4 | u32 LE | `user_id` |
| `+0x04` | 4 | bytes | Manager IP address as four dotted-decimal octets |
| `+0x08` | 4 | u32 LE | Manager port |
| `+0x0C` | 4 | u32 LE | `custom_id` |
| `+0x10` | 4 | u32 LE | `dev_id` |
| `+0x14` | 4 | u32 LE | Zero in the observed reply |
| `+0x18` | 128 | bytes | Base64-looking session ID, NUL-padded |

The current client considers the login successful when the session ID is non-empty, `user_id` is non-zero, and the printable reply does not contain `not correct`.

The default NET-manager endpoint observed during testing is `52.8.101.180:19000`. The camera-manager address and port are taken from the login response rather than assumed to be fixed.

### 5.4 Command 20200 helper

`ipc360.py` also contains a `mgr_sign_in()` helper that builds a 148-byte command `20200` payload:

- bytes `0..3`: two little-endian 16-bit values, both `1`;
- bytes `4..7`: `custom_id`;
- bytes `8..11`: owner/user ID;
- bytes `12..19`: zero;
- bytes `20..147`: session ID, up to 128 bytes.

The helper sends command `20200` and drains until it receives command `20200`. It is present in the source, but it is not part of the active `login()` function described above. Do not assume it is required for the demonstrated day-map or playback path without testing the call sequence being used.

## 6. Day-map request and response

The day map is retrieved from the camera manager using command `20400`. It is not fetched from the camera's LAN web or RTSP interface.

### 6.1 Camera-registration sequence

Before querying a day, the current `day_map()` implementation sends these manager messages, in this order:

```text
Command  Payload
-------  -------------------------------------------
1        [user_id, 0]                         keepalive
20202    [cam_id, manager_ip_as_4_bytes, 1, 10]
20357    [cam_id, user_id, 1]
20326    [cam_id, user_id]
20352    [cam_id, user_id, 0x7D]
20282    [cam_id, user_id, 0, 0]              16-byte payload
```

The `20282` request is 16 bytes. It was observed in the app capture immediately before the day-map query. The client drains pending manager replies after this sequence so the day-map receive loop starts with a clean stream.

### 6.2 Day-map request — command 20400

Command `20400` is sent with the standard 16-byte manager envelope. Its inner payload is 68 bytes. The current helper uses envelope sequence number `20373` by default:

```text
[cam_id, user_id, user_id, 0, 0]  ShmTime(day)  24 zero bytes
```

Each integer in the five-field prefix is a little-endian 32-bit value. `ShmTime` is six little-endian 32-bit integers:

```text
[year, month, day, hour, minute, second]
```

For a full-day request, hour, minute, and second are zero. For example, year 2026 is decimal 2026 (`0x000007EA`). The final 24 zero bytes are described in the capture notes as an empty range.

### 6.3 Day-map response — command 20401

The manager returns one or more `20401` frames. The payload begins with the manager envelope, followed by an inner response containing:

```text
[cam_id, user_id, dayEpoch, chunkCount, index, subLength, slotBytes...]
```

The client reads these fields at the following offsets in the full `20401` payload (including the 16-byte envelope):

| Payload offset | Size | Meaning |
|---:|---:|---|
| `+0x10` | 4 | Camera ID |
| `+0x14` | 4 | User ID |
| `+0x18` | 4 | `dayEpoch` value |
| `+0x1C` | 4 | Number of index chunks expected |
| `+0x20` | 4 | Index number (`idx`) |
| `+0x24` | 4 | Slot length (`subLength`) |
| `+0x28` | variable | Slot bytes |

The envelope sequence number is returned at payload offset `+0x0C`; the observed response sequence is request sequence plus one.

The slot meanings observed on the test camera are:

- `idx == 1`: an ASCII index-version string, up to 16 bytes.
- `idx == 2..9`: 180 one-byte minute flags per slot. The first minute represented by a slot is `(idx - 2) * 180`.

Eight 180-minute slots cover a 24-hour day. The client builds a 1,440-element array and writes each non-zero flag at its corresponding minute.

The client currently reads at most 120 manager frames while waiting for the index. It stops earlier when the number of received distinct index slots reaches the advertised chunk count.

### 6.4 Minute flags: observed values

The tested camera returned `0x82` for a closed/completed minute and `0x80` for a minute that was still being written while the camera was recording. A pull of an open minute returned a short result, so the gallery distinguishes it visually and does not offer it as a normal pull target.

**Do not infer that the lowest bit is the open/closed marker.** The observed values differ by `0x02`; the protocol notes' earlier description of this as the “low bit” is inaccurate. The complete flag bitfield has not been decoded. Treat `0x82` and `0x80` as observed values for this camera, not as a universal enum.

### 6.5 Day-map date and timezone

`ShmTime` is built from the requested calendar date. The reverse-engineering notes report that the test camera ran at UTC−4 during the relevant October 2026 captures. They also make two inconsistent statements about `dayEpoch`: one calls it UTC midnight, while the source comment for `shm()` calls it camera-local midnight. The client reads the `dayEpoch` value returned by the manager and reports the observed offset; it should not be treated as a proven UTC timestamp until that discrepancy is resolved. Re-check the offset for the date being queried, particularly across daylight-saving changes or when using another camera.

### 6.6 Example result

For 2026-10-30, the test camera returned nine index chunks and 47 active minutes. The observed active times included `02:20`, `06:16`, `07:34`, `08:25`, `08:47`, `08:59`, `10:58`, `11:17`, `11:32`, `11:38–11:39`, `12:31`, `12:33`, `12:37`, `13:00–13:01`, `13:27`, `13:30`, `13:35`, `14:18`, `15:43`, `16:26`, `16:53`, `16:55`, `17:10`, `17:27`, `17:30–17:31`, `18:00`, `18:26–18:27`, `18:30`, `18:33`, `18:43`, `19:26–19:27`, `20:03`, `20:13`, `20:59`, `21:21`, `21:44`, `21:57–21:58`, `22:01`, `22:11`, and `22:23`. The flag recorded for those completed minutes was `0x82`.

This is a sample result from one camera and one date, not a fixed number of index chunks or a guaranteed recording schedule.

### 6.7 Command 20402 after the index

The current implementation optionally sends command `20402` after receiving the day-map slots. The request is enveloped with sequence `90004`; its 220-byte inner payload begins with `[cam_id, user_id, 1]` followed by 208 zero bytes. The client then waits for up to three manager replies, each with a six-second receive timeout.

The response observed in the capture is command `20403`, sequence `90005`, with a body described as `[cam_id, user_id, 0, 0, 0, 0, "000"]`.

The original notes call this an “index acknowledgement” in the day-map section. Their later static-analysis section instead identifies `20402` (`0x4FB2`) as a generic manager getter used by `mgr_udsign_get` (envelope sequence `90001`) and `mgr_servertime_get` (sequence `90004`). Both use a 236-byte total manager payload: the 16-byte envelope plus a 220-byte inner structure. The inner structure is zero-initialized, with `dword[2]` holding the getter argument; in the observed request, `inner[8] = 1`. The reply is command `20403` (`0x4FB3`), and the SDK dispatches some replies by envelope sequence rather than command number alone. The `mgr_servertime_get` reply uses sequence `90005` and dispatches through case `0x15F95`; the related getter family fills server/cloud fields such as an AWS file path, access-key ID, secret, loss-time value, and the three-character string `000`. These details suggest that `20402`/`20403` are server-time and cloud-storage getter messages, not a dedicated day-map ACK. Those two descriptions are not fully reconciled. The client currently sends the request because it was present in the capture and because the author observed playback following this exchange, but the command's exact role in the day-map sequence should remain **unresolved**. Avoid documenting it as a proven index-ACK protocol message.

## 7. Playback setup and control flow

Playback uses the manager for session coordination and the P2P relay for chunk transport.

### 7.1 Playback request — command 20241

The SDK sends command `0x4F11` (`20241`) with a fixed 188-byte `SDK_REMOTE_FILE` payload. This payload is **not** wrapped in the 16-byte manager envelope.

The correct field layout, as implemented by `remote_file()`, is:

```text
Offset  Size  Type     Meaning
------  ----  -------  ---------------------------------------------
+0      4     u32 LE   Camera ID
+4      4     u32 LE   User ID
+8      4     u32 LE   Custom ID (defaults to user ID in this client)
+12     4     u32 LE   Device field; set to 0
+16     4     u32 LE   Control value
+20     112   bytes    Name field; zero-filled unless supplied
+132    24    ShmTime  Start time
+156    24    ShmTime  End time
+180    4     u32 LE   Constant 1
+184    4     u32 LE   Tail value
```

Both `ShmTime` values use six little-endian 32-bit integers. Passing `None` writes 24 zero bytes for that time.

The earlier protocol table placed a 32-bit field at `+120` and described the filename as a “112-char name tail.” That offset is incorrect. The source writes the 112-byte name field at `+20`, so it occupies bytes `+20..+131`; the start time begins at `+132`.

### 7.2 Control-value mapping

The reverse-engineered SDK function `base_conn::playback_send` maps its integer argument to the control field as follows:

| Argument passed to SDK function | Value written to `ctrl` |
|---:|---:|
| `0` | `2` |
| `1` | `3` |
| `2` | `0x0A` |
| `3` | `0x0B` (play) |
| `4` | `4` |
| `999` or other default case | `0` |

This mapping describes the SDK's argument-to-wire conversion. It is distinct from the `ctrl` value already stored in the 188-byte packet. The Python client builds the packet directly and uses the wire values below.

### 7.3 Observed playback sequence

The capture sequence, as reproduced in `pull()`, is:

1. Send manager command `20241` with `ctrl=0x0B`, empty start/end times, and tail `0x23`. The original notes call this the list probe or release form.
2. Send manager command `20203` (`mgr_p2p_msg`, 40-byte payload) to request the P2P endpoint.
3. Receive manager command `20204` (40-byte reply). Read the relay IP from payload bytes `+0x10..+0x13` and the relay port from `+0x14..+0x15` as a big-endian 16-bit value.
4. Open the P2P socket to the returned endpoint.
5. Send the camera-side handshake messages described below.
6. Echo each `40002` message back verbatim.
7. When `40014` arrives, send `20241` with the requested range and `ctrl=0` for the first event, then `ctrl=4` for the second. The capture had a third `40014` but no corresponding `20241` request. Each `40014` payload was 24 bytes and was observed as `[father_id, user_id, 0, cam_id, 1, 0]`.
8. Continue keepalives and send `20241` with `ctrl=0x0B` on each playback beat. On the tested camera, this request is what releases additional `40005` chunks.
9. Acknowledge each received `40005` with command `40004` unless ACKs have been disabled.
10. Stop after the configured idle interval with no new chunks, or when the per-pass time limit is reached.

In the measured capture, each `ctrl=0x0B` beat released roughly 250–550 chunks. The beat interval in the client is approximately 1.6 seconds. One analysis of 20 beats measured 249–552 chunks per beat and found no camera-idle interval longer than 0.31 seconds while data was flowing. This is an observed behavior for the tested camera; it should not be assumed to be a universal server-side flow-control specification. The optional `--early` path, which sends the `ctrl=0x0B` request immediately after `40013`, was not needed in the capture-faithful test; the normal sequence returned more chunks (6,575 versus 6,487 in the comparison recorded in the original notes).

### 7.4 P2P handshake commands

The P2P socket sends these payloads:

| Command | Payload length | Payload |
|---:|---:|---|
| `40001` | 12 bytes | `[cam_id, user_id, father_id]` as three little-endian u32 values |
| `40013` | 16 bytes | `[user_id, father_id, 0x10001, cam_id]` as four little-endian u32 values |

The capture summary describes `40001` three times and `40013` once, and the current `ipc360.py` does the same: the `40001` frame is sent inside a three-iteration loop, the single `40013` frame is sent once immediately after that loop (`ipc360.py`, `pull()`). Whether the relay tolerates or ignores the repeated `40001` frames has not established; only "three, then one" is the reproduced, working behavior.

### 7.5 Camera and manager keepalives

During playback, the client sends a keepalive about every 1.6 seconds:

- to the P2P socket: command `1`, 8-byte payload `[user_id, cam_id]`;
- to the manager socket: command `1`, 8-byte payload `[user_id, 0]`;
- to the manager socket, after the `40014` handshake has begun: command `20241`, `ctrl=0x0B`, requested range, and tail `0x23`.

The capture's P2P wire bytes show `[user_id, cam_id]`, even though the SDK symbol `video_conn::keep_alive` appears to construct the corresponding structure as `[camera_id, owner_client_id]`. The wire observation is used by the client.

A reverse-engineered manager watchdog, `mgr_man::keep_send_beat`, is not itself the chunk releaser. Its heartbeat logic checks manager connections and eventually closes a connection that remains idle for too many beats. The `20241` send through the manager connection's playback method is the operation associated with releasing chunks. Keeping the beat cadence active prevents the manager session from timing out during a long pull.

### 7.6 Stop command

Command `40015` is identified in the SDK as `ipc_stopplay`. Its 24-byte payload is:

```text
[cam_id, net_type=1, father_id, owner_client_id, video_type=1, 0]
```

The associated SDK log format is:

```text
ipc_stopplay:camera_id %d,net_type %d,dst_peerid %d,src_peerid %d,video_type %d
```

The current Python pull loop treats an incoming `40015` as a close/stop indication and exits.

## 8. P2P transport and frame parsing

### 8.1 Datagram transport

The tested camera data path uses a UDP datagram socket (`sendto`/`recvfrom` in the capture and `SOCK_DGRAM` in the client). This differs from the manager connections, which use TCP streams.

A UDP datagram is not necessarily a complete protocol frame. The capture contains cases where a `40005` frame is two bytes shorter than the length in its own outer header, and the next frame begins in the next datagram. The receiver therefore needs a carry-over buffer and must be able to recover a frame boundary across datagrams.

The client uses `recv(65536)` for UDP. A smaller receive buffer can truncate a datagram; the excess bytes are discarded by the socket API and cannot be recovered by the parser.

A socket timeout is not EOF. `read_msgs()` distinguishes a timeout (no new data yet) from EOF and returns the previous unconsumed bytes so they can be combined with the next datagram.

### 8.2 Magic-delimited parsing

`split_frames()` searches for the four on-wire magic bytes `CC DD EE FF` and uses the next magic marker as a recovery boundary when the declared total length does not fit. It retains an incomplete trailing frame for the next read.

This behavior is needed because:

- in one capture, 1,846 of 4,729 chunk datagrams were two bytes short of the header's declared total length;
- the following frame began in the next datagram;
- some relay traffic contained a 20-byte wrapper header followed immediately by an inner frame, with magic at offsets `+0` and `+20`. In that case the wrapper can produce an empty payload while the inner frame parses normally.

The parser should not blindly discard bytes based only on the outer `tot` field. It must preserve incomplete tails and resynchronize on the magic marker.

The original pull-function comment says “one frame per datagram,” but the parser's measured behavior and implementation explicitly support frames that continue across datagrams. The latter is the safer description of the observed transport.

## 9. Recording chunk format — command 40005

Command `40005` carries the recording data. Its payload begins with a 28-byte header. The currently preferred layout (A) is:

```text
Offset  Size  Type     Meaning
------  ----  -------  -----------------------------------------------
+0      4     u32 LE   Camera ID
+4      4     u32 LE   father_id
+8      2     u16 LE   net
+10     2     u16 LE   vt
+12     4     u32 LE   fid
+16     4     u32 LE   cnt
+20     2     u16 LE   idx
+22     2     u16 LE   tot
+24     4     u32 LE   dl
+28     ...            Chunk data
```

The parser's size check for layout A is:

```python
dl == len(payload) - 28
```

### 9.1 Layout selection and the legacy C fallback

The original research identified a second candidate layout, called C, in which the four bytes at offset `+16` look like three ASCII decimal digits plus padding. The current parser checks the data length for both candidates and gives layout A precedence whenever A's size test passes.

The tested fresh minute had 6,988 chunks and all 6,988 satisfied layout A's size test. In the older 2,510-chunk dump, all chunks also satisfied that test; 109 rows previously labelled C were the same layout-A header whose `cnt` bytes happened to look like three ASCII digits. The earlier discriminator `cnt == fid * tot + idx` was rejected: the counter does not obey that equation consistently.

The C fallback remains in `parse_chunk()` for packets where the layout-A size test fails. The current parser reads the C candidate's digit bytes at `+16..+18`, `idx` at `+22`, `tot` at `+24`, and `dl` as a 32-bit value beginning at `+26`, while the data slice still begins at `+28`. That fallback is retained for compatibility with a possible other firmware layout; it has not been demonstrated as a distinct layout on the tested camera. Its unusual overlapping read should not be presented as a fully established general-purpose wire structure.

If both A and C size checks pass, the parser chooses A. If neither passes, the current code falls back to A when the A `dl` value is non-zero and otherwise selects C. That last branch is a parser fallback, not a proven protocol discriminator.

### 9.2 Chunk and group limits

Static analysis of `sdk::ipc_conn::ipc_frame_data` identified these limits:

- `chunkTotal <= 0x200` (512);
- `dataLen <= 0x3D0` (976 bytes);
- a group is marked as the last chunk when `dataLen == 0x3D0`.

The SDK's per-frame slot is `0x2424` bytes. The reverse-engineered structure has these fields:

| SDK structure offset | Meaning |
|---:|---|
| `+0x19C` | Received-chunk count |
| `+0x1A0` | Expected total chunk count |
| `+0x1A4` | Total bytes |
| `+0x1A8` | `fid` |
| `+0x420 + idx * 0x10` | Per-slot `dl` |
| `+0x42C + idx * 0x10` | Per-slot `cnt` |
| `+0x3B7` | Start of a 0x200-entry received-flag array |

The SDK considers a group complete when the received-chunk count equals `tot`. Because of the observed short datagrams, the sum of the advertised `dl` values does not necessarily match the bytes actually received. The Python client therefore considers a group complete when it has all `tot` distinct `idx` slots, then concatenates them in index order.

### 9.3 Meaning of `net` on the tested camera

The observed stream uses these `net` values:

| `net` | Observed content | Observed packet/group shape |
|---:|---|---|
| `0` | H.264 IDR/keyframe groups; the camera marks the NAL unit type as 5 | Large groups, roughly 100 KB in the measured sample; 34 groups, `tot` 97–160, summed `dl` approximately 66–115 KB |
| `1` | H.264 slice NAL units, type 1 | 971 groups in the measured sample; `tot` 1–21, with small groups around 82–87 bytes and larger groups around 3–4 KB |
| `3` | Audio | `tot=1`, `idx=0`, and `dl=320` bytes in the measured sample; one packet is 40 ms at 8 kHz |

These shapes are observations for the tested camera and capture, not formal constraints for all firmware.

### 9.4 `net=3` audio bypasses normal group assembly

The SDK's `ipc_frame_data` path treats `net == 3` differently: it bypasses the normal indexed-slot assembly and delivers the packet directly to the callback. On the tested camera, audio packets have `tot=1` and `idx=0`.

A single pass can contain more than one audio packet with the same `fid`. In one measured minute, 1,314 audio chunks appeared across 1,051 distinct `fid` values; 263 `fid` values appeared twice in succession with different payloads. A grouping key based only on `(net, vt, fid)` would discard valid audio. The client adds an occurrence suffix and compares payload identity so that a repeated pull can be deduplicated without collapsing distinct packets from the same `fid`.

### 9.5 Video ordering and `fid`

For the tested camera, concatenating `net=0` and `net=1` groups in `(fid, net)` order, with chunks inside each group sorted by `idx`, produces an H.264 Annex-B stream that FFmpeg can parse.

The fuller client ordering includes an occurrence component when required: `(fid, net, occ)`. The occurrence component is especially important for `net=3` audio, where the same `fid` may identify multiple distinct packets.

The measured `fid` values form a shared SD-frame/group counter across nets `0`, `1`, and `3`, but the sets overlap differently. In one capture, `net=0` had 34 groups (IDs 0–1162, with an IDR approximately every 34 frames), `net=1` had 1,144 groups, and `net=3` had 1,106 distinct `fid` values. The union of `net=0` and `net=1` IDs covered 1,178 of 1,200 expected slots; `net=3` reused many `net=1` IDs. Across the combined measurements, the union spanned 1,179 IDs over 0–1199. The original one-minute sample therefore had 1,200 expected `fid` slots, but this does **not** prove that every SD minute is recorded at 20 frames per second; see [Frame rate and time mapping](#frame-rate-and-time-mapping).

## 10. Chunk counters and timing

The protocol notes contain two claims about `cnt` that should not be collapsed into a single rule:

- the chunk-analysis section describes `cnt` as a session-global counter across nets;
- the audio section uses `net=3` `cnt` as an ordinal on the requested minute's audio timeline.

The current assembler places audio using `cnt` and deduplicates a repeated packet by `cnt`, while the parser preserves the raw value. The measurements cited in the original notes include audio counters from a range that appears to begin at 49 and video counters interleaved with audio counters. This is consistent with a counter shared across at least part of the transfer, but does not fully establish how it is initialized or whether it resets for every requested range, relay, or session.

**Safe implementation rule:** preserve `cnt` exactly, do not derive it from `fid`, `idx`, or `tot`, and do not assume `cnt == fid * tot + idx`. The client may use the observed counter sequence to place audio packets, but the counter's general scope remains an open protocol question.

### 10.1 Frame rate and time mapping

The original 2026-10-30 13:00–13:01 sample contained 1,199 decoded video groups over approximately 60 seconds, corresponding to 20 fps. The on-screen timestamp (OSD) advanced monotonically across sampled frames, at approximately 19.6 fps. FFmpeg reported a 25 fps probe line in one test, but that was a demuxer guess and not the actual SD cadence. A different sample, 2026-10-31 00:42–00:45, contained 1,799 `fid` slots over about 180 seconds, corresponding to 10 fps.

Therefore, `fid` must not be converted to time using a fixed `fid * 0.05` formula. The current assembler estimates the video's frame rate from the observed video `fid` span and audio packet-counter span. The documented calculation is approximately:

```text
fps = 25 * (number of video fid slots) / (number of audio cnt slots)
```

The result is snapped to the nearest rate in `10, 12.5, 15, 20, 25, 30` fps. This relies on the observed 25 audio packets per second and should be treated as an implementation inference, not an invariant of every camera. The assembler writes the derived value as `video_fps` in `seg.stats.json`; the gallery also reads the rate reported by `ffprobe` for the assembled container.

## 11. Audio format and reconstruction

### 11.1 Codec

The tested camera's `net=3` data is G.711 A-law (`pcm_alaw`), not unsigned linear 8-bit PCM. The camera's own live RTSP Session Description Protocol (SDP) reports `pcm_alaw`, 8,000 Hz, mono. The P2P samples are 320 bytes each:

```text
320 bytes / 8,000 samples per second = 0.040 seconds per packet
25 packets per second * 320 bytes = 8,000 bytes per second
8,000 bytes per second * 8 bits = 64 kbit/s
```

A-law must be decoded with the A-law lookup table used by the client (`ALAW2S16`). The table is the FFmpeg `pcm_alaw` decode mapping for all 256 input byte values. In the original analysis it was generated by writing byte values 0–255 into a WAV file marked as A-law (`fmt` tag 6), then asking FFmpeg to convert it to `pcm_s16le`. Treating the bytes as linear unsigned 8-bit samples produces excessive amplitude and noise: the measured normalized error statistic was 0.645 for the raw-u8 interpretation versus 0.237 for the app reference. The A-law byte closest to silence in the decoder table is `0x55` (decimal 85); it decodes to −8, so the client uses `0x55` as the fill byte for missing audio packets.

### 11.2 Audio timeline

The client does not simply concatenate the audio packets it receives. It puts them on an absolute packet timeline using `cnt`, fills missing 320-byte packet slots with A-law silence (`0x55` repeated 320 times), and then decodes the result to signed 16-bit little-endian PCM for muxing.

The initial audio offset is controlled by `--audio-anchor`. The default `fid` anchor uses the first video `fid` and the derived frame rate to determine the corresponding audio packet offset. The alternative `start` anchor begins at packet zero; `end` aligns the final audio packet with the end of the video span. The CLI option selects among these anchor modes; it does not alter the wire protocol.

This timeline is important. Naively concatenating only received audio packets removes gaps and causes audio to drift relative to video. The original analysis measured drift of up to roughly 3.5 seconds by the end of a minute when missing packets were not represented as silence.

### 11.3 Measured audio recovery

For the 2026-10-30 13:00–13:01 test minute:

- approximately 1,450 audio packet slots were expected on the measured timeline;
- the union of five passes contained 1,421 of 1,500 slots (about 56.8 seconds of audio on a 59.95-second timeline);
- the union of seven passes contained 1,438 of 1,500 slots;
- `--passes 3` recovered 1,388 audio packets in the recorded test.

These figures describe the particular captured minute and are not expected packet counts for every one-minute recording.

### 11.4 Audio alignment against the app

The app-generated reference clip used for testing was `2026-10-30-13-04-07-fromIPC360app.mp4`. It reported H.264 1920×1080 at approximately 20.04 fps and AAC-LC mono audio at 8 kHz / 29 kbit/s.

Comparing the assembled WAV with the app's audio reference produced a best alignment shift of 2.00 seconds for the `fid` anchor. This matched the observed difference between the first burned-in timestamp in the two clips. The `end` anchor produced a much larger, incorrect shift in that test. The comparison tool reported a best shift of −2.00 seconds and mean absolute difference divided by maximum RMS of 0.0099. This is a measured result for that clip, not a universal offset.

## 12. ACK messages — command 40004

The client sends command `40004` after each valid `40005` chunk. Its 12-byte payload is three little-endian u32 values:

```text
[cam_id, counter, user_id]
```

The semantics of `counter` are not established. In the app capture, 1,031 ACK writes used values from approximately `0x0B42` to `0x0C69`; the value increased only about 295 times and sometimes repeated or skipped. This does not look like a simple chunk index.

The current Python client supports two ACK counter modes:

- `--ack seq`: send a client-maintained sequential value, starting at zero and incrementing per ACK. This is the current default.
- `--ack cnt`: copy the received chunk's `cnt` value into the ACK.
- `--ack none`: do not send ACKs; the original notes say this mode was not tested end-to-end.

Both `seq` and `cnt` were reported to work in testing, but neither should be described as the proven original app counter rule. The source comment says the captured counter is “approximately” the chunk's `cnt`, while the more detailed analysis says it is a session-global counter that increments less often than ACKs. That discrepancy remains unresolved.

The SDK command-table analysis did not find a static `0x9C44` constant for this command. The command value appears to be produced at runtime, so a simple search of the SDK's constant table does not identify the sending code.

## 13. Relevant SDK command senders

The reverse-engineered camera-side senders are in `sdk::ipc_conn` and use the SDK's `utils::send_msg_tcp()` path. The table records the function names and payload lengths identified in the SDK analysis.

| Command | SDK function | Payload length / notes |
|---:|---|---|
| `40001` | `p2p_stun_msg` / `make_ipc_conn` | Handshake request; 12-byte payload in the client implementation |
| `40002` | `p2p_stun_ack_msg` | 12 bytes |
| `40013` | `connect_video_msg` | 16 bytes |
| `40015` | `ipc_stopplay` | 24 bytes; see [Stop command](#76-stop-command) |
| `40023` | `ipc_send_proc` | Re-emits buffered bytes and builds the 20-byte outer header using the SDK revision |
| `20241` | `base_conn::playback_send` | 188 bytes; `SDK_REMOTE_FILE` |
| `20203` | `mgr_conn::mgr_p2p_msg` | 40 bytes |

Keepalive senders identified in the SDK:

- `ipc_conn::keep_alive`: command `1`, zero-byte payload, over the manager TCP path.
- `video_conn::keep_alive`: command `1`, eight-byte payload, sent through the P2P datagram path.

Commands `40004`, `40005`, `40014`, and `40087` were identified as camera-to-app receive messages in the SDK analysis; they were not found as SDK outgoing senders. The Python client nevertheless sends `40004` as its ACK to `40005` chunks.

The SDK function `CameraPlay::_publicPlayBack` contains the main playback sequence: create the P2P bind socket, request the P2P endpoint through `mgr_p2p_msg` (the decompiled path waits about 1,000 ms), send the STUN/connect messages (the decompiled path waits about 3,000 ms after the connect request), then deliver the `SDK_REMOTE_FILE` playback request through manager virtual method slot `+0x68` before registering the real-play callback with `frame_deck::SetRealPlayCallBack`. The original Ghidra references identify `_publicPlayBack` at ELF `0x1ADD5D`; the playback method is reached through the manager vtable slot rather than a statically identified direct call. The recorded failure values include `15000` for a connect-video timeout and `0x2EE3` (`12003`) for a send-data failure.

## 14. Security and encryption observations

The tested camera's `SEI_PW` / `PW_PASS_STREAM_SECURITY` field was observed as zero. The `DES`-based stream-security path therefore did not appear to be active for this camera, and the captured `40005` chunk data was readable without applying DES decryption.

This is not evidence that every IPC360/Puwell camera sends unencrypted video. The field and behavior should be checked against another camera or firmware before making a general claim. Earlier attempts to derive a DES key for the tested stream were not productive; the test evidence indicates that the path was not enabled in that case.

## 15. Other reverse-engineering findings

This section preserves findings that help interpret captures but are not required to implement the normal day-map and playback exchange.

### 15.1 Marker-like byte sequences

An older corpus (`probes/t2-in.bin`) contained 164 occurrences of `00` followed by three ASCII decimal digits in approximately 42 KB. The later, correctly assembled 4.3 MB stream contained only one such sequence. The older matches were therefore attributed to the earlier broken/truncated assembly path rather than to a real embedded container field.

No “strip the tags” edit is needed for FFmpeg to decode the correctly assembled H.264 stream. A past edit that appeared to improve a decode metric was later identified as noise introduced by the experiment. If the pre-fix capture corpus is regenerated or reinterpreted, this conclusion should be checked again.

### 15.2 Commands 20350 and 20351

Static analysis distinguished these commands from the nearby `20326` / `20327` Wi-Fi query:

- `20350` (`0x4F7E`) is sent by `base_conn::mgr_tfinfo_set`. The SDK's reply switch places it in a fall-through/default group, with no dedicated reply handler identified. It appears to be fire-and-forget.
- `20351` (`0x4F7F`) is treated as a manager error or “command rejected” notification. The SDK expects a 36-byte payload, corresponding to a 16-byte inner body after the envelope. The inner body contains `[cam_id, user_id, rejected_command, 0]`. The error handler finds the camera manager connection, marks it failed, and signals its event, which tears down that manager connection.
- `20326` (`0x4F66`) is `base_conn::mgr_wifi_get`, with an eight-byte body in the current day-map code. Its reply is `20327` (`0x4F67`), with a 0x74-byte inner payload at full-payload offset `+0x14` in the analyzed case.

The app capture did not contain `20350` or `20351`, and there is no reason to add `mgr_tfinfo_set` to the normal playback path based on the evidence available here.

A prior LAN-path experiment sent a request that received `20351` with body `[cam_id, user_id, 0x9C42, 0]`, interpreted as rejection of command `40002`; the error handler set the manager connection state at `conn+0x10` to `-1` and signalled `conn+0x27`, causing teardown. A later UDP test to the camera's LAN endpoint received `20312` but no `40005` chunks. In that test the manager restated the public endpoint `223.157.134.47:6126` and the LAN endpoint `<LAN IP>:23456`; the relay endpoint still did not return chunks. These observations support using the manager-announced relay for the tested camera.

### 15.3 Manager watchdog behavior

The SDK symbol `mgr_man::keep_send_beat` (ELF `0x1CE3F9`) was initially suspected to release chunks. Further analysis identified it as a watchdog. It calls `_mgr_check_heartbeat` (ELF `0x1CE459`); under the observed state condition, it invokes the connection keepalive slot `+0x60` while the beat counter at `conn+0x1C` is below five. At five, it marks the connection inactive (`conn+0x14 = 0`), calls `mgr_conn::mgr_clean()`, signals the disconnect path, and logs that the user should sign in again. The reported manager errors are `0x13888` (80008) and `0x13889` (80009). `LocalHeatProc` (ELF `0x1CE565`) applies a similar check to the local connection with a limit of nine.

The repeated `20241` request is a separate operation reached through the manager connection's playback method slot (`+0x68`). It is the operation correlated with the release of more chunks. The watchdog finding explains why the client must keep the manager connection active during a lengthy pull; it does not establish the complete server-side flow-control implementation.

### 15.4 Relay packet loss and multiple passes

In the test environment, different pulls of the same range could use different relay endpoints, and each pass lost a different set of datagrams. Combining the received chunk slots across passes improved the reconstructed video.

One measured one-minute range produced these results:

| Passes | Partial groups | Missing chunks (reported) | Frames | Decode errors | Container duration |
|---:|---:|---:|---:|---:|---:|
| 1 | 11 | 285 | 1,005 | 8 | 50.25 s |
| 2 | 6 | 106 | 1,112 | 3 | 55.60 s |
| 3 | 5 | 99 | 1,178 | 1 | 58.90 s |
| 5 | 2 | — | 1,199 | 1 | 59.95 s |
| 7 | 0 | — | 1,199 | 0 | 60.00 s |

These results are from one measured minute, not a guarantee that seven passes always produce a clean file. The analysis also found groups for which some chunks never appeared across repeated pulls. A low `decode_errors` count is therefore useful alongside the number of partial groups.

A second experiment pulled the same range twice at seven passes, strictly sequentially, then merged all 14 wire files offline. For 13:27–13:28, neither seven-pass run was clean by itself (one returned 1,197 groups / 1 partial / 1 decode error; the other 1,188 groups / 1 partial / 0 decode errors, including a pass that hit a relay returning zero chunks), but the 14-wire union returned 1,197 groups / 0 partial / 0 decode errors. An eight-wire union for 12:33–12:34 returned 1,204 groups / 1 partial / 0 decode errors; the remaining partial group was missing 11 chunks that had not left the camera. These experiments indicate that additional passes can improve recovery, but cannot recover a chunk that the camera or relay never supplies.

The original author's practical recommendation for that environment was five passes for ordinary viewing and seven when a minute still contained partial groups. A pull of one minute took approximately 50–57 seconds per pass, excluding setup and idle waits. These timings depend on the camera, relay, network, and date range.

### 15.5 One manager session at a time

The manager connection and P2P relay state are shared by the pull workflow. Starting a second list/pull operation while a pull is active can disrupt the first operation. Observed symptoms included `BrokenPipeError`, a missing `20204` reply, and a day map reported as zero chunks.

The current `ipc360.py` uses a file lock to prevent overlapping operations:

- default lock path: `camclips-share/grab.lock`;
- running-state sidecar: `camclips-share/grab-running.json`;
- stale-lock timeout: `LOCK_TIMEOUT_S = 1200` seconds;
- `--force` and `IPC360_LOCK_OK=1` bypass the guard;
- `--grab-lock -` disables lock-file handling for a run.

`--list` checks whether a pull lock is active but does not take the lock itself. The gallery also avoids launching a new day-map subprocess while a pull owns the manager session. The practical rule is to run pulls sequentially and avoid fresh day-map queries while a pull is in progress.

### 15.6 Pull duration and `--seconds`

The source sets `PASS_S = 57` seconds per pass per minute of SD footage as a measured planning value. In the observed environment, five passes over three minutes of SD content took approximately 14 minutes; seven passes over one minute took about 6.7 minutes. Actual setup, manager login, relay selection, and idle waits can add time before the first completed pass is visible.

The `--seconds` option caps a **single pass**, not the entire multi-pass pull. The pull loop restarts its timer at the beginning of each pass. A fixed `--seconds 150` can therefore truncate a requested range longer than roughly 2.6 minutes. The gallery calculates a range-dependent cap using:

```python
seconds = max(150, PASS_S * mins * 1.8)
kill_after = max(LOCK_TIMEOUT, PASS_S * passes * mins * 1.6 + 300)
```

These are gallery process-management values, not protocol fields.

## 16. Client-side assembly and output files

The protocol delivers chunks, not a ready-made MP4. `assemble()` reorders and reconstructs the video and audio before invoking FFmpeg.

The current output stages are:

```text
seg.h264
    H.264 data from nets 0 and 1, ordered by (fid, net, occurrence),
    with chunks inside each group ordered by idx.

seg-audio.raw
    net=3 audio on the absolute packet timeline. Missing 320-byte slots
    are filled with A-law silence (0x55).

seg-audio-s16.raw
    A-law decoded to signed 16-bit little-endian PCM.

seg.mkv
    Lossless H.264 video plus 8 kHz mono pcm_s16le audio.

seg.wav
    Lossless audio-only output.

seg.mp4
    H.264 video copied without re-encoding, with MP3 audio at 8 kHz / 64 kbit/s.

seg.stats.json
    Assembly statistics, including group/partial counts, decode errors,
    video frame-rate values, and audio fields. The gallery reads this file.
```

The FFmpeg 6.1.1 build used in the original tests did not have an AAC encoder (`aac` was decoder-only, and trying `-c:a avcodec_aac` returned “Encoder not found”). The MP4 output therefore uses MP3 audio at 8 kHz / 64 kbit/s with `-strict -1`; video is copied with `-c:v copy` and `-movflags +faststart`. Resampling the source from 8 kHz to 44.1 kHz was dropped because it added no source detail. The `--denoise NAME` option passes `-af NAME` to FFmpeg. Filters verified in that environment included `anlmdn`, `adynamicsmooth`, and `afftdn`; `denoise` by itself was not a valid filter, and `afftdn=nf=2` returned “Numerical result out of range.” The default is no audio filter. The MKV output is the lossless option for the reconstructed H.264 and decoded PCM tracks.

The assembler also runs a separate H.264 decode check (`-f null -`) and records `decode_errors` from that check. Mux stderr is captured, so the gallery should rely on the stats file rather than trying to infer decode errors from the mux log.

## 17. Developer reproduction commands

The following commands are retained as examples from the original test environment. They reference local output directories and test files that are not included in the public repository; replace them with paths that exist on your system.

```bash
# Query the SD day map.
IPC360_PW=your-camera-password python3 ipc360.py --list --day 2026-10-30

# Pull and assemble a minute range.
IPC360_PW=your-camera-password python3 ipc360.py \
    --play 13:00-13:01 --day 2026-10-30 \
    --out probes/play-P5b/seg --passes 5 --seconds 150 --idle 15

# Reassemble saved wire files without contacting the camera.
python3 ipc360.py \
    --from-wire probes/play-P5/pull-20261030-1300-1301-p1.wire,...,p5.wire \
    --out probes/play-P5b/seg

# Import an assembled recording into the LAN gallery.
camclips.py --import ipc360/probes/play-P5b/seg \
    --name 2026-10-30_1300-1301 --day 2026-10-30 --range 13:00-13:01

# Let the gallery run the pull itself.
camclips.py --grab 2026-10-30 13:00-13:01 --passes 5

# Re-publish a clip from its saved grab directory.
camclips.py --import camclips-share/grab-2026-10-31_0042-0045/seg \
    --name 2026-10-31_0042-0045 --day 2026-10-31 --range 00:42-00:45

# Query each date in a month to populate the calendar.
camclips.py --sweep 2026-10

# Compare reconstructed audio with an app-generated reference clip.
python3 tools/audiosync.py probes/play-P7b/seg.wav \
    camclips-share/2026-10-30-13-04-07-fromIPC360app.mp4
```

`--from-wire` is an offline operation: it parses saved wire data and assembles the result without logging in or contacting the camera. In the current code, the list of wire paths is comma-separated. Wildcard expansion is not performed by the program itself; expand patterns in the shell before passing the argument.

## 18. Gallery behavior that is not part of the wire protocol

The following notes concern `camclips.py`, its cache, and the browser UI. They are retained because they explain how the client presents the protocol results, but they should not be mistaken for camera commands or server-side protocol requirements.

### 18.1 Calendar and day-page data sources

- Opening `/day?day=X` normally triggers a day-map query for that date (`render_day(fresh=True)` → `daymap(X)`). In the original environment, this took about 8–12 seconds including login and the manager query.
- The month calendar itself does not query the camera. It renders from cached `daymap-<day>.json` files and local clip sidecars. Green `.cmin` marks represent minutes the SD card reports; dashed `.copen` marks represent open minutes (observed flag `0x80`); amber `.cclip` marks represent minutes already pulled.
- A date with no cached day map is different from a date whose successful day-map response contains zero active minutes. “Not checked” must not be rendered as “card empty.”
- A day-map read that receives no valid manager result is not cached as an empty day. The gallery requires the expected `day YYYY-MM-DD: N chunks, M active minutes` output line; otherwise it marks the attempt as failed and leaves the day-map cache untouched.
- A month sweep queries every date. A measurement from 2026-10-31 found about 7.5 seconds per date, or roughly four minutes for a 31-day month. The October 2026 test card reported recordings across the month, with five to 67 active minutes on dates that contained clips and zero active minutes on 2026-10-11. A separate check found that 2025-12-01 could return nine index chunks and zero active minutes; a successful empty result is still a valid day-map response. The manager-reported time-zone offset also differed between tested months (for example, +0 hours in October and −1 hour in December), reinforcing that the offset should be read from each response rather than hard-coded.

### 18.2 Cache policy for completed days

The gallery treats a past day as complete only when all of these are true: a cache exists, it is not marked failed, it has a timestamp, the date is in the past, and the cache timestamp is later than the local end of that date.

This avoids trusting a day map that was fetched before the date had finished. For example, a map read at 22:39 on 2026-10-31 cannot prove that the rest of the day had no recordings. Once a complete map has been read after the day ends, later page opens can use the cache without contacting the camera.

The day page can override the default behavior:

- no explicit check option: query unless the cached map is complete;
- `recheck=1`: always query;
- `cache=1`: never query;
- a pull page: do not query, because the pull owns the manager session.

A page served from cache can offer a “check this day” action. A page that has already queried should not imply that a fresh check was skipped.

The card may recycle old recordings, so the UI retains an explicit recheck option rather than treating cached history as permanently authoritative.

### 18.3 Day-map display

The day page renders the 24-hour map as two 12-hour rows: 00:00–11:59 and 12:00–23:59. A range crossing noon is clipped into both rows. This prevents noon from splitting a single visual strip into an ambiguous layout.

The server-rendered row is the no-JavaScript fallback. It includes data attributes for the time range and minute flags. The browser script can then redraw the same row around a zoom window:

- mouse wheel over a row zooms that row;
- double-click selects a 30-minute window;
- the visible span is clamped to 15–720 minutes within that row;
- a minimum cell width of 1.4% keeps one-minute cells clickable;
- 15-minute ticks appear below five hours and 5-minute ticks below two hours.

The main classes and elements used by the script include `.tl`, `input.scrub`, `.tmark`, `#st`, `#pullnote`, `.sdr`, `.cell`, and `.srlab`.

The afternoon row can be labelled in 12-hour time by adding `clock=pm`; the morning row remains in 24-hour time. The navigation control switches between PM labels and 24-hour labels without changing the underlying minute data.

### 18.4 Month navigation and pull status

The month chips include the current month, the previous five months, and months that contain a cached day map or clip. Previous/next controls remain available beyond those months so a user can navigate to a month with no cached data yet. The whole calendar date cell is a link to `/day?day=...`; the bar itself remains positioned relative to its container so its minute marks do not shift when the cell is clicked.

The gallery's live status bar polls every three seconds. The time between completed wire files is used as the measured pass rate once the first wire exists. Before the first wire file is written, setup and idle waits may still be in progress, so the UI should report that phase instead of displaying a misleading countdown.

The pull timer uses `PASS_S = 57` seconds per minute as a fallback before a wire file exists. The original test environment measured roughly 48–56 seconds per pass per minute of SD content after the pull began. These values are local measurements, not protocol constants.

### 18.5 Month validation and stale state

Month strings must be validated both for shape and for a valid month number. A string such as `2026-13` matches the shape `YYYY-MM` but is not a valid calendar month. The gallery added `month_ok()` to reject month values outside `01` through `12` before doing calendar work or touching the sweep lock.

If the gallery service is restarted during a sweep or pull, its worker thread is stopped but files such as `grab.lock` and `sweep-running.json` may remain. Those files can make the status bar appear active until the lock timeout expires. After confirming that no pull or sweep process is still running, remove the stale state files and any incomplete grab directory. Day-map caches that represent completed manager responses may be retained. The original deployment notes recommend restarting with `sudo -n systemctl restart camclips.service` only when `camclips-share/grab.lock` is not active; stopping the service during a pull kills that pull's process group.

The status page scans JSON sidecars when looking for the newest clip; it must skip `daymap-`, `grab-`, and `sweep-` state files. Otherwise, a newly written sweep-state JSON file can be mistaken for a zero-byte clip.

### 18.6 Gallery routes

The routes below describe the browser-facing application, not the camera protocol:

```text
/                       Current-month calendar (Sunday-first) and live status
/month?m=2026-09        Calendar for a selected month
/sweep?m=2026-10        Query every date in a month; progress in the live status bar
/day?day=2026-10-30     Day map and clips for a date
/clip?name=<NAME>       Video page
/help                   Usage and explanation page
/dl/<NAME>.mp4          Download a clip with Range support
/delete?name=<NAME>     Preview the files selected for deletion
/delete?name=<NAME>&confirm=1
                        Confirm deletion of gallery files
/statusfrag /status.json
                        Live-bar fragment and machine-readable status
/status                 Active lock and newest grab/sweep log information
/refresh?day=...        Refresh day-map data, then redirect
/pull?day=...&range=... Start a pull from the browser
/api/daymap?day=...     Raw day-map JSON
```

Gallery deletion removes files from the gallery's storage only. It does not delete the recording from the camera's SD card. The application uses a two-step confirmation flow.

## 19. Evidence limits and unresolved questions

The following points should remain qualified until another capture, firmware build, or SDK analysis resolves them.

1. **Relay `rev` tags.** The printable values in the `rev` dword on some received frames appear to be relay-side metadata. Their exact meanings are unknown.
2. **Command 40004 counter.** The original app's ACK counter repeats and skips. The client's sequential and chunk-counter modes both worked in tests, but the original counter algorithm has not been derived.
3. **Alternate chunk layout C.** The parser retains this fallback, but the tested camera's correctly delimited chunks all match layout A. There is no solid evidence that layout C is a separate layout on this camera.
4. **`cnt` scope.** The notes describe it both as a session-global counter shared across networks and as the audio packet ordinal within the requested minute. Preserve the value and avoid assuming a universal reset rule.
5. **`dayEpoch` timezone.** The source comment describes camera-local midnight, while an earlier protocol note says UTC midnight. Verify this for each camera/date before using it as an absolute timestamp.
6. **Day-map command 20402.** The current code sends it after the index, but the analysis also associates `20402`/`20403` with server-time and cloud-storage getters. Its exact role in the day-map exchange is not fully established.
7. **`40001` repetition.** The capture and the client both send `40001` three times and `40013` once. Why the handshake frame is repeated, and whether one send would be enough, is not established.
8. **Minute flag bitfield.** `0x82` and `0x80` were observed as closed and open minute values, respectively. The full meaning of the remaining bits has not been decoded; the difference is `0x02`, not the least-significant bit.
9. **Frame-rate estimate.** The current calculation is based on measured video IDs and audio counters. It worked for the tested 10- and 20-fps examples, but should be verified on other frame rates and firmware.
10. **Relay completeness.** Repeated pulls improved coverage in measured tests, but some groups remained incomplete across several passes. The missing chunks may not be retransmitted by the camera or relay.
11. **Stream encryption.** The security flag was zero on the tested camera. Other models or firmware may enable the DES-based security path.
12. **Other camera models.** The protocol has been verified on one Puwell-SDK camera enrolled in `com.ipc360`. A matching brand name alone does not prove compatibility. A different SDK build, manager family, or app ecosystem may use different messages or field layouts.

## 20. Source-code map

The primary implementation points in the current public source are:

| Function / area | Purpose |
|---|---|
| `frame()` / `envelope()` | Build the outer header and manager envelope |
| `recv_frame()` / `Mgr` | Read TCP manager frames |
| `login()` | Perform the `10030` / `10001` login exchange |
| `shm()` / `day_map()` | Build the day timestamp and retrieve the `20400` / `20401` index |
| `remote_file()` | Build the un-enveloped 188-byte `20241` playback request |
| `mgr_p2p_msg()` / `p2p_endpoint()` | Request and parse the relay endpoint |
| `parse_chunk()` | Parse a `40005` payload |
| `split_frames()` / `read_msgs()` | Recover frames from P2P datagram reads |
| `occ_key()` / `merge_groups()` | Preserve distinct audio occurrences and merge repeated passes |
| `audio_track()` / `assemble()` | Reconstruct audio timing and produce media files |
| `pull()` | Coordinate playback, P2P traffic, keepalives, ACKs, and chunk collection |

Useful source links (line anchors are the current `main`):

- [`ipc360.py`](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py) — [frame / envelope](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L82-L87), [login](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L161), [day map](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L207-L265), [`remote_file` / 20241](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L583-L615), [chunk parser](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L616-L656), [datagram reads](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L657-L720), [relay endpoint](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L721-L784), and [the pull loop](https://github.com/Neon-Pumpkin/ipc360/blob/main/ipc360.py#L785)
- [`camclips.py`](https://github.com/Neon-Pumpkin/ipc360/blob/main/camclips.py) — [day-map cache](https://github.com/Neon-Pumpkin/ipc360/blob/main/camclips.py#L367), [closed-day cache policy](https://github.com/Neon-Pumpkin/ipc360/blob/main/camclips.py#L241), and [two-row day-map rendering](https://github.com/Neon-Pumpkin/ipc360/blob/main/camclips.py#L719)
- [Repository README](https://github.com/Neon-Pumpkin/ipc360/blob/main/README.md)

---

*Last reviewed against the public `main` branch on 2026-11-02. This is a proposed documentation rewrite; it does not change the repository or claim that the inferred protocol is official.*
