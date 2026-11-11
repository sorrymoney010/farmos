package com.farmos.node.benchmark

import com.farmos.node.safety.ThermalGovernor
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Benchmark suite `farmos-bench-1` (spec §9). Produces RAW measurements only;
 * the backend turns them into component scores + FarmScore, so a device can
 * never self-assign a score.
 *
 * Hardware safety (non-negotiable):
 *  - refuses to start unless the device is thermally GREEN;
 *  - checks temperature between every stage and aborts above ORANGE max;
 *  - total runtime is a few seconds; storage test uses a small temp file in
 *    the app cache dir and always deletes it.
 *
 * Pure JVM (Android specifics are injected) so it is unit-testable.
 */
class BenchmarkRunner(
    private val cacheDir: File,
    private val temperatureProvider: () -> Double?,
    private val batteryProvider: () -> Map<String, Any?> = { emptyMap() },
    private val memoryTotalMb: () -> Int? = { null },
    private val latencyProbeMs: () -> Double? = { null },
    private val thresholds: ThermalGovernor.Thresholds = ThermalGovernor.Thresholds(),
    private val stageMillis: Long = 1_200L,
    private val storageMb: Int = 16,
) {
    class Aborted(message: String) : RuntimeException(message)

    fun run(): Map<String, Any?> {
        val startC = temperatureProvider()
        if (startC != null && startC >= thresholds.greenMax) {
            throw Aborted("device at ${startC}C; benchmark requires GREEN (<${thresholds.greenMax}C)")
        }
        val t0 = System.nanoTime()
        var peak = startC
        fun checkpoint(stage: String) {
            val c = temperatureProvider() ?: return
            peak = maxOf(peak ?: c, c)
            if (c > thresholds.orangeMax) throw Aborted("aborted at ${c}C during $stage (protect hardware)")
        }

        val cpu = cpu(); checkpoint("cpu")
        val ai = matmul(); checkpoint("ai")
        val memory = memory(); checkpoint("memory")
        val storage = storage(); checkpoint("storage")
        val network = mapOf("latency_ms" to latencyProbeMs())
        val endC = temperatureProvider()
        if (endC != null) peak = maxOf(peak ?: endC, endC)

        return mapOf(
            "cpu" to cpu,
            "ai" to ai,
            "memory" to memory,
            "storage" to storage,
            "network" to network,
            "thermal" to mapOf("start_c" to startC, "end_c" to endC, "peak_c" to peak),
            "battery" to batteryProvider(),
            "duration_ms" to (System.nanoTime() - t0) / 1_000_000L,
        )
    }

    private fun hashLoop(millis: Long): Long {
        val md = MessageDigest.getInstance("SHA-256")
        val block = Random.nextBytes(1024)
        val end = System.nanoTime() + millis * 1_000_000L
        var n = 0L
        while (System.nanoTime() < end) {
            repeat(100) { md.update(block); md.digest() }
            n += 100
        }
        return n
    }

    fun cpu(): Map<String, Any?> {
        val single = hashLoop(stageMillis) * 1000.0 / stageMillis
        val threads = Runtime.getRuntime().availableProcessors().coerceAtLeast(1)
        val pool = Executors.newFixedThreadPool(threads)
        val multi = try {
            pool.invokeAll(List(threads) { Callable { hashLoop(stageMillis) } })
                .sumOf { it.get() } * 1000.0 / stageMillis
        } finally {
            pool.shutdownNow()
            pool.awaitTermination(2, TimeUnit.SECONDS)
        }
        return mapOf(
            "single_thread_hashes_per_sec" to round1(single),
            "multi_thread_hashes_per_sec" to round1(multi),
            "threads" to threads,
        )
    }

    /** Naive fp32 matmul as the AI-capability proxy (no NNAPI dependency in V1). */
    fun matmul(n: Int = 128): Map<String, Any?> {
        val a = FloatArray(n * n) { Random.nextFloat() }
        val b = FloatArray(n * n) { Random.nextFloat() }
        val c = FloatArray(n * n)
        val start = System.nanoTime()
        for (i in 0 until n) for (k in 0 until n) {
            val aik = a[i * n + k]
            for (j in 0 until n) c[i * n + j] += aik * b[k * n + j]
        }
        val secs = ((System.nanoTime() - start) / 1e9).coerceAtLeast(1e-6)
        return mapOf("matmul_gflops" to round4(2.0 * n * n * n / secs / 1e9), "n" to n, "checksum" to round1(c.sum().toDouble()))
    }

    fun memory(): Map<String, Any?> {
        val size = 16 * 1_048_576
        val src = ByteArray(size)
        val dst = ByteArray(size)
        val rounds = 8
        val start = System.nanoTime()
        repeat(rounds) { System.arraycopy(src, 0, dst, 0, size) }
        val secs = ((System.nanoTime() - start) / 1e9).coerceAtLeast(1e-6)
        val rt = Runtime.getRuntime()
        return mapOf(
            "copy_mb_per_sec" to round1(rounds * 16 / secs),
            "total_mb" to memoryTotalMb(),
            "available_mb" to ((rt.maxMemory() - (rt.totalMemory() - rt.freeMemory())) / 1_048_576L).toInt(),
        )
    }

    fun storage(): Map<String, Any?> {
        val f = File(cacheDir, "farmos-bench-${System.nanoTime()}.tmp")
        val chunk = Random.nextBytes(1_048_576)
        try {
            var start = System.nanoTime()
            RandomAccessFile(f, "rw").use { raf ->
                repeat(storageMb) { raf.write(chunk) }
                raf.fd.sync()
            }
            val w = storageMb / ((System.nanoTime() - start) / 1e9).coerceAtLeast(1e-6)
            val buf = ByteArray(1_048_576)
            start = System.nanoTime()
            f.inputStream().use { input -> while (input.read(buf) > 0) { /* drain */ } }
            val r = storageMb / ((System.nanoTime() - start) / 1e9).coerceAtLeast(1e-6)
            return mapOf(
                "write_mb_per_sec" to round1(w),
                "read_mb_per_sec" to round1(r),
                "free_mb" to cacheDir.usableSpace / 1_048_576L,
            )
        } finally {
            f.delete()
        }
    }

    companion object {
        const val SUITE_VERSION = "farmos-bench-1"
        fun round1(v: Double) = Math.round(v * 10.0) / 10.0
        fun round4(v: Double) = Math.round(v * 10_000.0) / 10_000.0

        fun sha256Hex(s: String): String =
            MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
    }
}
