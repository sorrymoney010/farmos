package com.farmos.node.boot

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.farmos.node.identity.DeviceIdentityStore
import com.farmos.node.node.NodeEnrollmentScheduler

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            // Only schedule heartbeats + jobs if already enrolled. Enrollment
            // always requires an explicit token / claim code from the operator.
            val store = DeviceIdentityStore(context)
            if (store.isEnrolled()) {
                NodeEnrollmentScheduler.onEnrolled(context)
            }
        }
    }
}
