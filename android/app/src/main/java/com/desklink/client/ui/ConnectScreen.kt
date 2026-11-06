package com.desklink.client.ui

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "desklink")
private val HOSTS_KEY = stringPreferencesKey("hosts_json")

/** Outlives ConnectScreen — a rememberCoroutineScope dies the moment we
 * navigate to SessionScreen, which raced the DataStore write and lost it. */
private val ioScope = kotlinx.coroutines.CoroutineScope(
    kotlinx.coroutines.SupervisorJob() + kotlinx.coroutines.Dispatchers.IO)

data class SavedHost(val name: String, val host: String, val port: Int, val pin: String)

/**
 * Enter host (host[:port]) + PIN; saved hosts below reconnect with one tap.
 * Accepts pasted "host:port" or "host:port:pin" strings via the paste button.
 */
@Composable
fun ConnectScreen(onConnect: (host: String, port: Int, pin: String) -> Unit) {
    var hostInput by rememberSaveable { mutableStateOf("") }
    var pin by rememberSaveable { mutableStateOf("") }
    var status by rememberSaveable { mutableStateOf<String?>(null) }
    var hosts by remember { mutableStateOf<List<SavedHost>>(emptyList()) }
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    val context = androidx.compose.ui.platform.LocalContext.current

    fun persistHosts(list: List<SavedHost>) {
        ioScope.launch {
            context.dataStore.edit { it[HOSTS_KEY] = serializeHosts(list) }
        }
    }

    fun rememberAndConnect(host: String, port: Int, pinValue: String) {
        val entry = SavedHost(host, host, port, pinValue)
        hosts = (listOf(entry) + hosts.filter { !(it.host == host && it.port == port) }).take(8)
        persistHosts(hosts)
        status = "Connecting to $host:$port..."
        onConnect(host, port, pinValue)
    }

    LaunchedEffect(Unit) {
        val prefs = context.dataStore.data.first()
        hosts = parseHosts(prefs[HOSTS_KEY])
        hosts.firstOrNull()?.let {
            hostInput = "${it.host}:${it.port}"
            pin = it.pin
        }
    }

    val parseTarget: (String) -> Triple<String, Int, String>? = { raw ->
        val parts = raw.trim().split(":")
        when {
            parts.size >= 3 ->
                Triple(parts[0], parts[1].toIntOrNull() ?: 9090, parts.drop(2).joinToString(":"))
            parts.size == 2 && parts[1].toIntOrNull() != null -> Triple(parts[0], parts[1].toInt(), pin)
            parts.size == 1 && raw.isNotBlank() -> Triple(parts[0], 9090, pin)
            else -> null
        }
    }

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(Icons.Filled.Computer, contentDescription = null,
            modifier = Modifier.height(72.dp).width(72.dp).padding(bottom = 8.dp),
            tint = MaterialTheme.colorScheme.primary)
        Text("DeskLink", style = MaterialTheme.typography.headlineMedium)
        Text("KDE Plasma remote control", style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp)) {
                OutlinedTextField(
                    value = hostInput,
                    onValueChange = { hostInput = it; status = null },
                    label = { Text("Host (ip or ip:port)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    trailingIcon = {
                        IconButton(onClick = {
                            clipboard.getText()?.text?.let { pasted ->
                                val parsed = parseTarget(pasted) ?: return@let
                                hostInput = "${parsed.first}:${parsed.second}"
                                if (parsed.third.isNotBlank()) pin = parsed.third
                            }
                        }) {
                            Icon(Icons.Filled.ContentPaste, "paste host:port[:pin]")
                        }
                    },
                )
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = pin,
                    onValueChange = { pin = it; status = null },
                    label = { Text("PIN") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(16.dp))
                Button(
                    onClick = {
                        val parsed = parseTarget(hostInput)
                        if (parsed == null) {
                            status = "Use host or host:port"
                        } else if (pin.isBlank()) {
                            status = "PIN required"
                        } else {
                            rememberAndConnect(parsed.first, parsed.second, pin)
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Connect") }
                if (status != null) {
                    Spacer(Modifier.height(12.dp))
                    Text(status!!, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        if (hosts.isNotEmpty()) {
            Spacer(Modifier.height(16.dp))
            Text("Saved hosts", style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.align(Alignment.Start))
            Spacer(Modifier.height(4.dp))
            LazyColumn(modifier = Modifier.fillMaxWidth()) {
                items(hosts, key = { "${it.host}:${it.port}" }) { h ->
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant),
                        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            TextButton(
                                onClick = {
                                    hostInput = "${h.host}:${h.port}"
                                    pin = h.pin
                                    rememberAndConnect(h.host, h.port, h.pin)
                                },
                                modifier = Modifier.weight(1f),
                            ) {
                                Column(Modifier.fillMaxWidth()) {
                                    Text(h.name, style = MaterialTheme.typography.titleSmall)
                                    Text("${h.host}:${h.port}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                            IconButton(onClick = {
                                hosts = hosts.filter { it != h }
                                persistHosts(hosts)
                            }) {
                                Icon(Icons.Filled.Close, "remove ${h.name}",
                                    tint = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        Text("Tip: run `python3 -m desklink status` on the host for the address",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ------------------------------------------------------------- persistence
private fun parseHosts(json: String?): List<SavedHost> {
    if (json.isNullOrBlank()) return emptyList()
    return try {
        val arr = JSONArray(json)
        (0 until arr.length()).mapNotNull { i ->
            val o = arr.getJSONObject(i)
            SavedHost(
                o.optString("name", o.optString("host")),
                o.getString("host"),
                o.optInt("port", 9090),
                o.optString("pin", ""),
            )
        }
    } catch (e: Exception) {
        emptyList()
    }
}

private fun serializeHosts(list: List<SavedHost>): String {
    val arr = JSONArray()
    list.forEach {
        arr.put(JSONObject().put("name", it.name).put("host", it.host)
            .put("port", it.port).put("pin", it.pin))
    }
    return arr.toString()
}
