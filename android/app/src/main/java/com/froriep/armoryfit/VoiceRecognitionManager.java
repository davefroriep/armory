package com.froriep.armoryfit;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.util.Log;

import com.getcapacitor.JSObject;

import java.util.ArrayList;

/**
 * Foreground-scoped Android SpeechRecognizer manager.
 * Strictly listens ONLY while Armory Fit is open in the foreground (`desiredEnabled && appVisible`).
 * Sets active state truthfully on `onReadyForSpeech`/speech results, escalates cleanly from
 * on-device segmented SODA -> non-segmented -> standard RecognitionService on startup/binder
 * failures, and recovers cleanly after audio focus chimes.
 */
final class VoiceRecognitionManager {
    private static final String TAG = "ArmoryVoice";
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final int SEGMENT_WINDOW_MS = 30_000;
    private static final long STARTUP_TIMEOUT_MS = 4_000L;
    private static final long SESSION_MAX_IDLE_MS = 38_000L;
    private static final long WATCHDOG_PERIOD_MS = 4_000L;

    private static volatile boolean desiredEnabled = false;
    private static volatile boolean appVisible = true;
    private static volatile boolean active = false;
    private static volatile boolean fallbackStandard = false;
    private static volatile boolean usingOnDevice = false;
    private static volatile boolean useSegmented = true;
    private static volatile String lastError = null;
    private static volatile ArrayList<String> biasStrings = new ArrayList<>();

