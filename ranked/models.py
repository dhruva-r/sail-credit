import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class RankedMode:
    key: str  # stored on bets, e.g. "competitive", "solo", "flex"
    name: str  # shown to users, e.g. "Ranked Solo/Duo"


@dataclass
class GameAccount:
    """An account as seen by one game's API."""

    puuid: str
    name: str
    tag: str
    region: str


@dataclass
class RankedRecord:
    wins: int
    games: int
    rank: str


@dataclass
class RecentGame:
    match_id: str
    started_at: int


@dataclass
class GameResult:
    """The outcome of a finished ranked game, for every player in it."""

    match_id: str
    started_at: int
    # Map for Valorant, champion is per-player so this is left blank for League
    map_name: str
    # puuid -> "win", "loss" or "draw" (Valorant draw or League remake)
    results: Dict[str, str]
    # puuid -> (rounds won, rounds lost) for Valorant. Not set for League.
    scores: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    # puuid -> short per-player summary, e.g. "Bard 2/1/18" for League
    details: Dict[str, str] = field(default_factory=dict)


class SlidingWindowRateLimiter:
    """Allows bursts, but never more than `limit` requests in any `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.sent: deque = deque()

    def _prune(self, now: float):
        while self.sent and now - self.sent[0] >= self.window:
            self.sent.popleft()

    def delay(self, now: float) -> float:
        self._prune(now)
        if len(self.sent) < self.limit:
            return 0.0
        return self.window - (now - self.sent[0])


class RateLimiter:
    """Waits until every one of several sliding windows has room, e.g. 20/1s and 100/120s."""

    def __init__(self, limits: List[Tuple[int, float]]):
        self.windows = [SlidingWindowRateLimiter(limit, window) for limit, window in limits]
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                delay = max(w.delay(now) for w in self.windows)
                if delay <= 0:
                    for w in self.windows:
                        w.sent.append(now)
                    return
                await asyncio.sleep(delay)


class RankedGameClient:
    """What each game (Valorant, League) has to provide for ranked betting."""

    game_key: str  # "valorant" or "league"
    game_name: str  # "Valorant" or "League"
    modes: List[RankedMode]

    async def get_account(self, name: str, tag: str) -> Optional[GameAccount]:
        """Look up an account by Riot ID. None if it doesn't exist or has never played this game."""
        raise NotImplementedError

    async def get_record(self, puuid: str, region: str, mode: str) -> Optional[RankedRecord]:
        """Wins and games in a mode for the current act/split. None if the request failed."""
        raise NotImplementedError

    async def get_recent_games(
        self, puuid: str, region: str, mode: str, since: int
    ) -> Optional[List[RecentGame]]:
        """Recent games in a mode, at least those that started after `since`. None if the request failed."""
        raise NotImplementedError

    async def get_game_result(self, match_id: str, region: str) -> Optional[GameResult]:
        """The result of a finished game. None if it isn't available yet."""
        raise NotImplementedError

    async def get_live_game_start(self, puuid: str, region: str, mode: str) -> Optional[int]:
        """
        If the account is in a game of this mode right now, when it started. None if not, or if the
        game can't tell. Bets placed mid-game then wait for the next game.
        """
        return None

    def mode(self, key: str) -> Optional[RankedMode]:
        return next((m for m in self.modes if m.key == key), None)
