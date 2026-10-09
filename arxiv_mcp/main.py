"""Arxiv MCP server with STDIO transport for VLN research."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import arxiv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .arxiv_client import ArxivAPIError, ArxivClient, RequestMetadata
from .config import Settings


class RecentLogHandler(logging.Handler):
    """Keep a small, sanitized diagnostics buffer for the logs resource."""

    def __init__(self, capacity: int = 100) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        lowered = message.lower()
        if "api_key" in lowered or "token" in lowered:
            message = "[credential-bearing log entry redacted]"
        self.records.append(
            {
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:1000],
            }
        )


RECENT_LOGS = RecentLogHandler()


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)
    if not any(getattr(handler, "_arxiv_stderr", False) for handler in root.handlers):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler._arxiv_stderr = True  # type: ignore[attr-defined]
        stderr_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(stderr_handler)
    if RECENT_LOGS not in root.handlers:
        root.addHandler(RECENT_LOGS)


def _metadata(meta: RequestMetadata) -> dict[str, Any]:
    return {
        "attempts": meta.attempts,
        "warnings": meta.warnings,
        "cached": meta.cached,
    }


def create_server(settings: Settings) -> tuple[FastMCP, ArxivClient]:
    arxiv_client = ArxivClient(settings)

    mcp = FastMCP(
        "Arxiv VLN Research",
        instructions=(
            "Search and analyze Visual Language Navigation (VLN) research papers from Arxiv. "
            "Retrieve paper metadata, abstracts, and generate structured reading summaries. "
            "Use batch_summarize_papers to efficiently process multiple papers at once."
        ),
    )

    @mcp.tool()
    async def search_vln_papers(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="Search query for Arxiv, e.g. 'visual language navigation' or 'VLN embodied AI'",
            ),
        ] = "visual language navigation",
        max_results: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum number of papers to return"),
        ] = 20,
        sort_by: Literal["relevance", "lastUpdatedDate", "submittedDate"] = "submittedDate",
        sort_order: Literal["ascending", "descending"] = "descending",
    ) -> dict[str, Any]:
        """Search Arxiv for Visual Language Navigation papers."""
        try:
            # Map string to arxiv enum
            sort_criterion_map = {
                "relevance": arxiv.SortCriterion.Relevance,
                "lastUpdatedDate": arxiv.SortCriterion.LastUpdatedDate,
                "submittedDate": arxiv.SortCriterion.SubmittedDate,
            }
            sort_order_map = {
                "ascending": arxiv.SortOrder.Ascending,
                "descending": arxiv.SortOrder.Descending,
            }

            papers, meta = await arxiv_client.search_papers(
                query,
                max_results,
                sort_criterion_map[sort_by],
                sort_order_map[sort_order],
            )

            return {
                "ok": True,
                "query": query,
                "total_results": len(papers),
                "papers": papers,
                "meta": _metadata(meta),
            }
        except ArxivAPIError as exc:
            return exc.as_result()

    @mcp.tool()
    async def get_paper_details(
        arxiv_id: Annotated[
            str,
            Field(
                pattern=r"^\d{4}\.\d{4,5}(v\d+)?$",
                description="Arxiv paper ID, e.g. '2301.12345' or '2301.12345v2'",
            ),
        ],
    ) -> dict[str, Any]:
        """Get detailed metadata and full abstract for a single Arxiv paper."""
        try:
            paper, meta = await arxiv_client.get_paper_by_id(arxiv_id)
            return {
                "ok": True,
                "paper": paper,
                "meta": _metadata(meta),
            }
        except ArxivAPIError as exc:
            return exc.as_result()

    @mcp.tool()
    async def batch_summarize_papers(
        arxiv_ids: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=50,
                description="List of Arxiv paper IDs to summarize",
            ),
        ],
        include_categories: bool = True,
    ) -> dict[str, Any]:
        """Generate a structured summary report for multiple papers. Ideal for creating reading lists."""
        try:
            successful, failed, meta = await arxiv_client.batch_get_papers(arxiv_ids)

            # Group by year
            grouped_by_year: dict[str, int] = {}
            for paper in successful:
                if "published" in paper:
                    year = paper["published"][:4]
                    grouped_by_year[year] = grouped_by_year.get(year, 0) + 1

            # Format summaries
            summaries = []
            for paper in successful:
                summary_entry = {
                    "arxiv_id": paper["arxiv_id"],
                    "title": paper["title"],
                    "authors": paper["authors"],
                    "published_date": paper["published"][:10] if "published" in paper else None,
                    "summary_excerpt": paper["summary"][:200] + "..." if len(paper["summary"]) > 200 else paper["summary"],
                }
                if include_categories:
                    summary_entry["categories"] = paper.get("categories", [])
                    summary_entry["primary_category"] = paper.get("primary_category")
                summaries.append(summary_entry)

            return {
                "ok": True,
                "summary_report": {
                    "total_papers": len(arxiv_ids),
                    "successful": len(successful),
                    "failed": len(failed),
                    "papers": summaries,
                    "failed_papers": failed,
                    "grouped_by_year": dict(sorted(grouped_by_year.items(), reverse=True)),
                },
                "meta": _metadata(meta),
            }
        except ArxivAPIError as exc:
            return exc.as_result()

    @mcp.tool()
    async def search_papers_by_author(
        author_name: Annotated[
            str,
            Field(
                min_length=2,
                max_length=100,
                description="Author name, e.g. 'Peter Anderson'",
            ),
        ],
        max_results: Annotated[
            int,
            Field(ge=1, le=50, description="Maximum number of papers to return"),
        ] = 10,
    ) -> dict[str, Any]:
        """Search papers by author name in AI/CV categories."""
        try:
            # Construct query for author in AI/CV categories
            query = f'au:"{author_name}" AND (cat:cs.AI OR cat:cs.CV OR cat:cs.RO OR cat:cs.LG)'

            papers, meta = await arxiv_client.search_papers(
                query,
                max_results,
                arxiv.SortCriterion.SubmittedDate,
                arxiv.SortOrder.Descending,
            )

            return {
                "ok": True,
                "author": author_name,
                "query": query,
                "total_results": len(papers),
                "papers": papers,
                "meta": _metadata(meta),
            }
        except ArxivAPIError as exc:
            return exc.as_result()

    @mcp.resource("arxiv://search/latest")
    def arxiv_search_latest() -> str:
        """Most recent search results cached in memory."""
        # Try to get the most recent cache file
        cache_files = sorted(
            arxiv_client.cache_dir.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not cache_files:
            return json.dumps(
                {"error": "No cached search results available"},
                ensure_ascii=False,
                indent=2,
            )

        try:
            with open(cache_files[0], "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            return json.dumps(
                {
                    "cached_at": datetime.fromtimestamp(
                        cache_files[0].stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "paper_count": len(cached_data),
                    "papers": cached_data,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            return json.dumps(
                {"error": f"Failed to read cache: {str(exc)}"},
                ensure_ascii=False,
                indent=2,
            )

    @mcp.resource("arxiv://service/status")
    def arxiv_service_status() -> str:
        """Current service status and statistics."""
        return json.dumps(
            {
                "service": "Arxiv VLN Research MCP",
                "version": "1.0.0",
                "transport": "stdio",
                "status": arxiv_client.status(),
                "config": {
                    "request_interval_seconds": settings.request_interval,
                    "max_results_limit": settings.max_results,
                    "cache_ttl_seconds": settings.cache_ttl,
                    "max_concurrent": settings.max_concurrent,
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    @mcp.resource("arxiv://service/logs")
    def arxiv_service_logs() -> str:
        """Recent in-memory diagnostics logs."""
        return json.dumps(
            {"entries": list(RECENT_LOGS.records)},
            ensure_ascii=False,
            indent=2,
        )

    @mcp.prompt()
    def analyze_vln_research_trends(
        time_range: Annotated[
            str,
            Field(max_length=50, description="Time range to analyze, e.g. '2024' or '2023-2024'"),
        ] = "2024",
        focus_area: Annotated[
            str,
            Field(max_length=200, description="Specific sub-area to focus on"),
        ] = "general trends",
    ) -> str:
        """Analyze research trends in Visual Language Navigation field."""
        return f"""Analyze research trends in Visual Language Navigation for {time_range}, focusing on {focus_area}.

