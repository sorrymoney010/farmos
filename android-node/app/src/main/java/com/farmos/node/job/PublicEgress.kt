package com.farmos.node.job

import okhttp3.ConnectionPool
import okhttp3.Dns
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import java.net.InetAddress
import java.net.Proxy
import java.net.UnknownHostException

/** Validate the exact addresses OkHttp uses, not a separate preflight DNS lookup. */
object PublicEgress {
    fun isPublic(address: InetAddress): Boolean {
        val b = address.address.map { it.toInt() and 255 }
        if (address.isAnyLocalAddress || address.isLoopbackAddress || address.isLinkLocalAddress ||
            address.isSiteLocalAddress || address.isMulticastAddress) return false
        if (b.size == 4) {
            val a = b[0]; val c = b[1]
            return !(a == 0 || a == 10 || a == 127 || a >= 224 ||
                (a == 100 && c in 64..127) || (a == 169 && c == 254) ||
                (a == 172 && c in 16..31) || (a == 192 && c == 168) ||
                (a == 192 && c == 0) || (a == 192 && c == 88 && b[2] == 99) ||
                (a == 198 && c in 18..19) || (a == 198 && c == 51 && b[2] == 100) ||
                (a == 203 && c == 0 && b[2] == 113))
        }
        // Conservative IPv6 global unicast only; exclude transition, special
        // allocations and documentation. IPv4-mapped literals become IPv4 above.
        return b.size == 16 && (b[0] and 224) == 32 &&
            !(b[0] == 32 && b[1] == 1 && b[2] < 2) &&
            !(b[0] == 32 && b[1] == 2) &&
            !(b[0] == 32 && b[1] == 1 && b[2] == 13 && b[3] == 184) &&
            !(b[0] == 63 && b[1] == 255 && (b[2] and 240) == 0)
    }

    fun validateUrl(url: HttpUrl) {
        require(url.isHttps && url.port in setOf(443, 8443) &&
            url.username.isEmpty() && url.password.isEmpty() && !url.host.contains('%')) {
            "Job destination must use public HTTPS on 443 or 8443"
        }
        // OkHttp may bypass Dns for IP literals, so check them here as well.
        if (url.host.contains(':') || url.host.matches(Regex("[0-9.]+"))) {
            require(isPublic(InetAddress.getByName(url.host))) { "Non-public destination" }
        }
    }

    class PublicDns(private val delegate: Dns = Dns.SYSTEM) : Dns {
        override fun lookup(hostname: String): List<InetAddress> {
            val answers = delegate.lookup(hostname)
            if (answers.isEmpty() || answers.any { !isPublic(it) }) {
                throw UnknownHostException("Destination must resolve exclusively to public addresses")
            }
            return answers
        }
    }

    fun client(base: OkHttpClient): OkHttpClient = base.newBuilder()
        .dns(PublicDns(base.dns))
        .proxy(Proxy.NO_PROXY)
        .connectionPool(ConnectionPool())
        .followRedirects(false)
        .followSslRedirects(false)
        .addInterceptor { chain ->
            validateUrl(chain.request().url)
            chain.proceed(chain.request())
        }
        .build()
}
