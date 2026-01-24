"""Fuzzy tool discovery using rapidfuzz.

Lightweight alternative to sentence-transformers. For true semantic search,
use the Qdrant integration in context-mcp or a dedicated embedding service.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


@dataclass
class ToolMatch:
    """A tool match from search."""

    tool_id: str
    name: str
    description: str
    score: float
    estimated_tokens: int
    server: str
    matched_tags: list[str] = field(default_factory=list)


class DiscoveryService:
    """Fuzzy search over tool descriptions."""

    def __init__(self):
        """Initialize discovery service."""
        logger.info("Discovery service initialized (fuzzy search)")

    def _estimate_tokens(self, tool: dict) -> int:
        """Estimate token count for a tool definition."""
        name = tool.get("name", "")
        desc = tool.get("description", "") or ""
        schema = str(tool.get("inputSchema", {}))
        # Rough estimate: ~4 chars per token
        return (len(name) + len(desc) + len(schema)) // 4

    def search(
        self,
        query: str,
        tools: list[dict],
        tags: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[ToolMatch]:
        """Search tools by fuzzy matching.

        Args:
            query: Search query (keywords)
            tools: List of tool dicts with name, description, server, inputSchema
            tags: Optional tag filter (matches against server name)
            limit: Max results to return

        Returns:
            List of ToolMatch sorted by relevance score
        """
        if not tools:
            return []

        # Filter by tags if provided (match against server name)
        if tags:
            tools = [t for t in tools if t.get("server") in tags]

        if not tools:
            return []

        # Build search corpus: tool_id -> searchable text
        corpus = {}
        tool_lookup = {}
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "") or ""
            server = tool.get("server", "")
            tool_id = f"{server}__{name}"

            # Combine name and description for matching
            corpus[tool_id] = f"{name} {desc}".lower()
            tool_lookup[tool_id] = tool

        # Use rapidfuzz to find best matches
        query_lower = query.lower()

        # Get all scores using token_set_ratio for better multi-word matching
        results = process.extract(
            query_lower,
            corpus,
            scorer=fuzz.token_set_ratio,
            limit=limit,
        )

        matches = []
        for match_text, score, tool_id in results:
            tool = tool_lookup[tool_id]
            server = tool.get("server", "")

            # Normalize score to 0-1 range
            normalized_score = score / 100.0

            matched_tags = []
            if tags and server in tags:
                matched_tags.append(server)

            matches.append(
                ToolMatch(
                    tool_id=tool_id,
                    name=tool_id,
                    description=tool.get("description", "") or "",
                    score=normalized_score,
                    estimated_tokens=self._estimate_tokens(tool),
                    server=server,
                    matched_tags=matched_tags,
                )
            )

        return matches

    def clear_cache(self) -> None:
        """No-op for compatibility."""
        pass
