#pragma once
#include <stddef.h>
#include <stdint.h>
#include <math.h>

constexpr size_t CHAT_MAX        = 50;
constexpr size_t CHAT_TEXT_BYTES = 201;
constexpr size_t CHAT_SAVE_EVERY = 25;

struct ChatMessage {
  uint32_t id;
  uint32_t timestamp;       // unix seconds (or millis/1000 if no RTC)
  bool     incoming;        // true = RX, false = TX
  bool     sent;            // for TX, true if radio confirmed
  int16_t  rssi;            // INT16_MIN if not applicable
  float    snr;             // NAN if not applicable
  char     text[CHAT_TEXT_BYTES];
};

class ChatLog {
public:
  void begin();
  uint32_t addRx(const char* text, int16_t rssi, float snr);
  uint32_t addTx(const char* text, bool sent);
  bool     markSent(uint32_t id, bool sent);
  bool     remove(uint32_t id);
  size_t   getSince(uint32_t sinceId, ChatMessage* out, size_t maxOut);
  size_t   count();
  void     flushIfNeeded();   // triggers save if dirty count >= CHAT_SAVE_EVERY
  void     forceFlush();      // unconditional save (e.g. on shutdown)
};

extern ChatLog Chat;
