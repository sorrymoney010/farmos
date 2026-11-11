package com.farmos.node.job

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.Base64
import java.util.zip.Deflater

/**
 * Internal CPU/data workloads (spec §44): checksum, compression, json_transform.
 *
 * Kotlin port of backend `app/workloads/cpu_data.py`; results must be
 * byte-identical so the control plane can verify them by recomputation.
 * Cross-language vectors live in CpuDataExecutorTest / test_cpu_data_workloads.py.
 *
 * - det_stream(seed, n): SHA-256(seed_utf8 || uint32_be(i)) blocks, truncated.
 * - det_text(seed, n): lines "farmos-{seed}-{i % 97}\n", truncated.
 *
 * Checksum streams the input through the digest so an 8 MB job never holds
 * 8 MB in memory on a small phone.
 */
object CpuDataExecutor {
    const val MAX_CHECKSUM_BYTES = 8 * 1024 * 1024
    const val MAX_COMPRESSION_BYTES = 1024 * 1024
    const val MAX_JSON_DOC_BYTES = 256 * 1024
    val WORKLOADS = setOf("checksum", "compression", "json_transform")

    class WorkloadError(msg: String) : IllegalArgumentException(msg)

    fun execute(workload: String, payload: JSONObject): JSONObject {
        val start = System.nanoTime()
        val out = try {
            when (workload) {
                "checksum" -> checksum(payload)
                "compression" -> compression(payload)
                "json_transform" -> jsonTransform(payload)
                else -> throw WorkloadError("unsupported workload")
            }
        } catch (e: WorkloadError) {
            JSONObject().put("status", "FAILED").put("workload", workload).put("error", e.message)
        }
        return out.put("duration_ms", (System.nanoTime() - start) / 1_000_000L)
    }

    // ── deterministic inputs ────────────────────────────────────────────────

