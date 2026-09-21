package com.farmos.node.job

import android.content.Context
import com.farmos.node.identity.DeviceIdentityStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

/**
 * Device job API client matching Python [backend/app/agent/node_agent.py] and
 * [backend/app/api/jobs.py] contracts:
 *
 * - POST /api/v1/jobs/device/jobs/next          (Bearer node token, empty body)
 * - POST /api/v1/jobs/device/jobs/{id}/accept   (signed: nonce, ts, job_id, device_id)
 * - POST /api/v1/jobs/device/jobs/{id}/result   (signed: nonce, ts, job_id, device_id, result_hash, result_uri)
 *
 * Signatures cover ONLY the backend payload_to_verify fields (not execution_metrics).
 */
class JobRepository(
    private val context: Context,
    private val identityStore: DeviceIdentityStore,
) {
    private val client: OkHttpClient by lazy { OkHttpClient.Builder().build() }
    private val jsonType = "application/json".toMediaType()

    data class PolledJob(
        val jobId: String,
        val workloadType: String,
        val payload: JSONObject,
        val maxRuntimeSeconds: Int?,
    )

    suspend fun pollNextJob(): PolledJob? = withContext(Dispatchers.IO) {
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val response = postJson("/api/v1/jobs/device/jobs/next", JSONObject(), nodeToken)
        if (response.isNull("job")) return@withContext null
        val job = response.optJSONObject("job") ?: return@withContext null
        PolledJob(
            jobId = job.getString("job_id"),
            workloadType = job.optString("workload_type", "http_check"),
            payload = job.optJSONObject("payload") ?: JSONObject(),
            maxRuntimeSeconds = if (job.has("max_runtime_seconds") && !job.isNull("max_runtime_seconds")) {
                job.getInt("max_runtime_seconds")
            } else {
                null
            },
        )
    }

    suspend fun acceptJob(jobId: String): Boolean = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val toSign = JSONObject()
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
            .put("job_id", jobId)
            .put("device_id", deviceId)
        val body = JSONObject(toSign.toString())
            .put("signature", identityStore.signJsonObject(toSign))
        postJson("/api/v1/jobs/device/jobs/$jobId/accept", body, nodeToken)
        true
    }

    suspend fun executeJob(job: PolledJob): JSONObject = withContext(Dispatchers.IO) {
        when (job.workloadType) {
            "http_check", "HTTP_CHECK" -> HttpCheckExecutor.execute(job.payload)
            else -> JSONObject()
                .put("status", "FAILED")
                .put("error", "Unsupported workload_type: ${job.workloadType}")
                .put("duration_ms", 0)
                .put("bytes_transferred", 0)
        }
    }

    suspend fun submitResult(jobId: String, result: JSONObject): Boolean = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val humanId = identityStore.humanId().orEmpty()

        val canonical = identityStore.canonicalJson(result)
        val resultHash = sha256Hex(canonical)
        // Backend stores result_uri as the JSON string of the result payload.
        val resultUri = result.toString()

        // Sign ONLY the fields jobs.py puts in payload_to_verify (exclude execution_metrics).
        val toSign = JSONObject()
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
            .put("job_id", jobId)
            .put("device_id", deviceId)
            .put("result_hash", resultHash)
            .put("result_uri", resultUri)
        val metrics = JSONObject()
            .put("device_id", deviceId)
            .put("device_human_id", humanId)
            .put("executed_at", Instant.now().toString())
            .put("workload_status", result.optString("status", "UNKNOWN"))
        val body = JSONObject(toSign.toString())
            .put("signature", identityStore.signJsonObject(toSign))
            .put("execution_metrics", metrics)
        postJson("/api/v1/jobs/device/jobs/$jobId/result", body, nodeToken)
        true
    }

    private fun postJson(path: String, body: JSONObject, bearer: String): JSONObject {
        val url = identityStore.apiBaseUrl().removeSuffix("/") + path
        val request = Request.Builder()
            .url(url)
            .post(body.toString().toRequestBody(jsonType))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer $bearer")
            .build()
        client.newCall(request).execute().use { response ->
            val bodyStr = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw IllegalStateException("HTTP ${response.code}: $bodyStr")
            }
            return if (bodyStr.isBlank()) JSONObject() else JSONObject(bodyStr)
        }
    }

    private fun nonce(): String = UUID.randomUUID().toString().replace("-", "")

    companion object {
        fun sha256Hex(canonicalJson: String): String {
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(canonicalJson.toByteArray(Charsets.UTF_8))
            return digest.joinToString("") { b -> "%02x".format(b) }
        }

        /** Fields the backend verifies for accept (sorted for docs/tests). */
        fun acceptSignKeys(): List<String> =
            listOf("device_id", "job_id", "request_nonce", "request_timestamp")

        /** Fields the backend verifies for result submit. */
        fun resultSignKeys(): List<String> =
            listOf("device_id", "job_id", "request_nonce", "request_timestamp", "result_hash", "result_uri")
    }
}
