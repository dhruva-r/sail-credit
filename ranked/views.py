from typing import TYPE_CHECKING, Dict, List, Optional

import discord

import db
from util import create_embed, divide_chunks, get_balance

if TYPE_CHECKING:
    from ranked.betting import BetResolution, Odds, RankedBettingService
    from ranked.models import RankedGameClient

GAME_EMOJI = {"valorant": "🎯", "league": "⚔️"}


def _riot_id(account: Dict) -> str:
    return f"{account['name']}#{account['tag']}"


def _record_text(odds: "Odds", mode_name: str) -> str:
    record = odds.record
    if not record:
        return "Couldn't load stats — using even odds"
    if record.games == 0:
        return f"{record.rank} · No {mode_name} games this season yet"
    return (
        f"{record.rank} · {record.wins}W / {record.games} {mode_name} games "
        f"({round(100 * record.wins / record.games)}%)"
    )


class RankedBetView(discord.ui.View):
    """
    Bet slip for ranked games. Pick a linked account, pick a mode (if the game has more than one),
    pick Win or Loss with a button, then enter the amount in a modal.
    """

    def __init__(
        self,
        interaction: discord.Interaction,
        service: "RankedBettingService",
        client: "RankedGameClient",
        accounts: List[Dict],
        min_bet: int,
    ):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.user_id = interaction.user.id
        self.service = service
        self.client = client
        self.game = client.game_key
        self.emoji = GAME_EMOJI.get(self.game, "🎲")
        self.min_bet = min_bet
        self.accounts = {str(a["id"]): a for a in accounts}
        self.balance = get_balance(interaction)
        self.selected: Optional[Dict] = None
        # Games with one ranked mode (Valorant) skip the mode dropdown
        self.mode: Optional[str] = client.modes[0].key if len(client.modes) == 1 else None
        self.odds: Optional["Odds"] = None
        self._build_items()

    @classmethod
    async def send(
        cls,
        interaction: discord.Interaction,
        service: "RankedBettingService",
        game: str,
        min_bet: int,
        player: Optional[discord.Member] = None,
    ):
        client = service.client(game)
        if player:
            accounts = [a for a in await db.get_user_riot_accounts(player.id) if a[f"{game}_puuid"]]
            empty = f"{player.mention} hasn't linked any Riot accounts that play {client.game_name}."
        else:
            accounts = await db.get_all_riot_accounts(game)
            empty = f"Nobody has linked a {client.game_name} account yet! Use `/riot link` first."

        if not accounts:
            await interaction.response.send_message(
                embed=create_embed(message=empty), ephemeral=True
            )
            return

        # Dropdowns are limited to 25 options
        note = ""
        if len(accounts) > 25:
            accounts = accounts[:25]
            note = "\n\nShowing the first 25 accounts. Use the `player` option to find someone specific."

        view = cls(interaction, service, client, accounts, min_bet)
        await interaction.response.send_message(
            embed=view.build_embed(note), view=view, ephemeral=True
        )

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass

    def _owner_name(self, account: Dict) -> str:
        guild = self.interaction.guild
        member = guild.get_member(account["discord_id"]) if guild else None
        return member.display_name if member else ""

    def _build_items(self):
        self.clear_items()

        self.account_select = discord.ui.Select(placeholder="Select an account to bet on...", row=0)
        self.account_select.callback = self.select_account
        for key, account in self.accounts.items():
            owner = self._owner_name(account)
            self.account_select.options.append(
                discord.SelectOption(
                    label=_riot_id(account)[:100],
                    value=key,
                    description=(f"Linked by {owner}" if owner else None),
                    default=self.selected is not None and account["id"] == self.selected["id"],
                )
            )
        self.add_item(self.account_select)

        if self.selected and len(self.client.modes) > 1:
            self.mode_select = discord.ui.Select(placeholder="Select a ranked mode...", row=1)
            self.mode_select.callback = self.select_mode
            for mode in self.client.modes:
                self.mode_select.options.append(
                    discord.SelectOption(label=mode.name, value=mode.key, default=mode.key == self.mode)
                )
            self.add_item(self.mode_select)

        if self.selected and self.mode and self.odds:
            for pick, label, odds, style in (
                ("win", "Win", self.odds.win_odds, discord.ButtonStyle.green),
                ("loss", "Loss", self.odds.loss_odds, discord.ButtonStyle.red),
            ):
                button = discord.ui.Button(label=f"{label} ({odds:.2f}x)", style=style, row=2)
                button.callback = self._pick_callback(pick)
                self.add_item(button)

    def _pick_callback(self, pick: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(
                RankedBetAmountModal(self, self.selected, self.mode, pick)
            )

        return callback

    def build_embed(self, note: str = "") -> discord.Embed:
        title = f"{self.emoji} {self.client.game_name} Bet Slip"
        if not self.selected:
            return create_embed(
                title=title,
                message=(
                    "Pick an account from the dropdown below.\n\n"
                    f"Your balance: **{self.balance} SSC**{note}"
                ),
            )

        account = self.selected
        header = f"Player: <@{account['discord_id']}>"
        if not self.mode or not self.odds:
            return create_embed(
                title=f"{title} — {_riot_id(account)}",
                message=f"{header}\n\nPick a ranked mode below.\n\nYour balance: **{self.balance} SSC**",
            )

        mode_name = self.client.mode(self.mode).name
        return create_embed(
            title=f"{title} — {_riot_id(account)}",
            message=(
                f"{header}\n"
                f"{_record_text(self.odds, mode_name)}\n\n"
                f"**Win** — pays **{self.odds.win_odds:.2f}x**\n"
                f"**Loss** — pays **{self.odds.loss_odds:.2f}x**\n\n"
                f"Your bet counts on this account's next **{mode_name}** game that **starts after** you bet. "
                "If they don't play one within 24 hours, you get refunded.\n\n"
                f"Your balance: **{self.balance} SSC**"
            ),
        )

    async def _refresh_odds(self, interaction: discord.Interaction):
        self.odds = None
        if self.selected and self.mode:
            self.odds = await self.service.get_odds(
                self.game,
                self.selected[f"{self.game}_puuid"],
                self.selected[f"{self.game}_region"],
                self.mode,
            )
        self._build_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def select_account(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected = self.accounts.get(self.account_select.values[0])
        await self._refresh_odds(interaction)

    async def select_mode(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.mode = self.mode_select.values[0]
        await self._refresh_odds(interaction)

    async def place_bet(
        self, interaction: discord.Interaction, account_id: int, mode: str, pick: str, amount: int
    ):
        """Validate and place the bet. The interaction must already be deferred."""

        async def fail(message: str):
            await interaction.followup.send(embed=create_embed(message=message), ephemeral=True)

        # The account may have been unlinked since the slip opened
        account = await db.get_riot_account(account_id)
        if not account or not account[f"{self.game}_puuid"]:
            await fail("That account isn't linked anymore!")
            return

        mode_name = self.client.mode(mode).name

        # Can't bet both ways on the same account's next game
        existing = await db.get_user_pending_ranked_bets(
            self.user_id, self.game, account[f"{self.game}_puuid"], mode
        )
        if existing and existing[0]["pick"] != pick:
            await fail(
                f"You already have a pending **{existing[0]['pick']}** bet on **{_riot_id(account)}**'s "
                f"next {mode_name} game! You can't bet both ways on the same game."
            )
            return

        user = await db.get_user(self.user_id)
        current_ssc = user["sail_credit"]  # pyright: ignore
        if current_ssc < amount:
            await fail(
                f"You don't have enough SSC! You have **{current_ssc} SSC** but tried to bet **{amount} SSC**."
            )
            return

        bet = await self.service.place_bet(
            discord_id=self.user_id,
            game=self.game,
            mode=mode,
            account=account,
            pick=pick,
            amount=amount,
            channel_id=interaction.channel_id,
        )
        if not bet:
            await fail("Something went wrong placing your bet. Please try again!")
            return

        self.balance = bet["new_balance"]
        self.stop()
        await interaction.edit_original_response(
            embed=create_embed(
                title=f"{self.emoji} {self.client.game_name} Bet Slip",
                message="✅ Bet placed! Your bet slip has been posted in the channel.",
            ),
            view=None,
        )
        await interaction.followup.send(
            embed=create_bet_confirm_embed(
                game_name=self.client.game_name,
                emoji=self.emoji,
                mode_name=mode_name,
                bettor_id=self.user_id,
                account=account,
                pick=pick,
                amount=amount,
                odds=bet["odds_at_bet"],
                potential_payout=bet["potential_payout"],
                new_balance=bet["new_balance"],
                waiting_for_next_game=bet["waiting_for_next_game"],
            ),
        )


class RankedBetAmountModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount (SSC)", required=True, max_length=12)

    def __init__(self, bet_view: RankedBetView, account: Dict, mode: str, pick: str):
        super().__init__(title=f"Bet {_riot_id(account)} will {'win' if pick == 'win' else 'lose'}"[:45])
        self.bet_view = bet_view
        self.account_id = account["id"]
        self.mode = mode
        self.pick = pick
        self.amount.placeholder = f"Min {bet_view.min_bet} — you have {bet_view.balance} SSC"

    async def on_submit(self, interaction: discord.Interaction):
        raw_amount = self.amount.value.strip().replace(",", "")
        if not raw_amount.isdigit() or int(raw_amount) < self.bet_view.min_bet:
            await interaction.response.send_message(
                embed=create_embed(
                    message=f"Enter a whole number of at least **{self.bet_view.min_bet} SSC**."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await self.bet_view.place_bet(
            interaction, self.account_id, self.mode, self.pick, int(raw_amount)
        )


def create_bet_confirm_embed(
    game_name: str,
    emoji: str,
    mode_name: str,
    bettor_id: int,
    account: Dict,
    pick: str,
    amount: int,
    odds: float,
    potential_payout: float,
    new_balance: int,
    waiting_for_next_game: bool,
) -> discord.Embed:
    verb = "win" if pick == "win" else "lose"
    waiting = (
        "\n\n⏳ They're in a game right now, so this bet counts on the game **after** it."
        if waiting_for_next_game
        else ""
    )
    return create_embed(
        title=f"{emoji} {game_name} Bet Placed!",
        message=(
            f"<@{bettor_id}> bet **{amount} SSC** that **{_riot_id(account)}** "
            f"(<@{account['discord_id']}>) will **{verb}** their next **{mode_name}** game.\n\n"
            f"Locked odds: **{odds:.2f}x**\n"
            f"Potential payout: **{int(potential_payout)} SSC**\n\n"
            f"Remaining balance: **{new_balance} SSC**{waiting}"
        ),
        color=0x61B875,
    )


def create_resolution_embed(resolution: "BetResolution") -> discord.Embed:
    verb = "win" if resolution.pick == "win" else "lose"
    emoji_game = f"{resolution.game_name} {resolution.mode_name}"

    game = ""
    if resolution.outcome:
        result = {"win": "won", "loss": "lost", "draw": "drew"}[resolution.outcome]
        game = f"**{resolution.riot_id}** {result}"
        if resolution.score:
            game += f" **{resolution.score[0]}–{resolution.score[1]}**"
        if resolution.map_name:
            game += f" on {resolution.map_name}"
        if resolution.detail:
            game += f" as {resolution.detail}"
        game += f" ({emoji_game})"

    if resolution.status == "won":
        return create_embed(
            title=f"💰 {resolution.game_name} Bet Won!",
            message=(
                f"{game}\n\n<@{resolution.bettor_id}> bet **{resolution.amount} SSC** that they'd {verb} "
                f"and won **{resolution.payout} SSC**!"
            ),
            color=0x61B875,
        )
    if resolution.status == "lost":
        return create_embed(
            title=f"❌ {resolution.game_name} Bet Lost",
            message=(
                f"{game}\n\n<@{resolution.bettor_id}> bet **{resolution.amount} SSC** that they'd {verb} "
                "and lost it."
            ),
            color=0xE74C3C,
        )
    return create_embed(
        title=f"↩️ {resolution.game_name} Bet Refunded",
        message=(
            (f"{game}\n\n" if game else "")
            + f"<@{resolution.bettor_id}>'s **{resolution.amount} SSC** bet on **{resolution.riot_id}** "
            f"({emoji_game}) was refunded. {resolution.reason}"
        ),
    )


def build_user_bets_pages(
    bets: list, client: "RankedGameClient", per_page: int = 8
) -> List[discord.Embed]:
    """Build a list of embeds for a user's ranked bets in one game, one per page."""
    title = f"{GAME_EMOJI.get(client.game_key, '🎲')} Your {client.game_name} Bets"
    if not bets:
        return [create_embed(title=title, message=f"You haven't placed any {client.game_name} bets yet!")]

    pages = []
    for chunk in divide_chunks(bets, per_page):
        lines = []
        for bet in chunk:
            status_emoji = {
                "pending": "⏳",
                "won": "💰",
                "lost": "❌",
                "refunded": "↩️",
            }.get(bet["status"], "❓")

            payout_text = ""
            if bet["status"] == "won":
                payout_text = f" → **+{int(bet['potential_payout'])} SSC**"
            elif bet["status"] == "lost":
                payout_text = f" → **-{bet['amount']} SSC**"
            elif bet["status"] == "refunded":
                payout_text = " → **Refunded**"

            mode = client.mode(bet["mode"])
            lines.append(
                f"{status_emoji} **{bet['riot_id']}** to **{'win' if bet['pick'] == 'win' else 'lose'}** "
                f"{mode.name if mode else bet['mode']} (<@{bet['target_discord_id']}>)\n"
                f"  {bet['amount']} SSC @ {bet['odds_at_bet']:.2f}x · <t:{bet['placed_at']}:R>{payout_text}"
            )

        pages.append(create_embed(title=title, message="\n".join(lines)))

    return pages


def build_accounts_embed(accounts: List[Dict], player: Optional[discord.abc.User]) -> discord.Embed:
    title = f"🔗 {player.display_name}'s Riot Accounts" if player else "🔗 Linked Riot Accounts"
    if not accounts:
        return create_embed(
            title=title,
            message="No Riot accounts linked yet! Use `/riot link` to add one.",
        )

    def games(account: Dict) -> str:
        played = [name for key, name in (("valorant", "Valorant"), ("league", "League")) if account[f"{key}_puuid"]]
        return ", ".join(played)

    if player:
        lines = [f"• **{_riot_id(a)}** — {games(a)}" for a in accounts]
    else:
        lines = [f"• **{_riot_id(a)}** — <@{a['discord_id']}> · {games(a)}" for a in accounts]
    return create_embed(title=title, message="\n".join(lines))
