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
    // No custom hostnameVerifier: HTTPS certificate validation is always enforced.
    // Cleartext (lab only) is permitted solely via network-security-config for the
    // configured LAN host; OkHttp still refuses cleartext anywhere else.
    private val client: OkHttpClient by lazy { OkHttpClient.Builder().build() }
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

    /**
     * Wireless onboarding: exchange a short claim/pairing code for an enrollment
     * token over the network, then enroll. No USB/ADB required for the claim step.
     * Staging may accept fixed code "123" when STAGING_CLAIM_CODES is enabled.
     */
    suspend fun enrollWithClaimCode(claimCode: String): EnrollmentResult = withContext(Dispatchers.IO) {
        val exchangeBody = JSONObject().put("claim_code", claimCode.trim())
        val exchange = postJson("/api/v1/devices/claim-code/exchange", exchangeBody)
        val enrollmentToken = exchange.getString("enrollment_token")
        enroll(enrollmentToken)
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
        android.util.Log.i(" FarmosHb", "heartbeatOnce: deviceId=$deviceId nodeToken=${nodeToken.take(8)}...")
        // Build the body that will be sent (includes extra fields for the API).
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
        // The backend verifies the signature over the HeartbeatRequest model fields
        // via model_dump(mode="json", exclude={"signature"}) — which includes ALL
        // fields except signature: observed_at, battery_pct, charging, temperature_c,
        // cpu_util_pct, ram_used_mb, storage_free_mb, network, app_version,
        // request_nonce, request_timestamp. So we sign the FULL payload minus the
        // signature field itself (which is exactly what canonicalize does on the
        // payload as built above — same keys, same values, sorted).
        payload.put("signature", identityStore.signJsonObject(payload))
        android.util.Log.i(" FarmosHb", "heartbeatOnce: POST $deviceId/heartbeat")
        val result = postJson("/api/v1/devices/$deviceId/heartbeat", payload, nodeToken)
        android.util.Log.i(" FarmosHb", "heartbeatOnce: OK ${result.optString("status","?")}")
        result
    }

    private fun postJson(path: String, body: JSONObject, bearer: String? = null): JSONObject {
        try {
            val url = identityStore.apiBaseUrl().removeSuffix("/") + path
            android.util.Log.d(" FarmosHb", "postJson: $url bearer=${bearer?.take(8)}...")
            val requestBuilder = Request.Builder()
                .url(url)
                .post(body.toString().toRequestBody(jsonType))
                .header("Content-Type", "application/json")
            if (!bearer.isNullOrBlank()) requestBuilder.header("Authorization", "Bearer $bearer")
            android.util.Log.d(" FarmosHb", "postJson: calling execute, dispatching...")
            val call = client.newCall(requestBuilder.build())
            android.util.Log.d(" FarmosHb", "postJson: call created, executing synchronously...")
            call.execute().use { response ->
                android.util.Log.d(" FarmosHb", "postJson: got response, code=${response.code}, isSuccessful=${response.isSuccessful}")
                val bodyStr = response.body?.string().orEmpty()
                android.util.Log.d(" FarmosHb", "postJson: HTTP ${response.code} body=$bodyStr")
                if (!response.isSuccessful) {
                    val errMsg = "HTTP ${response.code}: $bodyStr"
                    android.util.Log.e(" FarmosHb", "postJson: server error: $errMsg")
                    throw IllegalStateException(errMsg)
                }
                return JSONObject(bodyStr)
            }
        } catch (e: Exception) {
            android.util.Log.e(" FarmosHb", "postJson FAILED: ${e.javaClass.name}: ${e.message}", e)
            throw e
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