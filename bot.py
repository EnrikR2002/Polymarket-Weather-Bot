"""
SYFER WEATHER BOT v1.0
======================
Morning: posts signal cards to Discord (7 AM)
Evening: auto-resolves W/L from Polymarket API (8 PM)
Weekly:  posts session report (Sunday 9 AM)

Setup:
  pip install discord.py aiohttp apscheduler python-dotenv
  Set env vars: DISCORD_TOKEN, CHANNEL_ID
  Run: python bot.py
"""

import asyncio
import json
import os
import math
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ──────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID    = int(os.getenv("CHANNEL_ID", "0"))
DATA_FILE     = Path("syfer_data.json")

# Cities: US uses NOAA, INTL uses Open-Meteo ensemble
US_CITIES = [
    {"name":"NYC",     "slug":"new-york-city", "lat":40.7128,  "lon":-74.0060, "unit":"F"},
    {"name":"Chicago", "slug":"chicago",       "lat":41.8781,  "lon":-87.6298, "unit":"F"},
    {"name":"Dallas",  "slug":"dallas",        "lat":32.7767,  "lon":-96.7970, "unit":"F"},
    {"name":"Toronto", "slug":"toronto",       "lat":43.6532,  "lon":-79.3832, "unit":"F"},
]
INTL_CITIES = [
    {"name":"London",  "slug":"london",        "lat":51.5074,  "lon":-0.1278,  "unit":"C"},
    {"name":"Seoul",   "slug":"seoul",         "lat":37.5665,  "lon":126.9780, "unit":"C"},
    {"name":"Ankara",  "slug":"ankara",        "lat":39.9334,  "lon":32.8597,  "unit":"C"},
]
ALL_CITIES = US_CITIES + INTL_CITIES

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# Grade thresholds
GRADE_A = 25
GRADE_B = 15
GRADE_C = 8

# Entry threshold (mirrors bot account strategy)
ENTRY_THRESHOLD_CENTS = 6.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("syfer")

# ── PERSISTENT STORAGE ──────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"signals": [], "trades": [], "stats": {
        "total":0, "won":0, "lost":0,
        "us_won":0, "us_lost":0,
        "intl_won":0, "intl_lost":0,
        "by_city":{}, "by_grade":{"A":{"w":0,"l":0},"B":{"w":0,"l":0},"C":{"w":0,"l":0}}
    }}

def save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

# ── DATE HELPERS ─────────────────────────────────────────────
def target_dates():
    """Tomorrow and day-after only — today is near-settlement by any reasonable hour."""
    today = datetime.now(timezone.utc).date()
    return [today + timedelta(days=1), today + timedelta(days=2)]

def build_slug(city_slug: str, date) -> str:
    return f"highest-temperature-in-{city_slug}-on-{MONTHS[date.month-1]}-{date.day}-{date.year}"

