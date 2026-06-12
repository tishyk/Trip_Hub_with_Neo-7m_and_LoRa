#pragma once
#include <stdint.h>

class WifiAp {
public:
  bool begin();
  void loop();             // pump DNS server + idle-sleep timer
  void noteActivity();     // call on each user HTTP request
  uint8_t clientCount();
  bool    isUp();
  bool    isSleeping();    // current modem-sleep state (true => low power)
};

extern WifiAp Net;
