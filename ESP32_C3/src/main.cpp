#include <Arduino.h>
#include <ctype.h>
#include <string.h>
#include <stdio.h>
#include <LittleFS.h>
#include "config.h"
#include "lora_radio.h"
#include "gps.h"
#include "net.h"
#include "web_server.h"
#include "chat_log.h"
#include "trip_storage.h"
#include "sync_manager.h"
#include "trip_tracker.h"
#include "presence.h"

// Runtime device id (declared extern in config.h). Initialised to the
// compile-time default here; overridden from /device_id.txt on boot.
char g_deviceId[16] = DEVICE_ID;
static const char* DEVICE_ID_FILE = "/device_id.txt";

// Permanent hardware id, hex of the 6-byte chip MAC. Burned at the
// factory; survives any flash erase. Pi 5 addresses rename commands to
// this so the (renameable) name and the (immutable) device identity
// stay separate on the wire. Exported via extern in config.h so
// sync_manager etc. can read it.
char g_deviceHwid[16] = {0};
static void computeHwid() {
  uint64_t mac = ESP.getEfuseMac();          // 48-bit chip id, low 6 bytes
  snprintf(g_deviceHwid, sizeof(g_deviceHwid),
           "%012llx", (unsigned long long)mac);
}

static bool g_radioOk = false;

// ---- Device-id persistence -------------------------------------------------
static bool isValidDeviceId(const char* s, size_t n) {
  if (n == 0 || n >= 16) return false;
  for (size_t i = 0; i < n; i++) {
    char c = s[i];
    if (!(isalnum((unsigned char)c) || c == '_' || c == '-')) return false;
  }
  return true;
}

static void loadDeviceId() {
  if (!LittleFS.begin(true)) return;   // also called by Chat.begin; idempotent
  if (!LittleFS.exists(DEVICE_ID_FILE)) {
    Serial.printf("device_id=%s (compile-time default)\n", g_deviceId);
    return;
  }
  File f = LittleFS.open(DEVICE_ID_FILE, "r");
  if (!f) { Serial.println("device_id: open failed"); return; }
  char buf[24] = {0};
  size_t n = f.read((uint8_t*)buf, sizeof(buf) - 1);
  f.close();
  // Trim trailing whitespace / newlines
  while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r' ||
                   buf[n-1] == ' '  || buf[n-1] == '\t')) {
    buf[--n] = '\0';
  }
  if (isValidDeviceId(buf, n)) {
    memcpy(g_deviceId, buf, n);
    g_deviceId[n] = '\0';
    Serial.printf("device_id=%s (loaded from flash)\n", g_deviceId);
  } else {
    Serial.printf("device_id file invalid, using default %s\n", g_deviceId);
  }
}

// Persist a new device id and reboot. Used by handleRenameMessage().
static bool persistDeviceIdAndRestart(const char* newId) {
  File f = LittleFS.open(DEVICE_ID_FILE, "w");
  if (!f) return false;
  f.print(newId);
  f.close();
  Serial.printf("[RENAME] persisted %s, rebooting\n", newId);
  delay(500);          // let any in-flight serial / LoRa drain
  ESP.restart();
  return true;         // unreachable
}

// Extract a "key":"value" string from a JSON-ish blob into out. We
// control both ends of the wire so a strstr-based parser is enough — no
// nesting, no escapes, all values are quoted strings.
static bool jsonGetStr(const char* blob, const char* key,
                       char* out, size_t outSize) {
  char pattern[24];
  snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);
  const char* p = strstr(blob, pattern);
  if (!p) return false;
  p += strlen(pattern);
  const char* end = strchr(p, '"');
  if (!end) return false;
  size_t n = (size_t)(end - p);
  if (n >= outSize) n = outSize - 1;
  memcpy(out, p, n);
  out[n] = '\0';
  return true;
}

