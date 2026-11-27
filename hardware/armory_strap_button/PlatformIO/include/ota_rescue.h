#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <esp_sleep.h>
#include <esp_system.h>

inline bool isDeepSleepWake() {
  return esp_reset_reason() == ESP_RST_DEEPSLEEP;
}

inline void initSafeOtaRescue(const char* ssid, const char* password, const char* hostname, uint32_t windowMs = 5000) {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);  // Wi-Fi only (no BLE) -> zero wake jitter
  if (ssid && ssid[0] != '\0' && strcmp(ssid, "YOUR_WIFI_SSID") != 0) {
    WiFi.begin(ssid, password);
  }
  ArduinoOTA.setHostname(hostname);
  ArduinoOTA.begin();

  esp_reset_reason_t reason = esp_reset_reason();
  Serial.printf("[BOOT] Reset Reason: %d\n", static_cast<int>(reason));

  if (reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT ||
      reason == ESP_RST_TASK_WDT || reason == ESP_RST_WDT) {
    Serial.println("[CRASH GUARD] Panic/WDT detected! Holding in SAFE RESCUE MODE.");
    while (true) {
      ArduinoOTA.handle();
      delay(10);
    }
  }

  // On normal power-on / SW reset, run full 5s pre-peripheral OTA rescue window.
  // On LP-GPIO deep-sleep button wake, proceed immediately so Set 1 click fires ASAP while OTA stays live!
  if (reason != ESP_RST_DEEPSLEEP) {
    Serial.printf("[BOOT] %lu ms pre-peripheral OTA rescue window active...\n", static_cast<unsigned long>(windowMs));
    uint32_t startMs = millis();
    while (millis() - startMs < windowMs) {
      ArduinoOTA.handle();
      delay(10);
    }
  } else {
    Serial.println("[BOOT] Deep-sleep wake -> fast path to dispatch wake click (OTA remains active).");
  }
}
