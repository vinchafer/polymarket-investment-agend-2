"""
researcher_v2.py — Multi-Source Internet Research Engine (Tavily-Free)
Replaces Tavily with 6 free/low-cost research sources.

Sources (priority order):
1. ESPN API       — free, no key, authoritative sports scores
2. DuckDuckGo     — free, no key, general web search (20 req/hr)
3. Google News RSS — free, no key, recent news (unlimited)
4. Serper API     — 2500 free/month, Google results
5. NewsAPI        — 100 req/day free, news articles
6. Bing           — free scraping fallback

Design:
- Graceful degradation: continues if any source fails
- Rate limit tracking in-memory (per hour / per day)
- Sport detection: ESPN used first for NBA/NFL/NHL/MLB/Soccer markets
- Identical ResearchResult format as researcher.py (backward compatible)
- Cache via TradeLogger (same TTL logic)
"""

import dataclasses
import hashlib
import json
import logging
import re
import time
from datetime import datetime, date
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, quote_plus

import requests

import config

logger = logging.getLogger(__name__)

# Suppress noisy debug output from search/HTTP libraries
for _lib in ["urllib3", "h2", "rustls", "hyper_util", "primp", "cookie_store", "duckduckgo_search", "ddgs"]:
    logging.getLogger(_lib).setLevel(logging.WARNING)


# =============================================================================
# Source Tier Definitions (same as researcher.py)
# =============================================================================

TIER1_DOMAINS = {
    "espn.com", "bbc.co.uk", "bbc.com",
    "uefa.com", "fifa.com", "nba.com", "nhl.com", "mlb.com", "nfl.com",
    "olympics.com", "atptour.com", "wtatennis.com", "formula1.com",
    "premierleague.com", "bundesliga.com", "laliga.com", "ligue1.com",
    "seriea.it", "transfermarkt.com", "flashscore.com", "sofascore.com",
    "livescore.com", "whoscored.com",
    "whitehouse.gov", "congress.gov", "senate.gov",
}

TIER2_DOMAINS = {
    "reuters.com", "apnews.com", "nytimes.com", "theguardian.com", "washingtonpost.com",
    "ft.com", "bloomberg.com", "telegraph.co.uk", "independent.co.uk",
    "spiegel.de", "faz.net", "sueddeutsche.de",
    "sky.com", "skysports.com", "goal.com", "cbssports.com",
    "nbcsports.com", "foxsports.com", "sportingnews.com",
    "theathletic.com", "90min.com", "marca.com", "as.com",
    "corrieredellosport.it", "kicker.de", "sport1.de",
    "cnbc.com", "wsj.com", "economist.com",
}

TIER4_DOMAINS = {
    "reddit.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "tiktok.com", "youtube.com",
    "quora.com", "stackexchange.com",
}

WIN_KEYWORDS = {
    "won", "wins", "winner", "victory", "beat", "defeated", "champion",
    "triumphant", "crowned", "first place", "gold medal", "confirmed",
    "approved", "passed", "elected", "signed", "launched",
}
LOSS_KEYWORDS = {
    "lost", "loses", "loser", "defeat", "beaten", "eliminated",
    "knocked out", "failed", "rejected", "denied", "cancelled",
    "blocked", "withdrew", "conceded",
}


# =============================================================================
# Rate Limits per Source
# =============================================================================

RATE_LIMITS = {
    "duckduckgo":  {"hourly": 20,   "daily": 9999},
    "google_news": {"hourly": 9999, "daily": 9999},
    "serper":      {"hourly": 9999, "daily": 80},
    "newsapi":     {"hourly": 3,    "daily": 100},
    "espn":        {"hourly": 9999, "daily": 9999},
    "bing":        {"hourly": 10,   "daily": 9999},
}


# =============================================================================
# ESPN Sport Detection Patterns
# =============================================================================