// Handle DEVICE:<json> umbrella for device-management.
// Wire grammar — every payload carries BOTH fields:
//   id   = permanent hardware id (hex)             - immutable
//   name = renameable label loaded from /device_id.txt
// DEVICE:{"id":"<hwid>","name":"<current_or_new_name>"}
//
// Direction is implicit:
//   announce (device->Pi):  id=mine, name=mine
//   rename   (Pi->device):  id=mine, name=<new>     (name != current)
//   ack post-reboot:        id=mine, name=<new>     (name now == current)
//
// A packet whose id matches OUR hwid AND whose name differs from
// g_deviceId is a rename request — persist + reboot. Other shapes
// (id not ours, or name == current) are silently consumed.
static bool handleDeviceMessage(const char* text, int16_t rssi) {
  if (strncmp(text, "DEVICE:", 7) != 0) return false;
  const char* body = text + 7;

  char hwid[24] = {0}, name[16] = {0};
  if (!jsonGetStr(body, "id",   hwid, sizeof(hwid))) return true;
  if (!jsonGetStr(body, "name", name, sizeof(name))) return true;

  // Every DEVICE: announce doubles as a liveness heartbeat. Record the
  // peer before the rename checks below (which only concern our own hwid).
  if (strcmp(hwid, g_deviceHwid) != 0) {
    Presence.noteSeen(hwid, name, rssi, millis());
  }

  if (strcmp(hwid, g_deviceHwid) != 0)        return true;  // not us
  if (strcmp(name, g_deviceId)   == 0)        return true;  // echo / no-op
  if (!isValidDeviceId(name, strlen(name)))   return true;

  Serial.printf("[DEVICE] rename %s -> %s (hwid=%s)\n",
                g_deviceId, name, g_deviceHwid);
  persistDeviceIdAndRestart(name);   // does not return
  return true;
}

// ----- Cadence-by-class with hysteresis (mirrors PicoB runtime.py) ------
static MoveClass g_class      = CLASS_IDLE;
static float     g_recentSpd[gps_cfg::SPEED_SMOOTH_N] = {0};
static size_t    g_recentIdx  = 0;
static size_t    g_recentN    = 0;

static float pushAndSmoothSpd(float spd) {
  g_recentSpd[g_recentIdx] = spd;
  g_recentIdx = (g_recentIdx + 1) % gps_cfg::SPEED_SMOOTH_N;
  if (g_recentN < gps_cfg::SPEED_SMOOTH_N) g_recentN++;
  float sum = 0;
  for (size_t i = 0; i < g_recentN; i++) sum += g_recentSpd[i];
  return sum / (float)g_recentN;
}

static MoveClass pickClass(float spd, MoveClass prev) {
  using namespace gps_cfg;
  if (prev == CLASS_WALKING) {
    if (spd >= WALK_TO_CYCLE_UP) {
      return (spd < CYCLE_TO_DRIVING_UP) ? CLASS_CYCLING : CLASS_DRIVING;
    }
    return CLASS_WALKING;
  }
  if (prev == CLASS_CYCLING) {
    if (spd >= CYCLE_TO_DRIVING_UP)   return CLASS_DRIVING;
    if (spd <  CYCLE_TO_WALK_DOWN) return CLASS_WALKING;
    return CLASS_CYCLING;
  }
  if (prev == CLASS_DRIVING) {
    if (spd < DRIVING_TO_CYCLE_DOWN) {
      return (spd < CYCLE_TO_WALK_DOWN) ? CLASS_WALKING : CLASS_CYCLING;
    }
    return CLASS_DRIVING;
  }
  // First call from IDLE: pick by static thresholds.
  if (spd >= CYCLE_TO_DRIVING_UP) return CLASS_DRIVING;
  if (spd >= WALK_TO_CYCLE_UP) return CLASS_CYCLING;
  return CLASS_WALKING;
}

static uint32_t intervalForClass(MoveClass c) {
  switch (c) {
    case CLASS_WALKING: return gps_cfg::WALKING_INTERVAL_MS;
    case CLASS_CYCLING: return gps_cfg::CYCLING_INTERVAL_MS;
    case CLASS_DRIVING:    return gps_cfg::DRIVING_INTERVAL_MS;
    default:            return gps_cfg::IDLE_INTERVAL_MS;
  }
}

