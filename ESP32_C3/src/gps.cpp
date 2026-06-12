#include "gps.h"
#include "config.h"
#include <Arduino.h>
#include <TinyGPSPlus.h>
#include <math.h>

namespace {
HardwareSerial GPSSerial(1);
TinyGPSPlus    g_tgps;

uint32_t g_intervalMs = 60000;
uint32_t g_lastEmitMs = 0;
bool     g_haveFix    = false;

// ---- Position-jump filter (mirrors PicoB gps_module.py) ----
// Multipath occasionally produces a confident-looking fix (good HDOP,
// plenty of sats) whose position is 100+ m off the actual location.
// HDOP gate doesn't catch this; the receiver is confident. Compare
// against the last accepted fix's expected delta and reject outliers.
constexpr float   POSJ_MIN_FLOOR_M       = 10.0f;  // smallest sane threshold
constexpr float   POSJ_NOISE_M           = 20.0f;  // GPS noise headroom
constexpr size_t  POSJ_HISTORY_LEN       = 5;      // median of recent deltas
constexpr uint8_t POSJ_MAX_REJECTS       = 3;      // force-accept after this many

double  g_lastAccLat   = 0.0;
double  g_lastAccLon   = 0.0;
bool    g_haveLastAcc  = false;
float   g_recentDeltas[POSJ_HISTORY_LEN] = {0};
size_t  g_deltaIdx     = 0;
size_t  g_deltaCount   = 0;
uint8_t g_consecRej    = 0;

float approxDistM(double lat1, double lon1, double lat2, double lon2) {
  double avgLat = (lat1 + lat2) * 0.5 * 0.0174532925;
  double cosLat = cos(avgLat);
  double dlat = (lat2 - lat1) * 111320.0;
  double dlon = (lon2 - lon1) * 111320.0 * cosLat;
  return (float)sqrt(dlat * dlat + dlon * dlon);
}

// Hinnant's days-from-civil. Converts (year, month, day) in the proleptic
// Gregorian calendar to days since 1970-01-01. Then add HMS for UTC epoch.
int32_t makeUtcEpoch(int y, int m, int d, int hh, int mm, int ss) {
  if (m <= 2) y -= 1;
  int era = (y >= 0 ? y : y - 399) / 400;
  unsigned yoe = (unsigned)(y - era * 400);
  unsigned doy = (153u * (unsigned)(m + (m > 2 ? -3 : 9)) + 2u) / 5u
               + (unsigned)d - 1u;
  unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
  long long days = (long long)era * 146097LL + (long long)doe - 719468LL;
  return (int32_t)(days * 86400LL
                   + (long long)hh * 3600LL
                   + (long long)mm * 60LL
                   + (long long)ss);
}
}  // namespace

void GpsReader::begin() {
  GPSSerial.begin(9600, SERIAL_8N1, pins::GPS_RX, pins::GPS_TX);
  g_intervalMs = gps_cfg::IDLE_INTERVAL_MS;
  Serial.printf("GPS UART1: RX=GPIO%d TX=GPIO%d 9600 8N1 (TinyGPSPlus)\n",
                pins::GPS_RX, pins::GPS_TX);
}

void GpsReader::setIntervalMs(uint32_t ms) {
  if (ms < 500) ms = 500;
  g_intervalMs = ms;
}

uint32_t GpsReader::getIntervalMs() const { return g_intervalMs; }
bool GpsReader::hasFix()         const     { return g_haveFix;    }

uint32_t GpsReader::charsProcessed()   const { return g_tgps.charsProcessed();   }
uint32_t GpsReader::sentencesWithFix() const { return g_tgps.sentencesWithFix(); }
uint32_t GpsReader::failedChecksum()   const { return g_tgps.failedChecksum();   }

bool GpsReader::latestFix(sync_codec::Fix* out) const {
  // Use the last fix we've ever accepted (post-quality-gate, post-jump
  // filter). g_lastAccLat/Lon are updated only when a fix passes every
  // filter, so we never hand back a multipath spike.
  if (!g_haveFix || !g_haveLastAcc || out == nullptr) return false;
  out->lat = g_lastAccLat;
  out->lon = g_lastAccLon;
  out->alt = g_tgps.altitude.isValid() ? (float)g_tgps.altitude.meters() : 0.0f;
  out->spd = g_tgps.speed.isValid()    ? (float)g_tgps.speed.kmph()      : 0.0f;
  if (g_tgps.date.isValid() && g_tgps.time.isValid()) {
    out->ts = makeUtcEpoch(g_tgps.date.year(),  g_tgps.date.month(),
                           g_tgps.date.day(),
                           g_tgps.time.hour(), g_tgps.time.minute(),
                           g_tgps.time.second());
  } else {
    out->ts = 0;
  }
  return true;
}

