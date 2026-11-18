package com.froriep.armoryfit;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMetadataRetriever;
import android.media.MediaMuxer;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import androidx.annotation.NonNull;
import androidx.annotation.OptIn;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MimeTypes;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.transformer.Composition;
import androidx.media3.transformer.EditedMediaItem;
import androidx.media3.transformer.EditedMediaItemSequence;
import androidx.media3.transformer.ExportException;
import androidx.media3.transformer.ExportResult;
import androidx.media3.transformer.Transformer;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Hardware MP4 trimmer backed by AndroidX Jetpack Media3 Transformer for frame-exact
 * clipping (never snaps start cuts 0.5-1.0s back to an I-frame, and tone-maps 10-bit HLG HDR
 * captures to universal SDR H.264), falling back to stream-copy if needed.
 */
final class Mp4Trimmer {
    private Mp4Trimmer() {}

    @OptIn(markerClass = UnstableApi.class)
    static void trim(Context c, Uri srcUri, File srcFile, long startMs, long endMs, File dstFile) throws Exception {
        Context appCtx = c.getApplicationContext();
        Uri inputUri = (srcFile != null) ? Uri.fromFile(srcFile) : srcUri;
        long safeStartMs = Math.max(0L, startMs);
        long safeEndMs = Math.max(safeStartMs + 100L, endMs);

        MediaItem.ClippingConfiguration clipCfg = new MediaItem.ClippingConfiguration.Builder()
                .setStartPositionMs(safeStartMs)
                .setEndPositionMs(safeEndMs)
                .setStartsAtKeyFrame(false)
                .build();

        MediaItem mediaItem = new MediaItem.Builder()
                .setUri(inputUri)
                .setClippingConfiguration(clipCfg)
                .build();

        EditedMediaItem editedItem = new EditedMediaItem.Builder(mediaItem).build();

        Exception firstErr = null;
        try {
            runMedia3Transform(appCtx, editedItem, dstFile, true);
            if (dstFile.exists() && dstFile.length() > 0) return;
        } catch (Exception e) {
            firstErr = e;
            if (dstFile.exists()) dstFile.delete();
        }

        try {
            runMedia3Transform(appCtx, editedItem, dstFile, false);
            if (dstFile.exists() && dstFile.length() > 0) return;
        } catch (Exception secondErr) {
            if (dstFile.exists()) dstFile.delete();
            try {
                streamCopyTrim(appCtx, srcUri, srcFile, safeStartMs, safeEndMs, dstFile);
                return;
            } catch (Exception ignored) {
                if (firstErr != null) throw firstErr;
                throw secondErr;
            }
        }

        streamCopyTrim(appCtx, srcUri, srcFile, safeStartMs, safeEndMs, dstFile);
    }

    @OptIn(markerClass = UnstableApi.class)
    private static void runMedia3Transform(
            Context appCtx,
            EditedMediaItem editedItem,
            File dstFile,
            boolean forceSdrH264
    ) throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<Exception> errRef = new AtomicReference<>();
        AtomicReference<Transformer> txRef = new AtomicReference<>();
        Handler mainHandler = new Handler(Looper.getMainLooper());

        Runnable startTask = () -> {
            try {
                Transformer.Builder tb = new Transformer.Builder(appCtx);
                if (forceSdrH264) {
                    tb.setVideoMimeType(MimeTypes.VIDEO_H264);
                }
                tb.addListener(new Transformer.Listener() {
                    @Override
                    public void onCompleted(@NonNull Composition composition, @NonNull ExportResult exportResult) {
                        latch.countDown();
                    }

                    @Override
                    public void onError(
                            @NonNull Composition composition,
                            @NonNull ExportResult exportResult,
                            @NonNull ExportException exportException
                    ) {
                        errRef.compareAndSet(null, exportException);
                        latch.countDown();
                    }
                });
                Transformer tx = tb.build();
                txRef.set(tx);

                if (forceSdrH264 && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    EditedMediaItemSequence seq = new EditedMediaItemSequence(editedItem);
                    Composition comp = new Composition.Builder(seq)
                            .setHdrMode(Composition.HDR_MODE_TONE_MAP_HDR_TO_SDR_USING_OPEN_GL)
                            .build();
                    tx.start(comp, dstFile.getAbsolutePath());
                } else {
                    tx.start(editedItem, dstFile.getAbsolutePath());
                }
            } catch (Exception e) {
                errRef.compareAndSet(null, e);
                latch.countDown();
            }
        };

        if (Looper.myLooper() == Looper.getMainLooper()) {
            startTask.run();
        } else {
            mainHandler.post(startTask);
        }

