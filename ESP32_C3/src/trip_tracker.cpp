#include "trip_tracker.h"
#include "config.h"
#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

const char* moveClassName(MoveClass c) {
  switch (c) {
    case CLASS_WALKING: return "walking";
    case CLASS_CYCLING: return "cycling";
    case CLASS_DRIVING: return "driving";
    default:            return "idle";
  }
}

static int classRank(MoveClass c) {
  switch (c) {
    case CLASS_DRIVING:    return 2;
    case CLASS_CYCLING: return 1;
    default:            return 0;
  }
}

void TripTracker::begin(const char* deviceId) {
  if (deviceId) {
    strncpy(m_deviceId, deviceId, sizeof(m_deviceId) - 1);
    m_deviceId[sizeof(m_deviceId) - 1] = '\0';
  }
  m_state       = STATE_IDLE;
  m_anchorSet   = false;
  m_fastSinceTs = 0;
  m_fixesSeen   = 0;
  m_motionBufN  = 0;
  m_tripId[0]   = '\0';
}

float TripTracker::distM(double lat1, double lon1,
                         double lat2, double lon2) {
  double avgLat = (lat1 + lat2) * 0.5 * 0.0174532925;
  double cosLat = cos(avgLat);
  double dlat = (lat2 - lat1) * 111320.0;
  double dlon = (lon2 - lon1) * 111320.0 * cosLat;
  return (float)sqrt(dlat * dlat + dlon * dlon);
}

MoveClass TripTracker::classify(float avg) {
  if (avg < trip_cfg::CLASS_WALK_MAX) return CLASS_WALKING;
  if (avg < trip_cfg::CLASS_BIKE_MAX) return CLASS_CYCLING;
  return CLASS_DRIVING;
}

MoveClass TripTracker::classifyWithMax(float avg, float mx) {
  // PicoB heuristic: a sustained-fast max signals "driving" even if the
  // average is dragged down by traffic stops. See PicoB trip_tracker.py.
  if (mx >= 40.0f) return CLASS_DRIVING;
  if (mx >= 32.0f && avg >= 24.0f) return CLASS_DRIVING;
  return classify(avg);
}

