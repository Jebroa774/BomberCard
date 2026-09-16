#pragma once

#include <Arduino.h>

#ifndef POCKETLAB_ALLOW_SUBGHZ_TX
#define POCKETLAB_ALLOW_SUBGHZ_TX 0
#endif

#ifndef POCKETLAB_ALLOW_IR_TX
#define POCKETLAB_ALLOW_IR_TX 0
#endif

#ifndef POCKETLAB_ALLOW_GPIO_OUTPUT
#define POCKETLAB_ALLOW_GPIO_OUTPUT 0
#endif

namespace pocketlab::config {

inline constexpr char FIRMWARE_NAME[] = "BomberCard";
inline constexpr char FIRMWARE_VERSION[] = "0.4.0-dev";
inline constexpr char HOSTNAME[] = "bombercard";

inline constexpr uint16_t HTTP_PORT = 80;
inline constexpr uint16_t WEBSOCKET_PORT = 81;
inline constexpr uint8_t WIFI_CHANNEL = 6;
inline constexpr uint8_t WIFI_MAX_CLIENTS = 4;

inline constexpr uint32_t I2C_FREQUENCY_HZ = 400000;
inline constexpr uint8_t TCA9535_STATUS_ADDRESS = 0x20;
inline constexpr uint8_t TCA9534_CONTROL_ADDRESS = 0x21;
inline constexpr uint8_t PN532_I2C_ADDRESS = 0x24;
inline constexpr uint8_t ATECC608C_I2C_ADDRESS = 0x60;
inline constexpr uint8_t OLED_I2C_ADDRESS = 0x3C;
inline constexpr uint8_t OLED_WIDTH = 72;
inline constexpr uint8_t OLED_HEIGHT = 40;

inline constexpr uint32_t SPI_FREQUENCY_HZ = 8000000;
inline constexpr uint32_t SD_FREQUENCY_HZ = 20000000;
inline constexpr uint32_t LF_SERIAL_HALF_PERIOD_US = 2;
inline constexpr uint32_t LF_POWER_SETTLE_MS = 15;
inline constexpr uint32_t PAIRING_WINDOW_MS = 60000;
inline constexpr float BOARD_TEMP_NOMINAL_OHMS = 10000.0F;
inline constexpr float BOARD_TEMP_BETA = 3950.0F;
inline constexpr float BOARD_TEMP_DIVIDER_OHMS = 10000.0F;
inline constexpr float BOARD_TEMP_DIVIDER_MV = 3300.0F;
inline constexpr float BOARD_TEMP_SHUTDOWN_C = 80.0F;
inline constexpr float BOARD_TEMP_RELEASE_C = 70.0F;
inline constexpr size_t MAX_FILE_PATH_LENGTH = 96;
inline constexpr size_t MAX_DIRECTORY_ENTRIES = 100;

inline constexpr bool SUBGHZ_TX_COMPILED = POCKETLAB_ALLOW_SUBGHZ_TX != 0;
inline constexpr bool IR_TX_COMPILED = POCKETLAB_ALLOW_IR_TX != 0;
inline constexpr bool GPIO_OUTPUT_COMPILED = POCKETLAB_ALLOW_GPIO_OUTPUT != 0;

}  // namespace pocketlab::config
