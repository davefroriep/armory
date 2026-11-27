#pragma once
#include <Arduino.h>

// =============================================================================
// Armory Chest Strap Wi-Fi Button Pod (Rev v1.0)
// MCU: Seeed Studio XIAO ESP32-C6 (ceramic antenna, LP-GPIO deep-sleep wake)
// =============================================================================

// Host Identity
constexpr const char* DEVICE_NAME   = "ArmoryStrapBtn";
constexpr const char* MDNS_HOSTNAME = "armory-btn";
constexpr const char* FW_REVISION   = "Rev v1.0";

// OptiPlex Hub Endpoint
constexpr const char* OPTIPLEX_TRIGGER_URL = "http://192.168.1.152:8766/api/bt-button/trigger";

// Seeed XIAO ESP32-C6 RF Antenna Switch (LOW/LOW = internal ceramic antenna active)
constexpr int PIN_RF_SW_PWR = 3;
constexpr int PIN_RF_SW_CFG = 14;

// Active-LOW Tactile Switches wired to LP-capable GPIOs (0..7 wake ESP32-C6 from deep sleep!)
constexpr int PIN_BTN1 = 1;    // D1 (GPIO1) -> Top Paddle (SW1: Done / Undo)
constexpr int PIN_BTN2 = 2;    // D2 (GPIO2) -> Bottom Paddle (SW2: Voice / Record)
constexpr int PIN_LED  = 15;   // Built-in yellow user LED (active LOW; strapping pin -> init post-rescue)

// Gesture & Power Management Timings
constexpr uint32_t DEBOUNCE_MS      = 30;
constexpr uint32_t DOUBLE_TAP_MS    = 260;
constexpr uint32_t LONG_PRESS_MS    = 500;
constexpr uint32_t WORKOUT_AWAKE_MS = 45UL * 60UL * 1000UL;  // Stay awake 45 min after last press, then deep sleep
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 8000;

// Layout modes matching server/armory_cam/bt_button.py
enum class ButtonLayoutMode : uint8_t {
  TWO_BUTTON = 0,  // SW1: single=check (0ms!), long=undo | SW2: single=voice, long/double=record
  ONE_BUTTON = 1   // SW1 & SW2 identical: single=check, double=record, long=voice
};

constexpr ButtonLayoutMode DEFAULT_LAYOUT_MODE = ButtonLayoutMode::TWO_BUTTON;
