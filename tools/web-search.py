"""
tools/web-search.py
─────────────────────────────────────────────────────────────────────────────
OpenWebUI Python Tool — Web Search (SearXNG)

HOW TO INSTALL:
  1. Open OpenWebUI: http://localhost:3000
  2. Go to Admin Panel → Tools → + New Tool
  3. Copy-paste this entire file into the code editor
  4. Click Save
  5. In any chat, enable the 🔧 wrench icon to activate tools

WHAT IT DOES:
  Lets the model search the live web itself through the bundled private
  SearXNG metasearch engine (JSON API). Returns the top results as
  title / URL / snippet so the model can ground its answer and cite
  sources inline as [Title](URL).

  Unlike the globe-icon Web Search toggle (which always retrieves pages
  before the model answers), this tool is model-driven: the model decides
  when a query needs live data and calls the tool only then. Pair it with
  the system prompt in config/prompts/web-search-assistant.md to make the
  model search proactively and cite consistently.
─────────────────────────────────────────────────────────────────────────────

title: Web Search
description: Search the live web through the bundled private SearXNG instance
version: 1.0.0
author: ai-service
"""
import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        """
        Configuration for this tool.
        Edit these in the tool's Settings panel in OpenWebUI.
        """
        searxng_url:    str = Field(default="http://searxng:8080",
                                    description="SearXNG server URL (internal Docker address)")
        max_results:    int = Field(default=5,
                                    description="Maximum search results to return (1-10)")
        snippet_length: int = Field(default=300,
                                    description="Max characters of page snippet per result")
        language:       str = Field(default="en",
                                    description="Search language code (e.g. en, zh-TW, de) or 'all'")
        safesearch:     int = Field(default=1,
                                    description="SafeSearch level: 0 off, 1 moderate, 2 strict")

    def __init__(self):
        self.valves = self.Valves()

    def web_search(self, query: str) -> str:
        """
        Search the web for current, factual information via the private
        SearXNG engine. Use this for time-sensitive questions, news,
        product reviews, weather, prices, or technical documentation
        updates — anything beyond your training data. Keep the query
        short and keyword-focused: no punctuation, no filler words.
        Do not repeat an identical search in the same conversation.
        Cite retrieved facts inline as [Title](URL).
        """
        try:
            resp = requests.get(
                f"{self.valves.searxng_url}/search",
                params={
                    "q": query,
                    "format": "json",
                    "language": self.valves.language,
                    "safesearch": self.valves.safesearch,
                },
                timeout=(5, 20),
            )
            resp.raise_for_status()
            data = resp.json()

            answers = data.get("answers", [])
            results = data.get("results", [])
            limit   = max(1, min(self.valves.max_results, 10))
            results = results[:limit]

            if not answers and not results:
                return (
                    f"SearXNG returned no results for: '{query}'\n"
                    "Tell the user no live results were found, then answer "
                    "from offline knowledge while noting the lack of live data."
                )

            output = []
            for a in answers:
                text = a.get("answer", a) if isinstance(a, dict) else a
                output.append(f"Direct answer: {text}")

            for i, r in enumerate(results, 1):
                title   = r.get("title", "untitled")
                url     = r.get("url", "")
                snippet = (r.get("content") or "")[: self.valves.snippet_length]
                output.append(f"[{i}] {title}\n{url}\n{snippet}")

            output.append(
                "Base the answer on these results and cite each fact "
                "inline as [Title](URL)."
            )
            return "\n\n---\n\n".join(output)

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return (
                "Cannot reach SearXNG — live web search is unavailable.\n"
                "Tell the user this limitation, answer from offline knowledge, "
                "and note the answer is not based on live data.\n"
                "To fix: check the container with: docker compose ps searxng"
            )
        except Exception as e:
            return f"Search error: {type(e).__name__}: {e}"