Steps:
1. Call search_vln_papers with query "visual language navigation" sorted by submittedDate descending
2. Filter papers by publication year matching {time_range}
3. Call batch_summarize_papers to get detailed information
4. Analyze the following dimensions:
   - Time distribution and publication velocity
   - Primary research directions and themes
   - Conference/journal distribution (from journal_ref and comments)
   - Key research groups and author collaboration patterns
   - Methodological evolution (e.g., learning paradigms, architectures)

Provide a structured report with:
- Overview statistics (total papers, category distribution, year-over-year trends)
- Top 3-5 research themes with representative paper examples
- Notable authors and their contributions
- Methodological trends and technical innovations
- Research gaps and future opportunities

Important: Base your analysis only on the actual papers returned by the tools. Do not fabricate papers or citations."""

    @mcp.prompt()
    def compare_vln_papers(
        arxiv_ids: Annotated[
            list[str],
            Field(
                min_length=2,
                max_length=10,
                description="List of Arxiv paper IDs to compare",
            ),
        ],
        comparison_aspects: Annotated[
            str,
            Field(max_length=300),
        ] = "methods, datasets, performance metrics",
    ) -> str:
        """Compare multiple VLN papers across specified aspects."""
        ids_str = ", ".join(arxiv_ids)
        return f"""Compare the following Visual Language Navigation papers: {ids_str}

Focus your comparison on: {comparison_aspects}

Steps:
1. Call batch_summarize_papers with arxiv_ids: {arxiv_ids}
2. For each paper, extract from the abstract:
   - Core methodology and approach
   - Datasets or environments used
   - Key contributions and novelty
   - Performance claims (if mentioned)
3. Create a structured comparison highlighting:
   - Similarities in approach or methodology
   - Key differences and unique contributions
   - Complementary aspects (papers that could be combined)
   - Chronological evolution (if applicable)

Provide:
- Summary table (Paper | Method | Dataset/Environment | Key Contribution)
- Detailed analysis of differences across {comparison_aspects}
- Recommendations: which paper is most relevant for different research scenarios
  (e.g., baseline implementation, latest SOTA, novel architecture, specific task)

Important: Only use information from the abstracts returned by the tool. Clearly state when information is not available in the abstract."""

    return mcp, arxiv_client


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Arxiv VLN Research MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio",),
        default="stdio",
        help="Transport mode (only stdio supported)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    mcp, _ = create_server(settings)

    # Only STDIO mode is supported
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
