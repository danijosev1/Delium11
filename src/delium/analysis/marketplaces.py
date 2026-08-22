"""Centralized Amazon-marketplace reference data (docs/cross-market.md §2).

Static reference facts (currency, locale, domain, unit system) — not user-tunable
config. New marketplaces are added HERE and in the `Marketplace` enum; the
cross-market engine reads this registry and never hardcodes a marketplace.

Pure data + lookups: no I/O, no network, no config, no LLM.
"""

from __future__ import annotations

from delium.analysis.models import Marketplace, MarketplaceInfo

# Well-known Amazon marketplace ids; reference data, reviewed when marketplaces
# are added. Unit system drives the localization "units differ" flag only.
MARKETPLACES: dict[Marketplace, MarketplaceInfo] = {
    Marketplace.US: MarketplaceInfo(
        code=Marketplace.US,
        country="United States",
        currency="USD",
        locale="en_US",
        domain="amazon.com",
        marketplace_id="ATVPDKIKX0DER",
        unit_system="imperial",
        language="en",
    ),
    Marketplace.CA: MarketplaceInfo(
        code=Marketplace.CA,
        country="Canada",
        currency="CAD",
        locale="en_CA",
        domain="amazon.ca",
        marketplace_id="A2EUQ1WTGCTBG2",
        unit_system="metric",
        language="en",
    ),
    Marketplace.UK: MarketplaceInfo(
        code=Marketplace.UK,
        country="United Kingdom",
        currency="GBP",
        locale="en_GB",
        domain="amazon.co.uk",
        marketplace_id="A1F83G8C2ARO7P",
        unit_system="metric",
        language="en",
    ),
    Marketplace.AU: MarketplaceInfo(
        code=Marketplace.AU,
        country="Australia",
        currency="AUD",
        locale="en_AU",
        domain="amazon.com.au",
        marketplace_id="A39IBJ37TRP1C6",
        unit_system="metric",
        language="en",
    ),
    Marketplace.IN: MarketplaceInfo(
        code=Marketplace.IN,
        country="India",
        currency="INR",
        locale="en_IN",
        domain="amazon.in",
        marketplace_id="A21TJRUUN4KGV",
        unit_system="metric",
        language="en",
    ),
}


def get_marketplace(code: Marketplace) -> MarketplaceInfo:
    """Return reference data for a marketplace, or raise if unregistered."""
    try:
        return MARKETPLACES[code]
    except KeyError as exc:  # pragma: no cover - guards a future enum/registry drift
        raise KeyError(f"marketplace {code!r} is not registered in MARKETPLACES") from exc


def unit_system_differs(a: Marketplace, b: Marketplace) -> bool:
    return get_marketplace(a).unit_system != get_marketplace(b).unit_system


def language_differs(a: Marketplace, b: Marketplace) -> bool:
    return get_marketplace(a).language != get_marketplace(b).language
