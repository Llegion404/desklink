package com.desklink.client.input

import android.view.KeyEvent
import com.desklink.client.net.Client

/** Android keycodes -> Linux evdev keycodes (host `{"type":"key"}`). */
object EvdevKeys {
    // hardware keyboard: letters + digits so Ctrl/Alt/Super combos work
    private val LETTERS = mapOf(
        KeyEvent.KEYCODE_A to 30, KeyEvent.KEYCODE_B to 48, KeyEvent.KEYCODE_C to 46,
        KeyEvent.KEYCODE_D to 32, KeyEvent.KEYCODE_E to 18, KeyEvent.KEYCODE_F to 33,
        KeyEvent.KEYCODE_G to 34, KeyEvent.KEYCODE_H to 35, KeyEvent.KEYCODE_I to 23,
        KeyEvent.KEYCODE_J to 36, KeyEvent.KEYCODE_K to 37, KeyEvent.KEYCODE_L to 38,
        KeyEvent.KEYCODE_M to 50, KeyEvent.KEYCODE_N to 49, KeyEvent.KEYCODE_O to 24,
        KeyEvent.KEYCODE_P to 25, KeyEvent.KEYCODE_Q to 16, KeyEvent.KEYCODE_R to 19,
        KeyEvent.KEYCODE_S to 31, KeyEvent.KEYCODE_T to 20, KeyEvent.KEYCODE_U to 22,
        KeyEvent.KEYCODE_V to 47, KeyEvent.KEYCODE_W to 17, KeyEvent.KEYCODE_X to 45,
        KeyEvent.KEYCODE_Y to 21, KeyEvent.KEYCODE_Z to 44,
    )

    private val DIGITS = mapOf(
        KeyEvent.KEYCODE_1 to 2, KeyEvent.KEYCODE_2 to 3, KeyEvent.KEYCODE_3 to 4,
        KeyEvent.KEYCODE_4 to 5, KeyEvent.KEYCODE_5 to 6, KeyEvent.KEYCODE_6 to 7,
        KeyEvent.KEYCODE_7 to 8, KeyEvent.KEYCODE_8 to 9, KeyEvent.KEYCODE_9 to 10,
        KeyEvent.KEYCODE_0 to 11,
    )

    val map = mapOf(
        KeyEvent.KEYCODE_ENTER to 28,
        KeyEvent.KEYCODE_NUMPAD_ENTER to 28,
        KeyEvent.KEYCODE_DEL to 14,           // backspace
        KeyEvent.KEYCODE_FORWARD_DEL to 111,  // forward delete
        KeyEvent.KEYCODE_TAB to 15,
        KeyEvent.KEYCODE_ESCAPE to 1,
        KeyEvent.KEYCODE_INSERT to 110,
        KeyEvent.KEYCODE_MOVE_HOME to 102,
        KeyEvent.KEYCODE_MOVE_END to 107,
        KeyEvent.KEYCODE_PAGE_UP to 104,
        KeyEvent.KEYCODE_PAGE_DOWN to 109,
        KeyEvent.KEYCODE_DPAD_UP to 103,
        KeyEvent.KEYCODE_DPAD_DOWN to 108,
        KeyEvent.KEYCODE_DPAD_LEFT to 105,
        KeyEvent.KEYCODE_DPAD_RIGHT to 106,
        KeyEvent.KEYCODE_CTRL_LEFT to 29,
        KeyEvent.KEYCODE_CTRL_RIGHT to 97,
        KeyEvent.KEYCODE_ALT_LEFT to 56,
        KeyEvent.KEYCODE_ALT_RIGHT to 100,
        KeyEvent.KEYCODE_SHIFT_LEFT to 42,
        KeyEvent.KEYCODE_SHIFT_RIGHT to 54,
        KeyEvent.KEYCODE_META_LEFT to 125,
        KeyEvent.KEYCODE_META_RIGHT to 126,
        KeyEvent.KEYCODE_F1 to 59, KeyEvent.KEYCODE_F2 to 60,
        KeyEvent.KEYCODE_F3 to 61, KeyEvent.KEYCODE_F4 to 62,
        KeyEvent.KEYCODE_F5 to 63, KeyEvent.KEYCODE_F6 to 64,
        KeyEvent.KEYCODE_F7 to 65, KeyEvent.KEYCODE_F8 to 66,
        KeyEvent.KEYCODE_F9 to 67, KeyEvent.KEYCODE_F10 to 68,
        KeyEvent.KEYCODE_F11 to 87, KeyEvent.KEYCODE_F12 to 88,
        KeyEvent.KEYCODE_SPACE to 57,
        KeyEvent.KEYCODE_MINUS to 12,
        KeyEvent.KEYCODE_EQUALS to 13,
        KeyEvent.KEYCODE_GRAVE to 41,
    ).plus(LETTERS).plus(DIGITS)

    /** Send a hardware key event; returns true if the keycode was mapped. */
    fun send(client: Client, keyCode: Int, action: Int): Boolean {
        val evdev = map[keyCode] ?: return false
        val down = action == KeyEvent.ACTION_DOWN
        client.sendKey(evdev, down)
        return true
    }
}

/** Special-key row descriptors for the session keyboard sheet. */
data class SpecialKey(val label: String, val evdev: Int? = null,
                      val text: String? = null)

val SPECIAL_KEYS_ROW_1 = listOf(
    SpecialKey("Esc", evdev = 1),
    SpecialKey("Tab", evdev = 15),
    SpecialKey("←", evdev = 105),
    SpecialKey("↑", evdev = 103),
    SpecialKey("↓", evdev = 108),
    SpecialKey("→", evdev = 106),
    SpecialKey("Home", evdev = 102),
    SpecialKey("End", evdev = 107),
    SpecialKey("PgUp", evdev = 104),
    SpecialKey("PgDn", evdev = 109),
)

val SPECIAL_KEYS_ROW_2 = listOf(
    SpecialKey("F1", evdev = 59), SpecialKey("F2", evdev = 60),
    SpecialKey("F3", evdev = 61), SpecialKey("F4", evdev = 62),
    SpecialKey("F5", evdev = 63), SpecialKey("F6", evdev = 64),
    SpecialKey("F7", evdev = 65), SpecialKey("F8", evdev = 66),
    SpecialKey("F9", evdev = 67), SpecialKey("F10", evdev = 68),
    SpecialKey("F11", evdev = 87), SpecialKey("F12", evdev = 88),
)

/** Modifier latches (sticky Ctrl/Alt/Shift/Super). */
object Modifiers {
    const val CTRL = 29
    const val ALT = 56
    const val SHIFT = 42
    const val SUPER = 125
}
