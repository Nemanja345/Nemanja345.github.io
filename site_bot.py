#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Game Sniper - dnevna SEO stranica "Free PC games" + (opciono) Telegram objave.

Sta radi pri svakom pokretanju:
  1) Povuce sa ITAD API-ja (isti /deals/v2 poziv kao bot.py) trenutno BESPLATNE
     igre (popust 100%) i najvece popuste na poznate igre.
  2) Napravi staticku stranicu  free-games/index.html  (+ sitemap.xml, robots.txt).
  3) Ako su podeseni TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID, objavi na Telegram
     kanal SAMO igre koje jos nisu objavljene (stanje: data/telegram_posted.json).

Tajne se citaju ISKLJUCIVO iz environment varijabli (GitHub Secrets):
  ITAD_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Opciono: SITE_URL (podrazumevano https://nemanja345.github.io)

Bezbednost: ako ITAD vrati gresku ili praznu listu, skripta se zaustavlja BEZ
menjanja postojece stranice (nikad ne objavljuje praznu stranicu zbog kvara).
"""
import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ITAD_URL = "https://api.isthereanydeal.com/deals/v2"
SITE_URL = os.environ.get("SITE_URL", "https://nemanja345.github.io").rstrip("/")
PAGE_DIR = "free-games"
PLAY_BASE = "https://play.google.com/store/apps/details?id=com.gamesniper.app"
SUPPORT_EMAIL = "gamesniper.support@gmail.com"

# isto kao u bot.py
BANNED_STORES = {"Fanatical", "itch.io", "Amazon"}
JUNK_WORDS = (
    "elearning", "programming", "course", "software", "ebook", "audiobook",
    "asset pack", "web dev", "magazine", "tutorial", "dlc", "soundtrack",
    "artbook", "expansion", "season pass",
)

MAX_TG_POSTS_PER_RUN = 1      # najvise novih objava po pokretanju
FIRST_RUN_TG_POSTS = 5        # pri prvom pokretanju objavi samo top N (ostalo se tiho oznaci)
STATE_KEEP_DAYS = 120


# ----------------------------------------------------------------- helpers
def play_link(source, campaign):
    return f"{PLAY_BASE}&referrer=utm_source%3D{source}%26utm_campaign%3D{campaign}"


def esc(s):
    return html.escape(str(s), quote=True)


def money(x):
    return f"${x:,.2f}"


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_expiry(value):
    dt = parse_dt(value)
    if not dt:
        return ""
    return f"{dt.strftime('%b')} {dt.day}"


def slugify(text):
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else "-")
    slug = "-".join(p for p in "".join(out).split("-") if p)
    return slug[:60] or "game"


# ----------------------------------------------------------------- ITAD
def itad_post(payload, key, max_retries=4):
    headers = {"ITAD-API-Key": key, "Content-Type": "application/json"}
    res = None
    for attempt in range(max_retries):
        res = requests.post(ITAD_URL, json=payload, headers=headers, timeout=40)
        if res.status_code != 429:
            break
        try:
            wait = float(res.headers.get("Retry-After") or 5 * (attempt + 1))
        except ValueError:
            wait = 5 * (attempt + 1)
        print(f"  HTTP 429 - cekam {wait}s ({attempt + 1}/{max_retries})")
        time.sleep(wait)
    if res is None or res.status_code != 200:
        code = res.status_code if res is not None else "n/a"
        raise RuntimeError(f"ITAD API greska: HTTP {code}")
    return res.json()


def parse_item(item):
    """Vraca ociscen recnik ili None ako igra ne treba da se prikaze."""
    deal = item.get("deal") or {}
    gid = item.get("id")
    title = item.get("title")
    if not gid or not title:
        return None
    store = (deal.get("shop") or {}).get("name") or "Unknown Store"
    if store in BANNED_STORES:
        return None
    low = title.lower()
    if any(w in low for w in JUNK_WORDS):
        return None
    try:
        price = float((deal.get("price") or {}).get("amount") or 0)
        regular = float((deal.get("regular") or {}).get("amount") or 0)
        cut = int(deal.get("cut") or 0)
    except (TypeError, ValueError):
        return None
    assets = item.get("assets") or {}
    thumb = assets.get("banner400") or assets.get("boxart") or ""
    url = deal.get("url") or ""
    if not url.startswith("https://") or not thumb.startswith("https://"):
        return None
    return {
        "id": gid, "title": title, "store": store, "price": price,
        "regular": regular, "cut": cut, "thumb": thumb, "url": url,
        "expiry": deal.get("expiry"),
    }


def fetch_free(key, max_pages=3):
    found = {}
    for page in range(max_pages):
        data = itad_post({"country": "US", "limit": 100, "offset": page * 100, "sort": "-cut"}, key)
        items = data.get("list") or []
        if not items:
            if page == 0:
                raise RuntimeError("ITAD je vratio praznu listu - prekidam bez izmena.")
            break
        reached_end = False
        for it in items:
            raw_cut = int(((it.get("deal") or {}).get("cut")) or 0)
            if raw_cut < 100:
                reached_end = True
                continue
            g = parse_item(it)
            if g and g["price"] == 0 and g["regular"] >= 1.0:
                found[g["id"]] = g
        if reached_end:
            break
        time.sleep(0.5)
    return sorted(found.values(), key=lambda g: (-g["regular"], g["title"].lower()))


def fetch_top(key, pages=2, limit=24):
    found = {}
    for page in range(pages):
        payload = {"country": "US", "limit": 100, "offset": page * 100, "sort": "-cut",
                   "filter": {"regular": {"min": 20}}}
        data = itad_post(payload, key)
        for it in data.get("list") or []:
            g = parse_item(it)
            if g and 50 <= g["cut"] < 100 and g["regular"] >= 20:
                found[g["id"]] = g
        time.sleep(0.5)
    best = sorted(found.values(), key=lambda g: -(g["regular"] - g["price"]))
    return best[:limit]


# ----------------------------------------------------------------- HTML
CSS = """
:root{color-scheme:dark;--bg:#0f1116;--card:#171a22;--line:#262b38;--text:#e8eaf0;--muted:#9aa3b5;--green:#69f0ae;--red:#ff5252}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--green)}
.wrap{max-width:1000px;margin:0 auto;padding:0 16px}
header{padding:24px 0 4px}
.brand{color:var(--green);font-weight:700;text-decoration:none;letter-spacing:.3px}
h1{font-size:clamp(1.7rem,4.5vw,2.4rem);line-height:1.2;margin:14px 0 10px}
h2{font-size:1.4rem;margin:38px 0 14px}
.lead{color:var(--muted);max-width:70ch;margin:0}
.cta{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:22px 0}
.cta p{margin:0;max-width:60ch}
.btn{background:var(--green);color:#06210f;font-weight:700;padding:10px 18px;border-radius:999px;text-decoration:none;white-space:nowrap}
.grid{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;color:inherit;text-decoration:none;height:100%}
.card a:hover{border-color:var(--green)}
.card img{width:100%;aspect-ratio:400/187;object-fit:cover;display:block;background:#0b0d12}
.body{padding:10px 12px 12px}
.body h3{font-size:.98rem;margin:0 0 6px;line-height:1.3}
.meta{font-size:.8rem;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap}
.price{margin-top:8px;display:flex;gap:8px;align-items:baseline}
.price s{color:var(--muted);font-size:.85rem}
.free{color:var(--green);font-weight:800}
.badge{background:var(--red);color:#fff;font-size:.75rem;font-weight:700;border-radius:6px;padding:1px 6px}
.empty{color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:18px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
summary{cursor:pointer;font-weight:600}
details p{margin:10px 0 0;color:var(--muted)}
footer{border-top:1px solid var(--line);margin-top:42px;padding:22px 0 40px;color:var(--muted);font-size:.85rem}
footer a{color:var(--muted)}
"""

FAQ = [
    ("How do free PC game giveaways work?",
     "Stores such as the Epic Games Store, Steam, GOG and others occasionally give a game away for a limited time. "
     "If you claim it before the offer ends, it is added to your library and stays yours to keep, even after the price goes back up."),
    ("Are these games free forever?",
     "No. Most giveaways last from a few hours to about a week. Claim a game while it shows as free; "
     "after the end date the normal price returns. The end date is shown on each card when the store provides it."),
    ("Do I need to pay anything or enter a credit card?",
     "Claiming a free game does not cost anything. Some stores ask you to sign in or create a free account first."),
    ("How can I get notified when a game becomes free?",
     "Install the Game Sniper app for Android, add the games you are waiting for to your price alerts, "
     "and you will get a notification when one goes on sale or becomes free."),
    ("Which stores are covered?",
     "Steam, Epic Games Store, GOG, Humble Store and many other PC game stores. Prices are shown in US dollars and can differ by region."),
]

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__DESC__">
<link rel="canonical" href="__CANON__">
<meta name="theme-color" content="#0f1116">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Game Sniper">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__DESC__">
<meta property="og:url" content="__CANON__">
__OGIMAGE__
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">__JSONLD__</script>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
<header><a class="brand" href="__HOME__">GAME SNIPER</a></header>
<main>
<h1>Free PC Games You Can Claim Right Now</h1>
<p class="lead">__INTRO__</p>
<div class="cta">
<p><strong>Never miss a free game again.</strong> Get a notification the moment a game goes free or hits your target price.</p>
<a class="btn" href="__PLAY_TOP__" rel="noopener">Get Game Sniper on Google Play</a>
</div>
<h2 id="free-now">Free to claim today</h2>
__FREE__
<h2 id="biggest-deals">Biggest PC game deals today</h2>
<p class="lead">The largest discounts on popular games right now (regular price $20 or more).</p>
<div style="height:14px"></div>
__TOP__
<h2>Frequently asked questions</h2>
__FAQ__
</main>
<footer>
<p>Last updated: __DATE__ (UTC). Prices in USD; availability and prices can vary by region and may change without notice. Game Sniper is not affiliated with Steam, Epic Games Store, GOG or any other store.</p>
<p>Deal data provided by <a href="https://isthereanydeal.com/" rel="noopener">IsThereAnyDeal.com</a>. Questions: <a href="mailto:__MAIL__">__MAIL__</a>__TGLINK__</p>
</footer>
</div>
</body>
</html>
"""


def render_card(g, free):
    slug = slugify(g["title"])
    ends = fmt_expiry(g["expiry"]) if free else ""
    meta = f'<span class="store">{esc(g["store"])}</span>'
    if ends:
        meta += f'<span class="ends">Free until {esc(ends)}</span>'
    if free:
        price = f'<s>{money(g["regular"])}</s><b class="free">FREE</b>'
    else:
        price = (f'<span class="badge">-{g["cut"]}%</span><b>{money(g["price"])}</b>'
                 f'<s>{money(g["regular"])}</s>')
    return (
        f'<li class="card" id="{esc(slug)}"><a href="{esc(g["url"])}" rel="nofollow noopener" target="_blank">'
        f'<img src="{esc(g["thumb"])}" alt="{esc(g["title"])} cover art" loading="lazy" width="400" height="187">'
        f'<div class="body"><h3>{esc(g["title"])}</h3><div class="meta">{meta}</div>'
        f'<div class="price">{price}</div></div></a></li>'
    )


def build_html(free, top, now):
    canon = f"{SITE_URL}/{PAGE_DIR}/"
    date = now.strftime("%B %d, %Y").replace(" 0", " ")
    n = len(free)
    if n:
        title = "Free PC Games Right Now (Updated Daily) | Game Sniper"
        desc = (f"{n} PC games you can claim for free today on Steam, Epic Games Store, GOG and more. "
                "Updated daily, with the biggest PC game deals and giveaways.")
        intro = (f"Updated {date}. {n} PC game{'s are' if n != 1 else ' is'} 100% free to claim right now, "
                 "plus the biggest discounts on popular games. Giveaways usually last only a few days, "
                 "so claim what you want before the offer ends.")
        free_html = '<ul class="grid">' + "".join(render_card(g, True) for g in free) + "</ul>"
    else:
        title = "Free PC Games and Biggest PC Game Deals Today | Game Sniper"
        desc = ("Daily list of free PC game giveaways and the biggest discounts on Steam, "
                "Epic Games Store, GOG and more.")
        intro = (f"Updated {date}. There are no free-to-keep PC games in our list at the moment. "
                 "Check back soon: new giveaways appear every week.")
        free_html = '<div class="empty">No free-to-keep games right now. New giveaways appear every week, check back soon.</div>'

    top_html = ('<ul class="grid">' + "".join(render_card(g, False) for g in top) + "</ul>") if top else \
        '<div class="empty">No big discounts to show right now.</div>'

    faq_html = "".join(f"<details><summary>{esc(q)}</summary><p>{esc(a)}</p></details>" for q, a in FAQ)

    items = [{"@type": "ListItem", "position": i + 1, "name": g["title"],
              "url": f"{canon}#{slugify(g['title'])}"} for i, g in enumerate(free[:30])]
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "ItemList",
                         "name": "Free PC games", "numberOfItems": len(items),
                         "itemListElement": items}, ensure_ascii=False).replace("</", "<\\/")

    og_src = (free or top or [None])[0]
    ogimage = f'<meta property="og:image" content="{esc(og_src["thumb"])}">' if og_src else ""

    tg_user = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    tglink = ""
    if tg_user.startswith("@"):
        tglink = f' &middot; <a href="https://t.me/{esc(tg_user[1:])}" rel="noopener">Telegram channel</a>'

    out = TEMPLATE
    for token, value in (
        ("__TITLE__", esc(title)), ("__DESC__", esc(desc)), ("__CANON__", esc(canon)),
        ("__OGIMAGE__", ogimage), ("__JSONLD__", jsonld), ("__CSS__", CSS),
        ("__HOME__", esc(SITE_URL + "/")), ("__INTRO__", esc(intro)),
        ("__PLAY_TOP__", esc(play_link("website", "free-games"))),
        ("__FREE__", free_html), ("__TOP__", top_html), ("__FAQ__", faq_html),
        ("__DATE__", esc(date)), ("__MAIL__", esc(SUPPORT_EMAIL)), ("__TGLINK__", tglink),
    ):
        out = out.replace(token, value)
    return out


def write_site(out_dir, free, top, now):
    out = Path(out_dir)
    page = out / PAGE_DIR / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(build_html(free, top, now), encoding="utf-8")

    lastmod = now.strftime("%Y-%m-%d")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{SITE_URL}/</loc><lastmod>{lastmod}</lastmod></url>\n'
        f'  <url><loc>{SITE_URL}/{PAGE_DIR}/</loc><lastmod>{lastmod}</lastmod><changefreq>daily</changefreq></url>\n'
        '</urlset>\n'
    )
    (out / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    robots = out / "robots.txt"
    if not robots.exists():
        robots.write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    print(f"Stranica napisana: {page} ({len(free)} besplatnih, {len(top)} popusta)")


# ----------------------------------------------------------------- Telegram
def tg_caption(g):
    lines = [f"\U0001F3AE <b>{html.escape(g['title'], quote=False)}</b> is FREE",
             f"\U0001F3EC {html.escape(g['store'], quote=False)} · normally {money(g['regular'])}"]
    ends = fmt_expiry(g["expiry"])
    if ends:
        lines.append(f"⏳ Free until {ends}")
    lines.append("")
    lines.append(f'\U0001F4F2 Want to see all free games and deals? <a href="{play_link("telegram", "channel")}">Get the Game Sniper app on Google Play</a>')
    return "\n".join(lines)


def tg_call(token, method, payload):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def tg_send_game(token, chat_id, g):
    keyboard = {"inline_keyboard": [
        [{"text": "Claim it now", "url": g["url"]}],
        [{"text": "See all deals in the app", "url": play_link("telegram", "channel_button")}]]}
    caption = tg_caption(g)
    code, body = tg_call(token, "sendPhoto", {
        "chat_id": chat_id, "photo": g["thumb"], "caption": caption,
        "parse_mode": "HTML", "reply_markup": keyboard})
    if code == 200 and body.get("ok"):
        return True, None
    if code == 429:
        return False, ("rate", (body.get("parameters") or {}).get("retry_after", 30))
    # rezerva: bez slike
    code2, body2 = tg_call(token, "sendMessage", {
        "chat_id": chat_id, "text": caption, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": keyboard})
    if code2 == 200 and body2.get("ok"):
        return True, None
    if code2 == 429:
        return False, ("rate", (body2.get("parameters") or {}).get("retry_after", 30))
    return False, ("error", body2.get("description") or body.get("description") or f"HTTP {code2}")


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), False
        except (ValueError, OSError):
            pass
    return {"posted": {}}, True


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def post_telegram(free, out_dir, token, chat_id, now):
    """Vraca True ako je sve proslo bez gresaka."""
    state_path = Path(out_dir) / "data" / "telegram_posted.json"
    state, first_run = load_state(state_path)
    posted = state.setdefault("posted", {})
    cutoff = (now - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    for k in [k for k, v in posted.items() if v < cutoff]:
        del posted[k]

    def key_of(g):
        return f"{g['id']}|{g['expiry'] or ''}"

    new = [g for g in free if key_of(g) not in posted]
    if first_run:
        for g in new[FIRST_RUN_TG_POSTS:]:
            posted[key_of(g)] = now.isoformat()      # tiho oznaceno, ne objavljuje se
        new = new[:FIRST_RUN_TG_POSTS]
        print(f"Prvo pokretanje: objavljujem top {len(new)}, ostale besplatne igre samo oznacavam.")
    new = new[:MAX_TG_POSTS_PER_RUN]

    ok_all = True
    for g in new:
        sent, err = tg_send_game(token, chat_id, g)
        if sent:
            posted[key_of(g)] = now.isoformat()
            save_state(state_path, state)
            print(f"  Telegram: objavljeno - {g['title']}")
            time.sleep(3)
            continue
        kind, detail = err
        print(f"  Telegram GRESKA ({kind}): {detail}")
        ok_all = False
        break    # rate limit ili losa konfiguracija - ostatak sledeci put
    save_state(state_path, state)
    if not new:
        print("Telegram: nema novih besplatnih igara za objavu.")
    return ok_all


# ----------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Game Sniper: free-games stranica + Telegram")
    ap.add_argument("--out", default=".", help="folder repoa (gde je free-games/), podrazumevano trenutni")
    ap.add_argument("--dry-run", action="store_true", help="napravi stranicu, ali NE objavljuj na Telegram")
    args = ap.parse_args(argv)

    key = os.environ.get("ITAD_API_KEY", "").strip()
    if not key:
        print("Nedostaje ITAD_API_KEY (GitHub Secret).", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    try:
        free = fetch_free(key)
        top = fetch_top(key)
    except Exception as e:                      # ne diramo postojecu stranicu
        print(f"GRESKA pri preuzimanju podataka, stranica NIJE izmenjena: {e}", file=sys.stderr)
        return 1

    write_site(args.out, free, top, now)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if args.dry_run or not token or not chat_id:
        print("Telegram preskocen (dry-run ili nema TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).")
        return 0
    return 0 if post_telegram(free, args.out, token, chat_id, now) else 1


if __name__ == "__main__":
    sys.exit(main())
