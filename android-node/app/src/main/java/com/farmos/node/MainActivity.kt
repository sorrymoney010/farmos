package com.farmos.node

import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.farmos.node.identity.DeviceIdentityStore
import com.farmos.node.node.NodeEnrollmentRepository
import com.farmos.node.node.NodeEnrollmentScheduler
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {
    private lateinit var identityStore: DeviceIdentityStore
    private lateinit var repository: NodeEnrollmentRepository

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        identityStore = DeviceIdentityStore(this)
        repository = NodeEnrollmentRepository(this, identityStore)

        val statusBody = findViewById<TextView>(R.id.statusBody)
        val apiUrlInput = findViewById<EditText>(R.id.apiUrlInput)
        val tokenInput = findViewById<EditText>(R.id.tokenInput)
        val enrollButton = findViewById<Button>(R.id.enrollButton)
        val saveUrlButton = findViewById<Button>(R.id.saveUrlButton)

        // Pre-fill the configured API URL (default HTTPS build URL unless overridden).
        apiUrlInput.setText(identityStore.apiBaseUrl())
        // Already enrolled? Show status and ensure heartbeats run.
        if (identityStore.isEnrolled()) {
            statusBody.text = "Enrolled as ${identityStore.humanId()}. Heartbeats scheduled."
            NodeEnrollmentScheduler.onEnrolled(this)
        }

        // Deep link: farmos://enroll?token=... or App Link https://.../enroll?token=...
        val data = intent?.data
        if (data != null) {
            val isEnrollDeepLink = data.scheme == "farmos" && data.host == "enroll"
            val isEnrollAppLink = data.scheme == "https" && data.path?.startsWith("/enroll") == true
            if (isEnrollDeepLink || isEnrollAppLink) {
                val token = data.getQueryParameter("token")
                if (!token.isNullOrBlank()) tokenInput.setText(token)
            }
        }

        saveUrlButton.setOnClickListener {
            val url = apiUrlInput.text.toString().trim()
            runCatching { identityStore.setApiBase(url) }
                .onSuccess { Toast.makeText(this, "API URL saved", Toast.LENGTH_SHORT).show() }
                .onFailure { Toast.makeText(this, "HTTPS URL required", Toast.LENGTH_SHORT).show() }
        }

        enrollButton.setOnClickListener {
            val token = tokenInput.text.toString().trim()
            if (token.isBlank()) {
                statusBody.text = "Enter a claim code or enrollment token first."
                return@setOnClickListener
            }
            lifecycleScope.launch {
                statusBody.text = "Generating Keystore identity and enrolling…"
                // Short codes (no "enroll-" prefix) go through claim-code exchange (wireless path).
                val useClaim = !token.startsWith("enroll-") && token.length <= 16
                runCatching {
                    if (useClaim) repository.enrollWithClaimCode(token) else repository.enroll(token)
                }
                    .onSuccess { result ->
                        NodeEnrollmentScheduler.onEnrolled(this@MainActivity)
                        statusBody.text = "Device ${result.humanId} is ${result.status}"
                    }
                    .onFailure { error ->
                        statusBody.text = "Enrollment failed: ${error.message}"
                    }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        // Stop the in-process loop when the activity is destroyed (fallback worker remains).
        com.farmos.node.node.HeartbeatLoop.stop()
    }
}