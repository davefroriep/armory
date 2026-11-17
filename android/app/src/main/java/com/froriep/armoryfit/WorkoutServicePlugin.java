package com.froriep.armoryfit;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.media.MediaMetadataRetriever;
import android.net.Uri;
import android.os.Build;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.provider.Settings;
import android.util.Base64;

import androidx.activity.result.ActivityResult;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayOutputStream;

/** JS bridge: WorkoutService lifecycle, rest-timer tray/overlay, chimes + camera/DCIM video linking. */
@CapacitorPlugin(name = "WorkoutService")
public class WorkoutServicePlugin extends Plugin {
    private static volatile WorkoutServicePlugin instance;

    @Override
    public void load() {
        instance = this;
    }

    static void setAppVisible(Context c, boolean visible) {
        WorkoutForegroundService.appVisible = visible;
        if (WorkoutForegroundService.running) {
            Intent i = new Intent(c, WorkoutForegroundService.class)
                    .setAction(WorkoutForegroundService.ACTION_APP_VISIBILITY);
            try {
                c.startService(i);
            } catch (Exception ignored) {
            }
        }
    }

    static void emitRestSync(String action, long remainingMs, int totalSec) {
        WorkoutServicePlugin p = instance;
        if (p == null) return;
        JSObject ret = new JSObject();
        ret.put("action", action);
        ret.put("remainingMs", remainingMs);
        ret.put("totalSec", totalSec);
        p.notifyListeners("restSync", ret);
    }

    @PluginMethod
    public void start(PluginCall call) {
        Context c = getContext();
        String wavB64 = call.getString("wavBase64", "");
        if (wavB64 != null && !wavB64.isEmpty()) {
            WorkoutChime.cacheWav(c, wavB64);
        }
        long startMs = call.getDouble("startMs", 0.0).longValue();
        Intent i = new Intent(c, WorkoutForegroundService.class)
                .setAction(WorkoutForegroundService.ACTION_START_WORKOUT)
                .putExtra("title", call.getString("title", "Armory Fit"))
                .putExtra("text", call.getString("text", "Workout active"))
                .putExtra("startMs", startMs);
        ContextCompat.startForegroundService(c, i);
        call.resolve();
    }

    @PluginMethod
    public void update(PluginCall call) {
        Context c = getContext();
        long startMs = call.getDouble("startMs", 0.0).longValue();
        Intent i = new Intent(c, WorkoutForegroundService.class)
                .setAction(WorkoutForegroundService.ACTION_UPDATE_WORKOUT)
                .putExtra("title", call.getString("title", "Armory Fit"))
                .putExtra("text", call.getString("text", ""))
                .putExtra("startMs", startMs);
        if (WorkoutForegroundService.running) {
            c.startService(i);
        }
        call.resolve();
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context c = getContext();
        if (WorkoutForegroundService.running) {
            c.startService(new Intent(c, WorkoutForegroundService.class).setAction(WorkoutForegroundService.ACTION_STOP_WORKOUT));
        }
        call.resolve();
    }

    @PluginMethod
    public void setRest(PluginCall call) {
        Context c = getContext();
        String wavB64 = call.getString("wavBase64", "");
        if (wavB64 != null && !wavB64.isEmpty()) {
            WorkoutChime.cacheWav(c, wavB64);
        }
        long remainingMs = call.getDouble("remainingMs", 90_000.0).longValue();
        int totalSec = call.getInt("totalSec", 90);
        String label = call.getString("label", "");
        boolean sound = call.getBoolean("sound", true);
        float vol = call.getDouble("volume", 0.8).floatValue();
        Intent i = new Intent(c, WorkoutForegroundService.class)
                .setAction(WorkoutForegroundService.ACTION_REST_SET)
                .putExtra("remainingMs", remainingMs)
                .putExtra("totalSec", totalSec)
                .putExtra("label", label)
                .putExtra("sound", sound)
                .putExtra("volume", vol);
        ContextCompat.startForegroundService(c, i);
        call.resolve();
    }

    @PluginMethod
    public void clearRest(PluginCall call) {
        Context c = getContext();
        if (WorkoutForegroundService.running) {
            c.startService(new Intent(c, WorkoutForegroundService.class).setAction(WorkoutForegroundService.ACTION_REST_CLEAR));
        }
        call.resolve();
    }

    @PluginMethod
    public void canOverlay(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("granted", RestOverlay.canDraw(getContext()));
        call.resolve(ret);
    }

