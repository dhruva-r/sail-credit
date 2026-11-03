import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db
from ranked.models import GameResult, RankedGameClient, RankedRecord

# How often to check accounts with pending bets for a finished game (in seconds)
POLL_INTERVAL_SECONDS = 120
# Bets with no qualifying game after this long are refunded
BET_EXPIRY_SECONDS = 24 * 3600
MIN_BET = 10
MAX_LINKED_ACCOUNTS = 5
# /refresh checks the API right away, so each user can only use it this often (in seconds)
REFRESH_COOLDOWN_SECONDS = 60

# Odds are based on the account's win rate in the mode this act/split, smoothed toward 50%
# as if every account had already played PRIOR_GAMES games at an even record. A new account
# gets exactly even odds, and a couple of games can't swing the odds wildly.
PRIOR_GAMES = 10
MIN_ODDS = 1.2
MAX_ODDS = 5.0


@dataclass
class Odds:
    win_odds: float
    loss_odds: float
    record: Optional[RankedRecord]


@dataclass
class BetResolution:
    """Posted to the channel the bet was placed in once a bet resolves."""

    game_name: str
    mode_name: str
    channel_id: Optional[int]
    bettor_id: int
    target_id: int
    riot_id: str
    pick: str
    amount: int
    status: str  # "won", "lost", "refunded"
    payout: int
    outcome: str = ""  # "win", "loss" or "draw" for the game itself
    map_name: str = ""
    score: Optional[Tuple[int, int]] = None
    detail: str = ""
    reason: str = ""


def calculate_odds(record: Optional[RankedRecord]) -> Odds:
    """Fair decimal odds (1 / win chance) for a win and a loss bet, from the ranked record."""
    wins = record.wins if record else 0
    games = record.games if record else 0
    win_chance = (wins + PRIOR_GAMES / 2) / (games + PRIOR_GAMES)

    def to_odds(chance: float) -> float:
        return round(min(MAX_ODDS, max(MIN_ODDS, 1 / chance)), 2)

    return Odds(
        win_odds=to_odds(win_chance),
        loss_odds=to_odds(1 - win_chance),
        record=record,
    )


