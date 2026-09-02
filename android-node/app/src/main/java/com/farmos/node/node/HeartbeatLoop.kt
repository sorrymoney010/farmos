package com.farmos.node.node

import android.content.Context
import com.farmos.node.identity.DeviceIdentityStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * In-process heartbeat loop at the backend's heartbeat_interval_seconds (30s).
 *
 * Design reconciliation with the offline policy:
 *   - Backend policy: node.heartbeat_interval_seconds = 30, node.offline_after_seconds = 180.
 *   - A 30s loop tolerates up to ~5 missed beats before the 3-minute offline mark.
 *   - WorkManager's 15-minute minimum is too coarse, so it is used ONLY as a
 *     process-death fallback (see NodeHeartbeatWorker). While the app process is
 *     alive this loop is the primary cadence. It is a best-effort, battery-aware
 *     loop (no wakelocks); Doze may defer it, which the 3-minute grace absorbs.
 */
object HeartbeatLoop {
    private const val INTERVAL_MS = 30_000L
    private var job: Job? = null

    fun start(context: Context, scope: CoroutineScope = CoroutineScope(Dispatchers.IO)) {
        if (job?.isActive == true) return
        job = scope.launch {
            val store = DeviceIdentityStore(context)
            while (isActive) {
                if (store.isEnrolled()) {
                    runCatching { NodeEnrollmentRepository(context, store).heartbeatOnce() }
                }
                delay(INTERVAL_MS)
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }
}