TrackerEvent TripTracker::update(const sync_codec::Fix& fix,
                                 TripStart* outStart, TripEnd* outEnd) {
  // Count every fix the tracker sees. We only care that this passes
  // the small gate constant; uint32_t wrap is ~136 years at 1 Hz.
  m_fixesSeen++;

  if (m_state == STATE_IDLE) {
    if (!m_anchorSet) {
      m_anchorLat = fix.lat;
      m_anchorLon = fix.lon;
      m_anchorSet = true;
    }
    float moved = distM(m_anchorLat, m_anchorLon, fix.lat, fix.lon);

    if (fix.spd >= trip_cfg::MOVE_SPEED_KMH) {
      if (m_fastSinceTs == 0) m_fastSinceTs = fix.ts;
    } else {
      m_fastSinceTs = 0;
    }

    // Motion buffer: hold recent IDLE fixes that showed non-trivial
    // doppler speed. When a trip-start trigger fires these become the
    // trip's prefix so start_lat/lon/ts reflect actual departure.
    if (fix.spd >= trip_cfg::TRIP_LOW_SPEED_KMH) {
      if (m_motionBufN < trip_cfg::MOTION_BUFFER_MAX) {
        m_motionBuf[m_motionBufN++] = fix;
      } else {
        // Ring: shift left, append at end.
        for (size_t i = 1; i < trip_cfg::MOTION_BUFFER_MAX; i++) {
          m_motionBuf[i - 1] = m_motionBuf[i];
        }
        m_motionBuf[trip_cfg::MOTION_BUFFER_MAX - 1] = fix;
      }
    } else if (m_motionBufN > 0) {
      // Stopped — whatever motion was building didn't pan out.
      m_motionBufN = 0;
    }

    bool sustained = (m_fastSinceTs != 0 &&
                      (fix.ts - m_fastSinceTs) >= (int32_t)trip_cfg::MOVE_HOLD_S);
    // `far` must coincide with non-trivial GPS-doppler speed. A
    // stationary device with multipath drift sees moved >> 100m on a
    // single bad fix while spd stays near zero — that signature alone
    // produced ghost trips overnight. Real motion covering 100m always
    // has continuous speed > ~2 km/h.
    bool far       = (moved >= trip_cfg::START_DIST_M && fix.spd >= 2.0f);

    // GPS-settling gate: refuse to start a trip until the receiver has
    // delivered MIN_FIXES_BEFORE_START accepted fixes. Cold-lock wander
    // produces both phantom distance (`far`) and phantom speed
    // (`sustained`); waiting for the solution to stabilise is the only
    // robust defense.
    bool settled = (m_fixesSeen >= trip_cfg::MIN_FIXES_BEFORE_START);

    if (settled && (sustained || far)) {
      beginTrip(fix, outStart);
      return EV_TRIPSTART;
    }

    // Drift anchor toward current position to absorb slow GPS noise
    if (m_fastSinceTs == 0) {
      m_anchorLat = m_anchorLat * 0.9 + fix.lat * 0.1;
      m_anchorLon = m_anchorLon * 0.9 + fix.lon * 0.1;
    }
    return EV_NONE;
  }

  // MOVING
  float segM = distM(m_lastLat, m_lastLon, fix.lat, fix.lon);
  m_distanceKm += segM / 1000.0f;
  m_lastLat = fix.lat;
  m_lastLon = fix.lon;

  // Sustained-max: a single GPS doppler spike (cold-start glitch,
  // multipath, brief lock loss) shouldn't become the trip's recorded
  // max. Require any candidate max to be sustained across the current
  // and immediately previous fix.
  float sustained = (fix.spd < m_prevSpd) ? fix.spd : m_prevSpd;
  if (sustained > m_maxSpdKmh) m_maxSpdKmh = sustained;
  m_prevSpd = fix.spd;
  m_sumSpd += fix.spd;
  m_nFixes++;

  // Running peak class (used to pick stop-detect threshold). Use the
  // SAME sustained value so a single doppler spike can't escalate the
  // class — symmetric with the m_maxSpdKmh hardening above.
  MoveClass curClass = classify(sustained);
  if (classRank(curClass) > classRank(m_peakClass)) m_peakClass = curClass;

  // Stop detection within END_DIST_M for STOP_S_<class> seconds
  float fromStop = distM(m_stopAnchorLat, m_stopAnchorLon, fix.lat, fix.lon);
  if (fromStop <= trip_cfg::END_DIST_M) {
    if (m_stopSinceTs == 0) {
      m_stopSinceTs = fix.ts;
    } else {
      uint32_t holdS;
      switch (m_peakClass) {
        case CLASS_DRIVING: holdS = trip_cfg::STOP_S_DRIVING; break;
        case CLASS_CYCLING: holdS = trip_cfg::STOP_S_CYCLING; break;
        default:            holdS = trip_cfg::STOP_S_WALKING; break;
      }
      // Guard the cast: if GPS time goes backwards (rare receiver
      // glitch on lock loss) the signed subtraction would wrap to ~4e9
      // and the trip ends instantly.
      if (fix.ts > m_stopSinceTs &&
          (uint32_t)(fix.ts - m_stopSinceTs) >= holdS) {
        // end_ts = when the stop began, not now (which is hold_s later).
        endTrip(fix.lat, fix.lon, m_stopSinceTs, outEnd);
        return EV_TRIPEND;
      }
    }
  } else {
    m_stopAnchorLat = fix.lat;
    m_stopAnchorLon = fix.lon;
    m_stopSinceTs   = 0;
  }
  return EV_NONE;
}

