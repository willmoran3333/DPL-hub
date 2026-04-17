-- DPL Project — Sleeper EPL Fantasy SQLite schema
-- All data pulled from api.sleeper.app using sport code "clubsoccer:epl"

PRAGMA foreign_keys = ON;

-- ====================================================================
-- League + users
-- ====================================================================

CREATE TABLE IF NOT EXISTS league (
    league_id           TEXT PRIMARY KEY,
    name                TEXT,
    sport               TEXT,
    season              TEXT,
    season_type         TEXT,
    status              TEXT,
    num_teams           INTEGER,
    draft_id            TEXT,
    previous_league_id  TEXT,
    avatar              TEXT,
    roster_positions    TEXT,     -- JSON array
    scoring_settings    TEXT,     -- JSON object
    settings            TEXT,     -- JSON object
    metadata            TEXT,     -- JSON object
    fetched_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league_users (
    league_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    display_name  TEXT,
    team_name     TEXT,
    avatar        TEXT,
    is_owner      INTEGER,
    metadata      TEXT,    -- JSON
    PRIMARY KEY (league_id, user_id),
    FOREIGN KEY (league_id) REFERENCES league(league_id)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id       TEXT NOT NULL,
    roster_id       INTEGER NOT NULL,
    owner_id        TEXT,
    co_owners       TEXT,     -- JSON
    players         TEXT,     -- JSON array of player_ids
    starters        TEXT,     -- JSON array
    reserve         TEXT,     -- JSON array
    taxi            TEXT,     -- JSON array
    keepers         TEXT,     -- JSON
    wins            INTEGER,
    losses          INTEGER,
    ties            INTEGER,
    fpts            REAL,
    fpts_decimal    REAL,
    fpts_against    REAL,
    fpts_against_decimal REAL,
    waiver_budget_used INTEGER,
    waiver_position INTEGER,
    total_moves     INTEGER,
    settings        TEXT,     -- JSON
    metadata        TEXT,     -- JSON (nicknames, formations per week, etc.)
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (league_id, roster_id),
    FOREIGN KEY (league_id) REFERENCES league(league_id)
);

-- ====================================================================
-- Teams + Players (EPL universe)
-- ====================================================================

CREATE TABLE IF NOT EXISTS teams (
    team_id   TEXT PRIMARY KEY,
    abbr      TEXT UNIQUE,
    name      TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id           TEXT PRIMARY KEY,
    full_name           TEXT,
    first_name          TEXT,
    last_name           TEXT,
    search_full_name    TEXT,
    team_abbr           TEXT,
    team_id             TEXT,         -- joined via schedule when possible
    position_primary    TEXT,         -- first element of fantasy_positions
    fantasy_positions   TEXT,         -- JSON array
    status              TEXT,
    active              INTEGER,
    number              INTEGER,
    height              TEXT,
    birth_date          TEXT,
    birth_city          TEXT,
    birth_country       TEXT,
    rookie_year         TEXT,
    hashtag             TEXT,
    channel_id          TEXT,
    search_rank         INTEGER,
    depth_chart_order   INTEGER,
    depth_chart_position TEXT,
    injury_status       TEXT,
    injury_body_part    TEXT,
    injury_start_date   TEXT,
    injury_notes        TEXT,
    news_updated        INTEGER,
    rotowire_id         INTEGER,
    rotoworld_id        INTEGER,
    yahoo_id            INTEGER,
    espn_id             INTEGER,
    sportradar_id       TEXT,
    stats_id            INTEGER,
    oddsjam_id          TEXT,
    swish_id            TEXT,
    metadata            TEXT,     -- JSON (raw extras)
    fetched_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_abbr);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position_primary);

-- ====================================================================
-- Fixtures (EPL schedule)
-- ====================================================================

CREATE TABLE IF NOT EXISTS fixtures (
    game_id       TEXT PRIMARY KEY,
    week          INTEGER,
    date          TEXT,
    status        TEXT,
    home_team_id  TEXT,
    home_abbr     TEXT,
    home_name     TEXT,
    away_team_id  TEXT,
    away_abbr     TEXT,
    away_name     TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fixtures_week ON fixtures(week);
CREATE INDEX IF NOT EXISTS idx_fixtures_date ON fixtures(date);

-- ====================================================================
-- Per-week per-player stats (normalized long form, easy to aggregate)
-- ====================================================================

CREATE TABLE IF NOT EXISTS player_stats (
    player_id   TEXT NOT NULL,
    season      TEXT NOT NULL,
    season_type TEXT NOT NULL,
    week        INTEGER NOT NULL,
    stat_key    TEXT NOT NULL,
    stat_value  REAL,
    PRIMARY KEY (player_id, season, season_type, week, stat_key)
);

CREATE INDEX IF NOT EXISTS idx_stats_week ON player_stats(season, week);
CREATE INDEX IF NOT EXISTS idx_stats_player ON player_stats(player_id);

CREATE TABLE IF NOT EXISTS player_projections (
    player_id   TEXT NOT NULL,
    season      TEXT NOT NULL,
    season_type TEXT NOT NULL,
    week        INTEGER NOT NULL,
    stat_key    TEXT NOT NULL,
    stat_value  REAL,
    PRIMARY KEY (player_id, season, season_type, week, stat_key)
);

CREATE INDEX IF NOT EXISTS idx_proj_week ON player_projections(season, week);
CREATE INDEX IF NOT EXISTS idx_proj_player ON player_projections(player_id);

-- ====================================================================
-- Transactions (adds/drops/trades)
-- ====================================================================

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    type            TEXT,
    status          TEXT,
    leg             INTEGER,    -- gameweek
    created         INTEGER,
    status_updated  INTEGER,
    creator         TEXT,
    roster_ids      TEXT,       -- JSON
    consenter_ids   TEXT,       -- JSON
    adds            TEXT,       -- JSON {player_id: roster_id}
    drops           TEXT,       -- JSON
    draft_picks     TEXT,       -- JSON
    waiver_budget   TEXT,       -- JSON
    settings        TEXT,       -- JSON
    metadata        TEXT,       -- JSON
    FOREIGN KEY (league_id) REFERENCES league(league_id)
);

