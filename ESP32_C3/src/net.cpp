#include "net.h"
#include "config.h"
#include <Arduino.h>
#include <WiFi.h>
#include <DNSServer.h>

namespace {
bool      g_apUp        = false;
DNSServer g_dns;
bool      g_dnsUp       = false;
bool      g_sleeping    = false;
uint32_t  g_lastActMs   = 0;
constexpr uint32_t IDLE_SLEEP_MS = 10UL * 60UL * 1000UL;  // 10 min

void wakeNow(const char* reason) {
  g_lastActMs = millis();
  if (g_sleeping) {
    WiFi.setSleep(false);
    g_sleeping = false;
    Serial.printf("[AP] wake (%s)\n", reason);
  }
}

void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_AP_STACONNECTED: {
      const uint8_t* m = info.wifi_ap_staconnected.mac;
      Serial.printf("[AP] client+: %02X:%02X:%02X:%02X:%02X:%02X\n",
                    m[0],m[1],m[2],m[3],m[4],m[5]);
      wakeNow("client_connect");
      break;
    }
    case ARDUINO_EVENT_WIFI_AP_STADISCONNECTED: {
      const uint8_t* m = info.wifi_ap_stadisconnected.mac;
      Serial.printf("[AP] client-: %02X:%02X:%02X:%02X:%02X:%02X\n",
                    m[0],m[1],m[2],m[3],m[4],m[5]);
      // Don't immediately sleep - wait for the idle timer (so brief
      // disconnect/reconnect flapping doesn't toggle sleep state).
      break;
    }
    default: break;
  }
}
}

bool WifiAp::begin() {
  WiFi.onEvent(onWifiEvent);
  Serial.print("WiFi softAP... ");
  WiFi.mode(WIFI_AP);
  bool ok = WiFi.softAP(wifi::SSID, wifi::PASSWORD,
                        wifi::CHANNEL, /*hidden=*/0,
                        wifi::MAX_CLIENTS);
  if (!ok) {
    Serial.println("FAIL");
    g_apUp = false;
    return false;
  }
  // Critical for Android stability: keep WiFi modem awake.
  // Default power save (modem-sleep between beacons) causes
  // Android to disconnect after a few seconds.
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

  g_apUp = true;
  IPAddress ip = WiFi.softAPIP();
  Serial.printf("OK SSID='%s' IP=%s ch=%d max=%d sleep=off\n",
                wifi::SSID, ip.toString().c_str(),
                wifi::CHANNEL, wifi::MAX_CLIENTS);

  g_dns.setErrorReplyCode(DNSReplyCode::NoError);
  g_dnsUp = g_dns.start(53, "*", ip);
  Serial.printf("Captive DNS: %s (any host -> %s)\n",
                g_dnsUp ? "OK" : "FAIL", ip.toString().c_str());

  g_lastActMs = millis();
  return true;
}

void WifiAp::loop() {
  if (g_dnsUp) g_dns.processNextRequest();

  // Idle-sleep state machine: when no clients AND no HTTP for 10 min,
  // drop the modem into power-save. Any new client / HTTP request
  // wakes it via wakeNow().
  uint32_t now = millis();
  bool clientsPresent = (clientCount() > 0);
  if (!g_sleeping && !clientsPresent && (now - g_lastActMs > IDLE_SLEEP_MS)) {
    WiFi.setSleep(true);
    g_sleeping = true;
    Serial.println("[AP] sleep (idle 10min, 0 clients)");
  }
}

void WifiAp::noteActivity() {
  wakeNow("http");
}

bool WifiAp::isSleeping() {
  return g_sleeping;
}

uint8_t WifiAp::clientCount() {
  return g_apUp ? WiFi.softAPgetStationNum() : 0;
}

bool WifiAp::isUp() {
  return g_apUp;
}

WifiAp Net;
