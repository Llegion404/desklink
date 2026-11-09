package com.desklink.client.net

import android.util.Log
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.receiveAsFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import kotlin.concurrent.thread

/** One monitor stream advertised by the host. `w/h` are the host's logical
 * monitor sizes; `actualW/actualH` (0 until known) are the real encoded video
 * dimensions reported via `stream-info` (may be downscaled). */
data class StreamDesc(val id: Int, val w: Int, val h: Int,
                      val actualW: Int = 0, val actualH: Int = 0)

data class FsEntry(val name: String, val isDir: Boolean, val size: Long)

sealed interface Event {
    data class Hello(val hostName: String, val clipboard: Boolean,
                     val audio: Boolean, val streams: List<StreamDesc>) : Event
    data class Denied(val msg: String) : Event
    data class Subscribed(val stream: Int) : Event
    data class Clipboard(val text: String) : Event
    data class FsList(val id: Int, val path: String, val entries: List<FsEntry>, val error: String?) : Event
    data class FsDataBegin(val id: Int, val size: Long, val name: String) : Event
    data class FsChunk(val id: Int, val data: ByteArray) : Event
    data class FsDataEnd(val id: Int) : Event
    data class FsPutAck(val id: Int) : Event
    data class FsPutDone(val id: Int, val path: String, val size: Long) : Event
    data class FsError(val id: Int, val msg: String) : Event
    data class Pong(val clientSentMs: Long, val hostMs: Long) : Event
    data class Quality(val kbps: Int, val maxh: Int, val ok: Boolean) : Event
    data class StreamInfo(val stream: Int, val w: Int, val h: Int) : Event
    data class StreamError(val stream: Int, val msg: String) : Event
    data class Closed(val reason: String) : Event
}

/**
 * Framed TCP client for the DeskLink host.
 *
 * One reader thread parses frames: video goes to [onVideoFrame] (hot path,
 * no flow allocation), everything else is emitted as [Event]s.
 * One writer thread drains a queue; [sendJson] never blocks the caller.
 */
