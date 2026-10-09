"""Resilient async client for the Arxiv API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import arxiv

from .config import Settings

LOGGER = logging.getLogger(__name__)


class ArxivAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "type": "arxiv_api_error",
                "message": str(self),
            },
        }


@dataclass(slots=True)
class RequestMetadata:
    attempts: int = 0
    warnings: list[str] = field(default_factory=list)
    cached: bool = False


class ArxivClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = arxiv.Client(
            page_size=100,
            delay_seconds=settings.request_interval,
            num_retries=3,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        self._last_request_time = 0.0
        self.total_requests = 0
        self.cache_hits = 0
        self.last_request_at: str | None = None

        # Initialize cache directory
        self.cache_dir = settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def search_papers(
        self,
        query: str,
        max_results: int,
        sort_by: arxiv.SortCriterion,
        sort_order: arxiv.SortOrder,
    ) -> tuple[list[dict[str, Any]], RequestMetadata]:
        """Search papers with caching."""
        metadata = RequestMetadata()

        # Check cache
        cache_key = self._generate_cache_key(query, max_results, sort_by.value, sort_order.value)
        cached_result = self._get_from_cache(cache_key)
        if cached_result:
            metadata.cached = True
            self.cache_hits += 1
            LOGGER.info(f"Cache hit for query: {query[:50]}...")
            return cached_result, metadata

        # Rate limit
        await self._rate_limit()

        try:
            search = arxiv.Search(
                query=query,
                max_results=max_results,
                sort_by=sort_by,
                sort_order=sort_order,
            )

            # Run synchronous arxiv library in executor
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: list(self._client.results(search)),
            )

            self.total_requests += 1
            self.last_request_at = datetime.now(timezone.utc).isoformat()
            metadata.attempts = 1

            # Format results
            papers = [self._format_result(r) for r in results]

            # Cache results
            self._save_to_cache(cache_key, papers)

            if not papers:
                metadata.warnings.append("No papers matched the query.")

            LOGGER.info(f"Searched arxiv: query='{query[:50]}...', found={len(papers)} papers")
            return papers, metadata

        except Exception as exc:
            LOGGER.error(f"Arxiv search failed: {exc}")
            raise ArxivAPIError(f"Arxiv search failed: {str(exc)}")

    async def get_paper_by_id(self, arxiv_id: str) -> tuple[dict[str, Any], RequestMetadata]:
        """Get a single paper by ID."""
        metadata = RequestMetadata()
        await self._rate_limit()

        try:
            search = arxiv.Search(id_list=[arxiv_id])
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: list(self._client.results(search)),
            )

            if not results:
                raise ArxivAPIError(f"Paper {arxiv_id} not found")

            self.total_requests += 1
            self.last_request_at = datetime.now(timezone.utc).isoformat()
            metadata.attempts = 1

            LOGGER.info(f"Fetched paper: {arxiv_id}")
            return self._format_result(results[0], include_full_abstract=True), metadata

        except ArxivAPIError:
            raise
        except Exception as exc:
            LOGGER.error(f"Failed to fetch paper {arxiv_id}: {exc}")
            raise ArxivAPIError(f"Failed to fetch paper: {str(exc)}")

    async def batch_get_papers(
        self, arxiv_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], RequestMetadata]:
        """Batch get papers with concurrency control."""
        metadata = RequestMetadata()

        async def fetch_one(arxiv_id: str) -> dict[str, Any]:
            async with self._semaphore:
                try:
                    paper, _ = await self.get_paper_by_id(arxiv_id)
                    return {"arxiv_id": arxiv_id, "status": "success", **paper}
                except ArxivAPIError as exc:
                    warning = f"Failed to fetch {arxiv_id}: {str(exc)}"
                    metadata.warnings.append(warning)
                    LOGGER.warning(warning)
                    return {"arxiv_id": arxiv_id, "status": "failed", "error": str(exc)}

        results = await asyncio.gather(*[fetch_one(arxiv_id) for arxiv_id in arxiv_ids])

        # Separate successful and failed
        successful = [r for r in results if r.get("status") == "success"]
        failed = [{"arxiv_id": r["arxiv_id"], "error": r.get("error", "Unknown error")}
                  for r in results if r.get("status") == "failed"]

        LOGGER.info(f"Batch fetched {len(successful)}/{len(arxiv_ids)} papers successfully")
        return successful, failed, metadata

    async def _rate_limit(self) -> None:
        """Ensure request interval."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.settings.request_interval:
            wait_time = self.settings.request_interval - elapsed
            LOGGER.debug(f"Rate limiting: waiting {wait_time:.2f}s")
            await asyncio.sleep(wait_time)
        self._last_request_time = time.time()

    def _format_result(self, result: arxiv.Result, include_full_abstract: bool = False) -> dict[str, Any]:
        """Format arxiv.Result to dictionary."""
        # Extract arxiv ID from entry_id URL
        arxiv_id = result.entry_id.split("/abs/")[-1]

        # Format abstract
        abstract = result.summary.replace("\n", " ").strip()
        if not include_full_abstract and len(abstract) > 300:
            abstract = abstract[:297] + "..."

        return {
            "arxiv_id": arxiv_id,
            "title": result.title,
            "authors": [author.name for author in result.authors],
            "summary": abstract,
            "published": result.published.isoformat(),
            "updated": result.updated.isoformat(),
            "primary_category": result.primary_category,
            "categories": result.categories,
            "comment": result.comment,
            "journal_ref": result.journal_ref,
            "doi": result.doi,
            "pdf_url": result.pdf_url,
            "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
        }

    def _generate_cache_key(self, *args: Any) -> str:
        """Generate cache key from arguments."""
        key_string = "_".join(str(arg) for arg in args)
        return hashlib.md5(key_string.encode()).hexdigest()

    def _get_from_cache(self, cache_key: str) -> list[dict[str, Any]] | None:
        """Get from cache if not expired."""
        cache_file = self.cache_dir / f"{cache_key}.json"
        if not cache_file.exists():
            return None

        # Check if cache is expired
        mtime = cache_file.stat().st_mtime
        if time.time() - mtime > self.settings.cache_ttl:
            LOGGER.debug(f"Cache expired: {cache_key}")
            return None

        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            LOGGER.warning(f"Failed to read cache: {exc}")
            return None

    def _save_to_cache(self, cache_key: str, data: list[dict[str, Any]]) -> None:
        """Save to cache."""
        cache_file = self.cache_dir / f"{cache_key}.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            LOGGER.debug(f"Cached results: {cache_key}")
        except Exception as exc:
            LOGGER.warning(f"Failed to save cache: {exc}")

    def status(self) -> dict[str, Any]:
        """Return client status."""
        # Calculate cache size
        cache_size = sum(f.stat().st_size for f in self.cache_dir.glob("*.json") if f.is_file())

        return {
            "total_requests": self.total_requests,
            "cache_hits": self.cache_hits,
            "cache_size_kb": cache_size // 1024,
            "last_request_at": self.last_request_at,
        }