        boolean finished = latch.await(120, TimeUnit.SECONDS);
        if (!finished) {
            Transformer tx = txRef.get();
            if (tx != null) mainHandler.post(tx::cancel);
            throw new IOException("Video trim timed out");
        }
        Exception err = errRef.get();
        if (err != null) throw err;
    }

    private static void streamCopyTrim(Context c, Uri srcUri, File srcFile, long startMs, long endMs, File dstFile) throws Exception {
        int rotation = 0;
        MediaMetadataRetriever mmr = new MediaMetadataRetriever();
        try {
            if (srcFile != null) mmr.setDataSource(srcFile.getAbsolutePath());
            else mmr.setDataSource(c, srcUri);
            String rot = mmr.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION);
            if (rot != null) rotation = Integer.parseInt(rot.trim());
        } catch (Exception ignored) {
        } finally {
            try { mmr.release(); } catch (Exception ignored) {}
        }

        MediaExtractor extractor = new MediaExtractor();
        MediaMuxer muxer = null;
        boolean muxerStarted = false;
        try {
            if (srcFile != null) extractor.setDataSource(srcFile.getAbsolutePath());
            else extractor.setDataSource(c, srcUri, null);

            int videoSrcTrack = -1;
            int audioSrcTrack = -1;
            int videoDstTrack = -1;
            int audioDstTrack = -1;
            int maxInputSize = 1024 * 1024;

            for (int i = 0; i < extractor.getTrackCount(); i++) {
                MediaFormat fmt = extractor.getTrackFormat(i);
                String mime = fmt.getString(MediaFormat.KEY_MIME);
                if (mime == null) continue;
                if (mime.startsWith("video/") && videoSrcTrack < 0) {
                    videoSrcTrack = i;
                    if (fmt.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                        maxInputSize = Math.max(maxInputSize, fmt.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE));
                    }
                } else if (mime.startsWith("audio/") && audioSrcTrack < 0) {
                    audioSrcTrack = i;
                    if (fmt.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                        maxInputSize = Math.max(maxInputSize, fmt.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE));
                    }
                }
            }

            if (videoSrcTrack < 0 && audioSrcTrack < 0) {
                throw new IOException("No video or audio tracks found");
            }

            muxer = new MediaMuxer(dstFile.getAbsolutePath(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            if (rotation != 0) muxer.setOrientationHint(rotation);

            if (videoSrcTrack >= 0) {
                extractor.selectTrack(videoSrcTrack);
                videoDstTrack = muxer.addTrack(extractor.getTrackFormat(videoSrcTrack));
            }
            if (audioSrcTrack >= 0) {
                extractor.selectTrack(audioSrcTrack);
                audioDstTrack = muxer.addTrack(extractor.getTrackFormat(audioSrcTrack));
            }

            muxer.start();
            muxerStarted = true;

            long startUs = Math.max(0L, startMs) * 1000L;
            long endUs = endMs * 1000L;
            extractor.seekTo(startUs, MediaExtractor.SEEK_TO_PREVIOUS_SYNC);

            ByteBuffer buf = ByteBuffer.allocate(maxInputSize);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            long basePtsUs = -1L;
            int writtenSamples = 0;

            while (true) {
                info.offset = 0;
                int sampleSize = extractor.readSampleData(buf, 0);
                if (sampleSize < 0) break;

                int track = extractor.getSampleTrackIndex();
                long sampleTimeUs = extractor.getSampleTime();

                if (track == videoSrcTrack && sampleTimeUs > endUs) {
                    break;
                }
                if (track == audioSrcTrack) {
                    if (videoSrcTrack >= 0 && basePtsUs < 0) {
                        extractor.advance();
                        continue;
                    }
                    if (sampleTimeUs > endUs + 200_000L) {
                        extractor.advance();
                        continue;
                    }
                }

                if (basePtsUs < 0) {
                    basePtsUs = sampleTimeUs;
                }

                int dstTrack = (track == videoSrcTrack) ? videoDstTrack : (track == audioSrcTrack ? audioDstTrack : -1);
                if (dstTrack >= 0) {
                    info.size = sampleSize;
                    info.presentationTimeUs = Math.max(0L, sampleTimeUs - basePtsUs);
                    int sampleFlags = extractor.getSampleFlags();
                    info.flags = ((sampleFlags & MediaExtractor.SAMPLE_FLAG_SYNC) != 0)
                            ? MediaCodec.BUFFER_FLAG_KEY_FRAME
                            : 0;
                    muxer.writeSampleData(dstTrack, buf, info);
                    writtenSamples++;
                }

                extractor.advance();
            }

            if (writtenSamples == 0) {
                throw new IOException("No samples within trim range");
            }
        } finally {
            try {
                if (muxer != null) {
                    if (muxerStarted) {
                        try { muxer.stop(); } catch (Exception ignored) {}
                    }
                    muxer.release();
                }
            } catch (Exception ignored) {}
            try { extractor.release(); } catch (Exception ignored) {}
        }
    }
}
