package com.farmos.node

import com.farmos.node.job.PublicEgress
import okhttp3.Dns
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress
import java.net.UnknownHostException

class PublicEgressTest {
    @Test fun blocksNonPublicAddresses() {
        listOf("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
            "169.254.169.254", "100.64.0.1", "224.0.0.1", "192.0.2.1",
            "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
            "64:ff9b::a00:1", "2002:a00:1::1", "2001:db8::1", "3fff::1")
            .forEach { assertFalse(it, PublicEgress.isPublic(InetAddress.getByName(it))) }
    }

    @Test fun acceptsPublicAddresses() {
        listOf("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111")
            .forEach { assertTrue(it, PublicEgress.isPublic(InetAddress.getByName(it))) }
    }

    @Test fun rejectsMixedDnsAndLaterRebinding() {
        var calls = 0
        val dns = PublicEgress.PublicDns(object : Dns {
            override fun lookup(hostname: String): List<InetAddress> {
                calls++
                return if (calls == 1) listOf(InetAddress.getByName("1.1.1.1"))
                else listOf(InetAddress.getByName("1.1.1.1"), InetAddress.getByName("10.0.0.1"))
            }
        })
        assertEquals("1.1.1.1", dns.lookup("example.test").single().hostAddress)
        assertThrows(UnknownHostException::class.java) { dns.lookup("example.test") }
    }

    @Test fun validatesLiteralUrlsAndDisablesRedirects() {
        listOf("http://example.com", "https://127.0.0.1", "https://[::1]",
            "https://user:pass@example.com", "https://example.com:22").forEach {
            assertThrows(IllegalArgumentException::class.java) { PublicEgress.validateUrl(it.toHttpUrl()) }
        }
        PublicEgress.validateUrl("https://example.com".toHttpUrl())
        val client = PublicEgress.client(OkHttpClient())
        assertFalse(client.followRedirects)
        assertFalse(client.followSslRedirects)
        assertEquals(java.net.Proxy.NO_PROXY, client.proxy)
    }
}
