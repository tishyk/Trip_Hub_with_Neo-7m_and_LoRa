#pragma once
#include <stddef.h>
#include <stdint.h>

class SyncManager {
public:
  void begin();
  bool onMessage(const char* text);
  void tick(uint32_t nowMs);
  void announce();
  bool sessionActive() const { return m_sessionActive; }

private:
  void handleQtrips(const char* deviceArg);
  void handleQtrip(const char* tripId);
  void handleQpts(const char* args);
  void handleAck(const char* tripId);

  bool     m_sessionActive    = false;
  uint32_t m_lastSyncMs       = 0;
  uint32_t m_lastQActivityMs  = 0;
  bool     m_initialAnnounce  = false;
};

extern SyncManager Sync;
