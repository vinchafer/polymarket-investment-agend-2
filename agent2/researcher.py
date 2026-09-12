"""
researcher.py — Simple market research via DuckDuckGo.

Strategy: One search per market, return top 5 snippets as plain text.
Query: "[market question] result latest news"
Max 10 seconds per market. No API key required.
"""

import logging

logger = logging.getLogger(__name__)


class FastResearcher:
    """
    Minimal market researcher. One DuckDuckGo search per market.
    Returns top 5 snippets as plain text for agent decision.
    """

    def research_market(self, question: str) -> str:
        """
        Search DuckDuckGo for the market question.
        Returns top 5 snippets joined as plain text.
        Falls back gracefully on any error.
        """
        query = f"{question} result latest news"
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            snippets = [r.get("body", "").strip() for r in results if r.get("body")]
            if not snippets:
                return "No search results found."
            return "\n---\n".join(snippets[:5])
        except Exception as e:
            logger.warning(f"Research failed for '{question[:50]}': {e}")
            return "Research unavailable."
