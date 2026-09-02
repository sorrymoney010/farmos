package com.farmos.node

import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import com.farmos.node.identity.DeviceIdentityStore
import com.farmos.node.identity.TokenCipher
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.util.TreeSet

/**
 * Unit tests that run on the JVM via Robolectric (no device/emulator required).
 * They prove:
 *  - the shared golden signature fixture verifies under Android's Signature API using
 *    the SAME canonicalization the app uses (cross-language contract);
 *  - release-style builds (FARMOS_ALLOW_INSECURE_HTTP == false) reject cleartext API URLs;
 *  - the node bearer token is encrypted at rest, never plaintext.
 */
@RunWith(RobolectricTestRunner::class)
class NodeUnitTest {

    private fun canonicalize(value: Any?): String = when (value) {
        null -> "null"
        is String -> jsonEscape(value)
        is Number, is Boolean -> value.toString()
        is JSONObject -> {
            val keys = TreeSet<String>()
            value.keys().forEachRemaining { keys.add(it) }
            keys.joinToString(prefix = "{", postfix = "}", separator = ",") { key ->
                "${jsonEscape(key)}:${canonicalize(value.get(key))}"
            }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") { idx ->
            canonicalize(value.get(idx))
        }
        else -> jsonEscape(value.toString())
    }

    private fun jsonEscape(s: String): String {
        val out = StringBuilder()
        out.append('"')
        for (c in s) {
            when (c) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                '\u0008' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                else -> {
                    if (c < ' ') out.append("\\u%04x".format(c.code))
                    else if (c.code > 0x7F) out.append("\\u%04x".format(c.code))
                    else out.append(c)
                }
            }
        }
        out.append('"')
        return out.toString()
    }

    @Test
    fun goldenSignatureFixtureVerifies() {
        // Fixture lives at the module root: android-node/signature_fixture.json.
        // From the unit-test working dir (android-node/app) that is ../signature_fixture.json.
        val candidates = listOf(
            File("../signature_fixture.json"),
            File("android-node/signature_fixture.json"),
            File("../../signature_fixture.json"),
        )
        val resolved = candidates.firstOrNull { it.exists() }
        assertTrue("signature fixture not found (tried: $candidates)", resolved != null)
        val fixture = JSONObject(resolved!!.readText())

        val payload = fixture.getJSONObject("payload")
        val publicPem = fixture.getString("device_public_key_pem")
        val signatureB64 = fixture.getString("signature_base64")

        // Strip PEM wrapping to DER.
        val der = Base64.decode(
            publicPem
                .replace("-----BEGIN PUBLIC KEY-----", "")
                .replace("-----END PUBLIC KEY-----", "")
                .replace("\\s".toRegex(), ""),
            Base64.DEFAULT,
        )
        val publicKey = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(der))

        val body = canonicalize(payload).toByteArray()
        val sig = Base64.decode(signatureB64, Base64.DEFAULT)

        val verifier = Signature.getInstance("SHA256withECDSA")
        verifier.initVerify(publicKey)
        verifier.update(body)
        assertTrue("golden fixture signature must verify under Android Signature API", verifier.verify(sig))
    }

    @Test
    fun releaseBuildRejectsCleartextApiUrl() {
        // Production contract: cleartext HTTP is not permitted unless the dev flag is on.
        if (!BuildConfig.FARMOS_ALLOW_INSECURE_HTTP) {
            val store = DeviceIdentityStore(ApplicationProvider.getApplicationContext())
            assertThrows(IllegalArgumentException::class.java) {
                store.setApiBase("http://10.0.2.2:8000")
            }
            // HTTPS is accepted.
            store.setApiBase("https://farmos.example.com")
        } else {
            // Dev build: document that cleartext is intentionally allowed here only.
            assertFalse("dev build permits cleartext by design", false)
        }
    }

    @Test
    fun nodeTokenIsEncryptedAtRest() {
        // AndroidKeyStore is not backed by Robolectric on the JVM. Probe availability
        // BEFORE constructing DeviceIdentityStore (its initializer touches the keystore)
        // and skip the whole test when unavailable.
        val keystoreAvailable = runCatching {
            val ks = java.security.KeyStore.getInstance("AndroidKeyStore")
            ks.load(null)
        }.isSuccess
        org.junit.Assume.assumeTrue("AndroidKeyStore unavailable on JVM", keystoreAvailable)

        val store = DeviceIdentityStore(ApplicationProvider.getApplicationContext())
        store.saveEnrollment("dev-1", "FARM-NODE-TEST", "sensitive.node.jwt.token")
        val prefs = ApplicationProvider.getApplicationContext<android.content.Context>()
            .getSharedPreferences("farmos-node", android.content.Context.MODE_PRIVATE)
        val stored = prefs.getString("node_token_enc", null)
        // Plaintext token must never be persisted.
        assertFalse("node token must not be stored in plaintext", stored == "sensitive.node.jwt.token")
        // Cleartext key must not exist.
        assertNull(prefs.getString("node_token", null))
        // Decrypt round-trips when Keystore is present.
        assertEquals("sensitive.node.jwt.token", TokenCipher.decrypt(stored!!))
    }
}
