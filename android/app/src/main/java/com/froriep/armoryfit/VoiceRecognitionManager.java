package com.froriep.armoryfit;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.speech.RecognitionListener;
import android.speech.RecognitionService;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.util.Log;

import com.getcapacitor.JSObject;

import java.util.ArrayList;
import java.util.LinkedHashSet;

/**
 * Foreground-scoped Android SpeechRecognizer manager.
 * Binds explicitly to GoogleTTSRecognitionService (`com.google.android.tts`) first on Pixel/Android
 * devices to bypass deadlocking Android System Intelligence (`AiAiSpeechRecognitionService`) binders,
 * reads both `RESULTS_RECOGNITION` and `UNSTABLE_TEXT` partial bundles, forwards full N-best
 * candidate lists to JS, and falls back gracefully on older/non-GMS devices.
 */
final class VoiceRecognitionManager {
    private static final String TAG = "ArmoryVoice";
    private static final ComponentName GOOGLE_TTS_SERVICE = new ComponentName(
            "com.google.android.tts",
            "com.google.android.apps.speech.tts.googletts.service.GoogleTTSRecognitionService"
    );
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final long STARTUP_TIMEOUT_MS = 4_000L;
    private static final long SESSION_MAX_IDLE_MS = 38_000L;
    private static final long WATCHDOG_PERIOD_MS = 4_000L;

    private static volatile boolean desiredEnabled = false;
    private static volatile boolean appVisible = true;
    private static volatile boolean ttsPaused = false;
    private static volatile boolean active = false;
    private static volatile boolean useGoogleTts = true;
    private static volatile boolean usingGoogleTts = false;
    private static volatile boolean usingOnDevice = false;
    private static volatile String lastError = null;
    private static volatile ArrayList<String> biasStrings = new ArrayList<>();

    private static SpeechRecognizer recognizer;
    private static int recognizerGen = 0;
    private static boolean listeningInProgress = false;
    private static boolean readyReceived = false;
    private static int consecutiveStartErrors = 0;
    private static int consecutiveModeFailures = 0;
    private static int consecutiveDeadRestarts = 0;
    private static long listenStartedElapsedMs = 0L;
    private static long lastCallbackElapsedMs = 0L;
    private static long lastSpeechElapsedMs = 0L;
    private static String lastPartial = "";
    private static ArrayList<String> lastPartialCands = null;
    private static Runnable pendingRestart;
    private static Runnable watchdogRunnable;

    private VoiceRecognitionManager() {}

