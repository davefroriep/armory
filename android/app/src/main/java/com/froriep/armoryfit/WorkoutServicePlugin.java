package com.froriep.armoryfit;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;

import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/** JS bridge: WorkoutService.start / update / stop. */
@CapacitorPlugin(name = "WorkoutService")
public class WorkoutServicePlugin extends Plugin {

    @PluginMethod
    public void start(PluginCall call) {
        Context c = getContext();
        Intent i = new Intent(c, WorkoutForegroundService.class)
                .putExtra("title", call.getString("title", "Armory Fit"))
                .putExtra("text", call.getString("text", "Workout active"));
        ContextCompat.startForegroundService(c, i);
        call.resolve();
    }

    @SuppressLint("MissingPermission")
    @PluginMethod
    public void update(PluginCall call) {
        Context c = getContext();
        try {
            WorkoutNotif.ensureChannel(c);
            NotificationManagerCompat.from(c).notify(
                    WorkoutNotif.ID,
                    WorkoutNotif.build(c, call.getString("title", "Armory Fit"), call.getString("text", "")));
        } catch (SecurityException ignored) {
            // POST_NOTIFICATIONS not granted: the service still runs, just without live text.
        }
        call.resolve();
    }

    @PluginMethod
    public void stop(PluginCall call) {
        getContext().stopService(new Intent(getContext(), WorkoutForegroundService.class));
        call.resolve();
    }
}
