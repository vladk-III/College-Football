# NCAA Football Betting Data Collector

Automated pipeline that collects game schedules, multi-sportsbook betting odds, team stats, weather, and post-game outcomes for NCAA FBS football — committed daily as CSVs to this repo via GitHub Actions.

## Architecture

Mirrors the options-data collector pattern: idempotent, season-aware, with email alerts.

```
Tue–Sat (pregame)                     Sun–Mon (postgame)
┌─────────────────────┐               ┌─────────────────────┐
│ • Game schedule      │               │ • Re-fetch scores    │
│ • CFBD betting lines │               │ • Compute ATS / O-U  │
│ • Odds API (multi-   │               │ • Update weather     │
│   book spreads/ML)   │               │   (actual vs forecast)│
│ • Team season stats  │               │ • Write outcomes.csv │
│ • SP+ ratings        │               └─────────────────────┘
│ • Weather forecast   │
└─────────────────────┘
```

### Schedule

| Cron (UTC)         | ET Equivalent | Purpose |
|--------------------|---------------|---------|
| `0 14 * * 2-6`     | 10 AM Tue–Sat | Morning odds snapshot |
| `0 20 * * 2-6`     | 4 PM Tue–Sat  | Afternoon snapshot + watchdog |
| `0 6 * * 0`        | 2 AM Sun      | Saturday night results |
| `0 14 * * 1`       | 10 AM Mon     | Final score cleanup |

Idempotent: only the first successful run per (day, mode) pair collects — subsequent runs exit immediately.

## Setup

### 1. Get API Keys (both free)

| Service | Free Tier | Sign Up |
|---------|-----------|---------|
| CollegeFootballData | Generous — Patreon for higher limits | https://collegefootballdata.com/key |
| The Odds API | 500 requests/month | https://the-odds-api.com |

### 2. Set Repository Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `CFBD_API_KEY` | **Yes** | Bearer token from CollegeFootballData |
| `ODDS_API_KEY` | No | The Odds API key (multi-book odds); omit and it just uses CFBD lines |
| `EMAIL_USER` | No | Gmail address for notifications |
| `EMAIL_APP_PASSWORD` | No | Gmail App Password (not your login password) |
| `EMAIL_TO` | No | Where to send the daily summary |

### 3. Push and Go

The workflow runs automatically during CFB season (late August – mid January). Outside the season, it exits cleanly with no API calls.

Manual trigger: **Actions → CFB Betting Data Collector → Run workflow** with optional overrides for mode, week, and year.

## Data Files

All in `data/`:

### `games.csv`
One row per game. Updated pregame (schedule) and postgame (scores).

| Column | Description |
|--------|-------------|
| `game_id` | CFBD unique game ID |
| `season`, `week`, `season_type` | When |
| `home_team`, `away_team` | Who |
| `home_points`, `away_points` | Final score (null until completed) |
| `completed` | Boolean |
| `venue`, `neutral_site`, `conference_game` | Context |

### `odds_snapshots.csv`
Timestamped odds — multiple rows per game (one per provider per snapshot). Tracks line movement across the week.

| Column | Description |
|--------|-------------|
| `snapshot_ts` | When this snapshot was taken |
| `source` | `cfbd` or `odds_api` |
| `provider` | Sportsbook name (DraftKings, FanDuel, consensus, etc.) |
| `spread`, `over_under`, `home_ml`, `away_ml` | The lines |
| `spread_open`, `ou_open` | Opening lines (CFBD only) |

### `team_season_stats.csv`
Advanced season stats from CFBD (updated weekly). PPA, success rate, explosiveness, etc. for offense and defense.

### `sp_ratings.csv`
Weekly SP+ ratings, Elo, and FPI composites.

### `weather.csv`
Temperature, wind, precipitation, humidity per game venue.

### `outcomes.csv`
**The training labels.** One row per (game, provider). Computed postgame.

| Column | Description |
|--------|-------------|
| `home_points`, `away_points`, `margin` | Final score |
| `closing_spread`, `ats_result` | `home_cover` / `away_cover` / `push` |
| `closing_ou`, `ou_result` | `over` / `under` / `push` |
| `ml_result` | `home_win` / `away_win` |

## Modeling Notes

The data is structured for a model that predicts **which side of the spread / total has value** each week. Key features to explore:

- **Line movement**: compare `spread_open` vs closing `spread` — the market's shift is signal
- **SP+ gap**: `sp_offense` − opponent `sp_defense` mismatches
- **Weather × totals**: wind speed and precipitation vs the total — outdoor games in bad weather trend under
- **ATS history**: some teams consistently cover or fail to cover — mean-reverting but useful short-term
- **Advanced stat edges**: PPA differential, success rate gaps, explosiveness mismatches