    private static SpeechRecognizer recognizer;
    private static int recognizerGen = 0;
    private static boolean listeningInProgress = false;
    private static boolean readyReceived = false;
    private static int consecutiveStartErrors = 0;
    private static int consecutiveOnDevErrors = 0;
    private static int consecutiveDeadRestarts = 0;
    private static long listenStartedElapsedMs = 0L;
    private static long lastCallbackElapsedMs = 0L;
    private static long lastSpeechElapsedMs = 0L;
    private static String lastPartial = "";
    private static Runnable pendingRestart;
    private static Runnable watchdogRunnable;

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
                ensureWatchdog(app);
                scheduleRestart(app, 250, true);
            }
        });
    }

    static void setEnabled(Context c, boolean enabled, ArrayList<String> bias, boolean forceRestart) {
        desiredEnabled = enabled;
        biasStrings = bias != null ? new ArrayList<>(bias) : new ArrayList<>();
        lastError = null;
        if (forceRestart) {
            // Let a manual restart re-evaluate clean recognizer mode if desired
            consecutiveOnDevErrors = 0;
            consecutiveDeadRestarts = 0;
        }
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (desiredEnabled && appVisible) {
                ensureWatchdog(app);
                if (forceRestart || !listeningInProgress) {
                    startListeningCycle(app, forceRestart);
                }
            } else {
                destroyInternal();
                if (!desiredEnabled) {
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "stopped", null);
                }
            }
        });
    }

    static void onAudioFocusRestored(Context c) {
        if (!desiredEnabled || !appVisible || c == null) return;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (!desiredEnabled || !appVisible) return;
            long now = SystemClock.elapsedRealtime();
            if (!listeningInProgress || (now - lastCallbackElapsedMs > 2_000L && now - lastSpeechElapsedMs > 2_000L)) {
                scheduleRestart(app, 200, true);
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

    private static void cancelWatchdog() {
        if (watchdogRunnable != null) {
            MAIN.removeCallbacks(watchdogRunnable);
            watchdogRunnable = null;
        }
    }

    private static void ensureWatchdog(Context app) {
        cancelWatchdog();
        if (!desiredEnabled || !appVisible) return;
        watchdogRunnable = new Runnable() {
            @Override
            public void run() {
                if (!desiredEnabled || !appVisible || watchdogRunnable != this) return;
                long now = SystemClock.elapsedRealtime();
                if (!listeningInProgress && pendingRestart == null) {
                    Log.w(TAG, "Watchdog: idle without pending restart, restarting");
                    startListeningCycle(app, true);
                } else if (listeningInProgress && !readyReceived && (now - listenStartedElapsedMs > STARTUP_TIMEOUT_MS)) {
                    Log.w(TAG, "Watchdog: startup timed out (onDev=" + usingOnDevice + ", seg=" + useSegmented + ")");
                    active = false;
                    escalateRecognizerMode("Recognizer startup timed out");
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                    startListeningCycle(app, true);
                } else if (listeningInProgress && (now - listenStartedElapsedMs > SESSION_MAX_IDLE_MS) && (now - lastSpeechElapsedMs > 6_000L)) {
                    Log.w(TAG, "Watchdog: session idle > 38s, recreating recognizer");
                    startListeningCycle(app, true);
                }
                if (desiredEnabled && appVisible) {
                    MAIN.postDelayed(this, WATCHDOG_PERIOD_MS);
                }
            }
        };
        MAIN.postDelayed(watchdogRunnable, WATCHDOG_PERIOD_MS);
    }

    private static void escalateRecognizerMode(String reason) {
        lastError = reason;
        if (usingOnDevice) {
            consecutiveOnDevErrors++;
            if (useSegmented) {
                useSegmented = false;
                Log.w(TAG, "Disabling segmented session (" + reason + ")");
            } else if (consecutiveOnDevErrors >= 2) {
                fallbackStandard = true;
                Log.w(TAG, "Switching from on-device SODA to standard SpeechRecognizer (" + reason + ")");
            }
        }
    }

    private static void scheduleRestart(Context app, long delayMs, boolean forceRecreate) {
        cancelRestart();
        if (!desiredEnabled || !appVisible) return;
        pendingRestart = () -> {
            pendingRestart = null;
            if (desiredEnabled && appVisible) startListeningCycle(app, forceRecreate);
        };
        MAIN.postDelayed(pendingRestart, delayMs);
    }

    private static void destroyInternal() {
        cancelRestart();
        cancelWatchdog();
        recognizerGen++;
        active = false;
        listeningInProgress = false;
        readyReceived = false;
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
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US");
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-US");
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3);
        if (!useOnDev) {
            i.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (useSegmented) {
                i.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, SEGMENT_WINDOW_MS);
                i.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1200);
                i.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 900);
                i.putExtra(RecognizerIntent.EXTRA_SEGMENTED_SESSION, RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS);
            }
            if (biasStrings != null && !biasStrings.isEmpty()) {
                i.putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, new ArrayList<>(biasStrings));
            }
        }
        return i;
    }

    private static boolean ensureRecognizer(Context app) {
        if (recognizer != null) return true;
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
            listeningInProgress = false;
            lastError = "Speech recognition unavailable on this device";
            desiredEnabled = false;
            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
            return false;
        }

        final int myGen = ++recognizerGen;
        usingOnDevice = useOnDev;
        final SpeechRecognizer activeSr = sr;
        recognizer = activeSr;
        Log.i(TAG, "ensureRecognizer created gen=" + myGen + " onDev=" + usingOnDevice + " seg=" + useSegmented);

        activeSr.setRecognitionListener(new RecognitionListener() {
            @Override
            public void onReadyForSpeech(Bundle params) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                long now = SystemClock.elapsedRealtime();
                readyReceived = true;
                active = true;
                consecutiveStartErrors = 0;
                consecutiveDeadRestarts = 0;
                lastError = null;
                lastCallbackElapsedMs = now;
                Log.i(TAG, "onReadyForSpeech gen=" + myGen + " onDev=" + usingOnDevice);
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "listening", null);
            }

            @Override
            public void onBeginningOfSpeech() {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
            }

            @Override
            public void onRmsChanged(float rmsdB) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                lastCallbackElapsedMs = SystemClock.elapsedRealtime();
            }

            @Override
            public void onBufferReceived(byte[] buffer) {}

            @Override
            public void onEndOfSpeech() {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                lastCallbackElapsedMs = SystemClock.elapsedRealtime();
            }

            @Override
            public void onError(int error) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                listeningInProgress = false;
                boolean wasReady = readyReceived;
                readyReceived = false;
                Log.w(TAG, "onError code=" + error + " wasReady=" + wasReady + " onDev=" + usingOnDevice + " seg=" + useSegmented);
                if (!desiredEnabled || !appVisible) return;

                if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                    desiredEnabled = false;
                    lastError = "Microphone permission denied";
                    destroyInternal();
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
                    return;
                }

                if (error == SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE
                        || error == SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED
                        || error == SpeechRecognizer.ERROR_CANNOT_CHECK_SUPPORT) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        try { activeSr.triggerModelDownload(buildIntent(usingOnDevice)); } catch (Exception ignored) {}
                    }
                    active = false;
                    fallbackStandard = true;
                    lastError = "Falling back to standard speech service";
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                    scheduleRestart(app, 350, true);
                    return;
                }

                if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                    if (!wasReady) {
                        consecutiveDeadRestarts++;
                        if (usingOnDevice && consecutiveDeadRestarts >= 2) {
                            active = false;
                            escalateRecognizerMode("On-device recognizer timed out before ready");
                            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                            scheduleRestart(app, 250, true);
                            return;
                        }
                    } else {
                        consecutiveDeadRestarts = 0;
                    }
                    // Recreate cleanly on on-device cycles every few silent timeouts to avoid stale binder drops
                    boolean recreate = !wasReady || usingOnDevice;
                    scheduleRestart(app, 180, recreate);
                    return;
                }

                // Real operational error (ERROR_RECOGNIZER_BUSY=8, ERROR_CLIENT=5, ERROR_AUDIO=3, ERROR_SERVER_DISCONNECTED=11, etc.)
                active = false;
                escalateRecognizerMode("Speech error #" + error);
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                scheduleRestart(app, 750, true);
            }

            @Override
            public void onResults(Bundle results) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                listeningInProgress = false;
                consecutiveStartErrors = 0;
                consecutiveOnDevErrors = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                String text = extractTop(results);
                lastPartial = "";
                Log.i(TAG, "onResults: \"" + text + "\"");
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, true, "mic", "listening", null);
                }
                if (desiredEnabled && appVisible) scheduleRestart(app, 150, usingOnDevice);
            }

            @Override
            public void onSegmentResults(Bundle segmentResults) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                readyReceived = true;
                active = true;
                consecutiveStartErrors = 0;
                consecutiveOnDevErrors = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                String text = extractTop(segmentResults);
                lastPartial = "";
                Log.i(TAG, "onSegmentResults: \"" + text + "\"");
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, true, "mic", "listening", null);
                }
            }

            @Override
            public void onEndOfSegmentedSession() {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                listeningInProgress = false;
                Log.i(TAG, "onEndOfSegmentedSession");
                if (desiredEnabled && appVisible) scheduleRestart(app, 150, true);
            }

            @Override
            public void onPartialResults(Bundle partialResults) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                readyReceived = true;
                active = true;
                consecutiveStartErrors = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                String text = extractTop(partialResults);
                if (!text.isEmpty() && !text.equals(lastPartial)) {
                    lastPartial = text;
                    Log.i(TAG, "onPartialResults: \"" + text + "\"");
                    WorkoutServicePlugin.deliverVoiceEvent(text, false, "mic", "listening", null);
                }
            }

            @Override
            public void onEvent(int eventType, Bundle params) {}
        });
        return true;
    }

    private static void startListeningCycle(Context app, boolean forceRecreate) {
        cancelRestart();
        if (!desiredEnabled || !appVisible) return;
        if (forceRecreate) {
            boolean keepActive = active;
            destroyInternal();
            active = keepActive;
        }
        ensureWatchdog(app);
        if (!ensureRecognizer(app)) return;

        final Intent intent = buildIntent(usingOnDevice);
        lastPartial = "";
        readyReceived = false;
        listeningInProgress = true;
        listenStartedElapsedMs = SystemClock.elapsedRealtime();
        lastCallbackElapsedMs = listenStartedElapsedMs;
        if (lastSpeechElapsedMs == 0L || forceRecreate) {
            lastSpeechElapsedMs = listenStartedElapsedMs;
        }

        try {
            if (listeningInProgress) {
                Log.i(TAG, "startListening gen=" + recognizerGen + " onDev=" + usingOnDevice + " seg=" + useSegmented);
                recognizer.startListening(intent);
            }
        } catch (Exception e) {
            Log.w(TAG, "startListening failed", e);
            listeningInProgress = false;
            active = false;
            consecutiveStartErrors++;
            if (consecutiveStartErrors >= 5) {
                lastError = e.getMessage();
                desiredEnabled = false;
                destroyInternal();
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
            } else {
                escalateRecognizerMode("startListening exception: " + e.getMessage());
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                scheduleRestart(app, 700, true);
            }
        }
    }

    private static String extractTop(Bundle b) {
        if (b == null) return "";
        ArrayList<String> list = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (list == null || list.isEmpty() || list.get(0) == null) return "";
        return list.get(0).trim();
    }
}