// Per-fix quality gate: rejects fixes the receiver itself flagged as
// poor geometry. Multipath spikes happen almost exclusively when sat
// count is low or HDOP is high.
//
// Thresholds chosen for marginal-sky-view use. HDOP 10 implies ~25 m
// position uncertainty — recognisable polyline at walking cadence.
// The downstream position-jump filter catches multi-fix glitches even
// when HDOP is moderate.
constexpr uint32_t MIN_NSAT      = 4;     // 4 sats minimum for 3D fix
constexpr uint32_t MAX_HDOP_X100 = 1000;  // 10.00 — moderate geometry OK

bool GpsReader::tryReadFix(sync_codec::Fix* out) {
  // Drain UART without allocating; TinyGPSPlus parses byte-by-byte.
  while (GPSSerial.available()) {
    g_tgps.encode((char)GPSSerial.read());
  }

  if (!g_tgps.location.isValid() || g_tgps.location.age() > 5000) return false;
  if (!g_tgps.date.isValid()     || !g_tgps.time.isValid())       return false;

  // Quality gate. Skip the fix entirely when either metric fails; the
  // 'age' check above will keep returning false until the receiver
  // produces a fix with good enough geometry.
  if (g_tgps.satellites.isValid() &&
      g_tgps.satellites.value() < MIN_NSAT) {
    return false;
  }
  if (g_tgps.hdop.isValid() &&
      g_tgps.hdop.value() > MAX_HDOP_X100) {
    return false;
  }

  uint32_t now = millis();
  if (g_haveFix && (now - g_lastEmitMs) < g_intervalMs) return false;

  // Position-jump rejection: cross-check the new fix against the
  // last accepted one. Multipath fixes typically jump 50-200 m
  // while genuine motion advances by ~ (cadence × speed). Threshold
  // is 2× the median of recent deltas plus a 20 m noise floor.
  double curLat = g_tgps.location.lat();
  double curLon = g_tgps.location.lng();
  if (g_haveLastAcc) {
    float delta = approxDistM(g_lastAccLat, g_lastAccLon, curLat, curLon);
    float medianD = 0.0f;
    if (g_deltaCount > 0) {
      // Insertion sort on a tiny array (max 5) to get the median.
      float sorted[POSJ_HISTORY_LEN];
      for (size_t i = 0; i < g_deltaCount; i++) sorted[i] = g_recentDeltas[i];
      for (size_t i = 1; i < g_deltaCount; i++) {
        float v = sorted[i]; size_t j = i;
        while (j > 0 && sorted[j-1] > v) { sorted[j] = sorted[j-1]; j--; }
        sorted[j] = v;
      }
      medianD = sorted[g_deltaCount / 2];
    }
    float floorBased = (2.0f * medianD > POSJ_MIN_FLOOR_M)
                       ? 2.0f * medianD : POSJ_MIN_FLOOR_M;
    float threshold  = floorBased + POSJ_NOISE_M;
    if (delta > threshold && g_consecRej < POSJ_MAX_REJECTS) {
      g_consecRej++;
      return false;
    }
    // Accept. Two paths:
    //   (a) delta within threshold → roll the delta into history so
    //       the median tracks the receiver's current behaviour.
    //   (b) delta over threshold but force-accepted (consecRej hit
    //       max) → reset retry counter but DON'T push this delta:
    //       it's almost certainly a real outlier and feeding it back
    //       would inflate the median, making future jumps easier to
    //       accept and degrading the filter.
    g_consecRej = 0;
    if (delta <= threshold) {
      g_recentDeltas[g_deltaIdx] = delta;
      g_deltaIdx = (g_deltaIdx + 1) % POSJ_HISTORY_LEN;
      if (g_deltaCount < POSJ_HISTORY_LEN) g_deltaCount++;
    }
  }
  g_lastAccLat = curLat;
  g_lastAccLon = curLon;
  g_haveLastAcc = true;

  if (out) {
    out->ts  = makeUtcEpoch(g_tgps.date.year(),  g_tgps.date.month(),
                            g_tgps.date.day(),
                            g_tgps.time.hour(), g_tgps.time.minute(),
                            g_tgps.time.second());
    out->lat = g_tgps.location.lat();
    out->lon = g_tgps.location.lng();
    out->alt = g_tgps.altitude.isValid() ? (float)g_tgps.altitude.meters() : 0.0f;
    out->spd = g_tgps.speed.isValid()    ? (float)g_tgps.speed.kmph()      : 0.0f;
  }
  g_haveFix    = true;
  g_lastEmitMs = now;
  return true;
}

GpsReader Gps;
