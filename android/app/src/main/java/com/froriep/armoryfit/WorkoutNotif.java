package com.froriep.armoryfit;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;

/** Shared notification builder for the foreground service and the live text updates. */
final class WorkoutNotif {
    static final String CHANNEL = "workout";
    static final int ID = 4242;

    private WorkoutNotif() {}

    static void ensureChannel(Context c) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Active workout", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Shown while a workout is recording so Bluetooth stays connected with the screen off");
            c.getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    static Notification build(Context c, String title, String text) {
        Intent open = new Intent(c, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(c, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new NotificationCompat.Builder(c, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setContentTitle(title)
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_WORKOUT)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setContentIntent(pi)
                .build();
    }
}
