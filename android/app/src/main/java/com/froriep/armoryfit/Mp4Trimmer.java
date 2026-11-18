package com.froriep.armoryfit;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMetadataRetriever;
import android.media.MediaMuxer;
import android.net.Uri;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;

/**
 * Hardware-native MP4 trimmer (`MediaExtractor` + `MediaMuxer`) preserving video orientation
 * and copying strictly the primary video and audio tracks so camera metadata tracks never fail export.
 */
final class Mp4Trimmer {
    private Mp4Trimmer() {}

    static void trim(Context c, Uri srcUri, File srcFile, long startMs, long endMs, File dstFile) throws Exception {
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