// ----- On-demand position query (QPOS:<target>) -----
// Returns true if the wire-format matched (whether or not we replied),
// so the caller can short-circuit further dispatch. Target may be our
// hwid (new wire grammar) or our renameable name (legacy fallback).
static bool handleQposMessage(const char* text);

// ----- Broadcast presence probe (WHO?) -----
// Pi 5 sends a single WHO? to all devices; each replies with the same
// DEVICE: announce it emits on boot. Receiver-side ingest bumps
// devices.last_seen, which is what the chat presence dots read.
static bool handleWhoMessage(const char* text);

static void sendDeviceAnnounce() {
  if (!g_radioOk) return;
  char ann[64];
  snprintf(ann, sizeof(ann),
           "DEVICE:{\"id\":\"%s\",\"name\":\"%s\"}",
           g_deviceHwid, g_deviceId);
  Radio.sendEncrypted(ann);
}

// ----- LoRa-side broadcasts -----
// GPS: payload is intentionally minimal — identity + position + ts.
// The live map dot is the only consumer; altitude/speed live in the
// trip log (TRIPEND + RPTS sync data), not in periodic broadcasts.
// Saves ~40 B per packet (~150 ms airtime at SF9).
static void broadcastGps(const sync_codec::Fix& f) {
  if (!g_radioOk || Sync.sessionActive()) {
    if (Sync.sessionActive()) Serial.println("[GPS TX skipped: sync active]");
    return;
  }
  char body[120];
  int n = snprintf(body, sizeof(body),
    "GPS:{\"hwid\":\"%s\",\"lat\":%.6f,\"lon\":%.6f,\"ts\":%ld}",
    g_deviceHwid, f.lat, f.lon, (long)f.ts);
  if (n <= 0 || (size_t)n >= sizeof(body)) return;
  bool ok = Radio.sendEncrypted(body);
  Serial.printf("[GPS TX] %s (%s)\n", body, ok ? "OK" : "FAIL");
}

static bool handleQposMessage(const char* text) {
  if (strncmp(text, "QPOS:", 5) != 0) return false;
  const char* target = text + 5;
  while (*target == ' ') target++;
  if (strcmp(target, g_deviceHwid) != 0 && strcmp(target, g_deviceId) != 0) {
    return true;  // someone else's ping — consumed but not for us
  }
  sync_codec::Fix f;
  if (!Gps.latestFix(&f)) {
    Serial.println("[QPOS] no fix yet, ignoring");
    return true;
  }
  broadcastGps(f);
  return true;
}

static bool handleWhoMessage(const char* text) {
  if (strncmp(text, "WHO?", 4) != 0) return false;
  Serial.println("[WHO] re-announcing DEVICE");
  sendDeviceAnnounce();
  return true;
}

static void broadcastTripStart(const TripStart& s) {
  if (!g_radioOk) return;
  char body[220];
  int n = snprintf(body, sizeof(body),
    "TRIPSTART:{\"device\":\"%s\",\"hwid\":\"%s\",\"id\":\"%s\","
    "\"ts\":%ld,\"lat\":%.6f,\"lon\":%.6f}",
    g_deviceId, g_deviceHwid, s.id, (long)s.ts, s.lat, s.lon);
  if (n <= 0 || (size_t)n >= sizeof(body)) return;
  bool ok = Radio.sendEncrypted(body);
  Serial.printf("[TRIPSTART] %s (%s)\n", s.id, ok ? "OK" : "FAIL");
}

