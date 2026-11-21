package com.froriep.armoryaudio;

import android.app.Activity;
import android.os.Bundle;

/** Headless launcher trampoline that ensures ArmoryAudioService is running and exits immediately. */
public class LauncherActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        ArmoryAudioService.start(this);
        finish();
    }
}
