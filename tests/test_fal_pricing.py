from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

from ace_service.costs import FalPricingClient


def test_fal_pricing_is_cached_exact_and_total_is_unit_gated() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/models/pricing"
        assert request.headers["authorization"] == "Key test-key"
        return httpx.Response(
            200,
            json={
                "prices": [
                    {
                        "endpoint_id": "cassetteai/music-generator",
                        "unit_price": 0.002,
                        "unit": "seconds",
                    }
                ]
            },
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pricing = FalPricingClient("test-key", ttl_seconds=60, client=client)
        first = await pricing.estimate(
            "cassetteai/music-generator",
            unit_quantity=Decimal("123.5"),
            declared_unit="second",
        )
        second = await pricing.estimate(
            "cassetteai/music-generator",
            unit_quantity=Decimal("123.5"),
            declared_unit="request",
        )
        assert first is not None and first.total_micro_usd == 247_000
        assert first.unit_price_usd == "0.002"
        assert second is not None and second.total_micro_usd is None
        assert calls == 1
        await client.aclose()

    asyncio.run(scenario())
