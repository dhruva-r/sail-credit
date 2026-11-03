-- This is the migration SQL file.
-- Each query here is ALWAYS ran in the dev setup script - make sure they are compatible!
-- Add conviction log table
CREATE TABLE IF NOT EXISTS conviction_log (
    discord_id INTEGER,
    reason TEXT,
    'timestamp' INTEGER
);

CREATE TABLE IF NOT EXISTS role_images (role_id INTEGER PRIMARY KEY, image_url TEXT);

-- Season history starts when the first season is explicitly created.
CREATE TABLE IF NOT EXISTS seasons (
    season_id INTEGER PRIMARY KEY,
    start_timestamp INTEGER NOT NULL,
    end_timestamp INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED'))
);

CREATE TABLE IF NOT EXISTS user_season_stats (
    discord_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL,
    highest_sail_credit INTEGER NOT NULL,
    highest_rank INTEGER,
    PRIMARY KEY (discord_id, season_id),
    FOREIGN KEY (discord_id) REFERENCES users (discord_id),
    FOREIGN KEY (season_id) REFERENCES seasons (season_id)
);

BEGIN;

CREATE TABLE `sail_credit_log_tmp` (
    discord_id INTEGER,
    season_id INTEGER,
    party_size INTEGER,
    party_created_at INTEGER,
    party_finished_at INTEGER,
    prev_sail_credit INTEGER,
    new_sail_credit INTEGER,
    source TEXT,
    'timestamp' INTEGER
);

INSERT INTO
    `sail_credit_log_tmp` (
        `discord_id`,
        `season_id`,
        `party_size`,
        `party_created_at`,
        `party_finished_at`,
        `prev_sail_credit`,
        `new_sail_credit`,
        `source`,
        `timestamp`
    )
SELECT
    `discord_id`,
    `season_id`,
    `party_size`,
    `party_created_at`,
    `party_finished_at`,
    `prev_sail_credit`,
    `new_sail_credit`,
    `source`,
    `timestamp`
FROM
    `sail_credit_log`;

DROP TABLE `sail_credit_log`;

ALTER TABLE
    `sail_credit_log_tmp` RENAME TO `sail_credit_log`;

COMMIT;

CREATE TABLE IF NOT EXISTS `casino_lobby_log` (
    `uuid` TEXT PRIMARY KEY,
    `start_time` INTEGER,
    `end_time` INTEGER,
    `metadata` BLOB,
    `game` TEXT
);

-- VCT Betting
CREATE TABLE
    IF NOT EXISTS `vct_bets` (
        `id` INTEGER PRIMARY KEY AUTOINCREMENT,
        `discord_id` INTEGER NOT NULL,
        `match_id` INTEGER NOT NULL,
        `team1_name` TEXT NOT NULL,
        `team2_name` TEXT NOT NULL,
        `team_pick` TEXT NOT NULL,
        `amount` INTEGER NOT NULL,
        `odds_at_bet` REAL NOT NULL,
        `potential_payout` REAL NOT NULL,
        `status` TEXT DEFAULT 'pending',
        `placed_at` INTEGER NOT NULL,
        `resolved_at` INTEGER
    );

-- LoL Betting
CREATE TABLE
    IF NOT EXISTS `lol_bets` (
        `id` INTEGER PRIMARY KEY AUTOINCREMENT,
        `discord_id` INTEGER NOT NULL,
        `match_id` INTEGER NOT NULL,
        `team1_name` TEXT NOT NULL,
        `team2_name` TEXT NOT NULL,
        `team_pick` TEXT NOT NULL,
        `amount` INTEGER NOT NULL,
        `odds_at_bet` REAL NOT NULL,
        `potential_payout` REAL NOT NULL,
        `status` TEXT DEFAULT 'pending',
        `placed_at` INTEGER NOT NULL,
        `resolved_at` INTEGER
    );

-- Ranked Betting (Valorant, League)
-- Riot gives each API its own ID for the same account, so each game's puuid is stored separately.
-- A game's puuid is NULL if the account has never played that game.
CREATE TABLE
    IF NOT EXISTS `riot_accounts` (
        `id` INTEGER PRIMARY KEY AUTOINCREMENT,
        `discord_id` INTEGER NOT NULL,
        `name` TEXT NOT NULL,
        `tag` TEXT NOT NULL,
        `valorant_puuid` TEXT UNIQUE,
        `valorant_region` TEXT,
        `league_puuid` TEXT UNIQUE,
        `league_region` TEXT,
        `linked_at` INTEGER NOT NULL
    );

CREATE TABLE
    IF NOT EXISTS `ranked_bets` (
        `id` INTEGER PRIMARY KEY AUTOINCREMENT,
        `discord_id` INTEGER NOT NULL,
        `target_discord_id` INTEGER NOT NULL,
        `game` TEXT NOT NULL,
        `mode` TEXT NOT NULL,
        `puuid` TEXT NOT NULL,
        `riot_id` TEXT NOT NULL,
        `region` TEXT NOT NULL,
        `pick` TEXT NOT NULL,
        `amount` INTEGER NOT NULL,
        `odds_at_bet` REAL NOT NULL,
        `potential_payout` REAL NOT NULL,
        `status` TEXT DEFAULT 'pending',
        `placed_at` INTEGER NOT NULL,
        `counts_from` INTEGER NOT NULL,
        `resolved_at` INTEGER,
        `match_id` TEXT,
        `channel_id` INTEGER
    );
