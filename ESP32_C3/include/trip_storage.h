#pragma once
#include <stddef.h>
#include <stdint.h>
#include "sync_codec.h"

namespace trip_storage {

enum SyncStatus : uint8_t {
  STATUS_UNSENT    = 0,
  STATUS_SENT      = 1,
  STATUS_CONFIRMED = 2,
};

constexpr size_t TRIP_ID_MAX  = 24;
constexpr size_t MAX_UNSENT   = 16;

// Compact trip metadata (matches what Pi 5 expects on the wire after a
// QTRIP query). Short keys keep the JSON well under 200 B.
struct Meta {
  char     device[16];
  char     hwid[20]; // permanent hex hwid; rename-proof identity
  char     id[TRIP_ID_MAX];
  int32_t  sts;
  int32_t  ets;     // 0 if trip still open
  double   slat;
  double   slon;
  double   elat;    // 0 if trip still open
  double   elon;
  float    km;
  uint32_t dur_s;
  char     type[12]; // "walking" / "cycling" / "driving" / "unknown"
  float    avg_kmh;
  float    max_kmh;
};

void   begin();

// ----- read side -----
size_t getUnsentTrips(char outIds[][TRIP_ID_MAX], size_t maxIds);
size_t tripNpts(const char* tripId);
size_t readFixesRange(const char* tripId, size_t fromIdx, size_t maxFixes,
                      sync_codec::Fix* outFixes);
size_t readMetaJson(const char* tripId, char* out, size_t outCap);
SyncStatus syncStatus(const char* tripId);
void       markSyncStatus(const char* tripId, SyncStatus s);
bool       hasUnsentTrips();

// ----- write side (called by trip_tracker / main.cpp) -----
// Append one fix as a single line `[ts,lat,lon,alt,spd]\n` to the
// trip's .gps file. Fast: append-only, no read-modify-write.
bool writeFix(const char* tripId, const sync_codec::Fix& fix);

// Replace the trip's .json file with compact-meta short-key form.
bool writeMeta(const char* tripId, const Meta& m);

// Remove a trip's .json + .gps files. Used to discard false-start
// trips at TRIPEND when the journey didn't confirm the trigger
// conditions (no distance, no sustained speed).
bool deleteTrip(const char* tripId);

// /in_progress.txt — one-line trip id; written on TRIPSTART, removed on TRIPEND.
bool   setInProgress(const char* tripId);
void   clearInProgress();
size_t readInProgress(char* out, size_t outCap);

}  // namespace trip_storage
