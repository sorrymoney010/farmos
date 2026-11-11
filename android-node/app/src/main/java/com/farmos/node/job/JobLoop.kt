package com.farmos.node.job

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.farmos.node.benchmark.BenchmarkRepository
import com.farmos.node.benchmark.BenchmarkRunner
import com.farmos.node.identity.DeviceIdentityStore
import com.farmos.node.safety.DeviceTelemetry
import com.farmos.node.safety.NodeSafetyState
import com.farmos.node.safety.ThermalGovernor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.random.Random

/**
 * In-process job poll loop (alongside [com.farmos.node.node.HeartbeatLoop]).
 *
 * Cadence mirrors Python agent FARMOS_JOB_POLL_INTERVAL (default 10s) with jitter.
 * Empty queue is normal; errors back off up to 60s.
 */
object JobLoop {
    private const val BASE_POLL_MS = 10_000L
    private const val MAX_BACKOFF_MS = 60_000L
    private const val SAFETY_PAUSE_MS = 30_000L
    private const val AUTO_BENCHMARK_RETRY_MS = 10 * 60_000L

    @Volatile
    private var lastAutoBenchmarkMs = 0L

    private var job: Job? = null
    @Volatile
    var lastStatusText: String = "Jobs: idle"
        private set

    private var statusListener: ((String) -> Unit)? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    fun setStatusListener(listener: ((String) -> Unit)?) {
        statusListener = listener
        listener?.let { mainHandler.post { it(lastStatusText) } }
    }

    fun start(context: Context, scope: CoroutineScope = CoroutineScope(Dispatchers.IO)) {
        if (job?.isActive == true) {
            android.util.Log.i("FarmosJob", "JobLoop: already running")
            return
        }
        android.util.Log.i("FarmosJob", "JobLoop: starting")
        job = scope.launch {
            val appCtx = context.applicationContext
            val store = DeviceIdentityStore(appCtx)
            var backoffMs = BASE_POLL_MS
            while (isActive) {
                if (!store.isEnrolled()) {
                    updateStatus("Jobs: waiting for enrollment")
                    delay(BASE_POLL_MS)
                    continue
                }
                // ── Local safety gate (defense in depth; spec §10) ──
                val snap = runCatching { DeviceTelemetry.read(appCtx) }.getOrNull()
                if (snap != null) NodeSafetyState.lastSnapshot = snap
                val decision = NodeSafetyState.decide()
                if (decision.workloadClass == ThermalGovernor.WorkloadClass.NONE) {
                    updateStatus("Jobs: PAUSED (${decision.band}) · ${decision.reason ?: "safety"}")
                    delay(SAFETY_PAUSE_MS)
                    continue
                }
                try {
                    // Initial baseline benchmark (spec §8: node is ACTIVE only after it passes).
                    val now = System.currentTimeMillis()
                    if (NodeSafetyState.benchmarkRequired && now - lastAutoBenchmarkMs > AUTO_BENCHMARK_RETRY_MS &&
                        decision.allows("benchmark")
                    ) {
                        lastAutoBenchmarkMs = now
                        updateStatus("Benchmark: running farmos-bench-1…")
                        val res = BenchmarkRepository(appCtx, store).runAndSubmit()
                        NodeSafetyState.benchmarkRequired = false
                        updateStatus("Benchmark: FarmScore ${res.optDouble("farm_score")} · ${res.optString("status")}")
                    }

                    val repo = JobRepository(appCtx, store)
                    val polled = repo.pollNextJob()
                    if (polled == null) {
                        updateStatus("Jobs: idle (no work) · last ok")
                        backoffMs = BASE_POLL_MS
                    } else if (!decision.allows(polled.workloadType)) {
                        // Leave it DISPATCHED on the server; pick it up once cooler.
                        updateStatus("Jobs: holding ${polled.workloadType} (${decision.band}: ${decision.reason})")
                        backoffMs = SAFETY_PAUSE_MS
                    } else if (polled.workloadType.equals("benchmark", ignoreCase = true)) {
                        updateStatus("Benchmark job ${polled.jobId.take(8)}…")
                        repo.acceptJob(polled.jobId)
                        val res = BenchmarkRepository(appCtx, store).runAndSubmit(polled.jobId)
                        updateStatus("Benchmark: FarmScore ${res.optDouble("farm_score")}")
                        backoffMs = BASE_POLL_MS
                    } else {
                        updateStatus("Jobs: accept ${polled.jobId.take(8)}… (${polled.workloadType})")
                        val lease = repo.acceptJob(polled.jobId) ?: polled.lease
                        updateStatus("Jobs: executing ${polled.workloadType}")
                        val result = executeWithLease(repo, polled, lease)
                        val status = result.optString("status", "?")
                        updateStatus("Jobs: submit ${polled.jobId.take(8)}… ($status)")
                        val resp = repo.submitResult(polled.jobId, result)
                        updateStatus(JobRepository.submitHint(polled.jobId, status, resp))
                        backoffMs = BASE_POLL_MS
                    }
                } catch (e: BenchmarkRunner.Aborted) {
                    android.util.Log.w("FarmosJob", "Benchmark aborted: ${e.message}")
                    updateStatus("Benchmark: aborted · ${e.message?.take(80)}")
                    NodeSafetyState.noteRed()
                    backoffMs = SAFETY_PAUSE_MS
                } catch (e: Exception) {
                    android.util.Log.e("FarmosJob", "JobLoop error", e)
                    updateStatus("Jobs: error ${e.message?.take(80) ?: e.javaClass.simpleName}")
                    backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
                }
                val jitter = Random.nextLong(0, (backoffMs * 0.5).toLong().coerceAtLeast(1))
                delay(backoffMs + jitter)
            }
        }
    }

    /** Runs the job while renewing its lease in the background (spec §71). */
    private suspend fun executeWithLease(
        repo: JobRepository,
        polled: JobRepository.PolledJob,
        lease: JobRepository.Lease?,
    ): org.json.JSONObject = coroutineScope {
        val renewer = lease?.let { l ->
            launch {
                while (isActive) {
                    delay(l.renewEverySeconds * 1000L)
                    val renewed = runCatching { repo.renewLease(polled.jobId, l.leaseId) }.getOrNull()
                    if (renewed == null) {
                        android.util.Log.w("FarmosJob", "Lease ${l.leaseId.take(8)} lost; server will requeue")
                        break
                    }
                }
            }
        }
        try {
            repo.executeJob(polled)
        } finally {
            renewer?.cancel()
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }

    private fun updateStatus(text: String) {
        lastStatusText = text
        val listener = statusListener ?: return
        mainHandler.post { listener(text) }
    }
}
