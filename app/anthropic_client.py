import os

import anthropic
import httpx


def anthropic_client(api_key: str) -> anthropic.AsyncAnthropic:
    socket_path = os.getenv("ANTHROPIC_UNIX_SOCKET")
    if not socket_path:
        return anthropic.AsyncAnthropic(api_key=api_key)

    # SSH carries the TLS stream. Certificate verification still uses the API hostname.
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        base_url="https://api.anthropic.com",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            follow_redirects=False,
        ),
    )