class RankedBettingService:
    """Bets on linked accounts' ranked games, for every game client given (Valorant, League)."""

    def __init__(self, clients: List[RankedGameClient]):
        self.clients: Dict[str, RankedGameClient] = {c.game_key: c for c in clients}
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        # Called with each BetResolution so the bot can announce it
        self.on_resolution: Optional[Callable] = None
        self._check_lock = asyncio.Lock()
        # discord_id -> when they last used /refresh
        self._last_refresh: Dict[int, float] = {}

    async def start(self):
        self.scheduler.add_job(
            self._poll,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="ranked_poll",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
            # A slow poll (rate limits) shouldn't overlap with the next one
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        print(f"Ranked Betting Service started for: {', '.join(self.clients)}")

    def client(self, game: str) -> Optional[RankedGameClient]:
        return self.clients.get(game)

    async def get_odds(self, game: str, puuid: str, region: str, mode: str) -> Odds:
        """Current odds for an account in a mode. Falls back to even odds if the record can't be loaded."""
        record = await self.clients[game].get_record(puuid, region, mode)
        return calculate_odds(record)

    async def place_bet(
        self,
        discord_id: int,
        game: str,
        mode: str,
        account: Dict,
        pick: str,
        amount: int,
        channel_id: Optional[int],
    ) -> Optional[Dict]:
        """
        Place a bet on an account's next ranked game in a mode. Odds are re-fetched and locked here.
        The caller is responsible for the other checks (balance, existing bets).
        """
        client = self.clients.get(game)
        if not client or not client.mode(mode) or pick not in ("win", "loss") or amount < MIN_BET:
            return None

        puuid = account[f"{game}_puuid"]
        region = account[f"{game}_region"]
        if not puuid:
            return None

        odds_info = await self.get_odds(game, puuid, region, mode)
        odds = odds_info.win_odds if pick == "win" else odds_info.loss_odds
        potential_payout = round(amount * odds, 2)

        # The bet counts on the first game that starts after the bet. A game already in progress
        # started before now, so it never counts. Checking for one just lets us tell the bettor.
        now = int(time.time())
        counts_from = now
        in_game = await client.get_live_game_start(puuid, region, mode) is not None

        user = await db.get_user(discord_id)
        if not user or user["sail_credit"] < amount:
            return None

        current_ssc = user["sail_credit"]
        await db.change_and_log_sail_credit(
            discord_id, -1, -1, -1, current_ssc, current_ssc - amount, "RANKED_DEBIT"
        )

        bet_id = await db.create_ranked_bet(
            discord_id=discord_id,
            target_discord_id=account["discord_id"],
            game=game,
            mode=mode,
            puuid=puuid,
            riot_id=f"{account['name']}#{account['tag']}",
            region=region,
            pick=pick,
            amount=amount,
            odds_at_bet=odds,
            potential_payout=potential_payout,
            placed_at=now,
            counts_from=counts_from,
            channel_id=channel_id,
        )
        return {
            "id": bet_id,
            "odds_at_bet": odds,
            "potential_payout": potential_payout,
            "new_balance": current_ssc - amount,
            "waiting_for_next_game": in_game,
        }

    async def _poll(self):
        """Check every pending bet. Runs on a timer."""
        try:
            pending = await db.get_pending_ranked_bets()
            if not pending:
                return
            accounts = await self.check_bets(pending)
            print(f"Ranked poll: checked {accounts} account(s) with {len(pending)} pending bet(s)")
        except Exception as e:
            print(f"Ranked poll error: {e}")

    def refresh_cooldown(self, discord_id: int) -> int:
        """Seconds until this user can use /refresh again, or 0 if they can now."""
        last = self._last_refresh.get(discord_id, 0)
        return max(0, int(last + REFRESH_COOLDOWN_SECONDS - time.monotonic()))

    async def refresh_user_bets(self, discord_id: int, game: str) -> Optional[Dict[str, int]]:
        """
        Check one user's pending bets right now instead of waiting for the poll.
        Returns how many resolved and how many are still pending, or None if on cooldown.
        """
        if self.refresh_cooldown(discord_id):
            return None
        self._last_refresh[discord_id] = time.monotonic()

        pending = await db.get_user_pending_ranked_bets_for_game(discord_id, game)
        if pending:
            await self.check_bets(pending)
        still_pending = await db.get_user_pending_ranked_bets_for_game(discord_id, game)
        return {
            "checked": len(pending),
            "resolved": len(pending) - len(still_pending),
            "pending": len(still_pending),
        }

    async def check_bets(self, pending: List[Dict]) -> int:
        """
        Resolve any of these bets whose game has finished. Only one check runs at a time, so a
        manual refresh waits for an in-progress poll instead of doubling the API calls.
        Returns how many accounts were checked.
        """
        async with self._check_lock:
            # Another check may have resolved some of these while this one waited for the lock
            still_pending = {b["id"] for b in await db.get_pending_ranked_bets()}
            bets_by_account = defaultdict(list)
            for bet in pending:
                if bet["id"] not in still_pending:
                    continue
                bets_by_account[(bet["game"], bet["puuid"], bet["region"], bet["mode"])].append(bet)

            # Full game results are large, so each game is only fetched once per check,
            # even if several accounts with bets played in it.
            results: Dict[str, Optional[GameResult]] = {}
            now = int(time.time())

            for (game, puuid, region, mode), bets in bets_by_account.items():
                client = self.clients.get(game)
                if not client:
                    continue
                since = min(bet["counts_from"] for bet in bets)
                games = await client.get_recent_games(puuid, region, mode, since)
                if games is None:
                    continue

                for bet in bets:
                    resolved = False
                    # Check games oldest first. Some clients don't know start times until the
                    # result is fetched (0), so those always get checked.
                    for recent in sorted(games, key=lambda g: g.started_at):
                        if recent.started_at and recent.started_at <= bet["counts_from"]:
                            continue
                        if recent.match_id not in results:
                            results[recent.match_id] = await client.get_game_result(
                                recent.match_id, region
                            )
                        result = results[recent.match_id]
                        # Listed but not finished/processed yet. The bet waits for this game.
                        if not result:
                            break
                        # A game only counts if it started after the bet, so nobody can bet on a
                        # game that's already in progress.
                        if result.started_at <= bet["counts_from"] or puuid not in result.results:
                            continue
                        await self._resolve(bet, result, client)
                        resolved = True
                        break

                    if not resolved and now - bet["placed_at"] > BET_EXPIRY_SECONDS:
                        await self._refund(
                            bet, client, f"No {client.mode(bet['mode']).name} game was played within 24 hours."
                        )

            return len(bets_by_account)

    async def _resolve(self, bet: Dict, result: GameResult, client: RankedGameClient):
        outcome = result.results[bet["puuid"]]
        if outcome == "draw":
            reason = "The game was remade." if client.game_key == "league" else "The game ended in a draw."
            await self._refund(bet, client, reason, result)
            return

        won = outcome == bet["pick"]
        payout = int(bet["potential_payout"]) if won else 0
        # Claim the bet before paying, so it can't be paid twice
        if not await db.resolve_ranked_bet(
            bet["id"], "won" if won else "lost", int(time.time()), result.match_id
        ):
            return
        if won:
            user = await db.get_user(bet["discord_id"])
            if user:
                await db.change_and_log_sail_credit(
                    bet["discord_id"],
                    -1,
                    -1,
                    -1,
                    user["sail_credit"],
                    user["sail_credit"] + payout,
                    "RANKED_CREDIT",
                )
        await self._announce(bet, client, "won" if won else "lost", payout, result)

    async def _refund(
        self,
        bet: Dict,
        client: RankedGameClient,
        reason: str,
        result: Optional[GameResult] = None,
    ):
        # Claim the bet before refunding, so it can't be refunded twice
        if not await db.resolve_ranked_bet(
            bet["id"], "refunded", int(time.time()), result.match_id if result else None
        ):
            return
        user = await db.get_user(bet["discord_id"])
        if user:
            await db.change_and_log_sail_credit(
                bet["discord_id"],
                -1,
                -1,
                -1,
                user["sail_credit"],
                user["sail_credit"] + bet["amount"],
                "RANKED_REFUND",
            )
        await self._announce(bet, client, "refunded", bet["amount"], result, reason)

    async def _announce(
        self,
        bet: Dict,
        client: RankedGameClient,
        status: str,
        payout: int,
        result: Optional[GameResult],
        reason: str = "",
    ):
        if not self.on_resolution:
            return
        mode = client.mode(bet["mode"])
        try:
            await self.on_resolution(
                BetResolution(
                    game_name=client.game_name,
                    mode_name=mode.name if mode else bet["mode"],
                    channel_id=bet["channel_id"],
                    bettor_id=bet["discord_id"],
                    target_id=bet["target_discord_id"],
                    riot_id=bet["riot_id"],
                    pick=bet["pick"],
                    amount=bet["amount"],
                    status=status,
                    payout=payout,
                    outcome=result.results.get(bet["puuid"], "") if result else "",
                    map_name=result.map_name if result else "",
                    score=result.scores.get(bet["puuid"]) if result else None,
                    detail=result.details.get(bet["puuid"], "") if result else "",
                    reason=reason,
                )
            )
        except Exception as e:
            print(f"Ranked announce error: {e}")
