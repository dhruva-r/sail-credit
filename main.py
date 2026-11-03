import asyncio
import os
import time
from typing import Literal, Optional
from zoneinfo import ZoneInfo
from discord import app_commands
import discord
from discord.ext import commands, tasks
from casino.casino import CasinoLobby, CasinoPitboss
from casino.models import DegenerateGambler
import db
from models import SerializableMessage
from party import Party, PartyService, STARTING_SSC
from vct.betting import VCTBettingService, MIN_BET
from vct.views import (
    build_matches_pages,
    build_user_bets_pages,
    create_bet_confirm_embed,
)
from lol.betting import LoLBettingService, MIN_BET as LOL_MIN_BET
from lol import views as lol_views
from ranked.betting import MAX_LINKED_ACCOUNTS, MIN_BET as RANKED_MIN_BET, RankedBettingService
from ranked.league import LeagueClient, load_riot_key
from ranked.valorant import ValorantClient, load_henrik_key
from ranked import views as ranked_views
import validators
from datetime import datetime, timedelta

from state import save_state, load_state
from util import (
    create_ssc_graph_url,
    divide_chunks,
    get_daily_reward,
    get_scheduled_datetime_from_string,
    get_season_emoji,
    user_command,
    create_embed,
    get_balance,
)
from views import (
    LeaderboardView,
    MatchBetView,
    MessageBook,
    PartyView,
    SeasonResetConfirmationView,
    TopupView,
)

intents = discord.Intents.default()
bot = commands.Bot(
    command_prefix="/",
    intents=intents,
    case_insensitive=False,
)

party_service: Optional[PartyService] = None
MAINTENENCE_MODE = False


@bot.tree.command(
    name="party-up",
    description="Creates a party, allowing others to let you know they want to join!",
)
@app_commands.describe(
    role="The role of the party you're creating (e.g. @goofy「无畏契约」, @flaccid)",
    name="The name of the party (optional)",
    max_size="The max size of the party (default is 5)",
    description="Any additional information you want to provide about the party.",
    start_time="The initial time for the party to start. Format is HH:MM [AM/PM] [EST/PST]. Default timezone is EST.",
    image_url="The optional image URL for the party.",
)
@user_command()
async def create_party(
    interaction: discord.Interaction,
    role: discord.Role,
    name: Optional[str],
    max_size: Optional[int],
    description: Optional[str],
    start_time: Optional[str],
    image_url: Optional[str],
):
    if MAINTENENCE_MODE:
        await interaction.response.send_message(
            embed=create_embed(
                message="The bot is currently in maintenance mode. Please try again later."
            ),
            ephemeral=True,
        )
        return

    created_at = int(time.time())

    parsed_start_time = None
    if start_time:
        parsed_start_time, time_parsing_error = get_scheduled_datetime_from_string(
            start_time
        )

        if time_parsing_error:
            await interaction.response.send_message(
                embed=create_embed(
                    message=f"There was a problem while parsing the start time: `{start_time}`.\n\n{time_parsing_error}",
                ),
                ephemeral=True,
            )
            return

    if image_url and not validators.url(image_url):
        await interaction.response.send_message(
            embed=create_embed(
                message="That doesn't look like a valid image URL! Please try again."
            ),
            ephemeral=True,
        )
        return

    if not image_url:
        image_url = await db.get_role_image_url(role.id)

    party: Party = party_service.create_party(
        user=interaction.user,
        user_ssc=get_balance(interaction),
        role_id=role.id,
        role_color=role.color.value,
        name=name,
        max_size=max_size,
        description=description,
        created_at=created_at,
        start_time=parsed_start_time,
        role_image_url=image_url,
    )

    content = f"<@{interaction.user.id}> has created a party for <@&{role.id}>!\n"
    if party.description:
        content += f"Description: `{party.description}`\n"

    # Don't send the party embed and view just yet, we need to get a message reference first
    await interaction.response.send_message(
        content=content,
        ephemeral=False,
        allowed_mentions=discord.AllowedMentions(),
        embed=create_embed(**party.generate_embed()),
    )

    # Get the jump URL (message link) for the party for management commands
    message = await interaction.original_response()

    party.message = SerializableMessage.from_message(message)
    party.jump_url = message.jump_url

    # Edit in the party embed and view now
    await interaction.edit_original_response(
        embed=create_embed(**party.generate_embed()),
        view=PartyView(party, party_service),
    )


