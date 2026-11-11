package com.farmos.node

import com.farmos.node.benchmark.BenchmarkRunner
import com.farmos.node.benchmark.SimpleJson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.nio.file.Files

/** Pure-JVM tests for the benchmark suite runner (no device needed). */
class BenchmarkRunnerTest {
    private fun runner(temps: List<Double?>): BenchmarkRunner {
        val it = temps.iterator()
        var last: Double? = null
        return BenchmarkRunner(
            cacheDir = Files.createTempDirectory("bench").toFile(),
            temperatureProvider = { if (it.hasNext()) it.next().also { v -> last = v } else last },
            batteryProvider = { mapOf("pct" to 90.0, "charging" to true, "health" to "GOOD") },
            memoryTotalMb = { 4096 },
            latencyProbeMs = { 42.0 },
            stageMillis = 50,
            storageMb = 2,
        )
    }

    @Test fun producesRawSchema() {
        val raw = runner(listOf(30.0, 31.0, 31.5, 32.0, 32.0, 32.5)).run()
        for (k in listOf("cpu", "ai", "memory", "storage", "network", "thermal", "battery", "duration_ms")) {
            assertTrue("missing $k", raw.containsKey(k))
        }
        val cpu = raw["cpu"] as Map<*, *>
        assertTrue((cpu["multi_thread_hashes_per_sec"] as Double) > 0)
        val thermal = raw["thermal"] as Map<*, *>
        assertEquals(30.0, thermal["start_c"])
        assertEquals(32.5, thermal["peak_c"])
    }

    @Test fun refusesToStartUnlessGreen() {
        try {
            runner(listOf(39.0)).run(); fail("should abort")
        } catch (e: BenchmarkRunner.Aborted) {
            assertTrue(e.message!!.contains("GREEN"))
        }
    }

    @Test fun abortsWhenOverheatingMidRun() {
        try {
            runner(listOf(30.0, 46.0)).run(); fail("should abort")
        } catch (e: BenchmarkRunner.Aborted) {
            assertTrue(e.message!!.contains("protect hardware"))
        }
    }

    @Test fun simpleJsonIsSortedCompactAndHashable() {
        val s = SimpleJson.write(mapOf("b" to 1, "a" to mapOf("y" to null, "x" to 1.5), "c" to Double.NaN, "d" to "é\""))
        assertEquals("{\"a\":{\"x\":1.5,\"y\":null},\"b\":1,\"c\":null,\"d\":\"\\u00e9\\\"\"}", s)
        assertEquals(64, BenchmarkRunner.sha256Hex(s).length)
    }
}
