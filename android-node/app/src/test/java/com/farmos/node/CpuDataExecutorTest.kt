package com.farmos.node

import com.farmos.node.job.CpuDataExecutor
import com.farmos.node.job.JobRepository
import com.farmos.node.job.ResultJson
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64
import java.util.zip.Inflater

/**
 * Cross-language known-answer vectors. The SAME values are asserted in
 * backend/app/tests/unit/test_cpu_data_workloads.py — the server verifies
 * device results by recomputing them in Python, so these must never drift.
 */
class CpuDataExecutorTest {

    private fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

    @Test
    fun detStreamAndTextMatchPython() {
        assertEquals("cf2db1ac9867debd", hex(CpuDataExecutor.detStream("abc", 8)))
        assertEquals("farmos-abc-0\nfarmos-abc-1\nfarm", String(CpuDataExecutor.detText("abc", 30)))
    }

    @Test
    fun checksumVectorsMatchPython() {
        val r = CpuDataExecutor.checksum(JSONObject().put("seed", "abc").put("size_bytes", 100))
        assertEquals("21d789823b7cbf0f789394a475d818cf277c3e074572ae3ab351628650e19fef", r.getString("output_sha256"))
        assertEquals("COMPLETED", r.getString("status"))
        val r3 = CpuDataExecutor.checksum(JSONObject().put("seed", "farm").put("size_bytes", 65536).put("rounds", 3))
        assertEquals("08f3563a3f597d74ec03bcc5b1634886dabf214a0135f79a58b74c8cf4a320e3", r3.getString("output_sha256"))
    }

    @Test
    fun checksumRejectsBadPayload() {
        for (p in listOf(
            JSONObject().put("size_bytes", 10),
            JSONObject().put("seed", "x").put("size_bytes", 0),
            JSONObject().put("seed", "x").put("size_bytes", 100_000_000),
            JSONObject().put("seed", "x").put("size_bytes", 10).put("rounds", 99),
        )) {
            assertEquals("FAILED", CpuDataExecutor.execute("checksum", p).getString("status"))
        }
    }

    @Test
    fun compressionInputMatchesPythonAndRoundTrips() {
        val r = CpuDataExecutor.compression(JSONObject().put("seed", "abc").put("size_bytes", 1000))
        assertEquals("39d0c958e97e19e9d26ae7500009dcc07a114ee7a0c69f3247a0c3e435879630", r.getString("input_sha256"))
        val blob = Base64.getDecoder().decode(r.getString("compressed_b64"))
        assertEquals(blob.size, r.getInt("compressed_size"))
        val inf = Inflater().apply { setInput(blob) }
        val out = ByteArray(2000)
        val n = inf.inflate(out)
        assertEquals(1000, n)
        assertEquals(r.getString("input_sha256"), ResultJson.sha256Hex(out.copyOf(n)))
        assertTrue(blob.size < 1000)
    }

    @Test
    fun jsonTransformMatchesPython() {
        val doc = JSONArray()
            .put(JSONObject().put("id", 1).put("g", "a").put("v", 3))
            .put(JSONObject().put("id", 2).put("g", "b").put("v", 4.0))
            .put(JSONObject().put("id", 3).put("g", "a").put("v", 5))
        val ops = JSONArray()
            .put(JSONObject().put("op", "filter_eq").put("field", "g").put("value", "a"))
            .put(JSONObject().put("op", "select").put("fields", JSONArray().put("id").put("v").put("missing")))
        val r = CpuDataExecutor.jsonTransform(JSONObject().put("document", doc).put("ops", ops))
        assertEquals("""[{"id":1,"missing":null,"v":3},{"id":3,"missing":null,"v":5}]""", ResultJson.canonical(r.get("output")))
        assertEquals("eb8350acdfcfd5a039dbc0f25129f56b0ca8aba133dff996cf0f64d5a28b3598", r.getString("output_sha256"))

        val doc2 = JSONArray()
            .put(JSONObject().put("id", 2).put("v", 4))
            .put(JSONObject().put("id", 1).put("v", 6))
            .put(JSONObject().put("id", 3).put("v", -1))
        val ops2 = JSONArray()
            .put(JSONObject().put("op", "sort_by").put("field", "id"))
            .put(JSONObject().put("op", "sum").put("field", "v"))
        val r2 = CpuDataExecutor.jsonTransform(JSONObject().put("document", doc2).put("ops", ops2))
        assertEquals(9L, (r2.get("output") as Number).toLong())
        assertEquals("19581e27de7ced00ff1ce50b2047e7a567c76b1cbaebabe5ef03f7c3017bb5b7", r2.getString("output_sha256"))

        val bad = CpuDataExecutor.execute("json_transform", JSONObject().put("document", JSONArray()
            .put(JSONObject().put("v", 1.5))).put("ops", JSONArray().put(JSONObject().put("op", "sum").put("field", "v"))))
        assertEquals("FAILED", bad.getString("status"))
    }

    @Test
    fun resultCanonicalMatchesPython() {
        val obj = JSONObject().put("s", "é/\"x").put("d", 12.0).put("f", 12.5).put("n", JSONObject.NULL)
            .put("l", JSONArray().put(1).put(2.0))
        // Python: cpu_data.canonical({'s':'é/"x','d':12.0,'f':12.5,'n':None,'l':[1,2.0]})
        assertEquals("""{"d":12,"f":12.5,"l":[1,2],"n":null,"s":"\u00e9/\"x"}""", ResultJson.canonical(obj))
    }

    @Test
    fun executeAddsIntegerDuration() {
        val r = CpuDataExecutor.execute("checksum", JSONObject().put("seed", "a").put("size_bytes", 1024))
        assertTrue(r.get("duration_ms") is Long)
        assertEquals("unsupported workload", CpuDataExecutor.execute("nope", JSONObject()).getString("error"))
    }

    @Test
    fun renewSignKeysMatchBackend() {
        assertEquals(listOf("device_id", "job_id", "lease_id", "request_nonce", "request_timestamp"),
            JobRepository.renewSignKeys())
    }

    @Test
    fun submitHintReflectsVerification() {
        assertTrue(JobRepository.submitHint("abcdef123", "COMPLETED", null).contains("no credit"))
        val ok = JSONObject().put("status", "VERIFIED")
            .put("verification", JSONObject().put("verification_status", "PASSED").put("test_earnings_credited", true))
        assertTrue(JobRepository.submitHint("abcdef123", "COMPLETED", ok).contains("VERIFIED"))
        val pending = JSONObject().put("verification", JSONObject().put("verification_status", "PENDING"))
        assertTrue(JobRepository.submitHint("abcdef123", "COMPLETED", pending).contains("verifying"))
        assertNull(null)
    }
}
