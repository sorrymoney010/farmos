package com.farmos.node.identity

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

class AndroidKeyStoreManager {
    private val alias = "farmos-node-key"
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun ensureKeypair(): PublicKey {
        val existing = keyStore.getCertificate(alias)?.publicKey
        if (existing != null) return existing

        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
        val spec = KeyGenParameterSpec.Builder(
            alias,
            KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
        )
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setUserAuthenticationRequired(false)
            .build()
        generator.initialize(spec)
        return generator.generateKeyPair().public
    }

    fun publicKeyPem(): String {
        val publicKey = ensureKeypair()
        val encoded = Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(publicKey.encoded)
        return "-----BEGIN PUBLIC KEY-----\n$encoded\n-----END PUBLIC KEY-----\n"
    }

    fun sign(bytes: ByteArray): String {
        ensureKeypair()
        val privateKey = keyStore.getKey(alias, null) as PrivateKey
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initSign(privateKey)
        signature.update(bytes)
        return Base64.getEncoder().encodeToString(signature.sign())
    }
}
