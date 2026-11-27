#pragma once
#include <Arduino.h>

// =============================================================================
// ArmoryClicker Chest Strap Wi-Fi Button Pod (Rev v2.3)
// MCU: Seeed Studio XIAO ESP32-C6 (ceramic antenna, LP-GPIO deep-sleep wake)
// =============================================================================

// Host Identity
constexpr const char* DEVICE_NAME   = "ArmoryClicker";
constexpr const char* MDNS_HOSTNAME = "armoryclicker";
constexpr const char* FW_REVISION   = "Rev v2.3";

// OptiPlex Hub Endpoint
constexpr const char* OPTIPLEX_TRIGGER_URL = "http://192.168.1.152:8766/api/bt-button/trigger";

// Seeed XIAO ESP32-C6 RF Antenna Switch (LOW/LOW = internal ceramic antenna active)
constexpr int PIN_RF_SW_PWR = 3;
constexpr int PIN_RF_SW_CFG = 14;

// Active-LOW Tactile Switches wired to LP-capable GPIOs (0..7 wake ESP32-C6 from deep sleep!)
constexpr int PIN_BTN1 = 1;    // D1 (GPIO1) -> SW1 Single Button (1x Check / 2x Record / 3x Undo / Hold Voice)
constexpr int PIN_BTN2 = 2;    // D2 (GPIO2) -> Optional spare button input
constexpr int PIN_LED  = 15;   // Built-in yellow user LED (active LOW; strapping pin -> init post-rescue)

// Gesture & Power Management Timings
constexpr uint32_t DEBOUNCE_MS          = 15;   // Crisp tactile debounce; zero minimum-hold rejection
constexpr uint32_t MULTI_TAP_WINDOW_MS  = 280;  // Max gap between rapid taps for 2x/3x detection
constexpr uint32_t LONG_PRESS_MS        = 500;
constexpr uint32_t WORKOUT_AWAKE_MS     = 45UL * 60UL * 1000UL;  // Stay awake 45 min after last press, then deep sleep
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 8000;

// Layout modes matching server/armory_cam/bt_button.py
enum class ButtonLayoutMode : uint8_t {
  TWO_BUTTON = 0,  // SW1: single=check (0ms!), long=undo | SW2: 1x=voice, 2x=record, 3x=undo, long=record
  ONE_BUTTON = 1   // SW1 & SW2 identical: 1x=check, 2x=record, 3x=undo, long=voice
};

constexpr ButtonLayoutMode DEFAULT_LAYOUT_MODE = ButtonLayoutMode::ONE_BUTTON;
