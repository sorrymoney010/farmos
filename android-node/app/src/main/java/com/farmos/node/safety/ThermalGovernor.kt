package com.farmos.node.safety

/**
 * On-device thermal / battery governor — defense in depth for the server policy
 * (backend/app/services/device_safety.py, spec §10). Pure Kotlin (no Android
 * imports) so it runs in plain JVM unit tests.
 *
 * Principle: do not make $5 while destroying a $50 device. The phone refuses
 * work locally even if the control plane is unreachable or wrong.
 *
 *   GREEN  < 38 C           all workloads
 *   YELLOW 38–42 C          all workloads, concurrency 1
 *   ORANGE 42–45 C          light workloads only
 *   RED    > 45 C           stop compute, cool down
 *   EMERGENCY >= 50 C       stop everything immediately
 */
object ThermalGovernor {

    data class Thresholds(
        val greenMax: Double = 38.0,
        val yellowMax: Double = 42.0,
        val orangeMax: Double = 45.0,
        val emergency: Double = 50.0,
        val batteryMinPctDischarging: Double = 40.0,
        val batteryCriticalPct: Double = 15.0,
        val batteryHeavyMinPctDischarging: Double = 60.0,
        val localCooldownMs: Long = 10 * 60_000L,
    )

    enum class Band { GREEN, YELLOW, ORANGE, RED, UNKNOWN }

    enum class WorkloadClass { ALL, LIGHT, NONE }

    val LIGHT_WORKLOADS = setOf("http_check", "monitor", "api_request")

    /** Android PowerManager.THERMAL_STATUS_SEVERE. */
    const val THERMAL_STATUS_SEVERE = 3
    const val THERMAL_STATUS_MODERATE = 2

    /** Battery health strings matching the backend (BatteryManager.BATTERY_HEALTH_*). */
    val BATTERY_FAULTS = setOf("DEAD", "OVER_VOLTAGE", "UNSPECIFIED_FAILURE")

    data class Decision(
        val band: Band,
        val workloadClass: WorkloadClass,
        val reason: String?,
        val emergencyStop: Boolean = false,
    ) {
        fun allows(workloadType: String): Boolean = when (workloadClass) {
            WorkloadClass.ALL -> true
            WorkloadClass.LIGHT -> workloadType.lowercase() in LIGHT_WORKLOADS
            WorkloadClass.NONE -> false
        }
    }

    fun band(tempC: Double?, t: Thresholds = Thresholds()): Band = when {
        tempC == null -> Band.UNKNOWN
        tempC < t.greenMax -> Band.GREEN
        tempC < t.yellowMax -> Band.YELLOW
        tempC <= t.orangeMax -> Band.ORANGE
        else -> Band.RED
    }

    /** Hottest sensor wins; PowerManager thermal status sets a floor (same as backend). */
    fun effectiveTemperature(snapshot: TelemetrySnapshot, t: Thresholds = Thresholds()): Double? {
        var eff = listOfNotNull(snapshot.temperatureC, snapshot.batteryTemperatureC).maxOrNull()
        val status = snapshot.thermalStatus
        if (status != null) {
            if (status >= THERMAL_STATUS_SEVERE) eff = maxOf(eff ?: 0.0, t.orangeMax + 0.1)
            else if (status == THERMAL_STATUS_MODERATE) eff = maxOf(eff ?: 0.0, t.yellowMax)
        }
        return eff
    }

    /**
     * Combine the local snapshot with the last server decision. The STRICTER of
     * the two always wins; the server can only further restrict, never loosen.
     */
    fun decide(
        snapshot: TelemetrySnapshot,
        server: ServerSafety? = null,
        nowMs: Long = System.currentTimeMillis(),
        localCooldownUntilMs: Long = 0L,
        t: Thresholds = Thresholds(),
    ): Decision {
        val temp = effectiveTemperature(snapshot, t)
        val b = band(temp, t)
        val health = snapshot.batteryHealth?.uppercase()

        if (health != null && health in BATTERY_FAULTS) {
            return Decision(b, WorkloadClass.NONE, "battery fault $health", emergencyStop = true)
        }
        if (temp != null && temp >= t.emergency) {
            return Decision(Band.RED, WorkloadClass.NONE, "EMERGENCY ${"%.1f".format(temp)}C", emergencyStop = true)
        }
        if (b == Band.RED || health == "OVERHEAT") {
            return Decision(Band.RED, WorkloadClass.NONE, "thermal RED ${temp?.let { "%.1f".format(it) }}C")
        }
        if (nowMs < localCooldownUntilMs) {
            return Decision(b, WorkloadClass.NONE, "local cooldown")
        }
        val pct = snapshot.batteryPct
        val charging = snapshot.charging == true
        if (pct != null) {
            if (pct < t.batteryCriticalPct) return Decision(b, WorkloadClass.NONE, "battery critical ${pct.toInt()}%")
            if (!charging && pct < t.batteryMinPctDischarging) {
                return Decision(b, WorkloadClass.NONE, "battery ${pct.toInt()}% (not charging)")
            }
        }
        var cls = when (b) {
            Band.ORANGE, Band.UNKNOWN -> WorkloadClass.LIGHT
            else -> WorkloadClass.ALL
        }
        if (cls == WorkloadClass.ALL && charging && pct != null && pct >= 99 && b != Band.GREEN) {
            cls = WorkloadClass.LIGHT // avoid full-charge + high-temperature cycles
        }
        if (cls == WorkloadClass.ALL && !charging && (pct == null || pct < t.batteryHeavyMinPctDischarging)) {
            cls = WorkloadClass.LIGHT
        }
        var reason: String? = when (b) {
            Band.YELLOW -> "thermal YELLOW"
            Band.ORANGE -> "thermal ORANGE: light only"
            Band.UNKNOWN -> "no temperature: light only"
            else -> null
        }

        if (server != null) {
            if (server.emergencyStop) return Decision(b, WorkloadClass.NONE, "server emergency stop", true)
            if (server.paused) return Decision(b, WorkloadClass.NONE, "server: ${server.reason ?: "paused"}")
            val serverCls = runCatching { WorkloadClass.valueOf(server.workloadClass) }.getOrDefault(WorkloadClass.LIGHT)
            if (serverCls.ordinal > cls.ordinal) {
                cls = serverCls
                reason = "server: ${server.reason ?: serverCls.name}"
            }
        }
        return Decision(b, cls, reason)
    }
}

/** Device telemetry snapshot (populated by [DeviceTelemetry] on Android). */
data class TelemetrySnapshot(
    val temperatureC: Double? = null,
    val batteryTemperatureC: Double? = null,
    val batteryPct: Double? = null,
    val charging: Boolean? = null,
    val batteryHealth: String? = null,
    val thermalStatus: Int? = null,
    val ramUsedMb: Int? = null,
    val ramTotalMb: Int? = null,
    val storageFreeMb: Long? = null,
    val storageTotalMb: Long? = null,
    val networkType: String? = null,
    val downMbps: Int? = null,
    val upMbps: Int? = null,
)

/** Safety block returned by the backend heartbeat response. */
data class ServerSafety(
    val temperatureState: String,
    val workloadClass: String,
    val paused: Boolean,
    val reason: String?,
    val emergencyStop: Boolean,
)