@bot.tree.command(
    name="parties", description="List of active parties you're a part of!"
)
@app_commands.describe(leader="If True, only list parties that you're the leader of.")
@user_command()
async def parties(interaction: discord.Interaction, leader: Optional[bool] = False):
    personal_party_list = []
    for party in party_service.parties.values():
        member_ids = {m.user_id for m in party.members}
        if interaction.user.id in member_ids and (
            not leader or interaction.user.id == party.owner_id
        ):
            personal_party_list.append(party)

    party_message = "\n".join(
        [
            f"{'👑 ' if p.owner_id == interaction.user.id else ''}{p.name} {p.jump_url}"
            for p in personal_party_list
        ]
    )
    await interaction.response.send_message(
        embed=create_embed(
            title=f"{interaction.user.display_name}'s Active Parties",
            message=party_message if party_message else "No Parties!",
        )
    )


@bot.tree.command(name="ssc", description="Check your Sail Social Credit (SSC) score!")
@app_commands.describe(user="The discord user to check SSC for.")
@user_command()
async def ssc(interaction: discord.Interaction, user: Optional[discord.User] = None):
    # If no user supplied, get SSC for yourself.
    if not user:
        await interaction.response.send_message(
            embed=create_embed(
                message=f"<@{interaction.user.id}>'s Sail Social Credit: **{interaction.data['user_data']['sail_credit']}**"
            ),
        )
        return

    # If we do have a user, get that user's SSC (if exists)
    user_info = await db.get_user(user.id)
    if not user_info:
        await interaction.response.send_message(
            embed=create_embed(message="This user hasn't used the bot before!"),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=create_embed(
            message=f"<@{user.id}>'s Sail Social Credit: **{user_info['sail_credit']}**"
        ),
    )


@bot.tree.context_menu(name="Get SSC")
@user_command()
async def get_ssc(interaction: discord.Interaction, user: discord.User):
    user_info = await db.get_user(user.id)
    if not user_info:
        await interaction.response.send_message(
            embed=create_embed(message="This user hasn't used the bot before!"),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=create_embed(
            message=f"<@{user.id}>'s Sail Social Credit: **{user_info['sail_credit']}**"
        ),
    )


@bot.tree.command(name="leaderboard", description="Check the SSC leaderboard!")
@user_command()
async def leaderboard(interaction: discord.Interaction):
    pages = []
    users = await db.get_ssc_leaderboard()
    chunks = divide_chunks(users, 10)

    me_page_idx = 0
    for i, chunk in enumerate(chunks):
        page_contents = []
        for j, user in enumerate(chunk):
            rank = i * 10 + j + 1
            page_contents.append(
                f"**#{rank}** <@{user['discord_id']}> ({user['sail_credit']} SSC)"
            )

            if user["discord_id"] == interaction.user.id:
                me_page_idx = i

        pages.append(
            create_embed(title="SSC Leaderboard", message="\n".join(page_contents))
        )

    if not pages:
        await interaction.response.send_message(
            embed=create_embed(title="SSC Leaderboard", message="Nobody!"),
        )
        return

    await interaction.response.send_message(
        embed=pages[0],
        view=LeaderboardView(
            user_id=interaction.user.id, pages=pages, me_page=me_page_idx
        ),
    )


season_group = app_commands.Group(name="season", description="Season commands.")


@season_group.command(
    name="history",
    description="View your highest SSC and rank for each season.",
)
@user_command()
async def season_history(interaction: discord.Interaction):
    history = await db.get_user_season_history(interaction.user.id)
    if not history:
        pages = [
            create_embed(
                title="Season History",
                message="You don't have any season history yet.",
            )
        ]
    else:
        pages = []
        for chunk in divide_chunks(history, 5):
            entries = []
            for season in chunk:
                highest_ssc = season["highest_sail_credit"]
                season_emoji = get_season_emoji(highest_ssc)
                rank = (
                    f"#{season['highest_rank']}"
                    if season["highest_rank"] is not None
                    else "Not available"
                )
                end = (
                    f" (ended <t:{season['end_timestamp']}:D>)"
                    if season["end_timestamp"] is not None
                    else " (in progress)"
                )
                entries.append(
                    f"**Season {season['season_id']}**{end}\n"
                    f"Highest SSC: **{highest_ssc}** {season_emoji}\n"
                    f"Season rank: **{rank}**"
                )
            pages.append(
                create_embed(title="Season History", message="\n\n".join(entries))
            )

    await interaction.response.send_message(
        embed=pages[0],
        view=MessageBook(user_id=interaction.user.id, pages=pages),
    )


