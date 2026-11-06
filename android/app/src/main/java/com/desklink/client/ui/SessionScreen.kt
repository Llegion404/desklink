package com.desklink.client.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.view.KeyEvent
import android.view.SurfaceView
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculateCentroid
import androidx.compose.foundation.gestures.calculateZoom
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CenterFocusStrong
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Keyboard
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Upload
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.desklink.client.ConnSpec
import com.desklink.client.KeyboardBridge
import com.desklink.client.input.EvdevKeys
import com.desklink.client.input.Modifiers
import com.desklink.client.input.SPECIAL_KEYS_ROW_1
import com.desklink.client.input.SPECIAL_KEYS_ROW_2
import com.desklink.client.net.Client
import com.desklink.client.net.Event
import com.desklink.client.net.FsEntry
import com.desklink.client.net.StreamDesc
import com.desklink.client.video.AvcDecoder
import kotlinx.coroutines.delay
import java.io.File
import java.io.OutputStream
import kotlin.math.abs

private const val TAG = "dl.Session"
private const val MAX_RECONNECTS = 4

/** One row in the transfer progress list (downloads + upload results). */
data class DownloadRow(
    val id: Int,
    val name: String,
    val size: Long,
    val received: Long = 0,
    val status: String = "downloading",
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionScreen(spec: ConnSpec, onExit: () -> Unit) {
    val context = LocalContext.current
    val view = LocalView.current

    // -- connection lifecycle (client instance is replaced on reconnect)
    var client by remember { mutableStateOf(Client(spec.host, spec.port, spec.pin)) }
    var userExit by remember { mutableStateOf(false) }
    var reconnects by remember { mutableStateOf(0) }
    var authFailed by remember { mutableStateOf(false) }

    // -- session state
    var streams by remember { mutableStateOf<List<StreamDesc>>(emptyList()) }
    var currentStream by remember { mutableStateOf<Int?>(null) }
    var hostName by remember { mutableStateOf("?") }
    var clipboardEnabled by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("connecting...") }
    var connected by remember { mutableStateOf(false) }

    // -- stats
    var fps by remember { mutableStateOf(0) }
    var kbps by remember { mutableStateOf(0) }
    var rttMs by remember { mutableStateOf<Long?>(null) }
    val frameCounter = remember { FrameCounter() }

    // -- QoL toggles
    var viewOnly by remember { mutableStateOf(false) }
    var immersive by remember { mutableStateOf(true) }
    var qualityKbps by remember { mutableStateOf(8000) }
    var qualityMaxH by remember { mutableStateOf(0) }

    // -- actual decoded video size (from the codec) drives letterboxing
    var videoSize by remember { mutableStateOf<Pair<Int, Int>?>(null) }

    // -- local view transform
    var zoom by remember { mutableFloatStateOf(1f) }
    var pan by remember { mutableStateOf(Offset.Zero) }

    // -- sticky keyboard modifiers
    val latches = remember { Latches() }

    // -- sheets
    var showKeyboard by remember { mutableStateOf(false) }
    var showFiles by remember { mutableStateOf(false) }

    // -- transfers (downloads stream straight to disk; rows are UI-only)
    val downloads = remember { mutableStateListOf<DownloadRow>() }
    val downloadWriters = remember { HashMap<Int, OutputStream>() }

    // -- clipboard sync
    val clipboardManager = remember {
        context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    }
    var suppressClipboardEchoUntil by remember { mutableStateOf(0L) }

    // -- decoder
    var decoder by remember { mutableStateOf<AvcDecoder?>(null) }
    var surfaceReady by remember { mutableStateOf(false) }
    val surfaceViewRef = remember { mutableStateOf<SurfaceView?>(null) }

    // ------------------------------------------------------------- lifecycle
    LaunchedEffect(client) { client.connect() }

    LaunchedEffect(client) {
        client.onVideoFrame = { _, _, pts, data ->
            frameCounter.onFrame(data.size)
            decoder?.feed(data, pts)
        }
    }

    LaunchedEffect(client) {
        client.events.collect { event ->
            when (event) {
                is Event.Hello -> {
                    hostName = event.hostName
                    streams = event.streams
                    clipboardEnabled = event.clipboard
                    connected = true
                    authFailed = false
                    reconnects = 0
                    status = "connected"
                    val target = currentStream?.takeIf { id ->
                        event.streams.any { it.id == id }
                    } ?: event.streams.firstOrNull()?.id
                    if (target != null) client.subscribe(target)
                }
                is Event.Denied -> {
                    authFailed = true
                    connected = false
                    status = "denied: ${event.msg}"
                }
                is Event.Subscribed -> {
                    currentStream = event.stream
                    videoSize = null
                    decoder?.reset()
                    client.requestIdr(event.stream)
                }
                is Event.StreamError -> status = "stream error: ${event.msg}"
                is Event.StreamInfo -> {
                    streams = streams.map {
                        if (it.id == event.stream) {
                            it.copy(actualW = event.w, actualH = event.h)
                        } else it
                    }
                }
                is Event.Pong -> rttMs = System.currentTimeMillis() - event.clientSentMs
                is Event.Quality -> {
                    qualityKbps = event.kbps
                    qualityMaxH = event.maxh
                }
                is Event.Clipboard -> {
                    if (System.currentTimeMillis() > suppressClipboardEchoUntil) {
                        suppressClipboardEchoUntil = System.currentTimeMillis() + 2000
                        clipboardManager.setPrimaryClip(
                            ClipData.newPlainText("desklink", event.text))
                    }
                }
                is Event.Closed -> {
                    connected = false
                    if (userExit || authFailed) return@collect
                    if (reconnects < MAX_RECONNECTS) {
                        val wait = 1500L shl reconnects
                        reconnects++
                        status = "connection lost — reconnecting in ${wait / 1000}s " +
                            "(${reconnects}/$MAX_RECONNECTS)"
                        delay(wait)
                        val old = client
                        client = Client(spec.host, spec.port, spec.pin)
                        old.close("reconnecting")
                        status = "reconnecting..."
                    } else {
                        status = "disconnected"
                    }
                }
                else -> {}
            }
        }
    }

    // file events: stream chunks straight to storage (never buffered in RAM)
    LaunchedEffect(client) {
        client.fileEvents.collect { event ->
            when (event) {
                is Event.FsDataBegin -> {
                    try {
                        val out = openDownload(context, event.name)
                        downloadWriters[event.id] = out
                        downloads.add(DownloadRow(event.id, event.name, event.size))
                    } catch (e: Exception) {
                        downloads.add(DownloadRow(event.id, event.name, event.size,
                            status = "failed: ${e.message}"))
                    }
                }
                is Event.FsChunk -> {
                    val out = downloadWriters[event.id]
                    val row = downloads.indexOfFirst { it.id == event.id }
                    if (out != null && row >= 0) {
                        try {
                            out.write(event.data)
                            downloads[row] = downloads[row].copy(
                                received = downloads[row].received + event.data.size)
                        } catch (e: Exception) {
                            closeQuietly(downloadWriters.remove(event.id))
                            downloads[row] = downloads[row].copy(status = "failed")
                        }
                    }
                }
                is Event.FsDataEnd -> {
                    closeQuietly(downloadWriters.remove(event.id))
                    val row = downloads.indexOfFirst { it.id == event.id }
                    if (row >= 0) downloads[row] = downloads[row].copy(status = "saved ✓")
                }
                is Event.FsError -> {
                    closeQuietly(downloadWriters.remove(event.id))
                    val row = downloads.indexOfFirst { it.id == event.id }
                    if (row >= 0) downloads[row] = downloads[row].copy(status = "failed")
                }
                is Event.FsPutDone -> downloads.add(DownloadRow(
                    event.id, "↑ ${File(event.path).name}", event.size,
                    received = event.size, status = "uploaded ✓"))
                else -> {}
            }
        }
    }

    // periodic stats + RTT probe
    LaunchedEffect(connected) {
        while (connected) {
            client.ping()
            fps = frameCounter.fps
            kbps = frameCounter.kbps
            frameCounter.tick()
            delay(1000)
        }
    }

    // local clipboard -> host
    val clipboardListener = remember {
        ClipboardManager.OnPrimaryClipChangedListener {
            if (!clipboardEnabled) return@OnPrimaryClipChangedListener
            if (System.currentTimeMillis() < suppressClipboardEchoUntil) return@OnPrimaryClipChangedListener
            val text = clipboardManager.primaryClip?.getItemAt(0)
                ?.coerceToText(context)?.toString() ?: return@OnPrimaryClipChangedListener
            if (text.isNotBlank() && text.length < 1_000_000) {
                suppressClipboardEchoUntil = System.currentTimeMillis() + 2000
                client.sendClipboard(text)
            }
        }
    }
    DisposableEffect(Unit) {
        clipboardManager.addPrimaryClipChangedListener(clipboardListener)
        onDispose {
            clipboardManager.removePrimaryClipChangedListener(clipboardListener)
        }
    }

    // hardware keyboard passthrough
    DisposableEffect(client) {
        KeyboardBridge.handler = lambda@{ keyCode, action, unicode, metaState ->
            val hasHardMods = metaState and (KeyEvent.META_CTRL_ON or
                KeyEvent.META_ALT_ON or KeyEvent.META_META_ON) != 0
            when {
                viewOnly || !connected -> false
                action != KeyEvent.ACTION_DOWN && action != KeyEvent.ACTION_UP -> false
                !hasHardMods && unicode >= 32 && action == KeyEvent.ACTION_DOWN -> {
                    client.sendText(unicode.toChar().toString())
                    true
                }
                EvdevKeys.map.containsKey(keyCode) -> {
                    EvdevKeys.send(client, keyCode, action)
                    true
                }
                else -> false
            }
        }
        onDispose { KeyboardBridge.handler = null }
    }

    // immersive mode (suspend while a sheet is open so the IME fits)
    LaunchedEffect(immersive, connected, showKeyboard, showFiles) {
        val controller = ViewCompat.getWindowInsetsController(view) ?: return@LaunchedEffect
        if (immersive && connected && !showKeyboard && !showFiles) {
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            controller.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }

    // teardown when leaving the session
    DisposableEffect(Unit) {
        onDispose {
            userExit = true
            KeyboardBridge.handler = null
            client.close("session closed")
            downloadWriters.values.forEach { closeQuietly(it) }
            downloadWriters.clear()
        }
    }

    // ---------------------------------------------------------------- layout
    val streamDesc = streams.firstOrNull { it.id == currentStream }
    // letterbox + gesture mapping use the real encoded size when known,
    // falling back to the hello dims (same aspect) until the decoder reports
    val mapW = videoSize?.first ?: streamDesc?.w ?: 0
    val mapH = videoSize?.second ?: streamDesc?.h ?: 0

    Box(Modifier.fillMaxSize().background(Color.Black),
        contentAlignment = Alignment.Center) {
        // aspect-fit the video, then apply the user's zoom/pan visually —
        // toNormIn() below models exactly this transform, so taps stay exact
        AndroidView(
            factory = { ctx ->
                SurfaceView(ctx).apply {
                    holder.setKeepScreenOn(true)
                    surfaceViewRef.value = this
                    holder.addCallback(object : android.view.SurfaceHolder.Callback {
                        override fun surfaceCreated(holder: android.view.SurfaceHolder) {
                            surfaceReady = true
                        }

                        override fun surfaceChanged(
                            holder: android.view.SurfaceHolder,
                            format: Int, width: Int, height: Int) {}

                        override fun surfaceDestroyed(holder: android.view.SurfaceHolder) {
                            surfaceReady = false
                            decoder?.reset()
                            decoder = null
                        }
                    })
                }
            },
            modifier = Modifier
                .then(
                    if (mapW > 0 && mapH > 0) {
                        Modifier.aspectRatio(mapW.toFloat() / mapH)
                    } else {
                        Modifier.fillMaxSize()
                    })
                .graphicsLayer {
                    scaleX = zoom
                    scaleY = zoom
                    translationX = pan.x
                    translationY = pan.y
                },
        )

        if (streamDesc != null) {
            GestureLayer(
                modifier = Modifier.fillMaxSize(),
                client = client,
                streamId = streamDesc.id,
                streamW = mapW,
                streamH = mapH,
                enabled = connected && !viewOnly,
                zoomProvider = { zoom },
                panProvider = { pan },
                onZoom = { zoom = it.coerceIn(0.5f, 6f) },
                onPan = { pan = it },
            )
        }

        LaunchedEffect(surfaceReady, currentStream) {
            val sv = surfaceViewRef.value
            if (surfaceReady && currentStream != null && sv != null) {
                decoder?.reset()
                decoder = AvcDecoder(sv.holder.surface).also { dec ->
                    dec.onVideoSize = { w, h -> videoSize = w to h }
                }
            }
        }

        // ---- top status bar overlay
        Row(
            Modifier.fillMaxWidth()
                .background(Color(0xAA000000))
                .padding(horizontal = 4.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.Start) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(hostName, color = Color.White,
                        style = MaterialTheme.typography.labelLarge,
                        modifier = Modifier.padding(end = 6.dp))
                    Text(
                        status, color = when {
                            connected -> Color(0xFF7ED37E)
                            authFailed -> Color(0xFFE0A0A0)
                            else -> Color(0xFFE0C060)
                        },
                        style = MaterialTheme.typography.labelSmall,
                        maxLines = 1,
                    )
                }
                // compact: the bar only fits ~15 chars next to the buttons
                val mbps = "%.1f".format(kbps / 1000f)
                val rtt = rttMs?.toString() ?: "?"
                Text("${fps}fps ${mbps}Mb ${rtt}ms",
                    color = Color(0xFFBBBBBB),
                    style = MaterialTheme.typography.labelSmall,
                    maxLines = 1)
            }

            // monitor picker
            if (streams.size > 1) {
                var menuOpen by remember { mutableStateOf(false) }
                Box {
                    TextButton(
                        onClick = { menuOpen = true },
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(6.dp),
                    ) {
                        Text("${streams.indexOfFirst { it.id == currentStream } + 1}/${streams.size}",
                            color = Color.White, style = MaterialTheme.typography.labelMedium)
                    }
                    DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                        streams.forEach { s ->
                            DropdownMenuItem(
                                text = {
                                    val dims = if (s.actualW > 0) "${s.actualW}×${s.actualH}"
                                               else "${s.w}×${s.h}"
                                    Text("#${streams.indexOf(s) + 1}: $dims")
                                },
                                onClick = {
                                    menuOpen = false
                                    if (currentStream != s.id) {
                                        currentStream = s.id
                                        zoom = 1f
                                        pan = Offset.Zero
                                        client.subscribe(s.id)
                                    }
                                })
                        }
                    }
                }
            }
            IconButton(onClick = { showKeyboard = true }) {
                Icon(Icons.Filled.Keyboard, "keyboard", tint = Color.White)
            }
            IconButton(onClick = { showFiles = true }) {
                Icon(Icons.Filled.Folder, "files", tint = Color.White)
            }

            // overflow: QoL toggles + quality
            var overflowOpen by remember { mutableStateOf(false) }
            var qualityOpen by remember { mutableStateOf(false) }
            Box {
                IconButton(onClick = { overflowOpen = true }) {
                    Icon(Icons.Filled.MoreVert, "more", tint = Color.White)
                }
                DropdownMenu(expanded = overflowOpen, onDismissRequest = { overflowOpen = false }) {
                    DropdownMenuItem(
                        text = { Text("View only") },
                        leadingIcon = {
                            Checkbox(checked = viewOnly, onCheckedChange = { viewOnly = it })
                        },
                        onClick = { viewOnly = !viewOnly },
                    )
                    DropdownMenuItem(
                        text = { Text("Immersive") },
                        leadingIcon = {
                            Checkbox(checked = immersive, onCheckedChange = { immersive = it })
                        },
                        onClick = { immersive = !immersive },
                    )
                    DropdownMenuItem(
                        text = { Text("Quality: ${qualityKbps / 1000} Mbps ▸") },
                        onClick = { qualityOpen = true },
                    )
                    DropdownMenu(expanded = qualityOpen, onDismissRequest = { qualityOpen = false }) {
                        // (kbps, maxH) — the low presets also cap resolution so
                        // relay paths (Tailscale DERP) aren't flooded by huge
                        // 4K keyframes
                        listOf(
                            Triple(1000, 720, "Very low (1 Mbps · 720p)"),
                            Triple(2000, 1080, "Low (2 Mbps · 1080p)"),
                            Triple(8000, 0, "Balanced (8 Mbps)"),
                            Triple(20000, 0, "High (20 Mbps)"),
                        ).forEach { (kbps, maxH, label) ->
                            val active = qualityKbps == kbps && qualityMaxH == maxH
                            DropdownMenuItem(
                                text = { Text(if (active) "● $label" else label) },
                                onClick = {
                                    qualityOpen = false
                                    overflowOpen = false
                                    qualityKbps = kbps
                                    qualityMaxH = maxH
                                    client.setQuality(kbps, maxH)
                                })
                        }
                    }
                }
            }
            IconButton(onClick = {
                userExit = true
                client.close("user exit")
                onExit()
            }) {
                Icon(Icons.Filled.Close, "disconnect", tint = Color(0xFFE0A0A0))
            }
        }

        // zoom reset — visible only while actually zoomed in
        if (zoom > 1.05f) {
            IconButton(
                onClick = { zoom = 1f; pan = Offset.Zero },
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(16.dp),
            ) {
                Icon(Icons.Filled.CenterFocusStrong, "reset zoom", tint = Color.White)
            }
        }

        // reconnect overlay — dims the frozen video while offline
        if (!connected && !userExit && !authFailed) {
            Box(
                Modifier.fillMaxSize().background(Color(0x88000000)),
                contentAlignment = Alignment.Center,
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.padding(24.dp),
                ) {
                    if (reconnects < MAX_RECONNECTS) {
                        CircularProgressIndicator(color = Color.White)
                        Spacer(Modifier.height(16.dp))
                        Text(status, color = Color.White,
                            style = MaterialTheme.typography.bodyMedium)
                    } else {
                        Text(status, color = Color(0xFFE0C060),
                            style = MaterialTheme.typography.bodyMedium)
                        Spacer(Modifier.height(20.dp))
                        Row {
                            Button(onClick = {
                                reconnects = 0
                                status = "reconnecting..."
                                val old = client
                                client = Client(spec.host, spec.port, spec.pin)
                                old.close("retry")
                            }) { Text("Retry") }
                            Spacer(Modifier.width(16.dp))
                            OutlinedButton(onClick = {
                                userExit = true
                                client.close("user exit")
                                onExit()
                            }) { Text("Exit") }
                        }
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------- sheets
    if (showKeyboard) {
        ModalBottomSheet(onDismissRequest = { showKeyboard = false }) {
            KeyboardSheet(client = client, latches = latches)
        }
    }
    if (showFiles) {
        ModalBottomSheet(onDismissRequest = { showFiles = false }) {
            FilesSheet(client = client, downloads = downloads)
        }
    }
}

private fun closeQuietly(out: OutputStream?) {
    try { out?.close() } catch (_: Exception) {}
}

/** Open a stream under public Downloads; MediaStore on 29+, legacy path below. */
private fun openDownload(context: Context, name: String): OutputStream {
    val safe = name.replace(Regex("[/\\\\]"), "_").ifBlank { "download.bin" }
    return if (Build.VERSION.SDK_INT >= 29) {
        val values = android.content.ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, safe)
            put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
        }
        val uri = context.contentResolver.insert(
            MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw IllegalStateException("MediaStore insert failed")
        context.contentResolver.openOutputStream(uri)
            ?: throw IllegalStateException("openOutputStream failed")
    } else {
        @Suppress("DEPRECATION")
        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        dir.mkdirs()
        var f = File(dir, safe)
        var i = 1
        while (f.exists()) {
            f = File(dir, safe.replace(".", " ($i)."))
            i++
        }
        f.outputStream()
    }
}

/** Counts frames + bytes for the stats overlay. */
class FrameCounter {
    private var frames = 0
    private var bytes = 0
    var fps = 0
    var kbps = 0

    fun onFrame(size: Int) {
        frames++
        bytes += size
    }

    fun tick() {
        fps = frames
        kbps = (bytes * 8) / 1000
        frames = 0
        bytes = 0
    }
}

// ================================================================ gestures
/** Shared gate so tap detection and the drag/scroll loop don't double-fire. */
private class GestureGate {
    @Volatile var multiTouch = false
    @Volatile var dragging = false

    fun reset() {
        multiTouch = false
        dragging = false
    }
}

@Composable
private fun GestureLayer(
    modifier: Modifier,
    client: Client,
    streamId: Int,
    streamW: Int,
    streamH: Int,
    enabled: Boolean,
    zoomProvider: () -> Float,
    panProvider: () -> Offset,
    onZoom: (Float) -> Unit,
    onPan: (Offset) -> Unit,
) {
    val gate = remember { GestureGate() }
    modifier
        .pointerInput(streamId, enabled) {
            // size is read per-event so rotation / layout changes stay correct
            detectTapGestures(
                onTap = { pos ->
                    if (enabled && !gate.multiTouch && !gate.dragging) {
                        val n = toNormIn(size.width, size.height, pos, streamW, streamH, zoomProvider, panProvider)
                        client.sendMotion(streamId, n.x.toDouble(), n.y.toDouble())
                        client.sendButton("left", true)
                        client.sendButton("left", false)
                    }
                    gate.reset()
                },
                onDoubleTap = { pos ->
                    if (enabled && !gate.multiTouch && !gate.dragging) {
                        val n = toNormIn(size.width, size.height, pos, streamW, streamH, zoomProvider, panProvider)
                        client.sendMotion(streamId, n.x.toDouble(), n.y.toDouble())
                        repeat(2) {
                            client.sendButton("left", true)
                            client.sendButton("left", false)
                        }
                    }
                    gate.reset()
                },
                onLongPress = { pos ->
                    if (enabled && !gate.multiTouch && !gate.dragging) {
                        val n = toNormIn(size.width, size.height, pos, streamW, streamH, zoomProvider, panProvider)
                        client.sendMotion(streamId, n.x.toDouble(), n.y.toDouble())
                        client.sendButton("right", true)
                        client.sendButton("right", false)
                    }
                    gate.reset()
                },
            )
        }
        .pointerInput(streamId, enabled) {
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false)
                var dragging = false
                var lastCentroid = down.position
                var scrollAccum = 0f
                var zoomRunning = zoomProvider()
                var panRunning = panProvider()
                // two-finger mode locks after a short dead zone: scroll unless
                // the gesture clearly spreads (zoom) — never both at once
                var twoMode = 0       // 0 undecided, 1 scroll, 2 zoom/pan
                var zoomAccum = 1f

                while (true) {
                    val event = awaitPointerEvent(PointerEventPass.Main)
                    val pressed = event.changes.filter { it.pressed }
                    if (pressed.isEmpty()) break

                    if (pressed.size >= 2) {
                        gate.multiTouch = true
                        if (dragging) {
                            client.sendButton("left", false)
                            dragging = false
                        }
                        val centroid = event.calculateCentroid(useCurrent = true)
                        val zoomChange = event.calculateZoom()
                        zoomAccum *= zoomChange
                        if (twoMode == 0) {
                            if (abs(zoomAccum - 1f) > 0.10f) {
                                twoMode = 2
                            } else if (abs(scrollAccum) > 40f) {
                                twoMode = 1
                            }
                        }
                        val delta = centroid - lastCentroid
                        lastCentroid = centroid
                        when (twoMode) {
                            2 -> {
                                zoomRunning *= zoomChange
                                onZoom(zoomRunning)
                                zoomRunning = zoomProvider()  // sync to coerced value
                                panRunning += delta
                                panRunning = clampPan(panRunning, size.width, size.height,
                                                      streamW, streamH, zoomRunning)
                                onPan(panRunning)
                            }
                            1 -> {
                                scrollAccum += delta.y
                                val step = 80f
                                while (scrollAccum <= -step) {
                                    if (enabled) client.sendScroll(0, -1)
                                    scrollAccum += step
                                }
                                while (scrollAccum >= step) {
                                    if (enabled) client.sendScroll(0, 1)
                                    scrollAccum -= step
                                }
                            }
                            else -> scrollAccum += delta.y  // still undecided
                        }
                        continue
                    }

                    val change = pressed.first()
                    if (gate.multiTouch) continue // wait for all fingers up

                    val slop = viewConfiguration.touchSlop
                    if (enabled && !dragging &&
                        (change.position - down.position).getDistance() > slop) {
                        dragging = true
                        gate.dragging = true
                        val n = toNormIn(size.width, size.height, change.position, streamW, streamH, zoomProvider, panProvider)
                        client.sendMotion(streamId, n.x.toDouble(), n.y.toDouble())
                        client.sendButton("left", true)
                    } else if (dragging) {
                        val n = toNormIn(size.width, size.height, change.position, streamW, streamH, zoomProvider, panProvider)
                        client.sendMotion(streamId, n.x.toDouble(), n.y.toDouble())
                    }
                }
                if (dragging) {
                    client.sendButton("left", false)
                }
                gate.reset()
            }
        }
}

