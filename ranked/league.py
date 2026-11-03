import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from ranked.models import (
    GameAccount,
    GameResult,
    RankedGameClient,
    RankedMode,
    RankedRecord,
    RateLimiter,
    RecentGame,
)

RIOT_TOKEN_FILE = "riot_token"
# Riot's app-wide limits for a development/personal key are 20 per second and 100 per 2 minutes.
# Stay a little under both.
RIOT_RATE_LIMITS = [(18, 1), (90, 120)]

# mode key -> (match-v5 queue ID, league-v4 queueType)
LEAGUE_QUEUES = {
    "solo": (420, "RANKED_SOLO_5x5"),
    "flex": (440, "RANKED_FLEX_SR"),
}

# Platform (where the account plays, e.g. na1) -> regional routing host for account and match data
PLATFORM_TO_REGION = {
    "na1": "americas",
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "euw1": "europe",
    "eun1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "me1": "europe",
    "kr": "asia",
    "jp1": "asia",
    "oc1": "sea",
    "sg2": "sea",
    "tw2": "sea",
    "vn2": "sea",
}


def load_riot_key() -> Optional[str]:
    """Read the Riot API key from the RIOT_API_KEY env var or the gitignored riot_token file."""
    key = os.environ.get("RIOT_API_KEY")
    if key:
        return key.strip()
    try:
        with open(RIOT_TOKEN_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _region_for(platform: str) -> str:
    return PLATFORM_TO_REGION.get(platform, "americas")


class LeagueClient(RankedGameClient):
    """League ranked data from the official Riot API."""

    game_key = "league"
    game_name = "League"
    modes = [RankedMode("solo", "Ranked Solo/Duo"), RankedMode("flex", "Ranked Flex")]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rate_limiter = RateLimiter(RIOT_RATE_LIMITS)

    async def _get(
        self, host: str, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        """GET a Riot API endpoint. Returns the JSON body, or None on any error (including 404)."""
        await self.rate_limiter.wait()
        url = f"https://{host}.api.riotgames.com{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers={"X-Riot-Token": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status != 404:
                        print(f"Riot API: got status {resp.status} for {url}")
                    return None
        except Exception as e:
            print(f"Riot API: error fetching {url}: {e}")
            return None

    async def get_account(self, name: str, tag: str) -> Optional[GameAccount]:
        # The account lookup works from any regional host. Then find which server they play League on.
        account = await self._get(
            "americas", f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}"
        )
        if not account:
            return None
        shard = await self._get(
            "americas", f"/riot/account/v1/region/by-game/lol/by-puuid/{account['puuid']}"
        )
        if not shard or not shard.get("region"):
            # Riot account exists but has never played League
            return None
        return GameAccount(
            puuid=account["puuid"],
            name=account["gameName"],
            tag=account["tagLine"],
            region=shard["region"],
        )

    async def get_record(self, puuid: str, region: str, mode: str) -> Optional[RankedRecord]:
        """Wins and games in this queue for the current split. Unranked this split gets 0/0."""
        entries = await self._get(region, f"/lol/league/v4/entries/by-puuid/{puuid}")
        if entries is None:
            return None
        queue_type = LEAGUE_QUEUES[mode][1]
        for entry in entries:
            if entry.get("queueType") == queue_type:
                wins = entry.get("wins") or 0
                losses = entry.get("losses") or 0
                tier = (entry.get("tier") or "").title()
                # Master and above have no divisions
                division = "" if tier in ("Master", "Grandmaster", "Challenger") else f" {entry.get('rank', '')}"
                return RankedRecord(
                    wins=wins,
                    games=wins + losses,
                    rank=f"{tier}{division} {entry.get('leaguePoints', 0)} LP",
                )
        return RankedRecord(wins=0, games=0, rank="Unranked")

    async def get_recent_games(
        self, puuid: str, region: str, mode: str, since: int
    ) -> Optional[List[RecentGame]]:
        """
        Match IDs in this queue since a time. Riot's filter isn't exact to the second, so the poll
        still checks each game's real start time from its result.
        """
        match_ids = await self._get(
            _region_for(region),
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            {"queue": LEAGUE_QUEUES[mode][0], "startTime": since - 3600, "count": 10},
        )
        if match_ids is None:
            return None
        # Start times aren't in this response. 0 means "unknown, check the result".
        return [RecentGame(match_id=m, started_at=0) for m in match_ids]

    async def get_game_result(self, match_id: str, region: str) -> Optional[GameResult]:
        data = await self._get(_region_for(region), f"/lol/match/v5/matches/{match_id}")
        if not data:
            return None
        info = data.get("info") or {}
        participants = info.get("participants") or []
        if not participants or not info.get("gameStartTimestamp"):
            return None

        # A remake ends the game early for everyone. Treat it like a draw so bets get refunded.
        remake = any(p.get("gameEndedInEarlySurrender") for p in participants)

        results = {}
        details = {}
        for p in participants:
            results[p["puuid"]] = "draw" if remake else "win" if p.get("win") else "loss"
            details[p["puuid"]] = (
                f"{p.get('championName', '?')} {p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}"
            )

        return GameResult(
            match_id=match_id,
            started_at=info["gameStartTimestamp"] // 1000,
            map_name="",
            results=results,
            details=details,
        )

    async def get_live_game_start(self, puuid: str, region: str, mode: str) -> Optional[int]:
        """When the account's current game started, if they're in a game of this queue right now."""
        game = await self._get(region, f"/lol/spectator/v5/active-games/by-summoner/{puuid}")
        if not game or game.get("gameQueueConfigId") != LEAGUE_QUEUES[mode][0]:
            return None
        start = game.get("gameStartTime")
        # gameStartTime is 0 while the game is still loading in. It's still a live game.
        return start // 1000 if start else int(time.time())
