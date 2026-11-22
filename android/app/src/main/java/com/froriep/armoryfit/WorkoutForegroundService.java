package com.froriep.armoryfit;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

/**
 * Keeps BLE, heart rate and the rest timer alive when Armory Fit is minimized or the screen is off.
 * Uses Android's native chronometer for smooth 1s workout/rest tray clocks, schedules an exact
 * chime + heads-up transition on rest completion, and displays RestOverlay when minimized.
 */
public class WorkoutForegroundService extends Service {
    static final String ACTION_START_WORKOUT = "com.froriep.armoryfit.START_WORKOUT";
    static final String ACTION_UPDATE_WORKOUT = "com.froriep.armoryfit.UPDATE_WORKOUT";
    static final String ACTION_STOP_WORKOUT = "com.froriep.armoryfit.STOP_WORKOUT";
    static final String ACTION_REST_SET = "com.froriep.armoryfit.REST_SET";
    static final String ACTION_REST_ADJUST = "com.froriep.armoryfit.REST_ADJUST";
    static final String ACTION_REST_CLEAR = "com.froriep.armoryfit.REST_CLEAR";
    static final String ACTION_REST_SKIP = "com.froriep.armoryfit.REST_SKIP";
    static final String ACTION_APP_VISIBILITY = "com.froriep.armoryfit.APP_VISIBILITY";

    private static final String TAG = "ArmoryWorkoutSvc";
    private static final long STALE_WATCHDOG_MS = 15L * 60L * 1000L;
    private static final long WATCHDOG_POLL_MS = 5L * 60L * 1000L;

    static volatile boolean appVisible = true;
    static volatile boolean running = false;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private PowerManager.WakeLock wakeLock;
    private RestOverlay overlay;

    private boolean workoutActive = false;
    private long lastActivityElapsedMs = 0L;
    private String workoutTitle = "Armory Fit";
    private String workoutText = "Workout active";
    private long workoutStartEpochMs = 0L;

    private boolean restActive = false;
    private long restEndElapsedMs = 0L;
    private long restEndEpochMs = 0L;
    private int restTotalSec = 90;
    private String restLabel = "";
    private boolean restSound = true;
    private float restVolume = 0.8f;
    private boolean restFired = false;

    private final Runnable fireRestRunnable = new Runnable() {
        @Override
        public void run() {
            if (!restActive || restFired) return;
            long rem = restEndElapsedMs - SystemClock.elapsedRealtime();
            if (rem > 40L) {
                handler.postDelayed(this, rem);
                return;
            }
            restFired = true;
            if (restSound) WorkoutChime.playCached(WorkoutForegroundService.this, restVolume);
            WorkoutChime.vibrateRestDone(WorkoutForegroundService.this);
            WorkoutServicePlugin.emitRestSync("fired", rem, restTotalSec);
            notifySafe(WorkoutNotif.buildRest(
                    WorkoutForegroundService.this, restLabel, workoutText, restEndEpochMs, rem, true, true));
            updateOverlay();
        }
    };

