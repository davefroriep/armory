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
import android.util.Base64;

import androidx.activity.result.ActivityResult;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayOutputStream;

/** JS bridge: WorkoutService.start / update / stop + camera/DCIM video linking. */
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
}
