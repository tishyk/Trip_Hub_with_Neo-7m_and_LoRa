#include "trip_storage.h"
#include <Arduino.h>
#include <LittleFS.h>
#include <math.h>
#include <string.h>
#include <stdio.h>

namespace trip_storage {

namespace {
constexpr const char* TRIPS_DIR   = "/trips";
constexpr const char* STATE_PATH  = "/sync_state.txt";
constexpr const char* INPROG_PATH = "/in_progress.txt";

bool g_fsOk = false;

void buildGpsPath(const char* tripId, char* buf, size_t cap) {
  snprintf(buf, cap, "%s/%s.gps", TRIPS_DIR, tripId);
}
void buildJsonPath(const char* tripId, char* buf, size_t cap) {
  snprintf(buf, cap, "%s/%s.json", TRIPS_DIR, tripId);
}

SyncStatus readStatusFromFile(const char* tripId) {
  if (!g_fsOk || !LittleFS.exists(STATE_PATH)) return STATUS_UNSENT;
  File f = LittleFS.open(STATE_PATH, "r");
  if (!f) return STATUS_UNSENT;
  size_t idLen = strlen(tripId);
  while (f.available()) {
    String line = f.readStringUntil('\n');
    if (line.length() < idLen + 2) continue;
    if (strncmp(line.c_str(), tripId, idLen) == 0 && line[idLen] == '=') {
      int v = line.substring(idLen + 1).toInt();
      f.close();
      if (v < 0 || v > 2) return STATUS_UNSENT;
      return (SyncStatus)v;
    }
  }
  f.close();
  return STATUS_UNSENT;
}

bool writeStatusToFile(const char* tripId, SyncStatus s) {
  if (!g_fsOk) return false;
  String all;
  bool replaced = false;
  size_t idLen = strlen(tripId);

  if (LittleFS.exists(STATE_PATH)) {
    File r = LittleFS.open(STATE_PATH, "r");
    if (r) {
      while (r.available()) {
        String line = r.readStringUntil('\n');
        if (line.length() == 0) continue;
        if (!replaced
            && line.length() > idLen + 1
            && strncmp(line.c_str(), tripId, idLen) == 0
            && line[idLen] == '=') {
          char buf[40];
          snprintf(buf, sizeof(buf), "%s=%u\n", tripId, (unsigned)s);
          all += buf;
          replaced = true;
        } else {
          all += line;
          all += '\n';
        }
      }
      r.close();
    }
  }
  if (!replaced) {
    char buf[40];
    snprintf(buf, sizeof(buf), "%s=%u\n", tripId, (unsigned)s);
    all += buf;
  }

  File w = LittleFS.open(STATE_PATH, "w");
  if (!w) return false;
  w.print(all);
  w.close();
  return true;
}

}  // namespace

void begin() {
  g_fsOk = LittleFS.begin(false) || LittleFS.begin(true);
  if (!g_fsOk) {
    Serial.println("[trip_storage] LittleFS unavailable");
    return;
  }
  if (!LittleFS.exists(TRIPS_DIR)) {
    LittleFS.mkdir(TRIPS_DIR);
  }
}

size_t getUnsentTrips(char outIds[][TRIP_ID_MAX], size_t maxIds) {
  if (!g_fsOk || maxIds == 0) return 0;
  File dir = LittleFS.open(TRIPS_DIR);
  if (!dir || !dir.isDirectory()) return 0;
  size_t n = 0;
  File entry;
  while ((entry = dir.openNextFile()) && n < maxIds) {
    if (entry.isDirectory()) { entry.close(); continue; }
    const char* fname = entry.name();
    const char* slash = strrchr(fname, '/');
    const char* base  = slash ? slash + 1 : fname;
    const char* dot   = strrchr(base, '.');
    if (!dot || strcmp(dot, ".gps") != 0) { entry.close(); continue; }
    size_t baseLen = (size_t)(dot - base);
    if (baseLen == 0 || baseLen >= TRIP_ID_MAX) { entry.close(); continue; }

    char tripId[TRIP_ID_MAX];
    memcpy(tripId, base, baseLen);
    tripId[baseLen] = '\0';
    entry.close();

    if (readStatusFromFile(tripId) != STATUS_CONFIRMED) {
      strncpy(outIds[n], tripId, TRIP_ID_MAX - 1);
      outIds[n][TRIP_ID_MAX - 1] = '\0';
      n++;
    }
  }
  dir.close();   // closing the dir handle was missing — LittleFS leak per call
  return n;
}

size_t tripNpts(const char* tripId) {
  // Count non-empty lines in the .gps file by scanning bytes in chunks.
  // Avoids allocating one Arduino String per fix — for a 200-fix trip that
  // saved 200 heap allocs that would compete with WiFi/web buffers.
  if (!g_fsOk || !tripId) return 0;
  char path[64];
  buildGpsPath(tripId, path, sizeof(path));
  if (!LittleFS.exists(path)) return 0;
  File f = LittleFS.open(path, "r");
  if (!f) return 0;
  size_t   n = 0;
  uint8_t  buf[256];
  bool     seenData = false;
  while (true) {
    int got = f.read(buf, sizeof(buf));
    if (got <= 0) break;
    for (int i = 0; i < got; i++) {
      uint8_t c = buf[i];
      if (c == '\n') {
        if (seenData) n++;
        seenData = false;
      } else if (c != '\r') {
        seenData = true;
      }
    }
  }
  if (seenData) n++;
  f.close();
  return n;
}

size_t readFixesRange(const char* tripId, size_t fromIdx, size_t maxFixes,
                      sync_codec::Fix* outFixes) {
  if (!g_fsOk || !tripId || !outFixes || maxFixes == 0) return 0;
  char path[64];
  buildGpsPath(tripId, path, sizeof(path));
  File f = LittleFS.open(path, "r");
  if (!f) return 0;

  size_t skipped = 0;
  while (skipped < fromIdx && f.available()) {
    String line = f.readStringUntil('\n');
    if (line.length() > 0) skipped++;
  }

  size_t n = 0;
  while (n < maxFixes && f.available()) {
    String line = f.readStringUntil('\n');
    if (line.length() == 0) continue;
    if (sync_codec::parseFixLine(line.c_str(), &outFixes[n])) {
      n++;
    }
  }
  f.close();
  return n;
}

size_t readMetaJson(const char* tripId, char* out, size_t outCap) {
  if (!g_fsOk || !tripId || !out || outCap < 2) return 0;
  char path[64];
  buildJsonPath(tripId, path, sizeof(path));
  if (!LittleFS.exists(path)) return 0;
  File f = LittleFS.open(path, "r");
  if (!f) return 0;
  size_t pos = 0;
  while (f.available() && pos < outCap - 1) {
    int c = f.read();
    if (c < 0) break;
    if (c == '\r' || c == '\n' || c == ' ' || c == '\t') continue;
    out[pos++] = (char)c;
  }
  out[pos] = '\0';
  f.close();
  return pos;
}

SyncStatus syncStatus(const char* tripId) {
  return readStatusFromFile(tripId);
}

void markSyncStatus(const char* tripId, SyncStatus s) {
  writeStatusToFile(tripId, s);
}

bool hasUnsentTrips() {
  char ids[1][TRIP_ID_MAX];
  return getUnsentTrips(ids, 1) > 0;
}

// ------------------------------------------------------------------
// Writer side
// ------------------------------------------------------------------

bool writeFix(const char* tripId, const sync_codec::Fix& f) {
  if (!g_fsOk || !tripId) return false;
  char path[64];
  buildGpsPath(tripId, path, sizeof(path));
  // Append-only: each fix is one line. Keeps writes cheap (no read-
  // modify-write) and lets the .gps file grow without size constraints
  // beyond available LittleFS space.
  File fh = LittleFS.open(path, "a");
  if (!fh) return false;
  char line[80];
  int n = snprintf(line, sizeof(line),
                   "[%ld,%.6f,%.6f,%d,%.2f]\n",
                   (long)f.ts, f.lat, f.lon,
                   (int)lroundf(f.alt), f.spd);
  if (n <= 0 || (size_t)n >= sizeof(line)) { fh.close(); return false; }
  size_t w = fh.write((const uint8_t*)line, (size_t)n);
  fh.close();
  return w == (size_t)n;
}

bool writeMeta(const char* tripId, const Meta& m) {
  if (!g_fsOk || !tripId) return false;
  char path[64];
  buildJsonPath(tripId, path, sizeof(path));
  File fh = LittleFS.open(path, "w");
  if (!fh) return false;
  // Compact short-key form. Pi 5 sync_manager._normalize_meta() accepts
  // both compact (d/sts/slat/...) and verbose (device/start_ts/start_lat/...).
  // ets/elat/elon are written as 0 while the trip is still open; the
  // tracker rewrites this file with real end values on TRIPEND.
  //
  // NO "d" field — the hub resolves the renameable name from hwid via
  // the devices table (it does the same for GPS: broadcasts). Dropping
  // "d" saves ~20 chars and kept this whole payload inside one SX1278
  // packet at SF9 when the AES padding rounds up. 5-decimal lat/lon
  // (≈ 1 m resolution) save 4 chars × 4 fields = 16 more chars,
  // bringing the worst-case to ~200 B comfortably.
  char buf[256];
  int n = snprintf(buf, sizeof(buf),
    "{\"hwid\":\"%s\",\"id\":\"%s\","
    "\"sts\":%ld,\"ets\":%ld,"
    "\"slat\":%.5f,\"slon\":%.5f,"
    "\"elat\":%.5f,\"elon\":%.5f,"
    "\"km\":%.3f,\"dur\":%lu,"
    "\"type\":\"%s\","
    "\"avg\":%.2f,\"max\":%.2f}",
    m.hwid, m.id,
    (long)m.sts, (long)m.ets,
    m.slat, m.slon,
    m.elat, m.elon,
    m.km, (unsigned long)m.dur_s,
    m.type,
    m.avg_kmh, m.max_kmh);
  if (n <= 0 || (size_t)n >= sizeof(buf)) { fh.close(); return false; }
  size_t w = fh.write((const uint8_t*)buf, (size_t)n);
  fh.close();
  return w == (size_t)n;
}

bool deleteTrip(const char* tripId) {
  if (!g_fsOk || !tripId) return false;
  char path[64];
  bool ok = true;
  buildJsonPath(tripId, path, sizeof(path));
  if (LittleFS.exists(path)) ok &= LittleFS.remove(path);
  buildGpsPath(tripId, path, sizeof(path));
  if (LittleFS.exists(path)) ok &= LittleFS.remove(path);
  return ok;
}

bool setInProgress(const char* tripId) {
  if (!g_fsOk || !tripId) return false;
  File fh = LittleFS.open(INPROG_PATH, "w");
  if (!fh) return false;
  size_t len = strlen(tripId);
  size_t w   = fh.write((const uint8_t*)tripId, len);
  fh.close();
  return w == len;
}

void clearInProgress() {
  if (!g_fsOk) return;
  if (LittleFS.exists(INPROG_PATH)) LittleFS.remove(INPROG_PATH);
}

size_t readInProgress(char* out, size_t outCap) {
  if (!g_fsOk || !out || outCap == 0) return 0;
  if (!LittleFS.exists(INPROG_PATH)) { out[0] = '\0'; return 0; }
  File fh = LittleFS.open(INPROG_PATH, "r");
  if (!fh) { out[0] = '\0'; return 0; }
  size_t n = 0;
  while (fh.available() && n < outCap - 1) {
    int c = fh.read();
    if (c < 0 || c == '\r' || c == '\n') break;
    out[n++] = (char)c;
  }
  out[n] = '\0';
  fh.close();
  return n;
}

}  // namespace trip_storage