    @PluginMethod
    public void requestOverlayPermission(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Intent i = new Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getContext().getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
        }
        call.resolve();
    }

    @PluginMethod
    public void openAppSettings(PluginCall call) {
        Intent i = new Intent(
                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getContext().getPackageName()));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(i);
        call.resolve();
    }

    @PluginMethod
    public void openApp(PluginCall call) {
        String pkg = call.getString("pkg", "");
        boolean opened = false;
        if (pkg != null && !pkg.isEmpty()) {
            Intent launch = getContext().getPackageManager().getLaunchIntentForPackage(pkg);
            if (launch != null) {
                launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(launch);
                opened = true;
            }
        }
        JSObject ret = new JSObject();
        ret.put("opened", opened);
        call.resolve(ret);
    }

    @PluginMethod
    public void launchVideoCamera(PluginCall call) {
        Context c = getContext();
        boolean opened = false;
        Intent i = new Intent(MediaStore.INTENT_ACTION_VIDEO_CAMERA);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            c.startActivity(i);
            opened = true;
        } catch (Exception ignored) {
            Intent fallback = new Intent(MediaStore.ACTION_VIDEO_CAPTURE);
            fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                c.startActivity(fallback);
                opened = true;
            } catch (Exception ignored2) {
            }
        }
        JSObject ret = new JSObject();
        ret.put("opened", opened);
        call.resolve(ret);
    }

    @PluginMethod
    public void pickDcimVideo(PluginCall call) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("video/*");
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            i.putExtra(
                    DocumentsContract.EXTRA_INITIAL_URI,
                    Uri.parse("content://com.android.externalstorage.documents/document/primary%3ADCIM%2FCamera"));
        }
        startActivityForResult(call, i, "onVideoPicked");
    }

    @ActivityCallback
    private void onVideoPicked(PluginCall call, ActivityResult result) {
        if (call == null) return;
        JSObject ret = new JSObject();
        if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null || result.getData().getData() == null) {
            ret.put("picked", false);
            call.resolve(ret);
            return;
        }
        Uri uri = result.getData().getData();
        Context c = getContext();
        try {
            c.getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } catch (Exception ignored) {
        }
        String fileName = uri.getLastPathSegment();
        long size = 0;
        long fileMs = 0;
        try (Cursor cur = c.getContentResolver().query(uri, null, null, null, null)) {
            if (cur != null && cur.moveToFirst()) {
                int ni = cur.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (ni >= 0 && cur.getString(ni) != null) fileName = cur.getString(ni);
                int si = cur.getColumnIndex(OpenableColumns.SIZE);
                if (si >= 0) size = cur.getLong(si);
                int mi = cur.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED);
                if (mi >= 0) fileMs = cur.getLong(mi);
            }
        } catch (Exception ignored) {
        }
        String mime = c.getContentResolver().getType(uri);
        int durationSec = 0;
        String thumb = "";
        MediaMetadataRetriever mmr = new MediaMetadataRetriever();
        try {
            mmr.setDataSource(c, uri);
            String d = mmr.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION);
            if (d != null) durationSec = (int) Math.round(Long.parseLong(d) / 1000.0);
            Bitmap bmp = mmr.getFrameAtTime(400_000L, MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
            if (bmp == null) bmp = mmr.getFrameAtTime();
            if (bmp != null) {
                int tw = 200;
                int th = Math.max(1, Math.round((float) tw * bmp.getHeight() / Math.max(1, bmp.getWidth())));
                Bitmap scaled = Bitmap.createScaledBitmap(bmp, tw, th, true);
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                scaled.compress(Bitmap.CompressFormat.JPEG, 60, baos);
                thumb = "data:image/jpeg;base64," + Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP);
                if (scaled != bmp) scaled.recycle();
                bmp.recycle();
            }
        } catch (Exception ignored) {
        } finally {
            try {
                mmr.release();
            } catch (Exception ignored) {
            }
        }
        ret.put("picked", true);
        ret.put("uri", uri.toString());
        ret.put("fileName", fileName != null ? fileName : "");
        ret.put("bytes", size);
        if (fileMs > 0) ret.put("fileMs", fileMs);
        ret.put("mime", mime != null ? mime : "video/mp4");
        ret.put("durationSec", durationSec);
        ret.put("thumb", thumb);
        call.resolve(ret);
    }

    @PluginMethod
    public void openVideoUri(PluginCall call) {
        String u = call.getString("uri", "");
        boolean opened = false;
        if (u != null && !u.isEmpty()) {
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(Uri.parse(u), "video/*");
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                getContext().startActivity(i);
                opened = true;
            } catch (Exception ignored) {
            }
        }
        JSObject ret = new JSObject();
        ret.put("opened", opened);
        call.resolve(ret);
    }

    /** Plays a short WAV alert with USAGE_MEDIA + transient ducking and abandons AudioFocus right after. */
    @PluginMethod
    public void playChime(PluginCall call) {
        String wavB64 = call.getString("wavBase64", "");
        float vol = call.getDouble("volume", 0.8).floatValue();
        if (wavB64 == null || wavB64.isEmpty()) {
            WorkoutChime.playCached(getContext(), vol);
        } else {
            WorkoutChime.playBase64(getContext(), wavB64, vol);
        }
        call.resolve();
    }

    /** Stops foreground workout service, removes the app task from Recents, and force-closes the process. */
    @PluginMethod
    public void forceClose(PluginCall call) {
        Activity act = getActivity();
        call.resolve();
        if (act == null) return;
        act.runOnUiThread(() -> {
            try {
                if (WorkoutForegroundService.running) {
                    act.stopService(new Intent(act, WorkoutForegroundService.class));
                }
            } catch (Exception ignored) {
            }
            act.finishAffinity();
            act.finishAndRemoveTask();
            act.getWindow().getDecorView().postDelayed(() ->
                    android.os.Process.killProcess(android.os.Process.myPid()), 120);
        });
    }
}
