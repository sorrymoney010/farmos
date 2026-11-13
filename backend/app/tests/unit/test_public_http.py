import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from app.security import public_http as guard


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
    '192.168.1.1', '100.64.0.1', '0.0.0.0', '224.0.0.1', '192.0.2.1', '::1',
    'fc00::1', 'fe80::1', '::ffff:127.0.0.1', '64:ff9b::a00:1', '2002:0a00:0001::1',
    '2001::1', 'ff02::1'])
def test_non_public(address):
    assert not guard.public_address(address)


@pytest.mark.parametrize('address', ['1.1.1.1', '8.8.8.8', '2606:4700:4700::1111'])
def test_public(address):
    assert guard.public_address(address)


@pytest.mark.asyncio
async def test_dns_rejects_mixed_answers(monkeypatch):
    loop = guard.asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[
        (socket.AF_INET, 1, 6, '', ('1.1.1.1', 443)),
        (socket.AF_INET, 1, 6, '', ('10.0.0.1', 443))]))
    with pytest.raises(guard.EgressDenied):
        await guard.resolve_public('mixed.example', 443)


@pytest.mark.asyncio
async def test_pins_ip_preserves_tls_and_host_and_disables_redirects(monkeypatch):
    calls = []
    resolver = AsyncMock(side_effect=['1.1.1.1', guard.EgressDenied('rebound')])
    monkeypatch.setattr(guard, 'resolve_public', resolver)

    class Wire(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            calls.append(req)
            return httpx.Response(302, headers={'Location': 'https://127.0.0.1/'},
                                  stream=httpx.ByteStream(b'redirect'))
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kw: Wire())
    async with httpx.AsyncClient(transport=guard.PublicHTTPTransport(), follow_redirects=False) as client:
        r = await client.get('https://public.example/path', headers={'Host': 'evil.example'})
        assert r.status_code == 302
        assert len(calls) == 1
        assert calls[0].url.host == '1.1.1.1'
        assert calls[0].headers['host'] == 'public.example'
        assert calls[0].extensions['sni_hostname'] == 'public.example'
        with pytest.raises(guard.EgressDenied):
            await client.get('https://public.example/path')
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://public.example', 'https://user:pass@public.example',
                                 'https://public.example:22'])
async def test_url_denied_before_dns(monkeypatch, url):
    resolver = AsyncMock()
    monkeypatch.setattr(guard, 'resolve_public', resolver)
    async with httpx.AsyncClient(transport=guard.PublicHTTPTransport()) as client:
        with pytest.raises(guard.EgressDenied):
            await client.get(url)
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_response_size_limit(monkeypatch):
    monkeypatch.setattr(guard, 'resolve_public', AsyncMock(return_value='1.1.1.1'))
    class Wire(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            return httpx.Response(200, stream=httpx.ByteStream(b'x' * 1025))
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kw: Wire())
    transport = guard.PublicHTTPTransport()
    transport.MAX_RESPONSE_BYTES = 1024
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(guard.EgressDenied):
            await client.get('https://public.example')


@pytest.mark.parametrize('url', ['https://100.64.0.1', 'https://example.com:8080',
    'https://example.com:bad', 'https://[broken', 'https://[64:ff9b::a00:1]'])
def test_deployment_rejects_unsafe_or_malformed_url(url):
    from app.services.developer.deployments import check_url, DeploymentError
    with pytest.raises(DeploymentError):
        check_url(url)


def test_deployment_accepts_public_ipv6():
    from app.services.developer.deployments import check_url
    assert check_url('https://[2606:4700:4700::1111]')


@pytest.mark.asyncio
async def test_dns_failure_is_transport_error(monkeypatch):
    loop = guard.asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(side_effect=socket.gaierror('no DNS')))
    with pytest.raises(guard.EgressDenied):
        await guard.resolve_public('invalid.example', 443)


@pytest.mark.asyncio
async def test_rejects_encoded_response(monkeypatch):
    monkeypatch.setattr(guard, 'resolve_public', AsyncMock(return_value='1.1.1.1'))
    class Wire(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            assert req.headers['accept-encoding'] == 'identity'
            return httpx.Response(200, headers={'Content-Encoding': 'gzip'},
                                  stream=httpx.ByteStream(b'compressed'))
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kw: Wire())
    async with httpx.AsyncClient(transport=guard.PublicHTTPTransport()) as client:
        with pytest.raises(guard.EgressDenied, match='Encoded'):
            await client.get('https://public.example')
