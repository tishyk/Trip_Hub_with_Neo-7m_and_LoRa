#pragma once
#include <stddef.h>
#include <stdint.h>
#include "sync_codec.h"
#include "config.h"

enum MoveClass : uint8_t {
  CLASS_IDLE    = 0,
  CLASS_WALKING = 1,
  CLASS_CYCLING = 2,
  CLASS_DRIVING     = 3,
};

const char* moveClassName(MoveClass c);

enum TrackerEvent : uint8_t {
  EV_NONE      = 0,
  EV_TRIPSTART = 1,
  EV_TRIPEND   = 2,
};

constexpr size_t TRIP_ID_LEN = 24;

struct TripStart {
  char    id[TRIP_ID_LEN];
  int32_t ts;
  double  lat;
  double  lon;
};

struct TripEnd {
  char      id[TRIP_ID_LEN];
  int32_t   sts;
  int32_t   ets;
  double    slat;
  double    slon;
  double    elat;
  double    elon;
  float     km;
  uint32_t  dur_s;
  MoveClass type;
  float     avg_kmh;
  float     max_kmh;
  // False when the trip didn't confirm the start trigger conditions
  // (no meaningful distance, no sustained speed). Caller should
  // trip_storage::deleteTrip(id) and SKIP persist + broadcast — it
  // was a false-start (GPS multipath burst that briefly fired
  // sustained/far then died).
  bool      confirmed;
};

class TripTracker {
public:
  void begin(const char* deviceId);

  // Feed one fix. Returns the event emitted; outStart/outEnd filled if applicable.
  TrackerEvent update(const sync_codec::Fix& fix,
                      TripStart* outStart, TripEnd* outEnd);

  bool        inTrip()  const { return m_state == STATE_MOVING; }
  const char* tripId()  const { return m_tripId; }

  // Pure helpers reused by the boot-time interrupted-trip finalizer.
  static float     distM(double lat1, double lon1,
                         double lat2, double lon2);
  static MoveClass classifyWithMax(float avg, float mx);

private:
  enum State : uint8_t { STATE_IDLE = 0, STATE_MOVING = 1 };

  static MoveClass classify(float avg);

  void beginTrip(const sync_codec::Fix& fix, TripStart* outStart);
  void endTrip(double endLat, double endLon, int32_t endTs, TripEnd* outEnd);

  char     m_deviceId[16] = "Sergii";
  State    m_state        = STATE_IDLE;
  char     m_tripId[TRIP_ID_LEN] = "";

  // IDLE
  double   m_anchorLat = 0;
  double   m_anchorLon = 0;
  bool     m_anchorSet = false;
  int32_t  m_fastSinceTs = 0;
  // GPS-settling gate: number of fixes seen since boot. Trip-start
  // triggers are suppressed until this passes MIN_FIXES_BEFORE_START
  // so the receiver's cold-lock wander doesn't produce ghost trips.
  uint32_t m_fixesSeen = 0;
  // Retroactive trip-start: ring buffer of the most recent IDLE fixes
  // that showed motion. When a trigger fires, these become the trip's
  // prefix so the recorded start coord matches actual departure.
  sync_codec::Fix m_motionBuf[trip_cfg::MOTION_BUFFER_MAX];
  size_t          m_motionBufN = 0;
public:
  // Accessors used by main.cpp on EV_TRIPSTART to write the prefix
  // fixes (the trigger fix is in the TripStart output as usual). Valid
  // ONLY between EV_TRIPSTART and the next update() — afterwards the
  // buffer is reused as the device returns to IDLE.
  size_t  precedingCount() const { return m_motionBufN; }
  const sync_codec::Fix& precedingFix(size_t i) const { return m_motionBuf[i]; }
private:

  // MOVING
  int32_t   m_startTs = 0;
  double    m_startLat = 0;
  double    m_startLon = 0;
  double    m_lastLat = 0;
  double    m_lastLon = 0;
  double    m_stopAnchorLat = 0;
  double    m_stopAnchorLon = 0;
  int32_t   m_stopSinceTs = 0;
  float     m_distanceKm = 0;
  float     m_maxSpdKmh = 0;
  // Previous fix's speed. Used so a single-fix doppler spike can't
  // become the recorded max — we require any new max to be sustained
  // across two consecutive fixes via min(prev, current).
  float     m_prevSpd = 0;
  float     m_sumSpd = 0;
  uint32_t  m_nFixes = 0;
  MoveClass m_peakClass = CLASS_WALKING;
};

extern TripTracker Tracker;