    private final Runnable overlayTickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!restActive || appVisible) return;
            updateOverlay();
            handler.postDelayed(this, 1000L);
        }
    };

    private final Runnable watchdogRunnable = new Runnable() {
        @Override
        public void run() {
            if (!workoutActive && !restActive) return;
            long idleMs = SystemClock.elapsedRealtime() - lastActivityElapsedMs;
            if (workoutActive && !restActive && !appVisible && lastActivityElapsedMs > 0L && idleMs > STALE_WATCHDOG_MS) {
                Log.w(TAG, "Stopping abandoned workout service after " + (idleMs / 1000L) + "s idle");
                workoutActive = false;
                stopServiceCleanly();
                return;
            }
            handler.postDelayed(this, WATCHDOG_POLL_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        running = true;
        WorkoutNotif.ensureChannel(this);
        overlay = new RestOverlay(this, new RestOverlay.Actions() {
            @Override
            public void onAdjust(int deltaSec) {
                adjustRest(deltaSec, true);
            }

            @Override
            public void onSkip() {
                clearRest(true);
            }
        });
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : ACTION_START_WORKOUT;
        if (action == null) action = ACTION_START_WORKOUT;

        switch (action) {
            case ACTION_START_WORKOUT: {
                workoutActive = true;
                touchActivity();
                String t = intent != null ? intent.getStringExtra("title") : null;
                String x = intent != null ? intent.getStringExtra("text") : null;
                long sm = intent != null ? intent.getLongExtra("startMs", 0L) : 0L;
                if (t != null) workoutTitle = t;
                if (x != null) workoutText = x;
                if (sm > 0L) workoutStartEpochMs = sm;
                ensureWakeLock();
                promoteForeground();
                scheduleWatchdog();
                break;
            }
            case ACTION_UPDATE_WORKOUT: {
                touchActivity();
                String t = intent != null ? intent.getStringExtra("title") : null;
                String x = intent != null ? intent.getStringExtra("text") : null;
                long sm = intent != null ? intent.getLongExtra("startMs", 0L) : 0L;
                if (t != null) workoutTitle = t;
                if (x != null) workoutText = x;
                if (sm > 0L) workoutStartEpochMs = sm;
                if (restActive) {
                    long rem = restEndElapsedMs - SystemClock.elapsedRealtime();
                    notifySafe(WorkoutNotif.buildRest(this, restLabel, workoutText, restEndEpochMs, rem, restFired, false));
                } else {
                    notifySafe(WorkoutNotif.build(this, workoutTitle, workoutText, workoutStartEpochMs));
                }
                break;
            }
            case ACTION_STOP_WORKOUT: {
                workoutActive = false;
                workoutStartEpochMs = 0L;
                if (!restActive) {
                    stopServiceCleanly();
                }
                break;
            }
            case ACTION_REST_SET: {
                touchActivity();
                long remMs = intent != null ? intent.getLongExtra("remainingMs", 90_000L) : 90_000L;
                restTotalSec = Math.max(5, intent != null ? intent.getIntExtra("totalSec", 90) : 90);
                restLabel = intent != null && intent.getStringExtra("label") != null ? intent.getStringExtra("label") : "";
                restSound = intent == null || intent.getBooleanExtra("sound", true);
                restVolume = intent != null ? intent.getFloatExtra("volume", 0.8f) : 0.8f;
                restActive = true;
                restEndElapsedMs = SystemClock.elapsedRealtime() + remMs;
                restEndEpochMs = System.currentTimeMillis() + remMs;
                restFired = remMs <= 0L;
                ensureWakeLock();
                promoteForeground();
                scheduleWatchdog();
                scheduleRestAlarm(remMs);
                notifySafe(WorkoutNotif.buildRest(this, restLabel, workoutText, restEndEpochMs, remMs, restFired, false));
                syncOverlayLoop();
                break;
            }
            case ACTION_REST_ADJUST: {
                touchActivity();
                int delta = intent != null ? intent.getIntExtra("deltaSec", 0) : 0;
                if (restActive && delta != 0) adjustRest(delta, true);
                break;
            }
            case ACTION_REST_CLEAR: {
                clearRest(false);
                break;
            }
            case ACTION_REST_SKIP: {
                clearRest(true);
                break;
            }
            case ACTION_APP_VISIBILITY: {
                syncOverlayLoop();
                break;
            }
        }
        return START_NOT_STICKY;
    }

    private void scheduleRestAlarm(long remMs) {
        handler.removeCallbacks(fireRestRunnable);
        if (!restFired && remMs > 0L) {
            handler.postDelayed(fireRestRunnable, remMs);
        }
    }

    private void syncOverlayLoop() {
        handler.removeCallbacks(overlayTickRunnable);
        if (restActive && !appVisible) {
            updateOverlay();
            handler.postDelayed(overlayTickRunnable, 1000L);
        } else {
            overlay.hide();
        }
    }

    private void updateOverlay() {
        if (!restActive || appVisible) {
            overlay.hide();
            return;
        }
        overlay.showOrUpdate(restLabel, restEndElapsedMs - SystemClock.elapsedRealtime(), restFired);
    }

    private void adjustRest(int deltaSec, boolean emit) {
        if (!restActive) return;
        restEndElapsedMs += deltaSec * 1000L;
        restEndEpochMs += deltaSec * 1000L;
        restTotalSec = Math.max(5, restTotalSec + deltaSec);
        long rem = restEndElapsedMs - SystemClock.elapsedRealtime();
        restFired = rem <= 0L;
        scheduleRestAlarm(rem);
        notifySafe(WorkoutNotif.buildRest(this, restLabel, workoutText, restEndEpochMs, rem, restFired, false));
        updateOverlay();
        if (emit) WorkoutServicePlugin.emitRestSync("adjust", rem, restTotalSec);
    }

    private void clearRest(boolean emit) {
        restActive = false;
        restEndElapsedMs = 0L;
        restFired = false;
        handler.removeCallbacks(fireRestRunnable);
        handler.removeCallbacks(overlayTickRunnable);
        overlay.hide();
        if (emit) WorkoutServicePlugin.emitRestSync("clear", 0L, 0);
        if (workoutActive) {
            notifySafe(WorkoutNotif.build(this, workoutTitle, workoutText, workoutStartEpochMs));
        } else {
            stopForeground(true);
            stopSelf();
        }
    }

    private void touchActivity() {
        lastActivityElapsedMs = SystemClock.elapsedRealtime();
    }

    private void scheduleWatchdog() {
        handler.removeCallbacks(watchdogRunnable);
        if (workoutActive || restActive) {
            handler.postDelayed(watchdogRunnable, WATCHDOG_POLL_MS);
        }
    }

    private void stopServiceCleanly() {
        handler.removeCallbacks(watchdogRunnable);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
        stopForeground(true);
        stopSelf();
    }

    private void promoteForeground() {
        long rem = restEndElapsedMs - SystemClock.elapsedRealtime();
        android.app.Notification n = restActive
                ? WorkoutNotif.buildRest(this, restLabel, workoutText, restEndEpochMs, rem, restFired, false)
                : WorkoutNotif.build(this, workoutTitle, workoutText, workoutStartEpochMs);
        try {
            boolean hasBt = Build.VERSION.SDK_INT < Build.VERSION_CODES.S
                    || ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                            == PackageManager.PERMISSION_GRANTED;
            if (Build.VERSION.SDK_INT >= 29 && hasBt) {
                startForeground(WorkoutNotif.ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            } else {
                startForeground(WorkoutNotif.ID, n);
            }
        } catch (Exception e) {
            Log.w(TAG, "promoteForeground failed", e);
            notifySafe(n);
            workoutActive = false;
            restActive = false;
            stopSelf();
        }
    }

    @SuppressLint("MissingPermission")
    private void notifySafe(android.app.Notification n) {
        try {
            NotificationManagerCompat.from(this).notify(WorkoutNotif.ID, n);
        } catch (SecurityException ignored) {
        }
    }

    private void ensureWakeLock() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "armoryfit:workout");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(4L * 60 * 60 * 1000);
            }
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        restActive = false;
        workoutActive = false;
        WorkoutMetronome.stop();
        handler.removeCallbacks(fireRestRunnable);
        handler.removeCallbacks(overlayTickRunnable);
        handler.removeCallbacks(watchdogRunnable);
        if (overlay != null) overlay.hide();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
