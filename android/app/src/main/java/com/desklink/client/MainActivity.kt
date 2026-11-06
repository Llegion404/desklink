package com.desklink.client

import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.desklink.client.ui.ConnectScreen
import com.desklink.client.ui.SessionScreen

/** Connection parameters for one session (survives recomposition). */
data class ConnSpec(val host: String, val port: Int, val pin: String)

/**
 * Hardware-keyboard passthrough: SessionScreen installs a handler while a
 * session is active; MainActivity routes every physical key event through it
 * before the default IME/view handling.
 */
object KeyboardBridge {
    @Volatile
    var handler: ((keyCode: Int, action: Int, unicodeChar: Int, metaState: Int) -> Boolean)? = null
}

class MainActivity : ComponentActivity() {
    private var spec by mutableStateOf<ConnSpec?>(null)

    /** Boot spec from launch extras, e.g.
     *  am start -n com.desklink.client/.MainActivity \
     *    --es host "192.168.1.20:9090" --es pin "1234"
     * Cleared on session exit so the user lands on the connect screen. */
    private var boot by mutableStateOf<ConnSpec?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        boot = parseBootIntent(intent)
        setContent {
            val current = spec ?: boot
            if (current == null) {
                ConnectScreen { host, port, pin -> spec = ConnSpec(host, port, pin) }
            } else {
                SessionScreen(current) {
                    spec = null
                    boot = null
                }
            }
        }
    }

    private fun parseBootIntent(intent: android.content.Intent?): ConnSpec? {
        val hostStr = intent?.getStringExtra("host") ?: return null
        val pin = intent.getStringExtra("pin") ?: return null
        val parts = hostStr.split(":")
        val host: String
        val port: Int
        if (parts.size >= 2) {
            host = parts[0]
            port = parts[1].toIntOrNull() ?: 9090
        } else {
            host = hostStr
            port = 9090
        }
        return ConnSpec(host, port, pin)
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val handler = KeyboardBridge.handler
        if (handler != null && handler(event.keyCode, event.action,
                event.unicodeChar, event.metaState)) {
            return true
        }
        return super.dispatchKeyEvent(event)
    }
}
