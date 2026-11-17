package com.froriep.armoryfit;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.SoundPool;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Drift-free native SoundPool cadence metronome (USAGE_MEDIA + CONTENT_TYPE_SONIFICATION) that
 * mixes clicks over Bluetooth headphones without acquiring AudioFocus or colliding with Spotify/YouTube.
 */
final class WorkoutMetronome {
    private static final String TAG = "ArmoryMetro";
    private static final int SAMPLE_RATE = 24_000;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private static SoundPool pool;
    private static int hiId = 0;
    private static int loId = 0;
    private static volatile boolean hiReady = false;
    private static volatile boolean loReady = false;

    private static boolean playing = false;
    private static long periodMs = 500L;
    private static float vol = 0.8f;
    private static int beatIdx = 0;
    private static long nextBeatAt = 0L;

    private WorkoutMetronome() {}

    private static final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!playing || pool == null) return;
            long now = SystemClock.uptimeMillis();
            boolean accent = (beatIdx % 2) == 0;
            int sid = accent ? hiId : loId;
            boolean ready = accent ? hiReady : loReady;
            if (sid != 0 && ready) {
                float gain = accent ? vol : Math.max(0.05f, vol * 0.68f);
                pool.play(sid, gain, gain, 1, 0, 1f);
            }
            beatIdx++;
            nextBeatAt += periodMs;
            if (nextBeatAt < now - periodMs) nextBeatAt = now + periodMs;
            MAIN.postAtTime(this, nextBeatAt);
        }
    };

    static synchronized void warm(Context c) {
        if (pool != null) return;
        Context app = c.getApplicationContext();
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        pool = new SoundPool.Builder().setMaxStreams(2).setAudioAttributes(attrs).build();
        pool.setOnLoadCompleteListener((sp, sampleId, status) -> {
            if (status == 0) {
                if (sampleId == hiId) hiReady = true;
                else if (sampleId == loId) loReady = true;
            }
        });
        try {
            File hi = ensureClickWav(app, "armory_click_hi.wav", 1500.0, 0.85);
            File lo = ensureClickWav(app, "armory_click_lo.wav", 1100.0, 0.58);
            hiId = pool.load(hi.getAbsolutePath(), 1);
            loId = pool.load(lo.getAbsolutePath(), 1);
        } catch (Exception e) {
            Log.w(TAG, "Failed to warm SoundPool metronome", e);
        }
    }

    static synchronized void set(Context c, boolean play, double bpm, float volume) {
        vol = Math.max(0f, Math.min(1f, volume));
        if (!play || bpm <= 0.0) {
            stop();
            return;
        }
        warm(c);
        periodMs = Math.max(150L, Math.round(60_000.0 / bpm));
        if (playing) return;
        playing = true;
        beatIdx = 0;
        nextBeatAt = SystemClock.uptimeMillis() + 40L;
        MAIN.removeCallbacks(tick);
        MAIN.postAtTime(tick, nextBeatAt);
    }

    static synchronized void stop() {
        playing = false;
        MAIN.removeCallbacks(tick);
    }

    private static File ensureClickWav(Context c, String name, double freq, double amp) throws Exception {
        File out = new File(c.getCacheDir(), name);
        if (out.exists() && out.length() > 44) return out;
        int n = Math.round(0.045f * SAMPLE_RATE);
        byte[] wav = new byte[44 + n * 2];
        ByteBuffer hdr = ByteBuffer.wrap(wav, 0, 44).order(ByteOrder.LITTLE_ENDIAN);
        hdr.put(new byte[] {'R', 'I', 'F', 'F'});
        hdr.putInt(36 + n * 2);
        hdr.put(new byte[] {'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
        hdr.putInt(16);
        hdr.putShort((short) 1);
        hdr.putShort((short) 1);
        hdr.putInt(SAMPLE_RATE);
        hdr.putInt(SAMPLE_RATE * 2);
        hdr.putShort((short) 2);
        hdr.putShort((short) 16);
        hdr.put(new byte[] {'d', 'a', 't', 'a'});
        hdr.putInt(n * 2);

        ByteBuffer pcm = ByteBuffer.wrap(wav, 44, n * 2).order(ByteOrder.LITTLE_ENDIAN);
        int atkSamples = Math.max(1, Math.round(0.004f * SAMPLE_RATE));
        double durSec = (double) n / SAMPLE_RATE;
        for (int i = 0; i < n; i++) {
            double t = (double) i / SAMPLE_RATE;
            double att = i < atkSamples ? (double) i / atkSamples : 1.0;
            double env = amp * att * Math.exp(-t / (durSec * 0.28));
            double sq = Math.sin(2.0 * Math.PI * freq * t) >= 0.0 ? 0.85 : -0.85;
            double tone = 0.65 * Math.sin(2.0 * Math.PI * freq * t) + 0.35 * sq;
            short s = (short) Math.max(-32767, Math.min(32767, Math.round(env * tone * 32767.0)));
            pcm.putShort(s);
        }
        try (FileOutputStream fos = new FileOutputStream(out, false)) {
            fos.write(wav);
            fos.flush();
        }
        return out;
    }
}
