#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections import Counter


ARXIV_API_URL = "https://export.arxiv.org/api/query"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
DEFAULT_CONFIG = Path("config/interests.json")
DEFAULT_OUTPUT = Path("web/data/papers.json")
RETAINED_MATCH_LEVELS = {"high", "medium"}
DEFAULT_MAX_NEW_PAPERS = 30
DEFAULT_MAX_STORED_PAPERS = 150
DEFAULT_MAX_DATA_BYTES = 8 * 1024 * 1024
DEFAULT_RECENT_HISTORY_DAYS = 90
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
DEFAULT_SOURCE_TYPES = ["arxiv", "openalex", "crossref"]
FEED_NAMESPACES = {"atom": "http://www.w3.org/2005/Atom"}
LIKES_PATH = Path("web/data/likes.json")
DISLIKES_PATH = Path("web/data/dislikes.json")
PREFERENCES_PATH = Path("web/data/preferences.json")
_ARXIV_QUERY_CACHE: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}


def load_feedback_ids(path: Path, field: str) -> set[str]:
    """Load paper ids from a small feedback JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, dict):
        values = data.get(field, {})
        if isinstance(values, dict):
            return {str(key) for key, value in values.items() if value is not False}
        if isinstance(values, list):
            return {str(value) for value in values}
    if isinstance(data, list):
        return {str(value) for value in data}
    return set()


def load_likes(path: Path = LIKES_PATH) -> set[str]:
    """Load liked paper ids from the likes file."""
    return load_feedback_ids(path, "likes")


def load_dislikes(path: Path = DISLIKES_PATH) -> set[str]:
    """Load explicitly dismissed paper ids from the dislikes file."""
    return load_feedback_ids(path, "dislikes")


def load_preferences(path: Path = PREFERENCES_PATH) -> dict[str, Any]:
    """Load learned preferences, tolerating missing or malformed files."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_preferences(prefs: dict[str, Any], path: Path = PREFERENCES_PATH) -> None:
    """Write learned preference data to a JSON file."""
    try:
        write_json(path, prefs)
        print(f"Saved preferences to {path}", flush=True)
    except OSError as exc:
        print(f"Warning: could not save preferences: {exc}", file=sys.stderr, flush=True)


_PREFERENCE_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "from", "as", "into", "through", "during", "before", "after", "between",
    "which", "what", "who", "not", "no", "than", "also", "about", "each",
    "all", "both", "some", "any", "more", "most", "other", "many", "much",
    "new", "one", "two", "paper", "study", "approach", "method", "results",
}


def preference_terms(text: str) -> set[str]:
    """Extract stable unigram and bigram signals from a paper."""
    words = [
        word
        for word in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
        if len(word) >= 4 and word not in _PREFERENCE_STOP_WORDS
    ]
    terms = set(words)
    terms.update(
        f"{left} {right}"
        for left, right in zip(words, words[1:])
        if left != right
    )
    return terms


def _preference_strength(
    liked_frequency: int,
    liked_count: int,
    corpus_frequency: int,
    corpus_count: int,
) -> float:
    liked_rate = liked_frequency / max(1, liked_count)
    corpus_rate = corpus_frequency / max(1, corpus_count)
    lift = liked_rate / max(0.05, corpus_rate)
    return round(min(1.0, liked_rate * math.log2(1.0 + lift)), 3)


