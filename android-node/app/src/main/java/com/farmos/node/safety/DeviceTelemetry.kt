package com.farmos.node.safety

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Environment
import android.os.PowerManager
import android.os.StatFs

/**
 * Reads REAL device telemetry through platform APIs only (spec §86: battery /
 * charging state via platform APIs; no invasive permissions). Unknown values
 * stay null and are omitted from the heartbeat — never faked.
 */
object DeviceTelemetry {

    fun read(context: Context): TelemetrySnapshot {
        val app = context.applicationContext
        val battery: Intent? = app.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))

        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val pct = if (level >= 0 && scale > 0) round1(level * 100.0 / scale) else null
        val status = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val plugged = battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val charging = if (battery == null) null else (plugged != 0 ||
            status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL)
        val tempTenths = battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
        val batteryTemp = if (tempTenths != Int.MIN_VALUE) round1(tempTenths / 10.0) else null
        val health = battery?.getIntExtra(BatteryManager.EXTRA_HEALTH, -1)?.let(::healthName)

        val thermalStatus = runCatching {
            (app.getSystemService(Context.POWER_SERVICE) as PowerManager).currentThermalStatus
        }.getOrNull()

        val mem = runCatching {
            val am = app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }
        }.getOrNull()

        val stat = runCatching { StatFs(Environment.getDataDirectory().path) }.getOrNull()

        var netType: String? = null
        var down: Int? = null
        var up: Int? = null
        runCatching {
            val cm = app.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            cm.getNetworkCapabilities(cm.activeNetwork)?.let { caps ->
                netType = when {
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                    else -> "other"
                }
                down = caps.linkDownstreamBandwidthKbps.takeIf { it > 0 }?.div(1000)
                up = caps.linkUpstreamBandwidthKbps.takeIf { it > 0 }?.div(1000)
            }
        }

        return TelemetrySnapshot(
            // Battery thermistor is the most reliable sensor apps can read without
            // privileged access; PowerManager thermal status covers SoC hot spots.
            temperatureC = batteryTemp,
            batteryTemperatureC = batteryTemp,
            batteryPct = pct,
            charging = charging,
            batteryHealth = health,
            thermalStatus = thermalStatus,
            ramUsedMb = mem?.let { ((it.totalMem - it.availMem) / 1_048_576L).toInt() },
            ramTotalMb = mem?.let { (it.totalMem / 1_048_576L).toInt() },
            storageFreeMb = stat?.let { it.availableBytes / 1_048_576L },
            storageTotalMb = stat?.let { it.totalBytes / 1_048_576L },
            networkType = netType,
            downMbps = down,
            upMbps = up,
        )
    }

    fun healthName(code: Int): String = when (code) {
        BatteryManager.BATTERY_HEALTH_GOOD -> "GOOD"
        BatteryManager.BATTERY_HEALTH_OVERHEAT -> "OVERHEAT"
        BatteryManager.BATTERY_HEALTH_DEAD -> "DEAD"
        BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "OVER_VOLTAGE"
        BatteryManager.BATTERY_HEALTH_UNSPECIFIED_FAILURE -> "UNSPECIFIED_FAILURE"
        BatteryManager.BATTERY_HEALTH_COLD -> "COLD"
        else -> "UNKNOWN"
    }

    private fun round1(v: Double): Double = Math.round(v * 10.0) / 10.0
}
