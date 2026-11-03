from datetime import datetime, timezone, timedelta
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import aiosqlite

import party
import json

db = None


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


async def init():
    global db
    db = await aiosqlite.connect("sail_credit.db", timeout=5)
    db.row_factory = dict_factory


async def cleanup():
    global db
    if db and db.is_alive():
        await db.close()


async def run_migrations():
    # Add the season column before the repeatable SQL migration rebuilds this
    # table. Existing rows remain unassigned until a season is started.
    async with db.execute("PRAGMA table_info(sail_credit_log)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "season_id" not in columns:
        print("Migration: adding season_id to sail_credit_log...")
        await db.execute("ALTER TABLE sail_credit_log ADD COLUMN season_id INTEGER")
        await db.commit()

    with open("migrations.sql", "r") as f:
        script = f.read()
    await db.executescript(script)
    await db.commit()

    async with db.execute("PRAGMA table_info(sail_credit_log)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "season_id" not in columns:
        raise RuntimeError(
            "Database migration failed: sail_credit_log has no season_id column"
        )


async def create_user(discord_id: int) -> Dict[str, Any]:
    await db.execute(
        f"INSERT INTO users (discord_id, sail_credit) VALUES (?, {party.STARTING_SSC})",
        (discord_id,),
    )
    async with db.execute(
        "SELECT season_id FROM seasons WHERE status = 'ACTIVE' ORDER BY season_id DESC LIMIT 1"
    ) as cursor:
        active_season = await cursor.fetchone()
    if active_season:
        await db.execute(
            "INSERT INTO user_season_stats (discord_id, season_id, highest_sail_credit) VALUES (?, ?, ?)",
            (discord_id, active_season["season_id"], party.STARTING_SSC),
        )
    await db.commit()
    return {"discord_id": discord_id, "sail_credit": party.STARTING_SSC}


async def get_user(discord_id: int) -> Optional[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None

        return row


async def set_user(discord_id: int, sail_credit: int) -> None:
    await db.execute(
        "UPDATE users SET sail_credit = ? WHERE discord_id = ?",
        (sail_credit, discord_id),
    )
    await db.commit()


async def get_user_sail_credit_log(
    discord_id: int, start_timestamp: int, source: Optional[str] = "PARTY"
) -> List[Dict[str, Any]]:

    source_clause = ""
    if source:
        source_clause = "AND source = ?"
    async with db.execute(
        f"SELECT * FROM sail_credit_log WHERE discord_id = ? AND timestamp > ? {source_clause} ORDER BY timestamp DESC",
        (
            (discord_id, start_timestamp)
            if not source
            else (discord_id, start_timestamp, source)
        ),
    ) as cursor:
        rows = await cursor.fetchall()
        return rows


async def change_and_log_sail_credit(
    discord_id: int,
    party_size: int,
    party_created_at: int,
    party_finished_at: int,
    old_ssc: int,
    new_ssc: int,
    source: str = "PARTY",
    timestamp: Optional[int] = None,
) -> None:
    if not timestamp:
        timestamp = int(time.time())
    async with db.execute(
        "SELECT season_id FROM seasons WHERE status = 'ACTIVE' ORDER BY season_id DESC LIMIT 1"
    ) as cursor:
        active_season = await cursor.fetchone()
    season_id = active_season["season_id"] if active_season else None

    await db.execute(
        "INSERT INTO sail_credit_log (discord_id, season_id, party_size, party_created_at, party_finished_at, prev_sail_credit, new_sail_credit, source, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            discord_id,
            season_id,
            party_size,
            party_created_at,
            party_finished_at,
            old_ssc,
            new_ssc,
            source,
            timestamp,
        ),
    )
    await db.execute(
        "UPDATE users SET sail_credit = ? WHERE discord_id = ?", (new_ssc, discord_id)
    )
    if season_id is not None:
        await db.execute(
            "INSERT INTO user_season_stats (discord_id, season_id, highest_sail_credit) VALUES (?, ?, ?) "
            "ON CONFLICT(discord_id, season_id) DO UPDATE SET highest_sail_credit = MAX(highest_sail_credit, excluded.highest_sail_credit)",
            (discord_id, season_id, new_ssc),
        )
    await db.commit()


async def create_new_season(start_timestamp: int) -> int:
    """Close the active season, snapshot its ranks, and reset users atomically."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute(
            "SELECT season_id FROM seasons WHERE status = 'ACTIVE' ORDER BY season_id DESC LIMIT 1"
        ) as cursor:
            active_season = await cursor.fetchone()

        async with db.execute(
            "SELECT 1 FROM seasons WHERE season_id = 0 LIMIT 1"
        ) as cursor:
            season_zero = await cursor.fetchone()

        if not season_zero:
            # Preserve any balances and logs from before explicit seasons began.
            await db.execute(
                "INSERT OR IGNORE INTO seasons (season_id, start_timestamp, end_timestamp, status) "
                "VALUES (0, COALESCE((SELECT MIN(timestamp) FROM sail_credit_log WHERE season_id IS NULL), ?), ?, 'COMPLETED')",
                (start_timestamp, start_timestamp),
            )
            await db.execute(
                "INSERT OR IGNORE INTO user_season_stats (discord_id, season_id, highest_sail_credit) "
                "SELECT u.discord_id, 0, MAX(u.sail_credit, "
                "COALESCE((SELECT MAX(prev_sail_credit) FROM sail_credit_log WHERE discord_id = u.discord_id AND season_id IS NULL), u.sail_credit), "
                "COALESCE((SELECT MAX(new_sail_credit) FROM sail_credit_log WHERE discord_id = u.discord_id AND season_id IS NULL), u.sail_credit)) "
                "FROM users AS u"
            )
            await db.execute(
                "UPDATE sail_credit_log SET season_id = 0 WHERE season_id IS NULL"
            )
            await db.execute(
                "UPDATE user_season_stats SET highest_rank = ("
                "SELECT 1 + COUNT(*) FROM user_season_stats AS other "
                "WHERE other.season_id = user_season_stats.season_id "
                "AND other.highest_sail_credit > user_season_stats.highest_sail_credit"
                ") WHERE season_id = 0"
            )

        if active_season:
            old_season_id = active_season["season_id"]
            await db.execute(
                "UPDATE user_season_stats SET highest_rank = ("
                "SELECT 1 + COUNT(*) FROM user_season_stats AS other "
                "WHERE other.season_id = user_season_stats.season_id "
                "AND other.highest_sail_credit > user_season_stats.highest_sail_credit"
                ") WHERE season_id = ?",
                (old_season_id,),
            )
            await db.execute(
                "UPDATE seasons SET status = 'COMPLETED', end_timestamp = ? WHERE season_id = ?",
                (start_timestamp, old_season_id),
            )
        async with db.execute(
            "SELECT COALESCE(MAX(season_id), 0) + 1 AS next_id FROM seasons"
        ) as cursor:
            row = await cursor.fetchone()
            new_season_id = row["next_id"]

        await db.execute(
            "INSERT INTO seasons (season_id, start_timestamp, status) VALUES (?, ?, 'ACTIVE')",
            (new_season_id, start_timestamp),
        )
        await db.execute("UPDATE users SET sail_credit = ?", (party.STARTING_SSC,))
        await db.execute(
            "INSERT INTO user_season_stats (discord_id, season_id, highest_sail_credit) "
            "SELECT discord_id, ?, ? FROM users",
            (new_season_id, party.STARTING_SSC),
        )
        await db.commit()
        return new_season_id
    except Exception:
        await db.rollback()
        raise


async def log_convict_reason(discord_id: int, reason: str) -> None:
    now = int(time.time())
    await db.execute(
        "INSERT INTO conviction_log VALUES (?, ?, ?)", (discord_id, reason, now)
    )
    await db.commit()


async def get_all_users() -> List[Dict[str, Any]]:
    async with db.execute("SELECT * FROM users") as cursor:
        rows = await cursor.fetchall()
        return rows


async def get_sail_credit_logs() -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM sail_credit_log ORDER BY timestamp ASC"
    ) as cursor:
        rows = await cursor.fetchall()
        return rows


async def clear_sail_credit_logs() -> None:
    await db.execute("DELETE FROM sail_credit_log")
    await db.commit()


async def get_ssc_leaderboard() -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT discord_id, sail_credit FROM users ORDER BY sail_credit DESC"
    ) as cursor:
        rows = await cursor.fetchall()
        return rows


async def get_user_season_history(discord_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT s.season_id, s.start_timestamp, s.end_timestamp, s.status, "
        "stats.highest_sail_credit, "
        "CASE WHEN s.status = 'ACTIVE' THEN ("
        "  SELECT 1 + COUNT(*) FROM user_season_stats AS other "
        "  WHERE other.season_id = stats.season_id "
        "    AND other.highest_sail_credit > stats.highest_sail_credit"
        ") ELSE stats.highest_rank END AS highest_rank "
        "FROM user_season_stats AS stats "
        "JOIN seasons AS s ON s.season_id = stats.season_id "
        "WHERE stats.discord_id = ? ORDER BY s.season_id DESC",
        (discord_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_season_leaderboard(season_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT stats.discord_id, stats.highest_sail_credit, "
        "s.end_timestamp, s.status, "
        "1 + (SELECT COUNT(*) FROM user_season_stats AS other "
        "WHERE other.season_id = stats.season_id "
        "AND other.highest_sail_credit > stats.highest_sail_credit) AS season_rank "
        "FROM user_season_stats AS stats "
        "JOIN seasons AS s ON s.season_id = stats.season_id "
        "WHERE stats.season_id = ? "
        "ORDER BY stats.highest_sail_credit DESC, stats.discord_id ASC",
        (season_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_conviction_log(discord_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if not discord_id:
        async with db.execute(
            "SELECT discord_id, reason, timestamp FROM conviction_log ORDER BY timestamp DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return rows

    async with db.execute(
        "SELECT discord_id, reason, timestamp FROM conviction_log WHERE discord_id = ? ORDER BY timestamp DESC",
        (discord_id,),
    ) as cursor:
        rows = await cursor.fetchall()
        return rows


async def update_role_image_url(role_id: int, image_url: Optional[str]) -> None:
    if image_url is None:
        await db.execute("DELETE FROM role_images WHERE role_id = ?", (role_id,))
    else:
        await db.execute(
            "INSERT OR REPLACE INTO role_images VALUES (?, ?)", (role_id, image_url)
        )
    await db.commit()


async def get_role_image_url(role_id: int) -> Optional[str]:
    async with db.execute(
        "SELECT image_url FROM role_images WHERE role_id = ?", (role_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None

        return row["image_url"]


async def create_casino_lobby_log(
    uuid: str, start_time: int, end_time: int, metadata: Dict[str, Any], game: str
):
    await db.execute(
        "INSERT INTO casino_lobby_log VALUES (?, ?, ?, ?, ?)",
        (uuid, start_time, end_time, json.dumps(metadata).encode(), game),
    )
    await db.commit()


async def get_casino_lobby_logs(game: str, limit: int = 10) -> Dict:
    async with db.execute(
        "SELECT * FROM casino_lobby_log WHERE game = ? ORDER BY start_time DESC LIMIT ?",
        (game, limit),
    ) as cursor:
        rows = await cursor.fetchall()
        return rows


def get_reset_time(timestamp: int) -> int:
    """
    Returns the day's reset timestamp of the given timestamp.
    """

    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    )
    reset_time = dt.replace(hour=8, minute=0, second=0, microsecond=0)
    return reset_time.timestamp()


def get_reset_day(ts: float):
    """
    Returns the 'reset day' (a plain date) that a timestamp belongs to.
    A reset day starts at 8:00 AM Eastern, so 7:59 AM counts as the previous day.
    """
    EASTERN = ZoneInfo("America/New_York")
    RESET_HOUR = 8
    local = datetime.fromtimestamp(ts, EASTERN)
    if local.hour < RESET_HOUR:
        local -= timedelta(days=1)
    return local.date()


async def get_daily_reward_streak(user_id: int) -> int:
    """
    To be called BEFORE registering today's daily reward.
    """
    async with db.execute(
        "SELECT timestamp FROM sail_credit_log WHERE discord_id = ? AND source = ? ORDER BY timestamp DESC",
        (user_id, "DAILY_SSC"),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return 0

    today = get_reset_day(datetime.now(timezone.utc).timestamp())
    expected = today - timedelta(days=1)  # yesterday's reset day
    streak = 0

    for row in rows:
        day = get_reset_day(row["timestamp"])
        if day > expected:
            continue  # today's claim or duplicate entries
        elif day == expected:
            streak += 1
            expected -= timedelta(days=1)
        else:
            break  # gap in the streak

    return streak


# --- VCT Betting ---


async def create_vct_bet(
    discord_id: int,
    match_id: int,
    team1_name: str,
    team2_name: str,
    team_pick: str,
    amount: int,
    odds_at_bet: float,
    potential_payout: float,
    placed_at: int,
) -> int:
    cursor = await db.execute(
        """INSERT INTO vct_bets
            (discord_id, match_id, team1_name, team2_name, team_pick,
             amount, odds_at_bet, potential_payout, status, placed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            discord_id,
            match_id,
            team1_name,
            team2_name,
            team_pick,
            amount,
            odds_at_bet,
            potential_payout,
            placed_at,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_user_vct_bets(discord_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM vct_bets WHERE discord_id = ? ORDER BY placed_at DESC",
        (discord_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_pending_vct_bets(match_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM vct_bets WHERE match_id = ? AND status = 'pending'",
        (match_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_matches_with_pending_bets() -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT DISTINCT match_id FROM vct_bets WHERE status = 'pending'"
    ) as cursor:
        return await cursor.fetchall()


async def resolve_vct_bet(bet_id: int, status: str, resolved_at: int) -> None:
    await db.execute(
        "UPDATE vct_bets SET status = ?, resolved_at = ? WHERE id = ?",
        (status, resolved_at, bet_id),
    )
    await db.commit()


async def get_vct_betting_leaderboard() -> List[Dict[str, Any]]:
    """Get users ranked by net VCT betting profit."""
    async with db.execute(
        """SELECT
               discord_id,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) as total_won,
               SUM(amount) as total_wagered,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) - SUM(amount) as net_profit,
               COUNT(*) as total_bets,
               SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as losses
           FROM vct_bets
           WHERE status IN ('won', 'lost')
           GROUP BY discord_id
           ORDER BY net_profit DESC
        """
    ) as cursor:
        return await cursor.fetchall()


async def get_user_pending_bets_for_match(
    discord_id: int, match_id: int
) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM vct_bets WHERE discord_id = ? AND match_id = ? AND status = 'pending'",
        (discord_id, match_id),
    ) as cursor:
        return await cursor.fetchall()


# --- LoL Betting ---


async def create_lol_bet(
    discord_id: int,
    match_id: int,
    team1_name: str,
    team2_name: str,
    team_pick: str,
    amount: int,
    odds_at_bet: float,
    potential_payout: float,
    placed_at: int,
) -> int:
    cursor = await db.execute(
        """INSERT INTO lol_bets
            (discord_id, match_id, team1_name, team2_name, team_pick,
             amount, odds_at_bet, potential_payout, status, placed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            discord_id,
            match_id,
            team1_name,
            team2_name,
            team_pick,
            amount,
            odds_at_bet,
            potential_payout,
            placed_at,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_user_lol_bets(discord_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM lol_bets WHERE discord_id = ? ORDER BY placed_at DESC",
        (discord_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_pending_lol_bets(match_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM lol_bets WHERE match_id = ? AND status = 'pending'",
        (match_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_lol_matches_with_pending_bets() -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT DISTINCT match_id FROM lol_bets WHERE status = 'pending'"
    ) as cursor:
        return await cursor.fetchall()


async def resolve_lol_bet(bet_id: int, status: str, resolved_at: int) -> None:
    await db.execute(
        "UPDATE lol_bets SET status = ?, resolved_at = ? WHERE id = ?",
        (status, resolved_at, bet_id),
    )
    await db.commit()


async def get_lol_betting_leaderboard() -> List[Dict[str, Any]]:
    """Get users ranked by net LoL betting profit."""
    async with db.execute(
        """SELECT
               discord_id,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) as total_won,
               SUM(amount) as total_wagered,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) - SUM(amount) as net_profit,
               COUNT(*) as total_bets,
               SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as losses
           FROM lol_bets
           WHERE status IN ('won', 'lost')
           GROUP BY discord_id
           ORDER BY net_profit DESC
        """
    ) as cursor:
        return await cursor.fetchall()


async def get_user_pending_lol_bets_for_match(
    discord_id: int, match_id: int
) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM lol_bets WHERE discord_id = ? AND match_id = ? AND status = 'pending'",
        (discord_id, match_id),
    ) as cursor:
        return await cursor.fetchall()


# --- Ranked Betting (Valorant, League) ---


async def link_riot_account(
    discord_id: int,
    name: str,
    tag: str,
    valorant_puuid: Optional[str],
    valorant_region: Optional[str],
    league_puuid: Optional[str],
    league_region: Optional[str],
    linked_at: int,
) -> int:
    cursor = await db.execute(
        """INSERT INTO riot_accounts
            (discord_id, name, tag, valorant_puuid, valorant_region, league_puuid, league_region, linked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (discord_id, name, tag, valorant_puuid, valorant_region, league_puuid, league_region, linked_at),
    )
    await db.commit()
    return cursor.lastrowid


async def update_riot_account(
    account_id: int,
    name: str,
    tag: str,
    valorant_puuid: Optional[str],
    valorant_region: Optional[str],
    league_puuid: Optional[str],
    league_region: Optional[str],
) -> None:
    """Refresh a linked account, e.g. after a name change or after it starts playing the other game."""
    await db.execute(
        """UPDATE riot_accounts SET name = ?, tag = ?,
               valorant_puuid = COALESCE(?, valorant_puuid), valorant_region = COALESCE(?, valorant_region),
               league_puuid = COALESCE(?, league_puuid), league_region = COALESCE(?, league_region)
           WHERE id = ?""",
        (name, tag, valorant_puuid, valorant_region, league_puuid, league_region, account_id),
    )
    await db.commit()


async def unlink_riot_account(account_id: int) -> None:
    await db.execute("DELETE FROM riot_accounts WHERE id = ?", (account_id,))
    await db.commit()


async def get_riot_account(account_id: int) -> Optional[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM riot_accounts WHERE id = ?", (account_id,)
    ) as cursor:
        return await cursor.fetchone()


async def find_riot_account_by_puuid(
    valorant_puuid: Optional[str], league_puuid: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Find an already-linked account by either game's ID."""
    async with db.execute(
        "SELECT * FROM riot_accounts WHERE valorant_puuid = ? OR league_puuid = ?",
        (valorant_puuid, league_puuid),
    ) as cursor:
        return await cursor.fetchone()


async def get_user_riot_accounts(discord_id: int) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM riot_accounts WHERE discord_id = ? ORDER BY linked_at",
        (discord_id,),
    ) as cursor:
        return await cursor.fetchall()


async def get_all_riot_accounts(game: Optional[str] = None) -> List[Dict[str, Any]]:
    """All linked accounts, or only those that have played `game` ("valorant" or "league")."""
    query = "SELECT * FROM riot_accounts"
    if game in ("valorant", "league"):
        query += f" WHERE {game}_puuid IS NOT NULL"
    async with db.execute(query + " ORDER BY discord_id, linked_at") as cursor:
        return await cursor.fetchall()


async def create_ranked_bet(
    discord_id: int,
    target_discord_id: int,
    game: str,
    mode: str,
    puuid: str,
    riot_id: str,
    region: str,
    pick: str,
    amount: int,
    odds_at_bet: float,
    potential_payout: float,
    placed_at: int,
    counts_from: int,
    channel_id: Optional[int],
) -> int:
    cursor = await db.execute(
        """INSERT INTO ranked_bets
            (discord_id, target_discord_id, game, mode, puuid, riot_id, region, pick, amount,
             odds_at_bet, potential_payout, status, placed_at, counts_from, channel_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            discord_id,
            target_discord_id,
            game,
            mode,
            puuid,
            riot_id,
            region,
            pick,
            amount,
            odds_at_bet,
            potential_payout,
            placed_at,
            counts_from,
            channel_id,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_user_ranked_bets(discord_id: int, game: str) -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM ranked_bets WHERE discord_id = ? AND game = ? ORDER BY placed_at DESC",
        (discord_id, game),
    ) as cursor:
        return await cursor.fetchall()


async def get_pending_ranked_bets() -> List[Dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM ranked_bets WHERE status = 'pending' ORDER BY placed_at"
    ) as cursor:
        return await cursor.fetchall()


async def get_user_pending_ranked_bets(
    discord_id: int, game: str, puuid: str, mode: str
) -> List[Dict[str, Any]]:
    async with db.execute(
        """SELECT * FROM ranked_bets
           WHERE discord_id = ? AND game = ? AND puuid = ? AND mode = ? AND status = 'pending'""",
        (discord_id, game, puuid, mode),
    ) as cursor:
        return await cursor.fetchall()


async def resolve_ranked_bet(
    bet_id: int, status: str, resolved_at: int, match_id: Optional[str]
) -> None:
    await db.execute(
        "UPDATE ranked_bets SET status = ?, resolved_at = ?, match_id = ? WHERE id = ?",
        (status, resolved_at, match_id, bet_id),
    )
    await db.commit()


async def get_ranked_betting_leaderboard(game: str) -> List[Dict[str, Any]]:
    """Get users ranked by net ranked betting profit for one game."""
    async with db.execute(
        """SELECT
               discord_id,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) as total_won,
               SUM(amount) as total_wagered,
               SUM(CASE WHEN status = 'won' THEN potential_payout ELSE 0 END) - SUM(amount) as net_profit,
               COUNT(*) as total_bets,
               SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as losses
           FROM ranked_bets
           WHERE game = ? AND status IN ('won', 'lost')
           GROUP BY discord_id
           ORDER BY net_profit DESC
        """,
        (game,),
    ) as cursor:
        return await cursor.fetchall()
