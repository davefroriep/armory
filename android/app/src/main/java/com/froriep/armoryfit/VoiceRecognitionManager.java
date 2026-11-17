package com.froriep.armoryfit;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.util.Log;

import com.getcapacitor.JSObject;

import java.util.ArrayList;

/**
 * Foreground-scoped Android SpeechRecognizer manager.
 * Strictly listens ONLY while Armory Fit is open in the foreground (`desiredEnabled && appVisible`).
 * Prefers Pixel's on-device NPU SpeechRecognizer when available, with automatic fallback to standard recognition.
 */
final class VoiceRecognitionManager {
    private static final String TAG = "ArmoryVoice";
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private static volatile boolean desiredEnabled = false;
    private static volatile boolean appVisible = true;
    private static volatile boolean active = false;
    private static volatile boolean fallbackStandard = false;
    private static volatile boolean usingOnDevice = false;
    private static volatile String lastError = null;
    private static volatile ArrayList<String> biasStrings = new ArrayList<>();

    private static SpeechRecognizer recognizer;
    private static String lastPartial = "";
    private static Runnable pendingRestart;

    private VoiceRecognitionManager() {}

    static void onAppVisibilityChanged(Context c, boolean visible) {
        appVisible = visible;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (!appVisible) {
                destroyInternal();
                if (desiredEnabled) {
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "paused_bg", null);
                }
            } else if (desiredEnabled) {
                scheduleRestart(app, 250);
            }
        });
    }

    static void setEnabled(Context c, boolean enabled, ArrayList<String> bias) {
        desiredEnabled = enabled;
        biasStrings = bias != null ? new ArrayList<>(bias) : new ArrayList<>();
        lastError = null;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (desiredEnabled && appVisible) {
                startInternal(app);
            } else {
                destroyInternal();
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "stopped", null);
            }
        });
    }

    static void stop() {
        desiredEnabled = false;
        MAIN.post(() -> {
            destroyInternal();
            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "stopped", null);
        });
    }

    static JSObject status(Context c, boolean permGranted) {
        Context app = c.getApplicationContext();
        boolean onDevAvail = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                && SpeechRecognizer.isOnDeviceRecognitionAvailable(app);
        boolean avail = onDevAvail || SpeechRecognizer.isRecognitionAvailable(app);
        JSObject ret = new JSObject();
        ret.put("listening", active && desiredEnabled && appVisible);
        ret.put("granted", permGranted);
        ret.put("available", avail);
        ret.put("onDevice", usingOnDevice || (onDevAvail && !fallbackStandard));
        if (lastError != null) ret.put("error", lastError);
        return ret;
    }

    private static void cancelRestart() {
        if (pendingRestart != null) {
            MAIN.removeCallbacks(pendingRestart);
            pendingRestart = null;
        }
    }

    private static void scheduleRestart(Context app, long delayMs) {
        cancelRestart();
        if (!desiredEnabled || !appVisible) return;
        pendingRestart = () -> {
            pendingRestart = null;
            if (desiredEnabled && appVisible) startInternal(app);
        };
        MAIN.postDelayed(pendingRestart, delayMs);
    }

    private static void destroyInternal() {
        cancelRestart();
        active = false;
        lastPartial = "";
        if (recognizer != null) {
            try { recognizer.cancel(); } catch (Exception ignored) {}
            try { recognizer.destroy(); } catch (Exception ignored) {}
            recognizer = null;
        }
    }

    private static Intent buildIntent(boolean useOnDev) {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3);
        i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
        if (useOnDev && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            i.putExtra(
                    RecognizerIntent.EXTRA_SEGMENTED_SESSION,
                    RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS);
            if (biasStrings != null && !biasStrings.isEmpty()) {
                i.putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, new ArrayList<>(biasStrings));
            }
        }
        return i;
    }

    private static void startInternal(Context app) {
        cancelRestart();
        if (!desiredEnabled || !appVisible) return;
        destroyInternal();

        boolean useOnDev = !fallbackStandard
                && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                && SpeechRecognizer.isOnDeviceRecognitionAvailable(app);

        SpeechRecognizer sr = null;
        try {
            if (useOnDev) {
                sr = SpeechRecognizer.createOnDeviceSpeechRecognizer(app);
            } else if (SpeechRecognizer.isRecognitionAvailable(app)) {
                sr = SpeechRecognizer.createSpeechRecognizer(app);
            }
        } catch (Exception e) {
            Log.w(TAG, "createSpeechRecognizer failed (onDevice=" + useOnDev + ")", e);
            if (useOnDev) {
                fallbackStandard = true;
                useOnDev = false;
                try {
                    if (SpeechRecognizer.isRecognitionAvailable(app)) {
                        sr = SpeechRecognizer.createSpeechRecognizer(app);
                    }
                } catch (Exception ignored) {}
            }
        }

        if (sr == null) {
            active = false;
            lastError = "Speech recognition unavailable on this device";
            desiredEnabled = false;
            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
            return;
        }

        final boolean sessionOnDev = useOnDev;
        usingOnDevice = sessionOnDev;
        final SpeechRecognizer activeSr = sr;
        recognizer = activeSr;
        final Intent intent = buildIntent(sessionOnDev);

        activeSr.setRecognitionListener(new RecognitionListener() {
            @Override
            public void onReadyForSpeech(Bundle params) {
                active = true;
                lastError = null;
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "listening", null);
            }

            @Override
            public void onBeginningOfSpeech() {}

            @Override
            public void onRmsChanged(float rmsdB) {}

            @Override
            public void onBufferReceived(byte[] buffer) {}

            @Override
            public void onEndOfSpeech() {}

            @Override
            public void onError(int error) {
                if (!desiredEnabled || !appVisible) return;
                if ((error == SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE
                        || error == SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED
                        || error == SpeechRecognizer.ERROR_CANNOT_CHECK_SUPPORT) && !fallbackStandard) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        try { activeSr.triggerModelDownload(intent); } catch (Exception ignored) {}
                    }
                    fallbackStandard = true;
                    scheduleRestart(app, 400);
                    return;
                }
                if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                    scheduleRestart(app, 250);
                    return;
                }
                if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                    desiredEnabled = false;
                    lastError = "Microphone permission denied";
                    destroyInternal();
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
                    return;
                }
                if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY
                        || error == SpeechRecognizer.ERROR_CLIENT
                        || error == SpeechRecognizer.ERROR_SERVER_DISCONNECTED) {
                    destroyInternal();
                    scheduleRestart(app, 1200);
                    return;
                }
                destroyInternal();
                scheduleRestart(app, 2000);
            }

            @Override
            public void onResults(Bundle results) {
                String text = extractTop(results);
                lastPartial = "";
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, true, "mic", "listening", null);
                }
                if (desiredEnabled && appVisible) scheduleRestart(app, 250);
            }

            @Override
            public void onSegmentResults(Bundle segmentResults) {
                String text = extractTop(segmentResults);
                lastPartial = "";
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, true, "mic", "listening", null);
                }
            }

            @Override
            public void onEndOfSegmentedSession() {
                if (desiredEnabled && appVisible) scheduleRestart(app, 250);
            }

            @Override
            public void onPartialResults(Bundle partialResults) {
                String text = extractTop(partialResults);
                if (!text.isEmpty() && !text.equals(lastPartial)) {
                    lastPartial = text;
                    WorkoutServicePlugin.deliverVoiceEvent(text, false, "mic", "listening", null);
                }
            }

            @Override
            public void onEvent(int eventType, Bundle params) {}
        });

        try {
            active = true;
            sr.startListening(intent);
        } catch (Exception e) {
            Log.w(TAG, "startListening failed", e);
            active = false;
            lastError = e.getMessage();
            destroyInternal();
            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
        }
    }

    private static String extractTop(Bundle b) {
        if (b == null) return "";
        ArrayList<String> list = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (list == null || list.isEmpty() || list.get(0) == null) return "";
        return list.get(0).trim();
    }
}
