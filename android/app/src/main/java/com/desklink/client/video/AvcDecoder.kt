package com.desklink.client.video

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer

/**
 * Hardware H.264 (Annex-B) decoder rendering straight to a Surface.
 *
 * The first access unit that carries SPS/PPS in-band bootstraps the codec
 * (the host's h264parse config-interval=-1 guarantees this on every IDR).
 *
 * feed() is called from the network reader thread while reset() may be
 * called from the UI thread — both go through [lock].
 */
class AvcDecoder(private val surface: Surface) {
    companion object {
        private const val TAG = "dl.Decoder"
        private const val MIME = "video/avc"
        private const val TIMEOUT_US = 0L
    }

    private val lock = Any()
    private var codec: MediaCodec? = null
    private var configured = false
    private var lastPts = 0L
    var framesDecoded = 0L
        private set

    /** Real decoded size (crop applied), reported from the codec's output
     * format; fires on the reader thread whenever the stream resolution
     * changes (e.g. host quality preset or monitor change). */
    var onVideoSize: ((width: Int, height: Int) -> Unit)? = null

    /** Feed one access unit (Annex-B, may include SPS/PPS). */
    fun feed(data: ByteArray, ptsUs: Long) {
        synchronized(lock) {
            if (!configured && !bootstrapLocked(data)) return
            val c = codec ?: return
            try {
                val index = c.dequeueInputBuffer(TIMEOUT_US)
                if (index >= 0) {
                    val buf = c.getInputBuffer(index)!!
                    buf.clear()
                    if (data.size <= buf.capacity()) {
                        buf.put(data)
                        c.queueInputBuffer(index, 0, data.size, nextPtsLocked(ptsUs), 0)
                        framesDecoded++
                    } else {
                        Log.w(TAG, "frame too large for input buffer (${data.size})")
                        c.queueInputBuffer(index, 0, 0, nextPtsLocked(ptsUs), 0)
                    }
                } // else: decoder busy — drop the frame; latency beats completeness
                drainLocked(c)
            } catch (e: IllegalStateException) {
                Log.w(TAG, "codec error, resetting: ${e.message}")
                resetLocked()
            }
        }
    }

    private fun nextPtsLocked(hostPts: Long): Long {
        // Host pts can be CLOCK_TIME_NONE(→0) or jump; enforce monotonic output.
        val pts = if (hostPts > lastPts) hostPts else lastPts + 33_333
        lastPts = pts
        return pts
    }

    private fun drainLocked(c: MediaCodec) {
        while (true) {
            val info = MediaCodec.BufferInfo()
            val outIndex = c.dequeueOutputBuffer(info, TIMEOUT_US)
            when {
                outIndex >= 0 -> c.releaseOutputBuffer(outIndex, true)
                outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED ->
                    reportSizeLocked(c.outputFormat)
                else -> return
            }
        }
    }

    /** Extract display dimensions (honouring the crop rect) from the codec's
     * output format and publish them via [onVideoSize]. */
    private fun reportSizeLocked(format: MediaFormat) {
        Log.i(TAG, "format: $format")
        try {
            val w = format.getInteger(MediaFormat.KEY_WIDTH)
            val h = format.getInteger(MediaFormat.KEY_HEIGHT)
            var dw = w
            var dh = h
            if (format.containsKey("crop-left") && format.containsKey("crop-right")) {
                dw = format.getInteger("crop-right") + 1 - format.getInteger("crop-left")
            }
            if (format.containsKey("crop-top") && format.containsKey("crop-bottom")) {
                dh = format.getInteger("crop-bottom") + 1 - format.getInteger("crop-top")
            }
            if (dw > 0 && dh > 0) onVideoSize?.invoke(dw, dh)
        } catch (e: Exception) {
            Log.w(TAG, "size report failed: ${e.message}")
        }
    }

    /** Extract SPS+PPS from a keyframe access unit and start the codec. */
    private fun bootstrapLocked(keyframe: ByteArray): Boolean {
        val sps = findNal(keyframe, 7) ?: return false
        val pps = findNal(keyframe, 8) ?: return false
        val format = MediaFormat.createVideoFormat(MIME, 1920, 1080) // real size parsed by codec
        format.setByteBuffer("csd-0", ByteBuffer.wrap(sps))
        format.setByteBuffer("csd-1", ByteBuffer.wrap(pps))
        if (Build.VERSION.SDK_INT >= 30) {
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        }
        format.setInteger(MediaFormat.KEY_OPERATING_RATE, Short.MAX_VALUE.toInt())
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 4 * 1024 * 1024)
        return try {
            val c = MediaCodec.createDecoderByType(MIME)
            c.configure(format, surface, null, 0)
            c.start()
            codec = c
            configured = true
            Log.i(TAG, "codec started (sps=${sps.size}B pps=${pps.size}B)")
            true
        } catch (e: Exception) {
            Log.e(TAG, "codec configure failed: ${e.message}")
            try { codec?.release() } catch (_: Exception) {}
            codec = null
            false
        }
    }

    /** Return the complete NAL (with Annex-B start code) of the given type. */
    private fun findNal(data: ByteArray, nalType: Int): ByteArray? {
        val starts = ArrayList<Int>()
        var i = 0
        while (i < data.size - 4) {
            if (data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
                data[i + 2] == 1.toByte()) {
                starts.add(i)
                i += 3
            } else if (i < data.size - 5 && data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
                data[i + 2] == 0.toByte() && data[i + 3] == 0.toByte() && data[i + 4] == 1.toByte()) {
                starts.add(i)
                i += 4
            } else {
                i++
            }
        }
        for ((idx, start) in starts.withIndex()) {
            val hdr = start + (if (data[start + 2] == 1.toByte()) 3 else 4)
            if (hdr >= data.size) continue
            val type = data[hdr].toInt() and 0x1F
            if (type == nalType) {
                val end = starts.getOrNull(idx + 1) ?: data.size
                return data.copyOfRange(start, end)
            }
        }
        return null
    }

    /** Full teardown; next keyframe will re-bootstrap (used on stream switch). */
    fun reset() {
        synchronized(lock) { resetLocked() }
    }

    private fun resetLocked() {
        configured = false
        lastPts = 0
        try { codec?.stop() } catch (_: Exception) {}
        try { codec?.release() } catch (_: Exception) {}
        codec = null
    }
}
