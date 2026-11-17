package com.froriep.armoryfit;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;

/**
 * Shared notification builder for both the baseline workout foreground service (smooth OS
 * chronometer clock) and the top-priority live rest-timer card (live OS countdown + tray actions).
 */
final class WorkoutNotif {
    static final String CHANNEL = "workout";
    static final String REST_CHANNEL = "rest_timer";
    static final int ID = 4242;

    private WorkoutNotif() {}

    static void ensureChannel(Context c) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = c.getSystemService(NotificationManager.class);
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Active workout", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Shown while a workout is recording so Bluetooth stays connected with the screen off");
            nm.createNotificationChannel(ch);

            NotificationChannel rest = new NotificationChannel(REST_CHANNEL, "Rest timer", NotificationManager.IMPORTANCE_HIGH);
            rest.setDescription("Keeps your active rest timer pinned at the top of your notification tray");
            rest.setSound(null, null);
            rest.enableVibration(false);
            rest.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            nm.createNotificationChannel(rest);
        }
    }

    private static PendingIntent openAppIntent(Context c) {
        Intent open = new Intent(c, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(c, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private static PendingIntent serviceActionIntent(Context c, String action, int deltaSec, int reqCode) {
        Intent i = new Intent(c, WorkoutForegroundService.class).setAction(action);
        if (deltaSec != 0) i.putExtra("deltaSec", deltaSec);
        return PendingIntent.getService(c, reqCode, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static Notification build(Context c, String title, String text, long startEpochMs) {
        NotificationCompat.Builder b = new NotificationCompat.Builder(c, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_armory)
                .setContentTitle(title)
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_WORKOUT)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setContentIntent(openAppIntent(c));
        if (startEpochMs > 0L) {
            b.setWhen(startEpochMs).setShowWhen(true).setUsesChronometer(true);
            if (Build.VERSION.SDK_INT >= 24) b.setChronometerCountDown(false);
        }
        return b.build();
    }

    private static String bpmPrefix(String workoutText) {
        if (workoutText == null || !workoutText.contains("BPM")) return null;
        int sep = workoutText.indexOf(" · ");
        return sep > 0 ? workoutText.substring(0, sep) : workoutText;
    }

    static Notification buildRest(Context c, String label, String workoutText, long endEpochMs, long remainingMs, boolean fired, boolean alertPop) {
        String title = fired
                ? ("GO! Rest complete" + (label != null && !label.isEmpty() ? " · " + label : ""))
                : ("Rest" + (label != null && !label.isEmpty() ? " · " + label : " timer"));
        String bpm = bpmPrefix(workoutText);
        String baseText = fired ? "Next set ready" : "Resting · live timer";
        String text = bpm != null ? (bpm + " · " + (fired ? "Next set ready" : "Resting")) : baseText;

        NotificationCompat.Builder b = new NotificationCompat.Builder(c, REST_CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_armory)
                .setContentTitle(title)
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(!alertPop)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_STOPWATCH)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setColor(fired ? 0xFFEF4444 : 0xFF16A34A)
                .setContentIntent(openAppIntent(c))
                .addAction(0, "−15s", serviceActionIntent(c, WorkoutForegroundService.ACTION_REST_ADJUST, -15, 101))
                .addAction(0, "+15s", serviceActionIntent(c, WorkoutForegroundService.ACTION_REST_ADJUST, 15, 102))
                .addAction(0, "Skip", serviceActionIntent(c, WorkoutForegroundService.ACTION_REST_SKIP, 0, 103));

        if (!fired && endEpochMs > System.currentTimeMillis() && remainingMs > 0L) {
            b.setWhen(endEpochMs).setShowWhen(true).setUsesChronometer(true);
            if (Build.VERSION.SDK_INT >= 24) b.setChronometerCountDown(true);
        } else if (fired) {
            b.setWhen(System.currentTimeMillis()).setShowWhen(true).setUsesChronometer(true);
            if (Build.VERSION.SDK_INT >= 24) b.setChronometerCountDown(false);
        }
        return b.build();
    }
}
