#include "lora_radio.h"
#include "config.h"
#include "crypto.h"
#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include <string.h>

namespace {
SX1278 sx = new Module(pins::LORA_NSS, pins::LORA_DIO0, pins::LORA_RST);
volatile bool g_rxFlag = false;

void IRAM_ATTR onRxIsr() {
  g_rxFlag = true;
}
}

bool LoraRadio::begin() {
  SPI.begin(pins::LORA_SCK, pins::LORA_MISO, pins::LORA_MOSI, pins::LORA_NSS);
  Serial.print("LoRa begin... ");
  int st = sx.begin(rf::FREQ_MHZ, rf::BW_KHZ, rf::SF, rf::CR,
                    rf::SYNC, rf::POWER, rf::PREAMBLE);
  if (st != RADIOLIB_ERR_NONE) {
    Serial.printf("FAIL code=%d\n", st);
    return false;
  }
  sx.setCRC(true);
  sx.setPacketReceivedAction(onRxIsr);
  int rxSt = sx.startReceive();
  if (rxSt != RADIOLIB_ERR_NONE) {
    Serial.printf("startReceive FAIL code=%d\n", rxSt);
    return false;
  }
  Serial.printf("OK (freq=%.1fMHz BW=%.0fkHz SF%d CR4/%d sync=0x%02X power=%ddBm) RX armed\n",
                rf::FREQ_MHZ, rf::BW_KHZ, rf::SF, rf::CR, rf::SYNC, rf::POWER);
  return true;
}

bool LoraRadio::sendEncrypted(const char* plaintext) {
  uint8_t cipher[256];
  size_t plainLen = strlen(plaintext);
  size_t cLen = crypto::encrypt((const uint8_t*)plaintext, plainLen,
                                cipher, sizeof(cipher));
  if (cLen == 0) return false;
  int rc = sx.transmit(cipher, cLen);
  // Clear any spurious IRQ raised during TX. SX1278's DIO0 is mapped
  // to TxDone while transmitting and to RxDone afterwards; the same
  // ISR (onRxIsr) latches g_rxFlag for both. Without this clear, the
  // next pollReceive() believes a packet arrived and reads stale FIFO
  // bytes — a mix of the just-sent ciphertext and whatever was there
  // from the previous RX. With the right (un)luck, PKCS7 validates
  // and a frankenstein "received" message lands in the chat log.
  g_rxFlag = false;
  // re-arm RX after TX (transmit() leaves the chip in standby). If
  // this fails the radio is silently deaf until next boot — log so
  // we can detect the situation in the heartbeat.
  int rx_rc = sx.startReceive();
  if (rx_rc != RADIOLIB_ERR_NONE) {
    Serial.printf("[RADIO] startReceive after TX failed: %d\n", rx_rc);
  }
  return rc == RADIOLIB_ERR_NONE;
}

bool LoraRadio::pollReceive(char* outText, size_t bufCap,
                            LoraRxResult* outMeta) {
  if (!g_rxFlag) return false;
  g_rxFlag = false;

  uint8_t cipher[256];
  size_t  cLen = sx.getPacketLength();
  if (cLen == 0 || cLen > sizeof(cipher)) {
    sx.startReceive();
    return false;
  }
  int rc = sx.readData(cipher, cLen);
  // re-arm RX immediately (independent of decrypt success)
  sx.startReceive();
  if (rc != RADIOLIB_ERR_NONE) {
    return false;
  }

  uint8_t plain[256];
  int pLen = crypto::decrypt(cipher, cLen, plain, sizeof(plain));
  if (pLen <= 0) {
    return false;
  }
  size_t copy = ((size_t)pLen < bufCap - 1) ? (size_t)pLen : (bufCap - 1);
  memcpy(outText, plain, copy);
  outText[copy] = '\0';

  if (outMeta) {
    outMeta->len  = copy;
    outMeta->rssi = (int16_t)sx.getRSSI();
    outMeta->snr  = sx.getSNR();
  }
  return true;
}

LoraRadio Radio;