CREATE INDEX IF NOT EXISTS idx_tx_leg ON transactions(league_id, leg);

-- ====================================================================
-- H2H Matchups (from GraphQL matchup_legs_raw)
-- Two rows per matchup per week share the same matchup_id
-- ====================================================================

CREATE TABLE IF NOT EXISTS matchup_legs (
    league_id   TEXT NOT NULL,
    season      TEXT NOT NULL,
    week        INTEGER NOT NULL,
    roster_id   INTEGER NOT NULL,
    matchup_id  INTEGER NOT NULL,
    points      REAL,
    starters    TEXT,    -- JSON array of player_ids
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (league_id, season, week, roster_id)
);

CREATE INDEX IF NOT EXISTS idx_matchup_week ON matchup_legs(league_id, season, week);
CREATE INDEX IF NOT EXISTS idx_matchup_id ON matchup_legs(league_id, season, week, matchup_id);

-- ====================================================================
-- Bookkeeping
-- ====================================================================

CREATE TABLE IF NOT EXISTS sport_state (
    sport              TEXT PRIMARY KEY,
    week               INTEGER,
    leg                INTEGER,
    season             TEXT,
    season_type        TEXT,
    league_season      TEXT,
    league_create_season TEXT,
    season_start_date  TEXT,
    display_week       INTEGER,
    season_has_scores  INTEGER,
    fetched_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    league_id   TEXT,
    season      TEXT,
    notes       TEXT
);

-- ====================================================================
-- Helpful views
-- ====================================================================

-- Standings view (points for/against computed from roster settings)
CREATE VIEW IF NOT EXISTS v_standings AS
SELECT
    r.league_id,
    r.roster_id,
    r.owner_id,
    u.display_name,
    u.team_name,
    r.wins,
    r.losses,
    r.ties,
    COALESCE(r.fpts, 0) + COALESCE(r.fpts_decimal, 0) AS points_for,
    COALESCE(r.fpts_against, 0) + COALESCE(r.fpts_against_decimal, 0) AS points_against,
    r.waiver_budget_used,
    r.total_moves
FROM rosters r
LEFT JOIN league_users u ON u.league_id = r.league_id AND u.user_id = r.owner_id;

-- H2H results per week: each row is one side of a matchup
-- Join on (league_id, season, week, matchup_id) with different roster_ids to get both sides
CREATE VIEW IF NOT EXISTS v_matchup_results AS
SELECT
    a.league_id,
    a.season,
    a.week,
    a.matchup_id,
    a.roster_id          AS roster_id_a,
    a.points             AS points_a,
    b.roster_id          AS roster_id_b,
    b.points             AS points_b,
    CASE WHEN a.points > b.points THEN a.roster_id
         WHEN b.points > a.points THEN b.roster_id
         ELSE NULL END    AS winner_roster_id
FROM matchup_legs a
JOIN matchup_legs b
  ON b.league_id = a.league_id
 AND b.season    = a.season
 AND b.week      = a.week
 AND b.matchup_id = a.matchup_id
 AND b.roster_id > a.roster_id;

-- Player season totals (summed over weeks, standard scoring)
CREATE VIEW IF NOT EXISTS v_player_season_points AS
SELECT
    p.player_id,
    p.full_name,
    p.team_abbr,
    p.position_primary,
    s.season,
    SUM(CASE WHEN s.stat_key = 'pts_std' THEN s.stat_value ELSE 0 END) AS total_pts_std,
    COUNT(DISTINCT CASE WHEN s.stat_key = 'pts_std' THEN s.week END) AS weeks_scored,
    SUM(CASE WHEN s.stat_key = 'min' THEN s.stat_value ELSE 0 END) AS total_minutes,
    SUM(CASE WHEN s.stat_key = 'g'   THEN s.stat_value ELSE 0 END) AS goals,
    SUM(CASE WHEN s.stat_key = 'at'  THEN s.stat_value ELSE 0 END) AS assists
FROM players p
LEFT JOIN player_stats s ON s.player_id = p.player_id
GROUP BY p.player_id, s.season;