void TripTracker::beginTrip(const sync_codec::Fix& fix, TripStart* out) {
  // If we have buffered IDLE fixes that showed motion leading up to
  // this trigger, the first one is the actual departure point. The
  // recorded start (trip_id, start_ts/lat/lon) uses that fix; main.cpp
  // is responsible for appending the buffered prefix to trip storage
  // by reading precedingCount() / precedingFix(i) right after the
  // EV_TRIPSTART event fires.
  const sync_codec::Fix& startFix =
      (m_motionBufN > 0) ? m_motionBuf[0] : fix;

  m_state = STATE_MOVING;
  snprintf(m_tripId, sizeof(m_tripId), "T%ld", (long)startFix.ts);

  m_startTs    = startFix.ts;
  m_startLat   = startFix.lat;
  m_startLon   = startFix.lon;
  m_lastLat    = fix.lat;     // current pos for distance/stop bookkeeping
  m_lastLon    = fix.lon;
  m_stopAnchorLat = fix.lat;
  m_stopAnchorLon = fix.lon;
  m_stopSinceTs   = 0;
  m_distanceKm    = 0;
  // Start at 0 — the first fix has no "previous" to validate against,
  // so the sustained-max filter will require fix N=2 to also exceed
  // before any value is recorded.
  m_maxSpdKmh     = 0;
  m_prevSpd       = fix.spd > 0 ? fix.spd : 0;
  m_sumSpd        = fix.spd;
  m_nFixes        = 1;
  m_peakClass     = classify(fix.spd);

  if (out) {
    strncpy(out->id, m_tripId, sizeof(out->id) - 1);
    out->id[sizeof(out->id) - 1] = '\0';
    out->ts  = m_startTs;
    out->lat = m_startLat;
    out->lon = m_startLon;
  }

  // Walk the buffered prefix to roll distance + sustained-max forward
  // before the caller's main loop appends the trigger fix. The buffer
  // itself stays valid for main.cpp to write to storage (read until
  // the next update() call); we just integrate stats here.
  if (m_motionBufN > 0) {
    m_lastLat = m_motionBuf[0].lat;
    m_lastLon = m_motionBuf[0].lon;
    m_prevSpd = m_motionBuf[0].spd > 0 ? m_motionBuf[0].spd : 0;
    for (size_t i = 1; i < m_motionBufN; i++) {
      float seg = distM(m_lastLat, m_lastLon,
                        m_motionBuf[i].lat, m_motionBuf[i].lon);
      m_distanceKm += seg / 1000.0f;
      m_lastLat = m_motionBuf[i].lat;
      m_lastLon = m_motionBuf[i].lon;
      float sustained = (m_motionBuf[i].spd < m_prevSpd)
                          ? m_motionBuf[i].spd : m_prevSpd;
      if (sustained > m_maxSpdKmh) m_maxSpdKmh = sustained;
      m_prevSpd = m_motionBuf[i].spd;
      m_sumSpd += m_motionBuf[i].spd;
      m_nFixes++;
    }
    // Continue from the last buffered fix to the trigger fix.
    float seg = distM(m_lastLat, m_lastLon, fix.lat, fix.lon);
    m_distanceKm += seg / 1000.0f;
    m_lastLat = fix.lat;
    m_lastLon = fix.lon;
    float sustained = (fix.spd < m_prevSpd) ? fix.spd : m_prevSpd;
    if (sustained > m_maxSpdKmh) m_maxSpdKmh = sustained;
    m_prevSpd = fix.spd;
  }
}

void TripTracker::endTrip(double endLat, double endLon, int32_t endTs,
                          TripEnd* out) {
  uint32_t durS = (endTs > m_startTs)
                  ? (uint32_t)(endTs - m_startTs) : 1u;
  float avgKmh = m_nFixes > 0 ? (m_sumSpd / (float)m_nFixes) : 0.0f;
  MoveClass type = classifyWithMax(avgKmh, m_maxSpdKmh);

  // Confirm the start trigger was real motion. False-starts (GPS
  // multipath burst that briefly fired sustained/far then died) end
  // with no distance covered and no sustained speed. Caller deletes
  // the trip files and skips persist + broadcast.
  bool confirmed = (m_distanceKm * 1000.0f) >= trip_cfg::MIN_REAL_TRIP_M
                || m_maxSpdKmh               >= trip_cfg::MIN_REAL_TRIP_MAX_KMH;

  if (out) {
    strncpy(out->id, m_tripId, sizeof(out->id) - 1);
    out->id[sizeof(out->id) - 1] = '\0';
    out->sts     = m_startTs;
    out->ets     = endTs;
    out->slat    = m_startLat;
    out->slon    = m_startLon;
    out->elat    = endLat;
    out->elon    = endLon;
    out->km      = m_distanceKm;
    out->dur_s   = durS;
    out->type    = type;
    out->avg_kmh = avgKmh;
    out->max_kmh = m_maxSpdKmh;
    out->confirmed = confirmed;
  }

  // Reset to IDLE; anchor on end position so we don't immediately re-start.
  m_state         = STATE_IDLE;
  m_anchorLat     = endLat;
  m_anchorLon     = endLon;
  m_anchorSet     = true;
  m_fastSinceTs   = 0;
  m_motionBufN    = 0;        // clear stale prefix from the just-ended trip
  m_tripId[0]     = '\0';
}

TripTracker Tracker;