def learn_preferences(liked_papers: list[dict[str, Any]], all_papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Learn preference signals using per-paper frequency and corpus lift."""
    liked_count = len(liked_papers)
    corpus = all_papers or liked_papers
    corpus_count = len(corpus)
    min_keyword_papers = max(2, math.ceil(liked_count * 0.4))

    liked_term_counts: Counter[str] = Counter()
    corpus_term_counts: Counter[str] = Counter()
    liked_categories: Counter[str] = Counter()
    corpus_categories: Counter[str] = Counter()
    liked_authors: Counter[str] = Counter()

    for paper in corpus:
        text = f"{paper.get('title', '')} {str(paper.get('summary', ''))[:500]}"
        corpus_term_counts.update(preference_terms(text))
        corpus_categories.update({str(cat).strip() for cat in paper.get("categories", []) if str(cat).strip()})

    for paper in liked_papers:
        text = f"{paper.get('title', '')} {str(paper.get('summary', ''))[:500]}"
        liked_term_counts.update(preference_terms(text))
        liked_categories.update({str(cat).strip() for cat in paper.get("categories", []) if str(cat).strip()})
        liked_authors.update({str(author).strip() for author in paper.get("authors", []) if str(author).strip()})

    ranked_terms = []
    for term, count in liked_term_counts.items():
        if count < min_keyword_papers:
            continue
        strength = _preference_strength(count, liked_count, corpus_term_counts[term], corpus_count)
        if strength >= 0.25:
            ranked_terms.append((term, strength))
    ranked_terms.sort(key=lambda item: (item[1], len(item[0])), reverse=True)

    category_boosts = {
        category: _preference_strength(count, liked_count, corpus_categories[category], corpus_count)
        for category, count in liked_categories.items()
        if count >= 2
    }
    author_boosts = {
        author: round(min(1.0, count / max(2, liked_count)), 3)
        for author, count in liked_authors.items()
        if count >= 2
    }

    return {
        "version": 2,
        "keyword_boosts": dict(ranked_terms[:40]),
        "category_boosts": category_boosts,
        "author_boosts": author_boosts,
        "liked_count": liked_count,
        "learned_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    keywords: list[str]
    arxiv_categories: list[str]


@dataclass(frozen=True)
class SourceConfig:
    type: str
    name: str
    url: str = ""
    enabled: bool = True
    headers_env: str = ""
    bearer_token_env: str = ""


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def json_size_bytes(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")) + 1


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "")
    if not value.strip():
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_topics(config: dict[str, Any]) -> list[Topic]:
    topics = []
    for item in config.get("topics", []):
        topic_id = item.get("id") or slugify(item.get("name", "topic"))
        topics.append(
            Topic(
                id=topic_id,
                name=item["name"],
                description=item.get("description", ""),
                keywords=[str(k) for k in item.get("keywords", [])],
                arxiv_categories=[str(c) for c in item.get("arxiv_categories", [])],
            )
        )
    if not topics:
        raise ValueError("No topics found in configuration.")
    return topics


def category_whitelist(config: dict[str, Any]) -> set[str] | None:
    values = config.get("arxiv_categories_whitelist", [])
    if not isinstance(values, list) or not values:
        return None
    return {str(v) for v in values}


def parse_negative_terms(config: dict[str, Any]) -> dict[str, float]:
    configured = config.get("negative_terms")
    if not isinstance(configured, dict):
        return {}
    result = {}
    for term, value in configured.items():
        try:
            result[str(term).lower()] = min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            continue
    return result


def parse_sources(config: dict[str, Any]) -> list[SourceConfig]:
    configured = config.get("sources")
    if not configured:
        configured = [{"type": source_type} for source_type in env_list("PAPER_SOURCES", DEFAULT_SOURCE_TYPES)]

    sources = []
    for item in configured:
        if isinstance(item, str):
            item = {"type": item}
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("type") or "").strip().lower()
        if not source_type:
            continue
        if (
            source_type in {"semantic_scholar", "semanticscholar", "semantic-scholar"}
            and not env_flag("ENABLE_SEMANTIC_SCHOLAR", False)
        ):
            continue
        if item.get("enabled", True) is False:
            continue
        name = str(item.get("name") or source_type.replace("_", " ").title())
        sources.append(
            SourceConfig(
                type=source_type,
                name=name,
                url=str(item.get("url") or ""),
                enabled=bool(item.get("enabled", True)),
                headers_env=str(item.get("headers_env") or ""),
                bearer_token_env=str(item.get("bearer_token_env") or ""),
            )
        )
    return sources


def merge_config(default_config: dict[str, Any], override_config: dict[str, Any] | None) -> dict[str, Any]:
    if not override_config:
        return default_config

    merged = copy.deepcopy(default_config)
    if "topics" in override_config and "negative_terms" not in override_config:
        merged["negative_terms"] = {}
    for key, value in override_config.items():
        merged[key] = value
    return merged


def github_request(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "paper-daily-collector",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_block(markdown: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, flags=re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1))
    stripped = markdown.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    return None


def load_issue_config(default_config: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    title = os.getenv("CONFIG_ISSUE_TITLE", "Research Interests")
    if not token or not repository:
        return default_config

    query = urllib.parse.urlencode({"state": "open", "per_page": "30"})
    url = f"https://api.github.com/repos/{repository}/issues?{query}"
    try:
        issues = github_request(url, token)
    except Exception as exc:
        print(f"Warning: cannot read GitHub issues, using config file: {exc}", file=sys.stderr)
        return default_config

    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title", "").strip().lower() == title.lower():
            body = issue.get("body") or ""
            try:
                issue_config = extract_json_block(body)
            except json.JSONDecodeError as exc:
                print(f"Warning: config issue JSON is invalid, using config file: {exc}", file=sys.stderr)
                return default_config
            if issue_config and issue_config.get("topics"):
                return merge_config(default_config, issue_config)
    return default_config


def arxiv_query_for_topic(topic: Topic) -> str:
    keyword_terms = arxiv_keyword_terms(topic.keywords[:8])

    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    query_mode = os.getenv("ARXIV_QUERY_MODE", "keyword").strip().lower()
    if query_mode == "keyword":
        if keyword_terms:
            return "(" + " OR ".join(keyword_terms) + ")"
        if category_terms:
            return "(" + " OR ".join(category_terms) + ")"
        return f'all:"{topic.name}"'

    if query_mode == "strict":
        parts = []
        if keyword_terms:
            parts.append("(" + " OR ".join(keyword_terms) + ")")
        if category_terms:
            parts.append("(" + " OR ".join(category_terms) + ")")
        return " AND ".join(parts) if parts else f'all:"{topic.name}"'

    terms = [*keyword_terms, *category_terms]
    if terms:
        return "(" + " OR ".join(terms) + ")"

    parts = []
    if keyword_terms:
        parts.append("(" + " OR ".join(keyword_terms) + ")")
    if category_terms:
        parts.append("(" + " OR ".join(category_terms) + ")")
    return " AND ".join(parts) if parts else f'all:"{topic.name}"'


def arxiv_keyword_terms(keywords: list[str]) -> list[str]:
    terms = []
    for keyword in keywords:
        escaped = keyword.replace('"', '\\"')
        terms.append(f'all:"{escaped}"')
    return terms


def arxiv_keyword_queries_for_topic(topic: Topic) -> list[str]:
    query_mode = os.getenv("ARXIV_QUERY_MODE", "keyword").strip().lower()
    if query_mode != "keyword":
        return [arxiv_query_for_topic(topic)]

    chunk_size = max(1, env_int("ARXIV_KEYWORD_CHUNK_SIZE", 8))
    chunk_count = max(1, env_int("ARXIV_KEYWORD_QUERY_CHUNKS", 1))
    selected_keywords = topic.keywords[: chunk_size * chunk_count]
    if not selected_keywords:
        return [arxiv_query_for_topic(topic)]

    queries = []
    for start in range(0, len(selected_keywords), chunk_size):
        keyword_terms = arxiv_keyword_terms(selected_keywords[start : start + chunk_size])
        if keyword_terms:
            queries.append("(" + " OR ".join(keyword_terms) + ")")
    return queries or [arxiv_query_for_topic(topic)]


def arxiv_category_query_for_topic(topic: Topic) -> str:
    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    if category_terms:
        return "(" + " OR ".join(category_terms) + ")"
    return arxiv_query_for_topic(topic)


def topic_plain_query(topic: Topic, limit: int = 6) -> str:
    return " ".join(topic.keywords[:limit]) or topic.name


def html_to_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(html.unescape(without_tags))


def date_to_iso(value: str | int | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, int):
        return f"{value:04d}-01-01T00:00:00+00:00"
    parsed = parse_datetime(str(value))
    if parsed:
        return parsed.isoformat()
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+00:00"
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01T00:00:00+00:00"
    return text


def request_json(url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "paper-daily-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_bytes(url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "paper-daily-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def source_request_headers(source: SourceConfig) -> dict[str, str]:
    headers = {"User-Agent": "paper-daily-collector/1.0"}
    if source.headers_env:
        raw_headers = os.getenv(source.headers_env, "")
        if raw_headers:
            try:
                configured_headers = json.loads(raw_headers)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source.headers_env} must contain a JSON object of HTTP headers") from exc
            if not isinstance(configured_headers, dict):
                raise ValueError(f"{source.headers_env} must contain a JSON object of HTTP headers")
            headers.update({str(key): str(value) for key, value in configured_headers.items()})
    if source.bearer_token_env:
        token = os.getenv(source.bearer_token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def arxiv_retry_wait_seconds(exc: Exception, attempt: int) -> float:
    min_wait = float(os.getenv("ARXIV_RETRY_MIN_SECONDS", "45"))
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return max(min_wait, float(retry_after))
    base = float(os.getenv("ARXIV_RETRY_BASE_SECONDS", "45"))
    cap = float(os.getenv("ARXIV_RETRY_MAX_SECONDS", "180"))
    return max(min_wait, min(cap, base * (2**attempt)))


def is_retryable_arxiv_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))


def should_retry_arxiv_error(exc: Exception) -> bool:
    if not is_retryable_arxiv_error(exc):
        return False
    if isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}:
        return env_flag("ARXIV_RETRY_THROTTLED", False)
    return True


def should_stop_arxiv_fetches(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}


def parse_arxiv_entries(xml_data: bytes, seed_topic: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        paper_id = entry.findtext("atom:id", default="", namespaces=ARXIV_NS).strip()
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
        summary = normalize_space(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
        published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS)
        updated = entry.findtext("atom:updated", default="", namespaces=ARXIV_NS)
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
            for author in entry.findall("atom:author", ARXIV_NS)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ARXIV_NS)
            if category.attrib.get("term")
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        papers.append(
            {
                "id": paper_id.rsplit("/", 1)[-1],
                "source": "arXiv",
                "title": title,
                "authors": [a for a in authors if a],
                "summary": summary,
                "published": published,
                "updated": updated,
                "paper_url": paper_id,
                "pdf_url": pdf_url or paper_id.replace("/abs/", "/pdf/"),
                "categories": categories,
                "seed_topic": seed_topic,
            }
        )
    return papers


def fetch_arxiv_query(search_query: str, max_results: int, sort_by: str, sort_order: str, label: str) -> list[dict[str, Any]]:
    cache_key = (search_query, max_results, sort_by, sort_order)
    if env_flag("ARXIV_QUERY_CACHE", True) and cache_key in _ARXIV_QUERY_CACHE:
        print(f"Using cached arXiv results for {label}", flush=True)
        return copy.deepcopy(_ARXIV_QUERY_CACHE[cache_key])

    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
    retry_count = max(1, int(os.getenv("ARXIV_RETRIES", "4")))
    timeout_seconds = float(os.getenv("ARXIV_TIMEOUT_SECONDS", "90"))
    last_error: Exception | None = None
    for attempt in range(retry_count):
        req = urllib.request.Request(url, headers={"User-Agent": "paper-daily-collector/1.0 (+https://github.com/Futuresxy/paper-daily)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                xml_data = resp.read()
            break
        except Exception as exc:
            last_error = exc
            if not should_retry_arxiv_error(exc) or attempt == retry_count - 1:
                raise
            wait_seconds = arxiv_retry_wait_seconds(exc, attempt)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                print(f"arXiv rate limited {label}, retrying in {wait_seconds:.0f}s", flush=True)
            else:
                print(f"arXiv temporary error for {label}: {exc}; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    else:
        raise RuntimeError(f"arXiv request failed: {last_error}")
    papers = parse_arxiv_entries(xml_data)
    if env_flag("ARXIV_QUERY_CACHE", True):
        _ARXIV_QUERY_CACHE[cache_key] = copy.deepcopy(papers)
    return papers


def fetch_arxiv(topic: Topic, max_results: int) -> list[dict[str, Any]]:
    sort_by = os.getenv("ARXIV_SORT_BY", "submittedDate").strip() or "submittedDate"
    papers = []
    keyword_queries = arxiv_keyword_queries_for_topic(topic)
    in_topic_delay = float(os.getenv("ARXIV_IN_TOPIC_DELAY_SECONDS", "3"))
    for query_index, search_query in enumerate(keyword_queries):
        if query_index and in_topic_delay > 0:
            time.sleep(in_topic_delay)
        label = topic.name if len(keyword_queries) == 1 else f"{topic.name} keywords {query_index + 1}/{len(keyword_queries)}"
        papers.extend(
            fetch_arxiv_query(
                search_query,
                max_results,
                sort_by=sort_by,
                sort_order="descending",
                label=label,
            )
        )
    if env_flag("ARXIV_EXPAND_CATEGORY_SEARCH", False) and topic.arxiv_categories:
        if in_topic_delay > 0:
            time.sleep(in_topic_delay)
        category_max_results = max(1, int(os.getenv("ARXIV_CATEGORY_MAX_RESULTS", str(max_results))))
        category_papers = fetch_arxiv_query(
            arxiv_category_query_for_topic(topic),
            category_max_results,
            sort_by=sort_by,
            sort_order="descending",
            label=f"{topic.name} categories",
        )
        papers = dedupe_papers([*papers, *category_papers])
    else:
        papers = dedupe_papers(papers)
    for paper in papers:
        paper["seed_topic"] = topic.id
    return papers


def semantic_scholar_paper_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    paper_id = str(item.get("paperId") or "")
    title = normalize_space(str(item.get("title") or ""))
    if not paper_id or not title:
        return None
    raw_authors = item.get("authors") or []
    raw_fields = item.get("fieldsOfStudy") or []
    authors = [str(author.get("name") or "") for author in raw_authors[:12] if isinstance(author, dict)]
    categories = [str(value) for value in raw_fields if value]
    venue = str(item.get("venue") or "")
    if venue:
        categories.append(venue)
    pdf_url = str((item.get("openAccessPdf") or {}).get("url") or "")
    return {
        "id": f"s2:{paper_id}",
        "source": "Semantic Scholar",
        "title": title,
        "authors": [author for author in authors if author],
        "summary": normalize_space(str(item.get("abstract") or "")),
        "published": date_to_iso(item.get("publicationDate") or item.get("year")),
        "updated": "",
        "paper_url": str(item.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"),
        "pdf_url": pdf_url,
        "categories": categories[:8],
    }


def openalex_paper_from_work(work: dict[str, Any], source_name: str = "OpenAlex") -> dict[str, Any] | None:
    title = normalize_space(str(work.get("title") or ""))
    work_id = str(work.get("id") or work.get("doi") or title)
    if not title or not work_id:
        return None
    locations = work.get("locations") or []
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    pdf_url = (
        primary.get("pdf_url")
        or best_oa.get("pdf_url")
        or next((location.get("pdf_url") for location in locations if location.get("pdf_url")), "")
    )
    authors = [
        str((authorship.get("author") or {}).get("display_name") or "")
        for authorship in work.get("authorships", [])
    ]
    concepts = [
        str(concept.get("display_name") or "")
        for concept in work.get("concepts", [])[:8]
        if concept.get("display_name")
    ]
    return {
        "id": f"openalex:{work_id.rsplit('/', 1)[-1]}",
        "source": source_name,
        "title": title,
        "authors": [author for author in authors if author],
        "summary": normalize_space(openalex_abstract_text(work)),
        "published": date_to_iso(work.get("publication_date") or work.get("publication_year")),
        "updated": "",
        "paper_url": str(work.get("doi") or work.get("id") or ""),
        "pdf_url": str(pdf_url or ""),
        "categories": concepts,
    }


def crossref_paper_from_item(item: dict[str, Any], source_name: str = "Crossref") -> dict[str, Any] | None:
    title = normalize_space(" ".join(str(part) for part in item.get("title", []) if part))
    doi = str(item.get("DOI") or "")
    paper_url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
    if not title or not (doi or paper_url):
        return None
    authors = []
    for author in item.get("author", [])[:12]:
        name = normalize_space(f"{author.get('given', '')} {author.get('family', '')}")
        if name:
            authors.append(name)
    pdf_url = ""
    for link in item.get("link", []):
        if "pdf" in str(link.get("content-type", "")).lower() and link.get("URL"):
            pdf_url = str(link.get("URL"))
            break
    return {
        "id": f"crossref:{doi or slugify(title)}",
        "source": source_name,
        "title": title,
        "authors": authors,
        "summary": html_to_text(str(item.get("abstract") or "")),
        "published": crossref_date(item),
        "updated": "",
        "paper_url": paper_url,
        "pdf_url": pdf_url,
        "categories": [str(subject) for subject in item.get("subject", [])[:8]],
    }


def openalex_abstract_text(work: dict[str, Any]) -> str:
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                words.append((position, str(word)))
    return " ".join(word for _, word in sorted(words))


def fetch_openalex(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "search": topic_plain_query(topic),
        "per-page": str(max_results),
        "sort": "publication_date:desc",
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("OPENALEX_EMAIL")
    if mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, timeout=float(os.getenv("OPENALEX_TIMEOUT_SECONDS", "60")))
    papers = []
    for work in data.get("results", []):
        paper = openalex_paper_from_work(work, source.name)
        if not paper:
            continue
        paper["seed_topic"] = topic.id
        papers.append(paper)
    return papers


def crossref_date(item: dict[str, Any]) -> str:
    for field in ("published-print", "published-online", "published", "created", "issued"):
        date_parts = (item.get(field) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            parts = list(date_parts[0])
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return dt.datetime(year, month, day, tzinfo=dt.timezone.utc).isoformat()
    return ""


def fetch_crossref(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "query.bibliographic": topic_plain_query(topic),
        "rows": str(max_results),
        "sort": "published",
        "order": "desc",
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("CROSSREF_EMAIL")
    if mailto:
        params["mailto"] = mailto
    headers = {"User-Agent": f"paper-daily-collector/1.0 (mailto:{mailto or 'unknown@example.com'})"}
    url = f"{CROSSREF_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("CROSSREF_TIMEOUT_SECONDS", "60")))
    papers = []
    for item in (data.get("message") or {}).get("items", []):
        paper = crossref_paper_from_item(item, source.name)
        if not paper:
            continue
        paper["seed_topic"] = topic.id
        papers.append(paper)
    return papers


def fetch_semantic_scholar(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "query": topic_plain_query(topic),
        "limit": str(min(max_results, 100)),
        "fields": "paperId,title,abstract,authors,year,publicationDate,url,openAccessPdf,venue,externalIds,fieldsOfStudy",
    }
    headers = {"User-Agent": "paper-daily-collector/1.0"}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key
    url = f"{SEMANTIC_SCHOLAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("SEMANTIC_SCHOLAR_TIMEOUT_SECONDS", "60")))
    papers = []
    for item in data.get("data") or []:
        candidate = semantic_scholar_paper_from_item(item)
        if not candidate:
            continue
        candidate["source"] = source.name
        candidate["seed_topic"] = topic.id
        papers.append(candidate)
    return papers


def fetch_google_scholar_serpapi(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    api_key = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY is required for google_scholar_serpapi source")
    params = {
        "engine": "google_scholar",
        "q": topic_plain_query(topic),
        "num": str(min(max_results, 20)),
        "api_key": api_key,
    }
    data = request_json(
        f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(params)}",
        timeout=float(os.getenv("SERPAPI_TIMEOUT_SECONDS", "90")),
    )
    papers = []
    for item in data.get("organic_results", []):
        title = normalize_space(str(item.get("title") or ""))
        paper_url = str(item.get("link") or "")
        if not title or not paper_url:
            continue
        publication = item.get("publication_info") or {}
        publication_summary = str(publication.get("summary") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", publication_summary)
        resources = item.get("resources") or []
        pdf_url = next((str(resource.get("link")) for resource in resources if str(resource.get("file_format", "")).upper() == "PDF"), "")
        papers.append(
            {
                "id": f"google-scholar:{slugify(paper_url or title)}",
                "source": source.name,
                "title": title,
                "authors": [],
                "summary": normalize_space(" ".join([str(item.get("snippet") or ""), publication_summary])),
                "published": date_to_iso(year_match.group(0) if year_match else ""),
                "updated": "",
                "paper_url": paper_url,
                "pdf_url": pdf_url,
                "categories": ["Google Scholar"],
                "seed_topic": topic.id,
            }
        )
    return papers


def link_from_atom(entry: ET.Element) -> str:
    alternate = ""
    for link in entry.findall("atom:link", FEED_NAMESPACES):
        href = link.attrib.get("href", "")
        rel = link.attrib.get("rel", "alternate")
        if rel == "alternate" and href:
            return href
        if href and not alternate:
            alternate = href
    return alternate


def fetch_feed(source: SourceConfig, max_results: int) -> list[dict[str, Any]]:
    if not source.url:
        return []
    xml_data = request_bytes(
        source.url,
        headers=source_request_headers(source),
        timeout=float(os.getenv("FEED_TIMEOUT_SECONDS", "60")),
    )
    root = ET.fromstring(xml_data)
    papers = []
    atom_entries = root.findall("atom:entry", FEED_NAMESPACES)
    if root.tag.endswith("entry"):
        atom_entries = [root]
    for entry in atom_entries[:max_results]:
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=FEED_NAMESPACES))
        summary = entry.findtext("atom:summary", default="", namespaces=FEED_NAMESPACES) or entry.findtext("atom:content", default="", namespaces=FEED_NAMESPACES)
        paper_url = link_from_atom(entry)
        paper_id = entry.findtext("atom:id", default=paper_url, namespaces=FEED_NAMESPACES)
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=FEED_NAMESPACES))
            for author in entry.findall("atom:author", FEED_NAMESPACES)
        ]
        categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", FEED_NAMESPACES)]
        papers.append(
            {
                "id": f"feed:{slugify(source.name)}:{paper_id or paper_url or slugify(title)}",
                "source": source.name,
                "title": title,
                "authors": [author for author in authors if author],
                "summary": html_to_text(summary or ""),
                "published": date_to_iso(entry.findtext("atom:published", default="", namespaces=FEED_NAMESPACES)),
                "updated": date_to_iso(entry.findtext("atom:updated", default="", namespaces=FEED_NAMESPACES)),
                "paper_url": paper_url,
                "pdf_url": "",
                "categories": [category for category in categories if category],
                "seed_topic": "",
            }
        )

    for item in root.findall(".//channel/item")[:max_results]:
        title = normalize_space(item.findtext("title", default=""))
        paper_url = normalize_space(item.findtext("link", default=""))
        guid = normalize_space(item.findtext("guid", default=paper_url))
        papers.append(
            {
                "id": f"feed:{slugify(source.name)}:{guid or paper_url or slugify(title)}",
                "source": source.name,
                "title": title,
                "authors": [],
                "summary": html_to_text(item.findtext("description", default="")),
                "published": date_to_iso(item.findtext("pubDate", default="")),
                "updated": "",
                "paper_url": paper_url,
                "pdf_url": "",
                "categories": [],
                "seed_topic": "",
            }
        )
    return [paper for paper in papers if paper.get("title")]


def fetch_source_topic(source: SourceConfig, topic: Topic, max_results: int) -> list[dict[str, Any]]:
    if source.type == "arxiv":
        return fetch_arxiv(topic, max_results)
    if source.type == "openalex":
        return fetch_openalex(topic, max_results, source)
    if source.type == "crossref":
        return fetch_crossref(topic, max_results, source)
    if source.type == "semantic_scholar":
        return fetch_semantic_scholar(topic, max_results, source)
    if source.type == "google_scholar_serpapi":
        return fetch_google_scholar_serpapi(topic, max_results, source)
    raise ValueError(f"Unsupported topic source type: {source.type}")


def is_feed_source(source: SourceConfig) -> bool:
    return source.type in {"feed", "rss", "atom"}


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def paper_datetime(paper: dict[str, Any]) -> dt.datetime:
    for field in ("published", "updated", "last_seen_at", "first_seen_at"):
        parsed = parse_datetime(str(paper.get(field, "")))
        if parsed:
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def paper_activity_datetime(paper: dict[str, Any]) -> dt.datetime:
    for field in ("updated", "published", "last_seen_at", "first_seen_at"):
        parsed = parse_datetime(str(paper.get(field, "")))
        if parsed:
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def collection_cutoff(
    existing_payload: dict[str, Any],
    now: dt.datetime,
    days: int,
    incremental_since_last_run: bool,
) -> tuple[dt.datetime, str]:
    if incremental_since_last_run:
        previous_run = parse_datetime(
            str(existing_payload.get("generated_at_iso") or existing_payload.get("generated_at") or "")
        )
        if previous_run:
            return previous_run, "incremental"
    return now - dt.timedelta(days=max(0, days)), "lookback"


# ── Bridge terms (from doc §10) ──
# Signal lists for each of the 4 topics.  When a paper hits terms from
# TWO different lists, both topics get a bridge bonus (§10.2 algorithm).
ALGEBRAIC_GEOMETRY_BRIDGE = [
    "scheme", "schemes", "algebraic stack", "derived stack",
    "quasi-coherent sheaf", "coherent sheaf", "perfect complexes",
    "smooth morphism", "etale morphism", "proper morphism",
    "descent", "moduli space", "intersection theory",
]
ARITHMETIC_GEOMETRY_BRIDGE = [
    "arithmetic geometry", "number field", "p-adic",
    "galois representation", "etale cohomology", "l-adic cohomology",
    "frobenius", "shimura variety", "l-function", "regulator",
    "syntomic cohomology", "galois cohomology",
]
HOMOTOPY_THEORY_BRIDGE = [
    "homotopy theory", "spectrum", "spectra", "stable homotopy",
    "sphere spectrum", "adams spectral sequence", "steenrod algebra",
    "generalized cohomology", "e_infinity", "stable infinity category",
    "thh", "tc", "cyclotomic spectra",
]
K_THEORY_MOTIVIC_BRIDGE = [
    "k-theory", "algebraic k-theory", "motivic k-theory",
    "kgl", "kq", "mgl", "motivic homotopy", "a1-homotopy",
    "stable motivic homotopy", "motivic spectra", "sh(k)", "sh(s)",
    "motivic cohomology", "dm(k)", "nisnevich", "cdh descent",
    "six functor formalism", "purity", "cyclotomic trace",
    "localizing invariant",
]

# Map topic_id to its bridge-signal list
_TOPIC_BRIDGE_SIGNALS: dict[str, list[str]] = {
    "motivic_k_theory":  K_THEORY_MOTIVIC_BRIDGE,
    "algebraic_geometry":   ALGEBRAIC_GEOMETRY_BRIDGE,
    "arithmetic_geometry":  ARITHMETIC_GEOMETRY_BRIDGE,
    "homotopy_theory":      HOMOTOPY_THEORY_BRIDGE,
}

# Cross-domain pairs: (term_substring, [topic_ids_that_get_bonus])
# When a paper contains one of these substrings, all listed topics get partial credit.
# Covers all 10 inter-domain pairs from the ontology:
#   Motivic∩AG, Motivic∩Arithmetic, Motivic∩Homotopy, Motivic∩K-theory,
#   AG∩Arithmetic, AG∩Homotopy, AG∩K-theory,
#   Arithmetic∩Homotopy, Arithmetic∩K-theory, Homotopy∩K-theory.
# Also includes the 3-way/4-way bridges (e.g. Motivic↔AG/Homotopy/K-theory).
CROSS_DOMAIN_SIGNALS: list[tuple[str, list[str], str]] = [
    # ── Motivic ∩ Algebraic Geometry (6.1) ──
    ("motivic spaces", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("smooth schemes", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("torsors", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("algebraic vector bundles", ["motivic_homotopy_theory", "algebraic_geometry", "k_theory"], "motivic ↔ AG / K-theory"),
    ("classifying spaces", ["motivic_homotopy_theory", "algebraic_geometry", "homotopy_theory"], "motivic ↔ AG / homotopy"),
    ("algebraic cycles", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("A1-representability", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("motivic cohomology", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("simplicial presheaves", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("simplicial sheaves", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("A1-weak equivalence", ["motivic_homotopy_theory", "algebraic_geometry", "homotopy_theory"], "motivic ↔ AG / homotopy"),
    ("Chow-Witt", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("A1-degree", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("Euler class", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),

    # ── Motivic ∩ Arithmetic Geometry (6.2) ──
    ("l-adic realization", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("arithmetic motives", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("mixed motives", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("Tate motives", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("motivic Galois group", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("periods", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("Hodge realization", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("de Rham realization", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("crystalline realization", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("special values", ["motivic_homotopy_theory", "arithmetic_geometry", "k_theory"], "motivic ↔ arithmetic / K-theory"),
    ("Beilinson conjectures", ["motivic_homotopy_theory", "arithmetic_geometry", "k_theory"], "motivic ↔ arithmetic / K-theory"),
    ("Bloch-Kato", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("Tate conjecture", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),

    # ── Motivic ∩ Homotopy Theory (6.3) ──
    ("motivic spectra", ["motivic_homotopy_theory", "homotopy_theory", "k_theory"], "motivic ↔ homotopy / K-theory"),
    ("motivic sphere spectrum", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("bigraded homotopy groups", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("motivic Adams spectral sequence", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("motivic Steenrod algebra", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("motivic E_infinity", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("motivic ring spectrum", ["motivic_homotopy_theory", "homotopy_theory", "k_theory"], "motivic ↔ homotopy / K-theory"),
    ("motivic stable stems", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("slice spectral sequence", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("S^{p,q}", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),

    # ── Motivic ∩ K-Theory (6.4) ──
    ("KGL", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory"),
    ("KQ", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory (hermitian)"),
    ("MGL", ["motivic_homotopy_theory", "k_theory", "homotopy_theory"], "motivic ↔ K-theory / homotopy"),
    ("motivic K-theory", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory"),
    ("algebraic K-theory spectrum", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory"),
    ("Thomason-Trobaugh", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory"),
    ("K-theory of schemes", ["k_theory", "algebraic_geometry", "motivic_homotopy_theory"], "K-theory ↔ AG / motivic"),
    ("K-theory of finite fields", ["k_theory", "arithmetic_geometry"], "K-theory ↔ arithmetic"),

    # ── Algebraic Geometry ∩ Arithmetic Geometry (6.5) ──
    ("schemes over Spec Z", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("arithmetic schemes", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("Dedekind schemes", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("regular models", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("arithmetic surfaces", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("abelian schemes", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("modular curves", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("Shimura variety", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("Arakelov", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("rational points", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("integral points", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),
    ("height function", ["algebraic_geometry", "arithmetic_geometry"], "AG ↔ arithmetic"),

    # ── Algebraic Geometry ∩ Homotopy Theory (6.6) ──
    ("derived algebraic geometry", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("spectral algebraic geometry", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("higher stack", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("derived stack", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("infinity stack", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("homotopical algebraic geometry", ["algebraic_geometry", "homotopy_theory"], "AG ↔ homotopy"),
    ("moduli stack", ["algebraic_geometry", "homotopy_theory", "arithmetic_geometry"], "AG ↔ homotopy / arithmetic"),

    # ── Algebraic Geometry ∩ K-Theory (6.7) ──
    ("perfect complexes", ["k_theory", "algebraic_geometry"], "K-theory ↔ AG"),
    ("locally free sheaves", ["k_theory", "algebraic_geometry"], "K-theory ↔ AG"),
    ("G-theory", ["k_theory", "algebraic_geometry"], "K-theory ↔ AG"),
    ("devissage", ["k_theory", "algebraic_geometry"], "K-theory ↔ AG"),
    ("Bass negative K-theory", ["k_theory", "algebraic_geometry"], "K-theory ↔ AG"),
    ("vector bundles", ["k_theory", "algebraic_geometry", "motivic_homotopy_theory"], "K-theory ↔ AG / motivic"),
    ("coherent sheaf", ["algebraic_geometry", "k_theory"], "AG ↔ K-theory"),

    # ── Arithmetic Geometry ∩ Homotopy Theory (6.8) ──
    ("etale homotopy type", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("profinite homotopy theory", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("arithmetic homotopy", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("p-adic homotopy", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("Galois descent", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("homotopy fixed points", ["arithmetic_geometry", "homotopy_theory"], "arithmetic ↔ homotopy"),
    ("chromatic homotopy", ["homotopy_theory", "arithmetic_geometry", "k_theory"], "homotopy ↔ arithmetic / K-theory"),

    # ── Arithmetic Geometry ∩ K-Theory (6.9) ──
    ("Quillen-Lichtenbaum", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),
    ("regulator", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),
    ("Beilinson regulator", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),
    ("Borel regulator", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),
    ("L-function", ["arithmetic_geometry", "k_theory", "motivic_homotopy_theory"], "arithmetic ↔ K-theory / motivic"),
    ("etale K-theory", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),
    ("K-theory of number rings", ["arithmetic_geometry", "k_theory"], "arithmetic ↔ K-theory"),

    # ── Homotopy Theory ∩ K-Theory (6.10) ──
    ("topological K-theory", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("K-theory spectrum", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("Bott periodicity", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("KU", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("KO", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("connective K-theory", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("THH", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("TC", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("cyclotomic spectra", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("trace methods", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("cyclotomic trace", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("Dennis trace", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("Hochschild homology", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("cyclic homology", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("assembly map", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("Baum-Connes", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),
    ("Farrell-Jones", ["homotopy_theory", "k_theory"], "homotopy ↔ K-theory"),

    # ── 3-way / 4-way core bridges (repeated to ensure coverage) ──
    ("A1-homotopy theory", ["motivic_homotopy_theory", "algebraic_geometry", "homotopy_theory"], "motivic ↔ AG / homotopy"),
    ("Nisnevich topology", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("etale cohomology", ["arithmetic_geometry", "algebraic_geometry"], "arithmetic ↔ AG"),
    ("l-adic cohomology", ["arithmetic_geometry", "algebraic_geometry"], "arithmetic ↔ AG"),
    ("etale realization", ["motivic_homotopy_theory", "arithmetic_geometry"], "motivic ↔ arithmetic"),
    ("localization sequence", ["k_theory", "homotopy_theory"], "K-theory ↔ homotopy"),
    ("E_infinity algebra", ["homotopy_theory", "k_theory", "motivic_homotopy_theory"], "homotopy ↔ K-theory / motivic"),
    ("stable infinity category", ["k_theory", "homotopy_theory"], "K-theory ↔ homotopy"),
    ("Galois representation", ["arithmetic_geometry", "motivic_homotopy_theory"], "arithmetic ↔ motivic"),
    ("modular forms", ["arithmetic_geometry", "algebraic_geometry"], "arithmetic ↔ AG"),
    ("Adams spectral sequence", ["homotopy_theory", "motivic_homotopy_theory"], "homotopy ↔ motivic"),
    ("Steenrod algebra", ["homotopy_theory", "motivic_homotopy_theory"], "homotopy ↔ motivic"),
    ("six functor", ["motivic_homotopy_theory", "algebraic_geometry", "arithmetic_geometry"], "motivic ↔ AG / arithmetic"),
    ("SH(k)", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("SH(S)", ["motivic_homotopy_theory", "algebraic_geometry", "homotopy_theory"], "motivic ↔ AG / homotopy"),
    ("Grothendieck-Witt", ["k_theory", "motivic_homotopy_theory"], "K-theory ↔ motivic"),
    ("Witt groups", ["k_theory", "motivic_homotopy_theory"], "K-theory ↔ motivic"),
    ("Hermitian K-theory", ["k_theory", "motivic_homotopy_theory"], "K-theory ↔ motivic"),
    ("Milnor-Witt K-theory", ["motivic_homotopy_theory", "k_theory"], "motivic ↔ K-theory"),
    ("higher Chow groups", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("descent", ["algebraic_geometry", "arithmetic_geometry", "homotopy_theory", "motivic_homotopy_theory"], "cross-domain: descent"),
    ("purity theorem", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("Gysin map", ["motivic_homotopy_theory", "algebraic_geometry"], "motivic ↔ AG"),
    ("Thom space", ["motivic_homotopy_theory", "homotopy_theory"], "motivic ↔ homotopy"),
    ("Thom spectrum", ["homotopy_theory", "motivic_homotopy_theory", "k_theory"], "homotopy ↔ motivic / K-theory"),
    ("Bousfield localization", ["homotopy_theory", "motivic_homotopy_theory"], "homotopy ↔ motivic"),
    ("zeta function", ["arithmetic_geometry", "k_theory", "motivic_homotopy_theory"], "arithmetic ↔ K-theory / motivic"),
    ("Serre spectral sequence", ["homotopy_theory", "algebraic_geometry"], "homotopy ↔ AG"),
]


_TOPIC_ID_ALIASES = {
    "motivic_homotopy_theory": "motivic_k_theory",
    "k_theory": "motivic_k_theory",
}


def canonical_topic_id(topic_id: str) -> str:
    return _TOPIC_ID_ALIASES.get(topic_id, topic_id)


# ── Negative keywords (§11) ──
NEGATIVE_TERMS: dict[str, float] = {
    "machine learning": 0.25, "neural network": 0.25,
    "large language model": 0.25, "computer vision": 0.30,
    "data mining": 0.30, "statistics": 0.30,
    "persistent homology": 0.30, "topological data analysis": 0.30,
    "quantum computing": 0.45, "condensed matter": 0.45,
    "d-brane": 0.45, "string theory": 0.45,
    "operator algebra": 0.55, "c*-algebra": 0.55,
}


def apply_negative_penalty(
    text: str,
    score: float,
    positive_signal: float = 0.0,
    negative_terms: dict[str, float] | None = None,
) -> float:
    """Apply a bounded penalty without erasing strong positive evidence."""
    t = text.lower()
    factor = 1.0
    for term, penalty in (NEGATIVE_TERMS if negative_terms is None else negative_terms).items():
        if term in t:
            factor = min(factor, penalty)
    if factor >= 1.0:
        return score
    confidence = min(1.0, max(0.0, positive_signal))
    effective_factor = 1.0 - (1.0 - factor) * 0.35 * (1.0 - 0.75 * confidence)
    return score * effective_factor


def compute_bridge_score(paper: dict[str, Any], topic_id: str) -> tuple[float, list[str]]:
    """Reward genuine cross-topic papers while keeping the bonus bounded."""
    topic_id = canonical_topic_id(topic_id)
    haystack = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()

    def _hits(signal_list: list[str]) -> int:
        return sum(1 for term in signal_list if term.lower() in haystack)

    own = _hits(_TOPIC_BRIDGE_SIGNALS.get(topic_id, []))
    pairs = []
    total = 0.0
    if own:
        for other_id, other_signals in _TOPIC_BRIDGE_SIGNALS.items():
            if other_id == topic_id:
                continue
            oh = _hits(other_signals)
            if oh > 0:
                shared_strength = min(own, oh)
                total += min(0.035, 0.012 + 0.008 * shared_strength)
                pairs.append(f"桥接 {topic_id}↔{other_id} (hits={own}/{oh})")

    for term, topic_ids, label in CROSS_DOMAIN_SIGNALS:
        canonical_ids = {canonical_topic_id(value) for value in topic_ids}
        if topic_id in canonical_ids and term.lower() in haystack:
            total += 0.012
            pairs.append(label)

    bonus = round(min(0.08, total), 3)
    return bonus, pairs[:3]


def normalized_match_text(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[$\\](?:[a-z]+\s)?", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return normalize_space(text)


def contains_signal(text: str, signal: str) -> bool:
    normalized = normalized_match_text(signal)
    if not normalized:
        return False
    if len(normalized) <= 3 and normalized.replace("_", "").isalpha():
        return bool(re.search(rf"\b{re.escape(normalized)}\b", text))
    return normalized in text


def keyword_score(topic: Topic, paper: dict[str, Any]) -> tuple[float, list[str]]:
    title = normalized_match_text(str(paper.get("title", "")))
    summary = normalized_match_text(str(paper.get("summary", "")))
    matched: list[tuple[str, str, bool]] = []
    for keyword in sorted(topic.keywords, key=lambda item: len(normalized_match_text(item)), reverse=True):
        normalized = normalized_match_text(keyword)
        in_title = contains_signal(title, normalized)
        if not in_title and not contains_signal(summary, normalized):
            continue
        # Avoid double-counting nested ontology entries such as
        # "motivic homotopy theory" and "homotopy theory".
        if any(normalized in longer for _, longer, _ in matched):
            continue
        matched.append((keyword, normalized, in_title))

    weighted = 0.0
    for _, normalized, in_title in matched:
        word_count = max(1, len(normalized.split()))
        specificity = min(0.85, 0.20 + 0.18 * word_count)
        weighted += specificity * (1.35 if in_title else 1.0)
    score = 1.0 - math.exp(-weighted / 1.35)
    return round(min(1.0, score), 3), [keyword for keyword, _, _ in matched[:8]]


def category_score(topic: Topic, paper: dict[str, Any]) -> float:
    paper_categories = [str(category) for category in paper.get("categories", [])]
    topic_categories = set(topic.arxiv_categories)
    if not paper_categories or not topic_categories:
        return 0.0
    if paper_categories[0] in topic_categories:
        return 1.0
    return 0.65 if set(paper_categories) & topic_categories else 0.0


# English stop-words that carry little domain information and inflate
# lexical-overlap scores across all mathematics papers.
_STOP_WORDS: set[str] = {
    "a", "an", "the", "and", "or", "of", "in", "on", "to", "for",
    "with", "by", "at", "from", "as", "is", "are", "was", "be",
    "it", "its", "we", "they", "this", "that", "which", "all",
    "not", "no", "but", "if", "can", "has", "have", "been", "will",
    "each", "some", "any", "also", "more", "over", "into", "than",
    "one", "two", "new", "via", "up", "out", "so", "using", "may",
    "such", "only", "these", "those", "between", "both", "does",
    "how", "what", "when", "where", "who", "why", "their",
    "about", "after", "before", "during", "under", "within",
    "toward", "along", "above", "below", "just", "most",
    "other", "well", "much", "many", "very",
}


def lexical_overlap_score(topic: Topic, paper: dict[str, Any]) -> float:
    """Content-word lexical overlap between topic keywords and paper content.

    Filters out common English stop-words and generic academic terms so that
    only meaningful mathematical vocabulary contributes.
    """
    topic_terms = set(
        w for w in re.findall(r"[a-zA-Z0-9]+", f"{topic.description} {' '.join(topic.keywords)}".lower())
        if w not in _STOP_WORDS and len(w) > 2
    )
    paper_terms = set(
        w for w in re.findall(r"[a-zA-Z0-9]+", f"{paper.get('title', '')} {paper.get('summary', '')}".lower())
        if w not in _STOP_WORDS and len(w) > 2
    )
    if not topic_terms or not paper_terms:
        return 0.0
    overlap = topic_terms & paper_terms
    return min(1.0, len(overlap) / max(10, math.sqrt(len(topic_terms) * len(paper_terms))))


def _stored_preference_strength(value: Any, liked_count: int) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric > 1.0:
        numeric /= max(1, liked_count)
    return min(1.0, max(0.0, numeric))


def preference_boost_score(
    paper: dict[str, Any],
    preferences: dict[str, Any] | None,
) -> tuple[float, list[str]]:
    if not preferences:
        return 0.0, []

    liked_count = int(preferences.get("liked_count") or 0)
    haystack = normalized_match_text(f"{paper.get('title', '')} {paper.get('summary', '')}")
    keyword_matches = []
    for term, value in (preferences.get("keyword_boosts") or {}).items():
        if contains_signal(haystack, str(term)):
            strength = _stored_preference_strength(value, liked_count)
            keyword_matches.append((str(term), strength))
    keyword_matches.sort(key=lambda item: item[1], reverse=True)
    keyword_bonus = min(0.06, sum(0.025 * strength for _, strength in keyword_matches[:3]))

    paper_categories = {str(category) for category in paper.get("categories", [])}
    category_strength = max(
        (
            _stored_preference_strength(value, liked_count)
            for category, value in (preferences.get("category_boosts") or {}).items()
            if str(category) in paper_categories
        ),
        default=0.0,
    )
    paper_authors = {str(author).lower() for author in paper.get("authors", [])}
    author_strength = max(
        (
            _stored_preference_strength(value, liked_count)
            for author, value in (preferences.get("author_boosts") or {}).items()
            if str(author).lower() in paper_authors
        ),
        default=0.0,
    )
    bonus = min(0.10, keyword_bonus + 0.025 * category_strength + 0.025 * author_strength)
    reasons = [f"偏好词：{term}" for term, _ in keyword_matches[:2]]
    if category_strength:
        reasons.append("收藏分类偏好")
    if author_strength:
        reasons.append("收藏作者偏好")
    return round(bonus, 3), reasons


def match_level(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.42:
        return "medium"
    return "low"


def score_paper(
    topic: Topic,
    paper: dict[str, Any],
    preferences: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
    negative_terms: dict[str, float] | None = None,
) -> dict[str, Any]:
    k_score, hits = keyword_score(topic, paper)
    c_score = category_score(topic, paper)
    l_score = lexical_overlap_score(topic, paper)
    base_score = round(0.55 * k_score + 0.18 * c_score + 0.17 * l_score, 3)

    bridge_bonus, bridge_reasons = compute_bridge_score(paper, topic.id)
    adjusted_score = round(base_score + bridge_bonus, 3)

    preference_bonus, preference_reasons = preference_boost_score(paper, preferences)
    adjusted_score = round(adjusted_score + preference_bonus, 3)

    reference_time = now or dt.datetime.now(dt.timezone.utc)
    pub = paper_activity_datetime(paper)
    age_days = max(0.0, (reference_time - pub).total_seconds() / 86400)
    r_score = 0.05 * math.exp(-age_days / 30.0) if pub > dt.datetime.min.replace(tzinfo=dt.timezone.utc) else 0.0
    adjusted_score = round(adjusted_score + r_score, 3)

    text = f"{paper.get('title', '')} {paper.get('summary', '')}"
    active_negative_terms = negative_terms or {}
    penalty_factor = apply_negative_penalty(text, 1.0, k_score, active_negative_terms)
    adjusted_score = round(
        apply_negative_penalty(text, adjusted_score, k_score, active_negative_terms),
        3,
    )
    adjusted_score = round(min(1.0, max(0.0, adjusted_score)), 3)

    reason_parts = []
    if hits:
        reason_parts.append("关键词命中：" + "、".join(hits))
    if c_score > 0:
        reason_parts.append("arXiv 分类重合：" + "、".join(sorted(set(topic.arxiv_categories) & set(paper.get("categories", [])))))
    if bridge_reasons:
        reason_parts.extend(bridge_reasons)
    if preference_reasons:
        reason_parts.extend(preference_reasons)
    if r_score > 0:
        reason_parts.append(f"时效加分 +{r_score:.2f}")
    if penalty_factor < 1.0:
        reason_parts.append(f"跨领域降权 (×{penalty_factor:.2f})")
    if not reason_parts:
        reason_parts.append("文本语义与方向描述存在弱相关，需要人工复核。")
    return {
        "topic_id": topic.id,
        "topic_name": topic.name,
        "score": adjusted_score,
        "base_score": base_score,
        "bridge_bonus": bridge_bonus,
        "preference_bonus": preference_bonus,
        "recency_bonus": round(r_score, 3),
        "level": match_level(adjusted_score),
        "reason": "；".join(reason_parts),
        "keyword_hits": hits,
    }


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def has_meaningful_summary(paper: dict[str, Any], min_chars: int = 80) -> bool:
    summary = normalize_space(str(paper.get("summary") or ""))
    return len(summary) >= min_chars


def is_relevant_enough(paper: dict[str, Any], best_match: dict[str, Any]) -> bool:
    score = float(best_match.get("score") or 0.0)
    if best_match.get("keyword_hits"):
        return score >= env_float("MIN_KEYWORD_MATCH_SCORE", 0.10)
    if not has_meaningful_summary(paper):
        return score >= env_float("MIN_TITLE_ONLY_SCORE", 0.20)
    return score >= env_float("MIN_PAPER_SCORE", 0.10)


def should_summarize_paper_with_llm(paper: dict[str, Any]) -> bool:
    has_summary = has_meaningful_summary(paper)
    if not has_summary and not env_flag("LLM_SUMMARIZE_TITLE_ONLY", False):
        return False
    return True


def fallback_summary(paper: dict[str, Any], best_match: dict[str, Any]) -> dict[str, str]:
    abstract = paper.get("summary", "")
    first_sentence = re.split(r"(?<=[.!?])\s+", abstract)[0] if abstract else ""
    if not has_meaningful_summary(paper):
        return {
            "problem": "来源没有提供足够摘要，当前不调用模型做标题猜测。",
            "method": "请打开论文链接查看方法细节。",
            "innovation": "标题信息不足，无法可靠提取创新点。",
            "evidence": "证据不足，需要阅读全文核验。",
            "limitations": "缺少摘要会降低自动相关性和中文总结质量。",
            "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
        }
    return {
        "problem": "未配置模型 API，当前仅基于标题、摘要和关键词生成基础摘要。",
        "method": first_sentence[:300] if first_sentence else "请打开论文链接查看方法细节。",
        "innovation": "需要接入模型 API 后自动抽取更精确的中文创新点。",
        "evidence": "来源摘要可在论文原文中核验。",
        "limitations": "基础模式不会阅读全文，也不会进行深度技术对比。",
        "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
    }


def llm_enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))


def llm_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "paper-daily-collector/1.0",
    }


def resolve_llm_model() -> str:
    configured_model = os.getenv("LLM_MODEL", "").strip()
    if configured_model:
        return configured_model
    return "deepseek-v4-flash" if os.getenv("DEEPSEEK_API_KEY") else "gpt-4o-mini"


def call_openai_compatible(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = os.getenv("LLM_BASE_URL", "")
    if not base_url:
        base_url = "https://api.deepseek.com/v1" if os.getenv("DEEPSEEK_API_KEY") else "https://api.openai.com/v1"
    model = resolve_llm_model()
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的论文技术分析助手。只输出合法 JSON，不要输出 Markdown。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=llm_headers(api_key),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def arxiv_base_id(paper: dict[str, Any]) -> str:
    value = " ".join(str(paper.get(field) or "") for field in ("id", "paper_url", "pdf_url"))
    match = re.search(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b", value)
    return match.group(1) if match else ""


def arxiv_html_url(paper: dict[str, Any]) -> str:
    base_id = arxiv_base_id(paper)
    return f"https://ar5iv.labs.arxiv.org/html/{base_id}" if base_id else ""


def html_document_to_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript|svg|math)[^>]*>.*?</\1>", " ", raw_html)
    cleaned = re.sub(r"(?is)<(nav|header|footer|aside)[^>]*>.*?</\1>", " ", cleaned)
    return html_to_text(cleaned)


def fetch_arxiv_fulltext_excerpt(paper: dict[str, Any]) -> tuple[str, str]:
    url = arxiv_html_url(paper)
    if not url:
        return "", ""
    timeout_seconds = env_float("ARXIV_HTML_TIMEOUT_SECONDS", 20.0)
    max_chars = max(500, env_int("ARXIV_HTML_MAX_CHARS", 6000))
    min_chars = max(100, env_int("ARXIV_HTML_MIN_CHARS", 600))
    html_data = request_bytes(
        url,
        headers={"User-Agent": "paper-daily-collector/1.0 (+https://github.com/Futuresxy/paper-daily)"},
        timeout=timeout_seconds,
    )
    text = html_document_to_text(html_data.decode("utf-8", errors="ignore"))
    if len(text) < min_chars:
        return "", url
    return text[:max_chars], url


def enrich_arxiv_fulltext(papers: list[dict[str, Any]]) -> dict[str, int]:
    if not env_flag("ENABLE_ARXIV_HTML_ENRICHMENT", False):
        return {"fulltext_attempted_count": 0, "fulltext_enriched_count": 0, "fulltext_failed_count": 0}

    limit = max(0, env_int("ARXIV_HTML_MAX_PAPERS", 12))
    delay_seconds = env_float("ARXIV_HTML_DELAY_SECONDS", 1.0)
    attempted = 0
    enriched = 0
    failed = 0
    for paper in papers:
        if attempted >= limit:
            break
        if str(paper.get("source", "")).lower() != "arxiv":
            continue
        if paper.get("fulltext_excerpt"):
            continue
        if not arxiv_base_id(paper):
            continue
        if attempted and delay_seconds > 0:
            time.sleep(delay_seconds)
        attempted += 1
        try:
            excerpt, source_url = fetch_arxiv_fulltext_excerpt(paper)
        except Exception as exc:
            failed += 1
            print(f"Warning: arXiv fulltext enrichment failed for {paper.get('id')}: {exc}", file=sys.stderr)
            continue
        if excerpt:
            paper["fulltext_excerpt"] = excerpt
            paper["fulltext_source_url"] = source_url
            enriched += 1
    return {
        "fulltext_attempted_count": attempted,
        "fulltext_enriched_count": enriched,
        "fulltext_failed_count": failed,
    }


def add_count_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def build_llm_prompt(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> str:
    fulltext_section = ""
    if paper.get("fulltext_excerpt"):
        fulltext_section = f"""
正文摘录（来自 ar5iv，可能不完整，优先用来核验方法和证据）：
{paper.get("fulltext_excerpt", "")}
""".strip()

    return f"""
请根据论文标题、摘要、分类、可用正文摘录和我的研究方向，输出精确中文分析。目标不是逐句翻译，而是综合已有材料快速判断这篇论文是否值得阅读。
要求：
1. 先识别论文真正解决的问题、核心机制、实验或系统证据，再翻译成自然中文。
2. 不要夸大摘要中没有的信息；如果证据不足，请明确说明。
3. 相关性判断要严格，说明它具体匹配哪些关键词、场景或系统瓶颈。
4. 如果论文只是泛泛相关，请把 match_level 降为 medium 或 low，并在 why_relevant 里说明需要人工复核。

我的研究方向：
名称：{topic.name}
描述：{topic.description}
关键词：{", ".join(topic.keywords)}

论文信息：
标题：{paper.get("title", "")}
作者：{", ".join(paper.get("authors", [])[:8])}
arXiv 分类：{", ".join(paper.get("categories", []))}
摘要：{paper.get("summary", "")}
{fulltext_section}

基础匹配信息：
分数：{base_match.get("score")}
等级：{base_match.get("level")}
原因：{base_match.get("reason")}

请输出 JSON，字段必须为：
{{
  "problem": "论文要解决的问题，中文，1-2句，避免空泛背景",
  "method": "核心方法，中文，2-3句，包含关键技术组件或系统流程",
  "innovation": "相对已有工作的具体创新点，中文，2-3点合并成一段",
  "evidence": "摘要中可核验的实验、理论或系统证据；没有则写证据不足",
  "limitations": "可能局限或需要阅读全文确认的点",
  "why_relevant": "为什么匹配我的研究方向",
  "match_score_adjustment": 0.0,
  "match_level": "high|medium|low"
}}
""".strip()


def summarize_with_llm(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    if not llm_enabled():
        return fallback_summary(paper, base_match), base_match

    prompt = build_llm_prompt(topic, paper, base_match)
    try:
        data = call_openai_compatible(prompt)
    except Exception as exc:
        print(f"Warning: LLM summary failed for {paper.get('id')}: {exc}", file=sys.stderr)
        return fallback_summary(paper, base_match), base_match

    summary = {
        "problem": str(data.get("problem", "")),
        "method": str(data.get("method", "")),
        "innovation": str(data.get("innovation", "")),
        "evidence": str(data.get("evidence", "")),
        "limitations": str(data.get("limitations", "")),
        "why_relevant": str(data.get("why_relevant", "")),
    }
    adjustment = float(data.get("match_score_adjustment", 0.0) or 0.0)
    adjusted_score = max(0.0, min(1.0, base_match["score"] + adjustment))
    adjusted_level = str(data.get("match_level") or match_level(adjusted_score)).lower()
    if adjusted_level not in {"high", "medium", "low"}:
        adjusted_level = match_level(adjusted_score)
    adjusted_match = dict(base_match)
    adjusted_match["score"] = round(adjusted_score, 3)
    adjusted_match["level"] = adjusted_level
    adjusted_match["llm_reason"] = summary["why_relevant"]
    return summary, adjusted_match


def summarize_one(args: tuple[Topic, dict[str, Any]]) -> tuple[str, dict[str, str], dict[str, Any]]:
    topic, paper = args
    paper_id = str(paper.get("id", ""))
    summary, adjusted_match = summarize_with_llm(topic, paper, paper["best_match"])
    return paper_id, summary, adjusted_match


def best_topic_for_paper(topics_by_id: dict[str, Topic], paper: dict[str, Any]) -> Topic | None:
    topic_id = str((paper.get("best_match") or {}).get("topic_id") or "")
    return topics_by_id.get(topic_id)


def build_summary_jobs(topics_by_id: dict[str, Topic], papers: list[dict[str, Any]]) -> list[tuple[Topic, dict[str, Any]]]:
    jobs = []
    for paper in papers:
        if not should_summarize_paper_with_llm(paper):
            continue
        best_topic = best_topic_for_paper(topics_by_id, paper)
        if best_topic:
            jobs.append((best_topic, paper))
    return jobs


def summarize_papers(jobs: list[tuple[Topic, dict[str, Any]]]) -> dict[str, tuple[dict[str, str], dict[str, Any]]]:
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    if not jobs:
        return summaries_by_id

    if llm_enabled():
        concurrency = max(1, int(os.getenv("LLM_CONCURRENCY", "2")))
        print(f"Summarizing {len(jobs)} papers with LLM using concurrency={concurrency}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(summarize_one, job) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                paper_id, summary, adjusted_match = future.result()
                summaries_by_id[paper_id] = (summary, adjusted_match)
                print(f"Finished summary: {paper_id}", flush=True)
    else:
        for topic, paper in jobs:
            summary, adjusted_match = summarize_with_llm(topic, paper, paper["best_match"])
            summaries_by_id[str(paper.get("id", ""))] = (summary, adjusted_match)
    return summaries_by_id


def apply_adjusted_matches(
    papers: list[dict[str, Any]],
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]],
) -> None:
    for paper in papers:
        paper_id = str(paper.get("id", ""))
        if paper_id not in summaries_by_id:
            continue
        _, adjusted_match = summaries_by_id[paper_id]
        paper["best_match"] = adjusted_match
        paper["matches"] = [adjusted_match if m["topic_id"] == adjusted_match["topic_id"] else m for m in paper["matches"]]


def dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for paper in papers:
        key = paper.get("id") or paper.get("paper_url")
        if key in seen:
            continue
        seen.add(key)
        unique.append(paper)
    return unique


def title_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_terms = set(re.findall(r"[a-z0-9]+", str(left.get("title", "")).lower()))
    right_terms = set(re.findall(r"[a-z0-9]+", str(right.get("title", "")).lower()))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def select_diverse_papers(papers: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Select high-quality papers with soft topic and near-duplicate penalties."""
    if limit <= 0 or len(papers) <= limit:
        return list(papers)

    pool = list(papers)
    selected: list[dict[str, Any]] = []
    topic_counts: Counter[str] = Counter()
    while pool and len(selected) < limit:
        def selection_key(paper: dict[str, Any]) -> tuple[float, dt.datetime]:
            best_match = paper.get("best_match") or {}
            topic_id = str(best_match.get("topic_id") or "")
            relevance = float(best_match.get("score") or 0.0)
            topic_penalty = min(0.14, topic_counts[topic_id] * 0.03)
            duplicate_penalty = 0.0
            if selected:
                duplicate_penalty = max(title_similarity(paper, chosen) for chosen in selected) * 0.10
            return relevance - topic_penalty - duplicate_penalty, paper_activity_datetime(paper)

        best = max(pool, key=selection_key)
        pool.remove(best)
        selected.append(best)
        topic_counts[str((best.get("best_match") or {}).get("topic_id") or "")] += 1
    return selected


def paper_key(paper: dict[str, Any]) -> str:
    return str(paper.get("id") or paper.get("paper_url") or "")


def best_match_level(paper: dict[str, Any]) -> str:
    return str((paper.get("best_match") or {}).get("level") or "low").lower()


def load_existing_payload(output_path: Path) -> dict[str, Any]:
    if not output_path.exists():
        return {}
    try:
        return load_json(output_path)
    except Exception as exc:
        print(f"Warning: cannot read existing paper data, starting fresh: {exc}", file=sys.stderr)
        return {}


def merge_with_retained_papers(
    current_papers: list[dict[str, Any]],
    existing_payload: dict[str, Any],
    now: dt.datetime,
    recent_history_days: int,
    liked_set: set[str] | None = None,
    disliked_set: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    existing_papers = existing_payload.get("papers", []) if isinstance(existing_payload, dict) else []
    existing_generated_at = str(existing_payload.get("generated_at_iso") or existing_payload.get("generated_at") or now.isoformat())
    retained_by_key: dict[str, dict[str, Any]] = {}
    dropped_low = 0
    dropped_disliked = 0
    retained_recent = 0
    for paper in existing_papers:
        if not isinstance(paper, dict):
            continue
        key = paper_key(paper)
        if not key:
            continue
        seen_at = parse_datetime(str(paper.get("first_seen_at") or paper.get("last_seen_at") or existing_generated_at))
        is_recent = bool(
            recent_history_days > 0
            and seen_at
            and (now.date() - seen_at.date()).days <= recent_history_days
        )
        liked_badge = bool(liked_set and key in liked_set)
        disliked_badge = bool(disliked_set and key in disliked_set and not liked_badge)
        if disliked_badge:
            dropped_disliked += 1
            continue
        if liked_badge or best_match_level(paper) in RETAINED_MATCH_LEVELS or is_recent:
            retained_by_key[key] = paper
            if not liked_badge and is_recent and best_match_level(paper) not in RETAINED_MATCH_LEVELS:
                retained_recent += 1
        else:
            dropped_low += 1

    merged = []
    seen = set()
    now_iso = now.isoformat()
    for paper in current_papers:
        key = paper_key(paper)
        previous = retained_by_key.get(key)
        if previous:
            paper.setdefault("first_seen_at", previous.get("first_seen_at") or existing_generated_at)
        else:
            paper.setdefault("first_seen_at", now_iso)
        paper["last_seen_at"] = now_iso
        paper["retained_from_previous_run"] = False
        merged.append(paper)
        if key:
            seen.add(key)

    retained_count = 0
    for key, paper in retained_by_key.items():
        if key in seen:
            continue
        retained = dict(paper)
        retained.setdefault("first_seen_at", existing_generated_at)
        retained.setdefault("last_seen_at", existing_generated_at)
        retained["retained_from_previous_run"] = True
        merged.append(retained)
        retained_count += 1

    return dedupe_papers(merged), {
        "retained_paper_count": retained_count,
        "retained_recent_low_count": retained_recent,
        "dropped_low_relevance_count": dropped_low,
        "dropped_disliked_count": dropped_disliked,
    }


def deletion_sort_key(paper: dict[str, Any], liked_set: set[str] | None = None) -> tuple[int, dt.datetime]:
    if liked_set and paper_key(paper) in liked_set:
        return 999, dt.datetime.max  # never delete liked papers
    level = best_match_level(paper)
    relevance_priority = 0 if level == "low" else 2
    return relevance_priority, paper_datetime(paper)


def trim_papers_for_storage(
    payload: dict[str, Any],
    max_stored_papers: int,
    max_data_bytes: int,
    liked_set: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    papers = list(payload.get("papers", []))
    removed_by_level = {"high": 0, "medium": 0, "low": 0, "unknown": 0}

    def projected_size() -> int:
        projected = dict(payload)
        projected["papers"] = papers
        return json_size_bytes(projected)

    data_bytes = projected_size()
    while papers and (
        (max_stored_papers > 0 and len(papers) > max_stored_papers)
        or (max_data_bytes > 0 and data_bytes > max_data_bytes)
    ):
        removable_indexes = [
            index
            for index, paper in enumerate(papers)
            if not liked_set or paper_key(paper) not in liked_set
        ]
        if not removable_indexes:
            break
        remove_index = min(removable_indexes, key=lambda index: deletion_sort_key(papers[index], liked_set))
        removed = papers.pop(remove_index)
        level = best_match_level(removed)
        removed_by_level[level if level in removed_by_level else "unknown"] += 1
        data_bytes = projected_size()

    return papers, {
        "max_stored_papers": max_stored_papers,
        "max_data_bytes": max_data_bytes,
        "data_bytes": data_bytes,
        "storage_trimmed_count": sum(removed_by_level.values()),
        "storage_trimmed_by_level": removed_by_level,
        "storage_limit_exceeded_by_likes": bool(
            (max_stored_papers > 0 and len(papers) > max_stored_papers)
            or (max_data_bytes > 0 and data_bytes > max_data_bytes)
        ),
    }


def collect(
    config_path: Path,
    output_path: Path,
    days: int,
    max_per_topic: int,
    max_summaries: int,
    max_new_papers: int,
    max_stored_papers: int,
    max_data_bytes: int,
    incremental_since_last_run: bool,
    recent_history_days: int,
    clear_cache: bool,
) -> dict[str, Any]:
    data_dir = output_path.parent
    likes_path = data_dir / "likes.json"
    dislikes_path = data_dir / "dislikes.json"
    preferences_path = data_dir / "preferences.json"
    liked_set = load_likes(likes_path)
    disliked_set = load_dislikes(dislikes_path)
    preferences = load_preferences(preferences_path) if len(liked_set) >= 3 else {}
    default_config = load_json(config_path)
    config = load_issue_config(default_config)
    topics = parse_topics(config)
    topics_by_id = {topic.id: topic for topic in topics}
    sources = parse_sources(config)
    negative_terms = parse_negative_terms(config)
    arxiv_category_filter = category_whitelist(config)
    now = dt.datetime.now(dt.timezone.utc)
    existing_payload = {} if clear_cache else load_existing_payload(output_path)
    existing_paper_ids = {
        str(paper.get("id", ""))
        for paper in existing_payload.get("papers", [])
        if isinstance(paper, dict) and paper.get("id")
    }
    cutoff, collection_mode = collection_cutoff(existing_payload, now, days, incremental_since_last_run)
    all_candidates = []
    successful_fetches = 0
    failed_fetches = 0
    source_stats: dict[str, dict[str, Any]] = {}
    source_delay_seconds = float(os.getenv("SOURCE_DELAY_SECONDS", "3"))
    for source in sources:
        source_stats[source.name] = {"type": source.type, "successful_fetches": 0, "failed_fetches": 0}
        if not source.enabled:
            continue
        if is_feed_source(source):
            print(f"Fetching feed source: {source.name}", flush=True)
            try:
                feed_papers = fetch_feed(source, max_per_topic * max(1, len(topics)))
                all_candidates.extend(feed_papers)
                successful_fetches += 1
                source_stats[source.name]["successful_fetches"] += 1
            except Exception as exc:
                failed_fetches += 1
                source_stats[source.name]["failed_fetches"] += 1
                source_stats[source.name]["last_error"] = str(exc)
                print(f"Warning: feed source failed for {source.name}: {exc}", file=sys.stderr)
            time.sleep(source_delay_seconds)
            continue

        for index, topic in enumerate(topics):
            if index:
                if source.type == "arxiv":
                    time.sleep(float(os.getenv("ARXIV_DELAY_SECONDS", "15")))
                else:
                    time.sleep(source_delay_seconds)
            print(f"Fetching {source.name} papers for topic: {topic.name}", flush=True)
            try:
                topic_papers = fetch_source_topic(source, topic, max_per_topic)
                all_candidates.extend(topic_papers)
                successful_fetches += 1
                source_stats[source.name]["successful_fetches"] += 1
            except Exception as exc:
                failed_fetches += 1
                source_stats[source.name]["failed_fetches"] += 1
                source_stats[source.name]["last_error"] = str(exc)
                print(f"Warning: {source.name} request failed for {topic.name}: {exc}", file=sys.stderr)
                if source.type == "arxiv" and should_stop_arxiv_fetches(exc):
                    skipped = len(topics) - index - 1
                    failed_fetches += skipped
                    source_stats[source.name]["failed_fetches"] += skipped
                    if skipped:
                        print(
                            f"Stopping arXiv fetches after {exc}; skipped {skipped} remaining topic(s) to avoid further throttling.",
                            file=sys.stderr,
                        )
                    break

    if successful_fetches == 0 and failed_fetches > 0 and existing_payload.get("papers"):
        print("All configured sources failed; preserving existing paper data.", file=sys.stderr)

    recent_papers = []
    daily_backfill_candidates = []
    filtered_low_relevance = 0
    filtered_wrong_category = 0
    filtered_disliked = 0
    raw_daily_candidate_count = 0
    daily_outside_cutoff_count = 0
    newly_discovered_backfill_count = 0
    backfill_days = max(days, env_int("DAILY_BACKFILL_DAYS", 14))
    daily_backfill_cutoff = now - dt.timedelta(days=max(0, backfill_days))
    for paper in dedupe_papers(all_candidates):
        raw_daily_candidate_count += 1
        activity_at = paper_activity_datetime(paper)
        in_primary_window = activity_at >= cutoff
        in_backfill_window = not in_primary_window and activity_at >= daily_backfill_cutoff
        if not in_primary_window and not in_backfill_window:
            daily_outside_cutoff_count += 1
            continue

        key = paper_key(paper)
        if key and key in disliked_set and key not in liked_set:
            filtered_disliked += 1
            continue

        if arxiv_category_filter:
            paper_cats = set(paper.get("categories", []))
            if not paper_cats & arxiv_category_filter:
                filtered_wrong_category += 1
                continue

        matches = [
            score_paper(
                topic,
                paper,
                preferences=preferences,
                now=now,
                negative_terms=negative_terms,
            )
            for topic in topics
        ]
        matches.sort(key=lambda item: item["score"], reverse=True)
        best_match = matches[0]
        if not is_relevant_enough(paper, best_match):
            filtered_low_relevance += 1
            continue
        paper["matches"] = matches
        paper["best_match"] = best_match
        strong_label_score = env_float("TOPIC_LABEL_STRONG_SCORE", 0.18)
        weak_label_score = env_float("TOPIC_LABEL_WEAK_SCORE", 0.10)
        top_labels = []
        for m in matches:
            base = m.get("base_score", 0.0)
            has_hit = bool(m.get("keyword_hits", []))
            if base >= strong_label_score or (base >= weak_label_score and has_hit):
                top_labels.append({
                    "topic_id": m["topic_id"],
                    "topic_name": m["topic_name"],
                    "base_score": base,
                    "score": m["score"],
                    "level": m["level"],
                    "keyword_hits": m.get("keyword_hits", []),
                })
        paper["top_labels"] = top_labels[:5]
        if in_backfill_window:
            paper["backfilled_from_recent_arxiv"] = True
            daily_outside_cutoff_count += 1
            if str(paper.get("id", "")) not in existing_paper_ids:
                paper["newly_discovered_from_recent_arxiv"] = True
                recent_papers.append(paper)
                newly_discovered_backfill_count += 1
            else:
                daily_backfill_candidates.append(paper)
        else:
            recent_papers.append(paper)

    recent_papers.sort(key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)), reverse=True)
    daily_recent_papers = list(recent_papers)
    daily_backfill_added_count = 0
    min_daily_papers = max(0, env_int("MIN_DAILY_PAPERS", 8))
    if len(daily_recent_papers) < min_daily_papers and daily_backfill_candidates:
        daily_backfill_candidates.sort(
            key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
            reverse=True,
        )
        existing_daily_ids = {str(paper.get("id", "")) for paper in daily_recent_papers}
        for paper in daily_backfill_candidates:
            if len(daily_recent_papers) >= min_daily_papers:
                break
            paper_id = str(paper.get("id", ""))
            if paper_id in existing_daily_ids:
                continue
            existing_daily_ids.add(paper_id)
            daily_recent_papers.append(paper)
            daily_backfill_added_count += 1
    candidate_paper_count = len(daily_recent_papers)
    daily_candidate_paper_count = len(daily_recent_papers)
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    fulltext_stats = {"fulltext_attempted_count": 0, "fulltext_enriched_count": 0, "fulltext_failed_count": 0}
    llm_rerank_pool_size = 0
    llm_rerank_enabled = env_flag("LLM_RERANK_BEFORE_SELECTION", False) and llm_enabled() and max_summaries > 0
    if llm_rerank_enabled and daily_recent_papers:
        llm_rerank_pool_size = min(
            len(daily_recent_papers),
            max(0, env_int("LLM_RERANK_POOL", max_summaries)),
        )
        if llm_rerank_pool_size:
            rerank_pool = sorted(
                daily_recent_papers,
                key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
                reverse=True,
            )[:llm_rerank_pool_size]
            add_count_stats(fulltext_stats, enrich_arxiv_fulltext(rerank_pool))
            summaries_by_id.update(summarize_papers(build_summary_jobs(topics_by_id, rerank_pool)))
            apply_adjusted_matches(daily_recent_papers, summaries_by_id)
            daily_recent_papers.sort(
                key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
                reverse=True,
            )

    if max_new_papers > 0:
        daily_recent_papers = select_diverse_papers(daily_recent_papers, max_new_papers)
    recent_papers = sorted(
        daily_recent_papers,
        key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
        reverse=True,
    )
    add_count_stats(fulltext_stats, enrich_arxiv_fulltext(recent_papers[:max_summaries]))
    pending_summary_papers = [
        paper
        for paper in recent_papers[:max_summaries]
        if str(paper.get("id", "")) not in summaries_by_id
    ]
    summaries_by_id.update(summarize_papers(build_summary_jobs(topics_by_id, pending_summary_papers)))

    for index, paper in enumerate(recent_papers):
        paper_id = str(paper.get("id", ""))
        if index < max_summaries and paper_id in summaries_by_id:
            summary, adjusted_match = summaries_by_id[paper_id]
            paper["chinese_summary"] = summary
            paper["best_match"] = adjusted_match
            paper["matches"] = [adjusted_match if m["topic_id"] == adjusted_match["topic_id"] else m for m in paper["matches"]]
        else:
            paper["chinese_summary"] = fallback_summary(paper, paper["best_match"])

    daily_merged_papers, daily_retention_stats = merge_with_retained_papers(
        recent_papers,
        existing_payload,
        now,
        recent_history_days,
        liked_set=liked_set,
        disliked_set=disliked_set,
    )
    daily_merged_papers.sort(key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)), reverse=True)

    base_stats = {
        "candidate_paper_count": candidate_paper_count,
        "daily_candidate_paper_count": daily_candidate_paper_count,
        "raw_daily_candidate_count": raw_daily_candidate_count,
        "daily_outside_cutoff_count": daily_outside_cutoff_count,
        "newly_discovered_backfill_count": newly_discovered_backfill_count,
        "daily_backfill_days": backfill_days,
        "daily_backfill_candidate_count": len(daily_backfill_candidates),
        "daily_backfill_added_count": daily_backfill_added_count,
        "min_daily_papers": min_daily_papers,
        "filtered_low_relevance_count": filtered_low_relevance,
        "filtered_wrong_category_count": filtered_wrong_category,
        "filtered_disliked_count": filtered_disliked,
        "arxiv_category_filter": list(arxiv_category_filter) if arxiv_category_filter else [],
        "days": days,
        "collection_mode": collection_mode,
        "collection_cutoff_iso": cutoff.isoformat(),
        "max_per_topic": max_per_topic,
        "max_new_papers": max_new_papers,
        "sources": [source.__dict__ for source in sources],
        "source_stats": source_stats,
        "llm_enabled": llm_enabled(),
        "llm_concurrency": int(os.getenv("LLM_CONCURRENCY", "2")),
        "llm_rerank_before_selection": llm_rerank_enabled,
        "llm_rerank_pool_size": llm_rerank_pool_size,
        **fulltext_stats,
        "preferences_active": bool(preferences),
        "preference_liked_count": int(preferences.get("liked_count") or 0),
        "disliked_count": len(disliked_set),
        "recent_history_days": recent_history_days,
        "successful_fetches": successful_fetches,
        "failed_fetches": failed_fetches,
        "clear_cache": clear_cache,
    }

    payload = {
        "generated_at": email.utils.format_datetime(now),
        "generated_at_iso": now.isoformat(),
        "config_source": "issue" if config is not default_config else "file",
        "topics": [topic.__dict__ for topic in topics],
        "papers": daily_merged_papers,
        "stats": {
            **base_stats,
            "paper_count": len(daily_merged_papers),
            "new_paper_count": len(daily_recent_papers),
            **daily_retention_stats,
        },
    }
    trimmed_papers, storage_stats = trim_papers_for_storage(payload, max_stored_papers, max_data_bytes, liked_set)
    trimmed_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    payload["papers"] = trimmed_papers
    payload["stats"].update(storage_stats)
    payload["stats"]["paper_count"] = len(trimmed_papers)
    payload["stats"]["data_bytes"] = json_size_bytes(payload)
    write_json(output_path, payload)

    # Learn preferences from liked papers
    if liked_set:
        liked_papers_list = [p for p in trimmed_papers if paper_key(p) in liked_set]
        if len(liked_papers_list) >= 3:
            prefs = learn_preferences(liked_papers_list, trimmed_papers)
            save_preferences(prefs, preferences_path)
        else:
            print(f"Not enough liked papers for preference learning ({len(liked_papers_list)} < 3)", flush=True)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect papers and build static data for paper-daily.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    parser.add_argument("--max-per-topic", type=int, default=int(os.getenv("MAX_PER_TOPIC", "25")))
    parser.add_argument("--max-summaries", type=int, default=int(os.getenv("MAX_SUMMARIES", "40")))
    parser.add_argument("--max-new-papers", type=int, default=int(os.getenv("MAX_NEW_PAPERS", str(DEFAULT_MAX_NEW_PAPERS))))
    parser.add_argument("--max-stored-papers", type=int, default=int(os.getenv("MAX_STORED_PAPERS", str(DEFAULT_MAX_STORED_PAPERS))))
    parser.add_argument("--max-data-bytes", type=int, default=int(os.getenv("MAX_DATA_BYTES", str(DEFAULT_MAX_DATA_BYTES))))
    parser.add_argument("--incremental-since-last-run", action="store_true", default=env_flag("INCREMENTAL_SINCE_LAST_RUN"))
    parser.add_argument("--recent-history-days", type=int, default=int(os.getenv("RECENT_HISTORY_DAYS", str(DEFAULT_RECENT_HISTORY_DAYS))))
    parser.add_argument("--clear-cache", action="store_true", default=env_flag("CLEAR_PAPER_CACHE"))
    args = parser.parse_args()
    payload = collect(
        args.config,
        args.output,
        args.days,
        args.max_per_topic,
        args.max_summaries,
        args.max_new_papers,
        args.max_stored_papers,
        args.max_data_bytes,
        args.incremental_since_last_run,
        args.recent_history_days,
        args.clear_cache,
    )
    print(f"Wrote {len(payload['papers'])} daily papers to {args.output}")
    stats = payload.get("stats", {})
    print(
        "Daily arXiv stats: "
        f"raw={stats.get('raw_daily_candidate_count', 0)}, "
        f"selected={stats.get('daily_candidate_paper_count', 0)}, "
        f"backfilled={stats.get('daily_backfill_added_count', 0)}, "
        f"filtered={stats.get('filtered_low_relevance_count', 0)}"
    )
    if stats.get("arxiv_category_filter"):
        print(
            "Category filter: "
            f"allowed={stats.get('arxiv_category_filter')}, "
            f"dropped={stats.get('filtered_wrong_category_count', 0)}"
        )


if __name__ == "__main__":
    main()