static void broadcastTripEnd(const TripEnd& e) {
  if (!g_radioOk) return;
  char body[260];
  int n = snprintf(body, sizeof(body),
    "TRIPEND:{\"device\":\"%s\",\"hwid\":\"%s\",\"id\":\"%s\","
    "\"sts\":%ld,\"ets\":%ld,"
    "\"slat\":%.6f,\"slon\":%.6f,\"elat\":%.6f,\"elon\":%.6f,"
    "\"km\":%.3f,\"dur\":%lu,\"type\":\"%s\","
    "\"avg\":%.2f,\"max\":%.2f}",
    g_deviceId, g_deviceHwid, e.id,
    (long)e.sts, (long)e.ets,
    e.slat, e.slon, e.elat, e.elon,
    e.km, (unsigned long)e.dur_s,
    moveClassName(e.type),
    e.avg_kmh, e.max_kmh);
  if (n <= 0 || (size_t)n >= sizeof(body)) return;
  bool ok = Radio.sendEncrypted(body);
  Serial.printf("[TRIPEND] %s type=%s km=%.3f (%s)\n",
                e.id, moveClassName(e.type), e.km, ok ? "OK" : "FAIL");
}

// ----- Persist a started/ended trip's meta -----
static void persistTripStart(const TripStart& s) {
  trip_storage::Meta meta = {};
  strncpy(meta.device, g_deviceId,   sizeof(meta.device) - 1);
  strncpy(meta.hwid,   g_deviceHwid, sizeof(meta.hwid)   - 1);
  strncpy(meta.id,     s.id,         sizeof(meta.id)     - 1);
  meta.sts  = s.ts;
  meta.slat = s.lat;
  meta.slon = s.lon;
  strncpy(meta.type, "unknown", sizeof(meta.type) - 1);
  trip_storage::writeMeta(s.id, meta);
  trip_storage::setInProgress(s.id);
}

static void persistTripEnd(const TripEnd& e) {
  trip_storage::Meta meta = {};
  strncpy(meta.device, g_deviceId,   sizeof(meta.device) - 1);
  strncpy(meta.hwid,   g_deviceHwid, sizeof(meta.hwid)   - 1);
  strncpy(meta.id,     e.id,         sizeof(meta.id)     - 1);
  meta.sts     = e.sts;
  meta.ets     = e.ets;
  meta.slat    = e.slat;
  meta.slon    = e.slon;
  meta.elat    = e.elat;
  meta.elon    = e.elon;
  meta.km      = e.km;
  meta.dur_s   = e.dur_s;
  strncpy(meta.type, moveClassName(e.type), sizeof(meta.type) - 1);
  meta.avg_kmh = e.avg_kmh;
  meta.max_kmh = e.max_kmh;
  trip_storage::writeMeta(e.id, meta);
  trip_storage::clearInProgress();
  trip_storage::markSyncStatus(e.id, trip_storage::STATUS_UNSENT);
}

