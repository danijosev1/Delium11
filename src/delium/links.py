"""Clickable product links (pure).

Builds the Amazon and Keepa URLs for a product from its ASIN + marketplace, so
every results view (UI, CLI, CSV) can link out consistently instead of showing a
bare ASIN. No I/O, no network — just string building.

The domain tables mirror the canonical ones (`analysis.marketplaces.MARKETPLACES`
for Amazon domains, `providers.keepa._KEEPA_DOMAINS` for Keepa ids); they are
duplicated here, small and reviewed-on-change, so this module stays dependency-free
and importable from any layer. Keepa has no Australia data in Delium, so AU gets
no Keepa link even though Keepa numbers the domain.
"""

from __future__ import annotations

from dataclasses import dataclass

# Marketplace code → Amazon storefront domain.
AMAZON_DOMAINS: dict[str, str] = {
    "US": "amazon.com",
    "UK": "amazon.co.uk",
    "CA": "amazon.ca",
    "AU": "amazon.com.au",
    "IN": "amazon.in",
}

# Marketplace code → Keepa domain id (Keepa's fixed numbering). AU omitted on
# purpose: Delium does not use Keepa Australia data, so we never link to it.
KEEPA_DOMAIN_IDS: dict[str, int] = {
    "US": 1,
    "UK": 2,
    "CA": 6,
    "IN": 10,
}


@dataclass(frozen=True)
class ProductLinks:
    """The outbound links for one product. `keepa_url` is None where Keepa has no
    data (e.g. AU) or the marketplace is unknown."""

    asin: str
    marketplace: str
    amazon_url: str | None
    keepa_url: str | None


def _norm(asin: str | None, marketplace: str | None) -> tuple[str, str] | None:
    if not asin or not marketplace:
        return None
    a = asin.strip().upper()
    mp = marketplace.strip().upper()
    if not a:
        return None
    return a, mp


def amazon_url(asin: str | None, marketplace: str | None) -> str | None:
    """`https://www.<domain>/dp/<ASIN>` for the marketplace, or None if the ASIN
    is missing or the marketplace is unknown."""
    norm = _norm(asin, marketplace)
    if norm is None:
        return None
    a, mp = norm
    domain = AMAZON_DOMAINS.get(mp)
    return f"https://www.{domain}/dp/{a}" if domain else None


def keepa_url(asin: str | None, marketplace: str | None) -> str | None:
    """`https://keepa.com/#!product/<domain_id>-<ASIN>`, or None if the ASIN is
    missing or the marketplace has no Keepa data (e.g. AU) / is unknown."""
    norm = _norm(asin, marketplace)
    if norm is None:
        return None
    a, mp = norm
    domain_id = KEEPA_DOMAIN_IDS.get(mp)
    return f"https://keepa.com/#!product/{domain_id}-{a}" if domain_id is not None else None


def product_links(asin: str | None, marketplace: str | None) -> ProductLinks:
    """Both links for a product in one call."""
    a = (asin or "").strip().upper()
    mp = (marketplace or "").strip().upper()
    return ProductLinks(
        asin=a,
        marketplace=mp,
        amazon_url=amazon_url(asin, marketplace),
        keepa_url=keepa_url(asin, marketplace),
    )
