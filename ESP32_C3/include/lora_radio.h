#pragma once
#include <stddef.h>
#include <stdint.h>

struct LoraRxResult {
  size_t   len;       // plaintext length, 0 if no packet or failure
  int16_t  rssi;      // dBm
  float    snr;       // dB
};

class LoraRadio {
public:
  bool begin();
  bool sendEncrypted(const char* plaintext);
  // Non-blocking: if a packet was received and decrypted, fills out->text/len
  // (text NUL-terminated, max bufCap bytes), returns true with rssi/snr.
  // Returns false if no packet, or packet failed CRC/decrypt.
  bool pollReceive(char* outText, size_t bufCap, LoraRxResult* outMeta);
};

extern LoraRadio Radio;
