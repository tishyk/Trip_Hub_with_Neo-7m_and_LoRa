#pragma once
#include <stddef.h>
#include <stdint.h>

namespace sync_codec {

struct Fix {
  int32_t ts;
  double  lat;
  double  lon;
  float   alt;
  float   spd;
};

constexpr size_t MAX_PACKET_BYTES = 200;

size_t encode(const Fix* fixes, size_t nFixes, char* out, size_t outCap);

bool   parseFixLine(const char* line, Fix* out);

}  // namespace sync_codec
