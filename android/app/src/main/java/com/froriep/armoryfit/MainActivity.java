package com.froriep.armoryfit;

import android.Manifest;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(WorkoutServicePlugin.class);
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
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
