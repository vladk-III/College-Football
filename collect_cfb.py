#!/usr/bin/env python3
"""
NCAA Football Betting Data Collector
=====================================
Collects game schedules, multi-book betting odds, team stats, weather, and
post-game outcomes — committed back to the repo as CSV files.

Architecture mirrors the options-data collector:
  • Idempotent:  one successful collection per calendar day, subsequent runs exit clean
  • Snapshot types:  pregame (Tue–Sat) vs postgame (Sun–Mon)
  • Season-aware:  only runs during CFB season (Week 0 ≈ late Aug → CFP ≈ mid-Jan)
  • Email summary:  one notification per successful run

Data sources:
  1. CollegeFootballData.com API  — games, team stats, SP+ ratings, betting lines, weather
  2. The Odds API                 — live multi-sportsbook odds (spreads, totals, moneylines)
  3. Open-Meteo API               — batched venue weather forecasting (Forecasts ONLY to prevent data leakage)

Outputs (all in data/):
  games.csv           — one row per game per season: schedule + final scores
  odds_snapshots.csv  — timestamped odds snapshots (tracks line movement)
  team_season_stats.csv — per-team season stats (offense/defense)
  weather.csv         — game-day weather conditions (Locks at final pre-game forecast)
  outcomes.csv        — final ATS / O/U / moneyline results per game
"""

import os
import sys
import json
import time
import logging
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import requests
import pytz

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EASTERN = pytz.timezone("US/Eastern")

CFBD_BASE = "https://api.collegefootballdata.com"
ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT = "americanfootball_ncaaf"

DATA_DIR = Path("data")
MARKER_DIR = Path("data/.markers")       # idempotency markers (gitignored)

# Season boundaries (approximate — the script also checks the CFBD calendar)
SEASON_START_MONTH = 8    # August (Week 0 can be late Aug)
SEASON_END_MONTH   = 1    # January (CFP title game)

CFBD_HEADERS = {}         # set in main()
ODDS_API_KEY = ""         # set in main()

