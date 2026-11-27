#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <driver/gpio.h>
#include <esp_sleep.h>

#include "pins_config.h"
#include "secrets.h"
#include "ota_rescue.h"
#include "button_engine.h"

WebServer server(80);
Preferences prefs;
ButtonEngine buttons;
WiFiUDP keepaliveUdp;

static uint64_t gWakeMask = 0;
static uint32_t gLastActivityMs = 0;
static uint32_t gWifiDrops = 0;
static uint32_t gWifiLostSinceMs = 0;
static uint32_t gLastRetryMs = 0;
static uint32_t gLastKeepaliveMs = 0;
static bool gWasConnected = false;
static uint32_t gTriggersSent = 0;
static String gLastAction = "none";

static void selectCeramicAntenna(bool enable) {
  pinMode(PIN_RF_SW_PWR, OUTPUT);
  pinMode(PIN_RF_SW_CFG, OUTPUT);
  if (enable) {
    digitalWrite(PIN_RF_SW_PWR, LOW);
    digitalWrite(PIN_RF_SW_CFG, LOW);
  } else {
    digitalWrite(PIN_RF_SW_PWR, HIGH);
  }
}

static void blinkLed(int count, int onMs = 45, int offMs = 55) {
  for (int i = 0; i < count; ++i) {
    digitalWrite(PIN_LED, LOW);
    delay(onMs);
    digitalWrite(PIN_LED, HIGH);
    if (i + 1 < count) delay(offMs);
  }
}

static void onButtonAction(const char* action, uint8_t btnId, const char* gesture) {
  gLastActivityMs = millis();
  gLastAction = String(action) + ":" + String(gesture);
  Serial.printf("[BTN%u:%s] -> action=%s\n", btnId, gesture, action);

  // Wait briefly if Wi-Fi just woke from deep sleep
  uint32_t waitStart = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - waitStart) < WIFI_CONNECT_TIMEOUT_MS) {
    ArduinoOTA.handle();
    delay(15);
  }

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.setTimeout(2500);
    http.begin(OPTIPLEX_TRIGGER_URL);
    http.addHeader("Content-Type", "application/json");
    JsonDocument doc;
    doc["action"] = action;
    doc["source"] = "xiao_c6_strap";
    doc["button"] = btnId;
    doc["gesture"] = gesture;
    String payload;
    serializeJson(doc, payload);
    int code = http.POST(payload);
    http.end();
    if (code >= 200 && code < 300) {
      gTriggersSent++;
      blinkLed(strcmp(action, "record") == 0 ? 2 : 1, 35, 50);
    } else {
      Serial.printf("[HTTP] Trigger POST failed (%d)\n", code);
      blinkLed(3, 25, 30);
    }
  } else {
    Serial.println("[WIFI] Not connected when action fired.");
    blinkLed(3, 25, 30);
  }
}

static void maintainWifiHealth(uint32_t now) {
  if (WiFi.status() == WL_CONNECTED) {
    if (!gWasConnected) {
      gWasConnected = true;
      gWifiLostSinceMs = 0;
      Serial.printf("[WIFI] Connected %s (RSSI %d)\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      MDNS.end();
      if (MDNS.begin(MDNS_HOSTNAME)) MDNS.addService("http", "tcp", 80);
      ArduinoOTA.begin();
    }
    if (now - gLastKeepaliveMs >= 60000UL) {
      gLastKeepaliveMs = now;
      IPAddress gw = WiFi.gatewayIP();
      if (gw != IPAddress(0, 0, 0, 0)) {
        keepaliveUdp.beginPacket(gw, 9);
        keepaliveUdp.write(static_cast<uint8_t>(0));
        keepaliveUdp.endPacket();
      }
    }
    return;
  }

  if (gWasConnected) {
    gWasConnected = false;
    gWifiLostSinceMs = now;
    gLastRetryMs = now;
    gWifiDrops++;
    WiFi.disconnect(false);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    return;
  }
  if (gWifiLostSinceMs == 0) gWifiLostSinceMs = now;
  if (now - gLastRetryMs >= 15000UL) {
    gLastRetryMs = now;
    WiFi.disconnect(false);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  if (now - gWifiLostSinceMs >= 90000UL) {
    ESP.restart();
  }
}

static void enterDeepSleep() {
  Serial.println("[SLEEP] Workout idle timeout -> entering LP-GPIO deep sleep.");
  digitalWrite(PIN_LED, HIGH);
  selectCeramicAntenna(false);
  gpio_pullup_en(static_cast<gpio_num_t>(PIN_BTN1));
  gpio_pullup_en(static_cast<gpio_num_t>(PIN_BTN2));
  const uint64_t mask = (1ULL << PIN_BTN1) | (1ULL << PIN_BTN2);
  esp_deep_sleep_enable_gpio_wakeup(mask, ESP_GPIO_WAKEUP_GPIO_LOW);
  Serial.flush();
  esp_deep_sleep_start();
}

static void setupHttp() {
  server.on("/api/status", HTTP_GET, []() {
    JsonDocument doc;
    doc["device"] = DEVICE_NAME;
    doc["rev"] = FW_REVISION;
    doc["mode"] = buttons.mode == ButtonLayoutMode::ONE_BUTTON ? "one_button" : "two_button";
    doc["rssi"] = WiFi.RSSI();
    doc["wifi_drops"] = gWifiDrops;
    doc["triggers_sent"] = gTriggersSent;
    doc["last_action"] = gLastAction;
    doc["awake_sec"] = millis() / 1000UL;
    String out;
    serializeJson(doc, out);
    server.send(200, "application/json", out);
  });

  server.on("/api/config", HTTP_POST, []() {
    JsonDocument body;
    if (deserializeJson(body, server.arg("plain")) == DeserializationError::Ok) {
      if (body["mode"].is<const char*>()) {
        const char* m = body["mode"];
        buttons.mode = (strcmp(m, "one_button") == 0) ? ButtonLayoutMode::ONE_BUTTON : ButtonLayoutMode::TWO_BUTTON;
        prefs.putUChar("mode", static_cast<uint8_t>(buttons.mode));
      }
    }
    server.send(200, "application/json", "{\"ok\":true}");
  });
  server.begin();
}

void setup() {
  Serial.begin(115200);
  if (isDeepSleepWake()) {
    gWakeMask = esp_sleep_get_gpio_wakeup_status();
  }

  selectCeramicAntenna(true);
  initSafeOtaRescue(WIFI_SSID, WIFI_PASS, MDNS_HOSTNAME, 5000);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);

  prefs.begin("armory_btn", false);
  uint8_t savedMode = prefs.getUChar("mode", static_cast<uint8_t>(DEFAULT_LAYOUT_MODE));
  buttons.mode = savedMode == 1 ? ButtonLayoutMode::ONE_BUTTON : ButtonLayoutMode::TWO_BUTTON;
  buttons.begin(onButtonAction);

  setupHttp();
  gLastActivityMs = millis();

  if (gWakeMask != 0) {
    buttons.dispatchWakePress(gWakeMask);
  }
}

void loop() {
  uint32_t now = millis();
  ArduinoOTA.handle();
  server.handleClient();
  maintainWifiHealth(now);
  buttons.update(now);

  if ((now - gLastActivityMs) >= WORKOUT_AWAKE_MS) {
    enterDeepSleep();
  }
  delay(5);
}