/** Keep the zoomed video from being dragged fully off-screen. */
private fun clampPan(p: Offset, viewW: Int, viewH: Int,
                     streamW: Int, streamH: Int, zoom: Float): Offset {
    if (streamW <= 0 || streamH <= 0) return p
    val fit = minOf(viewW.toFloat() / streamW, viewH.toFloat() / streamH)
    val scale = fit * zoom
    val maxX = maxOf(0f, (streamW * scale - viewW) / 2f)
    val maxY = maxOf(0f, (streamH * scale - viewH) / 2f)
    return Offset(p.x.coerceIn(-maxX, maxX), p.y.coerceIn(-maxY, maxY))
}

/** Map a view position to normalized stream coordinates under the transform. */
private fun toNormIn(
    viewW: Int,
    viewH: Int,
    p: Offset,
    streamW: Int,
    streamH: Int,
    zoomProvider: () -> Float,
    panProvider: () -> Offset,
): Offset {
    val fit = minOf(viewW.toFloat() / streamW, viewH.toFloat() / streamH)
    val scale = fit * zoomProvider()
    val drawW = streamW * scale
    val drawH = streamH * scale
    val offX = (viewW - drawW) / 2f + panProvider().x
    val offY = (viewH - drawH) / 2f + panProvider().y
    val nx = ((p.x - offX) / drawW).coerceIn(0f, 1f)
    val ny = ((p.y - offY) / drawH).coerceIn(0f, 1f)
    return Offset(nx, ny)
}

