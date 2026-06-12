import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from scripts.collect_papers import (
    SourceConfig,
    Topic,
    apply_negative_penalty,
    arxiv_query_for_topic,
    arxiv_retry_wait_seconds,
    category_score,
    collect,
    collection_cutoff,
    has_meaningful_summary,
    is_relevant_enough,
    keyword_score,
    learn_preferences,
    load_preferences,
    merge_config,
    merge_with_retained_papers,
    openalex_abstract_text,
    parse_arxiv_entries,
    parse_negative_terms,
    parse_sources,
    preference_boost_score,
    score_paper,
    select_diverse_papers,
    should_retry_arxiv_error,
    should_summarize_paper_with_llm,
    source_request_headers,
    title_similarity,
    trim_papers_for_storage,
)


UTC = dt.timezone.utc


def make_paper(
    paper_id: str,
    title: str,
    summary: str = "",
    category: str = "math.AG",
    published: str = "2026-06-10T00:00:00+00:00",
    topic_id: str = "algebraic_geometry",
    score: float = 0.5,
    level: str = "medium",
) -> dict:
    return {
        "id": paper_id,
        "source": "arXiv",
        "title": title,
        "authors": ["Ada Example"],
        "summary": summary,
        "published": published,
        "updated": published,
        "paper_url": f"https://arxiv.org/abs/{paper_id}",
        "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        "categories": [category] if category else [],
        "best_match": {
            "topic_id": topic_id,
            "topic_name": topic_id,
            "score": score,
            "base_score": score,
            "level": level,
            "reason": "test",
            "keyword_hits": [],
        },
        "matches": [],
        "chinese_summary": {},
    }


class CollectorTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "ARXIV_RETRY_MIN_SECONDS",
            "ARXIV_RETRY_BASE_SECONDS",
            "ARXIV_RETRY_MAX_SECONDS",
            "ARXIV_RETRY_THROTTLED",
            "CUSTOM_FEED_HEADERS",
            "CUSTOM_FEED_BEARER_TOKEN",
            "ENABLE_SEMANTIC_SCHOLAR",
            "ARXIV_QUERY_MODE",
            "MIN_DAILY_PAPERS",
            "DAILY_BACKFILL_DAYS",
            "MIN_KEYWORD_MATCH_SCORE",
            "MIN_TITLE_ONLY_SCORE",
            "MIN_PAPER_SCORE",
        ):
            os.environ.pop(name, None)

    def test_arxiv_retry_wait_uses_retry_after_and_cap(self) -> None:
        os.environ["ARXIV_RETRY_MIN_SECONDS"] = "30"
        error = urllib.error.HTTPError("url", 429, "limited", {"Retry-After": "75"}, None)
        self.assertEqual(arxiv_retry_wait_seconds(error, 0), 75.0)

        os.environ["ARXIV_RETRY_MIN_SECONDS"] = "5"
        os.environ["ARXIV_RETRY_BASE_SECONDS"] = "10"
        os.environ["ARXIV_RETRY_MAX_SECONDS"] = "25"
        self.assertEqual(arxiv_retry_wait_seconds(TimeoutError(), 2), 25.0)

    def test_throttled_arxiv_fast_fails_unless_enabled(self) -> None:
        error = urllib.error.HTTPError("url", 429, "limited", {}, None)
        self.assertFalse(should_retry_arxiv_error(error))
        os.environ["ARXIV_RETRY_THROTTLED"] = "true"
        self.assertTrue(should_retry_arxiv_error(error))

    def test_sources_support_feed_headers_and_semantic_opt_in(self) -> None:
        sources = parse_sources(
            {
                "sources": [
                    "arxiv",
                    {"type": "feed", "name": "Private", "url": "https://example.com/feed"},
                    "semantic_scholar",
                ]
            }
        )
        self.assertEqual([source.type for source in sources], ["arxiv", "feed"])

        os.environ["ENABLE_SEMANTIC_SCHOLAR"] = "true"
        sources = parse_sources({"sources": ["arxiv", "semantic_scholar"]})
        self.assertEqual([source.type for source in sources], ["arxiv", "semantic_scholar"])

        os.environ["CUSTOM_FEED_HEADERS"] = '{"X-API-Key": "secret"}'
        os.environ["CUSTOM_FEED_BEARER_TOKEN"] = "token"
        headers = source_request_headers(
            SourceConfig(
                type="feed",
                name="Private",
                headers_env="CUSTOM_FEED_HEADERS",
                bearer_token_env="CUSTOM_FEED_BEARER_TOKEN",
            )
        )
        self.assertEqual(headers["X-API-Key"], "secret")
        self.assertEqual(headers["Authorization"], "Bearer token")

    def test_negative_terms_are_configurable_and_invalid_values_are_ignored(self) -> None:
        terms = parse_negative_terms(
            {"negative_terms": {"machine learning": 0.25, "bad": "nope", "clamped": 2}}
        )
        self.assertEqual(terms, {"machine learning": 0.25, "clamped": 1.0})

        merged = merge_config(
            {"topics": [{"name": "Math"}], "negative_terms": {"machine learning": 0.25}},
            {"topics": [{"name": "LLM"}]},
        )
        self.assertEqual(merged["negative_terms"], {})

    def test_arxiv_query_modes_and_parser(self) -> None:
        topic = Topic("motivic", "Motivic", "", ["motivic spectra"], ["math.AG"])
        self.assertEqual(arxiv_query_for_topic(topic), '(all:"motivic spectra")')

        os.environ["ARXIV_QUERY_MODE"] = "strict"
        self.assertIn(" AND ", arxiv_query_for_topic(topic))

        xml = b"""
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2601.00001</id>
            <title>Motivic Spectra</title>
            <summary>A sufficiently long abstract about motivic spectra and algebraic geometry.</summary>
            <published>2026-01-01T00:00:00Z</published>
            <updated>2026-01-02T00:00:00Z</updated>
            <author><name>Ada Example</name></author>
            <category term="math.AG" />
            <link title="pdf" href="https://arxiv.org/pdf/2601.00001" />
          </entry>
        </feed>
        """
        paper = parse_arxiv_entries(xml, seed_topic="motivic")[0]
        self.assertEqual(paper["id"], "2601.00001")
        self.assertEqual(paper["seed_topic"], "motivic")
        self.assertEqual(paper["authors"], ["Ada Example"])

    def test_openalex_abstract_reconstruction(self) -> None:
        abstract = openalex_abstract_text(
            {"abstract_inverted_index": {"motivic": [1], "Fast": [0], "spectra": [2]}}
        )
        self.assertEqual(abstract, "Fast motivic spectra")

    def test_keyword_scoring_deduplicates_nested_phrases_and_favors_title(self) -> None:
        topic = Topic(
            "motivic",
            "Motivic",
            "",
            ["motivic homotopy theory", "homotopy theory"],
            [],
        )
        title_paper = make_paper("1", "Motivic homotopy theory", category="")
        summary_paper = make_paper(
            "2",
            "A general framework",
            summary="We develop motivic homotopy theory for schemes.",
            category="",
        )

        title_score, title_hits = keyword_score(topic, title_paper)
        summary_score, summary_hits = keyword_score(topic, summary_paper)

        self.assertEqual(title_hits, ["motivic homotopy theory"])
        self.assertEqual(summary_hits, ["motivic homotopy theory"])
        self.assertGreater(title_score, summary_score)

    def test_category_scoring_distinguishes_primary_and_secondary(self) -> None:
        topic = Topic("ag", "AG", "", [], ["math.AG"])
        primary = {"categories": ["math.AG", "math.AT"]}
        secondary = {"categories": ["math.AT", "math.AG"]}
        self.assertEqual(category_score(topic, primary), 1.0)
        self.assertEqual(category_score(topic, secondary), 0.65)

    def test_preference_learning_uses_document_frequency_and_boosts_matches(self) -> None:
        liked = [
            make_paper("1", "Motivic spectra over fields", "motivic spectra and realization functors"),
            make_paper("2", "Computing motivic spectra", "new tools for motivic spectra"),
            make_paper("3", "Descent for motivic spectra", "descent methods for motivic spectra"),
        ]
        corpus = liked + [
            make_paper("4", "Random matrix methods", "statistics and random matrices"),
            make_paper("5", "Elliptic curves", "arithmetic geometry"),
        ]

        preferences = learn_preferences(liked, corpus)
        self.assertIn("motivic spectra", preferences["keyword_boosts"])

        bonus, reasons = preference_boost_score(
            make_paper("6", "A survey of motivic spectra"),
            preferences,
        )
        self.assertGreater(bonus, 0)
        self.assertTrue(any("偏好词" in reason for reason in reasons))

    def test_load_preferences_tolerates_bad_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preferences.json"
            self.assertEqual(load_preferences(path), {})
            path.write_text("{bad", encoding="utf-8")
            self.assertEqual(load_preferences(path), {})

    def test_score_has_smooth_recency_and_bounded_bridge(self) -> None:
        topic = Topic(
            "motivic_k_theory",
            "Motivic K",
            "motivic spectra and algebraic K-theory",
            ["motivic spectra", "algebraic K-theory"],
            ["math.AG"],
        )
        now = dt.datetime(2026, 6, 12, tzinfo=UTC)
        recent = make_paper(
            "1",
            "Motivic spectra and algebraic K-theory",
            "We relate motivic spectra to algebraic K-theory and stable homotopy.",
            published="2026-06-12T00:00:00+00:00",
        )
        old = dict(recent, id="2", published="2025-06-12T00:00:00+00:00", updated="2025-06-12T00:00:00+00:00")

        recent_match = score_paper(topic, recent, now=now)
        old_match = score_paper(topic, old, now=now)

        self.assertGreater(recent_match["recency_bonus"], old_match["recency_bonus"])
        self.assertLessEqual(recent_match["bridge_bonus"], 0.08)
        self.assertGreater(recent_match["score"], old_match["score"])

    def test_negative_penalty_is_bounded_for_strong_positive_signal(self) -> None:
        raw = 0.8
        weak = apply_negative_penalty("machine learning", raw, positive_signal=0.0)
        strong = apply_negative_penalty("machine learning", raw, positive_signal=1.0)
        self.assertGreater(weak, 0.5)
        self.assertGreater(strong, weak)
        self.assertLess(strong, raw)

    def test_relevance_requires_minimum_score_even_with_keyword_hit(self) -> None:
        paper = make_paper("1", "Scheme", summary="")
        self.assertFalse(is_relevant_enough(paper, {"score": 0.05, "keyword_hits": ["scheme"]}))
        self.assertTrue(is_relevant_enough(paper, {"score": 0.20, "keyword_hits": ["scheme"]}))
        self.assertFalse(is_relevant_enough(paper, {"score": 0.19, "keyword_hits": []}))

    def test_title_only_papers_skip_llm_by_default(self) -> None:
        self.assertFalse(should_summarize_paper_with_llm({"summary": ""}))
        self.assertTrue(should_summarize_paper_with_llm({"summary": "x" * 100}))
        self.assertFalse(has_meaningful_summary({"summary": "short"}))

    def test_diverse_selection_penalizes_repeated_topics_and_titles(self) -> None:
        papers = [
            make_paper("a1", "Motivic spectra over fields", topic_id="a", score=0.90, level="high"),
            make_paper("a2", "Motivic spectra over a field", topic_id="a", score=0.88, level="high"),
            make_paper("b1", "Galois representations and periods", topic_id="b", score=0.84, level="high"),
        ]
        self.assertGreaterEqual(title_similarity(papers[0], papers[1]), 0.5)
        selected = select_diverse_papers(papers, 2)
        self.assertEqual([paper["id"] for paper in selected], ["a1", "b1"])

    def test_merge_retains_relevant_recent_and_liked_papers(self) -> None:
        now = dt.datetime(2026, 6, 12, tzinfo=UTC)
        high = make_paper("high", "High", level="high", score=0.8)
        recent_low = make_paper("recent", "Recent", level="low", score=0.2)
        recent_low["first_seen_at"] = "2026-06-10T00:00:00+00:00"
        liked_old = make_paper(
            "liked",
            "Liked",
            level="low",
            score=0.2,
            published="2025-01-01T00:00:00+00:00",
        )
        liked_old["first_seen_at"] = "2025-01-01T00:00:00+00:00"
        stale = make_paper(
            "stale",
            "Stale",
            level="low",
            score=0.2,
            published="2025-01-01T00:00:00+00:00",
        )
        stale["first_seen_at"] = "2025-01-01T00:00:00+00:00"
        existing = {
            "generated_at_iso": "2026-06-11T00:00:00+00:00",
            "papers": [high, recent_low, liked_old, stale],
        }

        merged, stats = merge_with_retained_papers(
            [],
            existing,
            now,
            recent_history_days=30,
            liked_set={"liked"},
        )
        self.assertEqual({paper["id"] for paper in merged}, {"high", "recent", "liked"})
        self.assertEqual(stats["dropped_low_relevance_count"], 1)

    def test_storage_trim_never_removes_liked_papers(self) -> None:
        payload = {
            "papers": [
                make_paper("liked", "Liked", level="low", score=0.1),
                make_paper("low", "Low", level="low", score=0.2),
                make_paper("high", "High", level="high", score=0.8),
            ],
            "stats": {},
        }
        trimmed, stats = trim_papers_for_storage(
            payload,
            max_stored_papers=1,
            max_data_bytes=0,
            liked_set={"liked"},
        )
        self.assertEqual([paper["id"] for paper in trimmed], ["liked"])
        self.assertFalse(stats["storage_limit_exceeded_by_likes"])

        payload["papers"] = [
            make_paper("liked-1", "Liked 1"),
            make_paper("liked-2", "Liked 2"),
        ]
        trimmed, stats = trim_papers_for_storage(
            payload,
            max_stored_papers=1,
            max_data_bytes=0,
            liked_set={"liked-1", "liked-2"},
        )
        self.assertEqual(len(trimmed), 2)
        self.assertTrue(stats["storage_limit_exceeded_by_likes"])

    def test_collection_cutoff_uses_previous_run_in_incremental_mode(self) -> None:
        now = dt.datetime(2026, 6, 12, 1, tzinfo=UTC)
        cutoff, mode = collection_cutoff(
            {"generated_at_iso": "2026-06-11T01:00:00+00:00"},
            now,
            days=7,
            incremental_since_last_run=True,
        )
        self.assertEqual(mode, "incremental")
        self.assertEqual(cutoff, dt.datetime(2026, 6, 11, 1, tzinfo=UTC))

    def test_collect_backfills_recent_papers_and_uses_current_signature(self) -> None:
        now = dt.datetime.now(UTC)
        config = {
            "sources": [{"type": "arxiv", "name": "arXiv"}],
            "topics": [
                {
                    "id": "motivic",
                    "name": "Motivic",
                    "description": "motivic spectra",
                    "keywords": ["motivic spectra"],
                    "arxiv_categories": ["math.AG"],
                }
            ],
        }
        fetched = make_paper(
            "2601.00001",
            "Motivic spectra over fields",
            summary="This paper develops motivic spectra over fields with new descent tools. " * 2,
            published=(now - dt.timedelta(days=3)).isoformat(),
        )
        os.environ["MIN_DAILY_PAPERS"] = "1"
        os.environ["DAILY_BACKFILL_DAYS"] = "14"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "interests.json"
            output_path = root / "data" / "papers.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with (
                mock.patch("scripts.collect_papers.fetch_source_topic", return_value=[fetched]),
                mock.patch("scripts.collect_papers.time.sleep"),
            ):
                payload = collect(
                    config_path=config_path,
                    output_path=output_path,
                    days=1,
                    max_per_topic=1,
                    max_summaries=0,
                    max_new_papers=10,
                    max_stored_papers=10,
                    max_data_bytes=0,
                    incremental_since_last_run=False,
                    recent_history_days=45,
                    clear_cache=True,
                )

        self.assertEqual(payload["stats"]["daily_backfill_added_count"], 1)
        self.assertTrue(payload["papers"][0]["backfilled_from_recent_arxiv"])


if __name__ == "__main__":
    unittest.main()