// If the device reset mid-trip, in_progress.txt still names a trip left
// with start-only meta. Rebuild end-meta from the on-flash fixes so it
// syncs as a normal trip, not ets=0/km=0/unknown (PicoB try_resume).
static void finalizeInterruptedTrip() {
  char id[trip_storage::TRIP_ID_MAX];
  if (trip_storage::readInProgress(id, sizeof(id)) == 0) return;  // clean shutdown

  if (trip_storage::tripNpts(id) < 2) {
    Serial.printf("[TRIP] interrupted %s too short — discarding\n", id);
    trip_storage::deleteTrip(id);
    trip_storage::clearInProgress();
    return;
  }

  sync_codec::Fix buf[16];
  sync_codec::Fix first{}, last{}, prev{};
  bool   haveFirst = false, havePrev = false;
  double km = 0.0;
  float  sumSpd = 0.0f, maxSpd = 0.0f, prevSpd = 0.0f;
  uint32_t count = 0;
  size_t from = 0;
  while (true) {
    size_t got = trip_storage::readFixesRange(id, from, 16, buf);
    if (got == 0) break;
    for (size_t i = 0; i < got; i++) {
      const sync_codec::Fix& f = buf[i];
      if (!haveFirst) { first = f; haveFirst = true; prevSpd = f.spd > 0 ? f.spd : 0.0f; }
      if (havePrev) km += TripTracker::distM(prev.lat, prev.lon, f.lat, f.lon) / 1000.0;
      // Sustained-max: min(prev,cur) so a doppler spike isn't the max.
      float sustained = (f.spd < prevSpd) ? f.spd : prevSpd;
      if (sustained > maxSpd) maxSpd = sustained;
      prevSpd = f.spd;
      sumSpd += f.spd;
      count++;
      prev = f; havePrev = true; last = f;
    }
    from += got;
    if (got < 16) break;
  }
  if (count < 2) {  // too little survived parsing
    trip_storage::deleteTrip(id);
    trip_storage::clearInProgress();
    return;
  }

  float     avg  = sumSpd / (float)count;
  MoveClass type = TripTracker::classifyWithMax(avg, maxSpd);

  trip_storage::Meta meta = {};
  strncpy(meta.device, g_deviceId,   sizeof(meta.device) - 1);
  strncpy(meta.hwid,   g_deviceHwid, sizeof(meta.hwid)   - 1);
  strncpy(meta.id,     id,           sizeof(meta.id)     - 1);
  meta.sts     = first.ts;
  meta.ets     = last.ts;
  meta.slat    = first.lat;
  meta.slon    = first.lon;
  meta.elat    = last.lat;
  meta.elon    = last.lon;
  meta.km      = (float)km;
  meta.dur_s   = (last.ts > first.ts) ? (uint32_t)(last.ts - first.ts) : 1u;
  strncpy(meta.type, moveClassName(type), sizeof(meta.type) - 1);
  meta.avg_kmh = avg;
  meta.max_kmh = maxSpd;
  trip_storage::writeMeta(id, meta);
  trip_storage::clearInProgress();
  trip_storage::markSyncStatus(id, trip_storage::STATUS_UNSENT);

  Serial.printf("[TRIP] recovered interrupted %s: %u pts %.3f km %us %s\n",
                id, (unsigned)count, km, (unsigned)meta.dur_s, moveClassName(type));
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println("=== ESP32-C3 LoRa+WiFi Chat + GPS ===");

  Chat.begin();             // also calls LittleFS.begin
  loadDeviceId();           // overrides g_deviceId from /device_id.txt if present
  computeHwid();            // factory-burned chip MAC -> g_deviceHwid (hex)
  Serial.printf("device_hwid=%s\n", g_deviceHwid);
  trip_storage::begin();
  finalizeInterruptedTrip();   // salvage a trip the last reset cut short
  Sync.begin();
  Tracker.begin(g_deviceId);
  Presence.begin(g_deviceHwid, g_deviceId);
  Net.begin();
  Web.begin();
  Gps.begin();
  g_radioOk = Radio.begin();

  // One-shot boot announce so the Pi 5 "+ Add device" listen window can
  // discover unknown devices and rename them via the existing RENAME
  // protocol. Best-effort; if the radio failed to start we just skip it.
  sendDeviceAnnounce();
}

