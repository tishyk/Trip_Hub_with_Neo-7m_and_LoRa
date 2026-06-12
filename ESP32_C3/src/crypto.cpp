#include "crypto.h"
#include "config.h"
#include <string.h>
#include "mbedtls/aes.h"

// Key + block size live in include/config.h (namespace crypto_cfg) so
// they sit alongside the rest of the device's tunables. Same key as
// PicoB's config.py LORA_KEY.
using crypto_cfg::AES_KEY;
using crypto_cfg::BLOCK;

namespace crypto {

size_t encrypt(const uint8_t* plain, size_t plainLen,
               uint8_t* outCipher, size_t outCap) {
  size_t pad   = BLOCK - (plainLen % BLOCK);
  size_t total = plainLen + pad;
  if (total > outCap) return 0;

  uint8_t buf[256];
  if (total > sizeof(buf)) return 0;
  memcpy(buf, plain, plainLen);
  memset(buf + plainLen, (uint8_t)pad, pad);

  mbedtls_aes_context ctx;
  mbedtls_aes_init(&ctx);
  mbedtls_aes_setkey_enc(&ctx, AES_KEY, 128);
  for (size_t i = 0; i < total; i += BLOCK) {
    mbedtls_aes_crypt_ecb(&ctx, MBEDTLS_AES_ENCRYPT, buf + i, outCipher + i);
  }
  mbedtls_aes_free(&ctx);
  return total;
}

int decrypt(const uint8_t* cipher, size_t cipherLen,
            uint8_t* outPlain, size_t outCap) {
  if (cipherLen == 0 || cipherLen % BLOCK != 0) return -1;
  if (cipherLen > outCap) return -1;

  mbedtls_aes_context ctx;
  mbedtls_aes_init(&ctx);
  mbedtls_aes_setkey_dec(&ctx, AES_KEY, 128);
  for (size_t i = 0; i < cipherLen; i += BLOCK) {
    mbedtls_aes_crypt_ecb(&ctx, MBEDTLS_AES_DECRYPT, cipher + i, outPlain + i);
  }
  mbedtls_aes_free(&ctx);

  uint8_t pad = outPlain[cipherLen - 1];
  if (pad < 1 || pad > BLOCK) return -1;
  for (size_t i = 0; i < pad; i++) {
    if (outPlain[cipherLen - 1 - i] != pad) return -1;
  }
  return (int)(cipherLen - pad);
}

}
