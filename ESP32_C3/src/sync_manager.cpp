#include "sync_manager.h"
#include "config.h"
#include "lora_radio.h"
#include "sync_codec.h"
#include "trip_storage.h"
#include <Arduino.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace {
constexpr uint32_t SYNC_RETRY_MS    = 5UL * 60UL * 1000UL;
constexpr uint32_t SESSION_IDLE_MS  = 30UL * 1000UL;
}

void SyncManager::begin() {
  m_sessionActive = false;
  m_lastSyncMs    = 0;
  m_lastQActivityMs = 0;
  m_initialAnnounce = false;
}

void SyncManager::announce() {
  // Wire prefix carries the permanent hwid (phase 3) — Pi keys sessions
  // by it so renames never redirect the session. Bridge OLED resolves
  // hwid -> friendly name from cached DEVICE: hello announces.
  char buf[40];
  snprintf(buf, sizeof(buf), "SYNC:%s", g_deviceHwid);
  bool ok = Radio.sendEncrypted(buf);
  Serial.printf("[SYNC] announce -> %s (%s)\n", buf, ok ? "OK" : "FAIL");
  m_lastSyncMs = millis();
}

bool SyncManager::onMessage(const char* text) {
  if (!text) return false;

  if (strncmp(text, "QTRIPS:", 7) == 0) {
    handleQtrips(text + 7);
    return true;
  }
  if (strncmp(text, "QTRIP:", 6) == 0) {
    handleQtrip(text + 6);
    return true;
  }
  if (strncmp(text, "QPTS:", 5) == 0) {
    handleQpts(text + 5);
    return true;
  }
  if (strncmp(text, "ACK:", 4) == 0) {
    handleAck(text + 4);
    return true;
  }
  return false;
}

void SyncManager::handleQtrips(const char* deviceArg) {
  while (*deviceArg == ' ') deviceArg++;
  // Accept either our hwid (new) or our current name (legacy Pi that
  // hasn't been redeployed yet). Full-token equality — strncmp by
  // length alone would prefix-match a longer name (e.g. 'esp32-c3-foo'
  // matching us as 'esp32-c3').
  bool isHwid = strcmp(deviceArg, g_deviceHwid) == 0;
  bool isName = strcmp(deviceArg, g_deviceId)   == 0;
  if (!isHwid && !isName) {
    return;  // not for us
  }
  m_sessionActive   = true;
  m_lastQActivityMs = millis();

  char ids[trip_storage::MAX_UNSENT][trip_storage::TRIP_ID_MAX];
  size_t n = trip_storage::getUnsentTrips(ids, trip_storage::MAX_UNSENT);

  char reply[240];
  int  pos = snprintf(reply, sizeof(reply), "RTRIPS:%s:", g_deviceHwid);
  for (size_t i = 0; i < n && pos < (int)sizeof(reply) - 32; i++) {
    size_t pts = trip_storage::tripNpts(ids[i]);
    int w = snprintf(reply + pos, sizeof(reply) - pos,
                     "%s%s:%u",
                     i == 0 ? "" : ",", ids[i], (unsigned)pts);
    if (w <= 0) break;
    pos += w;
  }
  bool ok = Radio.sendEncrypted(reply);
  Serial.printf("[SYNC] %s (%s, %u trips)\n", reply, ok ? "OK" : "FAIL", (unsigned)n);
}

void SyncManager::handleQtrip(const char* tripId) {
  while (*tripId == ' ') tripId++;
  m_sessionActive   = true;
  m_lastQActivityMs = millis();

  // Buffers sized for the slimmed-down JSON (no 'd' field, 5-decimal
  // coords). Older 200 B buffer was silently truncating the meta to
  // ~217 B mid-field, breaking hub parse + stalling sync forever.
  char meta[280];
  size_t mlen = trip_storage::readMetaJson(tripId, meta, sizeof(meta));
  if (mlen == 0) {
    Serial.printf("[SYNC] QTRIP: trip %s not found\n", tripId);
    return;
  }
  // Strip a legacy `"d":"...",` field if the on-flash JSON was
  // written by an older firmware that included it. AES-padded packet
  // must fit in the 256 B SX1278 FIFO; removing 'd' (≈ 20 chars)
  // brings the worst-case under the limit. The hub already resolves
  // the renameable name from hwid via the devices table.
  char* dKey = strstr(meta, "\"d\":\"");
  if (dKey) {
    char* dEnd = strchr(dKey + 5, '\"');                       // closing "
    if (dEnd && *(dEnd + 1) == ',') {
      // shift the rest of the string left over the `"d":"...",`
      char* after = dEnd + 2;
      size_t shiftLen = strlen(after) + 1;                     // incl '\0'
      memmove(dKey, after, shiftLen);
      mlen -= (size_t)(after - dKey);
    }
  }
  char reply[300];
  snprintf(reply, sizeof(reply), "RTRIP:%s:%s", tripId, meta);
  bool ok = Radio.sendEncrypted(reply);
  Serial.printf("[SYNC] RTRIP %s (%s, %u bytes)\n", tripId,
                ok ? "OK" : "FAIL", (unsigned)strlen(reply));
}