void loop() {
  Net.loop();
  Web.loop();

  // ---- GPS pipeline: parse NMEA, emit, feed tracker ----
  sync_codec::Fix fix;
  if (Gps.tryReadFix(&fix)) {
    broadcastGps(fix);

    TripStart ts_msg;
    TripEnd   te_msg;
    TrackerEvent ev = Tracker.update(fix, &ts_msg, &te_msg);

    if (ev == EV_TRIPSTART) {
      persistTripStart(ts_msg);
      // Retroactive prefix: any IDLE fixes that the tracker buffered
      // while motion was building up belong to this trip. The trigger
      // fix comes last in the polyline; the buffered fixes come first
      // so the trip's start coord matches actual departure.
      const size_t n = Tracker.precedingCount();
      for (size_t i = 0; i < n; i++) {
        trip_storage::writeFix(ts_msg.id, Tracker.precedingFix(i));
      }
      trip_storage::writeFix(ts_msg.id, fix);
      broadcastTripStart(ts_msg);
    } else if (ev == EV_TRIPEND) {
      if (te_msg.confirmed) {
        persistTripEnd(te_msg);
        broadcastTripEnd(te_msg);
        // Announce immediately so the Pi can pull the data while we're
        // still on the air; Sync.tick retries every 5 min if needed.
        Sync.announce();
      } else {
        // False-start: trigger fired but no real motion materialised
        // (GPS multipath burst followed by stop). Wipe the files and
        // skip the broadcast — keeps the hub DB and sync queue clean.
        Serial.printf("[TRIP] discarding unconfirmed %s (km=%.3f max=%.2f)\n",
                      te_msg.id, te_msg.km, te_msg.max_kmh);
        trip_storage::deleteTrip(te_msg.id);
        trip_storage::clearInProgress();
      }
    } else if (Tracker.inTrip()) {
      trip_storage::writeFix(Tracker.tripId(), fix);
    }

    // Cadence selection by class (smoothed speed + hysteresis)
    if (Tracker.inTrip()) {
      float smoothed = pushAndSmoothSpd(fix.spd);
      MoveClass nc = pickClass(smoothed, g_class);
      if (nc != g_class) {
        g_class = nc;
        Gps.setIntervalMs(intervalForClass(g_class));
        Serial.printf("[cadence] class=%s interval=%lums\n",
                      moveClassName(g_class),
                      (unsigned long)Gps.getIntervalMs());
      }
    } else if (g_class != CLASS_IDLE) {
      g_class = CLASS_IDLE;
      Gps.setIntervalMs(intervalForClass(CLASS_IDLE));
      g_recentN = g_recentIdx = 0;
    }
  }

  // ---- LoRa RX: sync first, then chat filter, then drop ----
  if (g_radioOk) {
    char text[251];
    LoraRxResult meta;
    if (Radio.pollReceive(text, sizeof(text), &meta)) {
      Serial.printf("[RX] '%s' (rssi=%d snr=%.1f)\n",
                    text, meta.rssi, meta.snr);
      // DEVICE: must be checked first — rename matches reboot the node;
      // hello/renamed echoes are silently consumed. Either way, the
      // packet doesn't fall through to chat or sync layers.
      if (handleDeviceMessage(text, meta.rssi)) {
        // unreachable for rename; consumed for hello/renamed
      } else if (handleQposMessage(text)) {
        // on-demand position query — fire-and-forget GPS broadcast
      } else if (handleWhoMessage(text)) {
        // presence probe — re-announce DEVICE
      } else if (Sync.onMessage(text)) {
        // handled by sync layer
      } else if (strncmp(text, "CHAT:", 5) == 0) {
        Chat.addRx(text + 5, meta.rssi, meta.snr);
        Chat.flushIfNeeded();
      }
    }
  }
  Sync.tick(millis());

  uint32_t now = millis();

  // Liveness heartbeat: re-announce DEVICE every 60 s so peers + the hub
  // keep us "online". touchSelf keeps our own roster entry fresh.
  static uint32_t lastAnnounce = 0;
  if (now - lastAnnounce >= 60000UL) {
    lastAnnounce = now;
    sendDeviceAnnounce();
    Presence.touchSelf(now);
  }

  static uint32_t lastTick = 0;
  if (now - lastTick >= 5000) {
    lastTick = now;
    Serial.printf("[heartbeat] radio=%s wifi=%s%s clients=%u/%d uptime=%lus "
                  "chat_msgs=%u gps=%s nmea_chars=%lu fix_sentences=%lu "
                  "csum_fail=%lu class=%s in_trip=%s\n",
                  g_radioOk ? "OK" : "FAIL",
                  Net.isUp() ? "up" : "DOWN",
                  Net.isSleeping() ? "(sleep)" : "",
                  Net.clientCount(), wifi::MAX_CLIENTS,
                  (unsigned long)(now / 1000),
                  (unsigned)Chat.count(),
                  Gps.hasFix() ? "fix" : "no_fix",
                  (unsigned long)Gps.charsProcessed(),
                  (unsigned long)Gps.sentencesWithFix(),
                  (unsigned long)Gps.failedChecksum(),
                  moveClassName(g_class),
                  Tracker.inTrip() ? "yes" : "no");
  }
}
