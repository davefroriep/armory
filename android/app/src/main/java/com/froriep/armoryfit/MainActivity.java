package com.froriep.armoryfit;

import android.Manifest;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(WorkoutServicePlugin.class);
        super.onCreate(savedInstanceState);
        SharedPreferences prefs = getSharedPreferences("armory_prefs", MODE_PRIVATE);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !prefs.getBoolean("notif_prompted", false)) {
            prefs.edit().putBoolean("notif_prompted", true).apply();
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1001);
        }
    }

    @Override
    public void onStart() {
        super.onStart();
        WorkoutServicePlugin.setAppVisible(this, true);
    }

    @Override
    public void onStop() {
        WorkoutServicePlugin.setAppVisible(this, false);
        super.onStop();
    }
}
