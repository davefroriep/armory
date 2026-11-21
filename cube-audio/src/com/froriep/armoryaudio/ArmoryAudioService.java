package com.froriep.armoryaudio;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Headless Fire TV Cube audio-mixer service listening on HTTP port 8767.
 * Supports three mixing policies over active Fire OS apps (Spotify, YouTube, Netflix, Prime):
 *   - overlay: plays via AudioFlinger without requesting AudioFocus
 *   - duck: requests AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK during playback
 *   - takeover: requests AUDIOFOCUS_GAIN_TRANSIENT to pause background media, then resumes it
 */
public class ArmoryAudioService extends Service {
    public static final int LISTEN_PORT = 8767;
    private static final int NOTIF_ID = 1042;
    private static final String CHANNEL_ID = "armory_audio_bg";
    private static final String APP_VERSION = "1.0";
    private static final int MAX_BODY_BYTES = 12 * 1024 * 1024;
    private static final int SOCKET_TIMEOUT_MS = 15000;

    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicLong playGen = new AtomicLong(0);
    private final AtomicLong playCount = new AtomicLong(0);
    private final AtomicBoolean playing = new AtomicBoolean(false);
    private final AtomicReference<String> activeMode = new AtomicReference<>("");
    private final AtomicReference<String> lastError = new AtomicReference<>("");
    private final Object playbackLock = new Object();

    private ServerSocket serverSocket;
    private Thread acceptThread;
    private ExecutorService httpPool;
    private ExecutorService playExecutor;
    private AudioTrack activeTrack;
    private MediaPlayer activePlayer;
    private FocusHandle activeFocus;

    /** Helper to launch this foreground service safely across API levels. */
    public static void start(Context ctx) {
        if (ctx == null) return;
        Intent intent = new Intent(ctx.getApplicationContext(), ArmoryAudioService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            ctx.getApplicationContext().startForegroundService(intent);
        } else {
            ctx.getApplicationContext().startService(intent);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        promoteForeground();
        httpPool = Executors.newCachedThreadPool();
        playExecutor = Executors.newSingleThreadExecutor();
        startHttpServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        promoteForeground();
        startHttpServer();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running.set(false);
        stopCurrentPlayback();
        closeServerSocket();
        if (httpPool != null) httpPool.shutdownNow();
        if (playExecutor != null) playExecutor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void promoteForeground() {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID,
                    "Armory House Audio",
                    NotificationManager.IMPORTANCE_MIN);
            ch.setSound(null, null);
            ch.enableVibration(false);
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.createNotificationChannel(ch);
            }
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        Notification notif = builder
                .setContentTitle("Armory House Audio")
                .setContentText("Audio mixer listening on :" + LISTEN_PORT)
                .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
                .setOngoing(true)
                .build();
        startForeground(NOTIF_ID, notif);
    }

    private synchronized void startHttpServer() {
        if (running.get() && serverSocket != null && !serverSocket.isClosed()) {
            return;
        }
        running.set(true);
        acceptThread = new Thread(new Runnable() {
            @Override
            public void run() {
                runAcceptLoop();
            }
        }, "ArmoryAudioHttpAccept");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    private void runAcceptLoop() {
        try {
            ServerSocket ss = new ServerSocket();
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress("0.0.0.0", LISTEN_PORT));
            synchronized (this) {
                serverSocket = ss;
            }
            while (running.get() && !ss.isClosed()) {
                final Socket client = ss.accept();
                if (httpPool != null && !httpPool.isShutdown()) {
                    httpPool.execute(new Runnable() {
                        @Override
                        public void run() {
                            handleClient(client);
                        }
                    });
                } else {
                    closeQuietly(client);
                }
            }
        } catch (IOException e) {
            if (running.get()) {
                lastError.set("HTTP server bind/accept error: " + e.getMessage());
            }
        }
    }

