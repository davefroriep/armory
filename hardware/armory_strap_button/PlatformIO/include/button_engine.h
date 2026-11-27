#pragma once
#include <Arduino.h>
#include "pins_config.h"

using ActionCallback = void (*)(const char* action, uint8_t btnId, const char* gesture);

struct BtnChannel {
  uint8_t id = 1;
  int pin = PIN_BTN1;
  bool rawLast = true;
  bool stablePressed = false;
  uint32_t lastBounceMs = 0;
  uint32_t pressStartMs = 0;
  bool longFired = false;
  uint8_t tapCount = 0;
  uint32_t lastReleaseMs = 0;
};

class ButtonEngine {
 public:
  ButtonLayoutMode mode = DEFAULT_LAYOUT_MODE;

  void begin(ActionCallback cb) {
    cb_ = cb;
    b1_.id = 1;
    b1_.pin = PIN_BTN1;
    b2_.id = 2;
    b2_.pin = PIN_BTN2;
    pinMode(b1_.pin, INPUT_PULLUP);
    pinMode(b2_.pin, INPUT_PULLUP);
    b1_.rawLast = digitalRead(b1_.pin) != LOW;
    b2_.rawLast = digitalRead(b2_.pin) != LOW;
  }

  // Called right after deep-sleep wake so the first press that woke the ESP32 is never lost.
  void dispatchWakePress(uint64_t wakeMask) {
    if (!cb_) return;
    const bool w1 = (wakeMask & (1ULL << PIN_BTN1)) != 0;
    const bool w2 = (wakeMask & (1ULL << PIN_BTN2)) != 0;
    if (!w1 && !w2) return;

    // Wait briefly up to LONG_PRESS_MS if button is still held down right at wake!
    const int wakePin = w2 && !w1 ? PIN_BTN2 : PIN_BTN1;
    const uint8_t bid = (wakePin == PIN_BTN2) ? 2 : 1;
    uint32_t t0 = millis();
    bool isLong = false;
    while (digitalRead(wakePin) == LOW) {
      if (millis() - t0 >= LONG_PRESS_MS) {
        isLong = true;
        break;
      }
      delay(5);
    }
    if (mode == ButtonLayoutMode::ONE_BUTTON) {
      cb_(isLong ? "voice" : "check", bid, isLong ? "wake_long" : "wake_single");
    } else if (bid == 1) {
      cb_(isLong ? "undo" : "check", 1, isLong ? "wake_long" : "wake_single");
    } else {
      cb_(isLong ? "record" : "voice", 2, isLong ? "wake_long" : "wake_single");
    }
  }

  void update(uint32_t now) {
    stepChannel(b1_, now);
    stepChannel(b2_, now);
  }

 private:
  ActionCallback cb_ = nullptr;
  BtnChannel b1_{};
  BtnChannel b2_{};

  bool needsDoubleWait(const BtnChannel& ch) const {
    if (mode == ButtonLayoutMode::ONE_BUTTON) return true;
    return ch.id == 2;  // In TWO_BUTTON mode, SW1 (check) fires with 0 ms delay on release!
  }

  void emitTap(uint8_t id, uint8_t count) {
    if (!cb_) return;
    if (mode == ButtonLayoutMode::ONE_BUTTON) {
      if (count >= 3) {
        cb_("undo", id, "triple");
      } else if (count == 2) {
        cb_("record", id, "double");
      } else {
        cb_("check", id, "single");
      }
      return;
    }
    if (id == 1) {
      cb_("check", 1, "single");
    } else if (count >= 3) {
      cb_("undo", 2, "triple");
    } else {
      cb_(count == 2 ? "record" : "voice", 2, count == 2 ? "double" : "single");
    }
  }

  void emitLong(uint8_t id) {
    if (!cb_) return;
    if (mode == ButtonLayoutMode::ONE_BUTTON) {
      cb_("voice", id, "long");
      return;
    }
    cb_(id == 1 ? "undo" : "record", id, "long");
  }

  void stepChannel(BtnChannel& ch, uint32_t now) {
    const bool isPressed = (digitalRead(ch.pin) == LOW);
    if (isPressed != ch.rawLast) {
      ch.rawLast = isPressed;
      ch.lastBounceMs = now;
    }

    if ((now - ch.lastBounceMs) >= DEBOUNCE_MS && isPressed != ch.stablePressed) {
      ch.stablePressed = isPressed;
      if (isPressed) {
        ch.pressStartMs = now;
        ch.longFired = false;
      } else if (!ch.longFired) {
        if (!needsDoubleWait(ch)) {
          ch.tapCount = 0;
          emitTap(ch.id, 1);
        } else {
          ch.tapCount++;
          ch.lastReleaseMs = now;
          if (ch.tapCount >= 3) {
            ch.tapCount = 0;
            emitTap(ch.id, 3);
          }
        }
      }
    }

    if (ch.stablePressed && !ch.longFired && (now - ch.pressStartMs) >= LONG_PRESS_MS) {
      ch.longFired = true;
      ch.tapCount = 0;
      emitLong(ch.id);
    }

    if (!ch.stablePressed && ch.tapCount > 0 && (now - ch.lastReleaseMs) >= MULTI_TAP_WINDOW_MS) {
      const uint8_t count = ch.tapCount;
      ch.tapCount = 0;
      emitTap(ch.id, count);
    }
  }
};
