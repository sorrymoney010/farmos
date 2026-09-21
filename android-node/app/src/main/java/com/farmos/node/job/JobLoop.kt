package com.farmos.node.job

import android.content.Context
import android.os.Handler
import android.os.Looper
import com.farmos.node.identity.DeviceIdentityStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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
                try {
                    val repo = JobRepository(appCtx, store)
                    val polled = repo.pollNextJob()
                    if (polled == null) {
                        updateStatus("Jobs: idle (no work) · last ok")
                        backoffMs = BASE_POLL_MS
                    } else {
                        updateStatus("Jobs: accept ${polled.jobId.take(8)}… (${polled.workloadType})")
                        repo.acceptJob(polled.jobId)
                        updateStatus("Jobs: executing ${polled.workloadType}")
                        val result = repo.executeJob(polled)
                        val status = result.optString("status", "?")
                        updateStatus("Jobs: submit ${polled.jobId.take(8)}… ($status)")
                        repo.submitResult(polled.jobId, result)
                        val hint = if (status == "COMPLETED") {
                            "Jobs: last ${polled.jobId.take(8)} COMPLETED · ~\$0.0001 test credit"
                        } else {
                            "Jobs: last ${polled.jobId.take(8)} $status"
                        }
                        updateStatus(hint)
                        backoffMs = BASE_POLL_MS
                    }
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