    private void closeServerSocket() {
        ServerSocket ss;
        synchronized (this) {
            ss = serverSocket;
            serverSocket = null;
        }
        if (ss != null) {
            try {
                ss.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void handleClient(Socket sock) {
        try {
            sock.setSoTimeout(SOCKET_TIMEOUT_MS);
            BufferedInputStream in = new BufferedInputStream(sock.getInputStream());
            OutputStream out = sock.getOutputStream();

            String headerText = readHeaderBlock(in);
            if (headerText == null || headerText.isEmpty()) {
                return;
            }
            String[] lines = headerText.split("\r\n");
            if (lines.length == 0) {
                writeJson(out, 400, "{\"ok\":false,\"error\":\"Empty request\"}");
                return;
            }
            String[] reqParts = lines[0].split(" ");
            if (reqParts.length < 2) {
                writeJson(out, 400, "{\"ok\":false,\"error\":\"Malformed request line\"}");
                return;
            }
            String method = reqParts[0].trim().toUpperCase(Locale.US);
            String target = reqParts[1].trim();

            int contentLength = 0;
            boolean expectContinue = false;
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon <= 0) continue;
                String name = lines[i].substring(0, colon).trim().toLowerCase(Locale.US);
                String val = lines[i].substring(colon + 1).trim();
                if ("content-length".equals(name)) {
                    try {
                        contentLength = Integer.parseInt(val);
                    } catch (NumberFormatException e) {
                        writeJson(out, 400, "{\"ok\":false,\"error\":\"Invalid Content-Length\"}");
                        return;
                    }
                } else if ("expect".equals(name) && val.toLowerCase(Locale.US).contains("100-continue")) {
                    expectContinue = true;
                }
            }

            if (contentLength < 0 || contentLength > MAX_BODY_BYTES) {
                writeJson(out, 413, "{\"ok\":false,\"error\":\"Payload exceeds 12 MB limit\"}");
                return;
            }
            if (expectContinue) {
                out.write("HTTP/1.1 100 Continue\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
                out.flush();
            }

            byte[] body = contentLength > 0 ? readExact(in, contentLength) : new byte[0];

            String path = target;
            Map<String, String> query = new HashMap<>();
            int qIdx = target.indexOf('?');
            if (qIdx >= 0) {
                path = target.substring(0, qIdx);
                parseQuery(target.substring(qIdx + 1), query);
            }

            if ("OPTIONS".equals(method)) {
                writeOptions(out);
                return;
            }
            if ("GET".equals(method) && "/health".equals(path)) {
                writeJson(out, 200, buildHealthJson());
                return;
            }
            if ("POST".equals(method) && "/stop".equals(path)) {
                stopCurrentPlayback();
                writeJson(out, 200, "{\"ok\":true,\"stopped\":true}");
                return;
            }
            if ("POST".equals(method) && "/play".equals(path)) {
                handlePlayRequest(out, query, body);
                return;
            }
            writeJson(out, 404, "{\"ok\":false,\"error\":\"Not found\"}");
        } catch (Exception e) {
            lastError.set("HTTP client error: " + e.getMessage());
            try {
                writeJson(sock.getOutputStream(), 500, "{\"ok\":false,\"error\":" + jsonString(e.getMessage()) + "}");
            } catch (IOException ignored) {
            }
        } finally {
            closeQuietly(sock);
        }
    }

    private void handlePlayRequest(OutputStream out, Map<String, String> q, final byte[] body) throws IOException {
        final String mode = normalizeMode(q.get("mode"));
        final String usage = normalizeUsage(q.get("usage"));
        final String content = normalizeContent(q.get("content"), usage);
        final float gain = clampFloat(parseFloatOr(q.get("gain"), 1.0f), 0.05f, 1.0f);
        final int maxSec = clampInt(parseIntOr(q.get("maxSec"), 30), 1, 60);
        final boolean sync = "1".equals(q.get("sync")) || "true".equalsIgnoreCase(q.get("sync"));
        final String url = q.get("url") != null ? q.get("url").trim() : "";

        if (url.isEmpty() && (body == null || body.length == 0)) {
            writeJson(out, 400, "{\"ok\":false,\"error\":\"Provide audio body bytes or ?url=\"}");
            return;
        }

        final long myGen = playGen.incrementAndGet();
        stopActiveDrivers();

        if (sync) {
            PlayResult res = executePlayback(myGen, mode, usage, content, gain, maxSec, url, body);
            if (!res.ok) {
                writeJson(out, 500, "{\"ok\":false,\"error\":" + jsonString(res.error) + "}");
                return;
            }
            writeJson(out, 200, String.format(
                    Locale.US,
                    "{\"ok\":true,\"played\":true,\"mode\":%s,\"usage\":%s,\"durationMs\":%d,\"gen\":%d}",
                    jsonString(mode), jsonString(usage), res.durationMs, myGen));
        } else {
            if (playExecutor != null && !playExecutor.isShutdown()) {
                playExecutor.execute(new Runnable() {
                    @Override
                    public void run() {
                        executePlayback(myGen, mode, usage, content, gain, maxSec, url, body);
                    }
                });
            }
            writeJson(out, 200, String.format(
                    Locale.US,
                    "{\"ok\":true,\"queued\":true,\"mode\":%s,\"usage\":%s,\"gen\":%d}",
                    jsonString(mode), jsonString(usage), myGen));
        }
    }

    private PlayResult executePlayback(
            long myGen,
            String mode,
            String usage,
            String content,
            float gain,
            int maxSec,
            String url,
            byte[] body) {
        if (playGen.get() != myGen) {
            return PlayResult.cancelled();
        }
        long startMs = System.currentTimeMillis();
        long deadlineMs = startMs + (maxSec * 1000L);
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        PowerManager.WakeLock wl = acquirePlayWakeLock((maxSec + 5) * 1000L);
        FocusHandle focus = null;

        playing.set(true);
        activeMode.set(mode);
        try {
            AudioAttributes attrs = buildAudioAttributes(usage, content);
            if (am != null && !"overlay".equals(mode)) {
                int focusType = "takeover".equals(mode)
                        ? AudioManager.AUDIOFOCUS_GAIN_TRANSIENT
                        : AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK;
                focus = requestTransientFocus(am, attrs, focusType);
                synchronized (playbackLock) {
                    activeFocus = focus;
                }
                long settleMs = "takeover".equals(mode) ? 120L : 60L;
                sleepInterruptibly(settleMs);
            }
            if (playGen.get() != myGen) {
                return PlayResult.cancelled();
            }

            WavSpec wav = (url.isEmpty() && body != null) ? parsePcm16Wav(body) : null;
            if (wav != null) {
                playWavWithAudioTrack(myGen, wav, body, attrs, gain, deadlineMs);
            } else {
                playWithMediaPlayer(myGen, url, body, attrs, gain, deadlineMs);
            }
            playCount.incrementAndGet();
            lastError.set("");
            return PlayResult.success(Math.max(0L, System.currentTimeMillis() - startMs));
        } catch (Exception e) {
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            lastError.set(msg);
            return PlayResult.fail(msg);
        } finally {
            if (focus != null && am != null) {
                abandonFocus(am, focus);
                synchronized (playbackLock) {
                    if (activeFocus == focus) {
                        activeFocus = null;
                    }
                }
            }
            if (playGen.get() == myGen) {
                playing.set(false);
                activeMode.set("");
            }
            releaseWakeLockQuietly(wl);
        }
    }

    private void playWavWithAudioTrack(
            long myGen,
            WavSpec wav,
            byte[] rawWav,
            AudioAttributes attrs,
            float gain,
            long deadlineMs) {
        byte[] pcm = new byte[wav.dataSize];
        System.arraycopy(rawWav, wav.dataOffset, pcm, 0, wav.dataSize);
        applyLinearEdgeFade16(pcm, wav.sampleRate, wav.channels, 6);

        int channelMask = (wav.channels == 1)
                ? AudioFormat.CHANNEL_OUT_MONO
                : AudioFormat.CHANNEL_OUT_STEREO;
        int minBuf = AudioTrack.getMinBufferSize(wav.sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT);
        if (minBuf <= 0) {
            minBuf = wav.sampleRate * wav.channels * 2 / 5;
        }
        int bufSize = Math.max(minBuf * 2, (wav.sampleRate * wav.channels * 2) / 4);
        int frameBytes = wav.channels * 2;

        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(attrs)
                .setAudioFormat(new AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(wav.sampleRate)
                        .setChannelMask(channelMask)
                        .build())
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build();

        synchronized (playbackLock) {
            if (playGen.get() != myGen) {
                track.release();
                return;
            }
            activeTrack = track;
        }

        try {
            track.setVolume(gain);
            track.play();
            long playStartMs = System.currentTimeMillis();
            int pos = 0;
            int chunkSize = 16384;
            while (pos < pcm.length && playGen.get() == myGen && System.currentTimeMillis() < deadlineMs) {
                int len = Math.min(chunkSize, pcm.length - pos);
                len = (len / frameBytes) * frameBytes;
                if (len <= 0) break;
                int wrote = track.write(pcm, pos, len);
                if (wrote < 0) {
                    throw new IllegalStateException("AudioTrack write failed: " + wrote);
                }
                if (wrote == 0) {
                    sleepInterruptibly(10L);
                    continue;
                }
                pos += wrote;
            }
            if (playGen.get() == myGen) {
                long totalFrames = pcm.length / (long) frameBytes;
                long expectedMs = (totalFrames * 1000L) / Math.max(1, wav.sampleRate);
                long drainDeadline = Math.min(deadlineMs, playStartMs + expectedMs + 160L);
                while (playGen.get() == myGen && System.currentTimeMillis() < drainDeadline) {
                    if (track.getPlaybackHeadPosition() >= totalFrames) {
                        break;
                    }
                    sleepInterruptibly(20L);
                }
            }
        } finally {
            synchronized (playbackLock) {
                if (activeTrack == track) {
                    activeTrack = null;
                }
            }
            try {
                track.pause();
                track.flush();
                track.stop();
            } catch (Exception ignored) {
            }
            try {
                track.release();
            } catch (Exception ignored) {
            }
        }
    }

    private void playWithMediaPlayer(
            long myGen,
            String url,
            byte[] body,
            AudioAttributes attrs,
            float gain,
            long deadlineMs) throws IOException {
        File tmpFile = null;
        final AtomicBoolean done = new AtomicBoolean(false);
        MediaPlayer mp = new MediaPlayer();
        synchronized (playbackLock) {
            if (playGen.get() != myGen) {
                mp.release();
                return;
            }
            activePlayer = mp;
        }
        try {
            mp.setAudioAttributes(attrs);
            if (!url.isEmpty()) {
                mp.setDataSource(url);
            } else {
                tmpFile = new File(getCacheDir(), "armory_play_" + myGen + ".bin");
                try (FileOutputStream fos = new FileOutputStream(tmpFile)) {
                    fos.write(body);
                }
                mp.setDataSource(tmpFile.getAbsolutePath());
            }
            mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer player) {
                    done.set(true);
                }
            });
            mp.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer player, int what, int extra) {
                    lastError.set("MediaPlayer error " + what + "/" + extra);
                    done.set(true);
                    return true;
                }
            });
            mp.prepare();
            if (playGen.get() != myGen) {
                return;
            }
            mp.setVolume(gain, gain);
            mp.start();
            while (!done.get() && playGen.get() == myGen && System.currentTimeMillis() < deadlineMs) {
                sleepInterruptibly(25L);
            }
        } finally {
            synchronized (playbackLock) {
                if (activePlayer == mp) {
                    activePlayer = null;
                }
            }
            try {
                if (mp.isPlaying()) {
                    mp.stop();
                }
            } catch (Exception ignored) {
            }
            try {
                mp.release();
            } catch (Exception ignored) {
            }
            if (tmpFile != null) {
                //noinspection ResultOfMethodCallIgnored
                tmpFile.delete();
            }
        }
    }

    private void stopCurrentPlayback() {
        playGen.incrementAndGet();
        stopActiveDrivers();
        playing.set(false);
        activeMode.set("");
    }

    private void stopActiveDrivers() {
        AudioTrack track;
        MediaPlayer mp;
        FocusHandle focus;
        synchronized (playbackLock) {
            track = activeTrack;
            activeTrack = null;
            mp = activePlayer;
            activePlayer = null;
            focus = activeFocus;
            activeFocus = null;
        }
        if (track != null) {
            try {
                track.pause();
                track.flush();
                track.stop();
            } catch (Exception ignored) {
            }
        }
        if (mp != null) {
            try {
                if (mp.isPlaying()) {
                    mp.stop();
                }
            } catch (Exception ignored) {
            }
        }
        if (focus != null) {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (am != null) {
                abandonFocus(am, focus);
            }
        }
    }

    private static AudioAttributes buildAudioAttributes(String usage, String content) {
        int u = AudioAttributes.USAGE_MEDIA;
        if ("assist".equals(usage)) {
            u = AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE;
        } else if ("sonification".equals(usage)) {
            u = AudioAttributes.USAGE_ASSISTANCE_SONIFICATION;
        } else if ("alarm".equals(usage)) {
            u = AudioAttributes.USAGE_ALARM;
        }
        int c = AudioAttributes.CONTENT_TYPE_MUSIC;
        if ("speech".equals(content)) {
            c = AudioAttributes.CONTENT_TYPE_SPEECH;
        } else if ("sonification".equals(content)) {
            c = AudioAttributes.CONTENT_TYPE_SONIFICATION;
        }
        return new AudioAttributes.Builder()
                .setUsage(u)
                .setContentType(c)
                .build();
    }

    private static FocusHandle requestTransientFocus(AudioManager am, AudioAttributes attrs, int focusType) {
        AudioManager.OnAudioFocusChangeListener listener = new AudioManager.OnAudioFocusChangeListener() {
            @Override
            public void onAudioFocusChange(int focusChange) {
                // Intentionally passive; focus is abandoned immediately when clip ends.
            }
        };
        if (Build.VERSION.SDK_INT >= 26) {
            AudioFocusRequest req = new AudioFocusRequest.Builder(focusType)
                    .setAudioAttributes(attrs)
                    .setAcceptsDelayedFocusGain(false)
                    .setOnAudioFocusChangeListener(listener)
                    .build();
            am.requestAudioFocus(req);
            return new FocusHandle(req, listener);
        } else {
            @SuppressWarnings("deprecation")
            int ignored = am.requestAudioFocus(listener, AudioManager.STREAM_MUSIC, focusType);
            return new FocusHandle(null, listener);
        }
    }

    private static void abandonFocus(AudioManager am, FocusHandle handle) {
        if (handle == null) return;
        try {
            if (Build.VERSION.SDK_INT >= 26 && handle.request != null) {
                am.abandonAudioFocusRequest(handle.request);
            } else if (handle.listener != null) {
                @SuppressWarnings("deprecation")
                int ignored = am.abandonAudioFocus(handle.listener);
            }
        } catch (Exception ignored) {
        }
    }

    private PowerManager.WakeLock acquirePlayWakeLock(long timeoutMs) {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) return null;
            PowerManager.WakeLock wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ArmoryAudio:Playback");
            wl.setReferenceCounted(false);
            wl.acquire(Math.max(1000L, timeoutMs));
            return wl;
        } catch (Exception e) {
            return null;
        }
    }

    private static void releaseWakeLockQuietly(PowerManager.WakeLock wl) {
        if (wl == null) return;
        try {
            if (wl.isHeld()) wl.release();
        } catch (Exception ignored) {
        }
    }

    private String buildHealthJson() {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        boolean musicActive = false;
        int vol = -1;
        int maxVol = -1;
        if (am != null) {
            try {
                musicActive = am.isMusicActive();
                vol = am.getStreamVolume(AudioManager.STREAM_MUSIC);
                maxVol = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            } catch (Exception ignored) {
            }
        }
        return String.format(
                Locale.US,
                "{\"ok\":true,\"pkg\":%s,\"version\":%s,\"port\":%d,\"playing\":%s,\"activeMode\":%s,"
                        + "\"musicActive\":%s,\"streamVol\":%d,\"streamMaxVol\":%d,\"playCount\":%d,\"lastError\":%s}",
                jsonString(getPackageName()),
                jsonString(APP_VERSION),
                LISTEN_PORT,
                playing.get() ? "true" : "false",
                jsonString(activeMode.get()),
                musicActive ? "true" : "false",
                vol,
                maxVol,
                playCount.get(),
                jsonString(lastError.get()));
    }

    private static WavSpec parsePcm16Wav(byte[] data) {
        if (data == null || data.length < 44) return null;
        if (data[0] != 'R' || data[1] != 'I' || data[2] != 'F' || data[3] != 'F') return null;
        if (data[8] != 'W' || data[9] != 'A' || data[10] != 'V' || data[11] != 'E') return null;

        ByteBuffer bb = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        int audioFormat = -1;
        int channels = 0;
        int sampleRate = 0;
        int bitsPerSample = 0;
        int dataOffset = -1;
        int dataSize = 0;

        int pos = 12;
        while (pos + 8 <= data.length) {
            String chunkId = new String(data, pos, 4, StandardCharsets.US_ASCII);
            long rawSize = bb.getInt(pos + 4) & 0xFFFFFFFFL;
            int chunkBody = pos + 8;
            if (chunkBody > data.length) break;
            int avail = (int) Math.min(rawSize, (long) (data.length - chunkBody));

            if ("fmt ".equals(chunkId) && avail >= 16) {
                audioFormat = bb.getShort(chunkBody) & 0xFFFF;
                channels = bb.getShort(chunkBody + 2) & 0xFFFF;
                sampleRate = bb.getInt(chunkBody + 4);
                bitsPerSample = bb.getShort(chunkBody + 14) & 0xFFFF;
            } else if ("data".equals(chunkId) && avail > 0) {
                dataOffset = chunkBody;
                dataSize = avail;
                break;
            }
            long step = 8L + ((rawSize + 1L) & ~1L);
            if (step <= 8L || pos + step > data.length) break;
            pos += (int) step;
        }

        if (audioFormat != 1 || bitsPerSample != 16 || (channels != 1 && channels != 2)
                || sampleRate < 8000 || sampleRate > 192000 || dataOffset < 0 || dataSize < channels * 2) {
            return null;
        }
        int frameBytes = channels * 2;
        dataSize = (dataSize / frameBytes) * frameBytes;
        return new WavSpec(channels, sampleRate, dataOffset, dataSize);
    }

    private static void applyLinearEdgeFade16(byte[] pcm, int sampleRate, int channels, int fadeMs) {
        int frameBytes = channels * 2;
        int totalFrames = pcm.length / frameBytes;
        int fadeFrames = Math.min(totalFrames / 4, Math.max(0, (sampleRate * fadeMs) / 1000));
        if (fadeFrames <= 1) return;
        ByteBuffer bb = ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN);
        for (int f = 0; f < fadeFrames; f++) {
            float gIn = (float) f / (float) fadeFrames;
            float gOut = (float) (fadeFrames - 1 - f) / (float) fadeFrames;
            int fOut = totalFrames - fadeFrames + f;
            for (int c = 0; c < channels; c++) {
                int idxIn = (f * channels + c) * 2;
                short sIn = bb.getShort(idxIn);
                bb.putShort(idxIn, (short) Math.round(sIn * gIn));

                int idxOut = (fOut * channels + c) * 2;
                short sOut = bb.getShort(idxOut);
                bb.putShort(idxOut, (short) Math.round(sOut * gOut));
            }
        }
    }

    private static String readHeaderBlock(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int prev3 = 0, prev2 = 0, prev1 = 0;
        int b;
        while ((b = in.read()) != -1) {
            buf.write(b);
            if (buf.size() > 32768) {
                throw new IOException("Header too large");
            }
            if (prev3 == '\r' && prev2 == '\n' && prev1 == '\r' && b == '\n') {
                return buf.toString(StandardCharsets.ISO_8859_1.name());
            }
            prev3 = prev2;
            prev2 = prev1;
            prev1 = b;
        }
        return buf.size() > 0 ? buf.toString(StandardCharsets.ISO_8859_1.name()) : null;
    }

    private static byte[] readExact(InputStream in, int length) throws IOException {
        byte[] out = new byte[length];
        int off = 0;
        while (off < length) {
            int r = in.read(out, off, length - off);
            if (r < 0) {
                throw new IOException("Unexpected EOF reading request body");
            }
            off += r;
        }
        return out;
    }

    private static void parseQuery(String rawQuery, Map<String, String> dst) {
        if (rawQuery == null || rawQuery.isEmpty()) return;
        for (String pair : rawQuery.split("&")) {
            if (pair.isEmpty()) continue;
            int eq = pair.indexOf('=');
            String k = eq >= 0 ? pair.substring(0, eq) : pair;
            String v = eq >= 0 ? pair.substring(eq + 1) : "";
            try {
                k = URLDecoder.decode(k, "UTF-8");
                v = URLDecoder.decode(v, "UTF-8");
            } catch (Exception ignored) {
            }
            dst.put(k, v);
        }
    }

    private static void writeOptions(OutputStream out) throws IOException {
        String hdr = "HTTP/1.1 204 No Content\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: Content-Type\r\n"
                + "Connection: close\r\n\r\n";
        out.write(hdr.getBytes(StandardCharsets.US_ASCII));
        out.flush();
    }

    private static void writeJson(OutputStream out, int status, String json) throws IOException {
        byte[] payload = json.getBytes(StandardCharsets.UTF_8);
        String reason = status == 200 ? "OK" : (status == 400 ? "Bad Request" : (status == 404 ? "Not Found"
                : (status == 413 ? "Payload Too Large" : "Internal Server Error")));
        String hdr = "HTTP/1.1 " + status + " " + reason + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + payload.length + "\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n\r\n";
        out.write(hdr.getBytes(StandardCharsets.US_ASCII));
        out.write(payload);
        out.flush();
    }

    private static String jsonString(String val) {
        if (val == null) return "\"\"";
        StringBuilder sb = new StringBuilder(val.length() + 4);
        sb.append('"');
        for (int i = 0; i < val.length(); i++) {
            char ch = val.charAt(i);
            if (ch == '"' || ch == '\\') {
                sb.append('\\').append(ch);
            } else if (ch == '\n') {
                sb.append("\\n");
            } else if (ch == '\r') {
                sb.append("\\r");
            } else if (ch == '\t') {
                sb.append("\\t");
            } else if (ch >= 0x20) {
                sb.append(ch);
            }
        }
        sb.append('"');
        return sb.toString();
    }

    private static String normalizeMode(String raw) {
        String s = raw == null ? "" : raw.trim().toLowerCase(Locale.US);
        if ("duck".equals(s) || "takeover".equals(s)) return s;
        return "overlay";
    }

    private static String normalizeUsage(String raw) {
        String s = raw == null ? "" : raw.trim().toLowerCase(Locale.US);
        if ("assist".equals(s) || "sonification".equals(s) || "alarm".equals(s)) return s;
        return "media";
    }

    private static String normalizeContent(String raw, String usage) {
        String s = raw == null ? "" : raw.trim().toLowerCase(Locale.US);
        if ("music".equals(s) || "speech".equals(s) || "sonification".equals(s)) return s;
        if ("assist".equals(usage)) return "speech";
        if ("sonification".equals(usage)) return "sonification";
        return "music";
    }

    private static float parseFloatOr(String s, float dflt) {
        if (s == null || s.isEmpty()) return dflt;
        try {
            return Float.parseFloat(s);
        } catch (NumberFormatException e) {
            return dflt;
        }
    }

    private static int parseIntOr(String s, int dflt) {
        if (s == null || s.isEmpty()) return dflt;
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return dflt;
        }
    }

    private static float clampFloat(float v, float lo, float hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    private static int clampInt(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    private static void sleepInterruptibly(long ms) {
        if (ms <= 0) return;
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    private static void closeQuietly(Socket s) {
        if (s == null) return;
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }

    private static final class WavSpec {
        final int channels;
        final int sampleRate;
        final int dataOffset;
        final int dataSize;

        WavSpec(int channels, int sampleRate, int dataOffset, int dataSize) {
            this.channels = channels;
            this.sampleRate = sampleRate;
            this.dataOffset = dataOffset;
            this.dataSize = dataSize;
        }
    }

    private static final class FocusHandle {
        final AudioFocusRequest request;
        final AudioManager.OnAudioFocusChangeListener listener;

        FocusHandle(AudioFocusRequest request, AudioManager.OnAudioFocusChangeListener listener) {
            this.request = request;
            this.listener = listener;
        }
    }

    private static final class PlayResult {
        final boolean ok;
        final long durationMs;
        final String error;

        PlayResult(boolean ok, long durationMs, String error) {
            this.ok = ok;
            this.durationMs = durationMs;
            this.error = error;
        }

        static PlayResult success(long durationMs) {
            return new PlayResult(true, durationMs, "");
        }

        static PlayResult cancelled() {
            return new PlayResult(true, 0L, "");
        }

        static PlayResult fail(String error) {
            return new PlayResult(false, 0L, error);
        }
    }
}
