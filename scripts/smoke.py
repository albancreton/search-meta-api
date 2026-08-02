"""Call every configured provider once, for real. Spends credits.

Usage: python scripts/smoke.py [query]
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

from search_meta_api.config import load_settings
from search_meta_api.models import SearchRequest
from search_meta_api.providers.base import resolve_request
from search_meta_api.providers.registry import build_adapter


async def main() -> int:
    query = " ".join(sys.argv[1:]) or "claude opus 5 pricing"
    settings = load_settings(os.environ.get("CONFIG_PATH", "config.yaml"))
    request = SearchRequest(query=query, max_results=3)
    failures = 0

    async with httpx.AsyncClient() as client:
        for cfg in settings.providers:
            adapter = build_adapter(cfg, client, settings.defaults.timeout_ms)
            effective, passthrough, degraded = resolve_request(
                request, cfg.name, cfg.type, adapter.capabilities
            )
            try:
                results = await adapter.search(effective, passthrough)
            except Exception as exc:  # noqa: BLE001 — the point is to report anything
                failures += 1
                print(f"FAIL  {cfg.name:<12} {exc}")
                continue
            note = f" degraded={degraded}" if degraded else ""
            print(f"ok    {cfg.name:<12} {len(results)} results{note}")
            for result in results[:2]:
                print(f"        {result.title[:60]} — {result.url}")

    print(f"\n{len(settings.providers) - failures}/{len(settings.providers)} providers healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
