"""
sync_codec.py - Encode and decode RPTS batched GPS fix packets.

Wire format for RPTS payload (after the RPTS:T<id>:<from_idx>: prefix):

    [[ts0,lat0,lon0,alt0,spd0],[dts1,dlat1,dlon1,dalt1,dspd1],...]

    - First element is the absolute fix: [ts, lat, lon, alt, spd]
    - Subsequent elements are integer deltas:
        dts   = ts  - prev_ts          (seconds, usually 10-15)
        dlat  = round((lat - prev_lat) * 100000)   (1e-5 degree units)
        dlon  = round((lon - prev_lon) * 100000)
        dalt  = round(alt - prev_alt)              (meters, integer)
        dspd  = round((spd - prev_spd) * 10)       (0.1 km/h units)

JSON-encoded, compact (no spaces).

Max fixes per packet is determined by the MAX_PACKET_BYTES limit.
Caller passes a list of raw fix lists [ts, lat, lon, alt, spd] and
encode_rpts packs as many as fit.

Usage:
    encoded, n_packed = encode_rpts(fixes_list)
    decoded_fixes     = decode_rpts(encoded)
"""

import json

MAX_PACKET_BYTES = 200  # leave room for RPTS:T<id>:<idx>: prefix + AES padding


def encode_rpts(fixes):
    """Encode a list of fix lists into a compact delta string.

    fixes: list of [ts, lat, lon, alt, spd].  At least 1 fix required.

    Returns (encoded_str, n_packed) where:
        encoded_str  -- compact JSON string of the packed fixes
        n_packed     -- number of fixes actually packed (may be < len(fixes)
                        if they don't all fit in MAX_PACKET_BYTES)
    """
    if not fixes:
        return ("[]", 0)

    out = []
    prev = None

    for i, fix in enumerate(fixes):
        if len(fix) < 5:
            continue
        ts, lat, lon, alt, spd = fix[0], fix[1], fix[2], fix[3] or 0, fix[4] or 0.0

        if prev is None:
            # First fix: absolute values
            encoded_fix = [ts, lat, lon, int(round(alt)), round(spd, 2)]
        else:
            # Delta encoding - integer deltas where possible
            dts  = int(ts  - prev[0])
            dlat = int(round((lat - prev[1]) * 100000))
            dlon = int(round((lon - prev[2]) * 100000))
            dalt = int(round(alt - prev[3]))
            dspd = int(round((spd - prev[4]) * 10))
            encoded_fix = [dts, dlat, dlon, dalt, dspd]

        # Check if adding this fix would exceed the byte limit
        candidate = out + [encoded_fix]
        encoded = json.dumps(candidate, separators=(",", ":"))
        if len(encoded) > MAX_PACKET_BYTES and out:
            # Doesn't fit — return what we have so far
            return (json.dumps(out, separators=(",", ":")), i)

        out.append(encoded_fix)
        prev = (ts, lat, lon, alt, spd)

    return (json.dumps(out, separators=(",", ":")), len(out))


def decode_rpts(encoded_str):
    """Decode an RPTS payload string back into a list of absolute fix lists.

    Returns list of [ts, lat, lon, alt, spd] in absolute values.
    Returns [] on any error.
    """
    try:
        data = json.loads(encoded_str)
    except Exception:
        return []

    if not data or not isinstance(data, list):
        return []

    out = []
    prev = None

    for i, item in enumerate(data):
        if not isinstance(item, list) or len(item) < 5:
            continue
        if prev is None:
            # First fix: absolute
            ts   = item[0]
            lat  = item[1]
            lon  = item[2]
            alt  = float(item[3])
            spd  = float(item[4])
        else:
            # Delta: reconstruct absolute values
            ts   = prev[0] + item[0]
            lat  = round(prev[1] + item[1] / 100000.0, 7)
            lon  = round(prev[2] + item[2] / 100000.0, 7)
            alt  = float(prev[3] + item[3])
            spd  = round(prev[4] + item[4] / 10.0, 3)
        out.append([ts, lat, lon, alt, spd])
        prev = (ts, lat, lon, alt, spd)

    return out


