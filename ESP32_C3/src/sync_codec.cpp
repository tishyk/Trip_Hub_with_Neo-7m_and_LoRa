#include "sync_codec.h"
#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace sync_codec {

size_t encode(const Fix* fixes, size_t nFixes, char* out, size_t outCap) {
  if (!out || outCap < 4) return 0;
  if (!fixes || nFixes == 0) {
    if (outCap >= 3) { out[0] = '['; out[1] = ']'; out[2] = '\0'; }
    return 0;
  }

  out[0] = '[';
  size_t pos    = 1;
  size_t packed = 0;
  Fix prev{};

  char inner[64];

  for (size_t i = 0; i < nFixes; i++) {
    const Fix& f = fixes[i];
    int n;
    if (packed == 0) {
      n = snprintf(inner, sizeof(inner),
                   "[%ld,%.6f,%.6f,%d,%.2f]",
                   (long)f.ts, f.lat, f.lon,
                   (int)lroundf(f.alt), f.spd);
    } else {
      int32_t dts  = (int32_t)(f.ts - prev.ts);
      int32_t dlat = (int32_t)lround((f.lat - prev.lat) * 100000.0);
      int32_t dlon = (int32_t)lround((f.lon - prev.lon) * 100000.0);
      int32_t dalt = (int32_t)lroundf(f.alt - prev.alt);
      int32_t dspd = (int32_t)lroundf((f.spd - prev.spd) * 10.0f);
      n = snprintf(inner, sizeof(inner),
                   "[%ld,%ld,%ld,%ld,%ld]",
                   (long)dts, (long)dlat, (long)dlon,
                   (long)dalt, (long)dspd);
    }
    if (n <= 0 || (size_t)n >= sizeof(inner)) break;

    size_t need = (size_t)n + (packed > 0 ? 1 : 0) + 1;  // sep + ']'
    if (pos + need >= outCap) break;
    if (pos + (size_t)n + (packed > 0 ? 1 : 0) + 1 > MAX_PACKET_BYTES) {
      if (packed > 0) break;
    }

    if (packed > 0) out[pos++] = ',';
    memcpy(out + pos, inner, (size_t)n);
    pos += (size_t)n;
    prev = f;
    packed++;
  }

  out[pos++] = ']';
  out[pos]   = '\0';
  return packed;
}

bool parseFixLine(const char* line, Fix* out) {
  if (!line || !out) return false;
  const char* p = strchr(line, '[');
  if (!p) return false;
  p++;
  char* end = nullptr;

  long ts = strtol(p, &end, 10);
  if (!end || *end != ',') return false;
  p = end + 1;

  double lat = strtod(p, &end);
  if (!end || *end != ',') return false;
  p = end + 1;

  double lon = strtod(p, &end);
  if (!end || *end != ',') return false;
  p = end + 1;

  double alt = strtod(p, &end);
  if (!end || *end != ',') return false;
  p = end + 1;

  double spd = strtod(p, &end);
  if (!end) return false;

  out->ts  = (int32_t)ts;
  out->lat = lat;
  out->lon = lon;
  out->alt = (float)alt;
  out->spd = (float)spd;
  return true;
}

}  // namespace sync_codec