@season_group.command(
    name="leaderboard",
    description="View the highest SSC leaderboard for a season.",
)
@app_commands.describe(season_id="The season number to view.")
@user_command()
async def season_leaderboard(interaction: discord.Interaction, season_id: int):
    leaderboard = await db.get_season_leaderboard(season_id)
    if not leaderboard:
        await interaction.response.send_message(
            embed=create_embed(
                title=f"Season {season_id} Leaderboard",
                message="No leaderboard entries found for that season.",
            ),
        )
        return

    pages = []
    season = leaderboard[0]
    season_period = (
        f"Season ended <t:{season['end_timestamp']}:F>"
        if season["end_timestamp"] is not None
        else "Season in progress"
    )
    for chunk in divide_chunks(leaderboard, 10):
        entries = [
            f"**#{entry['season_rank']}** {get_season_emoji(entry['highest_sail_credit'])} "
            f"<@{entry['discord_id']}> ({entry['highest_sail_credit']} SSC)"
            for entry in chunk
        ]
        pages.append(
            create_embed(
                title=f"Season {season_id} Leaderboard",
                message=f"{season_period}\n\n" + "\n".join(entries),
            )
        )

    await interaction.response.send_message(
        embed=pages[0],
        view=MessageBook(user_id=interaction.user.id, pages=pages),
    )


@bot.tree.command(
    name="ssc-graph", description="Check a graph of your SSC over a time period!"
)
@user_command()
async def ssc_graph(
    interaction: discord.Interaction,
    period: Literal["1h", "6h", "12h", "1d", "7d", "30d"],
):
    graph_url = await create_ssc_graph_url(
        interaction.user.id, interaction.user.display_name, period
    )
    await interaction.response.send_message(content=graph_url)


@bot.tree.command(name="search", description="Searches for active parties for a role!")
@app_commands.describe(role="The discord role the party should be for.")
@user_command()
async def search(interaction: discord.Interaction, role: discord.Role):
    party_list = []
    for party in party_service.parties.values():
        if party.role.id == role.id:
            party_list.append(party)

    party_message = "\n".join(
        [
            f"{'👑 ' if p.owner_id == interaction.user.id else ''}{p.name} {p.jump_url}"
            for p in party_list
        ]
    )
    await interaction.response.send_message(
        embed=create_embed(
            title=f"Party search for @{role.name}",
            message=party_message if party_message else "No Parties!",
        )
    )


@bot.tree.command(
    name="link-image",
    description="Link a default party image to a role (or delete)",
)
@app_commands.describe(
    role="The discord role to link the image to.",
    image_url="The image URL to link to the role. Leave blank to delete the image.",
)
@user_command()
async def link_image(
    interaction: discord.Interaction, role: discord.Role, image_url: Optional[str]
):

    # Mod check.
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            embed=create_embed(
                message="You need the **Manage Messages** permission to use this."
            ),
            ephemeral=True,
        )
        return

    if not image_url:
        await db.update_role_image_url(role.id, None)
        await interaction.response.send_message(
            embed=create_embed(
                message=f"Deleted image link for <@&{role.id}>.",
            ),
        )
        return

    if not validators.url(image_url):
        await interaction.response.send_message(
            embed=create_embed(
                message="That doesn't look like a valid URL! Please try again."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=create_embed(
            message=f"Linked image URL `{image_url}` to <@&{role.id}>.",
            image_url=image_url,
        ),
    )
    await db.update_role_image_url(role.id, image_url)


@bot.tree.command(
    name="adjust-ssc",
    description="Manually adjust a user's SSC. Requires admin privileges!",
)
@app_commands.describe(
    user="The discord user to adjust SSC for.",
    delta="The amount of SSC to add or deduct.",
)
@user_command()
async def adjust_ssc(interaction: discord.Interaction, user: discord.User, delta: int):

    # Check if this command is used in a server.
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            embed=create_embed(message="This can only be used in a server."),
            ephemeral=True,
        )
        return

    # Admin check.
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            embed=create_embed(message="You need admin priviliges to use this."),
            ephemeral=True,
        )
        return

    # Check if the user exists.
    user_info = await db.get_user(user.id)
    if not user_info:
        await interaction.response.send_message(
            embed=create_embed(message="This user hasn't used the bot before!"),
            ephemeral=True,
        )
        return

    new_ssc = max(0, user_info["sail_credit"] + delta)

    # Change and log SSC, with ADMIN as the source.
    await db.change_and_log_sail_credit(
        user.id, -1, -1, -1, user_info["sail_credit"], new_ssc, "ADMIN"
    )

    await interaction.response.send_message(
        embed=create_embed(
            message=f"Adjusted <@{user.id}>'s SSC by **{delta}**.\nNew SSC: **{new_ssc}**"
        ),
    )
    return


