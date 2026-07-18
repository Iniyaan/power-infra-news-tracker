"""
Daily News Website Builder
----------------------------
Checks Google News for your keywords (optionally limited to specific
websites), ranks each article's URGENCY (High/Medium/Low), gives an
AI analysis + suggestion for each, and builds a static website page
(index.html) showing it all.

Designed to run once a day via GitHub Actions' free scheduled workflow.
GitHub Pages then serves index.html as your own website automatically -
no email, no notifications, just refresh the page to see today's update.

The Gemini key is read from an environment variable first (set as a
GitHub Secret when deployed), falling back to the hardcoded value below
for local testing in Jupyter.
"""

import html as html_lib
import json
import os
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ======================= CONFIG (edit this part) =======================

# Keywords are grouped under 4 scope headers, shown on the website as
# District -> State -> National -> Global sections, in that order.
# Each entry is one combined search query (individual buckets/words from
# your list are consolidated into practical search phrases - searching
# every single word separately would be hundreds of queries, too many
# for a daily free-tier run).
KEYWORDS_BY_SCOPE = {
    "District (Kallakurichi)": [
        "Kallakurichi district news",
        "Kallakurichi collector district administration",
        "Kallakurichi industrial development investment",
        "Kallakurichi power electricity TANGEDCO",
    ],
    "State (Tamil Nadu)": [
        "Tamil Nadu government order GO notification gazette",
        "Tamil Nadu cabinet decision policy change",
        "TANGEDCO TNEB power supply substation outage Tamil Nadu",
        "SIPCOT SIDCO industrial estate approval Tamil Nadu",
        "Tamil Nadu land acquisition industrial building approval",
        "Tamil Nadu labour law industrial dispute strike union",
        "Tamil Nadu pollution control board environmental clearance",
        "Tamil Nadu recruitment employment skill development",
        "Tamil Nadu district collector RDO tahsildar revenue",
    ],
    "National (India)": [
        "India GST income tax budget fiscal policy",
        "India RBI interest rate inflation MSME subsidy PLI scheme",
        "India supply chain logistics port shipping disruption",
        "India labour code compliance factory safety",
        "India footwear textile leather apparel industry",
        "India manufacturing plant expansion industrial investment",
        "India national highway railway infrastructure project",
        "India cyber security data protection DPDP act",
        "India NGT court order environmental case",
        "India ease of doing business industrial corridor",
    ],
    "Global": [
        "China Vietnam Bangladesh manufacturing trade shift",
        "US tariff trade war India export",
        "China plus one manufacturing shift India",
        "global supply chain disruption shipping freight",
        "Nike Adidas Puma supplier factory expansion",
    ],
}

# Websites to search across (applies to every keyword above).
SITE = [
    "timesofindia.indiatimes.com",
    "thehindu.com",
    "ndtv.com",
    "indiatoday.in",
    "news18.com",
    "dailythanthi.com",
    "dinamalar.com",
    "thanthitv.com",
    "puthiyathalaimurai.com",
]

# Max number of articles to include per keyword
MAX_ARTICLES = 6

# Only show articles published within this many hours (filters out old/stale news)
TIME_WINDOW_HOURS = 36

# Turn AI analysis (problem summary + suggestion + urgency ranking) on or off
ENABLE_AI_ANALYSIS = True

# Get this FREE key (no credit card needed) from https://aistudio.google.com/apikey
# For GitHub Actions, this is overridden automatically by a GitHub Secret named GEMINI_API_KEY.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "PASTE_YOUR_REAL_GEMINI_KEY_HERE")

# Only analyze the top N articles per keyword with AI, to control cost/time
# and to stay under Gemini's free-tier rate limit (kept low on purpose -
# with 28 keywords total across all scopes, this gives up to 28 AI-analyzed articles)
MAX_ARTICLES_TO_ANALYZE = 1

# Filename for the generated website page (used by GitHub Pages)
OUTPUT_HTML_FILE = "index.html"

# =========================== END OF CONFIG ==============================


