#include "presence.h"
#include <Arduino.h>
#include <string.h>
#include <stdio.h>

PresenceTable Presence;

int PresenceTable::find(const char* hwid) const {
  for (size_t i = 0; i < MAX_PEERS; i++) {
    if (m_peers[i].used && strcmp(m_peers[i].hwid, hwid) == 0) return (int)i;
  }
  return -1;
}

void PresenceTable::begin(const char* selfHwid, const char* selfName) {
  memset(m_peers, 0, sizeof(m_peers));
  // Slot 0 is reserved for this device.
  strncpy(m_peers[0].hwid, selfHwid ? selfHwid : "", sizeof(m_peers[0].hwid) - 1);
  strncpy(m_peers[0].name, selfName ? selfName : "", sizeof(m_peers[0].name) - 1);
  m_peers[0].self       = true;
  m_peers[0].used       = true;
  m_peers[0].rssi       = INT16_MIN;   // self has no link signal
  m_peers[0].lastSeenMs = millis();
}

void PresenceTable::touchSelf(uint32_t nowMs) {
  m_peers[0].lastSeenMs = nowMs;
}

void PresenceTable::noteSeen(const char* hwid, const char* name, int16_t rssi, uint32_t nowMs) {
  if (!hwid || !hwid[0]) return;
  int idx = find(hwid);
  if (idx < 0) {
    // New peer: take a free slot, else evict the stalest non-self entry.
    for (size_t i = 1; i < MAX_PEERS; i++) {
      if (!m_peers[i].used) { idx = (int)i; break; }
    }
    if (idx < 0) {
      uint32_t oldest = 0;
      idx = 1;
      for (size_t i = 1; i < MAX_PEERS; i++) {
        uint32_t age = nowMs - m_peers[i].lastSeenMs;
        if (age >= oldest) { oldest = age; idx = (int)i; }
      }
    }
    memset(&m_peers[idx], 0, sizeof(Peer));
    m_peers[idx].used = true;
    strncpy(m_peers[idx].hwid, hwid, sizeof(m_peers[idx].hwid) - 1);
  }
  if (name && name[0]) {
    strncpy(m_peers[idx].name, name, sizeof(m_peers[idx].name) - 1);
    m_peers[idx].name[sizeof(m_peers[idx].name) - 1] = '\0';
  }
  m_peers[idx].rssi       = rssi;
  m_peers[idx].lastSeenMs = nowMs;
}

size_t PresenceTable::toJson(char* out, size_t cap, uint32_t nowMs) const {
  if (!out || cap < 3) return 0;
  size_t pos = 0;
  out[pos++] = '[';
  bool first = true;
  for (size_t i = 0; i < MAX_PEERS; i++) {
    if (!m_peers[i].used) continue;
    uint32_t ageMs = nowMs - m_peers[i].lastSeenMs;
    int w = snprintf(out + pos, cap - pos,
                     "%s{\"name\":\"%s\",\"hwid\":\"%s\",\"age\":%lu,"
                     "\"online\":%s,\"self\":%s,\"rssi\":%d}",
                     first ? "" : ",",
                     m_peers[i].name, m_peers[i].hwid,
                     (unsigned long)(ageMs / 1000UL),
                     ageMs < ONLINE_WINDOW_MS ? "true" : "false",
                     m_peers[i].self ? "true" : "false",
                     (int)m_peers[i].rssi);
    if (w <= 0 || (size_t)w >= cap - pos) break;  // out of room — stop cleanly
    pos += (size_t)w;
    first = false;
  }
  if (pos + 1 < cap) out[pos++] = ']';
  out[pos] = '\0';
  return pos;
}