    static void onAppVisibilityChanged(Context c, boolean visible) {
        appVisible = visible;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (!appVisible) {
                ttsPaused = false;
                destroyInternal();
                if (desiredEnabled) {
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "paused_bg", null);
                }
            } else if (desiredEnabled && !ttsPaused) {
                ensureWatchdog(app);
                scheduleRestart(app, 250, true);
            }
        });
    }

    static void pauseForTts() {
        ttsPaused = true;
        MAIN.post(() -> {
            boolean keepActive = active;
            destroyInternal();
            active = keepActive;
        });
    }

    static void resumeAfterTts(Context c) {
        ttsPaused = false;
        if (!desiredEnabled || !appVisible || c == null) return;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (ttsPaused || !desiredEnabled || !appVisible) return;
            ensureWatchdog(app);
            scheduleRestart(app, 160, true);
        });
    }

    static void setEnabled(Context c, boolean enabled, ArrayList<String> bias, boolean forceRestart) {
        desiredEnabled = enabled;
        biasStrings = bias != null ? new ArrayList<>(bias) : new ArrayList<>();
        lastError = null;
        if (!enabled) ttsPaused = false;
        if (forceRestart) {
            consecutiveModeFailures = 0;
            consecutiveDeadRestarts = 0;
        }
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (desiredEnabled && appVisible) {
                ensureWatchdog(app);
                if (!ttsPaused && (forceRestart || !listeningInProgress)) {
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
        if (!desiredEnabled || !appVisible || ttsPaused || c == null) return;
        Context app = c.getApplicationContext();
        MAIN.post(() -> {
            if (!desiredEnabled || !appVisible || ttsPaused) return;
            long now = SystemClock.elapsedRealtime();
            if (!listeningInProgress || (now - lastCallbackElapsedMs > 2_000L && now - lastSpeechElapsedMs > 2_000L)) {
                scheduleRestart(app, 200, true);
            }
        });
    }

    static void stop() {
        desiredEnabled = false;
        ttsPaused = false;
        MAIN.post(() -> {
            destroyInternal();
            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "stopped", null);
        });
    }

    static JSObject status(Context c, boolean permGranted) {
        Context app = c.getApplicationContext();
        boolean ttsAvail = hasGoogleTtsService(app);
        boolean avail = ttsAvail
                || SpeechRecognizer.isRecognitionAvailable(app)
                || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && SpeechRecognizer.isOnDeviceRecognitionAvailable(app));
        JSObject ret = new JSObject();
        ret.put("listening", active && desiredEnabled && appVisible);
        ret.put("granted", permGranted);
        ret.put("available", avail);
        ret.put("onDevice", usingOnDevice || usingGoogleTts);
        if (lastError != null) ret.put("error", lastError);
        return ret;
    }

    private static boolean hasGoogleTtsService(Context app) {
        try {
            return app.getPackageManager().resolveService(
                    new Intent(RecognitionService.SERVICE_INTERFACE).setComponent(GOOGLE_TTS_SERVICE),
                    0
            ) != null;
        } catch (Exception ignored) {
            return false;
        }
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
                if (ttsPaused) {
                    MAIN.postDelayed(this, WATCHDOG_PERIOD_MS);
                    return;
                }
                long now = SystemClock.elapsedRealtime();
                if (!listeningInProgress && pendingRestart == null) {
                    Log.w(TAG, "Watchdog: idle without pending restart, restarting");
                    startListeningCycle(app, true);
                } else if (listeningInProgress && !readyReceived && (now - listenStartedElapsedMs > STARTUP_TIMEOUT_MS)) {
                    Log.w(TAG, "Watchdog: startup timed out (googleTts=" + usingGoogleTts + ", onDev=" + usingOnDevice + ")");
                    active = false;
                    escalateRecognizerMode("Recognizer startup timed out");
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                    startListeningCycle(app, true);
                } else if (listeningInProgress && (now - listenStartedElapsedMs > SESSION_MAX_IDLE_MS) && (now - lastSpeechElapsedMs > 6_000L)) {
                    Log.w(TAG, "Watchdog: idle cycle > 38s, refreshing recognizer");
                    flushPartialAsFinalIfAny();
                    startListeningCycle(app, true);
                }
                if (desiredEnabled && appVisible) {
                    MAIN.postDelayed(this, WATCHDOG_PERIOD_MS);
                }
            }
        };
        MAIN.postDelayed(watchdogRunnable, WATCHDOG_PERIOD_MS);
    }

    private static void flushPartialAsFinalIfAny() {
        String pending = lastPartial == null ? "" : lastPartial.trim();
        ArrayList<String> cands = lastPartialCands != null ? new ArrayList<>(lastPartialCands) : null;
        lastPartial = "";
        lastPartialCands = null;
        if (pending.isEmpty()) return;
        if (cands == null || cands.isEmpty()) {
            cands = new ArrayList<>();
            cands.add(pending);
        }
        Log.i(TAG, "Flushing partial as final: cands=" + cands);
        WorkoutServicePlugin.deliverVoiceEvent(pending, cands, true, "mic", "listening", null);
    }

    private static void escalateRecognizerMode(String reason) {
        lastError = reason;
        consecutiveModeFailures++;
        if (usingGoogleTts && consecutiveModeFailures >= 3) {
            useGoogleTts = false;
            Log.w(TAG, "GoogleTTSRecognitionService failed 3x, falling back to default SpeechRecognizer (" + reason + ")");
        }
    }

    private static void scheduleRestart(Context app, long delayMs, boolean forceRecreate) {
        cancelRestart();
        if (!desiredEnabled || !appVisible || ttsPaused) return;
        pendingRestart = () -> {
            pendingRestart = null;
            if (desiredEnabled && appVisible && !ttsPaused) startListeningCycle(app, forceRecreate);
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
        lastPartialCands = null;
        if (recognizer != null) {
            try { recognizer.cancel(); } catch (Exception ignored) {}
            try { recognizer.destroy(); } catch (Exception ignored) {}
            recognizer = null;
        }
    }

    private static Intent buildIntent() {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US");
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-US");
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && biasStrings != null && !biasStrings.isEmpty()) {
            i.putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, new ArrayList<>(biasStrings));
        }
        return i;
    }

    private static boolean ensureRecognizer(Context app) {
        if (recognizer != null) return true;
        boolean targetTts = useGoogleTts && hasGoogleTtsService(app);
        boolean targetOnDev = false;

        SpeechRecognizer sr = null;
        try {
            if (targetTts) {
                sr = SpeechRecognizer.createSpeechRecognizer(app, GOOGLE_TTS_SERVICE);
            } else if (SpeechRecognizer.isRecognitionAvailable(app)) {
                sr = SpeechRecognizer.createSpeechRecognizer(app);
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && SpeechRecognizer.isOnDeviceRecognitionAvailable(app)) {
                targetOnDev = true;
                sr = SpeechRecognizer.createOnDeviceSpeechRecognizer(app);
            }
        } catch (Exception e) {
            Log.w(TAG, "createSpeechRecognizer failed (targetTts=" + targetTts + ")", e);
            targetTts = false;
            try {
                if (SpeechRecognizer.isRecognitionAvailable(app)) {
                    sr = SpeechRecognizer.createSpeechRecognizer(app);
                }
            } catch (Exception ignored) {}
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
        usingGoogleTts = targetTts;
        usingOnDevice = targetOnDev;
        final SpeechRecognizer activeSr = sr;
        recognizer = activeSr;
        Log.i(TAG, "ensureRecognizer created gen=" + myGen + " googleTts=" + usingGoogleTts + " onDev=" + usingOnDevice);

        activeSr.setRecognitionListener(new RecognitionListener() {
            @Override
            public void onReadyForSpeech(Bundle params) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                long now = SystemClock.elapsedRealtime();
                readyReceived = true;
                active = true;
                consecutiveStartErrors = 0;
                consecutiveModeFailures = 0;
                consecutiveDeadRestarts = 0;
                lastError = null;
                lastCallbackElapsedMs = now;
                Log.i(TAG, "onReadyForSpeech gen=" + myGen + " googleTts=" + usingGoogleTts);
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
                Log.w(TAG, "onError code=" + error + " wasReady=" + wasReady + " googleTts=" + usingGoogleTts);
                if (!desiredEnabled || !appVisible) return;

                if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                    desiredEnabled = false;
                    lastError = "Microphone permission denied";
                    destroyInternal();
                    WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "error", lastError);
                    return;
                }

                if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                    if (wasReady) flushPartialAsFinalIfAny();
                    if (!wasReady) {
                        consecutiveDeadRestarts++;
                        if (consecutiveDeadRestarts >= 2) {
                            active = false;
                            escalateRecognizerMode("Recognizer ended before speech ready (#" + error + ")");
                            WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                            scheduleRestart(app, 250, true);
                            return;
                        }
                    } else {
                        consecutiveDeadRestarts = 0;
                    }
                    scheduleRestart(app, 140, !wasReady);
                    return;
                }

                active = false;
                escalateRecognizerMode("Speech error #" + error);
                WorkoutServicePlugin.deliverVoiceEvent("", false, "mic", "recovering", lastError);
                scheduleRestart(app, 650, true);
            }

            @Override
            public void onResults(Bundle results) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                listeningInProgress = false;
                consecutiveStartErrors = 0;
                consecutiveModeFailures = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                ArrayList<String> cands = extractCandidates(results, false);
                String text = cands.isEmpty() ? "" : cands.get(0);
                lastPartial = "";
                lastPartialCands = null;
                Log.i(TAG, "onResults cands=" + cands);
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, cands, true, "mic", "listening", null);
                }
                if (desiredEnabled && appVisible) scheduleRestart(app, 120, false);
            }

            @Override
            public void onSegmentResults(Bundle segmentResults) {
                if (myGen != recognizerGen || recognizer != activeSr) return;
                readyReceived = true;
                active = true;
                consecutiveStartErrors = 0;
                consecutiveModeFailures = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                ArrayList<String> cands = extractCandidates(segmentResults, false);
                String text = cands.isEmpty() ? "" : cands.get(0);
                lastPartial = "";
                lastPartialCands = null;
                Log.i(TAG, "onSegmentResults cands=" + cands);
                if (!text.isEmpty()) {
                    WorkoutServicePlugin.deliverVoiceEvent(text, cands, true, "mic", "listening", null);
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
                consecutiveModeFailures = 0;
                consecutiveDeadRestarts = 0;
                long now = SystemClock.elapsedRealtime();
                lastCallbackElapsedMs = now;
                lastSpeechElapsedMs = now;
                ArrayList<String> cands = extractCandidates(partialResults, true);
                String text = cands.isEmpty() ? "" : cands.get(0);
                if (!text.isEmpty() && !text.equals(lastPartial)) {
                    lastPartial = text;
                    lastPartialCands = new ArrayList<>(cands);
                    Log.i(TAG, "onPartialResults cands=" + cands);
                    WorkoutServicePlugin.deliverVoiceEvent(text, cands, false, "mic", "listening", null);
                }
            }

            @Override
            public void onEvent(int eventType, Bundle params) {}
        });
        return true;
    }

    private static void startListeningCycle(Context app, boolean forceRecreate) {
        cancelRestart();
        if (!desiredEnabled || !appVisible || ttsPaused) return;
        if (forceRecreate) {
            boolean keepActive = active;
            destroyInternal();
            active = keepActive;
        }
        ensureWatchdog(app);
        if (!ensureRecognizer(app)) return;

        final Intent intent = buildIntent();
        lastPartial = "";
        lastPartialCands = null;
        readyReceived = false;
        listeningInProgress = true;
        listenStartedElapsedMs = SystemClock.elapsedRealtime();
        lastCallbackElapsedMs = listenStartedElapsedMs;
        if (lastSpeechElapsedMs == 0L || forceRecreate) {
            lastSpeechElapsedMs = listenStartedElapsedMs;
        }

        try {
            if (listeningInProgress) {
                Log.i(TAG, "startListening gen=" + recognizerGen + " googleTts=" + usingGoogleTts);
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
                scheduleRestart(app, 650, true);
            }
        }
    }

    private static ArrayList<String> extractCandidates(Bundle b, boolean isPartial) {
        ArrayList<String> out = new ArrayList<>();
        if (b == null) return out;
        LinkedHashSet<String> seen = new LinkedHashSet<>();

        ArrayList<String> list = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        ArrayList<String> unstable = b.getStringArrayList("android.speech.extra.UNSTABLE_TEXT");
        String topStable = "";
        if (list != null) {
            for (String s : list) {
                if (s != null && !s.trim().isEmpty()) {
                    topStable = s.trim();
                    break;
                }
            }
        }

        // GoogleTTSRecognitionService places active streaming partial words into UNSTABLE_TEXT.
        if (isPartial && unstable != null) {
            for (String s : unstable) {
                if (s == null) continue;
                String u = s.trim();
                if (!u.isEmpty()) {
                    String combined = topStable.isEmpty() ? u : (topStable + " " + u).trim();
                    if (seen.add(combined)) out.add(combined);
                    if (seen.add(u)) out.add(u);
                }
            }
        }

        if (list != null) {
            for (String s : list) {
                if (s == null) continue;
                String t = s.trim();
                if (!t.isEmpty() && seen.add(t)) out.add(t);
            }
        }

        if (!isPartial && unstable != null) {
            for (String s : unstable) {
                if (s == null) continue;
                String u = s.trim();
                if (!u.isEmpty()) {
                    String combined = topStable.isEmpty() ? u : (topStable + " " + u).trim();
                    if (seen.add(combined)) out.add(combined);
                    if (seen.add(u)) out.add(u);
                }
            }
        }
        return out;
    }
}
