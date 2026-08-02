import httpx
import pytest


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c
