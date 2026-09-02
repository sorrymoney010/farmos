package com.farmos.node.identity

import android.content.Context
import android.content.Context.MODE_PRIVATE
import android.content.SharedPreferences
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.TreeSet

/**
 * Stores the device enrollment (device id, human id, node token) and reads the
 * API base URL + dev-only HTTP flag from secure app settings.
 *
 * The device NEVER holds any admin credentials. Enrollment is performed with a
 * short-lived one-time token supplied out-of-band by the admin console (QR /
 * deep link / manual paste). The console issues it; this app only consumes it.
 */
class DeviceIdentityStore(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("farmos-node", MODE_PRIVATE)
    private val keystore = AndroidKeyStoreManager()

    fun publicKeyPem(): String = keystore.publicKeyPem()

    fun signJsonObject(json: JSONObject): String = keystore.sign(canonicalize(json).toByteArray())

    fun saveEnrollment(deviceId: String, humanId: String, nodeToken: String) {
        prefs.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_HUMAN_ID, humanId)
            .putString(KEY_NODE_TOKEN, nodeToken)
            .apply()
    }

    fun deviceId(): String? = prefs.getString(KEY_DEVICE_ID, null)
    fun humanId(): String? = prefs.getString(KEY_HUMAN_ID, null)
    fun nodeToken(): String? = prefs.getString(KEY_NODE_TOKEN, null)

    fun isEnrolled(): Boolean = deviceId() != null && nodeToken() != null

    /**
     * API base URL for real deployments. Defaults to HTTPS for safety.
     * Override via BuildConfig / remote config for LAN lab use.
     */
    fun apiBaseUrl(): String {
        val fromPrefs = prefs.getString(KEY_API_BASE, null)
        if (!fromPrefs.isNullOrBlank()) return fromPrefs
        return BuildConfig.FARMOS_API_BASE_URL.ifBlank { "https://farmos.local" }
    }

    /** Explicit, dev-only opt-in to cleartext HTTP (LAN lab only). */
    fun allowInsecureHttp(): Boolean = BuildConfig.FARMOS_ALLOW_INSECURE_HTTP

    fun setApiBase(url: String) {
        prefs.edit().putString(KEY_API_BASE, url).apply()
    }

    private fun canonicalize(value: Any?): String = when (value) {
        null -> "null"
        is String -> JSONObject.quote(value)
        is Number, is Boolean -> value.toString()
        is JSONObject -> {
            val keys = TreeSet<String>()
            value.keys().forEachRemaining { keys.add(it) }
            keys.joinToString(prefix = "{", postfix = "}", separator = ",") { key ->
                "${JSONObject.quote(key)}:${canonicalize(value.get(key))}"
            }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") { idx ->
            canonicalize(value.get(idx))
        }
        else -> JSONObject.quote(value.toString())
    }

    companion object {
        private const val TAG = "DeviceIdentityStore"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_HUMAN_ID = "human_id"
        private const val KEY_NODE_TOKEN = "node_token"
        private const val KEY_API_BASE = "api_base"

        init {
            if (BuildConfig.FARMOS_ALLOW_INSECURE_HTTP) {
                Log.w(TAG, "Insecure HTTP mode ENABLED (dev only) — do not ship to production.")
            }
        }
    }
}
