package com.farmos.node.node

import android.content.Context
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager

/**
 * Schedules node activity after a successful enrollment:
 *  - an immediate one-time heartbeat so the device reaches ACTIVE right away
 *  - a periodic heartbeat on a fixed interval
 */
object NodeEnrollmentScheduler {
    fun onEnrolled(context: Context) {
        // Immediate heartbeat.
        WorkManager.getInstance(context)
            .enqueue(OneTimeWorkRequestBuilder<NodeHeartbeatWorker>().build())
        // Ongoing periodic heartbeats.
        NodeHeartbeatWorker.schedule(context)
    }
}
