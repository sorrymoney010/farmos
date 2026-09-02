package com.farmos.node.node

import android.content.Context
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager

/**
 * Schedules node activity after a successful enrollment:
 *  - an immediate one-time heartbeat so the device reaches ACTIVE right away
 *  - an in-process 30s heartbeat loop (primary cadence for the 3-min offline policy)
 *  - a 15-min WorkManager fallback for when the process is dead
 */
object NodeEnrollmentScheduler {
    fun onEnrolled(context: Context) {
        // Immediate heartbeat.
        WorkManager.getInstance(context)
            .enqueue(OneTimeWorkRequestBuilder<NodeHeartbeatWorker>().build())
        // Primary in-process loop + coarse fallback.
        HeartbeatLoop.start(context)
        NodeHeartbeatWorker.schedule(context)
    }
}
