from __future__ import annotations

from typing import Iterator, Type

from ..config import CrawlConfig, load_crawl_config
from .base import SiteAdapter


def _load() -> dict[str, Type[SiteAdapter]]:
    from .aliexpress import AliExpressAdapter
    from .alibaba import AlibabaAdapter
    from .banggood import BanggoodAdapter, GearBestAdapter
    from .chinavasion import ChinavasionAdapter
    from .components import LcscAdapter, OctopartAdapter
    from .dhgate import DHgateAdapter
    from .ebay import EbayAdapter
    from .globalsources import GlobalSourcesAdapter
    from .madeinchina import MadeInChinaAdapter
    from .storefronts import GeekbuyingAdapter, TomtopAdapter
    from .taobao_family import Alibaba1688Adapter, TaobaoAdapter, TmallAdapter

    classes = [
        AliExpressAdapter, AlibabaAdapter, Alibaba1688Adapter, TaobaoAdapter,
        TmallAdapter, DHgateAdapter, ChinavasionAdapter, GlobalSourcesAdapter,
        MadeInChinaAdapter, GearBestAdapter, BanggoodAdapter, EbayAdapter,
        LcscAdapter, OctopartAdapter, TomtopAdapter, GeekbuyingAdapter,
    ]
    return {c.key: c for c in classes}


ADAPTERS: dict[str, Type[SiteAdapter]] = _load()


def get_adapter(site_key: str, config: CrawlConfig | None = None) -> SiteAdapter:
    try:
        cls = ADAPTERS[site_key]
    except KeyError:
        raise KeyError(
            f"unknown site {site_key!r}; known: {', '.join(sorted(ADAPTERS))}"
        ) from None
    return cls(config or load_crawl_config())


def iter_adapters(
    site_keys: list[str] | None = None, config: CrawlConfig | None = None
) -> Iterator[SiteAdapter]:
    cfg = config or load_crawl_config()
    keys = site_keys or cfg.enabled_sites() or list(ADAPTERS)
    for k in keys:
        if k in ADAPTERS and cfg.site(k).get("enabled", True):
            yield get_adapter(k, cfg)
