package com.farmos.node.identity

import android.content.Context
import android.content.Context.MODE_PRIVATE
import android.content.SharedPreferences
import android.util.Log
import com.farmos.node.BuildConfig
import org.json.JSONArray
import org.json.JSONObject
import java.util.TreeSet

/**
 * Stores the device enrollment (device id, human id, encrypted node token) and the
 * API base URL. The node bearer token is encrypted at rest with an Android
 * Keystore-backed AES key (see [TokenCipher]); it is never stored in plaintext.
 *
 * The device NEVER holds any admin credentials. Enrollment is performed with a
 * short-lived one-time token supplied out-of-band by the admin console (QR /
 * deep link / manual paste). The console issues it; this app only consumes it.
 */
class DeviceIdentityStore(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("farmos-node", MODE_PRIVATE)
    // URL settings and enrollment-state reads must work before a signing operation.
    // AndroidKeyStore is only required when public-key material is accessed or a
    // payload is signed, so defer its initialization until one of those operations.
    private val keystore: AndroidKeyStoreManager by lazy { AndroidKeyStoreManager() }

    fun publicKeyPem(): String = keystore.publicKeyPem()

    fun signJsonObject(json: JSONObject): String = keystore.sign(canonicalize(json).toByteArray())

    /** Public canonical JSON (sorted keys, compact) matching Python json.dumps(..., sort_keys=True, separators=(",",":")). */
    fun canonicalJson(json: JSONObject): String = canonicalize(json)


    fun saveEnrollment(deviceId: String, humanId: String, nodeToken: String) {
        prefs.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_HUMAN_ID, humanId)
            .putString(KEY_NODE_TOKEN_ENC, TokenCipher.encrypt(nodeToken))
            .apply()
    }

    fun deviceId(): String? = prefs.getString(KEY_DEVICE_ID, null)
    fun humanId(): String? = prefs.getString(KEY_HUMAN_ID, null)

    /** Decrypts the stored node token; null if absent or undecryptable. */
    fun nodeToken(): String? {
        val enc = prefs.getString(KEY_NODE_TOKEN_ENC, null) ?: return null
        return TokenCipher.decrypt(enc)
    }

    fun isEnrolled(): Boolean = deviceId() != null && nodeToken() != null

    /**
     * API base URL. Defaults to the build-time HTTPS URL. A user/physical-phone
     * configured LAN address may override it (stored in prefs); 10.0.2.2 is
     * emulator-loopback only and is never used as a default for real devices.
     */
    fun apiBaseUrl(): String {
        val fromPrefs = prefs.getString(KEY_API_BASE, null)
        if (!fromPrefs.isNullOrBlank()) return fromPrefs
        return BuildConfig.FARMOS_API_BASE_URL.ifBlank { "https://farmos.local" }
    }

    fun setApiBase(url: String) {
        require(url.startsWith("https://") || BuildConfig.FARMOS_ALLOW_INSECURE_HTTP) {
            "Only HTTPS API URLs are permitted (cleartext allowed only in dev builds)."
        }
        prefs.edit().putString(KEY_API_BASE, url).apply()
    }

    private fun canonicalize(value: Any?): String = when (value) {
        null -> "null"
        is String -> jsonEscape(value)
        is Number, is Boolean -> value.toString()
        is JSONObject -> {
            val keys = TreeSet<String>()
            value.keys().forEachRemaining { keys.add(it) }
            keys.joinToString(prefix = "{", postfix = "}", separator = ",") { key ->
                "${jsonEscape(key)}:${canonicalize(value.get(key))}"
            }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") { idx ->
            canonicalize(value.get(idx))
        }
        else -> jsonEscape(value.toString())
    }

    /**
     * JSON string escaper that matches Python's json.dumps(sort_keys=True,
     * separators=(",",":")) used by the backend for signature canonicalization:
     * escapes ", \, and control chars; leaves '/' unescaped (unlike JSONObject.quote,
     * which escapes '/' to '\/'); non-ASCII is escaped as \uXXXX.
     */
    private fun jsonEscape(s: String): String {
        val out = StringBuilder()
        out.append('"')
        for (c in s) {
            when (c) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                '\u0008' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                else -> {
                    if (c < ' ') {
                        out.append("\\u%04x".format(c.code))
                    } else if (c.code > 0x7F) {
                        out.append("\\u%04x".format(c.code))
                    } else {
                        out.append(c)
                    }
                }
            }
        }
        out.append('"')
        return out.toString()
    }

    companion object {
        private const val TAG = "DeviceIdentityStore"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_HUMAN_ID = "human_id"
        private const val KEY_NODE_TOKEN_ENC = "node_token_enc"
        private const val KEY_API_BASE = "api_base"

        init {
            if (BuildConfig.FARMOS_ALLOW_INSECURE_HTTP) {
                Log.w(TAG, "Insecure HTTP mode ENABLED (dev only) — do not ship to production.")
            }
        }
    }
}