def fetch_news_for_keyword(keyword, max_retries=3):
    """Fetch matching articles from Google News RSS for one keyword.
    Retries with a growing delay if Google returns a transient error (like 503)."""
    query = keyword
    if SITE:
        if isinstance(SITE, list):
            site_filter = " OR ".join(f"site:{s}" for s in SITE)
            query += f" ({site_filter})"
        else:
            query += f" site:{SITE}"

    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"

    last_error = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                xml_data = response.read()
            root = ET.fromstring(xml_data)
            items = root.findall("./channel/item")

            cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)

            articles = []
            for item in items:
                if len(articles) >= MAX_ARTICLES:
                    break
                title = item.findtext("title", default="No title")
                link = item.findtext("link", default="")
                pub_date = item.findtext("pubDate", default="")
                source_el = item.find("source")
                source = source_el.text if source_el is not None else ""
                snippet = item.findtext("description", default="")

                # Skip articles older than TIME_WINDOW_HOURS
                if pub_date:
                    try:
                        published = parsedate_to_datetime(pub_date)
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                        if published < cutoff:
                            continue
                    except Exception:
                        pass  # if the date can't be parsed, keep the article rather than lose it

                articles.append({
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "source": source,
                    "snippet": snippet,
                })
            return articles
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(8 * (attempt + 1))
                continue

    raise last_error


def analyze_article(title, snippet, max_retries=3):
    """Ask Gemini (free tier) to summarize the core problem, suggest a solution,
    and rank the article's URGENCY (how time-sensitive it is) as High/Medium/Low.
    Retries with a growing delay if the free tier's rate limit (429) is hit."""
    prompt = (
        "Here is a news headline and snippet:\n\n"
        f"Title: {title}\n"
        f"Snippet: {snippet}\n\n"
        "Respond with ONLY a JSON object (no markdown, no extra text) in this exact format:\n"
        '{"urgency": "High" or "Medium" or "Low", "analysis": "2-3 short sentences covering '
        '(1) the core problem/issue this news is about, and (2) one practical response or '
        'action someone could take regarding it"}\n\n'
        "Urgency guide: High = breaking/time-critical news needing action within hours/days. "
        "Medium = relevant but not urgent. Low = background/historical/opinion, no time pressure."
    )

    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    )

    for attempt in range(max_retries):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read())
                raw_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                cleaned = raw_text.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(cleaned)
                urgency = parsed.get("urgency", "Medium")
                if urgency not in ("High", "Medium", "Low"):
                    urgency = "Medium"
                analysis = parsed.get("analysis", "").strip()
                return urgency, analysis
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            return "Medium", f"(AI analysis unavailable: HTTP Error {e.code}: {e.reason})"
        except Exception as e:
            return "Medium", f"(AI analysis unavailable: {e})"

    return "Medium", "(AI analysis unavailable: rate limit persisted after retries)"


def analyze_and_sort_articles(articles):
    """Run AI analysis once per keyword's articles and sort by urgency.
    Shared by both the email and the website builder so we never call the AI twice."""
    analyzed = []
    for i, a in enumerate(articles):
        if ENABLE_AI_ANALYSIS and i < MAX_ARTICLES_TO_ANALYZE:
            urgency, analysis = analyze_article(a["title"], a["snippet"])
            time.sleep(6.5)
        else:
            urgency, analysis = "Unranked", ""
        analyzed.append({**a, "urgency": urgency, "analysis": analysis})

    urgency_order = {"High": 0, "Medium": 1, "Low": 2, "Unranked": 3}
    analyzed.sort(key=lambda x: urgency_order.get(x["urgency"], 3))
    return analyzed