# ── FORECAST: NOAA ───────────────────────────────────────────
async def fetch_noaa(session: aiohttp.ClientSession, city: dict) -> Optional[dict]:
    """Fetch NOAA hourly forecast → return {date_str: {median, spread, high, low, forecast_text}}"""
    try:
        # Get gridpoint
        async with session.get(
            f"https://api.weather.gov/points/{city['lat']},{city['lon']}",
            headers={"User-Agent":"SyferWeatherBot/1.0","Accept":"application/geo+json"},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200: raise Exception(f"gridpoint {r.status}")
            gp = await r.json()
            hourly_url = gp["properties"]["forecastHourly"]

        async with session.get(
            hourly_url,
            headers={"User-Agent":"SyferWeatherBot/1.0","Accept":"application/geo+json"},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200: raise Exception(f"hourly {r.status}")
            d = await r.json()

        periods = d["properties"]["periods"]
        results = {}
        for date in target_dates():
            ds = date.isoformat()
            day_ps = [p for p in periods
                      if p["startTime"][:10] == ds
                      and 6 <= datetime.fromisoformat(p["startTime"].replace("Z","+00:00")).hour <= 20
                      and p.get("isDaytime", True)]
            if not day_ps:
                continue
            temps = [p["temperature"] if p["temperatureUnit"]=="F"
                     else p["temperature"]*9/5+32 for p in day_ps]
            hi, lo = max(temps), min(temps)
            results[ds] = {
                "median": round((hi+lo)/2),
                "high": round(hi), "low": round(lo),
                "spread": round(hi-lo),
                "forecast": day_ps[0].get("shortForecast",""),
                "source": "NOAA", "unit": "F",
                "date": ds
            }
        return results if results else None

    except Exception as e:
        log.warning(f"NOAA failed for {city['name']}: {e}")
        return None

# ── FORECAST: OPEN-METEO ENSEMBLE ────────────────────────────
async def fetch_ensemble(session: aiohttp.ClientSession, city: dict) -> Optional[dict]:
    """Fetch Open-Meteo 51-member GFS ensemble → return {date_str: {median, spread, top_buckets}}"""
    try:
        unit = "fahrenheit" if city["unit"]=="F" else "celsius"
        url = (f"https://ensemble-api.open-meteo.com/v1/ensemble"
               f"?latitude={city['lat']}&longitude={city['lon']}"
               f"&hourly=temperature_2m&temperature_unit={unit}"
               f"&forecast_days=3&models=gfs_seamless&timezone=auto")

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200: raise Exception(f"ensemble {r.status}")
            d = await r.json()

        times = d["hourly"]["time"]
        members = []
        for m in range(51):
            key = f"temperature_2m_member{str(m).zfill(2)}"
            if key in d["hourly"]:
                members.append(d["hourly"][key])

        if not members:
            raise Exception("no ensemble members")

        results = {}
        for date in target_dates():
            ds = date.isoformat()
            daily_max = []
            for mem in members:
                vals = [mem[i] for i, t in enumerate(times)
                        if t.startswith(ds) and 6 <= int(t[11:13]) <= 20
                        and mem[i] is not None]
                if vals:
                    daily_max.append(max(vals))

            if len(daily_max) < 5:
                continue

            sorted_m = sorted(daily_max)
            median = sorted_m[len(sorted_m)//2]
            spread = sorted_m[int(len(sorted_m)*0.85)] - sorted_m[int(len(sorted_m)*0.15)]
            buckets = build_buckets(daily_max, median, city["unit"])

            results[ds] = {
                "median": round(median*10)/10,
                "spread": round(spread*10)/10,
                "members": len(daily_max),
                "buckets": buckets,
                "source": "Ensemble", "unit": city["unit"],
                "date": ds
            }
        return results if results else None

    except Exception as e:
        log.warning(f"Ensemble failed for {city['name']}: {e}")
        return None

def build_buckets(members: list, median: float, unit: str) -> list:
    """Build probability distribution across 1-degree buckets."""
    center = round(median)
    lo, hi = center - 4, center + 5
    buckets = []

    # Below range
    buckets.append({"label": f"{lo}°{unit} or lower", "low": -999, "high": lo, "cnt": 0})
    # Individual degree buckets
    for v in range(lo, hi+1):
        buckets.append({"label": f"{v+1}°{unit}", "low": v, "high": v+1, "cnt": 0})
    # Above range
    buckets.append({"label": f"{hi+1}°{unit} or higher", "low": hi+1, "high": 9999, "cnt": 0})

    total = len(members)
    for m in members:
        for b in buckets:
            if b["low"] < m <= b["high"]:
                b["cnt"] += 1
                break

    for b in buckets:
        b["prob"] = round(b["cnt"] / total * 100)

    return [b for b in buckets if b["prob"] > 0]

# ── POLYMARKET: FETCH EVENT ───────────────────────────────────
async def fetch_pm_event(session: aiohttp.ClientSession, city: dict, date) -> Optional[dict]:
    """Fetch a Polymarket event by slug → return bucket prices."""
    from urllib.parse import quote_plus
    slug = build_slug(city["slug"], date)
    try:
        proxy = f"https://corsproxy.io/?url={quote_plus('https://gamma-api.polymarket.com/events?slug='+slug)}"
        # Try direct first, then proxy
        for url in [
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            proxy
        ]:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                       headers={"Accept-Encoding": "gzip, deflate"}) as r:
                    if r.status != 200: continue
                    data = await r.json()
                    ev = data[0] if isinstance(data, list) and data else data
                    if not ev or not ev.get("markets"):
                        continue

                    buckets = []
                    for m in ev["markets"]:
                        try:
                            prices = json.loads(m.get("outcomePrices", '["0.5","0.5"]'))
                            yes_cents = round(float(prices[0]) * 100)
                        except:
                            yes_cents = 50
                        buckets.append({
                            "label": (m.get("groupItemTitle") or m.get("question") or "").strip(),
                            "question": m.get("question",""),
                            "yes_cents": yes_cents,
                            "volume": float(m.get("volume", 0)),
                            "market_id": m.get("conditionId",""),
                            "closed": m.get("closed", False),
                            "outcome_prices": m.get("outcomePrices","")
                        })

                    valid = [b for b in buckets if 0 < b["yes_cents"] < 100]
                    if valid:
                        return {
                            "slug": slug, "city": city["slug"],
                            "question": ev.get("title",""),
                            "buckets": valid, "date": date.isoformat(),
                            "volume": float(ev.get("volume", 0))
                        }
            except:
                continue

        return None
    except Exception as e:
        log.warning(f"PM fetch failed {city['name']} {date}: {e}")
        return None

# ── SIGNAL ENGINE ─────────────────────────────────────────────
def compute_signals(forecasts: dict, pm_events: dict) -> list:
    """Match ensemble bucket probabilities to PM market prices. Return sorted signals."""
    signals = []

    for city in ALL_CITIES:
        slug = city["slug"]
        fc = forecasts.get(slug)
        if not fc:
            continue

        for date_str, day_data in fc.items():
            pm_key = f"{slug}_{date_str}"
            pm = pm_events.get(pm_key)
            buckets = day_data.get("buckets", [])

            if not buckets:
                # NOAA — build synthetic buckets
                med = day_data["median"]
                spread = day_data.get("spread", 4)
                import random
                synth = [med + (random.random()-0.5)*spread*1.2 for _ in range(51)]
                buckets = build_buckets(synth, med, day_data["unit"])
                day_data["buckets"] = buckets

            # Skip NOAA signals with wide spread — too uncertain, synthetic buckets create fake gaps
            if day_data.get("source") == "NOAA" and day_data.get("spread", 0) > 8:
                log.info(f"Skipping {city['name']} {date_str} — NOAA spread too wide ({day_data.get('spread')}°F)")
                continue

            if not pm:
                continue

            # Skip thin markets — 1¢ prices on low-volume markets aren't real edge
            if pm.get("volume", 0) < 15000:
                log.info(f"Skipping {city['name']} {date_str} — PM volume too low (${pm.get('volume',0):,.0f})")
                continue

            # Match each significant ensemble bucket to best PM bucket
            best_sig = None
            for ens_b in sorted(buckets, key=lambda x: -x["prob"]):
                if ens_b["prob"] < 15:
                    break

                # Find PM bucket with best temperature overlap
                best_pm = None
                best_score = -1
                for pm_b in pm["buckets"]:
                    if pm_b["closed"]:
                        continue
                    txt = (pm_b["question"] + " " + pm_b["label"]).lower()
                    import re
                    nums = re.findall(r"-?\d+\.?\d*", txt)
                    if not nums:
                        continue
                    pm_lo = float(nums[0])
                    pm_hi = float(nums[1]) if len(nums) > 1 else pm_lo + 1
                    overlap = min(ens_b["high"], pm_hi) - max(ens_b["low"], pm_lo)
                    if overlap > best_score:
                        best_score = overlap
                        best_pm = pm_b

                if not best_pm:
                    best_pm = max(pm["buckets"], key=lambda x: x["volume"]) if pm["buckets"] else None
                if not best_pm:
                    continue

                gap = abs(ens_b["prob"] - best_pm["yes_cents"])
                if not best_sig or gap > best_sig["gap"]:
                    grade = "A" if gap >= GRADE_A else "B" if gap >= GRADE_B else "C" if gap >= GRADE_C else "D"
                    direction = "BUY YES" if ens_b["prob"] > best_pm["yes_cents"] else "BUY NO"
                    best_sig = {
                        "city": city["name"],
                        "city_slug": slug,
                        "is_us": city in US_CITIES,
                        "date_str": date_str,
                        "source": day_data["source"],
                        "unit": day_data["unit"],
                        "median": day_data["median"],
                        "spread": day_data.get("spread", day_data.get("members", 0)),
                        "ens_prob": ens_b["prob"],
                        "ens_bucket": ens_b["label"],
                        "pm_price": best_pm["yes_cents"],
                        "gap": round(gap),
                        "grade": grade,
                        "direction": direction,
                        "question": pm["question"],
                        "pm_slug": pm["slug"],
                        "pm_volume": pm["volume"],
                        "shotgun_buckets": [
                            b for b in pm["buckets"]
                            if b["yes_cents"] <= 10 and not b["closed"]
                        ]
                    }

            if best_sig and best_sig["grade"] in ("A","B","C"):
                signals.append(best_sig)

    return sorted(signals, key=lambda x: -x["gap"])

# ── DISCORD FORMATTING ────────────────────────────────────────
GRADE_EMOJI = {"A":"🔥","B":"✅","C":"🟡","D":"⚪"}
GRADE_COLOR  = {"A":0x00ff88,"B":0x00d4ff,"C":0xffd166,"D":0x4a6680}

def signal_embed(sig: dict, idx: int) -> discord.Embed:
    grade = sig["grade"]
    date_nice = datetime.strptime(sig["date_str"], "%Y-%m-%d").strftime("%b %d")
    src_flag = "🇺🇸 NOAA" if sig["is_us"] else "🌍 Ensemble"

    embed = discord.Embed(
        title=f"{GRADE_EMOJI[grade]} GRADE {grade} — {sig['city']} {date_nice}",
        description=f"**{sig['direction']}** `{sig['ens_bucket']}`",
        color=GRADE_COLOR[grade]
    )
    embed.add_field(name="Ensemble",   value=f"**{sig['ens_prob']}%**",           inline=True)
    embed.add_field(name="Market",     value=f"**{sig['pm_price']}¢**",           inline=True)
    embed.add_field(name="Gap",        value=f"**{sig['gap']}%** edge",           inline=True)
    embed.add_field(name="Forecast",   value=f"{sig['median']}°{sig['unit']} ±{sig['spread']}°", inline=True)
    embed.add_field(name="Source",     value=src_flag,                            inline=True)
    embed.add_field(name="PM Volume",  value=f"${sig['pm_volume']:,.0f}",         inline=True)

    # Shotgun buckets (bot strategy: buy ≤6¢ adjacent buckets)
    sb = sig.get("shotgun_buckets", [])
    if sb:
        shotgun_str = "\n".join(
            f"`{b['label'][:20]:<20}` {b['yes_cents']}¢ → **BUY**"
            for b in sb[:5]
        )
        total_cost = sum(b["yes_cents"] for b in sb[:4])
        embed.add_field(
            name=f"🎯 SHOTGUN BUCKETS ({len(sb)} ≤10¢)",
            value=f"{shotgun_str}\n\n*Buy all above at ~6¢ · Total: ~{total_cost}¢ · One wins = 100¢*",
            inline=False
        )

    embed.add_field(
        name="Market",
        value=f"[polymarket.com/event/{sig['pm_slug']}](https://polymarket.com/event/{sig['pm_slug']})",
        inline=False
    )
    embed.set_footer(text=f"Signal #{idx} · React ✅ if you placed this trade · React ❌ to skip")
    return embed

def summary_embed(signals: list) -> discord.Embed:
    total = len(signals)
    a_cnt = sum(1 for s in signals if s["grade"]=="A")
    b_cnt = sum(1 for s in signals if s["grade"]=="B")
    c_cnt = sum(1 for s in signals if s["grade"]=="C")
    best = signals[0] if signals else None

    embed = discord.Embed(
        title="☀️ SYFER WEATHER — MORNING SIGNALS",
        description=f"**{total} signals** for tomorrow + day-after\n"
                    f"🔥 Grade A: {a_cnt}  ✅ Grade B: {b_cnt}  🟡 Grade C: {c_cnt}",
        color=GRADE_COLOR.get(best["grade"] if best else "D", 0x4a6680)
    )
    if best:
        embed.add_field(
            name="⚡ BEST SIGNAL",
            value=f"**{best['city']}** — {best['gap']}% gap · {best['ens_prob']}% ensemble vs {best['pm_price']}¢ market",
            inline=False
        )

    us_sigs = [s for s in signals if s["is_us"]]
    intl_sigs = [s for s in signals if not s["is_us"]]
    embed.add_field(name=f"🇺🇸 US ({len(us_sigs)})",   value=", ".join(s['city'] for s in us_sigs) or "none",   inline=True)
    embed.add_field(name=f"🌍 INTL ({len(intl_sigs)})", value=", ".join(s['city'] for s in intl_sigs) or "none", inline=True)
    embed.set_footer(text="React ✅ on individual signal cards if you traded them · Bot auto-resolves at 8 PM")
    return embed

def resolution_embed(resolved: list, data: dict) -> discord.Embed:
    won = [r for r in resolved if r["outcome"]=="won"]
    lost = [r for r in resolved if r["outcome"]=="lost"]
    stats = data["stats"]

    embed = discord.Embed(
        title="🌙 SYFER WEATHER — EVENING RESOLUTION",
        description=f"**{len(resolved)} signals resolved**\n✅ Won: {len(won)}  ❌ Lost: {len(lost)}",
        color=0x00ff88 if len(won) > len(lost) else 0xff3366
    )

    for r in resolved[:8]:
        icon = "✅" if r["outcome"]=="won" else "❌"
        pnl = r.get("pnl_est","")
        embed.add_field(
            name=f"{icon} {r['city']} — {r['ens_bucket']}",
            value=f"Pred: {r['ens_prob']}% · Market was {r['pm_price']}¢ · {pnl}",
            inline=False
        )

    # Session stats
    total = stats["total"]
    wr = round(stats["won"]/total*100) if total else 0
    us_t = stats["us_won"]+stats["us_lost"]
    intl_t = stats["intl_won"]+stats["intl_lost"]
    us_wr = round(stats["us_won"]/us_t*100) if us_t else 0
    intl_wr = round(stats["intl_won"]/intl_t*100) if intl_t else 0

    embed.add_field(
        name="📊 RUNNING STATS",
        value=(f"Overall: **{wr}%** win ({stats['won']}W/{stats['lost']}L)\n"
               f"🇺🇸 US (NOAA): **{us_wr}%** ({stats['us_won']}W/{stats['us_lost']}L)\n"
               f"🌍 INTL (Ens): **{intl_wr}%** ({stats['intl_won']}W/{stats['intl_lost']}L)"),
        inline=False
    )
    embed.set_footer(text="Resolution auto-detected from Polymarket outcomePrices > 0.95")
    return embed

def report_embed(data: dict) -> discord.Embed:
    stats = data["stats"]
    total = stats["total"]
    wr = round(stats["won"]/total*100) if total else 0
    us_t = stats["us_won"]+stats["us_lost"]
    intl_t = stats["intl_won"]+stats["intl_lost"]
    us_wr = round(stats["us_won"]/us_t*100) if us_t else 0
    intl_wr = round(stats["intl_won"]/intl_t*100) if intl_t else 0

    by_city = stats.get("by_city", {})
    city_lines = []
    for city, cs in sorted(by_city.items(), key=lambda x: -(x[1]["w"]+x[1]["l"])):
        t = cs["w"]+cs["l"]
        if t == 0: continue
        r = round(cs["w"]/t*100)
        city_lines.append(f"**{city}**: {r}% ({cs['w']}W/{cs['l']}L)")

    by_grade = stats.get("by_grade", {})
    grade_lines = []
    for g in ["A","B","C"]:
        gs = by_grade.get(g, {"w":0,"l":0})
        t = gs["w"]+gs["l"]
        if t == 0: continue
        r = round(gs["w"]/t*100)
        grade_lines.append(f"Grade **{g}**: {r}% ({gs['w']}W/{gs['l']}L)")

    embed = discord.Embed(
        title="📋 SYFER WEATHER — WEEKLY REPORT",
        description=f"**{total} total signals logged**\nOverall win rate: **{wr}%**",
        color=0xa78bfa
    )
    embed.add_field(name="🇺🇸 US (NOAA)",          value=f"**{us_wr}%** win rate\n{stats['us_won']}W / {stats['us_lost']}L", inline=True)
    embed.add_field(name="🌍 INTL (Ensemble)",      value=f"**{intl_wr}%** win rate\n{stats['intl_won']}W / {stats['intl_lost']}L", inline=True)
    embed.add_field(name="📊 By Grade",             value="\n".join(grade_lines) or "no data", inline=False)
    embed.add_field(name="🏙️ By City",              value="\n".join(city_lines[:8]) or "no data", inline=False)

    # Recommendation
    if total < 20:
        rec = f"⚠️ Only {total} signals — need 20+ for statistical confidence. Keep logging."
    elif wr >= 65:
        rec = "✅ Edge confirmed. Consider scaling up position size."
    elif wr >= 50:
        rec = "🟡 Marginal edge. Focus on Grade A signals only, tighten entry filter."
    else:
        rec = "🔴 Below 50% — review which cities/grades are dragging. Check ensemble calibration."

    embed.add_field(name="🤖 Recommendation", value=rec, inline=False)
    embed.set_footer(text="Data persists in syfer_data.json — survives restarts")
    return embed

# ── KEEP-ALIVE SERVER ─────────────────────────────────────────
from aiohttp import web as aiohttp_web

async def start_keepalive():
    app = aiohttp_web.Application()
    app.router.add_get("/", lambda r: aiohttp_web.Response(text="SYFER alive"))
    app.router.add_get("/health", lambda r: aiohttp_web.Response(text="OK"))
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "0.0.0.0", 8080).start()
    log.info("Keep-alive server running on port 8080")

# ── BOT CLASS ─────────────────────────────────────────────────
class SyferBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)
        self.scheduler = AsyncIOScheduler(timezone="US/Eastern")
        self.channel: Optional[discord.TextChannel] = None
        # Maps message_id → signal dict (to log W/L via reaction)
        self.pending_reactions: dict = {}

    async def setup_hook(self):
        self.scheduler.add_job(self.morning_signals, "cron", hour=7, minute=0)
        self.scheduler.add_job(self.evening_resolution, "cron", hour=20, minute=0)
        self.scheduler.add_job(self.weekly_report, "cron", day_of_week="sun", hour=9, minute=0)
        self.scheduler.start()
        log.info("Scheduler started — 7 AM signals, 8 PM resolution, Sunday reports")
        asyncio.create_task(start_keepalive())

    async def on_ready(self):
        self.channel = self.get_channel(CHANNEL_ID)
        log.info(f"SYFER bot online as {self.user} — channel: {self.channel}")
        # Restore today's pending reactions from disk so reactions survive restarts
        data = load_data()
        today = datetime.now(ET).date().isoformat()
        for sig in data.get("signals", []):
            if sig.get("signal_date") == today and sig.get("message_id"):
                self.pending_reactions[int(sig["message_id"])] = sig
        log.info(f"Restored {len(self.pending_reactions)} pending reactions from disk")
        if self.channel:
            await self.channel.send(embed=discord.Embed(
                title="🌤️ SYFER WEATHER BOT ONLINE",
                description="Scheduled: **7 AM** signals · **8 PM** resolution · **Sunday** weekly report\n\nReact ✅ on signal cards to log a trade · ❌ to skip",
                color=0x00d4ff
            ))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id: return
        msg_id = payload.message_id
        if msg_id not in self.pending_reactions: return

        sig = self.pending_reactions[msg_id]
        emoji = str(payload.emoji)

        if emoji == "✅":
            sig["traded"] = True
            # Persist to disk so evening resolution sees it
            data = load_data()
            for s in data.get("signals", []):
                if s.get("message_id") == msg_id:
                    s["traded"] = True
                    break
            save_data(data)
            log.info(f"Signal marked as TRADED: {sig['city']} {sig['grade']}")
            channel = self.get_channel(payload.channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(msg_id)
                    await message.add_reaction("📝")
                except Exception as e:
                    log.warning(f"Could not add 📝 reaction: {e}")
        elif emoji == "❌":
            sig["traded"] = False
            data = load_data()
            for s in data.get("signals", []):
                if s.get("message_id") == msg_id:
                    s["traded"] = False
                    break
            save_data(data)
            log.info(f"Signal skipped: {sig['city']} {sig['grade']}")

    # ── MORNING JOB ──────────────────────────────────────────
    async def morning_signals(self):
        log.info("Morning job starting...")
        if not self.channel:
            self.channel = self.get_channel(CHANNEL_ID)
        if not self.channel:
            log.error("Channel not found")
            return

        await self.channel.send(embed=discord.Embed(
            title="🌅 Fetching forecasts + market prices...",
            color=0x4a6680
        ))

        async with aiohttp.ClientSession() as session:
            # Fetch forecasts
            forecasts = {}
            for city in US_CITIES:
                fc = await fetch_noaa(session, city)
                if fc: forecasts[city["slug"]] = fc
                else:
                    fc = await fetch_ensemble(session, city)
                    if fc: forecasts[city["slug"]] = fc
                await asyncio.sleep(0.5)

            for city in INTL_CITIES:
                fc = await fetch_ensemble(session, city)
                if fc: forecasts[city["slug"]] = fc
                await asyncio.sleep(0.3)

            # Fetch PM events
            pm_events = {}
            for city in ALL_CITIES:
                for date in target_dates():
                    pm = await fetch_pm_event(session, city, date)
                    if pm:
                        key = f"{city['slug']}_{date.isoformat()}"
                        pm_events[key] = pm
                await asyncio.sleep(0.3)

        # Compute signals
        signals = compute_signals(forecasts, pm_events)

        if not signals:
            await self.channel.send(embed=discord.Embed(
                title="😴 No signals today",
                description="No PM markets found or no significant gaps. Check back tomorrow.",
                color=0x4a6680
            ))
            return

        # Post summary
        await self.channel.send(embed=summary_embed(signals))

        # Post individual signal cards (Grade A and B only to avoid spam)
        tradeable = [s for s in signals if s["grade"] in ("A","B")]
        for idx, sig in enumerate(tradeable[:6], 1):
            embed = signal_embed(sig, idx)
            msg = await self.channel.send(embed=embed)
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            # Store with today's date as key for evening resolution
            sig["signal_date"] = datetime.now(ET).date().isoformat()
            sig["traded"] = False
            sig["message_id"] = msg.id
            self.pending_reactions[msg.id] = sig
            await asyncio.sleep(0.5)

        # Save signals to disk
        data = load_data()
        today = datetime.now(ET).date().isoformat()
        data["signals"] = [s for s in data.get("signals", []) if s.get("signal_date") != today]
        data["signals"].extend([s for s in tradeable[:6]])
        save_data(data)
        log.info(f"Morning job complete: {len(tradeable)} signals posted")

    # ── EVENING JOB ──────────────────────────────────────────
    async def evening_resolution(self):
        log.info("Evening resolution job starting...")
        if not self.channel:
            self.channel = self.get_channel(CHANNEL_ID)

        data = load_data()
        today = datetime.now(ET).date().isoformat()
        today_sigs = [s for s in data.get("signals", []) if s.get("signal_date") == today]

        if not today_sigs:
            await self.channel.send("😴 No signals to resolve today.")
            return

        resolved = []
        async with aiohttp.ClientSession() as session:
            for sig in today_sigs:
                # Fetch current market prices — near-settlement tells us outcome
                try:
                    url = f"https://gamma-api.polymarket.com/events?slug={sig['pm_slug']}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                           headers={"Accept-Encoding": "gzip, deflate"}) as r:
                        if r.status != 200: continue
                        ev_data = await r.json()
                        ev = ev_data[0] if isinstance(ev_data, list) and ev_data else ev_data
                        if not ev or not ev.get("markets"): continue

                        # Find which bucket is at 99¢+ (= winner)
                        winner_bucket = None
                        for m in ev["markets"]:
                            try:
                                prices = json.loads(m.get("outcomePrices", '["0.5","0.5"]'))
                                yes_price = float(prices[0])
                                if yes_price >= 0.95:  # settled as YES
                                    winner_bucket = (m.get("groupItemTitle") or m.get("question","")).strip()
                                    break
                            except: continue

                        if winner_bucket is None:
                            continue  # Not settled yet

                        # Compare winner to our signal's prediction
                        our_bucket = sig.get("ens_bucket","")
                        # Normalize bucket labels for comparison
                        def normalize(s):
                            import re
                            nums = re.findall(r"-?\d+", s)
                            return tuple(nums)

                        outcome = "won" if normalize(winner_bucket) == normalize(our_bucket) else "lost"
                        entry = sig["pm_price"] / 100
                        size = 50  # assumed $50/trade for P&L estimate
                        fee = 0.001 * size  # 0.1% taker fee
                        pnl = round((1-entry)*size - fee if outcome=="won" else -(entry*size + fee), 2)
                        pnl_str = f"+${pnl}" if pnl > 0 else f"-${abs(pnl)}"

                        resolved_sig = {**sig, "outcome": outcome, "pnl_est": pnl_str,
                                        "winner_bucket": winner_bucket}
                        resolved.append(resolved_sig)

                        # Update stats
                        data["stats"]["total"] += 1
                        if outcome == "won":
                            data["stats"]["won"] += 1
                            if sig["is_us"]: data["stats"]["us_won"] += 1
                            else: data["stats"]["intl_won"] += 1
                        else:
                            data["stats"]["lost"] += 1
                            if sig["is_us"]: data["stats"]["us_lost"] += 1
                            else: data["stats"]["intl_lost"] += 1

                        city = sig["city"]
                        if city not in data["stats"]["by_city"]:
                            data["stats"]["by_city"][city] = {"w":0,"l":0}
                        if outcome == "won": data["stats"]["by_city"][city]["w"] += 1
                        else: data["stats"]["by_city"][city]["l"] += 1

                        grade = sig.get("grade","C")
                        if grade not in data["stats"]["by_grade"]:
                            data["stats"]["by_grade"][grade] = {"w":0,"l":0}
                        if outcome == "won": data["stats"]["by_grade"][grade]["w"] += 1
                        else: data["stats"]["by_grade"][grade]["l"] += 1

                except Exception as e:
                    log.warning(f"Resolution failed for {sig['city']}: {e}")
                    continue

        save_data(data)

        if resolved:
            await self.channel.send(embed=resolution_embed(resolved, data))
        else:
            await self.channel.send(embed=discord.Embed(
                title="🌙 Evening check — markets not settled yet",
                description="Polymarket hasn't resolved today's markets. Try again later or check manually.",
                color=0x4a6680
            ))

        log.info(f"Evening job complete: {len(resolved)} resolved")

    # ── WEEKLY REPORT ─────────────────────────────────────────
    async def weekly_report(self):
        log.info("Weekly report job starting...")
        if not self.channel:
            self.channel = self.get_channel(CHANNEL_ID)
        data = load_data()
        await self.channel.send(embed=report_embed(data))

    # ── MANUAL COMMANDS ───────────────────────────────────────
    async def on_message(self, message):
        if message.author.bot: return
        if not message.content.startswith("!syfer"): return

        parts = message.content.split()
        cmd = parts[1] if len(parts) > 1 else ""

        if cmd == "signals":
            await self.morning_signals()

        elif cmd == "resolve":
            await self.evening_resolution()

        elif cmd == "report":
            data = load_data()
            await message.channel.send(embed=report_embed(data))

        elif cmd == "stats":
            data = load_data()
            stats = data["stats"]
            total = stats["total"]
            wr = round(stats["won"]/total*100) if total else 0
            await message.channel.send(
                f"**SYFER STATS** — {total} signals · {wr}% win rate · "
                f"US: {stats['us_won']}W/{stats['us_lost']}L · "
                f"INTL: {stats['intl_won']}W/{stats['intl_lost']}L"
            )

        elif cmd == "reset":
            save_data({"signals":[],"trades":[],"stats":{
                "total":0,"won":0,"lost":0,
                "us_won":0,"us_lost":0,"intl_won":0,"intl_lost":0,
                "by_city":{},"by_grade":{"A":{"w":0,"l":0},"B":{"w":0,"l":0},"C":{"w":0,"l":0}}
            }})
            await message.channel.send("✅ Stats reset.")

        elif cmd == "help":
            await message.channel.send(
                "**SYFER COMMANDS**\n"
                "`!syfer signals` — run morning signal job now\n"
                "`!syfer resolve` — run evening resolution now\n"
                "`!syfer report` — show weekly report\n"
                "`!syfer stats` — quick stats\n"
                "`!syfer reset` — reset all stats\n"
                "`!syfer help` — this message\n\n"
                "**REACTIONS** on signal cards:\n"
                "✅ = you placed this trade\n"
                "❌ = you skipped\n"
                "Bot auto-resolves at 8 PM ET"
            )

# ── ENTRY POINT ───────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: Set DISCORD_TOKEN in .env file")
        print("Get token from: discord.com/developers/applications")
        exit(1)
    if not CHANNEL_ID:
        print("ERROR: Set CHANNEL_ID in .env file")
        print("Right-click your Discord channel → Copy ID (enable Developer Mode first)")
        exit(1)

    bot = SyferBot()
    bot.run(DISCORD_TOKEN)