# Order matters: first match wins. Competition-specific terms before team names.
ESPN_SPORT_PATTERNS = [
    ("basketball/nba", re.compile(
        r"\b(NBA|basketball\s+game|Lakers|Celtics|Warriors|Bulls|Heat|Nets|Knicks|"
        r"Bucks|Suns|Nuggets|76ers|Clippers|Mavericks|Rockets|Spurs|Grizzlies|"
        r"Pelicans|Thunder|Jazz|Trail Blazers|Kings|Timberwolves|Pacers|Raptors|"
        r"Hawks|Hornets|Wizards|Pistons|Cavaliers|Magic)\b",
        re.I,
    )),
    ("football/nfl", re.compile(
        r"\b(NFL|Super Bowl|Chiefs|Patriots|Cowboys|Packers|Steelers|Ravens|"
        r"Seahawks|49ers|Saints|Falcons|Panthers|Buccaneers|Bears|Lions|Vikings|"
        r"Rams|Chargers|Raiders|Broncos|Dolphins|Bills|Jets|Giants|Eagles|"
        r"Commanders|Browns|Bengals|Titans|Colts|Jaguars|Texans|Cardinals)\b",
        re.I,
    )),
    ("hockey/nhl", re.compile(
        r"\b(NHL|hockey|Bruins|Maple Leafs|Rangers|Penguins|Blackhawks|Red Wings|"
        r"Canadiens|Flyers|Blues|Avalanche|Lightning|Panthers|Capitals|Hurricanes|"
        r"Blue Jackets|Wild|Jets|Ducks|Kings|Sharks|Senators|Flames|Oilers|"
        r"Canucks|Golden Knights|Kraken)\b",
        re.I,
    )),
    ("baseball/mlb", re.compile(
        r"\b(MLB|baseball|Yankees|Red Sox|Dodgers|Cubs|Cardinals|Giants|Mets|"
        r"Phillies|Braves|Nationals|Marlins|Rays|Blue Jays|Orioles|Astros|"
        r"Rangers|Angels|Athletics|Mariners|Rockies|Padres|Diamondbacks|Reds|"
        r"Pirates|Brewers|Twins|White Sox|Tigers|Royals)\b",
        re.I,
    )),
    ("soccer/uefa.champions_league", re.compile(
        r"\b(Champions League|UCL|Europa League|Conference League)\b",
        re.I,
    )),
    ("soccer/eng.1", re.compile(
        r"\b(Premier League|EPL|Arsenal|Chelsea|Liverpool|Manchester City|"
        r"Manchester United|Tottenham|Newcastle|Aston Villa|West Ham|Brighton)\b",
        re.I,
    )),
    ("soccer/esp.1", re.compile(
        r"\b(La Liga|LaLiga|Real Madrid|Atletico Madrid|Sevilla|Valencia|"
        r"Real Sociedad|Villarreal)\b",
        re.I,
    )),
    ("soccer/ger.1", re.compile(
        r"\b(Bundesliga|Bayern Munich|Borussia Dortmund|Leverkusen|RB Leipzig)\b",
        re.I,
    )),
    ("soccer/ita.1", re.compile(
        r"\b(Serie A|Juventus|AC Milan|Inter Milan|Napoli|Roma|Lazio)\b",
        re.I,
    )),
    ("soccer/fra.1", re.compile(
        r"\b(Ligue 1|PSG|Paris Saint-Germain|Marseille|Lyon|Monaco)\b",
        re.I,
    )),
]

NON_SPORT_CATEGORIES = {
    "politics", "political", "crypto", "cryptocurrency",
    "economics", "economic", "finance", "entertainment", "science", "technology",
}


# =============================================================================
# Data Classes (identical fields to researcher.py for backward compatibility)
# =============================================================================

@dataclass
class ResearchResult:
    """Collected research results for a market. Identical interface to researcher.py."""
    market_question: str
    search_queries: list
    articles: list
    summary: str
    key_facts: list
    sentiment: str
    confidence_in_research: float
    research_timestamp: str
    sources: list

    # Source quality
    source_tiers: list
    tier1_2_source_count: int
    weighted_confidence: float

    # Contradiction detection
    has_contradiction: bool
    contradiction_details: str

    # Verification
    verified_result: Optional[str]
    is_past_event: bool


# =============================================================================
# Multi-Source Researcher
# =============================================================================