// ================================================================ keyboard
/** Sticky modifier latches, one set per session. */
private class Latches {
    val ctrl = mutableStateOf(false)
    val alt = mutableStateOf(false)
    val shift = mutableStateOf(false)
    val sup = mutableStateOf(false)

    fun pressed(): List<Int> = listOf(
        ctrl to Modifiers.CTRL, alt to Modifiers.ALT,
        shift to Modifiers.SHIFT, sup to Modifiers.SUPER,
    ).filter { it.first.value }.map { it.second }

    fun clear() {
        ctrl.value = false; alt.value = false
        shift.value = false; sup.value = false
    }
}

@Composable
private fun KeyboardSheet(client: Client, latches: Latches) {
    var imeText by remember { mutableStateOf("") }

    Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp)) {
        OutlinedTextField(
            value = imeText,
            onValueChange = { fresh ->
                if (fresh.length > imeText.length && fresh.startsWith(imeText)) {
                    client.sendText(fresh.substring(imeText.length))
                } else if (fresh.length < imeText.length && imeText.startsWith(fresh)) {
                    repeat(imeText.length - fresh.length) {
                        client.sendKey(14, true)
                        client.sendKey(14, false)
                    }
                }
                imeText = if (fresh.length > 200) "" else fresh
            },
            label = { Text("Type here — text goes to the host") },
            modifier = Modifier.fillMaxWidth(),
        )

        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier.fillMaxWidth()) {
            LatchChip("Ctrl", latches, Modifiers.CTRL)
            LatchChip("Alt", latches, Modifiers.ALT)
            LatchChip("Shift", latches, Modifiers.SHIFT)
            LatchChip("Super", latches, Modifiers.SUPER)
        }
        Spacer(Modifier.height(6.dp))

        listOf(SPECIAL_KEYS_ROW_1, SPECIAL_KEYS_ROW_2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp),
                modifier = Modifier.fillMaxWidth()) {
                row.forEach { key ->
                    TextButton(
                        onClick = {
                            key.evdev?.let { code ->
                                val pressed = latches.pressed()
                                pressed.forEach { m -> client.sendKey(m, true) }
                                client.sendKey(code, true)
                                client.sendKey(code, false)
                                pressed.forEach { m -> client.sendKey(m, false) }
                                latches.clear()
                            }
                            key.text?.let { client.sendText(it) }
                        },
                    ) { Text(key.label) }
                }
            }
            Spacer(Modifier.height(4.dp))
        }
        TextButton(onClick = { imeText = "" }) { Text("Clear") }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun LatchChip(label: String, latches: Latches, evdev: Int) {
    val state = when (evdev) {
        Modifiers.CTRL -> latches.ctrl
        Modifiers.ALT -> latches.alt
        Modifiers.SHIFT -> latches.shift
        else -> latches.sup
    }
    TextButton(onClick = { state.value = !state.value }) {
        Text(if (state.value) "● $label" else label,
            color = if (state.value) MaterialTheme.colorScheme.primary
            else MaterialTheme.colorScheme.onSurface)
    }
}

