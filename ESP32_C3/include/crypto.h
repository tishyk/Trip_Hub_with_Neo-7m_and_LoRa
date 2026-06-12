#pragma once
#include <stddef.h>
#include <stdint.h>

namespace crypto {

size_t encrypt(const uint8_t* plain, size_t plainLen,
               uint8_t* outCipher, size_t outCap);

int decrypt(const uint8_t* cipher, size_t cipherLen,
            uint8_t* outPlain, size_t outCap);

}
