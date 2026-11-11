package com.farmos.node

import com.farmos.node.safety.ServerSafety
import com.farmos.node.safety.TelemetrySnapshot
import com.farmos.node.safety.ThermalGovernor
import com.farmos.node.safety.ThermalGovernor.Band
import com.farmos.node.safety.ThermalGovernor.WorkloadClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM tests for the on-device thermal/battery governor (spec §10). */
class ThermalGovernorTest {
    private fun snap(temp: Double? = 30.0, pct: Double? = 90.0, charging: Boolean? = true,
                     health: String? = "GOOD", thermalStatus: Int? = null) =
        TelemetrySnapshot(temperatureC = temp, batteryTemperatureC = temp, batteryPct = pct,
            charging = charging, batteryHealth = health, thermalStatus = thermalStatus)

    @Test fun bandsMatchSpec() {
        assertEquals(Band.UNKNOWN, ThermalGovernor.band(null))
        assertEquals(Band.GREEN, ThermalGovernor.band(37.9))
        assertEquals(Band.YELLOW, ThermalGovernor.band(38.0))
        assertEquals(Band.ORANGE, ThermalGovernor.band(42.0))
        assertEquals(Band.ORANGE, ThermalGovernor.band(45.0))
        assertEquals(Band.RED, ThermalGovernor.band(45.1))
    }

    @Test fun greenAllowsEverything() {
        val d = ThermalGovernor.decide(snap())
        assertEquals(WorkloadClass.ALL, d.workloadClass)
        assertTrue(d.allows("benchmark"))
    }

    @Test fun orangeLightOnly() {
        val d = ThermalGovernor.decide(snap(temp = 43.0))
        assertTrue(d.allows("http_check"))
        assertFalse(d.allows("benchmark"))
    }

    @Test fun redStopsAndEmergencyFlags() {
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(temp = 46.0)).workloadClass)
        assertTrue(ThermalGovernor.decide(snap(temp = 51.0)).emergencyStop)
    }

    @Test fun androidThermalStatusSevereIsRed() {
        val d = ThermalGovernor.decide(snap(temp = 30.0, thermalStatus = ThermalGovernor.THERMAL_STATUS_SEVERE))
        assertEquals(WorkloadClass.NONE, d.workloadClass)
    }

    @Test fun batteryRules() {
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(pct = 30.0, charging = false)).workloadClass)
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(pct = 10.0, charging = true)).workloadClass)
        assertEquals(WorkloadClass.LIGHT, ThermalGovernor.decide(snap(pct = 50.0, charging = false)).workloadClass)
        assertTrue(ThermalGovernor.decide(snap(health = "OVER_VOLTAGE")).emergencyStop)
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(health = "OVERHEAT")).workloadClass)
    }

    @Test fun localCooldownBlocks() {
        val d = ThermalGovernor.decide(snap(), null, nowMs = 1_000L, localCooldownUntilMs = 5_000L)
        assertEquals(WorkloadClass.NONE, d.workloadClass)
    }

    @Test fun serverCanOnlyTighten() {
        val paused = ServerSafety("COOLING", "NONE", true, "cooling", false)
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(), paused).workloadClass)
        val light = ServerSafety("ORANGE", "LIGHT", false, null, false)
        assertEquals(WorkloadClass.LIGHT, ThermalGovernor.decide(snap(), light).workloadClass)
        // Server saying ALL cannot loosen a locally-hot phone.
        val all = ServerSafety("GREEN", "ALL", false, null, false)
        assertEquals(WorkloadClass.NONE, ThermalGovernor.decide(snap(temp = 46.0), all).workloadClass)
    }
}