@season_group.command(
    name="new",
    description="Create a new SSC season and reset all users' balances.",
)
@user_command()
async def new_season(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You need administrator privileges to start a season.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Start a new season and reset every user's SSC to {STARTING_SSC}? This cannot be undone.",
        view=SeasonResetConfirmationView(interaction.user.id),
        ephemeral=True,
    )


bot.tree.add_command(season_group)


@bot.tree.command(
    name="conviction-log",
    description="See your or someone else's conviction log!",
)
@app_commands.describe(
    user="The user's conviction log to view.",
)
@user_command()
async def conviction_log(
    interaction: discord.Interaction, user: Optional[discord.User] = None
):

    title = (
        f"Global Conviction Log"
        if not user
        else f"{user.display_name}'s Conviction Log"
    )

    if not user:
        conviction_log = await db.get_conviction_log()
    else:
        conviction_log = await db.get_conviction_log(user.id)

    if not conviction_log:
        await interaction.response.send_message(
            embed=create_embed(message="No convictions found!"),
            ephemeral=True,
        )
        return

    pages = []
    chunks = divide_chunks(conviction_log, 10)

    for chunk in chunks:
        page_contents = []
        for log in chunk:
            log_info = f"<t:{log['timestamp']}:f>"
            # Global - include username here
            if not user:
                log_info += f" <@{log['discord_id']}>"
            page_contents.append(f"{log_info} **-** `{log['reason']}`")

        pages.append(create_embed(title=title, message="\n".join(page_contents)))

    await interaction.response.send_message(
        embed=pages[0], view=MessageBook(interaction.user.id, pages=pages)
    )


casino_group = app_commands.Group(name="sscasino", description="Gamble your SSC!")


@casino_group.command(name="crash", description="Start a crash lobby.")
@user_command()
async def casino_crash(interaction: discord.Interaction):
    if MAINTENENCE_MODE:
        await interaction.response.send_message(
            embed=create_embed(
                message="The bot is currently in maintenance mode. Please try again later."
            ),
            ephemeral=True,
        )
        return
    await casino_pitboss.start_lobby("crash", interaction)


@casino_group.command(name="coinflip", description="Start a 1v1 coinflip lobby.")
@app_commands.describe(amount="SSC to wager (opponent must match).")
@app_commands.describe(choice="Side of the coin (heads or tails).")
@user_command()
async def casino_coinflip(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 10, 1000],
    choice: Literal["heads", "tails"],
):

    if MAINTENENCE_MODE:
        await interaction.response.send_message(
            embed=create_embed(
                message="The bot is currently in maintenance mode. Please try again later."
            ),
            ephemeral=True,
        )
        return

    current_ssc = get_balance(interaction)
    if current_ssc < amount:
        await interaction.response.send_message(
            embed="You don't have enough SSC to bet!"
        )
        return

    await db.change_and_log_sail_credit(
        interaction.user.id,
        -1,
        -1,
        -1,
        current_ssc,
        current_ssc - amount,
        source="COINFLIP",
    )

    def on_lobby_create(lobby: CasinoLobby):
        lobby.members.append(
            DegenerateGambler(
                user_id=interaction.user.id,
                bet_amount=amount,
                avatar_url=interaction.user.display_avatar.url,
            )
        )

    await casino_pitboss.start_lobby(
        "coinflip",
        interaction,
        on_lobby_create,
        host_id=interaction.user.id,
        host_bet=amount,
        host_choice=choice,
    )


@casino_group.command(name="jackpot", description="Start a jackpot lobby.")
@user_command()
async def casino_jackpot(interaction: discord.Interaction):
    if MAINTENENCE_MODE:
        await interaction.response.send_message(
            embed=create_embed(
                message="The bot is currently in maintenance mode. Please try again later."
            ),
            ephemeral=True,
        )
        return
    await casino_pitboss.start_lobby("jackpot", interaction)


bot.tree.add_command(casino_group)


# --- VCT Betting ---

vct_group = app_commands.Group(name="vct", description="Bet on VCT matches!")

vct_service: Optional[VCTBettingService] = None


