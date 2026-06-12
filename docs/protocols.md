# Communication protocols

Every layer of the link, from the LoRa PHY up to the application messages.

- [Radio (PHY)](#radio-phy)
- [Encryption](#encryption)
- [Pi ⇄ Pico A USB serial](#pi--pico-a-usb-serial)
- [Application messages (over LoRa)](#application-messages-over-lora)
- [Trip sync (store-and-forward)](#trip-sync-store-and-forward)
- [RPTS delta codec](#rpts-delta-codec)
- [Device identity & presence](#device-identity--presence)

---

## Radio (PHY)

All nodes use the **Semtech SX1276/78** (Ai-Thinker Ra-01 module) configured
identically — any mismatch and packets simply won’t decode:

| Parameter | Value |
|---|---|
| Carrier | **434.0 MHz** |
| Spreading factor | **SF9** |
| Bandwidth | **125 kHz** |
| Coding rate | **4/5** |
| Sync word | **0x34** |
| Preamble | **8** |
| CRC | **on** |
| TX power | **+20 dBm** (PA_BOOST) |

This is **raw LoRa, not LoRaWAN** — a private protocol with no network server,
no join procedure and no airtime duty-cycle middleware. You own the whole stack.

## Encryption

Application-layer **AES-128-ECB** with **PKCS#7** padding. The 16-byte key is
identical on every node (demo value `LoRaMeshDemoKey1` on this branch — change
it, see [../SECURITY.md](../SECURITY.md)). Plaintext is the tagged ASCII payload;
ciphertext must be **≤ 250 B** to fit one LoRa frame, so the chat body is capped
and trip data is fragmented (see below).

> ECB is used for simplicity/interoperability across three crypto libraries
> (mbedTLS on ESP32, `cryptolib` on MicroPython, `pycryptodome`/stdlib on the
> Pi). For production, prefer an authenticated mode (e.g. AES-GCM) + per-message
> nonce — noted in [../SECURITY.md](../SECURITY.md).

## Pi ⇄ Pico A USB serial

Pico A is a transparent modem for the Pi. Plaintext crosses USB; Pico A does the
AES on the radio side.

| Pi → Pico A | Meaning |
|---|---|
| `TX:<plaintext>` | encrypt + transmit over LoRa |
| `PING` | liveness check → `PONG` |
| `RESET` | re-init the radio → `READY` |
| `TIME:<iso>` | set the DS1302 RTC |

| Pico A → Pi | Meaning |
|---|---|
| `READY` | bridge up / radio re-armed |
| `RX:<text>\|<rssi>\|<snr>` | a decrypted LoRa packet + link quality |
| `LOG:<note>` / `ERR:<reason>` | diagnostics |

## Application messages (over LoRa)

Every payload begins with an **uppercase tag + `:`**. Untagged packets are logged
but never routed to chat.

| Tag | Direction | Payload | Purpose |
|---|---|---|---|
| `GPS:` | tracker → all | `{"hwid","lat","lon","ts"}` | live position dot |
| `TRIPSTART:` | tracker → hub | `{"device","hwid","id","ts","lat","lon"}` | trip began |
| `TRIPEND:` | tracker → hub | `{...,"km","dur","type","avg","max",...}` | trip finished |
| `CHAT:` | any → any | `CHAT:<sender>:<body>` | user message (body ≤ ~204 B) |
| `DEVICE:` | any → all / Pi→device | `{"id":<hwid>,"name":<name>}` | announce · heartbeat · rename |
| `WHO?` | Pi → all | — | presence probe; everyone re-announces `DEVICE:` |
| `QPOS:<target>` | Pi → device | — | on-demand position; target replies with `GPS:` |
| `SYNC:` / `Q*:` / `R*:` / `ACK:` | trip sync | see below | store-and-forward transfer |

Identity is carried by the **permanent hardware id (`hwid`)** — RP2040
`unique_id` (16 hex chars) or ESP32 chip MAC (12 hex chars) — so a device can be
renamed without breaking session routing.

## Trip sync (store-and-forward)

The tracker buffers complete trips on flash and the **Pi pulls them** when the
device announces. The Pi drives every step, which makes the transfer robust to
the lossy radio link.

```
 tracker                              Pi (Hub_Server)
   │  SYNC:<hwid>                ───────►  (I have unsent trips)
   │  ◄───────  QTRIPS:<hwid>              (enumerate)
   │  RTRIPS:<hwid>:T123:88,T456:42 ─────► (ids + point counts)
   │  ◄───────  QTRIP:T123                 (want metadata)
   │  RTRIP:T123:{json}            ──────► (start/end/km/dur/type/avg/max)
   │  ◄───────  QPTS:T123:0:11            (want points 0..11)
   │  RPTS:T123:0:[[...]]          ──────► (a delta-encoded batch)
   │  ◄───────  QPTS:T123:11:11           (next window)
   │      ... until all N points received ...
   │  ◄───────  ACK:T123                   (stored — delete it)
   ▼  delete T123.gps/.json, mark CONFIRMED
```

Robustness properties:

- **Pi-driven cursor.** The Pi tracks `expected_from` and advances only by the
  number of points actually decoded — so a short/partial batch never skips data.
- **Duplicate/stale rejection.** An `RPTS` whose `from_idx` ≠ the expected cursor
  is dropped (prevents double-counting if a `QPTS` retry races a slow reply).
- **Empty-batch ≠ done.** An empty `RPTS` while points are still expected triggers
  retry, not a premature `ACK` (so a marginal-RSSI moment can’t truncate a trip).
- **Idempotent recovery.** If a device resets mid-trip, on next boot it rebuilds
  the trip’s end-metadata from the fixes already on flash, so it still syncs as a
  complete trip instead of an empty/blank one.
- **ACK = delete.** Only after every point is in the DB does the Pi `ACK`, and
  only then does the tracker free the flash.

## RPTS delta codec

To pack as many fixes as possible into one ≤250 B AES frame, `RPTS` payloads are
delta-encoded JSON. The first fix is absolute; the rest are integer deltas:

```
[[ts0, lat0, lon0, alt0, spd0],          # absolute
 [dts, dlat, dlon, dalt, dspd], ...]      # deltas
   dts  = ts  - prev_ts        (s)
   dlat = round((lat-prev)*1e5) (1e-5 deg)
   dlon = round((lon-prev)*1e5)
   dalt = round(alt-prev)       (m)
   dspd = round((spd-prev)*10)  (0.1 km/h)
```

The encoder packs fixes until the next one would exceed the byte budget, returns
how many it packed, and the Pi requests the remainder in the next `QPTS`. Decoder
reverses it to absolute `[ts,lat,lon,alt,spd]`. Implemented identically in
`sync_codec.py` (MicroPython / Pi) and `sync_codec.cpp` (ESP32).

## Device identity & presence

One `DEVICE:{"id":<hwid>,"name":<name>}` message does three jobs:

1. **Boot announce** — lets the hub discover a new device (and offer a rename).
2. **Heartbeat** — re-sent **every 60 s** by every node. The Pi treats an
   unchanged-name announce as a heartbeat: it upserts `devices(id, name,
   last_seen, last_rssi)`. A device is **online** if heard within **10 minutes**.
3. **Rename** — a `DEVICE:` addressed to a device’s own hwid with a *different*
   name is a rename request; the device persists it and reboots, and the new
   boot announce confirms it. Because identity is the immutable hwid, renames
   never disturb stored trips or routing.

**Signal strength.** The bridge forwards each announce with its RSSI; the hub
stores it as `last_rssi` and both web UIs render a `X/10` score
(`-120 dBm → 1/10`, `-50 dBm → 10/10`). Each tracker also keeps a local roster of
peers it hears directly, so even with no Pi present a node knows who’s alive.
