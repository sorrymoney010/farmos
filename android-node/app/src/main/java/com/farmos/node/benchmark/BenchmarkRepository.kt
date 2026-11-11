package com.farmos.node.benchmark

import android.app.ActivityManager
import android.content.Context
import com.farmos.node.identity.DeviceIdentityStore
import com.farmos.node.safety.DeviceTelemetry
import com.farmos.node.safety.ThermalGovernor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * Runs [BenchmarkRunner] with real Android providers and submits the signed result:
 *
 * POST /api/v1/devices/{device_id}/benchmark/result
 *   body: suite_version, job_id?, raw_results_json, results_sha256, request_nonce,
 *         request_timestamp, signature
 *   signed canonical JSON: {device_id, job_id ("" if none), request_nonce,
 *         request_timestamp, results_sha256, suite_version}
 *
 * Signing the SHA-256 of the exact raw string avoids Kotlin/Python float
 * formatting differences breaking signature verification.
 */
class BenchmarkRepository(
    private val context: Context,
    private val identityStore: DeviceIdentityStore,
) {
    private val client by lazy { OkHttpClient.Builder().callTimeout(30, TimeUnit.SECONDS).build() }

    fun runner(): BenchmarkRunner {
        val app = context.applicationContext
        return BenchmarkRunner(
            cacheDir = app.cacheDir,
            temperatureProvider = {
                val s = DeviceTelemetry.read(app)
                ThermalGovernor.effectiveTemperature(s)
            },
            batteryProvider = {
                val s = DeviceTelemetry.read(app)
                mapOf(
                    "pct" to s.batteryPct,
                    "charging" to s.charging,
                    "health" to s.batteryHealth,
                    "temperature_c" to s.batteryTemperatureC,
                )
            },
            memoryTotalMb = {
                runCatching {
                    val am = app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                    ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }.totalMem.div(1_048_576L).toInt()
                }.getOrNull()
            },
            latencyProbeMs = { probeLatency() },
        )
    }

    private fun probeLatency(): Double? {
        val url = identityStore.apiBaseUrl().removeSuffix("/") + "/health"
        val samples = (1..3).mapNotNull {
            runCatching {
                val start = System.nanoTime()
                client.newCall(Request.Builder().url(url).get().build()).execute().use { it.isSuccessful }
                (System.nanoTime() - start) / 1e6
            }.getOrNull()
        }
        return samples.minOrNull()?.let { BenchmarkRunner.round1(it) }
    }

    /** Run + submit. Throws [BenchmarkRunner.Aborted] if the device is not safe to benchmark. */
    suspend fun runAndSubmit(jobId: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val deviceId = identityStore.deviceId() ?: error("Device not enrolled")
        val nodeToken = identityStore.nodeToken() ?: error("No node token")
        val raw = runner().run()
        val rawJson = SimpleJson.write(raw)
        val sha = BenchmarkRunner.sha256Hex(rawJson)
        val toSign = JSONObject()
            .put("request_nonce", UUID.randomUUID().toString().replace("-", ""))
            .put("request_timestamp", Instant.now().toString())
            .put("device_id", deviceId)
            .put("suite_version", BenchmarkRunner.SUITE_VERSION)
            .put("job_id", jobId ?: "")
            .put("results_sha256", sha)
        val body = JSONObject()
            .put("suite_version", BenchmarkRunner.SUITE_VERSION)
            .put("job_id", jobId ?: JSONObject.NULL)
            .put("raw_results_json", rawJson)
            .put("results_sha256", sha)
            .put("request_nonce", toSign.getString("request_nonce"))
            .put("request_timestamp", toSign.getString("request_timestamp"))
            .put("signature", identityStore.signJsonObject(toSign))
        val url = identityStore.apiBaseUrl().removeSuffix("/") + "/api/v1/devices/$deviceId/benchmark/result"
        val req = Request.Builder()
            .url(url)
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .header("Authorization", "Bearer $nodeToken")
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}: $text")
            JSONObject(text)
        }
    }
}