void SyncManager::handleQpts(const char* args) {
  m_sessionActive   = true;
  m_lastQActivityMs = millis();

  char tripId[trip_storage::TRIP_ID_MAX];
  size_t fromIdx = 0, count = 0;
  const char* c1 = strchr(args, ':');
  if (!c1) return;
  size_t idLen = (size_t)(c1 - args);
  if (idLen >= sizeof(tripId)) return;
  memcpy(tripId, args, idLen);
  tripId[idLen] = '\0';

  const char* p2 = c1 + 1;
  char* end = nullptr;
  fromIdx = (size_t)strtoul(p2, &end, 10);
  if (!end || *end != ':') return;
  count = (size_t)strtoul(end + 1, &end, 10);
  if (count == 0) return;

  sync_codec::Fix fixes[16];
  size_t cap = count > 16 ? 16 : count;
  size_t nRead = trip_storage::readFixesRange(tripId, fromIdx, cap, fixes);

  // encoded must leave room for the RPTS prefix in the on-wire packet.
  // Wire = "RPTS:" + tripId + ":" + from + ":" + encoded → up to ~30 B
  // prefix worst case. SX1278 plaintext budget is 240 B (256 B AES-padded
  // cipher fits the FIFO). Cap encoded at 200 to stay comfortably under.
  char encoded[200];
  size_t nPacked = sync_codec::encode(fixes, nRead, encoded, sizeof(encoded));

  char reply[256];
  int rn = snprintf(reply, sizeof(reply), "RPTS:%s:%u:%s",
                    tripId, (unsigned)fromIdx, encoded);
  if (rn <= 0 || (size_t)rn >= sizeof(reply)) {
    Serial.printf("[SYNC] RPTS reply truncated (n=%d), refusing send\n", rn);
    return;
  }
  bool ok = Radio.sendEncrypted(reply);
  Serial.printf("[SYNC] RPTS %s from=%u packed=%u (%s)\n",
                tripId, (unsigned)fromIdx, (unsigned)nPacked,
                ok ? "OK" : "FAIL");

  if (nPacked > 0
      && trip_storage::syncStatus(tripId) == trip_storage::STATUS_UNSENT) {
    trip_storage::markSyncStatus(tripId, trip_storage::STATUS_SENT);
  }
}

void SyncManager::handleAck(const char* tripId) {
  while (*tripId == ' ') tripId++;
  // Hub has the trip — wipe it from flash and drop the sync_state
  // entry. Keeping confirmed trips around just costs LittleFS space
  // and slows the unsent-list scan on every RTRIPS.
  bool ok = trip_storage::deleteTrip(tripId);
  trip_storage::markSyncStatus(tripId, trip_storage::STATUS_CONFIRMED);
  m_sessionActive = false;
  Serial.printf("[SYNC] ACK %s -> deleted (%s)\n",
                tripId, ok ? "OK" : "missing");
}

void SyncManager::tick(uint32_t nowMs) {
  if (m_sessionActive
      && (nowMs - m_lastQActivityMs) > SESSION_IDLE_MS) {
    m_sessionActive = false;
    Serial.println("[SYNC] session idle timeout");
  }

  if (!m_initialAnnounce) {
    if (trip_storage::hasUnsentTrips()) {
      announce();
    }
    m_initialAnnounce = true;
    return;
  }

  if (!m_sessionActive
      && trip_storage::hasUnsentTrips()
      && (nowMs - m_lastSyncMs) >= SYNC_RETRY_MS) {
    Serial.println("[SYNC] retry: unsent trips");
    announce();
  }
}

SyncManager Sync;
