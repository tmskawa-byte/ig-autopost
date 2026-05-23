"""
Tavily Search API wrapper.

Docs: https://docs.tavily.com/api-reference/endpoint/search

Auth via TAVILY_API_KEY env var. We rely on the news topic so we can pass
`days` and `include_domains` for Japan-media-only retrieval.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger(__name__)


class TavilyError(RuntimeError):
    pass


class TavilyClient:
    BASE_URL = "https://api.tavily.com/search"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY")
        if not self.api_key:
            raise TavilyError("TAVILY_API_KEY is not set")

    def search(
        self,
        query: str,
        topic: str = "news",
        max_results: int = 5,
        days: int = 365,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        search_depth: str = "advanced",
        timeout: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Returns a list of result dicts:
            { title, url, content, score, published_date? }
        Empty list if nothing matched.
        """
        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "topic": topic,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        # `days` is only meaningful when topic == "news"
        if topic == "news":
            payload["days"] = days
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        try:
            resp = requests.post(self.BASE_URL, json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise TavilyError(f"Network error: {e}") from e

        if resp.status_code >= 400:
            raise TavilyError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise TavilyError(f"Invalid JSON: {e}") from e

        results = data.get("results", []) or []
        LOG.info("Tavily: %d results for query=%r", len(results), query)
        return results