    private fun block(seed: ByteArray, i: Int): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        md.update(seed)
        md.update(byteArrayOf((i ushr 24).toByte(), (i ushr 16).toByte(), (i ushr 8).toByte(), i.toByte()))
        return md.digest()
    }

    fun detStream(seed: String, size: Int): ByteArray {
        val out = ByteArrayOutputStream(size)
        val s = seed.toByteArray(Charsets.UTF_8)
        var i = 0
        while (out.size() < size) {
            val b = block(s, i++)
            out.write(b, 0, minOf(b.size, size - out.size()))
        }
        return out.toByteArray()
    }

    fun detText(seed: String, size: Int): ByteArray {
        val out = ByteArrayOutputStream(size)
        var i = 0
        while (out.size() < size) {
            val line = "farmos-$seed-${i % 97}\n".toByteArray(Charsets.UTF_8)
            out.write(line, 0, minOf(line.size, size - out.size()))
            i++
        }
        return out.toByteArray()
    }

    // ── param helpers (mirror Python validation) ────────────────────────────

    private fun seedOf(p: JSONObject): String {
        val s = p.opt("seed")
        if (s !is String || s.isEmpty() || s.length > 128) throw WorkloadError("seed must be a 1-128 char string")
        return s
    }

    private fun intOf(p: JSONObject, key: String, lo: Int, hi: Int, default: Int? = null): Int {
        val v = p.opt(key)
        if (v == null && default != null) return default
        val n = when (v) {
            is Int -> v.toLong()
            is Long -> v
            else -> throw WorkloadError("$key must be an integer in [$lo, $hi]")
        }
        if (n < lo || n > hi) throw WorkloadError("$key must be an integer in [$lo, $hi]")
        return n.toInt()
    }

    // ── workloads ───────────────────────────────────────────────────────────

    fun checksum(p: JSONObject): JSONObject {
        val seed = seedOf(p)
        val size = intOf(p, "size_bytes", 1, MAX_CHECKSUM_BYTES)
        val rounds = intOf(p, "rounds", 1, 16, default = 1)
        val md = MessageDigest.getInstance("SHA-256")
        val s = seed.toByteArray(Charsets.UTF_8)
        var written = 0
        var i = 0
        while (written < size) {
            val b = block(s, i++)
            val n = minOf(b.size, size - written)
            md.update(b, 0, n)
            written += n
        }
        var digest = md.digest()
        repeat(rounds - 1) { digest = MessageDigest.getInstance("SHA-256").digest(digest) }
        return JSONObject()
            .put("status", "COMPLETED")
            .put("workload", "checksum")
            .put("algorithm", "sha256")
            .put("size_bytes", size)
            .put("rounds", rounds)
            .put("output_sha256", digest.joinToString("") { "%02x".format(it) })
    }

    fun compression(p: JSONObject): JSONObject {
        val seed = seedOf(p)
        val size = intOf(p, "size_bytes", 1, MAX_COMPRESSION_BYTES)
        val level = intOf(p, "level", 1, 9, default = 6)
        val data = detText(seed, size)
        val deflater = Deflater(level) // zlib wrapper, readable by Python zlib.decompress
        deflater.setInput(data)
        deflater.finish()
        val buf = ByteArray(8192)
        val out = ByteArrayOutputStream()
        while (!deflater.finished()) {
            val n = deflater.deflate(buf)
            out.write(buf, 0, n)
        }
        deflater.end()
        val compressed = out.toByteArray()
        return JSONObject()
            .put("status", "COMPLETED")
            .put("workload", "compression")
            .put("algorithm", "zlib")
            .put("input_size", size)
            .put("input_sha256", ResultJson.sha256Hex(data))
            .put("compressed_size", compressed.size)
            .put("compressed_b64", Base64.getEncoder().encodeToString(compressed))
    }

    // json_transform operates on plain Kotlin values (Map/List/Long/Double/String/Boolean/null).

    private fun toPlain(v: Any?): Any? = when (v) {
        null, JSONObject.NULL -> null
        is JSONObject -> {
            val m = LinkedHashMap<String, Any?>()
            v.keys().forEach { k -> m[k] = toPlain(v.opt(k)) }
            m
        }
        is JSONArray -> (0 until v.length()).map { toPlain(v.opt(it)) }
        is Int -> v.toLong()
        is Long -> v
        is Double -> if (v.isFinite() && v == Math.floor(v) && Math.abs(v) < 9.007199254740992E15) v.toLong() else v
        is Float -> toPlain(v.toDouble())
        is Number -> v.toLong()
        else -> v
    }

    private fun field(r: Any?, f: Any?): Any? = (r as? Map<*, *>)?.get(f?.toString())

    private fun same(a: Any?, b: Any?): Boolean = ResultJson.canonical(a) == ResultJson.canonical(b)

    private fun applyOp(doc: Any?, op: Map<*, *>): Any? {
        val list = doc as? List<*>
        return when (val kind = op["op"]) {
            "select" -> {
                val fields = op["fields"] as? List<*>
                if (list == null || fields == null) throw WorkloadError("select needs a list document and fields list")
                list.filterIsInstance<Map<*, *>>().map { r ->
                    val m = LinkedHashMap<String, Any?>()
                    fields.forEach { f -> m[f.toString()] = r[f.toString()] }
                    m
                }
            }
            "filter_eq" -> {
                list ?: throw WorkloadError("filter_eq needs a list document")
                list.filter { it is Map<*, *> && same(field(it, op["field"]), op["value"]) }
            }
            "sort_by" -> {
                list ?: throw WorkloadError("sort_by needs a list document")
                list.sortedBy { ResultJson.canonical(field(it, op["field"])) }
            }
            "count" -> (list ?: throw WorkloadError("count needs a list document")).size.toLong()
            "sum" -> {
                list ?: throw WorkloadError("sum needs a list document")
                var total = 0L
                for (r in list) {
                    val v = field(r, op["field"]) ?: continue
                    if (v !is Long) throw WorkloadError("sum supports integer fields only (deterministic)")
                    total += v
                }
                total
            }
            else -> throw WorkloadError("unsupported op '$kind'")
        }
    }

    fun jsonTransform(p: JSONObject): JSONObject {
        if (!p.has("document") || p.isNull("document")) throw WorkloadError("json_transform needs document and <=16 ops")
        val opsRaw = p.opt("ops") ?: JSONArray()
        val ops = toPlain(opsRaw) as? List<*> ?: throw WorkloadError("json_transform needs document and <=16 ops")
        if (ops.size > 16) throw WorkloadError("json_transform needs document and <=16 ops")
        var out: Any? = toPlain(p.opt("document"))
        if (ResultJson.canonical(out).length > MAX_JSON_DOC_BYTES) throw WorkloadError("document too large")
        for (op in ops) {
            out = applyOp(out, op as? Map<*, *> ?: throw WorkloadError("each op must be an object"))
        }
        val canonicalOut = ResultJson.canonical(out)
        return JSONObject()
            .put("status", "COMPLETED")
            .put("workload", "json_transform")
            .put("ops_applied", ops.size)
            .put("output", toJson(out))
            .put("output_sha256", ResultJson.sha256Hex(canonicalOut))
    }

    private fun toJson(v: Any?): Any = when (v) {
        null -> JSONObject.NULL
        is Map<*, *> -> JSONObject().also { o -> v.forEach { (k, x) -> o.put(k.toString(), toJson(x)) } }
        is List<*> -> JSONArray().also { a -> v.forEach { a.put(toJson(it)) } }
        else -> v
    }
}
