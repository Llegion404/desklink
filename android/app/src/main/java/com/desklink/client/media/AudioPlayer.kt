package com.desklink.client.media

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.util.Log
import kotlin.concurrent.thread

/**
 * Desktop-audio playback: raw Opus packets -> MediaCodec("audio/opus") ->
 * AudioTrack. The host sends a synthesized OpusHead as the first frame
 * (kind 1) — MediaCodec requires it as csd-0. One decoder thread; packets
 * are queued (small, drop-oldest) so the network reader never blocks.
 */
class AudioPlayer {
    companion object {
        private const val TAG = "dl.Audio"
        private const val SAMPLE_RATE = 48000
        private const val CHANNELS = 2
        const val KIND_OPUS = 0
        const val KIND_OPUS_HEAD = 1
    }

    private var decoder: MediaCodec? = null
    private var track: AudioTrack? = null
    private var worker: Thread? = null
    private val queue = ArrayDeque<ByteArray>()
    private var csd0: ByteArray? = null
    private val lock = Object()
    @Volatile private var running = false
    private var inputPtsUs = 0L

    /** Feed one audio frame from the wire (reader thread). */
    fun push(kind: Int, data: ByteArray) {
        if (kind == KIND_OPUS_HEAD) {
            synchronized(lock) {
                csd0 = data
                lock.notifyAll()
            }
            return
        }
        if (kind != KIND_OPUS) return
        synchronized(lock) {
            if (queue.size > 30) queue.removeFirst()   // don't lag behind live
            queue.addLast(data)
            lock.notifyAll()
        }
    }

    fun start() {
        if (running) return
        running = true
        worker = thread(name = "dl-audio") { runLoop() }
    }

    fun stop() {
        running = false
        synchronized(lock) { lock.notifyAll() }
        worker?.join(2000)
        worker = null
    }

    private fun runLoop() {
        try {
            val csd = synchronized(lock) {
                while (csd0 == null && running) lock.wait(2000)
                csd0
            }
            if (csd == null || !running) return

            val format = MediaFormat.createAudioFormat(
                MediaFormat.MIMETYPE_AUDIO_OPUS, SAMPLE_RATE, CHANNELS)
            // the public SDK has no setByteArray: csd-0 goes via ByteBuffer
            format.setByteBuffer("csd-0", java.nio.ByteBuffer.wrap(csd))
            val dec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
            dec.configure(format, null, null, 0)
            dec.start()
            decoder = dec

            val minBuf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_STEREO,
                AudioFormat.ENCODING_PCM_16BIT)
            val at = AudioTrack(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build(),
                AudioFormat.Builder()
                    .setSampleRate(SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                    .build(),
                maxOf(minBuf * 4, 32 * 1024),
                AudioTrack.MODE_STREAM,
                AudioManager.AUDIO_SESSION_ID_GENERATE)
            at.play()
            track = at

            val info = MediaCodec.BufferInfo()
            while (running) {
                val packet: ByteArray = synchronized(lock) {
                    while (queue.isEmpty() && running) lock.wait(100)
                    if (queue.isNotEmpty()) queue.removeFirst() else null
                } ?: continue

                val inIdx = dec.dequeueInputBuffer(10_000)
                if (inIdx >= 0) {
                    val buf = dec.getInputBuffer(inIdx) ?: continue
                    buf.clear()
                    buf.put(packet)
                    dec.queueInputBuffer(inIdx, 0, packet.size, inputPtsUs, 0)
                    inputPtsUs += 20_000   // 20 ms frames
                }
                var outIdx = dec.dequeueOutputBuffer(info, 10_000)
                while (outIdx >= 0 && running) {
                    val pcm = dec.getOutputBuffer(outIdx)
                    if (pcm != null) {
                        at.write(pcm, info.size, AudioTrack.WRITE_BLOCKING)
                    }
                    dec.releaseOutputBuffer(outIdx, false)
                    outIdx = dec.dequeueOutputBuffer(info, 0)
                }
            }
        } catch (e: Exception) {
            if (running) Log.w(TAG, "audio decode stopped: ${e.message}")
        } finally {
            try { decoder?.stop() } catch (_: Exception) {}
            try { decoder?.release() } catch (_: Exception) {}
            decoder = null
            try { track?.stop() } catch (_: Exception) {}
            try { track?.release() } catch (_: Exception) {}
            track = null
        }
    }
}
