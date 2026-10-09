"""Graph analysis template for the ``graph`` capability pack."""

from __future__ import annotations

import asyncio
import logging

import networkx as nx

logger = logging.getLogger(__name__)


class GraphAnalyzer:
    """Builds a graph from edges and answers path and centrality questions."""

    def __init__(self) -> None:
        """Start with an empty directed graph."""
        self.graph = nx.DiGraph()

    def add_edges(self, edges: list[tuple[str, str, float]]) -> None:
        """Add ``(source, target, weight)`` edges."""
        self.graph.add_weighted_edges_from(edges)

    async def shortest_path(self, source: str, target: str) -> list[str]:
        """Weighted shortest path, or an empty list when there is none."""
        try:
            return await asyncio.to_thread(nx.shortest_path, self.graph, source, target,
                                           weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            logger.info("no path %s -> %s: %s", source, target, exc)
            return []

    async def central_nodes(self, top: int = 5) -> list[tuple[str, float]]:
        """The ``top`` nodes by PageRank."""
        ranks = await asyncio.to_thread(nx.pagerank, self.graph)
        return sorted(ranks.items(), key=lambda item: item[1], reverse=True)[:top]