LOG = logging.getLogger("cfb_collector")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cfbd_get(endpoint: str, params: dict | None = None) -> list | dict | None:
    """GET from CFBD API with retries."""
    url = f"{CFBD_BASE}{endpoint}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=CFBD_HEADERS, params=params or {}, timeout=30)
            if r.status_code == 429:
                wait = 2 ** (attempt + 1)
                LOG.warning(f"CFBD rate-limited, waiting {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            LOG.warning(f"CFBD {endpoint} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    LOG.error(f"CFBD {endpoint} failed after 3 attempts")
    return None


def odds_get(endpoint: str, params: dict | None = None) -> list | dict | None:
    """GET from The Odds API with retries."""
    url = f"{ODDS_BASE}{endpoint}"
    base_params = {"apiKey": ODDS_API_KEY}
    base_params.update(params or {})
    for attempt in range(3):
        try:
            r = requests.get(url, params=base_params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            r.raise_for_status()
            remaining = r.headers.get("x-requests-remaining", "?")
            LOG.info(f"Odds API requests remaining: {remaining}")
            return r.json()
        except requests.RequestException as e:
            LOG.warning(f"Odds API attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    LOG.error(f"Odds API {endpoint} failed after 3 attempts")
    return None


def append_or_create_csv(df: pd.DataFrame, path: Path, dedup_cols: list[str] | None = None):
    """Append rows to a CSV, creating it if needed.  Optionally dedup on key columns."""
    if path.exists():
        existing = pd.read_csv(path)
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df
    if dedup_cols:
        combined = combined.drop_duplicates(subset=dedup_cols, keep="last")
    combined.to_csv(path, index=False)
    LOG.info(f"  → {path.name}: {len(combined)} total rows ({len(df)} new)")
    return len(df)


_FBS_TEAMS_CACHE: dict[int, set] = {}


def get_fbs_teams(year: int) -> set:
    """Return the set of FBS team names for a given year (cached per run)."""
    if year in _FBS_TEAMS_CACHE:
        return _FBS_TEAMS_CACHE[year]
    LOG.info(f"Fetching FBS team list: year={year}")
    data = cfbd_get("/teams/fbs", {"year": year})
    teams = {t.get("school", "") for t in data} if data else set()
    if not teams:
        LOG.warning("Could not fetch FBS team list — division filtering will be skipped")
    _FBS_TEAMS_CACHE[year] = teams
    return teams


def determine_cfb_week(year: int, today: date, mode: str = "pregame") -> int | None:
    """Ask CFBD for the calendar and return the best week number."""
    cal = cfbd_get("/calendar", {"year": year})
    if not cal:
        return None

    weeks = []
    for entry in cal:
        try:
            start = datetime.fromisoformat(entry["firstGameStart"].replace("Z", "+00:00")).date()
            end   = datetime.fromisoformat(entry["lastGameStart"].replace("Z", "+00:00")).date()
            weeks.append((entry["week"], start, end))
        except (KeyError, ValueError):
            continue
    if not weeks:
        return None
    weeks.sort(key=lambda w: w[1])

    for wk, start, end in weeks:
        if start <= today <= end + timedelta(days=2):
            return wk

    if mode == "postgame":
        for wk, start, end in reversed(weeks):
            if today > end:
                return wk
    else:
        for wk, start, end in weeks:
            if today < start:
                return wk

    first_wk, first_start, _ = weeks[0]
    last_wk, _, last_end = weeks[-1]
    if today < first_start and (first_start - today).days <= 7:
        return first_wk      
    if today > last_end and (today - last_end).days <= 3:
        return last_wk       

    return None


def is_in_season(now_et: datetime) -> bool:
    """Quick month-based check before hitting the API."""
    m = now_et.month
    return m >= SEASON_START_MONTH or m <= SEASON_END_MONTH


def already_ran_today(snapshot_type: str, today_str: str) -> bool:
    marker = MARKER_DIR / f"{today_str}_{snapshot_type}.done"
    return marker.exists()


def mark_done(snapshot_type: str, today_str: str):
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    (MARKER_DIR / f"{today_str}_{snapshot_type}.done").touch()


# ---------------------------------------------------------------------------
# Pregame collection
# ---------------------------------------------------------------------------

def collect_games(year: int, week: int) -> pd.DataFrame:
    """Fetch FBS games for the given week."""
    LOG.info(f"Fetching games: year={year} week={week}")
    data = cfbd_get("/games", {
        "year": year,
        "week": week,
        "seasonType": "regular",
        "division": "fbs",
    })
    post = cfbd_get("/games", {
        "year": year,
        "week": week,
        "seasonType": "postseason",
        "division": "fbs",
    })
    if post:
        data = (data or []) + post

    if not data:
        LOG.warning("No games found")
        return pd.DataFrame()

    rows = []
    for g in data:
        rows.append({
            "game_id":        g.get("id"),
            "season":         g.get("season"),
            "week":           g.get("week"),
            "season_type":    g.get("season_type", g.get("seasonType", "")),
            "start_date":     g.get("start_date", g.get("startDate", "")),
            "neutral_site":   g.get("neutral_site", g.get("neutralSite", False)),
            "conference_game": g.get("conference_game", g.get("conferenceGame", False)),
            "home_team":      g.get("home_team", g.get("homeTeam", "")),
            "home_conference": g.get("home_conference", g.get("homeConference", "")),
            "home_points":    g.get("home_points", g.get("homePoints")),
            "away_team":      g.get("away_team", g.get("awayTeam", "")),
            "away_conference": g.get("away_conference", g.get("awayConference", "")),
            "away_points":    g.get("away_points", g.get("awayPoints")),
            "venue":          g.get("venue"),
            "completed":      g.get("completed", False),
        })
    return pd.DataFrame(rows)


def get_games_within_hours(games_df: pd.DataFrame, hours: int = 24) -> tuple[set, dict]:
    """Return (eligible_game_ids, game_id -> start_datetime)"""
    if games_df.empty:
        return set(), {}
    now = datetime.now(pytz.UTC)
    cutoff = now + timedelta(hours=hours)
    eligible = set()
    starts = {}
    for _, g in games_df.iterrows():
        raw = g.get("start_date", "")
        if not raw:
            continue
        try:
            start_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        starts[g["game_id"]] = start_dt
        if now <= start_dt <= cutoff:
            eligible.add(g["game_id"])
    return eligible, starts


def collect_cfbd_lines(year: int, week: int, eligible_game_ids: set | None = None) -> pd.DataFrame:
    """Fetch betting lines from CFBD (consensus / historical)."""
    LOG.info(f"Fetching CFBD lines: year={year} week={week}")
    data = cfbd_get("/lines", {"year": year, "week": week})
    if not data:
        return pd.DataFrame()

    rows = []
    ts = datetime.now(EASTERN).isoformat()
    for game in data:
        game_id = game.get("id")
        home    = game.get("homeTeam", "")
        away    = game.get("awayTeam", "")
        for line in game.get("lines", []):
            rows.append({
                "snapshot_ts":    ts,
                "source":         "cfbd",
                "game_id":        game_id,
                "home_team":      home,
                "away_team":      away,
                "provider":       line.get("provider", ""),
                "spread":         line.get("spread"),
                "spread_open":    line.get("spreadOpen"),
                "over_under":     line.get("overUnder"),
                "ou_open":        line.get("overUnderOpen"),
                "home_ml":        line.get("homeMoneyline"),
                "away_ml":        line.get("awayMoneyline"),
            })
    df = pd.DataFrame(rows)
    if eligible_game_ids is not None and not df.empty:
        before = len(df)
        df = df[df["game_id"].isin(eligible_game_ids)]
        LOG.info(f"  CFBD lines: kept {len(df)}/{before} rows within 24h-of-kickoff window")
    return df


def collect_odds_api(hours: int = 24) -> pd.DataFrame:
    """Fetch live odds from The Odds API (multi-sportsbook)."""
    if not ODDS_API_KEY:
        LOG.info("No ODDS_API_KEY set, skipping The Odds API")
        return pd.DataFrame()

    now    = datetime.now(pytz.UTC)
    cutoff = now + timedelta(hours=hours)

    LOG.info(f"Fetching odds from The Odds API (kickoff window: next {hours}h)")
    data = odds_get(f"/sports/{ODDS_SPORT}/odds", {
        "regions":          "us",
        "markets":          "h2h,spreads,totals",
        "oddsFormat":       "american",
        "dateFormat":       "iso",
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo":   cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if not data:
        return pd.DataFrame()

    rows = []
    ts = datetime.now(EASTERN).isoformat()
    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = event.get("commence_time", "")
        for book in event.get("bookmakers", []):
            book_key = book.get("key", "")
            row = {
                "snapshot_ts":    ts,
                "source":         "odds_api",
                "game_id":        event.get("id", ""),
                "home_team":      home,
                "away_team":      away,
                "commence_time":  commence,
                "provider":       book_key,
                "spread":         None,
                "spread_price":   None,
                "over_under":     None,
                "ou_price":       None,
                "home_ml":        None,
                "away_ml":        None,
            }
            for market in book.get("markets", []):
                mkey = market.get("key")
                outcomes = market.get("outcomes", [])
                if mkey == "h2h":
                    for o in outcomes:
                        if o.get("name") == home:
                            row["home_ml"] = o.get("price")
                        elif o.get("name") == away:
                            row["away_ml"] = o.get("price")
                elif mkey == "spreads":
                    for o in outcomes:
                        if o.get("name") == home:
                            row["spread"] = o.get("point")
                            row["spread_price"] = o.get("price")
                elif mkey == "totals":
                    for o in outcomes:
                        if o.get("name") == "Over":
                            row["over_under"] = o.get("point")
                            row["ou_price"]   = o.get("price")
            rows.append(row)
    return pd.DataFrame(rows)


def collect_team_stats(year: int) -> pd.DataFrame:
    """Fetch season-level advanced team stats from CFBD, FBS teams only."""
    LOG.info(f"Fetching team season stats: year={year}")
    fbs_teams = get_fbs_teams(year)

    rows = []
    ts = datetime.now(EASTERN).strftime("%Y-%m-%d")
    adv = cfbd_get("/stats/season/advanced", {"year": year})
    if adv:
        for entry in adv:
            team = entry.get("team", "")
            if fbs_teams and team not in fbs_teams:
                continue    
            conf = entry.get("conference", "")
            off  = entry.get("offense", {})
            defe = entry.get("defense", {})
            rows.append({
                "snapshot_date":       ts,
                "team":                team,
                "conference":          conf,
                "games":               entry.get("games", entry.get("season")),
                "off_plays":           off.get("plays"),
                "off_drives":          off.get("drives"),
                "off_ppa":             off.get("ppa"),                
                "off_success_rate":    off.get("successRate"),
                "off_explosiveness":   off.get("explosiveness"),
                "off_power_success":   off.get("powerSuccess"),
                "off_stuff_rate":      off.get("stuffRate"),
                "off_line_yards":      off.get("lineYards"),
                "off_pace":            off.get("pace"),
                "def_plays":           defe.get("plays"),
                "def_drives":          defe.get("drives"),
                "def_ppa":             defe.get("ppa"),
                "def_success_rate":    defe.get("successRate"),
                "def_explosiveness":   defe.get("explosiveness"),
                "def_power_success":   defe.get("powerSuccess"),
                "def_stuff_rate":      defe.get("stuffRate"),
                "def_line_yards":      defe.get("lineYards"),
                "def_havoc_total":     defe.get("havoc", {}).get("total") if isinstance(defe.get("havoc"), dict) else None,
            })
    return pd.DataFrame(rows)


def collect_sp_ratings(year: int) -> pd.DataFrame:
    """Fetch SP+ ratings from CFBD, FBS teams only."""
    LOG.info(f"Fetching SP+ ratings: year={year}")
    fbs_teams = get_fbs_teams(year)
    data = cfbd_get("/ratings/sp", {"year": year})
    if not data:
        return pd.DataFrame()
    rows = []
    ts = datetime.now(EASTERN).strftime("%Y-%m-%d")
    for entry in data:
        team = entry.get("team")
        if fbs_teams and team not in fbs_teams:
            continue    
        rows.append({
            "snapshot_date": ts,
            "team":          team,
            "conference":    entry.get("conference"),
            "sp_overall":    entry.get("rating"),
            "sp_offense":    entry.get("offense", {}).get("rating") if isinstance(entry.get("offense"), dict) else entry.get("offense"),
            "sp_defense":    entry.get("defense", {}).get("rating") if isinstance(entry.get("defense"), dict) else entry.get("defense"),
            "sp_st":         entry.get("specialTeams", {}).get("rating") if isinstance(entry.get("specialTeams"), dict) else None,
            "elo":           entry.get("elo"),
            "fpi":           entry.get("fpi"),
        })
    return pd.DataFrame(rows)


_VENUES_CACHE: dict | None = None

def get_venues() -> dict:
    """Fetch the venue list once per run and cache it: venue name -> {lat, lon, is_dome}."""
    global _VENUES_CACHE
    if _VENUES_CACHE is not None:
        return _VENUES_CACHE

    LOG.info("Fetching venue list (for weather lookups)")
    data = cfbd_get("/venues")
    venues = {}
    if data:
        for v in data:
            name = v.get("name")
            if not name:
                continue
            loc = v.get("location") if isinstance(v.get("location"), dict) else {}
            lat = v.get("latitude", loc.get("y"))
            lon = v.get("longitude", loc.get("x"))
            if lat is None or lon is None:
                continue
            venues[name] = {
                "lat": float(lat),
                "lon": float(lon),
                "is_dome": bool(v.get("dome", False)),
            }
    if not venues:
        LOG.warning("Could not fetch venue coordinates — weather collection will be skipped")
    _VENUES_CACHE = venues
    return venues


def collect_weather(games_df: pd.DataFrame) -> pd.DataFrame:
    """Fetch game weather in bulk using Open-Meteo's location batching API."""
    if games_df.empty:
        return pd.DataFrame()

    venues = get_venues()
    rows = []
    outdoor_games = []
    
    now_utc = datetime.now(pytz.UTC)

    # 1. Filter and group indoor vs outdoor games
    for _, g in games_df.iterrows():
        venue_name = g.get("venue")
        start_raw = g.get("start_date")
        
        try:
            game_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        venue_info = venues.get(venue_name) if pd.notna(venue_name) else None

        # FIX 2: Check for dome BEFORE filtering by the 14-day API window 
        # so domes > 14 days out properly get populated rather than orphaned as nulls
        if venue_info and venue_info.get("is_dome"):
            rows.append({
                "game_id": g.get("game_id"), "season": g.get("season"), "week": g.get("week"),
                "home_team": g.get("home_team"), "away_team": g.get("away_team"), "venue": venue_name,
                "temperature": 72.0, "dewpoint": None, "humidity": None,
                "precipitation": 0.0, "snowfall": 0.0, "wind_direction": None, "wind_speed": 0.0,
                "weather_cond": "Dome", "is_indoor": True,
            })
            continue

        # OPEN-METEO LIMITATION: Forecast endpoint only supports up to 14 days in future
        days_diff = (game_dt - now_utc).days
        if days_diff > 14 or days_diff < -80:
            rows.append({
                "game_id": g.get("game_id"), "season": g.get("season"), "week": g.get("week"),
                "home_team": g.get("home_team"), "away_team": g.get("away_team"), "venue": venue_name,
                "temperature": None, "dewpoint": None, "humidity": None,
                "precipitation": None, "snowfall": None, "wind_direction": None, "wind_speed": None,
                "weather_cond": None, "is_indoor": False,
            })
            continue

        if not venue_info:
            # We silently append empty weather for unknown/NAIA venues to avoid spamming the Actions log
            rows.append({
                "game_id": g.get("game_id"), "season": g.get("season"), "week": g.get("week"),
                "home_team": g.get("home_team"), "away_team": g.get("away_team"), "venue": venue_name,
                "temperature": None, "dewpoint": None, "humidity": None,
                "precipitation": None, "snowfall": None, "wind_direction": None, "wind_speed": None,
                "weather_cond": None, "is_indoor": False,
            })
            continue

        outdoor_games.append((g, game_dt, venue_info))

    if not outdoor_games:
        return pd.DataFrame(rows)

    LOG.info(f"Batch-fetching weather for {len(outdoor_games)} outdoor games...")

    # 2. Group games strictly by their Date string. 
    # This completely prevents "Date Range Too Large" 400 Bad Requests.
    games_by_date = defaultdict(list)
    for g, game_dt, venue_info in outdoor_games:
        date_str = game_dt.strftime("%Y-%m-%d")
        games_by_date[date_str].append((g, game_dt, venue_info))

    # 3. Batch fetch in chunks of 40 per date
    CHUNK_SIZE = 40
    with requests.Session() as session:
        for date_str, daily_games in games_by_date.items():
            for i in range(0, len(daily_games), CHUNK_SIZE):
                chunk = daily_games[i:i+CHUNK_SIZE]
                
                # Format lats and lons into comma-separated strings
                lats = ",".join(str(round(v["lat"], 4)) for _, _, v in chunk)
                lons = ",".join(str(round(v["lon"], 4)) for _, _, v in chunk)
                
                params = {
                    "latitude": lats,
                    "longitude": lons,
                    "start_date": date_str,
                    "end_date": date_str,
                    "hourly": "temperature_2m,precipitation,windspeed_10m,relative_humidity_2m",
                    "temperature_unit": "fahrenheit",
                    "windspeed_unit": "mph",
                    "precipitation_unit": "inch",
                    "timezone": "UTC",
                }

                # Fetch the batch with backoff
                data = None
                for attempt in range(4):
                    try:
                        r = session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20)
                        if r.status_code == 429:
                            wait = 2 ** (attempt + 2)
                            time.sleep(wait)
                            continue
                        r.raise_for_status()
                        data = r.json()
                        break
                    except requests.RequestException as e:
                        if attempt == 3:
                            LOG.error(f"Open-Meteo batched request failed for {date_str}: {e}")
                        time.sleep(2 ** attempt)
                
                if not data:
                    continue
                
                # Open-Meteo returns a dict if 1 location requested, or a list of dicts if >1
                results = data if isinstance(data, list) else [data]
                
                # FIX 3: Length check before zipping to prevent silent misassignment
                if len(results) != len(chunk):
                    LOG.error(f"Open-Meteo returned {len(results)} results for {len(chunk)} locations on {date_str}. Skipping chunk.")
                    continue
                
                # 4. Match the batched results back to the games in the chunk
                for (g, game_dt, _), loc_data in zip(chunk, results):
                    hourly = loc_data.get("hourly", {})
                    times = hourly.get("time", [])
                    if not times:
                        continue
                        
                    target = game_dt.replace(minute=0, second=0, microsecond=0)
                    target_str = target.strftime("%Y-%m-%dT%H:00")
                    
                    if target_str in times:
                        idx = times.index(target_str)
                    else:
                        parsed = [datetime.fromisoformat(t) for t in times]
                        idx = min(range(len(parsed)), key=lambda j: abs(parsed[j] - target.replace(tzinfo=None)))

                    def _at(key):
                        vals = hourly.get(key, [])
                        return vals[idx] if idx < len(vals) else None

                    rows.append({
                        "game_id": g.get("game_id"), "season": g.get("season"), "week": g.get("week"),
                        "home_team": g.get("home_team"), "away_team": g.get("away_team"), "venue": g.get("venue"),
                        "temperature": _at("temperature_2m"), "dewpoint": None, "humidity": _at("relative_humidity_2m"),
                        "precipitation": _at("precipitation"), "snowfall": None,
                        "wind_direction": None, "wind_speed": _at("windspeed_10m"),
                        "weather_cond": None, "is_indoor": False,
                    })
                    
                # Brief safety delay between batches
                time.sleep(0.5)
            
    return pd.DataFrame(rows)


def run_pregame(year: int, week: int) -> dict:
    """Execute full pregame collection. Returns stats dict for email."""
    DATA_DIR.mkdir(exist_ok=True)
    stats = {"type": "pregame", "year": year, "week": week}

    # 1. Games schedule
    games_df = collect_games(year, week)
    if not games_df.empty:
        n = append_or_create_csv(games_df, DATA_DIR / "games.csv", ["game_id"])
        stats["games"] = n

    # 2. Betting lines — only for games kicking off within the next 24 hours
    ODDS_WINDOW_HOURS = 24
    eligible_ids, _starts = get_games_within_hours(games_df, hours=ODDS_WINDOW_HOURS)
    stats["games_in_odds_window"] = len(eligible_ids)
    if not eligible_ids:
        LOG.info(f"No games kick off within {ODDS_WINDOW_HOURS}h — skipping odds collection this run")
        cfbd_lines = pd.DataFrame()
        odds_api   = pd.DataFrame()
    else:
        cfbd_lines = collect_cfbd_lines(year, week, eligible_game_ids=eligible_ids)
        odds_api   = collect_odds_api(hours=ODDS_WINDOW_HOURS)
    all_odds = pd.concat([cfbd_lines, odds_api], ignore_index=True)
    if not all_odds.empty:
        n = append_or_create_csv(all_odds, DATA_DIR / "odds_snapshots.csv")
        stats["odds_rows"] = n

    # 3. Team stats + SP+ (once per week is enough)
    team_stats = collect_team_stats(year)
    if not team_stats.empty:
        n = append_or_create_csv(team_stats, DATA_DIR / "team_season_stats.csv",
                                  ["snapshot_date", "team"])
        stats["team_stats"] = n

    sp = collect_sp_ratings(year)
    if not sp.empty:
        n = append_or_create_csv(sp, DATA_DIR / "sp_ratings.csv",
                                  ["snapshot_date", "team"])
        stats["sp_ratings"] = n

    # 4. Weather (FIX 1: Cross-Week Backfill)
    # Load all known games to find any upcoming games missing/updating weather, 
    # rather than just strictly running weather for the current CFB week.
    all_games_file = DATA_DIR / "games.csv"
    if all_games_file.exists():
        known_games = pd.read_csv(all_games_file)
        combined_games = pd.concat([known_games, games_df]).drop_duplicates(subset=["game_id"], keep="last")
    else:
        combined_games = games_df.copy()

    now_utc = datetime.now(pytz.UTC)
    upcoming_games_list = []
    
    # Filter only to games that are upcoming (or started very recently).
    # This prevents the pregame collector from overwriting past, locked-in pregame forecasts with actuals.
    for _, g in combined_games.iterrows():
        try:
            dt = datetime.fromisoformat(str(g["start_date"]).replace("Z", "+00:00"))
            if dt > now_utc - timedelta(hours=6):
                upcoming_games_list.append(g)
        except (ValueError, TypeError):
            continue
            
    if upcoming_games_list:
        weather_target_df = pd.DataFrame(upcoming_games_list)
        weather = collect_weather(weather_target_df)
        if not weather.empty:
            n = append_or_create_csv(weather, DATA_DIR / "weather.csv", ["game_id"])
            stats["weather"] = n

    return stats


# ---------------------------------------------------------------------------
# Postgame collection
# ---------------------------------------------------------------------------

def compute_outcomes(games_df: pd.DataFrame, odds_path: Path) -> pd.DataFrame:
    """Join completed games with their closing lines to compute ATS/O-U results."""
    completed = games_df[games_df["completed"] == True].copy()
    if completed.empty:
        return pd.DataFrame()

    if not odds_path.exists():
        LOG.warning("No odds_snapshots.csv found — skipping outcome computation")
        return pd.DataFrame()

    odds_df = pd.read_csv(odds_path)

    rows = []
    for _, g in completed.iterrows():
        gid        = g["game_id"]
        home_pts   = g["home_points"]
        away_pts   = g["away_points"]
        if pd.isna(home_pts) or pd.isna(away_pts):
            continue
        home_pts, away_pts = int(home_pts), int(away_pts)
        total      = home_pts + away_pts
        margin     = home_pts - away_pts   # positive = home won

        # Get the LAST snapshot for this game from each provider
        game_odds = odds_df[odds_df["game_id"].astype(str) == str(gid)]
        if game_odds.empty:
            # Still record the score even without odds
            rows.append({
                "game_id": gid, "home_team": g["home_team"], "away_team": g["away_team"],
                "home_points": home_pts, "away_points": away_pts,
                "total_points": total, "margin": margin,
                "provider": "none",
                "closing_spread": None, "ats_result": None,
                "closing_ou": None, "ou_result": None,
                "home_ml": None, "ml_result": None,
            })
            continue

        # Use last snapshot per provider
        last_odds = game_odds.sort_values("snapshot_ts").groupby("provider").last()
        for provider, lo in last_odds.iterrows():
            spread = lo.get("spread")
            ou     = lo.get("over_under")
            h_ml   = lo.get("home_ml")

            ats_result = None
            if pd.notna(spread):
                spread = float(spread)
                adj = margin + spread       # home covers if margin + spread > 0
                if adj > 0:
                    ats_result = "home_cover"
                elif adj < 0:
                    ats_result = "away_cover"
                else:
                    ats_result = "push"

            ou_result = None
            if pd.notna(ou):
                ou = float(ou)
                if total > ou:
                    ou_result = "over"
                elif total < ou:
                    ou_result = "under"
                else:
                    ou_result = "push"

            ml_result = None
            if margin > 0:
                ml_result = "home_win"
            elif margin < 0:
                ml_result = "away_win"
            else:
                ml_result = "tie"

            rows.append({
                "game_id":         gid,
                "home_team":       g["home_team"],
                "away_team":       g["away_team"],
                "home_points":     home_pts,
                "away_points":     away_pts,
                "total_points":    total,
                "margin":          margin,
                "provider":        provider,
                "closing_spread":  lo.get("spread"),
                "ats_result":      ats_result,
                "closing_ou":      lo.get("over_under"),
                "ou_result":       ou_result,
                "home_ml":         h_ml,
                "ml_result":       ml_result,
            })

    return pd.DataFrame(rows)


def run_postgame(year: int, week: int) -> dict:
    """Re-fetch games (now with scores) and compute outcomes."""
    DATA_DIR.mkdir(exist_ok=True)
    stats = {"type": "postgame", "year": year, "week": week}

    # Re-pull games — now completed with scores
    games_df = collect_games(year, week)
    if not games_df.empty:
        n = append_or_create_csv(games_df, DATA_DIR / "games.csv", ["game_id"])
        stats["games_updated"] = n
        completed_count = games_df["completed"].sum() if "completed" in games_df.columns else 0
        stats["completed"] = int(completed_count)

    # REMOVED: Post-game actual weather collection to prevent lookahead bias.
    # The last forecast pulled during the pre-game runs will now stay permanently.

    # Compute outcomes
    outcomes_df = compute_outcomes(games_df, DATA_DIR / "odds_snapshots.csv")
    if not outcomes_df.empty:
        n = append_or_create_csv(outcomes_df, DATA_DIR / "outcomes.csv",
                                  ["game_id", "provider"])
        stats["outcomes"] = n

    return stats


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    global CFBD_HEADERS, ODDS_API_KEY

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="CFB Betting Data Collector")
    parser.add_argument("--force", action="store_true", help="Ignore idempotency markers")
    parser.add_argument("--mode", choices=["auto", "pregame", "postgame"], default="auto",
                        help="Force a specific collection mode")
    parser.add_argument("--week", type=int, help="Override week number")
    parser.add_argument("--year", type=int, help="Override season year")
    args = parser.parse_args()

    # API keys from environment
    cfbd_key = os.environ.get("CFBD_API_KEY", "")
    ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
    if not cfbd_key:
        LOG.error("CFBD_API_KEY not set — cannot proceed")
        sys.exit(1)
    CFBD_HEADERS = {
        "Authorization": f"Bearer {cfbd_key}",
        "Accept": "application/json",
    }

    now_et    = datetime.now(EASTERN)
    today     = now_et.date()
    today_str = today.isoformat()
    dow       = now_et.weekday()   # 0=Mon … 6=Sun

    LOG.info(f"Run at {now_et.strftime('%Y-%m-%d %H:%M ET')}, dow={dow}")

    # --- Season check ---
    if not is_in_season(now_et):
        LOG.info("Off-season — exiting cleanly")
        sys.exit(0)

    # --- Determine season year ---
    year = args.year or (now_et.year if now_et.month >= 6 else now_et.year - 1)

    # --- Determine mode ---
    if args.mode != "auto":
        mode = args.mode
    elif dow in (6, 0):  # Sun=6, Mon=0
        mode = "postgame"
    else:
        mode = "pregame"

    # --- Determine week ---
    week = args.week
    if week is None:
        week = determine_cfb_week(year, today, mode=mode)
        if week is None:
            week = determine_cfb_week(year, today - timedelta(days=3), mode=mode)
        if week is None:
            LOG.info("No active CFB week found — exiting cleanly")
            sys.exit(0)

    LOG.info(f"Season {year}, Week {week}")
    LOG.info(f"Mode: {mode}")

    # --- Idempotency ---
    if not args.force and already_ran_today(mode, today_str):
        LOG.info(f"Already ran {mode} today — exiting cleanly")
        sys.exit(0)

    # --- Run ---
    if mode == "pregame":
        stats = run_pregame(year, week)
    else:
        stats = run_postgame(year, week)

    mark_done(mode, today_str)

    # --- Emit stats for GitHub Actions ---
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"mode={mode}\n")
            f.write(f"year={year}\n")
            f.write(f"week={week}\n")
            f.write(f"stats={json.dumps(stats)}\n")
            f.write("collected=true\n")

    LOG.info(f"Done! Stats: {json.dumps(stats, indent=2)}")


if __name__ == "__main__":
    main()
