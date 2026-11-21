package com.froriep.armoryaudio;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Starts the headless house-audio HTTP foreground service when Fire TV Cube boots. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (context != null) {
            ArmoryAudioService.start(context);
        }
    }
}
