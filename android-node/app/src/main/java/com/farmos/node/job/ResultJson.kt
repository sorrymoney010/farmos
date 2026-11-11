package com.farmos.node.job

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.util.TreeMap

/**
 * Canonical JSON for job results — byte-identical to the backend's
 * `app.workloads.cpu_data.canonical` (Python `json.dumps(normalize_numbers(v),
 * sort_keys=True, separators=(",", ":"))`, ensure_ascii):
 *
 * - keys sorted, compact separators
 * - integral doubles rendered as integers (5.0 -> 5), matching what org.json
 *   writes and what the server re-canonicalises after parsing
 * - non-ASCII and control chars escaped as lowercase \uXXXX; '/' not escaped
 *
 * The canonical string is used both as the signed `result_uri` and as the
 * input to `result_hash`, so the server can recompute the hash exactly.
 */
object ResultJson {
    private const val MAX_SAFE = 9007199254740992.0 // 2^53

    fun canonical(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is String -> quote(value)
        is Boolean -> value.toString()
        is Double -> number(value)
        is Float -> number(value.toDouble())
        is Number -> value.toLong().toString()
        is JSONObject -> {
            val sorted = TreeMap<String, Any?>()
            value.keys().forEach { k -> sorted[k] = value.opt(k) }
            sorted.entries.joinToString(prefix = "{", postfix = "}", separator = ",") { (k, v) -> "${quote(k)}:${canonical(v)}" }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") { canonical(value.opt(it)) }
        is Map<*, *> -> value.entries.sortedBy { it.key.toString() }
            .joinToString(prefix = "{", postfix = "}", separator = ",") { (k, v) -> "${quote(k.toString())}:${canonical(v)}" }
        is Iterable<*> -> value.joinToString(prefix = "[", postfix = "]", separator = ",") { canonical(it) }
        else -> quote(value.toString())
    }

    private fun number(d: Double): String {
        if (!d.isFinite()) return "null"
        if (d == Math.floor(d) && Math.abs(d) < MAX_SAFE) return d.toLong().toString()
        return d.toString() // results avoid non-integral floats where verification matters
    }

    fun quote(s: String): String {
        val out = StringBuilder("\"")
        for (c in s) {
            when {
                c == '"' -> out.append("\\\"")
                c == '\\' -> out.append("\\\\")
                c == '\n' -> out.append("\\n")
                c == '\r' -> out.append("\\r")
                c == '\t' -> out.append("\\t")
                c == '\u0008' -> out.append("\\b")
                c == '\u000C' -> out.append("\\f")
                c < ' ' || c.code > 0x7F -> out.append("\\u%04x".format(c.code))
                else -> out.append(c)
            }
        }
        return out.append('"').toString()
    }

    fun sha256Hex(s: String): String = sha256Hex(s.toByteArray(Charsets.UTF_8))

    fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
}
