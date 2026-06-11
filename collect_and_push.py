#!/usr/bin/env python3
"""Paper Daily - Collect papers and push to WeChat group bot.

Prerequisite: Set WECHAT_WEBHOOK_URL to your 企业微信群机器人 webhook.

Get the webhook:
  企业微信群 → 群设置（右上角...）→ 群机器人 → 添加 → 复制 Webhook 地址

Usage:
  WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx \
    python3 collect_and_push.py
"""

import json
import sys
import os
import urllib.request
import subprocess
import datetime
import logging

PROJECT = "/Users/silver/Documents/paper-daily"
LOG_DIR = os.path.join(PROJECT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, f"collect-{datetime.date.today().isoformat()}.log"),
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger("paper-daily")


def post_markdown(webhook_url: str, content: str) -> bool:
    """Post a markdown message to 企业微信群机器人 webhook."""
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
        logger.error(f"Webhook post failed: {exc}")
        return False

    if result.get("errcode") != 0:
        logger.error(f"WeChat API error: {result}")
        return False
    return True


def split_long(msg: str, max_chars: int = 3800) -> list[str]:
    """Split a message at line boundaries to stay under limit."""
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


def format_topic_digest(topic: dict, papers: list[dict], top_n: int, min_score: float) -> str | None:
    """Format one topic's daily digest as WeChat Markdown.

    Returns None if no qualifying papers.
    """
    # Filter: base_score >= min_score, then sort by base_score desc
    qualified = []
    for p in papers:
        bm = p.get("best_match", {})
        base = bm.get("base_score", bm.get("score", 0))
        if base >= min_score:
            qualified.append((base, p))
    qualified.sort(key=lambda x: x[0], reverse=True)
    papers = [p for _, p in qualified[:top_n]]
    if not papers:
        return None

    emoji_map = {"high": "🔴", "medium": "🟡", "low": "⚪"}
    level_cn = {"high": "高", "medium": "中", "low": "低"}

    name = topic["name"]
    desc = topic.get("description", "")
    lines = [f"## {name}", f"> {desc}", ""]

    for i, paper in enumerate(papers, 1):
        bm = paper.get("best_match", {})
        base = bm.get("base_score", bm.get("score", 0))
        level = bm.get("level", "low")
        emoji = emoji_map.get(level, "⚪")

        title = paper.get("title", "Untitled")
        url = paper.get("paper_url") or paper.get("pdf_url") or ""
        title_line = f"[{title}]({url})" if url.startswith("http") else title

        authors = ", ".join(paper.get("authors", [])[:3])
        author_line = f" — *{authors}*" if authors else ""

        reason = bm.get("reason", "")
        # Truncate reason to fit WeChat markdown nicely
        reason_short = reason[:140] + "…" if len(reason) > 140 else reason

        lines.append(f"{emoji} **{i}.** {title_line}{author_line}")
        lines.append(f"> `{base:.2f}` · {reason_short}")
        lines.append("")

    lines.append("---")
    lines.append("🤖 [Paper Daily](https://github.com/Futuresxy/paper-daily)")
    return "\n".join(lines)


def main():
    os.chdir(PROJECT)

    # ── Config ──────────────────────────────────────────────
    webhook_url = os.getenv("WECHAT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.error("WECHAT_WEBHOOK_URL not set. "
                      "Get one from: 企业微信群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址")
        return

    top_n = int(os.getenv("WECHAT_TOP_N", "5"))
    min_score = float(os.getenv("WECHAT_MIN_SCORE", "0.15"))

    # ── Step 1: Collect papers ──────────────────────────────
    os.environ["DEEPSEEK_API_KEY"] = "sk-b4b3ec200f734f1e9138d23fcee9541b"

    logger.info("Collecting papers...")
    result = subprocess.run(
        [
            sys.executable, "scripts/collect_papers.py",
            "--days", "1", "--max-per-topic", "25",
            "--max-summaries", "40", "--max-new-papers", "50",
            "--max-stored-papers", "200",
        ],
        capture_output=True, text=True, timeout=600,
    )
    logger.info(result.stdout)
    if result.stderr:
        stderr_lines = result.stderr.strip().split("\n")
        for line in stderr_lines:
            logger.error(line)

    # ── Step 2: Load results ────────────────────────────────
    data_path = os.path.join(PROJECT, "web", "data", "papers.json")
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    papers = data.get("papers", [])
    topics = data.get("topics", [])
    stats = data.get("stats", {})

    if not papers:
        logger.info("No papers found. Skipping push.")
        return

    # ── Step 3: Group by topic and post ─────────────────────
    # Partition papers by best_match topic
    buckets: dict[str, list[dict]] = {t["id"]: [] for t in topics}
    for p in papers:
        tid = p.get("best_match", {}).get("topic_id", "")
        if tid in buckets:
            buckets[tid].append(p)

    today = datetime.date.today().isoformat()
    total_sent = 0

    # Send in topic order: most papers first
    topic_order = sorted(buckets.keys(), key=lambda tid: len(buckets[tid]), reverse=True)

    for tid in topic_order:
        ps = buckets[tid]
        topic = next((t for t in topics if t["id"] == tid), None)
        if not topic:
            continue

        msg = format_topic_digest(topic, ps, top_n, min_score)
        if not msg:
            continue

        chunks = split_long(msg)
        for ci, chunk in enumerate(chunks):
            label = f"{topic['name']} ({ci+1}/{len(chunks)})"
            if post_markdown(webhook_url, chunk):
                logger.info(f"✓ Sent: {label}")
                total_sent += 1
            else:
                logger.error(f"✗ Failed: {label}")

    # ── Step 4: Summary footer ──────────────────────────────
    hi = sum(1 for p in papers if p.get("best_match", {}).get("level") == "high")
    md = sum(1 for p in papers if p.get("best_match", {}).get("level") == "medium")
    lo = sum(1 for p in papers if p.get("best_match", {}).get("level") == "low")

    footer = (
        f"## 📊 概览\n"
        f"> {today} · 共收录 **{len(papers)}** 篇论文\n"
        f"> 🔴 高相关 **{hi}** 篇 · 🟡 中相关 **{md}** 篇 · ⚪ 低相关 **{lo}** 篇\n"
        f"> LLM 总结: {'✅' if stats.get('llm_enabled') else '❌'}\n"
    )
    post_markdown(webhook_url, footer)
    logger.info(f"Done. Posted {total_sent} topic digests + 1 summary footer.")


if __name__ == "__main__":
    main()