def build_html_page(results_by_scope):
    """Build a static HTML page showing today's news, grouped by scope
    (District -> State -> National -> Global), then by keyword within each,
    ranked by urgency, with AI analysis."""
    updated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    urgency_colors = {"High": "#dc2626", "Medium": "#d97706", "Low": "#16a34a", "Unranked": "#6b7280"}

    nav_links = []
    scope_blocks = []

    for scope, results_by_keyword in results_by_scope.items():
        scope_id = html_lib.escape(scope.lower().replace(" ", "-").replace("(", "").replace(")", ""))
        nav_links.append(f'<a href="#{scope_id}">{html_lib.escape(scope)}</a>')

        sections_html = []
        for keyword, analyzed in results_by_keyword.items():
            cards = []
            if not analyzed:
                cards.append('<p class="empty">No new articles found today.</p>')
            for a in analyzed:
                color = urgency_colors.get(a["urgency"], "#6b7280")
                badge = (
                    f'<span class="badge" style="background:{color}">{html_lib.escape(a["urgency"].upper())}</span>'
                    if a["urgency"] != "Unranked" else ""
                )
                analysis_html = (
                    f'<p class="analysis">{html_lib.escape(a["analysis"])}</p>' if a["analysis"] else ""
                )
                published_str = ""
                if a.get("pub_date"):
                    try:
                        dt = parsedate_to_datetime(a["pub_date"])
                        published_str = dt.strftime("%d %b, %I:%M %p")
                    except Exception:
                        published_str = ""
                source_line = html_lib.escape(a["source"])
                if published_str:
                    source_line += f" &middot; {html_lib.escape(published_str)}"
                cards.append(f'''
                <div class="card">
                  {badge}
                  <h3><a href="{html_lib.escape(a["link"])}" target="_blank" rel="noopener">{html_lib.escape(a["title"])}</a></h3>
                  <p class="source">{source_line}</p>
                  {analysis_html}
                </div>''')

            sections_html.append(f'''
            <section>
              <h3 class="keyword-heading">{html_lib.escape(keyword)}</h3>
              <div class="grid">{"".join(cards)}</div>
            </section>''')

        scope_blocks.append(f'''
        <div class="scope-block" id="{scope_id}">
          <h2 class="scope-heading">{html_lib.escape(scope)}</h2>
          {"".join(sections_html)}
        </div>''')

    nav_html = " &nbsp;|&nbsp; ".join(nav_links)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EC NEWS</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#f8fafc; color:#1e293b; margin:0; padding:0 0 60px; }}
  header {{ background:#1e293b; color:#fff; padding:28px 20px; text-align:center; }}
  header h1 {{ margin:0 0 6px; font-size:1.6rem; }}
  header p {{ margin:0; color:#94a3b8; font-size:0.9rem; }}
  nav {{ background:#0f172a; text-align:center; padding:12px; font-size:0.9rem; }}
  nav a {{ color:#93c5fd; text-decoration:none; font-weight:600; }}
  nav a:hover {{ text-decoration:underline; }}
  main {{ max-width:1000px; margin:0 auto; padding:24px 20px; }}
  .scope-block {{ margin-bottom:48px; scroll-margin-top:16px; }}
  .scope-heading {{ font-size:1.5rem; background:#1e293b; color:#fff; padding:10px 16px; border-radius:8px; margin-bottom:8px; }}
  section {{ margin-bottom:30px; margin-top:20px; }}
  .keyword-heading {{ font-size:1.05rem; color:#334155; border-bottom:2px solid #e2e8f0; padding-bottom:8px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(280px,1fr)); gap:16px; margin-top:14px; }}
  .card {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:16px; position:relative; }}
  .card h3 {{ margin:8px 0 4px; font-size:1rem; line-height:1.4; }}
  .card h3 a {{ color:#1e293b; text-decoration:none; }}
  .card h3 a:hover {{ text-decoration:underline; }}
  .source {{ color:#64748b; font-size:0.8rem; margin:0 0 8px; }}
  .analysis {{ font-size:0.88rem; color:#334155; background:#f1f5f9; padding:10px; border-radius:6px; margin:0; }}
  .badge {{ display:inline-block; color:#fff; font-size:0.7rem; font-weight:600; padding:2px 8px; border-radius:999px; margin-bottom:6px; }}
  .empty {{ color:#94a3b8; font-style:italic; }}
</style>
</head>
<body>
<header>
  <h1>EC NEWS</h1>
  <p>Last updated: {updated_at} &middot; refreshes daily at 3pm IST</p>
</header>
<nav>{nav_html}</nav>
<main>
{"".join(scope_blocks)}
</main>
</body>
</html>'''


def main():
    results_by_scope = {}
    for scope, keywords in KEYWORDS_BY_SCOPE.items():
        results_by_scope[scope] = {}
        for keyword in keywords:
            try:
                articles = fetch_news_for_keyword(keyword)
                results_by_scope[scope][keyword] = analyze_and_sort_articles(articles)
            except Exception as e:
                results_by_scope[scope][keyword] = []
                print(f"Error fetching news for '{keyword}': {e}")
            time.sleep(3)  # small pause between keywords to avoid Google News rate limiting

    html_page = build_html_page(results_by_scope)
    with open(OUTPUT_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_page)
    print(f"Website page written to {OUTPUT_HTML_FILE}")


if __name__ == "__main__":
    main()
