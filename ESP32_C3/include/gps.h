#pragma once
#include <stdint.h>
#include "sync_codec.h"   // sync_codec::Fix

class GpsReader {
public:
  void begin();

  // Pump UART through TinyGPSPlus. Returns true at most once per
  // intervalMs when a complete sentence with a valid lat/lon/UTC has
  // arrived (and is fresher than 5 s). Caller controls interval to
  // implement movement-class cadence.
  bool tryReadFix(sync_codec::Fix* out);

  // Cadence control. Clamped to >=500 ms.
  void     setIntervalMs(uint32_t ms);
  uint32_t getIntervalMs() const;

  // True once a valid fix has been observed since boot.
  bool hasFix() const;

  // Latest cached fix without touching cadence / position-jump state.
  // Returns false if no valid fix has been observed yet. Used by the
  // on-demand QPOS handler so a UI ping can broadcast immediately
  // without waiting for the next cadence cycle.
  bool latestFix(sync_codec::Fix* out) const;

  // TinyGPSPlus running counters — surfaced for the heartbeat so we
  // can tell "NMEA not flowing at all" from "flowing but no usable
  // fix". charsProcessed == 0 means UART/antenna is dead.
  uint32_t charsProcessed()    const;
  uint32_t sentencesWithFix()  const;
  uint32_t failedChecksum()    const;
};

extern GpsReader Gps;