@vct_group.command(name="matches", description="View upcoming VCT matches and odds.")
@user_command()
async def vct_matches(interaction: discord.Interaction):
    if not vct_service:
        await interaction.response.send_message(
            embed=create_embed(message="VCT service is not ready yet. Try again in a moment!"),
            ephemeral=True,
        )
        return

    # An empty list is normal before an event's bracket is published; the pages show an empty state
    pages = build_matches_pages(vct_service.get_matches())
    await interaction.response.send_message(embed=pages[0], view=MessageBook(interaction.user.id, pages))


@vct_group.command(name="bet", description="Bet SSC on a VCT match.")
@user_command()
async def vct_bet(interaction: discord.Interaction):
    if not vct_service:
        await interaction.response.send_message(
            embed=create_embed(message="VCT service is not ready yet. Try again in a moment!"),
            ephemeral=True,
        )
        return

    await MatchBetView.send(
        interaction, vct_service, "VCT", MIN_BET, create_bet_confirm_embed
    )


@vct_group.command(name="bets", description="View your VCT betting history.")
@user_command()
async def vct_bets(interaction: discord.Interaction):
    bets = await db.get_user_vct_bets(interaction.user.id)
    pages = build_user_bets_pages(bets)
    await interaction.response.send_message(embed=pages[0], view=MessageBook(interaction.user.id, pages))


@vct_group.command(name="leaderboard", description="VCT betting leaderboard — who's profited the most?")
@user_command()
async def vct_leaderboard(interaction: discord.Interaction):
    rows = await db.get_vct_betting_leaderboard()
    if not rows:
        await interaction.response.send_message(
            embed=create_embed(
                title="🏆 VCT Betting Leaderboard",
                message="No resolved bets yet!",
            ),
        )
        return

    lines = []
    for i, row in enumerate(rows[:15]):
        rank = i + 1
        net = int(row["net_profit"])
        sign = "+" if net >= 0 else ""
        lines.append(
            f"**#{rank}** <@{row['discord_id']}> — {sign}{net} SSC "
            f"({row['wins']}W/{row['losses']}L, {int(row['total_wagered'])} wagered)"
        )

    await interaction.response.send_message(
        embed=create_embed(
            title="🏆 VCT Betting Leaderboard",
            message="\n".join(lines),
        ),
    )


bot.tree.add_command(vct_group)


# --- LoL Betting ---

lol_group = app_commands.Group(name="lol", description="Bet on LoL Worlds matches!")

lol_service: Optional[LoLBettingService] = None


