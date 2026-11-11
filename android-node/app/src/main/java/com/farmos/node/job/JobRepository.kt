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
 * - POST /api/v1/jobs/device/jobs/{id}/lease/renew (signed: nonce, ts, job_id, device_id, lease_id)
 *
 * Phase 2: every dispatch carries a lease (spec §71). The device renews it while
 * a job runs; results are idempotent (same result_hash -> same outcome). The
 * result is sent as canonical JSON ([ResultJson]) and hashed over exactly those
 * bytes so the server can recompute the hash.
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
        val lease: Lease? = null,
    )

    data class Lease(val leaseId: String, val expiresAt: String?, val renewEverySeconds: Long)

    /** HTTP error with status code so callers can treat 409 (lease lost / conflict) specially. */
    class ApiException(val code: Int, body: String) : IllegalStateException("HTTP $code: $body")

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
            lease = parseLease(job.optJSONObject("lease") ?: response.optJSONObject("lease")),
        )
    }

    /** Accepts a job; returns the (runtime) lease from the server, or null for legacy servers. */
    suspend fun acceptJob(jobId: String): Lease? = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val toSign = JSONObject()
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
            .put("job_id", jobId)
            .put("device_id", deviceId)
        val body = JSONObject(toSign.toString())
            .put("signature", identityStore.signJsonObject(toSign))
        val resp = postJson("/api/v1/jobs/device/jobs/$jobId/accept", body, nodeToken)
        parseLease(resp.optJSONObject("lease"))
    }

    /** Extends a running job's lease; returns the updated lease or null if it was lost. */
    suspend fun renewLease(jobId: String, leaseId: String): Lease? = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val toSign = JSONObject()
            .put("request_nonce", nonce())
            .put("request_timestamp", Instant.now().toString())
            .put("job_id", jobId)
            .put("device_id", deviceId)
            .put("lease_id", leaseId)
        val body = JSONObject()
            .put("request_nonce", toSign.getString("request_nonce"))
            .put("request_timestamp", toSign.getString("request_timestamp"))
            .put("lease_id", leaseId)
            .put("signature", identityStore.signJsonObject(toSign))
        try {
            parseLease(postJson("/api/v1/jobs/device/jobs/$jobId/lease/renew", body, nodeToken).optJSONObject("lease"))
        } catch (e: ApiException) {
            if (e.code == 409 || e.code == 404) null else throw e
        }
    }

    suspend fun executeJob(job: PolledJob): JSONObject = withContext(Dispatchers.Default) {
        when (job.workloadType) {
            "http_check", "HTTP_CHECK" -> withContext(Dispatchers.IO) { HttpCheckExecutor.execute(job.payload) }
            in CpuDataExecutor.WORKLOADS -> CpuDataExecutor.execute(job.workloadType, job.payload)
            else -> JSONObject()
                .put("status", "FAILED")
                .put("error", "Unsupported workload_type: ${job.workloadType}")
                .put("duration_ms", 0)
                .put("bytes_transferred", 0)
        }
    }

    /**
     * Submits a result. Returns the server response (status + verification), or
     * null when the server rejected it with 409 (lease expired / conflicting result).
     */
    suspend fun submitResult(jobId: String, result: JSONObject): JSONObject? = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val humanId = identityStore.humanId().orEmpty()

        // result_uri IS the canonical JSON; result_hash = sha256(result_uri).
        val resultUri = ResultJson.canonical(result)
        val resultHash = sha256Hex(resultUri)

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
        try {
            postJson("/api/v1/jobs/device/jobs/$jobId/result", body, nodeToken)
        } catch (e: ApiException) {
            if (e.code == 409) null else throw e
        }
    }

    private fun parseLease(obj: JSONObject?): Lease? {
        if (obj == null) return null
        val id = obj.optString("lease_id").ifBlank { return null }
        return Lease(
            leaseId = id,
            expiresAt = obj.optString("expires_at").ifBlank { null },
            renewEverySeconds = obj.optLong("renew_every_seconds", 60L).coerceIn(15L, 600L),
        )
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
                throw ApiException(response.code, bodyStr.take(500))
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

        fun submitHint(jobId: String, status: String, resp: JSONObject?): String {
            val short = jobId.take(8)
            if (resp == null) return "Jobs: last $short rejected (lease expired / conflict) · no credit"
            val v = resp.optJSONObject("verification")
            val vs = v?.optString("verification_status").orEmpty()
            return when {
                vs == "PASSED" && v?.optBoolean("test_earnings_credited") == true ->
                    "Jobs: last $short VERIFIED · test credit posted"
                vs == "PASSED" -> "Jobs: last $short VERIFIED"
                vs == "PENDING" -> "Jobs: last $short submitted · verifying (redundant check)"
                vs == "FAILED" -> "Jobs: last $short FAILED verification · ${v?.optString("reason")?.take(60)}"
                else -> "Jobs: last $short $status · ${resp.optString("status")}"
            }
        }

        /** Fields the backend verifies for lease renewal. */
        fun renewSignKeys(): List<String> =
            listOf("device_id", "job_id", "lease_id", "request_nonce", "request_timestamp")

        /** Fields the backend verifies for result submit. */
        fun resultSignKeys(): List<String> =
            listOf("device_id", "job_id", "request_nonce", "request_timestamp", "result_hash", "result_uri")
    }
}
