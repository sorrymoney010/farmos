package com.farmos.node.benchmark

/**
 * Tiny deterministic JSON writer (sorted keys, compact) for benchmark raw
 * results. Pure Kotlin so it is unit-testable without org.json stubs. The
 * backend hashes the exact string we send, so only validity matters here;
 * non-finite doubles are written as null.
 */
object SimpleJson {
    fun write(value: Any?): String = when (value) {
        null -> "null"
        is String -> quote(value)
        is Boolean -> value.toString()
        is Double -> if (value.isFinite()) value.toString() else "null"
        is Float -> if (value.isFinite()) value.toDouble().toString() else "null"
        is Number -> value.toString()
        is Map<*, *> -> value.entries
            .sortedBy { it.key.toString() }
            .joinToString(prefix = "{", postfix = "}", separator = ",") { (k, v) -> "${quote(k.toString())}:${write(v)}" }
        is Iterable<*> -> value.joinToString(prefix = "[", postfix = "]", separator = ",") { write(it) }
        else -> quote(value.toString())
    }

    private fun quote(s: String): String {
        val out = StringBuilder("\"")
        for (c in s) {
            when {
                c == '"' -> out.append("\\\"")
                c == '\\' -> out.append("\\\\")
                c == '\n' -> out.append("\\n")
                c == '\r' -> out.append("\\r")
                c == '\t' -> out.append("\\t")
                c < ' ' || c.code > 0x7F -> out.append("\\u%04x".format(c.code))
                else -> out.append(c)
            }
        }
        return out.append('"').toString()
    }
}