@lol_group.command(name="matches", description="View LoL Worlds matches and odds.")
@user_command()
async def lol_matches(interaction: discord.Interaction):
    if not lol_service:
        await interaction.response.send_message(
            embed=create_embed(message="LoL service is not ready yet. Try again in a moment!"),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    await lol_service.refresh_odds()

    matches = lol_service.get_matches()
    if not matches:
        await interaction.followup.send(
            embed=create_embed(
                message="No LoL matches loaded yet. Polymarket may be unreachable — try again in a minute!"
            ),
            ephemeral=True,
        )
        return

    pages = lol_views.build_matches_pages(matches)
    await interaction.followup.send(embed=pages[0], view=MessageBook(interaction.user.id, pages))


@lol_group.command(name="bet", description="Bet SSC on a LoL Worlds match.")
@user_command()
async def lol_bet(interaction: discord.Interaction):
    if not lol_service:
        await interaction.response.send_message(
            embed=create_embed(message="LoL service is not ready yet. Try again in a moment!"),
            ephemeral=True,
        )
        return

    await MatchBetView.send(
        interaction, lol_service, "LoL", LOL_MIN_BET, lol_views.create_bet_confirm_embed
    )


@lol_group.command(name="bets", description="View your LoL betting history.")
@user_command()
async def lol_bets(interaction: discord.Interaction):
    bets = await db.get_user_lol_bets(interaction.user.id)
    pages = lol_views.build_user_bets_pages(bets)
    await interaction.response.send_message(embed=pages[0], view=MessageBook(interaction.user.id, pages))


@lol_group.command(name="leaderboard", description="LoL betting leaderboard — who's profited the most?")
@user_command()
async def lol_leaderboard(interaction: discord.Interaction):
    rows = await db.get_lol_betting_leaderboard()
    if not rows:
        await interaction.response.send_message(
            embed=create_embed(
                title="🏆 LoL Betting Leaderboard",
                message="No resolved bets yet!",
            ),
        )
        return

    lines = []
    for i, row in enumerate(rows[:15]):
        rank = i + 1
        net = int(row["net_profit"])
        sign = "+" if net >= 0 else ""
        lines.append(
            f"**#{rank}** <@{row['discord_id']}> — {sign}{net} SSC "
            f"({row['wins']}W/{row['losses']}L, {int(row['total_wagered'])} wagered)"
        )

    await interaction.response.send_message(
        embed=create_embed(
            title="🏆 LoL Betting Leaderboard",
            message="\n".join(lines),
        ),
    )


bot.tree.add_command(lol_group)


# --- Ranked Betting (Valorant, League) ---

ranked_service: Optional[RankedBettingService] = None

riot_group = app_commands.Group(name="riot", description="Link your Riot accounts for ranked betting.")


@riot_group.command(name="link", description="Link a Riot account so people can bet on its ranked games.")
@app_commands.describe(riot_id="Your Riot ID, like name#tag")
@user_command()
async def riot_link(interaction: discord.Interaction, riot_id: str):
    if not ranked_service:
        await interaction.response.send_message(
            embed=create_embed(message="Ranked betting isn't available right now. Try again in a moment!"),
            ephemeral=True,
        )
        return

    name, sep, tag = riot_id.strip().rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not sep or not name or not tag:
        await interaction.response.send_message(
            embed=create_embed(message="Enter your Riot ID as **name#tag**, e.g. `pucks in#deep`."),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Look the account up in every game. Each game's API has its own ID for the same account.
    found = {}
    for game, client in ranked_service.clients.items():
        found[game] = await client.get_account(name, tag)
    if not any(found.values()):
        await interaction.followup.send(
            embed=create_embed(
                message=f"Couldn't find **{riot_id}** in {' or '.join(c.game_name for c in ranked_service.clients.values())}. Check the spelling and tag!"
            ),
            ephemeral=True,
        )
        return

    account_name = next(a for a in found.values() if a)
    ids = {
        "valorant_puuid": found["valorant"].puuid if found.get("valorant") else None,
        "valorant_region": found["valorant"].region if found.get("valorant") else None,
        "league_puuid": found["league"].puuid if found.get("league") else None,
        "league_region": found["league"].region if found.get("league") else None,
    }
    games_text = " and ".join(ranked_service.clients[g].game_name for g, a in found.items() if a)

    existing = await db.find_riot_account_by_puuid(ids["valorant_puuid"], ids["league_puuid"])
    if existing:
        if existing["discord_id"] != interaction.user.id:
            await interaction.followup.send(
                embed=create_embed(message=f"**{riot_id}** is already linked to <@{existing['discord_id']}>."),
                ephemeral=True,
            )
            return
        # Already theirs. Refresh the name, and pick up a game they've started playing since.
        await db.update_riot_account(existing["id"], account_name.name, account_name.tag, **ids)
        await interaction.followup.send(
            embed=create_embed(message=f"**{account_name.name}#{account_name.tag}** is already linked to you ({games_text})."),
            ephemeral=True,
        )
        return

    linked = await db.get_user_riot_accounts(interaction.user.id)
    if len(linked) >= MAX_LINKED_ACCOUNTS:
        await interaction.followup.send(
            embed=create_embed(
                message=f"You can link up to **{MAX_LINKED_ACCOUNTS}** accounts. Use `/riot unlink` to remove one first."
            ),
            ephemeral=True,
        )
        return

    await db.link_riot_account(
        interaction.user.id, account_name.name, account_name.tag, linked_at=int(time.time()), **ids
    )
    await interaction.followup.send(
        embed=create_embed(
            message=f"✅ Linked **{account_name.name}#{account_name.tag}** ({games_text}). People can now bet on its ranked games!",
            color=0x61B875,
        ),
        ephemeral=True,
    )


@riot_group.command(name="unlink", description="Unlink one of your Riot accounts.")
@app_commands.describe(account="The linked account to remove")
@user_command()
async def riot_unlink(interaction: discord.Interaction, account: str):
    accounts = await db.get_user_riot_accounts(interaction.user.id)
    match = next(
        (a for a in accounts if str(a["id"]) == account or f"{a['name']}#{a['tag']}".lower() == account.strip().lower()),
        None,
    )
    if not match:
        await interaction.response.send_message(
            embed=create_embed(message=f"You don't have **{account}** linked. Check `/riot accounts`."),
            ephemeral=True,
        )
        return

    # Pending bets keep their own copy of the account, so they still resolve after an unlink
    await db.unlink_riot_account(match["id"])
    await interaction.response.send_message(
        embed=create_embed(message=f"Unlinked **{match['name']}#{match['tag']}**."),
        ephemeral=True,
    )


@riot_unlink.autocomplete("account")
async def riot_unlink_autocomplete(interaction: discord.Interaction, current: str):
    accounts = await db.get_user_riot_accounts(interaction.user.id)
    choices = []
    for a in accounts:
        name = f"{a['name']}#{a['tag']}"
        if current.lower() in name.lower():
            choices.append(app_commands.Choice(name=name[:100], value=str(a["id"])))
    return choices[:25]


@riot_group.command(name="accounts", description="See linked Riot accounts.")
@app_commands.describe(player="Whose accounts to show (default: everyone)")
@user_command()
async def riot_accounts(interaction: discord.Interaction, player: Optional[discord.Member] = None):
    accounts = await db.get_user_riot_accounts(player.id) if player else await db.get_all_riot_accounts()
    await interaction.response.send_message(embed=ranked_views.build_accounts_embed(accounts, player))


bot.tree.add_command(riot_group)


def _make_ranked_group(game: str, game_name: str) -> app_commands.Group:
    """Build the /valorant or /league command group. Both work the same way."""
    group = app_commands.Group(name=game, description=f"Bet on each other's {game_name} games!")

    async def ready(interaction: discord.Interaction) -> bool:
        if ranked_service and ranked_service.client(game):
            return True
        await interaction.response.send_message(
            embed=create_embed(message=f"{game_name} betting isn't available right now. Try again in a moment!"),
            ephemeral=True,
        )
        return False

    @group.command(name="bet", description=f"Bet on whether someone wins or loses their next {game_name} game.")
    @app_commands.describe(player="Only show this player's accounts (optional)")
    @user_command()
    async def bet(interaction: discord.Interaction, player: Optional[discord.Member] = None):
        if not await ready(interaction):
            return
        await ranked_views.RankedBetView.send(interaction, ranked_service, game, RANKED_MIN_BET, player)

    @group.command(name="bets", description=f"View your {game_name} betting history.")
    @user_command()
    async def bets(interaction: discord.Interaction):
        if not await ready(interaction):
            return
        rows = await db.get_user_ranked_bets(interaction.user.id, game)
        pages = ranked_views.build_user_bets_pages(rows, ranked_service.client(game))
        await interaction.response.send_message(embed=pages[0], view=MessageBook(interaction.user.id, pages))

    @group.command(name="leaderboard", description=f"{game_name} betting leaderboard — who's profited the most?")
    @user_command()
    async def leaderboard(interaction: discord.Interaction):
        title = f"🏆 {game_name} Betting Leaderboard"
        rows = await db.get_ranked_betting_leaderboard(game)
        if not rows:
            await interaction.response.send_message(embed=create_embed(title=title, message="No resolved bets yet!"))
            return

        lines = []
        for i, row in enumerate(rows[:15]):
            net = int(row["net_profit"])
            sign = "+" if net >= 0 else ""
            lines.append(
                f"**#{i + 1}** <@{row['discord_id']}> — {sign}{net} SSC "
                f"({row['wins']}W/{row['losses']}L, {int(row['total_wagered'])} wagered)"
            )
        await interaction.response.send_message(embed=create_embed(title=title, message="\n".join(lines)))

    return group


bot.tree.add_command(_make_ranked_group("valorant", "Valorant"))
bot.tree.add_command(_make_ranked_group("league", "League"))


async def announce_ranked_resolution(resolution):
    """Post a resolved bet in the channel it was placed in."""
    if not resolution.channel_id:
        return
    channel = bot.get_channel(resolution.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(resolution.channel_id)
        except discord.HTTPException:
            return
    await channel.send(
        content=f"<@{resolution.bettor_id}>",
        embed=ranked_views.create_resolution_embed(resolution),
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(resolution.bettor_id)]),
    )


@bot.tree.command(
    name="beg",
    description="Request a top up to your balance.",
)
@app_commands.describe(
    message="An optional description for the request.",
)
@user_command()
async def topup(
    interaction: discord.Interaction,
    message: str = "I'm irresponsible and lost all my credits 🥺",
):
    content = f"**Emergency Funding Request** (<@{interaction.user.id}> | {get_balance(interaction)} SSC)\n> _{message}_"

    await interaction.response.send_message(
        content=content,
        view=TopupView(user_id=interaction.user.id),
    )


@bot.tree.command(
    name="daily",
    description="Claim your daily SSC!",
)
@user_command()
async def daily_ssc(interaction: discord.Interaction):
    user_id = interaction.user.id

    # Daily reset at 8:00 AM EST.
    est = ZoneInfo("America/New_York")
    now_est = datetime.now(est)
    # If it's before 8 AM today, the last reset was yesterday at 8 AM
    # If it's after 8 AM today, the last reset was today at 8 AM
    if now_est.hour < 8:
        last_reset = now_est.replace(
            hour=8, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
    else:
        last_reset = now_est.replace(hour=8, minute=0, second=0, microsecond=0)

    user_info = await db.get_user_sail_credit_log(
        user_id, last_reset.timestamp(), source="DAILY_SSC"
    )

    reward_info = await get_daily_reward(user_id)
    if not user_info:
        current_ssc = get_balance(interaction)
        await db.change_and_log_sail_credit(
            user_id,
            -1,
            -1,
            -1,
            current_ssc,
            current_ssc + reward_info.total_reward,
            "DAILY_SSC",
        )
        embed = create_embed(
            title="Daily Reward",
            message=f"Daily reward of **{reward_info.total_reward} SSC** claimed. Your balance is now **{current_ssc + reward_info.total_reward} SSC**.",
        )

        embed.add_field(name="💳 Base amount", value=f"{reward_info.base_amount} SSC")
        if reward_info.random_bonus > 0:
            embed.add_field(
                name="🎲 Random bonus",
                value=f"{reward_info.random_bonus} SSC",
                inline=True,
            )
        embed.add_field(
            name=f"🏅 Ranking bonus (#{reward_info.user_rank}/#{reward_info.total_entries})",
            value=f"{reward_info.ranking_bonus} SSC",
            inline=True,
        )
        embed.add_field(
            name=f"🔥 Streak bonus ({reward_info.current_streak})",
            value=f"{reward_info.streak_bonus} SSC",
            inline=True,
        )
        await interaction.response.send_message(
            embed=embed,
        )
    else:
        await interaction.response.send_message(
            embed=create_embed(
                message="You've already claimed your daily reward today. Why don't you try starting a **party?**"
            ),
        )


@bot.tree.command(
    name="maintenance",
    description="Toggle maintenence mode for the bot. Requires admin privileges!",
)
async def maintenence(interaction: discord.Interaction, mode: bool):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            embed=create_embed(message="You need admin priviliges to use this."),
            ephemeral=True,
        )
        return

    global MAINTENENCE_MODE
    MAINTENENCE_MODE = mode
    await interaction.response.send_message(
        embed=create_embed(
            message=f"Maintenance mode {'enabled' if mode else 'disabled'}."
        ),
        ephemeral=True,
    )


@tasks.loop(minutes=1)
async def autosave():
    await save_state(party_service, casino_pitboss)


@bot.event
async def on_ready():
    global party_service
    party_service = PartyService()
    global casino_pitboss
    casino_pitboss = CasinoPitboss()

    # Restore saved state for parties and lobbies
    await load_state(bot, party_service, casino_pitboss)
    if not autosave.is_running():
        autosave.start()

    global vct_service
    vct_service = VCTBettingService()
    await vct_service.start()

    global lol_service
    lol_service = LoLBettingService()
    await lol_service.start()

    # on_ready can fire again on reconnect. Only start one poller, since it shares the API rate limits.
    global ranked_service
    if not ranked_service:
        clients = []
        henrik_key = load_henrik_key()
        if henrik_key:
            clients.append(ValorantClient(henrik_key))
        else:
            print("No HenrikDev API key (henrik_token file or HENRIK_API_KEY) — Valorant betting disabled.")
        riot_key = load_riot_key()
        if riot_key:
            clients.append(LeagueClient(riot_key))
        else:
            print("No Riot API key (riot_token file or RIOT_API_KEY) — League betting disabled.")
        if clients:
            ranked_service = RankedBettingService(clients)
            ranked_service.on_resolution = announce_ranked_resolution
            await ranked_service.start()

    await bot.tree.sync()

    print("Ready!")


# janky function to patch discord.py's bot close to include our async db cleanup
def patch_close():
    original_close_fn = bot.close

    async def new_close(*args, **kwargs):
        await db.cleanup()
        await save_state(party_service, casino_pitboss)
        await original_close_fn(*args, **kwargs)

    bot.close = new_close


if __name__ == "__main__":
    asyncio.run(db.init())

    # Run migrations
    asyncio.run(db.run_migrations())

    token_file = "test_token" if os.environ.get("SC_TEST") else "token"
    with open(token_file, "r") as f:
        token = f.read()

    patch_close()
    bot.run(token)
