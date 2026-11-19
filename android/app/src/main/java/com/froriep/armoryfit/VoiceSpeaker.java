package com.froriep.armoryfit;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Log;

import java.util.Locale;

/**
 * Spoken confirmation readback via Android TextToSpeech (`USAGE_MEDIA` + `AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK`).
 * Pauses VoiceRecognitionManager while speaking and always resumes afterwards (via UtteranceProgressListener
 * plus a bounded failsafe timeout) so the phone mic never transcribes its own TTS readback.
 */
final class VoiceSpeaker {
    private static final String TAG = "ArmoryVoiceTts";
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final AudioAttributes TTS_ATTRS = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build();

    private static TextToSpeech tts;
    private static boolean initializing = false;
    private static boolean ready = false;
    private static String pendingText = null;
    private static int speakSeq = 0;
    private static Object activeFocusReq = null;
    private static Runnable safetyResume = null;

    private VoiceSpeaker() {}

    static void speak(Context c, String rawText) {
        if (c == null || rawText == null) return;
        final String text = rawText.trim();
        if (text.isEmpty()) return;
        final Context app = c.getApplicationContext();
        MAIN.post(() -> startOnMain(app, text));
    }

    private static void startOnMain(Context app, String text) {
        VoiceRecognitionManager.pauseForTts();
        if (ready && tts != null) {
            speakNowOnMain(app, text);
            return;
        }
        pendingText = text;
        if (initializing) return;
        initializing = true;
        try {
            tts = new TextToSpeech(app, status -> MAIN.post(() -> {
                initializing = false;
                if (status != TextToSpeech.SUCCESS || tts == null) {
                    Log.w(TAG, "TextToSpeech init failed status=" + status);
                    ready = false;
                    tts = null;
                    pendingText = null;
                    finishOnMain(app);
                    return;
                }
                tts.setLanguage(Locale.US);
                tts.setSpeechRate(1.12f);
                tts.setAudioAttributes(TTS_ATTRS);
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {}

                    @Override
                    public void onDone(String utteranceId) {
                        MAIN.post(() -> {
                            if (("armory_" + speakSeq).equals(utteranceId)) {
                                finishOnMain(app);
                            }
                        });
                    }

                    @Override
                    public void onError(String utteranceId) {
                        MAIN.post(() -> {
                            if (("armory_" + speakSeq).equals(utteranceId)) {
                                finishOnMain(app);
                            }
                        });
                    }
                });
                ready = true;
                String queued = pendingText;
                pendingText = null;
                if (queued != null && !queued.isEmpty()) {
                    speakNowOnMain(app, queued);
                } else {
                    finishOnMain(app);
                }
            }));
        } catch (Exception e) {
            Log.w(TAG, "TextToSpeech constructor failed", e);
            initializing = false;
            ready = false;
            tts = null;
            pendingText = null;
            finishOnMain(app);
        }
    }

    private static void speakNowOnMain(Context app, String text) {
        cancelSafetyResume();
        requestDuckFocus(app);
        final int seq = ++speakSeq;
        final String uttId = "armory_" + seq;
        long timeoutMs = Math.min(4_500L, 1_200L + (long) text.length() * 85L);
        safetyResume = () -> finishOnMain(app);
        MAIN.postDelayed(safetyResume, timeoutMs);

        try {
            tts.setAudioAttributes(TTS_ATTRS);
            int res = tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, uttId);
            if (res == TextToSpeech.ERROR) {
                Log.w(TAG, "tts.speak returned ERROR");
                finishOnMain(app);
            }
        } catch (Exception e) {
            Log.w(TAG, "tts.speak threw", e);
            finishOnMain(app);
        }
    }

    private static void finishOnMain(Context app) {
        cancelSafetyResume();
        abandonFocus(app);
        VoiceRecognitionManager.resumeAfterTts(app);
    }

    private static void cancelSafetyResume() {
        if (safetyResume != null) {
            MAIN.removeCallbacks(safetyResume);
            safetyResume = null;
        }
    }

    private static void requestDuckFocus(Context app) {
        abandonFocus(app);
        AudioManager am = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return;
        AudioManager.OnAudioFocusChangeListener listener = focusChange -> {};
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            AudioFocusRequest req = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(TTS_ATTRS)
                    .setWillPauseWhenDucked(false)
                    .setAcceptsDelayedFocusGain(true)
                    .setOnAudioFocusChangeListener(listener)
                    .build();
            am.requestAudioFocus(req);
            activeFocusReq = req;
        } else {
            am.requestAudioFocus(listener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK);
            activeFocusReq = listener;
        }
    }

    private static void abandonFocus(Context app) {
        AudioManager am = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
        if (am != null && activeFocusReq != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && activeFocusReq instanceof AudioFocusRequest) {
                am.abandonAudioFocusRequest((AudioFocusRequest) activeFocusReq);
            } else if (activeFocusReq instanceof AudioManager.OnAudioFocusChangeListener) {
                am.abandonAudioFocus((AudioManager.OnAudioFocusChangeListener) activeFocusReq);
            }
            activeFocusReq = null;
        }
    }
}
