package com.farmos.node.node

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.farmos.node.identity.DeviceIdentityStore
import java.util.concurrent.TimeUnit

/**
 * Periodic heartbeat worker. It runs on a fixed interval while the device is
 * enrolled. A single heartbeat is also fired immediately on enrollment (see
 * NodeEnrollmentScheduler) so ACTIVE state is reached without waiting a full
 * interval.
 */
class NodeHeartbeatWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val identityStore = DeviceIdentityStore(applicationContext)
        if (!identityStore.isEnrolled()) return Result.success()
        return try {
            val repo = NodeEnrollmentRepository(applicationContext, identityStore)
            repo.heartbeatOnce()
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        const val WORK_NAME = "farmos-node-heartbeat"

        /** Schedule periodic heartbeats (minimum 15 min enforced by Android). */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<NodeHeartbeatWorker>(15, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME,
                androidx.work.ExistingPeriodicWorkPolicy.UPDATE,
                request,
            )
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME)
        }
    }
}
