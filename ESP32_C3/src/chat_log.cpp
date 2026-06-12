#include "chat_log.h"
#include <Arduino.h>
#include <LittleFS.h>
#include <string.h>

namespace {

constexpr uint32_t MAGIC      = 0xC0FFEE01;
constexpr const char* PATH    = "/chat.dat";

ChatMessage g_msgs[CHAT_MAX];
size_t      g_count    = 0;
uint32_t    g_nextId   = 1;
size_t      g_dirty    = 0;
bool        g_fsOk     = false;

void appendInPlace(const ChatMessage& m) {
  if (g_count < CHAT_MAX) {
    g_msgs[g_count++] = m;
  } else {
    memmove(&g_msgs[0], &g_msgs[1], (CHAT_MAX - 1) * sizeof(ChatMessage));
    g_msgs[CHAT_MAX - 1] = m;
  }
}

bool save() {
  if (!g_fsOk) return false;
  File f = LittleFS.open(PATH, "w");
  if (!f) return false;
  uint32_t magic = MAGIC;
  uint32_t cnt   = (uint32_t)g_count;
  f.write((const uint8_t*)&magic,    sizeof(magic));
  f.write((const uint8_t*)&g_nextId, sizeof(g_nextId));
  f.write((const uint8_t*)&cnt,      sizeof(cnt));
  if (g_count > 0) {
    f.write((const uint8_t*)g_msgs, g_count * sizeof(ChatMessage));
  }
  f.close();
  g_dirty = 0;
  return true;
}

bool load() {
  if (!g_fsOk) return false;
  if (!LittleFS.exists(PATH)) return false;
  File f = LittleFS.open(PATH, "r");
  if (!f) return false;
  uint32_t magic = 0, nextId = 1, cnt = 0;
  if (f.read((uint8_t*)&magic,  sizeof(magic))  != sizeof(magic))  { f.close(); return false; }
  if (f.read((uint8_t*)&nextId, sizeof(nextId)) != sizeof(nextId)) { f.close(); return false; }
  if (f.read((uint8_t*)&cnt,    sizeof(cnt))    != sizeof(cnt))    { f.close(); return false; }
  if (magic != MAGIC || cnt > CHAT_MAX) { f.close(); return false; }
  if (cnt > 0) {
    size_t want = cnt * sizeof(ChatMessage);
    if (f.read((uint8_t*)g_msgs, want) != (int)want) { f.close(); return false; }
  }
  f.close();
  g_count  = (size_t)cnt;
  g_nextId = nextId;
  return true;
}

void markDirty() {
  g_dirty++;
}

}  // anonymous namespace

// Protocol-shaped prefixes that must NEVER live in the chat log. Stored
// messages from before the CHAT: prefix filter existed leaked GPS / sync
// payloads into chat.dat; this list lets us strip them at boot.
static const char* PROTOCOL_PREFIXES[] = {
  "GPS:", "TRIPSTART:", "TRIPEND:",
  "SYNC:", "RTRIPS:", "RTRIP:", "RPTS:",
  "QTRIPS:", "QTRIP:", "QPTS:", "ACK:",
  nullptr,
};
static bool looksLikeProtocol(const char* text) {
  if (!text) return false;
  for (size_t i = 0; PROTOCOL_PREFIXES[i]; i++) {
    size_t n = strlen(PROTOCOL_PREFIXES[i]);
    if (strncmp(text, PROTOCOL_PREFIXES[i], n) == 0) return true;
  }
  return false;
}

void ChatLog::begin() {
  g_fsOk = LittleFS.begin(/*formatOnFail=*/true);
  Serial.printf("LittleFS: %s\n", g_fsOk ? "OK" : "FAIL");
  if (g_fsOk && load()) {
    Serial.printf("Chat log: loaded %u messages, nextId=%u\n",
                  (unsigned)g_count, (unsigned)g_nextId);
    // One-time legacy cleanup: drop any persisted entry whose text
    // looks like a LoRa protocol payload. After the first boot post-
    // upgrade this becomes a no-op.
    size_t kept = 0;
    for (size_t i = 0; i < g_count; i++) {
      if (!looksLikeProtocol(g_msgs[i].text)) {
        if (kept != i) g_msgs[kept] = g_msgs[i];
        kept++;
      }
    }
    if (kept != g_count) {
      Serial.printf("Chat log: pruned %u protocol-shaped entries\n",
                    (unsigned)(g_count - kept));
      g_count = kept;
      save();
    }
  } else {
    g_count = 0;
    g_nextId = 1;
    Serial.println("Chat log: starting fresh");
  }
}

uint32_t ChatLog::addRx(const char* text, int16_t rssi, float snr) {
  ChatMessage m{};
  m.id        = g_nextId++;
  m.timestamp = (uint32_t)(millis() / 1000);
  m.incoming  = true;
  m.sent      = true;
  m.rssi      = rssi;
  m.snr       = snr;
  strncpy(m.text, text, CHAT_TEXT_BYTES - 1);
  m.text[CHAT_TEXT_BYTES - 1] = '\0';
  appendInPlace(m);
  markDirty();
  return m.id;
}

uint32_t ChatLog::addTx(const char* text, bool sent) {
  ChatMessage m{};
  m.id        = g_nextId++;
  m.timestamp = (uint32_t)(millis() / 1000);
  m.incoming  = false;
  m.sent      = sent;
  m.rssi      = INT16_MIN;
  m.snr       = NAN;
  strncpy(m.text, text, CHAT_TEXT_BYTES - 1);
  m.text[CHAT_TEXT_BYTES - 1] = '\0';
  appendInPlace(m);
  markDirty();
  return m.id;
}

bool ChatLog::markSent(uint32_t id, bool sent) {
  for (size_t i = 0; i < g_count; i++) {
    if (g_msgs[i].id == id) {
      g_msgs[i].sent = sent;
      markDirty();
      return true;
    }
  }
  return false;
}

bool ChatLog::remove(uint32_t id) {
  for (size_t i = 0; i < g_count; i++) {
    if (g_msgs[i].id == id) {
      memmove(&g_msgs[i], &g_msgs[i+1],
              (g_count - i - 1) * sizeof(ChatMessage));
      g_count--;
      markDirty();
      return true;
    }
  }
  return false;
}

size_t ChatLog::getSince(uint32_t sinceId, ChatMessage* out, size_t maxOut) {
  size_t n = 0;
  for (size_t i = 0; i < g_count && n < maxOut; i++) {
    if (g_msgs[i].id > sinceId) {
      out[n++] = g_msgs[i];
    }
  }
  return n;
}

size_t ChatLog::count() {
  return g_count;
}

void ChatLog::flushIfNeeded() {
  if (g_dirty > 0) save();
}

void ChatLog::forceFlush() {
  if (g_dirty > 0) save();
}

ChatLog Chat;