class Client(
    private val host: String,
    private val port: Int,
    private val pin: String,
) {
    companion object {
        private const val TAG = "dl.Client"
        private const val FT_JSON = 0x01
        private const val FT_VIDEO = 0x02
        private const val FT_FILE = 0x04
        private const val FT_AUDIO = 0x05
        private const val MAX_FRAME = 16 * 1024 * 1024
    }

    /** Set before [connect]; called on the reader thread. */
    var onVideoFrame: ((streamId: Int, keyframe: Boolean, ptsUs: Long, data: ByteArray) -> Unit)? = null

    /** Set before [connect]; called on the reader thread. */
    var onAudio: ((kind: Int, data: ByteArray) -> Unit)? = null

    private val _events = MutableSharedFlow<Event>(
        extraBufferCapacity = 128, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    val events: SharedFlow<Event> = _events

    /**
     * File-transfer events get their own unbounded, non-dropping channel:
     * a dropped FsChunk would corrupt a download, so the lossy semantics of
     * [_events] (fine for stats/pong/clipboard) are unacceptable here.
     */
    private val _fileEvents = Channel<Event>(Channel.UNLIMITED)
    val fileEvents: Flow<Event> = _fileEvents.receiveAsFlow()

    private var socket: Socket? = null
    private var out: DataOutputStream? = null
    private val writeQueue = LinkedBlockingQueue<ByteArray>(512)
    @Volatile private var running = false
    private var reader: Thread? = null
    private var writer: Thread? = null
    private var nextReqId = 1

    fun connect() {
        if (running) return
        running = true
        thread(name = "dl-reader") { runReader() }
    }

    fun close(reason: String = "client closed") {
        running = false
        try { socket?.close() } catch (_: IOException) {}
        writeQueue.clear()
        writeQueue.offer(ByteArray(0)) // poison pill unblocks the writer
        emit(Event.Closed(reason))
    }

    val isConnected: Boolean get() = running

    // ---------------------------------------------------------------- reader
    private fun runReader() {
        var sock: Socket? = null
        try {
            sock = Socket()
            sock.tcpNoDelay = true
            sock.connect(InetSocketAddress(host, port), 8000)
            sock.soTimeout = 0
            socket = sock
            val input = DataInputStream(sock.getInputStream())
            val output = DataOutputStream(sock.getOutputStream())
            out = output
            thread(name = "dl-writer") { runWriter(output) }

            handshake(input, output)

            while (running) {
                val ftype = input.readByte().toInt() and 0xFF
                val len = input.readInt()
                if (len < 0 || len > MAX_FRAME) throw IOException("bad frame length $len")
                val payload = ByteArray(len)
                input.readFully(payload)
                when (ftype) {
                    FT_VIDEO -> {
                        if (len < 10) continue
                        val streamId = payload[0].toInt() and 0xFF
                        val flags = payload[1].toInt() and 0xFF
                        val pts = varLong(payload, 2)
                        onVideoFrame?.invoke(streamId, flags and 1 != 0, pts,
                            payload.copyOfRange(10, len))
                    }
                    FT_FILE -> {
                        if (len < 4) continue
                        val id = ((payload[0].toInt() and 0xFF) shl 24) or
                                 ((payload[1].toInt() and 0xFF) shl 16) or
                                 ((payload[2].toInt() and 0xFF) shl 8) or
                                 (payload[3].toInt() and 0xFF)
                        emit(Event.FsChunk(id, payload.copyOfRange(4, len)))
                    }
                    FT_AUDIO -> {
                        if (len < 1) continue
                        onAudio?.invoke(payload[0].toInt() and 0xFF,
                            payload.copyOfRange(1, len))
                    }
                    FT_JSON -> handleJson(payload)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "reader stopped: ${e.message}")
            emit(Event.Closed(when (e) {
                is java.net.ConnectException -> "connection refused"
                is java.net.SocketTimeoutException -> "connect timeout"
                is IOException -> "connection lost"
                else -> e.message ?: "error"
            }))
        } finally {
            running = false
            try { sock?.close() } catch (_: IOException) {}
        }
    }

    private fun varLong(b: ByteArray, off: Int): Long {
        var v = 0L
        for (i in 0 until 8) v = (v shl 8) or (b[off + i].toLong() and 0xFF)
        return v
    }

    // -------------------------------------------------------------- handshake
    private fun handshake(input: DataInputStream, output: DataOutputStream) {
        // greet
        run {
            val ftype = input.readByte().toInt()
            val len = input.readInt()
            val payload = ByteArray(len)
            input.readFully(payload)
            require(ftype == FT_JSON) { "expected greet" }
            val greet = JSONObject(String(payload))
            if (greet.optString("type") != "greet") throw IOException("no greet")
            val salt = greet.getString("salt")
            val mac = hmacSha256(sha256(pin), salt.toByteArray(Charsets.UTF_8))
            sendJson(JSONObject().put("type", "auth")
                .put("mac", mac)
                .put("client", "Android DeskLink"))
        }
        // hello / deny
        val ftype = input.readByte().toInt()
        val len = input.readInt()
        val payload = ByteArray(len)
        input.readFully(payload)
        val hello = JSONObject(String(payload))
        when (hello.optString("type")) {
            "hello" -> {
                val streams = ArrayList<StreamDesc>()
                val arr: JSONArray = hello.optJSONArray("streams") ?: JSONArray()
                for (i in 0 until arr.length()) {
                    val s = arr.getJSONObject(i)
                    streams.add(StreamDesc(s.getInt("id"), s.getInt("w"), s.getInt("h")))
                }
                emit(Event.Hello(hello.optString("host"), hello.optBoolean("clipboard"),
                    hello.optBoolean("audio"), streams))
            }
            "deny" -> {
                emit(Event.Denied(hello.optString("msg", "denied")))
                throw IOException("auth denied")
            }
            else -> throw IOException("unexpected ${hello.optString("type")}")
        }
    }

    private fun sha256(s: String): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8))

    private fun hmacSha256(key: ByteArray, data: ByteArray): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data).joinToString("") { "%02x".format(it) }
    }

    // --------------------------------------------------------------- writer
    private fun frame(ftype: Int, payload: ByteArray): ByteArray {
        val outBuf = ByteArray(5 + payload.size)
        outBuf[0] = ftype.toByte()
        val len = payload.size
        outBuf[1] = ((len ushr 24) and 0xFF).toByte()
        outBuf[2] = ((len ushr 16) and 0xFF).toByte()
        outBuf[3] = ((len ushr 8) and 0xFF).toByte()
        outBuf[4] = (len and 0xFF).toByte()
        System.arraycopy(payload, 0, outBuf, 5, payload.size)
        return outBuf
    }

    private fun runWriter(output: DataOutputStream) {
        try {
            while (running) {
                val item = writeQueue.take() // blocks until work or poison pill
                if (item.isEmpty()) break // poison pill
                output.write(item)
                output.flush()
            }
        } catch (e: IOException) {
            Log.w(TAG, "writer stopped: ${e.message}")
            running = false
            try { socket?.close() } catch (_: IOException) {}
        } catch (_: InterruptedException) {
            // shutdown
        }
    }

    private fun enqueue(bytes: ByteArray) {
        if (!writeQueue.offer(bytes)) {
            Log.w(TAG, "write queue full, dropping message")
        }
    }

    fun sendJson(obj: JSONObject) = enqueue(frame(FT_JSON, obj.toString().toByteArray(Charsets.UTF_8)))

    // ------------------------------------------------------------- commands
    fun subscribe(streamId: Int) = sendJson(JSONObject().put("type", "subscribe").put("stream", streamId))
    fun requestIdr(streamId: Int) = sendJson(JSONObject().put("type", "request-idr").put("stream", streamId))
    fun ping() = sendJson(JSONObject().put("type", "ping").put("t", System.currentTimeMillis()))
    fun sendText(text: String) = sendJson(JSONObject().put("type", "text").put("s", text))
    fun sendKey(code: Int, down: Boolean) = sendJson(JSONObject().put("type", "key").put("code", code).put("down", down))
    fun sendMotion(streamId: Int, x: Double, y: Double) =
        sendJson(JSONObject().put("type", "motion").put("stream", streamId).put("x", x).put("y", y))
    fun sendButton(btn: String, down: Boolean) =
        sendJson(JSONObject().put("type", "button").put("btn", btn).put("down", down))
    fun sendSetAudio(on: Boolean) = sendJson(
        JSONObject().put("type", "set-audio").put("on", on).put("format", "raw"))

    fun sendScroll(dx: Int, dy: Int) =
        sendJson(JSONObject().put("type", "scroll").put("dx", dx).put("dy", dy))
    fun sendClipboard(text: String) =
        sendJson(JSONObject().put("type", "clip-set").put("text", text))

    fun fsList(path: String): Int {
        val id = nextReqId++
        sendJson(JSONObject().put("type", "fs-list").put("path", path).put("id", id))
        return id
    }

    fun fsGet(path: String): Int {
        val id = nextReqId++
        sendJson(JSONObject().put("type", "fs-get").put("path", path).put("id", id))
        return id
    }

    fun fsPut(dir: String, name: String, size: Long): Int {
        val id = nextReqId++
        sendJson(JSONObject().put("type", "fs-put").put("dir", dir)
            .put("name", name).put("size", size).put("id", id))
        return id
    }

    fun sendFileChunk(id: Int, chunk: ByteArray) {
        val payload = ByteArray(4 + chunk.size)
        payload[0] = ((id ushr 24) and 0xFF).toByte()
        payload[1] = ((id ushr 16) and 0xFF).toByte()
        payload[2] = ((id ushr 8) and 0xFF).toByte()
        payload[3] = (id and 0xFF).toByte()
        System.arraycopy(chunk, 0, payload, 4, chunk.size)
        enqueue(frame(FT_FILE, payload))
    }

    fun fsPutEnd(id: Int) = sendJson(JSONObject().put("type", "fs-put-end").put("id", id))

    fun setQuality(kbps: Int, maxH: Int = 0) =
        sendJson(JSONObject().put("type", "set-quality")
            .put("kbps", kbps).put("maxh", maxH))

    // ---------------------------------------------------------------- events
    private fun emit(event: Event) {
        _events.tryEmit(event)
    }

    private fun emitFile(event: Event) {
        _fileEvents.trySend(event) // UNLIMITED: never drops
    }

    private fun handleJson(payload: ByteArray) {
        try {
            val msg = JSONObject(String(payload))
            when (msg.optString("type")) {
                "hello" -> {
                    // re-announce after a host session restart (auto
                    // re-request): the caller resubscribes on this event
                    val arr = msg.optJSONArray("streams") ?: JSONArray()
                    val streams = ArrayList<StreamDesc>(arr.length())
                    for (i in 0 until arr.length()) {
                        val s = arr.getJSONObject(i)
                        streams.add(StreamDesc(s.getInt("id"), s.getInt("w"),
                            s.getInt("h")))
                    }
                    emit(Event.Hello(msg.optString("host"),
                        msg.optBoolean("clipboard"), msg.optBoolean("audio"),
                        streams))
                }
                "clip" -> emit(Event.Clipboard(msg.optString("text")))
                "fs-list-reply" -> {
                    val arr = msg.optJSONArray("entries") ?: JSONArray()
                    val entries = ArrayList<FsEntry>(arr.length())
                    for (i in 0 until arr.length()) {
                        val e = arr.getJSONObject(i)
                        entries.add(FsEntry(e.getString("n"), e.optBoolean("d"),
                            e.optLong("s")))
                    }
                    emit(Event.FsList(msg.optInt("id"), msg.optString("path"), entries,
                        if (msg.isNull("error")) null else msg.optString("error")))
                }
                "fs-data-begin" -> emitFile(Event.FsDataBegin(msg.getInt("id"),
                    msg.getLong("size"), msg.optString("name")))
                "fs-data-end" -> emitFile(Event.FsDataEnd(msg.getInt("id")))
                "fs-put-ack" -> emitFile(Event.FsPutAck(msg.getInt("id")))
                "fs-put-done" -> emitFile(Event.FsPutDone(msg.getInt("id"),
                    msg.optString("path"), msg.optLong("size")))
                "fs-error" -> emitFile(Event.FsError(msg.getInt("id"), msg.optString("msg")))
                "pong" -> emit(Event.Pong(msg.optLong("t"), msg.optLong("host")))
                "quality" -> emit(Event.Quality(msg.optInt("kbps"),
                    msg.optInt("maxh"), msg.optBoolean("ok")))
                "stream-info" -> emit(Event.StreamInfo(msg.getInt("stream"),
                    msg.optInt("w"), msg.optInt("h")))
                "subscribed" -> emit(Event.Subscribed(msg.getInt("stream")))
                "stream-error" -> emit(Event.StreamError(msg.getInt("stream"), msg.optString("msg")))
                "host-error" -> emit(Event.StreamError(-1, msg.optString("msg")))
                "bye" -> { running = false; try { socket?.close() } catch (_: IOException) {} }
                else -> Log.d(TAG, "unhandled: ${msg.optString("type")}")
            }
        } catch (e: Exception) {
            Log.w(TAG, "bad json frame: ${e.message}")
        }
    }
}
