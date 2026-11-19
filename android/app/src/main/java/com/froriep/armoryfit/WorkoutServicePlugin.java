package com.froriep.armoryfit;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.ActivityManager;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.media.MediaMetadataRetriever;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.provider.Settings;
import android.util.Base64;
import android.util.Size;
import android.view.WindowManager;

import androidx.activity.result.ActivityResult;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/** JS bridge: WorkoutService lifecycle, rest-timer tray/overlay, wallboard HTTP/SSE, voice recognition + video linking. */
@CapacitorPlugin(
        name = "WorkoutService",
        permissions = {
                @Permission(strings = { Manifest.permission.RECORD_AUDIO }, alias = "microphone"),
                @Permission(strings = { Manifest.permission.READ_MEDIA_VIDEO }, alias = "videos"),
                @Permission(strings = { Manifest.permission.READ_EXTERNAL_STORAGE }, alias = "videosLegacy")
        }
)
public class WorkoutServicePlugin extends Plugin {
    private static volatile WorkoutServicePlugin instance;
    private static volatile boolean keepScreenOnWanted = false;
    private volatile File pendingCaptureFile;

    @Override
    public void load() {
        instance = this;
        WorkoutMetronome.warm(getContext());
    }

    static void setAppVisible(Context c, boolean visible) {
        WorkoutForegroundService.appVisible = visible;
        VoiceRecognitionManager.onAppVisibilityChanged(c, visible);
        WorkoutServicePlugin p = instance;
        if (visible && p != null && keepScreenOnWanted) {
            Activity act = p.getActivity();
            if (act != null) {
                act.runOnUiThread(() -> act.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
            }
        }
        if (WorkoutForegroundService.running) {
            Intent i = new Intent(c, WorkoutForegroundService.class)
                    .setAction(WorkoutForegroundService.ACTION_APP_VISIBILITY);
            try {
                c.startService(i);
            } catch (Exception ignored) {
            }
        }
    }

    static void deliverVoiceEvent(String transcript, boolean isFinal, String source, String state, String error) {
        deliverVoiceEvent(transcript, null, isFinal, source, state, error);
    }

    static void deliverVoiceEvent(String transcript, List<String> candidates, boolean isFinal, String source, String state, String error) {
        WorkoutServicePlugin p = instance;
        if (p == null) return;
        JSObject ret = new JSObject();
        ret.put("transcript", transcript != null ? transcript : "");
        if (candidates != null && !candidates.isEmpty()) {
            JSArray arr = new JSArray();
            for (String c : candidates) {
                if (c != null && !c.isEmpty()) arr.put(c);
            }
            if (arr.length() > 0) ret.put("candidates", arr);
        }
        ret.put("isFinal", isFinal);
        ret.put("source", source != null ? source : "mic");
        ret.put("state", state != null ? state : "listening");
        if (error != null) ret.put("error", error);
        p.notifyListeners("voiceResult", ret);
    }

    static void deliverExternalVoice(String transcript, String source) {
        if (transcript == null || transcript.trim().isEmpty()) return;
        deliverVoiceEvent(transcript.trim(), true, source != null ? source : "lan", "lan", null);
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
    public void setKeepScreenOn(PluginCall call) {
        boolean on = call.getBoolean("on", false);
        keepScreenOnWanted = on;
        Activity act = getActivity();
        if (act != null) {
            act.runOnUiThread(() -> {
                if (on) act.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else act.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }
        call.resolve();
    }

    @PluginMethod
    public void setMetronome(PluginCall call) {
        boolean play = call.getBoolean("play", false);
        double bpm = call.getDouble("bpm", 0.0);
        float vol = call.getDouble("volume", 0.8).floatValue();
        WorkoutMetronome.set(getContext(), play, bpm, vol);
        call.resolve();
    }

    @PluginMethod
    public void startWallboard(PluginCall call) {
        int port = call.getInt("port", 8765);
        String html = call.getString("html", "");
        call.resolve(WallboardServer.start(getContext(), port, html));
    }

    @PluginMethod
    public void stopWallboard(PluginCall call) {
        WallboardServer.stop();
        call.resolve();
    }

    @PluginMethod
    public void getWallboardStatus(PluginCall call) {
        call.resolve(WallboardServer.status());
    }

    @PluginMethod
    public void pushWallboardState(PluginCall call) {
        String stateJson = call.getString("stateJson", "{\"mode\":\"standby\"}");
        int clients = WallboardServer.pushState(stateJson);
        JSObject ret = new JSObject();
        ret.put("clients", clients);
        call.resolve(ret);
    }

    @PluginMethod
    public void setVoiceListening(PluginCall call) {
        boolean enabled = call.getBoolean("enabled", false);
        boolean forceRestart = call.getBoolean("forceRestart", false);
        if (enabled && getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "onMicPermResult");
            return;
        }
        VoiceRecognitionManager.setEnabled(getContext(), enabled, readBiasPhrases(call), forceRestart);
        call.resolve(VoiceRecognitionManager.status(getContext(), getPermissionState("microphone") == PermissionState.GRANTED));
    }

    @PermissionCallback
    private void onMicPermResult(PluginCall call) {
        boolean granted = getPermissionState("microphone") == PermissionState.GRANTED;
        if (granted) {
            VoiceRecognitionManager.setEnabled(getContext(), true, readBiasPhrases(call), true);
            call.resolve(VoiceRecognitionManager.status(getContext(), true));
        } else {
            VoiceRecognitionManager.stop();
            JSObject ret = VoiceRecognitionManager.status(getContext(), false);
            ret.put("listening", false);
            ret.put("error", "Microphone permission denied");
            call.resolve(ret);
        }
    }

    @PluginMethod
    public void getVoiceStatus(PluginCall call) {
        call.resolve(VoiceRecognitionManager.status(getContext(), getPermissionState("microphone") == PermissionState.GRANTED));
    }

    @PluginMethod
    public void ensureVoicePermission(PluginCall call) {
        if (getPermissionState("microphone") == PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("granted", true);
            call.resolve(ret);
            return;
        }
        requestPermissionForAlias("microphone", call, "onVoicePermOnlyResult");
    }

    @PermissionCallback
    private void onVoicePermOnlyResult(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("granted", getPermissionState("microphone") == PermissionState.GRANTED);
        call.resolve(ret);
    }

    @PluginMethod
    public void speakVoice(PluginCall call) {
        String text = call.getString("text", "");
        VoiceSpeaker.speak(getContext(), text);
        call.resolve();
    }

    private static ArrayList<String> readBiasPhrases(PluginCall call) {
        ArrayList<String> out = new ArrayList<>();
        JSArray arr = call.getArray("biasPhrases");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                String s = arr.optString(i, "").trim();
                if (!s.isEmpty()) out.add(s);
            }
        }
        return out;
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

    /** Captures a single video via ACTION_VIDEO_CAPTURE (with camera ✓ confirm), moves it into DCIM/Camera, and returns metadata for 1-step linking. */
    @PluginMethod
    public void recordVideo(PluginCall call) {
        Context c = getContext();
        File capDir = new File(c.getCacheDir(), "video_capture");
        if (!capDir.exists()) capDir.mkdirs();
        File temp = new File(capDir, "cap_" + System.currentTimeMillis() + ".mp4");
        pendingCaptureFile = temp;
        try {
            Uri captureUri = FileProvider.getUriForFile(c, c.getPackageName() + ".fileprovider", temp);
            Intent i = new Intent(MediaStore.ACTION_VIDEO_CAPTURE);
            i.putExtra(MediaStore.EXTRA_OUTPUT, captureUri);
            i.putExtra(MediaStore.EXTRA_VIDEO_QUALITY, 1);
            i.setClipData(ClipData.newRawUri("", captureUri));
            i.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivityForResult(call, i, "onVideoCaptured");
        } catch (Exception e) {
            pendingCaptureFile = null;
            temp.delete();
            JSObject ret = new JSObject();
            ret.put("picked", false);
            ret.put("error", e.getMessage() != null ? e.getMessage() : "Could not open Camera app");
            call.resolve(ret);
        }
    }

    @ActivityCallback
    private void onVideoCaptured(PluginCall call, ActivityResult result) {
        if (call == null) return;
        File capturedFile = pendingCaptureFile;
        pendingCaptureFile = null;
        Intent data = result.getData();
        Uri dataUri = data != null ? data.getData() : null;
        if (result.getResultCode() != Activity.RESULT_OK) {
            if (capturedFile != null) capturedFile.delete();
            JSObject ret = new JSObject();
            ret.put("picked", false);
            call.resolve(ret);
            return;
        }
        Context appCtx = getContext().getApplicationContext();
        new Thread(() -> {
            JSObject ret = new JSObject();
            try {
                boolean hasFile = capturedFile != null && capturedFile.exists() && capturedFile.length() > 0;
                if (!hasFile && dataUri == null) {
                    ret.put("picked", false);
                    call.resolve(ret);
                    return;
                }
                SimpleDateFormat sdf = new SimpleDateFormat("yyyyMMdd_HHmmssSSS", Locale.US);
                sdf.setTimeZone(TimeZone.getTimeZone("UTC"));
                long nowMs = System.currentTimeMillis();
                String fileName = "PXL_" + sdf.format(new Date(nowMs)) + ".mp4";
                long bytes = hasFile ? capturedFile.length() : 0L;
                Uri dcimUri;
                try (InputStream is = hasFile
                        ? new FileInputStream(capturedFile)
                        : appCtx.getContentResolver().openInputStream(dataUri)) {
                    if (is == null) throw new IllegalStateException("Cannot read captured video");
                    dcimUri = saveStreamToDcimCamera(appCtx, is, fileName);
                }
                ret = buildPickedVideoJson(appCtx, dcimUri, fileName, bytes, nowMs);
            } catch (Exception e) {
                ret.put("picked", false);
                ret.put("error", e.getMessage() != null ? e.getMessage() : "Failed to save recorded video");
            } finally {
                if (capturedFile != null && capturedFile.exists()) capturedFile.delete();
            }
            call.resolve(ret);
        }).start();
    }

    private static boolean hasVideoReadAccess(Context c) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(c, Manifest.permission.READ_MEDIA_VIDEO)
                    == PackageManager.PERMISSION_GRANTED) {
                return true;
            }
            return Build.VERSION.SDK_INT >= 34
                    && ContextCompat.checkSelfPermission(c, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED)
                    == PackageManager.PERMISSION_GRANTED;
        }
        return ContextCompat.checkSelfPermission(c, Manifest.permission.READ_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED;
    }

