# Security notes

This is a demonstration repo. Read this before deploying anything for real.

## Demo credentials — change them

This branch ships **placeholder secrets** so nothing real is published:

| Secret | Demo value | Where |
|---|---|---|
| LoRa AES-128 key | `LoRaMeshDemoKey1` | `ESP32_C3/include/config.h`, `PicoB/config.py`, `PicoB/runtime.py`, `Hub_Server_Firmware/lora_bridge.py`, `Hub_Server_Firmware/lora_chat.py` |
| WiFi softAP password | `ChangeMe-LoRa24` | `ESP32_C3/include/config.h` |

**Before any real deployment, replace both.** The LoRa key **must be identical
on every node** — if you change it, re-flash *all* devices together or they won’t
decode each other’s packets.

## Crypto caveats

- The link uses **AES-128-ECB**. ECB is chosen for simple interop across three
  crypto stacks (mbedTLS / MicroPython `cryptolib` / Python). It is **not
  authenticated** and leaks equality of 16-byte blocks. For production, move to an
  authenticated mode (e.g. **AES-GCM**) with a per-message nonce and reject frames
  that fail the tag.
- There is no replay protection on the wire. Add a monotonic counter / timestamp
  if replay matters for your use case.
- The `hwid` (chip id) is an **identifier, not a secret** — don’t use it for auth.

## Operational

- Keep the **real** key out of a public repo. Suggested pattern: hold secrets in
  an untracked file (e.g. `secrets.local.*`) and `.gitignore` it, or keep your
  live values on a private branch (this repo keeps real values on `master` and
  demo values on `docs/showcase`).
- The ESP32 web UI is gated by device-id only and served over **plain HTTP** on a
  local AP — fine for a demo, not a substitute for transport security.
- Run LoRa radios with an antenna attached and within your local RF regulations
  for the 433 MHz band and TX power.

## Reporting

This is demo code; for issues open a GitHub issue. Do not include real keys or
location data in reports.
