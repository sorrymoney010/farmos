package com.farmos.node.safety

/**
 * Process-wide safety state shared by HeartbeatLoop (writer) and JobLoop
 * (reader). Values are volatile; stale server state is ignored after 3 minutes
 * (backend offline window), falling back to the local governor alone.
 */
object NodeSafetyState {
    private const val SERVER_STATE_TTL_MS = 180_000L

    @Volatile var lastSnapshot: TelemetrySnapshot = TelemetrySnapshot()
    @Volatile private var server: ServerSafety? = null
    @Volatile private var serverAtMs: Long = 0L
    @Volatile var benchmarkRequired: Boolean = false
    @Volatile var deviceStatus: String = "UNKNOWN"
    @Volatile var farmScore: Double? = null
    @Volatile var localCooldownUntilMs: Long = 0L

    fun updateFromServer(safety: ServerSafety?, status: String?, benchmarkRequired: Boolean, farmScore: Double?) {
        server = safety
        serverAtMs = System.currentTimeMillis()
        status?.let { deviceStatus = it }
        this.benchmarkRequired = benchmarkRequired
        this.farmScore = farmScore
    }

    fun serverSafety(nowMs: Long = System.currentTimeMillis()): ServerSafety? =
        if (nowMs - serverAtMs <= SERVER_STATE_TTL_MS) server else null

    /** Start a local cooldown window (called whenever the governor sees RED). */
    fun noteRed(nowMs: Long = System.currentTimeMillis(), t: ThermalGovernor.Thresholds = ThermalGovernor.Thresholds()) {
        localCooldownUntilMs = maxOf(localCooldownUntilMs, nowMs + t.localCooldownMs)
    }

    fun decide(snapshot: TelemetrySnapshot = lastSnapshot): ThermalGovernor.Decision {
        val now = System.currentTimeMillis()
        val d = ThermalGovernor.decide(snapshot, serverSafety(now), now, localCooldownUntilMs)
        if (d.band == ThermalGovernor.Band.RED) noteRed(now)
        return d
    }
}