    /** Queries device videos via MediaStore ordered newest-first (`DATE_MODIFIED DESC`). */
    @PluginMethod
    public void listDeviceVideos(PluginCall call) {
        if (hasVideoReadAccess(getContext())) {
            queryDeviceVideosAsync(call);
            return;
        }
        String alias = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU ? "videos" : "videosLegacy";
        requestPermissionForAlias(alias, call, "onVideosPermission");
    }

    @PermissionCallback
    private void onVideosPermission(PluginCall call) {
        if (call == null) return;
        if (hasVideoReadAccess(getContext())) {
            queryDeviceVideosAsync(call);
            return;
        }
        JSObject ret = new JSObject();
        ret.put("granted", false);
        ret.put("hasMore", false);
        ret.put("videos", new JSArray());
        call.resolve(ret);
    }

    private void queryDeviceVideosAsync(PluginCall call) {
        Context appCtx = getContext().getApplicationContext();
        int limit = Math.max(1, Math.min(250, call.getInt("limit", 48)));
        new Thread(() -> {
            JSObject ret = new JSObject();
            JSArray videos = new JSArray();
            boolean hasMore = false;
            ContentResolver cr = appCtx.getContentResolver();
            String[] proj = new String[] {
                    MediaStore.Video.Media._ID,
                    MediaStore.Video.Media.DISPLAY_NAME,
                    MediaStore.Video.Media.SIZE,
                    MediaStore.Video.Media.DURATION,
                    MediaStore.Video.Media.DATE_MODIFIED,
                    MediaStore.Video.Media.MIME_TYPE,
                    MediaStore.Video.Media.BUCKET_DISPLAY_NAME
            };
            String sort = MediaStore.Video.Media.DATE_MODIFIED + " DESC, " + MediaStore.Video.Media._ID + " DESC";
            try (Cursor cur = cr.query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, proj, null, null, sort)) {
                if (cur != null) {
                    int idCol = cur.getColumnIndexOrThrow(MediaStore.Video.Media._ID);
                    int nameCol = cur.getColumnIndex(MediaStore.Video.Media.DISPLAY_NAME);
                    int sizeCol = cur.getColumnIndex(MediaStore.Video.Media.SIZE);
                    int durCol = cur.getColumnIndex(MediaStore.Video.Media.DURATION);
                    int dateCol = cur.getColumnIndex(MediaStore.Video.Media.DATE_MODIFIED);
                    int mimeCol = cur.getColumnIndex(MediaStore.Video.Media.MIME_TYPE);
                    int bucketCol = cur.getColumnIndex(MediaStore.Video.Media.BUCKET_DISPLAY_NAME);
                    while (cur.moveToNext()) {
                        long size = sizeCol >= 0 ? cur.getLong(sizeCol) : 0L;
                        if (size <= 0L) continue;
                        if (videos.length() >= limit) {
                            hasMore = true;
                            break;
                        }
                        long id = cur.getLong(idCol);
                        Uri itemUri = ContentUris.withAppendedId(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, id);
                        String fileName = (nameCol >= 0 && cur.getString(nameCol) != null)
                                ? cur.getString(nameCol)
                                : ("VID_" + id + ".mp4");
                        long durMs = durCol >= 0 ? cur.getLong(durCol) : 0L;
                        long dateSec = dateCol >= 0 ? cur.getLong(dateCol) : 0L;
                        String mime = (mimeCol >= 0 && cur.getString(mimeCol) != null)
                                ? cur.getString(mimeCol)
                                : "video/mp4";
                        String bucket = (bucketCol >= 0 && cur.getString(bucketCol) != null)
                                ? cur.getString(bucketCol)
                                : "";
                        String thumb = extractVideoThumb(appCtx, cr, itemUri);
                        JSObject item = new JSObject();
                        item.put("uri", itemUri.toString());
                        item.put("fileName", fileName);
                        item.put("bytes", size);
                        item.put("fileMs", dateSec > 0 ? dateSec * 1000L : 0L);
                        item.put("durationSec", durMs > 0 ? (int) Math.round(durMs / 1000.0) : 0);
                        item.put("mime", mime);
                        if (!bucket.isEmpty()) item.put("bucket", bucket);
                        item.put("thumb", thumb);
                        videos.put(item);
                    }
                }
                ret.put("granted", true);
                ret.put("hasMore", hasMore);
                ret.put("videos", videos);
            } catch (Exception e) {
                ret.put("granted", false);
                ret.put("hasMore", false);
                ret.put("videos", new JSArray());
                ret.put("error", e.getMessage() != null ? e.getMessage() : "Video query failed");
            }
            call.resolve(ret);
        }).start();
    }

    @PluginMethod
    public void pickDcimVideo(PluginCall call) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("video/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"video/*"});
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            i.putExtra(
                    DocumentsContract.EXTRA_INITIAL_URI,
                    DocumentsContract.buildRootUri("com.android.providers.media.documents", "videos_root"));
        }
        startActivityForResult(call, i, "onVideoPicked");
    }

    @ActivityCallback
    private void onVideoPicked(PluginCall call, ActivityResult result) {
        if (call == null) return;
        if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null || result.getData().getData() == null) {
            JSObject ret = new JSObject();
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
        JSObject ret = buildPickedVideoJson(c, uri, uri.getLastPathSegment(), 0L, 0L);
        call.resolve(ret);
    }

    private static String encodeThumbDataUrl(Bitmap bmp, int maxW, int quality) {
        if (bmp == null) return "";
        int tw = Math.min(maxW, Math.max(1, bmp.getWidth()));
        int th = Math.max(1, Math.round((float) tw * bmp.getHeight() / Math.max(1, bmp.getWidth())));
        Bitmap scaled = (tw != bmp.getWidth() || th != bmp.getHeight())
                ? Bitmap.createScaledBitmap(bmp, tw, th, true)
                : bmp;
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        scaled.compress(Bitmap.CompressFormat.JPEG, quality, baos);
        if (scaled != bmp) scaled.recycle();
        bmp.recycle();
        return "data:image/jpeg;base64," + Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP);
    }

    private static String extractVideoThumb(Context c, ContentResolver cr, Uri uri) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                Bitmap bmp = cr.loadThumbnail(uri, new Size(240, 240), null);
                if (bmp != null) return encodeThumbDataUrl(bmp, 200, 60);
            } catch (Exception ignored) {
            }
        }
        MediaMetadataRetriever mmr = new MediaMetadataRetriever();
        try {
            mmr.setDataSource(c, uri);
            Bitmap bmp = mmr.getFrameAtTime(400_000L, MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
            if (bmp == null) bmp = mmr.getFrameAtTime();
            if (bmp != null) return encodeThumbDataUrl(bmp, 200, 60);
        } catch (Exception ignored) {
        } finally {
            try {
                mmr.release();
            } catch (Exception ignored) {
            }
        }
        return "";
    }

    private static JSObject buildPickedVideoJson(Context c, Uri uri, String fallbackName, long fallbackBytes, long fallbackMs) {
        JSObject ret = new JSObject();
        ContentResolver cr = c.getContentResolver();
        String fileName = fallbackName;
        long size = fallbackBytes;
        long fileMs = fallbackMs;
        try (Cursor cur = cr.query(uri, null, null, null, null)) {
            if (cur != null && cur.moveToFirst()) {
                int ni = cur.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (ni >= 0 && cur.getString(ni) != null) fileName = cur.getString(ni);
                int si = cur.getColumnIndex(OpenableColumns.SIZE);
                if (si >= 0 && cur.getLong(si) > 0) size = cur.getLong(si);
                int mi = cur.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED);
                if (mi >= 0 && cur.getLong(mi) > 0) {
                    fileMs = cur.getLong(mi);
                } else {
                    int di = cur.getColumnIndex(MediaStore.MediaColumns.DATE_MODIFIED);
                    if (di >= 0 && cur.getLong(di) > 0) fileMs = cur.getLong(di) * 1000L;
                }
            }
        } catch (Exception ignored) {
        }
        String mime = cr.getType(uri);
        if (mime != null && !mime.startsWith("video/")) {
            ret.put("picked", false);
            ret.put("error", "Not a video file");
            return ret;
        }
        int durationSec = 0;
        String thumb = "";
        MediaMetadataRetriever mmr = new MediaMetadataRetriever();
        try {
            mmr.setDataSource(c, uri);
            String d = mmr.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION);
            if (d != null) durationSec = (int) Math.round(Long.parseLong(d) / 1000.0);
            Bitmap bmp = mmr.getFrameAtTime(400_000L, MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
            if (bmp == null) bmp = mmr.getFrameAtTime();
            if (bmp != null) thumb = encodeThumbDataUrl(bmp, 200, 60);
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
        return ret;
    }

    private static Uri saveStreamToDcimCamera(Context c, InputStream is, String fileName) throws Exception {
        return saveVideoStream(c, is, Environment.DIRECTORY_DCIM, "Camera", fileName).uri;
    }

    private static class SavedVideoResult {
        final Uri uri;
        final String displayPath;
        SavedVideoResult(Uri uri, String displayPath) {
            this.uri = uri;
            this.displayPath = displayPath;
        }
    }

    private static SavedVideoResult saveVideoStream(Context c, InputStream is, String rootDir, String subDir, String fileName) throws Exception {
        String relPath = rootDir + "/" + subDir;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentResolver cr = c.getContentResolver();
            ContentValues cv = new ContentValues();
            cv.put(MediaStore.Video.Media.DISPLAY_NAME, fileName);
            cv.put(MediaStore.Video.Media.MIME_TYPE, "video/mp4");
            cv.put(MediaStore.Video.Media.RELATIVE_PATH, relPath + "/");
            cv.put(MediaStore.Video.Media.IS_PENDING, 1);
            Uri dstUri = cr.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, cv);
            if (dstUri == null) throw new IllegalStateException("MediaStore insert failed");
            try (OutputStream os = cr.openOutputStream(dstUri, "w")) {
                if (os == null) throw new IllegalStateException("MediaStore write failed");
                copyStreams(is, os);
            }
            ContentValues done = new ContentValues();
            done.put(MediaStore.Video.Media.IS_PENDING, 0);
            cr.update(dstUri, done, null, null);
            return new SavedVideoResult(dstUri, relPath + "/" + fileName);
        } else {
            File dir = new File(Environment.getExternalStoragePublicDirectory(rootDir), subDir);
            if (!dir.exists()) dir.mkdirs();
            File dst = new File(dir, fileName);
            try (OutputStream os = new FileOutputStream(dst)) {
                copyStreams(is, os);
            }
            return new SavedVideoResult(Uri.fromFile(dst), dst.getAbsolutePath());
        }
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

    /** Registers a `content://` DCIM video or LAN `http://` clip on the loopback HTTP range server so `<video>` plays in-app. */
    @PluginMethod
    public void getPlayableVideoUrl(PluginCall call) {
        String u = call.getString("uri", "");
        String mime = call.getString("mime", "video/mp4");
        JSObject ret = new JSObject();
        if (u == null || u.isEmpty()) {
            ret.put("error", "Missing uri");
            call.resolve(ret);
            return;
        }
        try {
            String url = LocalMediaServer.register(getContext(), Uri.parse(u), mime);
            ret.put("url", url);
        } catch (Exception e) {
            ret.put("error", e.getMessage());
        }
        call.resolve(ret);
    }

    /** Hardware-trims (optional) and shares or saves an MP4 clip from `content://`, LAN HTTP, or base64. */
    @PluginMethod
    public void exportVideo(PluginCall call) {
        String uriStr = call.getString("uri", "");
        String httpUrl = call.getString("httpUrl", "");
        String base64 = call.getString("base64", "");
        String rawName = call.getString("fileName", "armory-clip.mp4");
        String title = call.getString("title", "Armory Fit Lift Clip");
        double startMsD = call.getDouble("startMs", -1.0);
        double endMsD = call.getDouble("endMs", -1.0);
        String action = call.getString("action", "share");
        Context c = getContext().getApplicationContext();

        new Thread(() -> {
            JSObject ret = new JSObject();
            File srcTemp = null;
            File outFile = null;
            try {
                File exportDir = new File(c.getCacheDir(), "video_export");
                if (!exportDir.exists()) exportDir.mkdirs();
                File[] stale = exportDir.listFiles();
                if (stale != null) {
                    long cutoff = System.currentTimeMillis() - 3_600_000L;
                    for (File f : stale) {
                        if (f.lastModified() < cutoff) f.delete();
                    }
                }

                String base = (rawName != null ? rawName.trim() : "armory-clip.mp4")
                        .replaceAll("[^a-zA-Z0-9._-]", "_")
                        .replaceAll("^\\.+", "");
                if (base.isEmpty()) base = "armory-clip.mp4";
                if (!base.toLowerCase().endsWith(".mp4")) base = base + ".mp4";
                String safeName = base;

                Uri srcUri = null;
                File srcFile = null;
                if (uriStr != null && uriStr.startsWith("content://")) {
                    srcUri = Uri.parse(uriStr);
                } else if (httpUrl != null && (httpUrl.startsWith("http://") || httpUrl.startsWith("https://"))) {
                    srcTemp = new File(exportDir, "src_" + System.currentTimeMillis() + ".mp4");
                    downloadToFile(httpUrl, srcTemp);
                    srcFile = srcTemp;
                } else if (base64 != null && !base64.isEmpty()) {
                    srcTemp = new File(exportDir, "src_" + System.currentTimeMillis() + ".mp4");
                    byte[] raw = Base64.decode(base64, Base64.DEFAULT);
                    try (FileOutputStream fos = new FileOutputStream(srcTemp)) {
                        fos.write(raw);
                    }
                    srcFile = srcTemp;
                } else {
                    ret.put("ok", false);
                    ret.put("error", "No video source");
                    call.resolve(ret);
                    return;
                }

                outFile = new File(exportDir, safeName);
                if (outFile.exists()) outFile.delete();
                boolean hasTrim = startMsD >= 0 && endMsD > startMsD + 100.0;
                if (hasTrim) {
                    Mp4Trimmer.trim(c, srcUri, srcFile, Math.round(startMsD), Math.round(endMsD), outFile);
                } else if (srcFile != null) {
                    try (InputStream is = new FileInputStream(srcFile); OutputStream os = new FileOutputStream(outFile)) {
                        copyStreams(is, os);
                    }
                } else {
                    try (InputStream is = c.getContentResolver().openInputStream(srcUri); OutputStream os = new FileOutputStream(outFile)) {
                        if (is == null) throw new IllegalStateException("Cannot read video URI");
                        copyStreams(is, os);
                    }
                }

                if ("save".equals(action)) {
                    String savedPath = saveVideoToGallery(c, outFile, safeName);
                    outFile.delete();
                    ret.put("ok", true);
                    ret.put("savedPath", savedPath);
                } else {
                    Uri contentUri = FileProvider.getUriForFile(c, c.getPackageName() + ".fileprovider", outFile);
                    Intent send = new Intent(Intent.ACTION_SEND);
                    send.setType("video/mp4");
                    send.putExtra(Intent.EXTRA_STREAM, contentUri);
                    send.putExtra(Intent.EXTRA_SUBJECT, title);
                    send.setClipData(ClipData.newRawUri(title, contentUri));
                    send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    Intent chooser = Intent.createChooser(send, title);
                    chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    c.startActivity(chooser);
                    ret.put("ok", true);
                }
            } catch (Exception e) {
                if (outFile != null && outFile.exists()) outFile.delete();
                ret.put("ok", false);
                ret.put("error", e.getMessage() != null ? e.getMessage() : "Video export failed");
            } finally {
                if (srcTemp != null && srcTemp.exists()) srcTemp.delete();
            }
            call.resolve(ret);
        }).start();
    }

    private static void downloadToFile(String httpUrl, File dst) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(httpUrl).openConnection();
        conn.setConnectTimeout(10_000);
        conn.setReadTimeout(30_000);
        try {
            int code = conn.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IllegalStateException("HTTP " + code);
            }
            try (InputStream is = conn.getInputStream(); OutputStream os = new FileOutputStream(dst)) {
                copyStreams(is, os);
            }
        } finally {
            conn.disconnect();
        }
    }

    private static void copyStreams(InputStream is, OutputStream os) throws Exception {
        byte[] buf = new byte[65_536];
        int n;
        while ((n = is.read(buf)) >= 0) {
            os.write(buf, 0, n);
        }
        os.flush();
    }

    private static String saveVideoToGallery(Context c, File src, String fileName) throws Exception {
        try (InputStream is = new FileInputStream(src)) {
            return saveVideoStream(c, is, Environment.DIRECTORY_MOVIES, "ArmoryFit", fileName).displayPath;
        }
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

    /** Writes a JSON backup file into shared Documents/ArmoryFit Backup/ for DriveSync. */
    @PluginMethod
    public void writeBackupFile(PluginCall call) {
        String rawName = call.getString("fileName", "armory-fit-latest.json");
        String fileName = (rawName == null || rawName.trim().isEmpty()) ? "armory-fit-latest.json" : rawName.trim().replace('/', '_');
        String content = call.getString("content", "");
        byte[] bytes = (content != null ? content : "").getBytes(StandardCharsets.UTF_8);
        Context c = getContext().getApplicationContext();

        new Thread(() -> {
            JSObject ret = new JSObject();
            String relPath = Environment.DIRECTORY_DOCUMENTS + "/ArmoryFit Backup";
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ContentResolver cr = c.getContentResolver();
                Uri collection = MediaStore.Files.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY);
                Uri existing = null;
                String sel = MediaStore.MediaColumns.DISPLAY_NAME + "=? AND ("
                        + MediaStore.MediaColumns.RELATIVE_PATH + "=? OR "
                        + MediaStore.MediaColumns.RELATIVE_PATH + "=?)";
                String[] args = new String[] {fileName, relPath, relPath + "/"};
                try (Cursor cur = cr.query(collection, new String[] {MediaStore.MediaColumns._ID}, sel, args, null)) {
                    if (cur != null && cur.moveToFirst()) {
                        long id = cur.getLong(0);
                        existing = ContentUris.withAppendedId(collection, id);
                    }
                } catch (Exception ignored) {
                }

                Uri target = existing;
                boolean wrote = false;
                if (target != null) {
                    try (OutputStream os = cr.openOutputStream(target, "wt")) {
                        if (os != null) {
                            os.write(bytes);
                            os.flush();
                            wrote = true;
                        }
                    } catch (Exception ignored) {
                        try {
                            cr.delete(target, null, null);
                        } catch (Exception ignored2) {
                        }
                        target = null;
                    }
                }
                if (!wrote) {
                    ContentValues cv = new ContentValues();
                    cv.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
                    cv.put(MediaStore.MediaColumns.MIME_TYPE, "application/json");
                    cv.put(MediaStore.MediaColumns.RELATIVE_PATH, relPath + "/");
                    try {
                        target = cr.insert(collection, cv);
                        if (target != null) {
                            try (OutputStream os = cr.openOutputStream(target, "w")) {
                                if (os != null) {
                                    os.write(bytes);
                                    os.flush();
                                    wrote = true;
                                }
                            }
                        }
                    } catch (Exception e) {
                        ret.put("error", e.getMessage());
                    }
                }
                ret.put("saved", wrote);
                ret.put("path", relPath + "/" + fileName);
            } else {
                boolean wrote = false;
                File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS), "ArmoryFit Backup");
                try {
                    if (!dir.exists()) dir.mkdirs();
                    File out = new File(dir, fileName);
                    try (FileOutputStream fos = new FileOutputStream(out, false)) {
                        fos.write(bytes);
                        fos.flush();
                        wrote = true;
                    }
                    ret.put("path", out.getAbsolutePath());
                } catch (Exception e) {
                    ret.put("error", e.getMessage());
                }
                ret.put("saved", wrote);
            }
            call.resolve(ret);
        }).start();
    }

    /** Opens Android file picker pointed at Documents/ArmoryFit Backup/ so DriveSync backups restore in 1 tap. */
    @PluginMethod
    public void pickBackupFile(PluginCall call) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"application/json", "text/plain"});
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            i.putExtra(
                    DocumentsContract.EXTRA_INITIAL_URI,
                    Uri.parse("content://com.android.externalstorage.documents/document/primary%3ADocuments%2FArmoryFit%20Backup"));
        }
        startActivityForResult(call, i, "onBackupPicked");
    }

    @ActivityCallback
    private void onBackupPicked(PluginCall call, ActivityResult result) {
        if (call == null) return;
        JSObject ret = new JSObject();
        if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null || result.getData().getData() == null) {
            ret.put("picked", false);
            call.resolve(ret);
            return;
        }
        Uri uri = result.getData().getData();
        Context c = getContext();
        String fileName = uri.getLastPathSegment();
        try (Cursor cur = c.getContentResolver().query(uri, null, null, null, null)) {
            if (cur != null && cur.moveToFirst()) {
                int ni = cur.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (ni >= 0 && cur.getString(ni) != null) fileName = cur.getString(ni);
            }
        } catch (Exception ignored) {
        }
        try (InputStream is = c.getContentResolver().openInputStream(uri)) {
            if (is == null) {
                ret.put("picked", false);
                call.resolve(ret);
                return;
            }
            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = is.read(buf)) >= 0) {
                baos.write(buf, 0, n);
            }
            ret.put("picked", true);
            ret.put("fileName", fileName != null ? fileName : "backup.json");
            ret.put("content", new String(baos.toByteArray(), StandardCharsets.UTF_8));
        } catch (Exception e) {
            ret.put("picked", false);
            ret.put("error", e.getMessage());
        }
        call.resolve(ret);
    }

    /** Stops foreground workout service, voice recognizer, wallboard/media servers & metronome, and removes all Android Recents tasks cleanly. */
    @PluginMethod
    public void forceClose(PluginCall call) {
        WorkoutMetronome.stop();
        VoiceRecognitionManager.stop();
        WallboardServer.stop();
        LocalMediaServer.stop();
        Context appCtx = getContext().getApplicationContext();
        Activity act = getActivity();
        call.resolve();
        try {
            appCtx.stopService(new Intent(appCtx, WorkoutForegroundService.class));
        } catch (Exception ignored) {
        }
        if (act == null) return;
        act.runOnUiThread(() -> {
            boolean removedViaAppTasks = false;
            try {
                ActivityManager am = (ActivityManager) act.getSystemService(Context.ACTIVITY_SERVICE);
                if (am != null) {
                    List<ActivityManager.AppTask> tasks = am.getAppTasks();
                    if (tasks != null && !tasks.isEmpty()) {
                        for (ActivityManager.AppTask task : tasks) {
                            task.setExcludeFromRecents(true);
                            task.finishAndRemoveTask();
                        }
                        removedViaAppTasks = true;
                    }
                }
            } catch (Exception ignored) {
            }
            if (!removedViaAppTasks) {
                act.finishAndRemoveTask();
            }
        });
    }
}