# =========================================================================
# Self-tests
# =========================================================================
if __name__ == "__main__":
    print("sync_codec.py self-test")
    print("-" * 50)
    failures = 0

    # 1. Encode + decode round-trip: single fix
    print("\n[1] Round-trip: single fix:")
    fixes = [[1777674008, 50.127184, 14.12072, 421.6, 3.71]]
    enc, n = encode_rpts(fixes)
    dec = decode_rpts(enc)
    ok = (n == 1 and len(dec) == 1
          and dec[0][0] == 1777674008
          and abs(dec[0][1] - 50.127184) < 1e-5
          and abs(dec[0][2] - 14.12072)  < 1e-5)
    print("    enc={} n={}  dec[0]={}  {}".format(enc, n, dec[0] if dec else None,
                                                    "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 2. Round-trip: multiple fixes with deltas
    print("\n[2] Round-trip: 5 walking fixes:")
    fixes = [
        [1777674008, 50.127184, 14.12072,  421.6, 3.71],
        [1777674023, 50.127140, 14.120649, 412.1, 1.34],
        [1777674038, 50.127030, 14.120769, 402.5, 4.11],
        [1777674053, 50.126892, 14.121033, 416.8, 3.34],
        [1777674068, 50.126824, 14.121293, 433.4, 4.38],
    ]
    enc, n = encode_rpts(fixes)
    dec = decode_rpts(enc)
    ok = (n == 5 and len(dec) == 5)
    print("    n={} len_dec={}  {}".format(n, len(dec), "OK" if ok else "FAIL"))
    if not ok: failures += 1
    # Verify precision on last fix
    last_in  = fixes[-1]
    last_out = dec[-1]
    ok2 = (abs(last_out[1] - last_in[1]) < 2e-5
           and abs(last_out[2] - last_in[2]) < 2e-5
           and abs(last_out[3] - last_in[3]) < 1.0
           and abs(last_out[4] - last_in[4]) < 0.2)
    print("    last fix precision OK: lat={:.6f} lon={:.6f} alt={:.1f} spd={:.2f}  {}".format(
        last_out[1], last_out[2], last_out[3], last_out[4],
        "OK" if ok2 else "FAIL"))
    if not ok2: failures += 1

    # 3. Packet size limit: encode many fixes, should split
    print("\n[3] Packet splitting at MAX_PACKET_BYTES:")
    big_fixes = []
    for i in range(20):
        big_fixes.append([1777674008 + i*15, 50.127 + i*0.0002,
                          14.121 + i*0.0001, 420.0 + i, 4.0 + i*0.1])
    enc, n = encode_rpts(big_fixes)
    ok = (n < 20 and len(enc) <= MAX_PACKET_BYTES)
    print("    packed {} of 20 fixes, {} bytes  {}".format(
        n, len(enc), "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 4. Empty input
    print("\n[4] Empty input:")
    enc, n = encode_rpts([])
    ok = (n == 0 and enc == "[]")
    print("    n={} enc={}  {}".format(n, enc, "OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 5. Decode garbage
    print("\n[5] Decode garbage returns []:")
    ok = (decode_rpts("not json") == [])
    print("    {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    # 6. Verify delta encoding is compact
    print("\n[6] Delta encoding compactness vs raw:")
    raw_json = json.dumps([[f[0],f[1],f[2],f[3],f[4]] for f in fixes],
                          separators=(",",":"))
    enc_json, _ = encode_rpts(fixes)
    print("    raw={} bytes  encoded={} bytes  saved={}%".format(
        len(raw_json), len(enc_json),
        int(100*(1 - len(enc_json)/len(raw_json)))))
    ok = (len(enc_json) < len(raw_json))
    print("    encoded is smaller  {}".format("OK" if ok else "FAIL"))
    if not ok: failures += 1

    print()
    print("ALL SELF-TESTS PASSED" if failures == 0
          else "{} FAILURES".format(failures))