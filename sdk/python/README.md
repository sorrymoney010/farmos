# farmos-sdk

Python SDK + `farmos` CLI for the FARMOS developer platform. See
`docs/DEVELOPER_PLATFORM_BILLING.md` in the repo for the API.

```bash
pip install -e sdk/python
export FARMOS_API_URL=http://localhost:8000 FARMOS_API_KEY=fos_test_...
farmos balance
farmos deploy http_check '{"url":"https://example.com"}' --every 300 --runs 12 --max-price 0.01
```