// ================================================================== files
@Composable
private fun FilesSheet(client: Client, downloads: List<DownloadRow>) {
    val context = LocalContext.current
    var path by remember { mutableStateOf("~") }
    var entries by remember { mutableStateOf<List<FsEntry>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }

    fun refresh(p: String) {
        path = p
        client.fsList(p)
    }

    LaunchedEffect(Unit) {
        client.events.collect { event ->
            if (event is Event.FsList) {
                if (event.error == null) {
                    entries = event.entries
                    path = event.path
                    error = null
                } else {
                    error = event.error
                }
            }
        }
    }
    LaunchedEffect(Unit) { refresh("~") }

    val uploadPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            val resolver = context.contentResolver
            val name = uri.lastPathSegment?.substringAfterLast("/") ?: "upload.bin"
            val size = resolver.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1L
            val id = client.fsPut("~", name, size)
            Thread {
                try {
                    resolver.openInputStream(uri)?.use { input ->
                        val chunk = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(chunk)
                            if (n <= 0) break
                            client.sendFileChunk(id, if (n == chunk.size) chunk else chunk.copyOf(n))
                        }
                    }
                    client.fsPutEnd(id)
                } catch (e: Exception) {
                    android.util.Log.w(TAG, "upload failed: ${e.message}")
                }
            }.start()
        }
    }

    Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(path, style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.weight(1f))
            TextButton(onClick = { refresh("~") }) { Text("Home") }
            TextButton(onClick = { uploadPicker.launch(arrayOf("*/*")) }) {
                Icon(Icons.Filled.Upload, null, Modifier.width(18.dp))
                Text(" Upload")
            }
        }
        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall)
        }
        if (downloads.isNotEmpty()) {
            Text("Transfers", style = MaterialTheme.typography.labelMedium)
            downloads.takeLast(6).forEach { row ->
                Text(
                    "  ${row.status} ${row.name} " +
                        "(${row.received / 1024}/${row.size / 1024} KB)",
                    style = MaterialTheme.typography.bodySmall,
                    maxLines = 1,
                )
            }
            Spacer(Modifier.height(6.dp))
        }
        LazyColumn(Modifier.fillMaxWidth().height(400.dp)) {
            items(entries, key = { it.name + it.isDir }) { entry ->
                TextButton(onClick = {
                    val child = if (path == "~") "~/${entry.name}" else "$path/${entry.name}"
                    if (entry.isDir) refresh(child)
                    else client.fsGet(child)
                }) {
                    Icon(if (entry.isDir) Icons.Filled.Folder else Icons.Filled.Description,
                        null, Modifier.width(20.dp),
                        tint = if (entry.isDir) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                    Text(
                        if (entry.isDir) "  ${entry.name}/"
                        else "  ${entry.name}  (${entry.size / 1024} KB)",
                        modifier = Modifier.weight(1f),
                        maxLines = 1,
                    )
                }
            }
            item {
                Text(
                    "Tap a file to download it to your Downloads folder",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(8.dp),
                )
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}
