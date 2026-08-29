from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        admin_token=ADMIN_TOKEN,
        chaos_enabled=True,
        limiter_backend="memory",
        _env_file=None,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def store(app):
    return app.state.store


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://stormdoor.test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def key(store):
    """A permissive key: no budget, no rate limits, every model."""
    return await store.create_key(name="test")


@pytest_asyncio.fixture
async def auth(key):
    _key, secret = key
    return {"Authorization": f"Bearer {secret}"}


def chat_body(**overrides) -> dict:
    body = {
        "model": "echo-small",
        "messages": [{"role": "user", "content": "hello stormdoor"}],
    }
    body.update(overrides)
    return body
