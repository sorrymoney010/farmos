package com.farmos.node.job

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * MVP job executor for workload_type=http_check.
 *
 * Result shape matches backend verification in job_executor._verify_http_check:
 * status=COMPLETED, http_status in 100..599, plus latency/body preview fields.
 */
object HttpCheckExecutor {
    private val jsonType = "application/json".toMediaType()

    fun execute(payload: JSONObject, client: OkHttpClient = defaultClient(payload)): JSONObject {
        val url = payload.optString("url").ifBlank { "https://example.com" }
        val method = payload.optString("method", "GET").uppercase().ifBlank { "GET" }
        val timeoutSec = payload.optDouble("timeout", 30.0).coerceIn(1.0, 120.0)
        val headersObj = payload.optJSONObject("headers")

        val httpClient = if (client === shared) {
            shared.newBuilder()
                .callTimeout(timeoutSec.toLong().coerceAtLeast(1), TimeUnit.SECONDS)
                .connectTimeout(timeoutSec.toLong().coerceAtLeast(1), TimeUnit.SECONDS)
                .readTimeout(timeoutSec.toLong().coerceAtLeast(1), TimeUnit.SECONDS)
                .build()
        } else {
            client
        }

        return try {
            val builder = Request.Builder().url(url)
            if (headersObj != null) {
                val keys = headersObj.keys()
                while (keys.hasNext()) {
                    val key = keys.next()
                    builder.header(key, headersObj.optString(key))
                }
            }
            val bodyRaw = payload.opt("body")
            val requestBody = when {
                method == "GET" || method == "HEAD" -> null
                bodyRaw == null || bodyRaw == JSONObject.NULL -> ByteArray(0).toRequestBody(null)
                bodyRaw is JSONObject || bodyRaw is org.json.JSONArray ->
                    bodyRaw.toString().toRequestBody(jsonType)
                else -> bodyRaw.toString().toRequestBody("text/plain".toMediaType())
            }
            builder.method(method, requestBody)

            val startNs = System.nanoTime()
            httpClient.newCall(builder.build()).execute().use { response ->
                val durationMs = (System.nanoTime() - startNs) / 1_000_000.0
                val preview = (response.body?.string().orEmpty()).take(1024)
                JSONObject()
                    .put("status", "COMPLETED")
                    .put("http_status", response.code)
                    .put("duration_ms", (Math.round(durationMs * 100.0) / 100.0))
                    .put("bytes_transferred", preview.toByteArray(Charsets.UTF_8).size)
                    .put("response_preview", preview)
                    .put("url", url)
                    .put("method", method)
            }
        } catch (e: Exception) {
            JSONObject()
                .put("status", "FAILED")
                .put("error", e.message ?: e.javaClass.simpleName)
                .put("duration_ms", 0)
                .put("bytes_transferred", 0)
                .put("url", url)
                .put("method", method)
        }
    }

    /** Shape a synthetic result for unit tests without network. */
    fun shapeCompleted(httpStatus: Int, url: String, method: String, durationMs: Double, preview: String): JSONObject {
        return JSONObject()
            .put("status", "COMPLETED")
            .put("http_status", httpStatus)
            .put("duration_ms", durationMs)
            .put("bytes_transferred", preview.toByteArray(Charsets.UTF_8).size)
            .put("response_preview", preview.take(1024))
            .put("url", url)
            .put("method", method)
    }

    private val shared = OkHttpClient.Builder().build()

    private fun defaultClient(payload: JSONObject): OkHttpClient = shared
}
