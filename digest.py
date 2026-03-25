"""Email digest v2 — compose and send daily digest with scoring metadata."""

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Template

from fetcher import FeedItem

logger = logging.getLogger(__name__)

EMAIL_TEMPLATE = Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { margin: 0; padding: 0; background: #f8f7f4; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #2c2c2a; }
  .container { max-width: 640px; margin: 0 auto; padding: 24px 16px; }
  .header { padding: 20px 0; border-bottom: 2px solid #e8e6df; margin-bottom: 24px; }
  .header h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; color: #2c2c2a; }
  .header .meta { font-size: 13px; color: #888780; }
  .stats-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat { background: #f1efe8; padding: 8px 12px; border-radius: 6px; font-size: 12px; color: #5f5e5a; }
  .stat b { font-weight: 600; color: #2c2c2a; }
  .category { margin-bottom: 24px; }
  .category h2 { font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: #888780; margin: 0 0 12px; padding-bottom: 6px; border-bottom: 1px solid #e8e6df; }
  .item { padding: 12px 0; border-bottom: 1px solid #f0eee8; }
  .item:last-child { border-bottom: none; }
  .item-header { display: flex; align-items: flex-start; gap: 10px; }
  .score { display: inline-block; min-width: 28px; height: 28px; line-height: 28px; text-align: center; border-radius: 6px; font-size: 13px; font-weight: 600; flex-shrink: 0; }
  .score-high { background: #eaf3de; color: #3b6d11; }
  .score-mid { background: #faeeda; color: #854f0b; }
  .score-low { background: #f1efe8; color: #5f5e5a; }
  .item-title { font-size: 15px; font-weight: 500; margin: 0; }
  .item-title a { color: #185fa5; text-decoration: none; }
  .item-title a:hover { text-decoration: underline; }
  .item-meta { font-size: 12px; color: #888780; margin-top: 3px; }
  .stage-badge { display: inline-block; font-size: 10px; padding: 1px 5px; border-radius: 3px; background: #eeedfe; color: #534ab7; margin-left: 4px; }
  .item-reason { font-size: 13px; color: #5f5e5a; margin-top: 4px; line-height: 1.4; }
  .item-summary { font-size: 13px; color: #73726c; margin-top: 4px; line-height: 1.5; }
  .footer { margin-top: 32px; padding-top: 16px; border-top: 2px solid #e8e6df; font-size: 12px; color: #b4b2a9; text-align: center; }
  .empty { text-align: center; padding: 40px 0; color: #888780; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>AI Research Digest</h1>
    <div class="meta">{{ date }} &middot; v2 with progressive filtering</div>
  </div>

  <div class="stats-row">
    <div class="stat"><b>{{ total_fetched }}</b> fetched</div>
    <div class="stat"><b>{{ new_count }}</b> new</div>
    <div class="stat"><b>{{ after_dedup }}</b> after dedup</div>
    <div class="stat"><b>{{ s1_filtered }}</b> title-filtered</div>
    <div class="stat"><b>{{ item_count }}</b> in digest</div>
    {% if api_savings %}<div class="stat"><b>~{{ api_savings }}%</b> API saved</div>{% endif %}
  </div>

  {% if items %}
  {% for category, cat_items in grouped_items.items() %}
  <div class="category">
    <h2>{{ category_labels.get(category, category) }}</h2>
    {% for item in cat_items %}
    <div class="item">
      <div class="item-header">
        <span class="score {% if item.score >= 8 %}score-high{% elif item.score >= 6 %}score-mid{% else %}score-low{% endif %}">{{ item.score | int }}</span>
        <div>
          <p class="item-title"><a href="{{ item.url }}">{{ item.title }}</a></p>
          <div class="item-meta">
            {{ item.source_name }}
            {% if item.authors %} &middot; {{ item.authors | join(', ') }}{% endif %}
            {% if item.published %} &middot; {{ item.published.strftime('%b %d') }}{% endif %}
            {% if item.score_stage == 'stage3' %}<span class="stage-badge">deep analyzed</span>{% endif %}
          </div>
          <div class="item-reason">{{ item.score_reason }}</div>
          {% if item.summary and item.score_stage != 'stage3' %}
          <div class="item-summary">{{ item.summary[:200] }}{% if item.summary|length > 200 %}&hellip;{% endif %}</div>
          {% endif %}
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endfor %}
  {% else %}
  <div class="empty">
    <p>No new items above the relevance threshold today.</p>
    <p>Try lowering <code>min_score</code> in config.yaml.</p>
  </div>
  {% endif %}

  <div class="footer">
    AI Research Digest v2 &middot; Progressive filtering &middot; Powered by Claude
  </div>
</div>
</body>
</html>""")

CATEGORY_LABELS = {
    "arxiv": "Papers",
    "blogs": "Technical blogs",
    "labs": "Lab & company blogs",
    "github": "GitHub releases",
    "newsletters": "Newsletters & digests",
    "podcasts": "Podcasts",
}


def _group_items_by_category(items: list[FeedItem]) -> dict[str, list[FeedItem]]:
    grouped = {}
    order = ["arxiv", "blogs", "labs", "github", "newsletters", "podcasts"]
    for category in order:
        cat_items = [i for i in items if i.source_category == category]
        if cat_items:
            grouped[category] = cat_items
    for item in items:
        if item.source_category not in order:
            grouped.setdefault(item.source_category, []).append(item)
    return grouped


def compose_digest(
    items: list[FeedItem],
    total_fetched: int,
    new_count: int,
    after_dedup: int,
    stage_counts: dict,
    min_score: float,
    subject_prefix: str = "AI Digest",
) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d, %Y")
    grouped = _group_items_by_category(items)

    s1_filtered = stage_counts.get("stage1_filtered", 0)
    total_scored = sum(stage_counts.values())
    api_savings = round((s1_filtered / total_scored) * 100) if total_scored > 0 else 0

    html = EMAIL_TEMPLATE.render(
        date=date_str,
        items=items,
        grouped_items=grouped,
        category_labels=CATEGORY_LABELS,
        item_count=len(items),
        total_fetched=total_fetched,
        new_count=new_count,
        after_dedup=after_dedup,
        s1_filtered=s1_filtered,
        min_score=min_score,
        api_savings=api_savings if api_savings > 0 else None,
    )

    top_score = max((i.score for i in items), default=0)
    subject = f"{subject_prefix} — {date_str} ({len(items)} items, top: {top_score:.0f})"
    return subject, html


def send_email(subject: str, html_body: str, sender_name: str = "AI Research Digest") -> bool:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    to_email = os.environ.get("DIGEST_TO_EMAIL", "")

    if not all([smtp_user, smtp_password, to_email]):
        logger.error("Missing email config. Set SMTP_USER, SMTP_PASSWORD, DIGEST_TO_EMAIL.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{smtp_user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(f"{subject}\n\nView in HTML for full digest.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, to_email, msg.as_string())
        logger.info(f"Digest sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
