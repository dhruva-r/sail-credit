import os
import time
from datetime import datetime
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

HENRIK_API_URL = "https://api.henrikdev.xyz/valorant"
HENRIK_TOKEN_FILE = "henrik_token"
# HenrikDev's basic key allows 30 requests per minute. Stay a little under it.
HENRIK_RATE_LIMITS = [(27, 60)]


def load_henrik_key() -> Optional[str]:
    """Read the HenrikDev API key from the HENRIK_API_KEY env var or the gitignored henrik_token file."""
    key = os.environ.get("HENRIK_API_KEY")
    if key:
        return key.strip()
    try:
        with open(HENRIK_TOKEN_FILE, "r") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class ValorantClient(RankedGameClient):
    """Valorant ranked data from the HenrikDev API (Riot doesn't offer Valorant match data to small apps)."""

    game_key = "valorant"
    game_name = "Valorant"
    modes = [RankedMode("competitive", "Competitive")]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rate_limiter = RateLimiter(HENRIK_RATE_LIMITS)
        self._current_act_id: Optional[str] = None
        self._current_act_checked_at = 0.0

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """GET a HenrikDev endpoint. Returns the "data" field, or None on any error."""
        await self.rate_limiter.wait()
        url = f"{HENRIK_API_URL}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers={"Authorization": self.api_key},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        return (await resp.json()).get("data")
                    if resp.status != 404:
                        print(f"HenrikDev: got status {resp.status} for {url}")
                    return None
        except Exception as e:
            print(f"HenrikDev: error fetching {url}: {e}")
            return None

    async def get_account(self, name: str, tag: str) -> Optional[GameAccount]:
        data = await self._get(f"/v2/account/{quote(name)}/{quote(tag)}")
        if not data:
            return None
        return GameAccount(
            puuid=data["puuid"],
            name=data["name"],
            tag=data["tag"],
            region=data["region"],
        )

    async def get_recent_games(
        self, puuid: str, region: str, mode: str, since: int
    ) -> Optional[List[RecentGame]]:
        """
        Recent competitive games from the MMR history. It's a small response, so it's what the poll
        uses to spot new games. It covers the last ~10 games, which is plenty for a 24 hour bet window.
        """
        data = await self._get(f"/v2/by-puuid/mmr-history/{region}/pc/{puuid}")
        if data is None:
            return None
        games = []
        for entry in data.get("history") or []:
            try:
                games.append(RecentGame(match_id=entry["match_id"], started_at=_parse_time(entry["date"])))
            except (KeyError, ValueError):
                continue
        return games

    async def get_game_result(self, match_id: str, region: str) -> Optional[GameResult]:
        data = await self._get(f"/v4/match/{region}/{match_id}")
        if not data:
            return None
        metadata = data.get("metadata") or {}
        if not metadata.get("is_completed"):
            return None

        team_rounds = {
            team["team_id"]: (team["rounds"]["won"], team["rounds"]["lost"])
            for team in data.get("teams") or []
        }

        results = {}
        scores = {}
        for player in data.get("players") or []:
            rounds = team_rounds.get(player.get("team_id"))
            if not rounds:
                continue
            won, lost = rounds
            results[player["puuid"]] = "win" if won > lost else "loss" if won < lost else "draw"
            scores[player["puuid"]] = rounds

        return GameResult(
            match_id=match_id,
            started_at=_parse_time(metadata["started_at"]),
            map_name=(metadata.get("map") or {}).get("name", ""),
            results=results,
            scores=scores,
        )

    async def _get_current_act_id(self) -> Optional[str]:
        """The ID of the act that's live right now. Cached for 6 hours since it rarely changes."""
        if self._current_act_id and time.monotonic() - self._current_act_checked_at < 6 * 3600:
            return self._current_act_id
        data = await self._get("/v1/content")
        if data:
            for act in data.get("acts") or []:
                if act.get("isActive") and act.get("type") == "act":
                    self._current_act_id = act["id"]
                    self._current_act_checked_at = time.monotonic()
                    break
        return self._current_act_id

    async def get_record(self, puuid: str, region: str, mode: str) -> Optional[RankedRecord]:
        """Competitive wins and games this act. An account that hasn't played this act gets 0/0."""
        act_id = await self._get_current_act_id()
        data = await self._get(f"/v3/by-puuid/mmr/{region}/pc/{puuid}")
        if data is None:
            return None

        rank = ((data.get("current") or {}).get("tier") or {}).get("name") or "Unrated"
        for season in data.get("seasonal") or []:
            if act_id and (season.get("season") or {}).get("id") == act_id:
                return RankedRecord(
                    wins=season.get("wins") or 0,
                    games=season.get("games") or 0,
                    rank=rank,
                )
        return RankedRecord(wins=0, games=0, rank=rank)
