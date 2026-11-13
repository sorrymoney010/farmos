"""Public-only HTTP transport: resolve once, validate all answers, connect to IP.

Each request gets a separate pool so two hostnames sharing an IP cannot reuse
one another's TLS session. Original Host and TLS SNI/certificate name survive
IP pinning. Environment proxies and redirects must never bypass this boundary.
"""
import asyncio
import ipaddress
import socket

import httpx


class EgressDenied(httpx.TransportError):
    pass


def public_address(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if '%' in value or not ip.is_global or ip.is_multicast:
        return False
    if ip.version == 6:
        # Reject mapped/transition/translation ranges, including private IPv4
        # destinations hidden inside otherwise globally routable IPv6 prefixes.
        return (ip in ipaddress.ip_network('2000::/3')
                and ip not in ipaddress.ip_network('2001::/23')
                and ip not in ipaddress.ip_network('2002::/16')
                and ip not in ipaddress.ip_network('3fff::/20'))
    return True


async def resolve_public(host: str, port: int) -> str:
    try:
        rows = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5)
    except (OSError, asyncio.TimeoutError) as exc:
        raise EgressDenied('Destination DNS resolution failed') from exc
    addresses = [row[4][0] for row in rows]
    if not addresses or not all(public_address(ip) for ip in addresses):
        raise EgressDenied('Destination must resolve exclusively to public addresses')
    return addresses[0]


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    MAX_RESPONSE_BYTES = 1024 * 1024

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (url.scheme != 'https' or not url.host or url.userinfo
                or (url.port or 443) not in (443, 8443) or '%' in url.host):
            raise EgressDenied('Outbound jobs and webhooks require public HTTPS on 443 or 8443')
        address = await resolve_public(url.host, url.port or 443)
        headers = request.headers.copy()
        headers['Host'] = url.netloc.decode('ascii')
        headers['Accept-Encoding'] = 'identity'
        # Do not accept user-supplied SNI or proxy routing. This pool receives
        # only the validated literal IP; DNS cannot change between check/connect.
        extensions = {**request.extensions, 'sni_hostname': url.host}
        pinned = httpx.Request(request.method, url.copy_with(host=address), headers=headers,
                               stream=request.stream, extensions=extensions)
        async with httpx.AsyncHTTPTransport(retries=0, trust_env=False) as transport:
            response = await transport.handle_async_request(pinned)
            data = bytearray()
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                await response.aclose()
                raise EgressDenied('Encoded responses are not supported')
            try:
                async for chunk in response.aiter_raw():
                    data.extend(chunk)
                    if len(data) > self.MAX_RESPONSE_BYTES:
                        raise EgressDenied('Outbound response exceeds 1 MiB limit')
            finally:
                await response.aclose()
        # Preserve raw encoding headers: HTTPX decodes the buffered response.
        return httpx.Response(response.status_code, headers=response.headers,
                              content=bytes(data), extensions=response.extensions)
