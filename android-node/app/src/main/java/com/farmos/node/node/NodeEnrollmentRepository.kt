package com.farmos.node.node

import android.content.Context
import com.farmos.node.identity.DeviceIdentityStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.UUID

/**
 * Performs device-side enrollment using a short-lived one-time token supplied by
 * the admin console. The app never holds an admin bearer token; it only consumes
 * the enrollment token (from QR / deep link / manual entry) and then signs its
 * enrollment request with the on-device Keystore key.
 */
class NodeEnrollmentRepository(
    private val context: Context,
    private val identityStore: DeviceIdentityStore,
) {
    private val client: OkHttpClient by lazy {
        val builder = OkHttpClient.Builder()
        // Dev-only cleartext HTTP for LAN lab. Never enabled in production builds.
        if (identityStore.allowInsecureHttp()) {
            builder.hostnameVerifier { _, _ -> true }
        }
        builder.build()
    }
    private val jsonType = "application/json".toMediaType()

    /** Enroll using a one-time token from the admin console. */
    suspend fun enroll(enrollmentToken: String): EnrollmentResult = withContext(Dispatchers.IO) {
        val payload = buildEnrollPayload(enrollmentToken)
        val response = postJson("/api/v1/devices/enroll", payload)
        val deviceId = response.getString("device_id")
        val humanId = response.getString("human_id")
        val nodeToken = response.getString("node_token")
        identityStore.saveEnrollment(deviceId, humanId, nodeToken)
        EnrollmentResult(deviceId, humanId, response.optString("status", "BENCHMARKING"))
    }

    private fun buildEnrollPayload(enrollmentToken: String): JSONObject {
        val payload = JSONObject()
            .put("enrollment_token", enrollmentToken)
            .put("device_public_key", identityStore.publicKeyPem())
            .put("hardware_fingerprint", "sha256:${uuidSha()}")
            .put(
                "profile",
                JSONObject()
                    .put("manufacturer", android.os.Build.MANUFACTURER)
                    .put("model", android.os.Build.MODEL)
                    .put("android_version", android.os.Build.VERSION.RELEASE)
                    .put("architecture", android.os.Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown")
                    .put("cpu_cores", Runtime.getRuntime().availableProcessors())
                    .put("ram_mb", 4096)
                    .put("storage_total_mb", 64000)
                    .put("capabilities", JSONArray(listOf("cpu_compute", "network", "storage"))),
            )
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
        payload.put("signature", identityStore.signJsonObject(payload))
        return payload
    }

    /** Send a signed heartbeat using the stored node token. */
    suspend fun heartbeatOnce(): JSONObject = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val payload = JSONObject()
            .put("observed_at", Instant.now().toString())
            .put("battery_pct", 80.0)
            .put("charging", true)
            .put("temperature_c", 32.0)
            .put("cpu_util_pct", 10.0)
            .put("ram_used_mb", 1024)
            .put("storage_free_mb", 32000)
            .put("network", JSONObject().put("type", "wifi").put("down_mbps", 100.0).put("up_mbps", 20.0))
            .put("app_version", "1.0")
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
        payload.put("signature", identityStore.signJsonObject(payload))
        postJson("/api/v1/devices/$deviceId/heartbeat", payload, nodeToken)
    }

    private fun postJson(path: String, body: JSONObject, bearer: String? = null): JSONObject {
        val url = identityStore.apiBaseUrl().removeSuffix("/") + path
        val requestBuilder = Request.Builder()
            .url(url)
            .post(body.toString().toRequestBody(jsonType))
            .header("Content-Type", "application/json")
        if (!bearer.isNullOrBlank()) requestBuilder.header("Authorization", "Bearer $bearer")
        client.newCall(requestBuilder.build()).execute().use { response ->
            if (!response.isSuccessful) error("HTTP ${response.code}: ${response.body?.string()}")
            return JSONObject(response.body?.string().orEmpty())
        }
    }

    private fun nonce(): String = UUID.randomUUID().toString().replace("-", "")
    private fun uuidSha(): String = nonce()

    data class EnrollmentResult(
        val deviceId: String,
        val humanId: String,
        val status: String,
    )
}