class MultiSourceResearcher:
    """
    Drop-in replacement for MarketResearcher (researcher.py).
    Uses 6 free sources instead of paid Tavily API.
    Tries sources in order; gracefully skips any that fail.
    """

    def __init__(self, db=None):
        self._db = db
        self._cache_hits = 0
        self._cache_misses = 0
        # In-memory rate limit state: {source: {hour_key, hourly_count, day_key, daily_count}}
        self._rl: dict = {}

    # -------------------------------------------------------------------------
    # Public Interface (same as MarketResearcher)
    # -------------------------------------------------------------------------

    def research_market(self, question: str, description: str = "",
                        category: str = "", days_to_resolution: int = 7,
                        market_id: str = "") -> ResearchResult:
        """Main method: researches a Polymarket market using multiple free sources."""
        logger.info(f"  Recherchiere: '{question[:60]}...'")

        is_past_event = days_to_resolution <= 0
        is_today = (days_to_resolution == 0)

        # === CACHE CHECK ===
        if self._db and not is_today:
            cache_key = self._make_cache_key(market_id or question, days_to_resolution)
            cached = self._db.get_cached_research(cache_key)
            if cached:
                try:
                    result = self._deserialize_result(cached)
                    self._cache_hits += 1
                    logger.info(f"    Cache-Hit (Hits: {self._cache_hits}, Misses: {self._cache_misses})")
                    return result
                except Exception as e:
                    logger.warning(f"    Cache-Deserialize-Fehler: {e}")

        self._cache_misses += 1

        # Generate search queries
        queries = self._generate_search_queries(question, description, category, days_to_resolution)
        logger.debug(f"    Suchanfragen: {queries}")

        # Detect sport for ESPN optimization
        sport = self._detect_sport(question + " " + description, category)
        if sport:
            logger.debug(f"    Sport erkannt: {sport}")

        # Fetch from all sources
        all_articles, direct_answers = self._fetch_all_sources(queries, sport, question)

        # Deduplicate by URL
        seen_urls: set = set()
        unique_articles = []
        for a in all_articles:
            url = a.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_articles.append(a)
            elif not url:
                unique_articles.append(a)

        source_summary = self._get_source_summary(all_articles)
        logger.info(f"    {len(unique_articles)} Artikel ({source_summary})")

        # Source tier classification
        source_tiers = [self._classify_source_tier(a) for a in unique_articles]
        tier1_2_count = sum(1 for t in source_tiers if t["tier"] <= 2)
        weighted_conf = self._compute_weighted_confidence(unique_articles, source_tiers)

        # Contradiction detection
        has_contradiction, contradiction_details = self._detect_contradictions(
            unique_articles, source_tiers, question, direct_answers
        )
        if has_contradiction:
            logger.warning(f"    WIDERSPRUCH: {contradiction_details}")

        # Verified result (past events only, with enough high-quality sources)
        verified_result = None
        if is_past_event and tier1_2_count >= config.MIN_TIER12_SOURCES and not has_contradiction:
            verified_result = self._extract_verified_result(
                unique_articles, source_tiers, question, direct_answers
            )
            if verified_result:
                logger.info(f"    Verifiziert: {verified_result}")

        key_facts = self._extract_key_facts(unique_articles, question)
        sentiment, legacy_confidence = self._assess_sentiment(unique_articles, question, key_facts)
        summary = self._create_summary(
            unique_articles, question, key_facts, days_to_resolution,
            direct_answers, source_tiers, has_contradiction, verified_result,
        )

        result = ResearchResult(
            market_question=question,
            search_queries=queries,
            articles=unique_articles[:10],
            summary=summary,
            key_facts=key_facts,
            sentiment=sentiment,
            confidence_in_research=legacy_confidence,
            research_timestamp=datetime.now().isoformat(),
            sources=list({a.get("url", "") for a in unique_articles if a.get("url")})[:10],
            source_tiers=source_tiers[:10],
            tier1_2_source_count=tier1_2_count,
            weighted_confidence=weighted_conf,
            has_contradiction=has_contradiction,
            contradiction_details=contradiction_details,
            verified_result=verified_result,
            is_past_event=is_past_event,
        )

        # === CACHE SAVE ===
        if self._db and not is_today:
            try:
                ttl = 60 * 24 * 7 if is_past_event else 20  # 7 days past / 20 min future
                self._db.save_cached_research(
                    cache_key=cache_key,
                    market_id=market_id or question[:100],
                    result_json=self._serialize_result(result),
                    ttl_minutes=ttl,
                    is_past_event=is_past_event,
                )
            except Exception as e:
                logger.warning(f"    Cache speichern fehlgeschlagen: {e}")

        return result

    def quick_search(self, query: str, max_results: int = 3) -> str:
        """Fast search for simple fact checks. Compatible with old quick_search interface."""
        # Try DuckDuckGo first (no key needed, fast)
        if not self._is_rate_limited("duckduckgo"):
            articles = self._search_duckduckgo(query, max_results=max_results)
            texts = [a.get("content", "")[:200] for a in articles if a.get("content")]
            if texts:
                return " | ".join(texts[:3])

        # Fallback: Google News RSS
        articles = self._search_google_news_rss(query, max_results=max_results)
        texts = [a.get("content", "")[:200] for a in articles if a.get("content")]
        return " | ".join(texts[:3]) if texts else ""

    @property
    def cache_hit_rate(self) -> float:
        total = self._cache_hits + self._cache_misses
        return self._cache_hits / total if total > 0 else 0.0

    # -------------------------------------------------------------------------
    # Source Orchestration
    # -------------------------------------------------------------------------

    def _fetch_all_sources(self, queries: list, sport: Optional[str],
                           question: str) -> tuple:
        """
        Fetch articles from all available sources.
        Returns (articles_list, direct_answers_list).
        direct_answers is a list of short authoritative strings (e.g. ESPN scores).
        """
        all_articles = []
        direct_answers = []

        primary_query = queries[0] if queries else ""

        # 1. ESPN — fastest and most authoritative for sports scores
        if sport:
            espn_articles, espn_answers = self._search_espn(sport, question)
            all_articles.extend(espn_articles)
            direct_answers.extend(espn_answers)
            if espn_articles:
                logger.debug(f"    ESPN ({sport}): {len(espn_articles)} Ergebnisse")

        # 2. DuckDuckGo — primary query only (preserve 20/hr rate limit for 15 markets)
        if primary_query and not self._is_rate_limited("duckduckgo"):
            articles = self._search_duckduckgo(primary_query)
            all_articles.extend(articles)
            if articles:
                logger.debug(f"    DDG: {len(articles)}")

        # 3. Google News RSS — all queries (unlimited, great for recent events)
        for query in queries:
            articles = self._search_google_news_rss(query)
            all_articles.extend(articles)

        # 4. Serper — primary query if configured and not rate-limited
        serper_key = getattr(config, "SERPER_API_KEY", "") or ""
        if serper_key and primary_query and not self._is_rate_limited("serper"):
            articles = self._search_serper(primary_query)
            all_articles.extend(articles)
            if articles:
                logger.debug(f"    Serper: {len(articles)}")

        # 5. NewsAPI — primary query if configured and not rate-limited
        newsapi_key = getattr(config, "NEWSAPI_KEY", "") or ""
        if newsapi_key and primary_query and not self._is_rate_limited("newsapi"):
            articles = self._search_newsapi(primary_query)
            all_articles.extend(articles)
            if articles:
                logger.debug(f"    NewsAPI: {len(articles)}")

        # 6. Bing — fallback only when very few results found
        if len(all_articles) < 5 and primary_query and not self._is_rate_limited("bing"):
            articles = self._search_bing(primary_query)
            all_articles.extend(articles)
            if articles:
                logger.debug(f"    Bing (fallback): {len(articles)}")

        return all_articles, direct_answers

    # -------------------------------------------------------------------------
    # Individual Source Implementations
    # -------------------------------------------------------------------------

    def _search_espn(self, sport: str, question: str) -> tuple:
        """
        Fetch ESPN scoreboard for the given sport.
        Returns (articles, score_summaries).
        Only includes events whose teams are mentioned in the question.
        """
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/scoreboard"
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                return [], []

            events = resp.json().get("events", [])
            if not events:
                return [], []

            question_lower = question.lower()
            articles = []
            summaries = []

            for event in events:
                comps = event.get("competitions", [])
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                t1 = competitors[0].get("team", {}).get("displayName", "")
                t2 = competitors[1].get("team", {}).get("displayName", "")
                s1 = competitors[0].get("score", "")
                s2 = competitors[1].get("score", "")
                w1 = competitors[0].get("winner", False)
                w2 = competitors[1].get("winner", False)
                status = comp.get("status", {}).get("type", {})
                completed = status.get("completed", False)
                state = status.get("state", "pre")

                # Only include if this event's teams appear in the question
                relevant = any(
                    word in question_lower
                    for team in (t1, t2)
                    for word in team.lower().split()
                    if len(word) > 3
                )
                if not relevant:
                    continue

                if completed and s1 and s2:
                    winner = t1 if w1 else (t2 if w2 else "Draw")
                    title = f"{t1} {s1}-{s2} {t2} (Final)"
                    content = f"Final score: {t1} {s1} - {s2} {t2}. Winner: {winner}."
                    summaries.append(f"{t1} {s1}-{s2} {t2}, {winner} won")
                elif state == "in":
                    title = f"LIVE: {t1} {s1}-{s2} {t2}"
                    content = f"In progress: {t1} {s1} - {s2} {t2}"
                else:
                    title = f"Upcoming: {t1} vs {t2}"
                    content = f"Scheduled matchup: {t1} vs {t2}"

                articles.append({
                    "url": f"https://www.espn.com/sport/game/_/gameId/{event.get('id', '')}",
                    "title": title,
                    "content": content,
                    "score": 0.9,
                    "source": "espn",
                })

            self._record_request("espn")
            return articles, summaries

        except Exception as e:
            logger.warning(f"    ESPN Fehler ({sport}): {e}")
            return [], []

    def _search_duckduckgo(self, query: str, max_results: int = 10) -> list:
        """Search via DuckDuckGo (free, no API key needed)."""
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            self._record_request("duckduckgo")
            return [
                {
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "content": r.get("body", ""),
                    "score": 0.5,
                    "source": "duckduckgo",
                }
                for r in results
            ]
        except ImportError:
            logger.warning("    ddgs nicht installiert: pip install ddgs")
            return []
        except Exception as e:
            if "ratelimit" in str(e).lower() or "202" in str(e):
                logger.warning("    DuckDuckGo: Rate limit erreicht, ueberspringe")
            else:
                logger.warning(f"    DuckDuckGo Fehler: {e}")
            return []

    def _search_google_news_rss(self, query: str, max_results: int = 15) -> list:
        """Search via Google News RSS feed (completely free, no key needed)."""
        try:
            import feedparser
            url = (
                f"https://news.google.com/rss/search"
                f"?q={quote_plus(query)}&hl=en&gl=US&ceid=US:en"
            )
            feed = feedparser.parse(url)
            articles = []
            for entry in feed.entries[:max_results]:
                articles.append({
                    "url": entry.get("link", ""),
                    "title": entry.get("title", ""),
                    "content": entry.get("summary", ""),
                    "score": 0.5,
                    "source": "google_news",
                })
            return articles
        except ImportError:
            logger.warning("    feedparser nicht installiert: pip install feedparser>=6.0.0")
            return []
        except Exception as e:
            logger.warning(f"    Google News RSS Fehler: {e}")
            return []

    def _search_serper(self, query: str) -> list:
        """Search via Serper API (2500 free searches/month from serper.dev)."""
        try:
            key = getattr(config, "SERPER_API_KEY", "") or ""
            if not key:
                return []
            headers = {"X-API-KEY": key, "Content-Type": "application/json"}
            resp = requests.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": 10},
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"    Serper HTTP {resp.status_code}: {resp.text[:80]}")
                return []
            self._record_request("serper")
            return [
                {
                    "url": r.get("link", ""),
                    "title": r.get("title", ""),
                    "content": r.get("snippet", ""),
                    "score": 0.6,
                    "source": "serper",
                }
                for r in resp.json().get("organic", [])
            ]
        except Exception as e:
            logger.warning(f"    Serper Fehler: {e}")
            return []

    def _search_newsapi(self, query: str) -> list:
        """Search via NewsAPI (100 free requests/day from newsapi.org)."""
        try:
            key = getattr(config, "NEWSAPI_KEY", "") or ""
            if not key:
                return []
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "apiKey": key,
                    "pageSize": 10,
                    "language": "en",
                    "sortBy": "relevancy",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"    NewsAPI HTTP {resp.status_code}: {resp.text[:80]}")
                return []
            self._record_request("newsapi")
            articles = []
            for a in resp.json().get("articles", []):
                if a.get("title") in ("[Removed]", None):
                    continue
                articles.append({
                    "url": a.get("url", ""),
                    "title": a.get("title", ""),
                    "content": a.get("description", "") or a.get("content", ""),
                    "score": 0.55,
                    "source": "newsapi",
                })
            return articles
        except Exception as e:
            logger.warning(f"    NewsAPI Fehler: {e}")
            return []

    def _search_bing(self, query: str) -> list:
        """Search via Bing web scraping (last-resort fallback)."""
        try:
            from bs4 import BeautifulSoup
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
            resp = requests.get(
                f"https://www.bing.com/search?q={quote_plus(query)}&count=10",
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            articles = []
            for result in soup.find_all("li", class_="b_algo")[:10]:
                title_tag = result.find("h2")
                link_tag = result.find("a")
                snippet_tag = result.find("p")
                title = title_tag.get_text(strip=True) if title_tag else ""
                url = link_tag.get("href", "") if link_tag else ""
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                if title and url:
                    articles.append({
                        "url": url,
                        "title": title,
                        "content": snippet,
                        "score": 0.4,
                        "source": "bing",
                    })
            self._record_request("bing")
            return articles
        except ImportError:
            logger.warning("    beautifulsoup4 nicht installiert: pip install beautifulsoup4>=4.12.0")
            return []
        except Exception as e:
            logger.warning(f"    Bing Fehler: {e}")
            return []

    # -------------------------------------------------------------------------
    # Rate Limiting (in-memory, resets on restart)
    # -------------------------------------------------------------------------

    def _is_rate_limited(self, source: str) -> bool:
        limits = RATE_LIMITS.get(source, {})
        now = datetime.now()
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key = now.strftime("%Y-%m-%d")
        state = self._rl.get(source, {})

        hourly = state.get("hourly_count", 0) if state.get("hour_key") == hour_key else 0
        daily = state.get("daily_count", 0) if state.get("day_key") == day_key else 0

        if hourly >= limits.get("hourly", 9999):
            logger.debug(f"    {source}: hourly limit ({hourly}/{limits['hourly']})")
            return True
        if daily >= limits.get("daily", 9999):
            logger.debug(f"    {source}: daily limit ({daily}/{limits['daily']})")
            return True
        return False

    def _record_request(self, source: str):
        now = datetime.now()
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key = now.strftime("%Y-%m-%d")
        state = self._rl.get(source, {})

        self._rl[source] = {
            "hour_key": hour_key,
            "day_key": day_key,
            "hourly_count": (state.get("hourly_count", 0) if state.get("hour_key") == hour_key else 0) + 1,
            "daily_count": (state.get("daily_count", 0) if state.get("day_key") == day_key else 0) + 1,
        }

    # -------------------------------------------------------------------------
    # Sport Detection
    # -------------------------------------------------------------------------

    def _detect_sport(self, text: str, category: str) -> Optional[str]:
        """Detect ESPN sport endpoint from market text. Returns None for non-sports."""
        if category.lower() in NON_SPORT_CATEGORIES:
            return None
        for sport_key, pattern in ESPN_SPORT_PATTERNS:
            if pattern.search(text):
                return sport_key
        return None

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_source_summary(self, articles: list) -> str:
        counts: dict = {}
        for a in articles:
            src = a.get("source", "?")
            counts[src] = counts.get(src, 0) + 1
        return ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))

    # -------------------------------------------------------------------------
    # Cache (identical logic to researcher.py)
    # -------------------------------------------------------------------------

    def _make_cache_key(self, market_id: str, days_to_resolution: int) -> str:
        raw = f"{market_id}_{date.today()}_{days_to_resolution}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _serialize_result(self, result: ResearchResult) -> str:
        return json.dumps(dataclasses.asdict(result), default=str)

    def _deserialize_result(self, json_str: str) -> ResearchResult:
        return ResearchResult(**json.loads(json_str))

    # -------------------------------------------------------------------------
    # Query Generation (identical to researcher.py)
    # -------------------------------------------------------------------------

    def _generate_search_queries(self, question: str, description: str,
                                  category: str, days_to_resolution: int) -> list:
        queries = []
        keywords = self._extract_keywords(question)
        main_kw = " ".join(keywords[:4]) if keywords else question[:50]
        month_year = datetime.now().strftime("%B %Y")

        if days_to_resolution <= 3:
            queries.append(f"{main_kw} result {month_year}")
            if category.lower() == "sports":
                teams = self._extract_team_names(question)
                if len(teams) >= 2:
                    queries.append(f"{teams[0]} vs {teams[1]} score {month_year}")
                else:
                    queries.append(f"{main_kw} score {month_year}")
            else:
                queries.append(f"{main_kw} outcome decision {month_year}")
            queries.append(f"{main_kw} winner confirmed {month_year}")
        else:
            queries.append(self._clean_question_for_search(question))
            time_prefix = "latest news" if days_to_resolution <= 7 else "recent developments"
            queries.append(f"{time_prefix} {main_kw}")
            category_queries = {
                "politics": f"political analysis prediction {main_kw}",
                "sports": f"sports odds prediction {main_kw}",
                "crypto": f"crypto market analysis {main_kw}",
                "economics": f"economic forecast {main_kw}",
            }
            cat_query = category_queries.get(category.lower())
            if cat_query:
                queries.append(cat_query)
            elif description:
                desc_kw = " ".join(self._extract_keywords(description)[:4])
                queries.append(desc_kw)
            else:
                queries.append(f"{main_kw} analysis forecast")

        return queries[:3]

    def _extract_keywords(self, text: str) -> list:
        stopwords = {
            "will", "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "by", "in",
            "on", "at", "to", "for", "of", "and", "or", "but", "not", "with",
            "this", "that", "it", "he", "she", "they", "we", "you", "before",
            "after", "during", "until", "end", "year", "month", "day", "win",
        }
        words = re.findall(r"\b[A-Za-z]{3,}\b", text)
        keywords = [w for w in words if w.lower() not in stopwords]
        proper_nouns = [w for w in keywords if w[0].isupper()]
        other_words = [w for w in keywords if not w[0].isupper()]
        return proper_nouns + other_words

    def _extract_team_names(self, question: str) -> list:
        patterns = [
            r"Will\s+([A-Z][A-Za-z\s]{2,25}?)\s+(?:beat|defeat|vs\.?|against)\s+([A-Z][A-Za-z\s]{2,25}?)(?:\s+(?:in|on|at|by|\?)|$)",
            r"([A-Z][A-Za-z\s]{2,20}?)\s+vs\.?\s+([A-Z][A-Za-z\s]{2,20})",
            r"([A-Z][A-Za-z\s]{2,20}?)\s+against\s+([A-Z][A-Za-z\s]{2,20})",
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                return [match.group(1).strip(), match.group(2).strip()]
        return []

    def _clean_question_for_search(self, question: str) -> str:
        cleaned = re.sub(r"^Will\s+", "", question, flags=re.IGNORECASE)
        cleaned = re.sub(r"\?$", "", cleaned)
        cleaned = re.sub(
            r"\s+by\s+(January|February|March|April|May|June|July|"
            r"August|September|October|November|December)\s+\d+",
            "", cleaned,
        )
        cleaned = re.sub(r"\s+before\s+\d{4}", "", cleaned)
        return cleaned.strip()[:100]

    # -------------------------------------------------------------------------
    # Source Tier Classification (same as researcher.py)
    # -------------------------------------------------------------------------

    def _classify_source_tier(self, article: dict) -> dict:
        url = article.get("url", "")
        # ESPN articles always get tier 1
        if article.get("source") == "espn":
            return {"url": url, "domain": "espn.com", "tier": 1, "weight": 1.0,
                    "score": article.get("score", 0.9)}
        try:
            domain = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
        except Exception:
            domain = ""

        if any(domain == d or domain.endswith("." + d) for d in TIER1_DOMAINS):
            tier, weight = 1, 1.0
        elif any(domain == d or domain.endswith("." + d) for d in TIER2_DOMAINS):
            tier, weight = 2, 0.7
        elif any(domain == d or domain.endswith("." + d) for d in TIER4_DOMAINS):
            tier, weight = 4, 0.1
        else:
            tier, weight = 3, 0.4

        return {"url": url, "domain": domain, "tier": tier, "weight": weight,
                "score": article.get("score", 0)}

    def _compute_weighted_confidence(self, articles: list, source_tiers: list) -> float:
        if not articles:
            return 0.1
        total_weight = 0.0
        weighted_score = 0.0
        for article, tier_info in zip(articles, source_tiers):
            relevance = article.get("score", 0.3)
            weight = tier_info["weight"]
            weighted_score += relevance * weight
            total_weight += weight
        if total_weight == 0:
            return 0.1
        raw_conf = weighted_score / total_weight
        tier12_bonus = min(0.2, len([t for t in source_tiers if t["tier"] <= 2]) * 0.04)
        return min(0.95, raw_conf + tier12_bonus)

    # -------------------------------------------------------------------------
    # Contradiction Detection (same as researcher.py)
    # -------------------------------------------------------------------------

    def _detect_contradictions(self, articles: list, source_tiers: list,
                                question: str, direct_answers: list) -> tuple:
        tier12_articles = [a for a, t in zip(articles, source_tiers) if t["tier"] <= 2]
        if len(tier12_articles) < 2:
            return False, ""

        claimed_outcomes = []
        for article in tier12_articles[:6]:
            outcome = self._extract_claimed_outcome(article, question)
            if outcome:
                claimed_outcomes.append((outcome, article.get("url", "")[:50]))

        if len(claimed_outcomes) < 2:
            return False, ""

        unique_outcomes = set(o[0] for o in claimed_outcomes)
        if len(unique_outcomes) > 1 and self._are_outcomes_contradictory(unique_outcomes, question):
            details = f"Widerspruch: {'; '.join(f'{o[0]} ({o[1]})' for o in claimed_outcomes[:3])}"
            return True, details

        if len(direct_answers) >= 2 and self._check_answer_contradiction(direct_answers, question):
            details = f"Widerspruch in Antworten: '{direct_answers[0][:80]}' vs '{direct_answers[1][:80]}'"
            return True, details

        return False, ""

    def _extract_claimed_outcome(self, article: dict, question: str) -> Optional[str]:
        content = (article.get("raw_content") or article.get("content", "")) + " " + article.get("title", "")
        content = content[:2000]
        question_keywords = set(w.lower() for w in self._extract_keywords(question))

        score_match = re.search(r"(\d+)[:\-](\d+)", content)
        if score_match:
            pos = score_match.start()
            ctx = content[max(0, pos - 100):pos + 100]
            teams = self._extract_team_names(question)
            if teams and (teams[0].lower() in ctx.lower() or
                          (len(teams) > 1 and teams[1].lower() in ctx.lower())):
                return f"score_{score_match.group(1)}_{score_match.group(2)}"

        for pattern in [
            r"([\w\s]{3,30}?)\s+(?:won|wins|beat|defeated|beats)\s",
            r"([\w\s]{3,30}?)\s+(?:victory|champion|winner)",
        ]:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                candidate = match.group(1).strip()
                if set(candidate.lower().split()) & question_keywords:
                    return candidate.lower()[:40]
        return None

    def _are_outcomes_contradictory(self, outcomes: set, question: str) -> bool:
        outcomes_list = list(outcomes)
        scores = [o for o in outcomes_list if o.startswith("score_")]
        if len(scores) > 1 and len(set(scores)) > 1:
            return True
        question_teams = self._extract_team_names(question)
        if question_teams:
            teams_claiming_win = [
                team for outcome in outcomes_list
                for team in question_teams
                if team.lower() in outcome.lower()
            ]
            if len(set(teams_claiming_win)) > 1:
                return True
        return False

    def _check_answer_contradiction(self, answers: list, question: str) -> bool:
        yes_indicators: set = set()
        no_indicators: set = set()
        teams = self._extract_team_names(question)
        for answer in answers[:3]:
            answer_lower = answer.lower()
            has_win = any(kw in answer_lower for kw in WIN_KEYWORDS)
            has_loss = any(kw in answer_lower for kw in LOSS_KEYWORDS)
            if teams:
                for team in teams:
                    tl = team.lower()
                    if tl in answer_lower:
                        if has_win:
                            yes_indicators.add(tl)
                        elif has_loss:
                            no_indicators.add(tl)
        return bool(yes_indicators & no_indicators) or len(yes_indicators) > 1

    # -------------------------------------------------------------------------
    # Verified Result Extraction
    # -------------------------------------------------------------------------

    def _extract_verified_result(self, articles: list, source_tiers: list,
                                  question: str, direct_answers: list) -> Optional[str]:
        # ESPN summaries are the most reliable (already formatted as "Team A 2-1 Team B, X won")
        if direct_answers:
            ans = direct_answers[0]
            if 5 < len(ans) < 300:
                return ans[:200]
        # Fallback: title of best tier 1-2 article
        for article, tier_info in zip(articles, source_tiers):
            if tier_info["tier"] <= 2:
                title = article.get("title", "")
                if title and len(title) > 10:
                    return title[:150]
        return None

    # -------------------------------------------------------------------------
    # Key Facts & Sentiment (identical to researcher.py)
    # -------------------------------------------------------------------------

    def _extract_key_facts(self, articles: list, question: str) -> list:
        facts = []
        question_keywords = set(w.lower() for w in self._extract_keywords(question))
        for article in articles[:8]:
            content = article.get("raw_content") or article.get("content", "")
            title = article.get("title", "")
            if article.get("score", 0) < 0.2:
                continue
            for sentence in re.split(r"[.!?]+", content):
                sentence = sentence.strip()
                if 30 <= len(sentence) <= 350:
                    sentence_words = set(w.lower() for w in re.findall(r"\b\w+\b", sentence))
                    if len(question_keywords & sentence_words) >= 2:
                        facts.append(sentence)
            if title and any(kw in title.lower() for kw in question_keywords):
                facts.append(f"[Headline] {title}")
        seen: set = set()
        unique_facts = []
        for f in facts:
            fc = f.lower().strip()
            if fc not in seen:
                seen.add(fc)
                unique_facts.append(f)
        return unique_facts[:8]

    def _assess_sentiment(self, articles: list, question: str, facts: list) -> tuple:
        if not articles:
            return "unclear", 0.1
        pos_count = 0
        neg_count = 0
        all_text = " ".join(
            (a.get("raw_content") or a.get("content", ""))[:500] for a in articles[:5]
        )
        for word in all_text.lower().split():
            clean = re.sub(r"[^a-z]", "", word)
            if clean in WIN_KEYWORDS:
                pos_count += 1
            elif clean in LOSS_KEYWORDS:
                neg_count += 1
        avg_score = sum(a.get("score", 0) for a in articles) / len(articles)
        confidence = min(0.9, avg_score * len(articles) / 5)
        if pos_count > neg_count * 1.5:
            return "bullish", confidence
        elif neg_count > pos_count * 1.5:
            return "bearish", confidence
        else:
            return "neutral", confidence * 0.7

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------

    def _create_summary(self, articles: list, question: str, facts: list,
                         days_to_resolution: int, direct_answers: list = None,
                         source_tiers: list = None, has_contradiction: bool = False,
                         verified_result: str = None) -> str:
        if not articles:
            return f"Keine aktuellen Artikel zu '{question}' gefunden."
        parts = []
        if has_contradiction:
            parts.append("WARNUNG: Widersprueche in den Quellen gefunden!")
        if verified_result:
            parts.append(f"Verifiziertes Ergebnis: {verified_result}")
        if direct_answers:
            parts.append(f"Top-Ergebnis: {direct_answers[0][:300]}")
        if source_tiers:
            tier_counts: dict = {}
            for t in source_tiers:
                tier_counts[t["tier"]] = tier_counts.get(t["tier"], 0) + 1
            tier_str = ", ".join(f"Tier {k}: {v}" for k, v in sorted(tier_counts.items()))
            source_names = sorted({a.get("source", "?") for a in articles})
            parts.append(f"Quellen ({len(articles)} Artikel, {', '.join(source_names)}): {tier_str}")
        parts.append(f"Basierend auf {len(articles)} Artikeln:")
        if facts:
            parts.append("Schluesselfindings:")
            for f in facts[:5]:
                parts.append(f"  - {f}")
        parts.append(f"Tage bis Aufloesung: {days_to_resolution}")
        return "\n".join(parts)


# =============================================================================
# CLI Test Mode: python researcher_v2.py --test
# =============================================================================

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test MultiSourceResearcher sources")
    parser.add_argument("--test", action="store_true", help="Run source tests")
    parser.add_argument(
        "--query",
        default="Lakers Celtics NBA score March 2026",
        help="Test query (default: NBA Lakers Celtics)",
    )
    args = parser.parse_args()

    if not args.test:
        print("Run with --test to test all sources")
        sys.exit(1)

    researcher = MultiSourceResearcher()
    query = args.query
    sep = "=" * 60

    print(f"\n{sep}")
    print(f"  MultiSourceResearcher — Source Test")
    print(f"  Query: {query}")
    print(f"{sep}\n")

    # 1. DuckDuckGo
    print("1. DuckDuckGo (free, no key):")
    r = researcher._search_duckduckgo(query, max_results=3)
    for item in r[:2]:
        print(f"   {item['title'][:70]}")
    print(f"   -> {len(r)} results {'OK' if r else 'FAILED (install duckduckgo-search)'}\n")

    # 2. Google News RSS
    print("2. Google News RSS (free, no key):")
    r = researcher._search_google_news_rss(query, max_results=3)
    for item in r[:2]:
        print(f"   {item['title'][:70]}")
    print(f"   -> {len(r)} results {'OK' if r else 'FAILED (install feedparser)'}\n")

    # 3. ESPN
    print("3. ESPN API (free, no key) — NBA scoreboard:")
    r, summaries = researcher._search_espn("basketball/nba", query)
    for item in r[:2]:
        print(f"   {item['title'][:70]}")
    if summaries:
        print(f"   Scores: {summaries[:2]}")
    print(f"   -> {len(r)} results {'OK' if r else 'No matching games (normal if offseason)'}\n")

    # 4. Serper
    serper_key = getattr(config, "SERPER_API_KEY", "") or ""
    print(f"4. Serper API ({'configured' if serper_key else 'NOT configured — add SERPER_API_KEY to .env'}):")
    if serper_key:
        r = researcher._search_serper(query)
        for item in r[:2]:
            print(f"   {item['title'][:70]}")
        print(f"   -> {len(r)} results {'OK' if r else 'FAILED'}")
    print()

    # 5. NewsAPI
    newsapi_key = getattr(config, "NEWSAPI_KEY", "") or ""
    print(f"5. NewsAPI ({'configured' if newsapi_key else 'NOT configured — add NEWSAPI_KEY to .env'}):")
    if newsapi_key:
        r = researcher._search_newsapi(query)
        for item in r[:2]:
            print(f"   {item['title'][:70]}")
        print(f"   -> {len(r)} results {'OK' if r else 'FAILED'}")
    print()

    # 6. Full research test
    print("6. Full research_market() test:")
    result = researcher.research_market(
        question="Will the Lakers beat the Celtics in their next game?",
        category="sports",
        days_to_resolution=1,
    )
    print(f"   Sentiment:          {result.sentiment}")
    print(f"   Articles found:     {len(result.articles)}")
    print(f"   Tier 1/2 sources:   {result.tier1_2_source_count}")
    print(f"   Weighted conf:      {result.weighted_confidence:.2f}")
    print(f"   Has contradiction:  {result.has_contradiction}")
    print(f"   Verified result:    {result.verified_result}")
    print(f"   Sources used:       {', '.join(set(a.get('source','?') for a in result.articles))}")

    print(f"\n{sep}")
    print("  Test complete. Zero 'usage limit exceeded' errors = SUCCESS.")
    print(f"{sep}\n")
