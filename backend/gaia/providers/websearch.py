"""Web search tools used by open-ended chat and the diagnosis agent.

Results carry provenance (title, URL, domain) and a coarse source-quality tier
so that institutional agricultural sources are preferred and labelled.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

_INSTITUTIONAL_SUFFIXES = (
    ".edu", ".gov", ".gov.in", ".nic.in", ".ac.in", ".res.in", ".ac.uk", ".gov.uk", ".edu.au",
    ".gov.au", ".int",
)
_INSTITUTIONAL_DOMAINS = (
    "fao.org", "icar.org.in", "cabi.org", "plantwise.org", "apsnet.org", "cimmyt.org", "irri.org",
    "icrisat.org", "worldveg.org", "extension.org", "ipmcenters.org", "rhs.org.uk", "vikaspedia.in",
    "cgiar.org", "eppo.int",
)
# Forums, social media and video sites: never used to back advice when anything better exists.
_USER_GENERATED_DOMAINS = (
    "reddit.com", "quora.com", "pinterest.com", "facebook.com", "instagram.com", "tiktok.com",
    "youtube.com", "twitter.com", "x.com", "medium.com",
)
QUALITY_RANK = {"institutional": 0, "general": 1, "user_generated": 2}


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    query: str = ""

    @property
    def domain(self) -> str:
        host = urlparse(self.url).hostname or ""
        return host.removeprefix("www.")

    @property
    def quality(self) -> str:
        d = self.domain
        if d.endswith(_INSTITUTIONAL_SUFFIXES) or any(d == x or d.endswith("." + x) for x in _INSTITUTIONAL_DOMAINS):
            return "institutional"
        if any(d == x or d.endswith("." + x) for x in _USER_GENERATED_DOMAINS):
            return "user_generated"
        return "general"

    def to_dict(self) -> dict:
        return {**asdict(self), "domain": self.domain, "quality": self.quality}


class WebSearch(ABC):
    name = "base"
    available = True

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[WebResult]: ...

    def search_many(self, queries: list[str], max_results: int) -> list[WebResult]:
        """Run queries in parallel, de-duplicate by URL, rank institutional sources first, and drop
        user-generated content (forums, social media) whenever other sources are available."""
        queries = [q.strip() for q in queries if q and q.strip()]
        if not queries or not self.available:
            return []
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            batches = list(pool.map(lambda q: self._safe_search(q, max_results), queries))
        seen: set[str] = set()
        merged: list[WebResult] = []
        # Interleave so every query contributes its best results first.
        for rank in range(max(map(len, batches), default=0)):
            for batch in batches:
                if rank < len(batch):
                    r = batch[rank]
                    key = r.url.split("#")[0].rstrip("/")
                    if key not in seen:
                        seen.add(key)
                        merged.append(r)
        merged.sort(key=lambda r: QUALITY_RANK[r.quality])
        vetted = [r for r in merged if r.quality != "user_generated"]
        return vetted or merged

    def _safe_search(self, query: str, max_results: int) -> list[WebResult]:
        try:
            return self.search(query, max_results)
        except Exception as exc:  # network errors / rate limits must not break the pipeline
            log.warning("%s search failed for %r: %s", self.name, query, exc)
            return []


class TavilySearch(WebSearch):
    name = "tavily"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._http = httpx.Client(timeout=20)

    def search(self, query: str, max_results: int) -> list[WebResult]:
        r = self._http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"api_key": self._api_key, "query": query, "max_results": max_results, "search_depth": "basic"},
        )
        r.raise_for_status()
        return [
            WebResult(title=x.get("title") or x["url"], url=x["url"], snippet=(x.get("content") or "")[:900], query=query)
            for x in r.json().get("results", [])
            if x.get("url")
        ]


class DuckDuckGoSearch(WebSearch):
    """Keyless search via the ``ddgs`` metasearch package."""

    name = "duckduckgo"

    def search(self, query: str, max_results: int) -> list[WebResult]:
        from ddgs import DDGS

        rows = DDGS().text(query, max_results=max_results)
        return [
            WebResult(title=x.get("title") or x["href"], url=x["href"], snippet=(x.get("body") or "")[:900], query=query)
            for x in rows or []
            if x.get("href")
        ]


class NullSearch(WebSearch):
    name = "none"
    available = False

    def search(self, query: str, max_results: int) -> list[WebResult]:
        return []


def build_search(settings: Settings) -> WebSearch:
    provider = settings.search_provider
    if provider == "auto":
        provider = "tavily" if settings.tavily_api_key else "duckduckgo"
    if provider == "tavily" and settings.tavily_api_key:
        return TavilySearch(settings.tavily_api_key)
    if provider == "duckduckgo":
        try:
            import ddgs  # noqa: F401
        except ImportError:
            log.warning("ddgs is not installed; web search disabled")
            return NullSearch()
        return DuckDuckGoSearch()
    return NullSearch()
