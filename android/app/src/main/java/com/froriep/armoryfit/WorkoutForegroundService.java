package com.froriep.armoryfit;

import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Keeps the app process, the Bluetooth GATT links and the CPU alive while a workout records, so
 * locking the phone or switching apps never drops the Polar H10 / AirBike. Type connectedDevice.
 */
public class WorkoutForegroundService extends Service {
    private PowerManager.WakeLock wakeLock;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String title = intent != null && intent.getStringExtra("title") != null ? intent.getStringExtra("title") : "Armory Fit";
        String text = intent != null && intent.getStringExtra("text") != null ? intent.getStringExtra("text") : "Workout active";
        WorkoutNotif.ensureChannel(this);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(WorkoutNotif.ID, WorkoutNotif.build(this, title, text), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(WorkoutNotif.ID, WorkoutNotif.build(this, title, text));
        }
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "armoryfit:workout");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire(4L * 60 * 60 * 1000); // safety net: 4 h max
        }
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
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
