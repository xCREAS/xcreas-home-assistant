"""Shared fixtures for the xCREAS integration tests.

The suite runs against pytest-homeassistant-custom-component (pinned to the
HA version in requirements_test.txt). The contract samples live in
fixtures/contract_samples.json and mirror docs/ha-integration.md §4/§5.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

# Load the pytest_homeassistant_custom_component plugins (hass fixtures,
# aioclient_mock, enable_custom_integrations...).
pytest_plugins = "pytest_homeassistant_custom_component"

from custom_components.xcreas.api import XcreasApiClient
from custom_components.xcreas.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the repo's custom_components/xcreas discoverable to the loader."""
    yield

FIXTURES = Path(__file__).parent / "fixtures" / "contract_samples.json"

with FIXTURES.open() as _f:
    CONTRACT: dict[str, Any] = json.load(_f)


@pytest.fixture
def contract() -> dict[str, Any]:
    """The shared contract samples (docs/ha-integration.md §4/§5)."""
    return deepcopy(CONTRACT)


class FakeTokenSession:
    """A static-token stand-in for OAuth2Session."""

    def __init__(self, token: str = "test-access-token") -> None:
        self._token = token

    async def __call__(self) -> str:
        return self._token


class FakeAiohttpSession:
    """Minimal aiohttp session stand-in that serves canned responses.

    Mirrors the `await session.request(...)` → resp shape the client uses.
    """

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, str, Any]] = []

    async def request(self, method: str, url: str, **kwargs):
        from urllib.parse import urlparse

        body = self._responses.get((method, urlparse(url).path))
        self.requests.append((method, url, kwargs))
        if isinstance(body, _HttpError):
            return _FakeResp(body.status, body.body)
        return _FakeResp(200, body)


class _FakeResp:
    def __init__(self, status: int, body) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _HttpError:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body


def make_client(responses: dict[tuple[str, str], Any]) -> XcreasApiClient:
    """A client over a canned session with a static token."""
    return XcreasApiClient(FakeAiohttpSession(responses), "https://api.xcreas.test", FakeTokenSession())


@pytest.fixture
def snapshot_response() -> dict[tuple[str, str], Any]:
    return {("GET", "/api/integrations/ha/snapshot"): CONTRACT["snapshot"]}
