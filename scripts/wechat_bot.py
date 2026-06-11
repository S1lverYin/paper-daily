#!/usr/bin/env python3
"""Post daily paper digests to an Enterprise WeChat (企业微信) group bot.

Requires a webhook URL set via WECHAT_WEBHOOK_URL environment variable.
Each message covers one topic: title, top N papers with scores, and links.

Usage:
  WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx \
    python3 scripts/wechat_bot.py

Options (env vars):
  WECHAT_WEBHOOK_URL      – required; the bot webhook URL
  WECHAT_TOP_N             – papers per topic (default: 5)
  WECHAT_MIN_SCORE         – minimum base_score to include (default: 0.15)
  WECHAT_DRY_RUN           – if "1"/"true", print messages without sending
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "web" / "data" / "papers.json"


def env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "")
    return val.strip().lower() in {"1", "true", "yes"} if val else default


def load_papers(path: Path) -> tuple[list[dict], list[dict]]:
    """Return (papers, topics)."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("papers", []), data.get("topics", [])


def group_by_topic(papers: list[dict], topics: list[dict], top_n: int, min_score: float):
    """Group papers by their best-match topic, sorted by base_score desc."""
    topic_map = {t["id"]: t for t in topics}
    buckets: dict[str, list[dict]] = {t["id"]: [] for t in topics}

    for paper in papers:
        bm = paper.get("best_match", {})
        tid = bm.get("topic_id", "")
        base = bm.get("base_score", bm.get("score", 0))
        if tid not in buckets:
            continue
        if base < min_score:
            continue
        buckets[tid].append((base, paper))

    result = {}
    for tid, items in buckets.items():
        items.sort(key=lambda x: x[0], reverse=True)
        result[tid] = [p for _, p in items[:top_n]]
    return result


def format_message(topic: dict, papers: list[dict]) -> str:
    """Format a single-topic digest as Enterprise WeChat Markdown.

    WeChat bot Markdown supports:
      # H1–H6, **bold**, [text](url), > quote, `code`
      NOT: tables, images, HTML
    """
    name = topic["name"]
    desc = topic.get("description", "")
    lines = [f"## {name}", f"> {desc}", ""]

    today = papers[0].get("last_seen_at", "")[:10] if papers else ""
    if today:
        lines.append(f"**{today}** · 共 {len(papers)} 篇")
        lines.append("")

    for i, paper in enumerate(papers, 1):
        bm = paper.get("best_match", {})
        score = bm.get("base_score", bm.get("score", 0))
        level = bm.get("level", "low")
        emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(level, "⚪")
        level_cn = {"high": "高", "medium": "中", "low": "低"}.get(level, "低")

        title = paper.get("title", "Untitled")
        url = paper.get("paper_url") or paper.get("pdf_url") or ""
        if url and url.startswith("http"):
            title_line = f"[{title}]({url})"
        else:
            title_line = title

        authors = ", ".join(paper.get("authors", [])[:3])
        if authors:
            authors = f" — *{authors}*"

        reason = bm.get("reason", "")

        lines.append(f"{emoji} **{i}.** {title_line}{authors}")
        lines.append(f"> 匹配度 {level_cn} `{score:.2f}` · {reason[:120]}")
        lines.append("")

    lines.append("---")
    lines.append("🤖 [Paper Daily](https://github.com/Futuresxy/paper-daily)")
    return "\n".join(lines)


def split_long_message(msg: str, max_chars: int = 3800) -> list[str]:
    """Split a message at paragraph boundaries to stay under WeChat's limit."""
    if len(msg) <= max_chars:
        return [msg]
    chunks = []
    current = ""
    for line in msg.split("\n"):
        if len(current) + len(line) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def post_to_wechat(webhook_url: str, content: str) -> bool:
    """Post a markdown message to a WeChat group bot webhook.

    Returns True on success.
    """
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Webhook post failed: {exc}", file=sys.stderr)
        return False

    if result.get("errcode") != 0:
        print(f"WeChat API error: {result}", file=sys.stderr)
        return False
    return True


def main() -> None:
    webhook_url = os.getenv("WECHAT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("Error: WECHAT_WEBHOOK_URL is required.", file=sys.stderr)
        print(
            "Get one from: 企业微信群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址",
            file=sys.stderr,
        )
        sys.exit(1)

    top_n = int(os.getenv("WECHAT_TOP_N", "5"))
    min_score = float(os.getenv("WECHAT_MIN_SCORE", "0.15"))
    dry_run = env_flag("WECHAT_DRY_RUN", False)

    data_path = Path(os.getenv("PAPER_DATA_PATH", str(DEFAULT_DATA)))
    papers, topics = load_papers(data_path)
    if not papers:
        print("No papers found. Run collect_papers.py first.", file=sys.stderr)
        sys.exit(1)

    buckets = group_by_topic(papers, topics, top_n, min_score)

    # Order topics by total paper count
    topic_order = sorted(
        buckets.keys(),
        key=lambda tid: len(buckets[tid]),
        reverse=True,
    )

    total_sent = 0
    for tid in topic_order:
        ps = buckets[tid]
        if not ps:
            continue
        topic = next((t for t in topics if t["id"] == tid), {"name": tid, "description": ""})
        msg = format_message(topic, ps)
        chunks = split_long_message(msg)

        for ci, chunk in enumerate(chunks):
            if dry_run:
                print(f"[DRY RUN] To: {topic['name']} (chunk {ci+1}/{len(chunks)})")
                print(chunk[:500])
                print("...\n")
            else:
                ok = post_to_wechat(webhook_url, chunk)
                if ok:
                    print(f"Sent: {topic['name']} (chunk {ci+1}/{len(chunks)})")
                    total_sent += 1
                else:
                    print(f"Failed: {topic['name']} (chunk {ci+1}/{len(chunks)})", file=sys.stderr)

    print(f"\nDone. Posted {total_sent} messages for {len(topic_order)} topics.")


if __name__ == "__main__":
    main()
