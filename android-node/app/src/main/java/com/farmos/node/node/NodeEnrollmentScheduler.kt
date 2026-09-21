package com.farmos.node.node

import android.content.Context
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.farmos.node.job.JobLoop

/**
 * Schedules node activity after a successful enrollment:
 *  - an immediate one-time heartbeat so the device reaches ACTIVE right away
 *  - an in-process 30s heartbeat loop (primary cadence for the 3-min offline policy)
 *  - an in-process ~10s job poll/execute/submit loop (Wi‑Fi job path)
 *  - a 15-min WorkManager fallback for when the process is dead
 */
object NodeEnrollmentScheduler {
    fun onEnrolled(context: Context) {
        android.util.Log.i("FarmosHb", "NodeEnrollmentScheduler.onEnrolled: scheduling heartbeat + jobs")
        // Immediate heartbeat.
        WorkManager.getInstance(context)
            .enqueue(OneTimeWorkRequestBuilder<NodeHeartbeatWorker>().build())
        // Primary in-process loops + coarse fallback.
        HeartbeatLoop.start(context)
        JobLoop.start(context)
        NodeHeartbeatWorker.schedule(context)
    }
}
