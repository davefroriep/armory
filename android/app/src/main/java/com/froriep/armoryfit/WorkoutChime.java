package com.froriep.armoryfit;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.util.Base64;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;

/**
 * Plays WAV alert chimes using USAGE_MEDIA + MediaPlayer + AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK so
 * Bluetooth headphones always hear the chime at media volume while music ducks and pops right back.
 */
final class WorkoutChime {
    private static final String TAG = "ArmoryChime";
    private static final String FILE_NAME = "armory_rest_chime.wav";
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private static MediaPlayer activePlayer;
    private static Object activeFocusReq;
    private static Runnable safetyRelease;

    private WorkoutChime() {}

    static File cacheFile(Context c) {
        return new File(c.getCacheDir(), FILE_NAME);
    }

    static synchronized void cacheWav(Context c, String wavBase64) {
        if (wavBase64 == null || wavBase64.isEmpty()) return;
        try {
            byte[] bytes = Base64.decode(wavBase64, Base64.DEFAULT);
            if (bytes.length < 44) return;
            File out = cacheFile(c);
            try (FileOutputStream fos = new FileOutputStream(out, false)) {
                fos.write(bytes);
                fos.flush();
            }
        } catch (Exception e) {
            Log.w(TAG, "cacheWav failed", e);
        }
    }

    static void playBase64(Context c, String wavBase64, float volume) {
        if (wavBase64 != null && !wavBase64.isEmpty()) cacheWav(c, wavBase64);
        playCached(c, volume);
    }

    static void playCached(Context c, float volume) {
        final File f = cacheFile(c);
        if (!f.exists() || f.length() < 44) {
            Log.w(TAG, "No cached chime WAV to play");
            return;
        }
        final float vol = Math.max(0f, Math.min(1f, volume));
        final Context appCtx = c.getApplicationContext();
        MAIN.post(() -> startOnMain(appCtx, f, vol));
    }

    static void vibrateRestDone(Context c) {
        try {
            long[] pattern = new long[] {0L, 220L, 90L, 220L};
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                VibratorManager vm = (VibratorManager) c.getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
                if (vm != null) {
                    vm.getDefaultVibrator().vibrate(VibrationEffect.createWaveform(pattern, -1));
                }
            } else {
                Vibrator v = (Vibrator) c.getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null && v.hasVibrator()) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        v.vibrate(VibrationEffect.createWaveform(pattern, -1));
                    } else {
                        v.vibrate(pattern, -1);
                    }
                }
            }
        } catch (Exception ignored) {
        }
    }

    private static void startOnMain(Context c, File wavFile, float vol) {
        releaseOnMain(c);
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        AudioManager am = (AudioManager) c.getSystemService(Context.AUDIO_SERVICE);
        if (am != null) {
            AudioManager.OnAudioFocusChangeListener listener = focusChange -> {};
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                AudioFocusRequest req = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                        .setAudioAttributes(attrs)
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

        MediaPlayer mp = new MediaPlayer();
        try {
            mp.setAudioAttributes(attrs);
            mp.setDataSource(wavFile.getAbsolutePath());
            mp.prepare();
            mp.setVolume(vol, vol);
            mp.setOnCompletionListener(p -> releaseOnMain(c));
            mp.setOnErrorListener((p, what, extra) -> {
                releaseOnMain(c);
                return true;
            });
            activePlayer = mp;
            int durMs = Math.max(400, mp.getDuration());
            safetyRelease = () -> releaseOnMain(c);
            MAIN.postDelayed(safetyRelease, durMs + 350L);
            mp.start();
        } catch (Exception e) {
            Log.w(TAG, "MediaPlayer chime failed", e);
            try {
                mp.release();
            } catch (Exception ignored) {
            }
            releaseOnMain(c);
        }
    }

    private static void releaseOnMain(Context c) {
        if (safetyRelease != null) {
            MAIN.removeCallbacks(safetyRelease);
            safetyRelease = null;
        }
        if (activePlayer != null) {
            try {
                if (activePlayer.isPlaying()) activePlayer.stop();
            } catch (Exception ignored) {
            }
            try {
                activePlayer.release();
            } catch (Exception ignored) {
            }
            activePlayer = null;
        }
        AudioManager am = (AudioManager) c.getSystemService(Context.AUDIO_SERVICE);
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
