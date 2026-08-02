from __future__ import annotations

from typing import Any

import httpx

from search_meta_api.providers.base import Adapter
from search_meta_api.providers.brave import BraveAdapter
from search_meta_api.providers.exa import ExaAdapter
from search_meta_api.providers.linkup import LinkupAdapter
from search_meta_api.providers.tavily import TavilyAdapter
from search_meta_api.providers.tinyfish import TinyFishAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    BraveAdapter.type_name: BraveAdapter,
    ExaAdapter.type_name: ExaAdapter,
    LinkupAdapter.type_name: LinkupAdapter,
    TavilyAdapter.type_name: TavilyAdapter,
    TinyFishAdapter.type_name: TinyFishAdapter,
}

# Options each type must be given in config, beyond name/type.
REQUIRED_OPTIONS: dict[str, tuple[str, ...]] = {
    "brave": ("api_key",),
    "exa": ("api_key",),
    "linkup": ("api_key",),
    "tavily": ("api_key",),
    "tinyfish": ("api_key",),
}

OPTION_FIELDS = ("api_key", "base_url", "cx")


def build_adapter(cfg: Any, client: httpx.AsyncClient, default_timeout_ms: int) -> Adapter:
    options = {f: getattr(cfg, f) for f in OPTION_FIELDS if getattr(cfg, f, None) is not None}
    return ADAPTERS[cfg.type](
        cfg.name,
        client,
        timeout_ms=cfg.timeout_ms if cfg.timeout_ms is not None else default_timeout_ms,
        **options,
    )
