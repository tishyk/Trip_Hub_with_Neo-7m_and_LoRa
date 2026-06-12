#pragma once
#include <stdint.h>
#include <stddef.h>

// Liveness roster. Every node broadcasts a DEVICE: announce each minute;
// receiving one is a heartbeat. Slot 0 is always self. A device counts as
// online if its last announce is within ONLINE_WINDOW_MS.
class PresenceTable {
public:
  static constexpr size_t   MAX_PEERS        = 8;
  static constexpr uint32_t ONLINE_WINDOW_MS = 10UL * 60UL * 1000UL;  // 10 min

  void   begin(const char* selfHwid, const char* selfName);
  void   noteSeen(const char* hwid, const char* name, int16_t rssi, uint32_t nowMs);
  void   touchSelf(uint32_t nowMs);
  // Render roster as a JSON array into out: [{name,hwid,age,online,self}].
  size_t toJson(char* out, size_t cap, uint32_t nowMs) const;

private:
  struct Peer {
    char     hwid[20];   // RP2040 unique_id is 16 hex chars (+null); ESP32 is 12
    char     name[16];
    uint32_t lastSeenMs;
    int16_t  rssi;       // RSSI of last announce heard; INT16_MIN = unknown (self)
    bool     self;
    bool     used;
  };
  Peer m_peers[MAX_PEERS];
  int  find(const char* hwid) const;
};

extern PresenceTable Presence;
