package com.farmos.node

import com.farmos.node.job.HttpCheckExecutor
import com.farmos.node.job.JobRepository
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest
import java.util.TreeSet

/**
 * JVM unit tests for Android job-loop contracts (no device required).
 */
class JobLoopUnitTest {

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
                    if (c < ' ') out.append("\\u%04x".format(c.code))
                    else if (c.code > 0x7F) out.append("\\u%04x".format(c.code))
                    else out.append(c)
                }
            }
        }
        out.append('"')
        return out.toString()
    }

    @Test
    fun acceptSignKeysMatchBackendPayloadToVerify() {
        assertEquals(
            listOf("device_id", "job_id", "request_nonce", "request_timestamp"),
            JobRepository.acceptSignKeys(),
        )
    }

    @Test
    fun resultSignKeysExcludeExecutionMetrics() {
        assertEquals(
            listOf("device_id", "job_id", "request_nonce", "request_timestamp", "result_hash", "result_uri"),
            JobRepository.resultSignKeys(),
        )
    }

    @Test
    fun resultHashIsSha256OfCanonicalJson() {
        val result = HttpCheckExecutor.shapeCompleted(
            httpStatus = 200,
            url = "https://example.com",
            method = "GET",
            durationMs = 12.34,
            preview = "ok",
        )
        val canonical = canonicalize(result)
        val expected = MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        assertEquals(expected, JobRepository.sha256Hex(canonical))
        assertTrue(result.getString("status") == "COMPLETED")
        assertEquals(200, result.getInt("http_status"))
    }

    @Test
    fun httpCheckCompletedShapeIsVerifiable() {
        val shaped = HttpCheckExecutor.shapeCompleted(204, "https://example.com/health", "GET", 5.0, "")
        assertEquals("COMPLETED", shaped.getString("status"))
        val code = shaped.getInt("http_status")
        assertTrue(code in 100 until 600)
        assertTrue(shaped.has("duration_ms"))
        assertTrue(shaped.has("url"))
        assertTrue(shaped.has("method"))
    }

    @Test
    fun acceptCanonicalOrderIsStable() {
        val payload = JSONObject()
            .put("request_timestamp", "2026-01-01T00:00:00Z")
            .put("device_id", "dev-1")
            .put("job_id", "job-1")
            .put("request_nonce", "abc")
        val canonical = canonicalize(payload)
        assertEquals(
            """{"device_id":"dev-1","job_id":"job-1","request_nonce":"abc","request_timestamp":"2026-01-01T00:00:00Z"}""",
            canonical,
        )
    }
}
