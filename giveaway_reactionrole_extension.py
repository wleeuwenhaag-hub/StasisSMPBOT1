# giveaway_reactionrole_extension.py

import json
import os
import random
import re
import time
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands


# =========================================================
# FILES AND SETTINGS
# =========================================================

GIVEAWAYS_FILE = "giveaways.json"
MESSAGE_COUNTS_FILE = "giveaway_message_counts.json"


def load_json(filename, default):
    if not os.path.exists(filename):
        return default

    try:
        with open(filename, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, type(default)) else default
    except (json.JSONDecodeError, OSError):
        return default


def save_json(filename, data):
    temporary_file = filename + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4)

    os.replace(temporary_file, filename)


def parse_duration(value: str) -> int:
    value = value.lower().strip()
    matches = re.findall(r"(\d+)\s*(s|m|h|d|w)", value)

    if not matches:
        raise ValueError("Use a duration such as `30m`, `2h`, or `3d`.")

    cleaned = re.sub(r"\d+\s*(?:s|m|h|d|w)", "", value)

    if cleaned.strip():
        raise ValueError("Invalid duration. Example: `2d 4h`.")

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }

    total = sum(
        int(amount) * multipliers[unit]
        for amount, unit in matches
    )

    if total < 1:
        raise ValueError("Duration must be at least one second.")

    return total


def parse_role_ids(value: str) -> list[int]:
    if not value:
        return []

    found = re.findall(r"\d+", value)
    return list(dict.fromkeys(int(role_id) for role_id in found))


def parse_hex_colour(value: str) -> discord.Colour:
    try:
        value = value.strip().replace("#", "")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
            raise ValueError
        return discord.Colour(int(value, 16))
    except (ValueError, AttributeError):
        return discord.Colour.gold()


def format_timestamp(timestamp: float) -> str:
    return f"<t:{int(timestamp)}:R>"


def valid_giveaway_record(giveaway: dict) -> bool:
    """Check whether a saved giveaway has the fields needed to finish."""
    if not isinstance(giveaway, dict):
        return False

    required = ("guild_id", "channel_id", "message_id", "ends_at", "prize")

    for key in required:
        if giveaway.get(key) in (None, "", 0, "0"):
            return False

    try:
        int(giveaway["guild_id"])
        int(giveaway["channel_id"])
        int(giveaway["message_id"])
        float(giveaway["ends_at"])
    except (TypeError, ValueError):
        return False

    return True


# =========================================================
# DATA STORAGE
# =========================================================

class GiveawayStore:
    def __init__(self):
        self.data = load_json(GIVEAWAYS_FILE, {})
        self.message_counts = load_json(MESSAGE_COUNTS_FILE, {})

        # Remove invalid top-level entries without deleting valid giveaways.
        if not isinstance(self.data, dict):
            self.data = {}

        if not isinstance(self.message_counts, dict):
            self.message_counts = {}

    def save(self):
        save_json(GIVEAWAYS_FILE, self.data)

    def save_counts(self):
        save_json(MESSAGE_COUNTS_FILE, self.message_counts)

    def get_counts(self, guild_id: int, user_id: int) -> list[float]:
        guild_data = self.message_counts.get(str(guild_id), {})
        if not isinstance(guild_data, dict):
            return []

        counts = guild_data.get(str(user_id), [])
        return counts if isinstance(counts, list) else []

    def add_message(self, guild_id: int, user_id: int):
        guild_key = str(guild_id)
        user_key = str(user_id)

        self.message_counts.setdefault(guild_key, {})
        self.message_counts[guild_key].setdefault(user_key, [])

        self.message_counts[guild_key][user_key].append(time.time())

        # Keep old message history for total-message requirements.
        self.save_counts()


store = GiveawayStore()


# =========================================================
# REQUIREMENTS AND WINNER DRAWING
# =========================================================

def has_bypass(member: discord.Member, giveaway: dict) -> bool:
    bypass_roles = {
        int(role_id)
        for role_id in giveaway.get("bypass_roles", [])
    }
    member_roles = {role.id for role in member.roles}

    return bool(bypass_roles.intersection(member_roles))


def check_requirements(
    member: discord.Member,
    giveaway: dict,
) -> tuple[bool, str]:

    # Bypass roles skip every entry requirement.
    if has_bypass(member, giveaway):
        return True, ""

    member_roles = {role.id for role in member.roles}

    required_roles = {
        int(role_id)
        for role_id in giveaway.get("required_roles", [])
    }

    # Every required role must be present.
    if required_roles - member_roles:
        return False, "You do not have all the required roles."

    timestamps = store.get_counts(member.guild.id, member.id)
    now = time.time()

    daily_required = int(giveaway.get("daily_messages", 0))
    weekly_required = int(giveaway.get("weekly_messages", 0))
    monthly_required = int(giveaway.get("monthly_messages", 0))
    total_required = int(giveaway.get("total_messages", 0))

    daily_count = sum(1 for stamp in timestamps if stamp >= now - 86400)
    weekly_count = sum(1 for stamp in timestamps if stamp >= now - 604800)
    monthly_count = sum(1 for stamp in timestamps if stamp >= now - 2592000)
    total_count = len(timestamps)

    if daily_count < daily_required:
        return False, f"You need {daily_required} messages in the last 24 hours."

    if weekly_count < weekly_required:
        return False, f"You need {weekly_required} messages in the last 7 days."

    if monthly_count < monthly_required:
        return False, f"You need {monthly_required} messages in the last 30 days."

    if total_count < total_required:
        return False, f"You need {total_required} total tracked messages."

    return True, ""


def eligible_entries(
    giveaway: dict,
    guild: discord.Guild,
) -> dict[int, int]:

    result = {}
    extra_entries = max(0, int(giveaway.get("extra_entries", 0)))

    entries = giveaway.get("entries", {})

    # Support old records that may have stored entries as a list.
    if isinstance(entries, list):
        user_ids = entries
    elif isinstance(entries, dict):
        user_ids = entries.keys()
    else:
        user_ids = []

    for user_id in user_ids:
        try:
            member = guild.get_member(int(user_id))
        except (TypeError, ValueError):
            continue

        if member is None:
            continue

        eligible, _ = check_requirements(member, giveaway)

        if eligible:
            result[member.id] = 1 + extra_entries

    return result


def draw_winners(
    giveaway: dict,
    guild: discord.Guild,
    amount: int,
    excluded: Optional[set[int]] = None,
) -> list[discord.Member]:

    excluded = excluded or set()
    entries = eligible_entries(giveaway, guild)

    pool = []

    for user_id, weight in entries.items():
        if user_id in excluded:
            continue

        member = guild.get_member(user_id)

        if member is not None:
            pool.extend([member] * max(1, weight))

    winners = []

    while pool and len(winners) < amount:
        winner = random.choice(pool)

        if winner.id not in {member.id for member in winners}:
            winners.append(winner)

        # Remove all tickets belonging to this winner.
        pool = [member for member in pool if member.id != winner.id]

    return winners


# =========================================================
# GIVEAWAY EMBED
# =========================================================

def make_embed(giveaway: dict) -> discord.Embed:
    title = giveaway.get("title") or "🎉 GIVEAWAY 🎉"
    description = giveaway.get("description", "")
    prize = giveaway.get("prize", "Unknown prize")

    embed = discord.Embed(
        title=title,
        description=description,
        colour=parse_hex_colour(giveaway.get("colour", "#FFD700")),
    )

    embed.add_field(name="🎁 Prize", value=prize, inline=False)
    embed.add_field(
        name="🏆 Winners",
        value=str(giveaway.get("winners", 1)),
        inline=True,
    )
    embed.add_field(
        name="👤 Host",
        value=f"<@{giveaway.get('host_id', 0)}>",
        inline=True,
    )

    end_time = giveaway.get("ends_at")

    if end_time:
        embed.add_field(
            name="⏰ Ends",
            value=format_timestamp(float(end_time)),
            inline=True,
        )

    entries = giveaway.get("entries", {})
    entry_count = len(entries) if isinstance(entries, (dict, list)) else 0

    embed.add_field(
        name="👥 Entrants",
        value=str(entry_count),
        inline=True,
    )

    requirements = []

    if giveaway.get("required_roles"):
        roles = " ".join(
            f"<@&{role_id}>"
            for role_id in giveaway["required_roles"]
        )
        requirements.append(f"**Required roles:** {roles}")

    if giveaway.get("daily_messages"):
        requirements.append(
            f"**Daily messages:** {giveaway['daily_messages']}"
        )

    if giveaway.get("weekly_messages"):
        requirements.append(
            f"**Weekly messages:** {giveaway['weekly_messages']}"
        )

    if giveaway.get("monthly_messages"):
        requirements.append(
            f"**Monthly messages:** {giveaway['monthly_messages']}"
        )

    if giveaway.get("total_messages"):
        requirements.append(
            f"**Total messages:** {giveaway['total_messages']}"
        )

    if requirements:
        embed.add_field(
            name="📋 Entry requirements",
            value="\n".join(requirements),
            inline=False,
        )

    if giveaway.get("image_url"):
        embed.set_image(url=giveaway["image_url"])

    embed.set_footer(
        text=f"Giveaway ID: {giveaway.get('id', 'Unknown')}"
    )

    return embed


# =========================================================
# JOIN / LEAVE BUTTON
# =========================================================

class GiveawayJoinView(discord.ui.View):
    def __init__(self, cog, giveaway_id: str):
        super().__init__(timeout=None)

        self.cog = cog
        self.giveaway_id = giveaway_id

        button = discord.ui.Button(
            label="Enter / Leave",
            style=discord.ButtonStyle.success,
            emoji="🎉",
            custom_id=f"giveaway:join:{giveaway_id}",
        )

        button.callback = self.join_callback
        self.add_item(button)

    async def join_callback(self, interaction: discord.Interaction):
        giveaway = store.data.get(self.giveaway_id)

        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message(
                "This giveaway has ended.",
                ephemeral=True,
            )
            return

        if not interaction.guild or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "You must be in the server to enter.",
                ephemeral=True,
            )
            return

        entries = giveaway.setdefault("entries", {})

        if not isinstance(entries, dict):
            entries = {}
            giveaway["entries"] = entries

        user_key = str(interaction.user.id)

        if user_key in entries:
            del entries[user_key]
            store.save()

            await interaction.response.send_message(
                "You have left the giveaway.",
                ephemeral=True,
            )

            await self.cog.refresh_message(giveaway)
            return

        eligible, reason = check_requirements(interaction.user, giveaway)

        if not eligible:
            await interaction.response.send_message(
                f"❌ You cannot enter: {reason}",
                ephemeral=True,
            )
            return

        entries[user_key] = {"joined_at": time.time()}
        store.save()

        await interaction.response.send_message(
            "🎉 You have entered the giveaway!",
            ephemeral=True,
        )

        await self.cog.refresh_message(giveaway)


# =========================================================
# EDIT MODALS
# =========================================================

class GiveawayEditModal(discord.ui.Modal):
    def __init__(
        self,
        cog,
        giveaway_id: str,
        section: str,
        editor_id: int,
    ):
        super().__init__(title="Edit giveaway settings")

        self.cog = cog
        self.giveaway_id = giveaway_id
        self.section = section
        self.editor_id = editor_id

        giveaway = store.data[giveaway_id]

        if section == "basic":
            self.title_input = discord.ui.TextInput(
                label="Giveaway name / title",
                default=giveaway.get("title", "🎉 GIVEAWAY 🎉"),
                required=False,
                max_length=256,
            )

            self.prize_input = discord.ui.TextInput(
                label="Prize",
                default=giveaway.get("prize", ""),
                required=False,
                max_length=200,
            )

            self.description_input = discord.ui.TextInput(
                label="Description",
                default=giveaway.get("description", ""),
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )

            self.duration_input = discord.ui.TextInput(
                label="New duration (e.g. 2d 4h)",
                placeholder="Leave blank to keep the current end time",
                required=False,
                max_length=40,
            )

            self.winners_input = discord.ui.TextInput(
                label="Number of winners",
                default=str(giveaway.get("winners", 1)),
                required=False,
                max_length=3,
            )

            for item in (
                self.title_input,
                self.prize_input,
                self.description_input,
                self.duration_input,
                self.winners_input,
            ):
                self.add_item(item)

        elif section == "requirements":
            self.extra_input = discord.ui.TextInput(
                label="Extra entries",
                default=str(giveaway.get("extra_entries", 0)),
                required=False,
                max_length=4,
            )

            self.required_roles_input = discord.ui.TextInput(
                label="Required role IDs or mentions",
                default=", ".join(
                    map(str, giveaway.get("required_roles", []))
                ),
                required=False,
                max_length=1000,
            )

            self.bypass_roles_input = discord.ui.TextInput(
                label="Bypass role IDs or mentions",
                default=", ".join(
                    map(str, giveaway.get("bypass_roles", []))
                ),
                required=False,
                max_length=1000,
            )

            self.daily_input = discord.ui.TextInput(
                label="Daily message requirement",
                default=str(giveaway.get("daily_messages", 0)),
                required=False,
                max_length=6,
            )

            self.weekly_input = discord.ui.TextInput(
                label="Weekly message requirement",
                default=str(giveaway.get("weekly_messages", 0)),
                required=False,
                max_length=6,
            )

            for item in (
                self.extra_input,
                self.required_roles_input,
                self.bypass_roles_input,
                self.daily_input,
                self.weekly_input,
            ):
                self.add_item(item)

        elif section == "more":
            self.monthly_input = discord.ui.TextInput(
                label="Monthly message requirement",
                default=str(giveaway.get("monthly_messages", 0)),
                required=False,
                max_length=6,
            )

            self.total_input = discord.ui.TextInput(
                label="Total tracked message requirement",
                default=str(giveaway.get("total_messages", 0)),
                required=False,
                max_length=7,
            )

            self.channel_input = discord.ui.TextInput(
                label="Giveaway channel ID or channel mention",
                default=str(giveaway.get("channel_id", "")),
                required=False,
                max_length=30,
            )

            self.host_input = discord.ui.TextInput(
                label="Host user ID or mention",
                default=str(giveaway.get("host_id", "")),
                required=False,
                max_length=30,
            )

            for item in (
                self.monthly_input,
                self.total_input,
                self.channel_input,
                self.host_input,
            ):
                self.add_item(item)

        elif section == "extras":
            self.winner_role_input = discord.ui.TextInput(
                label="Winner role ID (blank removes it)",
                default=str(giveaway.get("winner_role") or ""),
                required=False,
                max_length=30,
            )

            self.dm_input = discord.ui.TextInput(
                label="Winner DM message",
                default=giveaway.get(
                    "winner_dm",
                    "Congratulations! You won **{prize}**!",
                ),
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )

            self.image_input = discord.ui.TextInput(
                label="Image URL",
                default=giveaway.get("image_url", ""),
                required=False,
                max_length=1000,
            )

            self.colour_input = discord.ui.TextInput(
                label="Embed colour (hex, e.g. #FFD700)",
                default=giveaway.get("colour", "#FFD700"),
                required=False,
                max_length=7,
            )

            for item in (
                self.winner_role_input,
                self.dm_input,
                self.image_input,
                self.colour_input,
            ):
                self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.editor_id:
            await interaction.response.send_message(
                "Only the person who opened this editor can use it.",
                ephemeral=True,
            )
            return

        giveaway = store.data.get(self.giveaway_id)

        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message(
                "This giveaway no longer exists or has ended.",
                ephemeral=True,
            )
            return

        try:
            if self.section == "basic":
                if self.title_input.value.strip():
                    giveaway["title"] = self.title_input.value.strip()

                if self.prize_input.value.strip():
                    giveaway["prize"] = self.prize_input.value.strip()

                giveaway["description"] = self.description_input.value.strip()

                if self.duration_input.value.strip():
                    seconds = parse_duration(self.duration_input.value)
                    giveaway["ends_at"] = time.time() + seconds

                if self.winners_input.value.strip():
                    winners = int(self.winners_input.value)

                    if not 1 <= winners <= 100:
                        raise ValueError(
                            "Winner count must be between 1 and 100."
                        )

                    giveaway["winners"] = winners

            elif self.section == "requirements":
                if self.extra_input.value.strip():
                    extra = int(self.extra_input.value)
                    if not 0 <= extra <= 100:
                        raise ValueError(
                            "Extra entries must be between 0 and 100."
                        )
                    giveaway["extra_entries"] = extra

                giveaway["required_roles"] = parse_role_ids(
                    self.required_roles_input.value
                )

                giveaway["bypass_roles"] = parse_role_ids(
                    self.bypass_roles_input.value
                )

                if self.daily_input.value.strip():
                    giveaway["daily_messages"] = max(
                        0, int(self.daily_input.value)
                    )

                if self.weekly_input.value.strip():
                    giveaway["weekly_messages"] = max(
                        0, int(self.weekly_input.value)
                    )

            elif self.section == "more":
                if self.monthly_input.value.strip():
                    giveaway["monthly_messages"] = max(
                        0, int(self.monthly_input.value)
                    )

                if self.total_input.value.strip():
                    giveaway["total_messages"] = max(
                        0, int(self.total_input.value)
                    )

                old_channel_id = int(giveaway.get("channel_id", 0))
                old_message_id = int(giveaway.get("message_id", 0))
                new_channel_id = old_channel_id

                if self.channel_input.value.strip():
                    channel_ids = parse_role_ids(self.channel_input.value)

                    if not channel_ids:
                        raise ValueError(
                            "Enter a valid channel ID or mention."
                        )

                    new_channel_id = channel_ids[0]

                host_id = int(giveaway.get("host_id", 0))

                if self.host_input.value.strip():
                    host_ids = parse_role_ids(self.host_input.value)

                    if not host_ids:
                        raise ValueError(
                            "Enter a valid host user ID or mention."
                        )

                    host_id = host_ids[0]

                guild = interaction.guild

                if guild is None:
                    raise ValueError(
                        "This editor must be used in the server."
                    )

                host_member = guild.get_member(host_id)

                if host_member is None:
                    try:
                        host_member = await guild.fetch_member(host_id)
                    except discord.HTTPException:
                        raise ValueError(
                            "That host is not a member of this server."
                        )

                giveaway["host_id"] = host_member.id

                if new_channel_id != old_channel_id:
                    destination = guild.get_channel(new_channel_id)

                    if destination is None:
                        try:
                            destination = await guild.fetch_channel(
                                new_channel_id
                            )
                        except discord.HTTPException:
                            raise ValueError(
                                "I could not find that channel."
                            )

                    if not isinstance(destination, discord.TextChannel):
                        raise ValueError(
                            "The giveaway channel must be a text channel."
                        )

                    bot_member = guild.me

                    if bot_member is None and self.cog.bot.user:
                        bot_member = guild.get_member(
                            self.cog.bot.user.id
                        )

                    if bot_member is None:
                        raise ValueError(
                            "I could not check my permissions in that channel."
                        )

                    bot_permissions = destination.permissions_for(bot_member)

                    if (
                        not bot_permissions.send_messages
                        or not bot_permissions.embed_links
                    ):
                        raise ValueError(
                            "I need permission to send messages and embed links "
                            "in that channel."
                        )

                    # Post in the new channel before changing the saved IDs.
                    new_message = await destination.send(
                        embed=make_embed(giveaway),
                        view=GiveawayJoinView(
                            self.cog,
                            self.giveaway_id,
                        ),
                    )

                    giveaway["channel_id"] = destination.id
                    giveaway["message_id"] = new_message.id

                    # Register the persistent view for the new message.
                    self.cog.bot.add_view(
                        GiveawayJoinView(self.cog, self.giveaway_id),
                        message_id=new_message.id,
                    )

                    store.save()

                    # Delete the old giveaway post if it still exists.
                    old_channel = self.cog.bot.get_channel(old_channel_id)

                    if old_channel and old_message_id:
                        try:
                            old_message = await old_channel.fetch_message(
                                old_message_id
                            )
                            await old_message.delete()
                        except discord.HTTPException:
                            pass

            elif self.section == "extras":
                role_ids = parse_role_ids(self.winner_role_input.value)

                if role_ids:
                    role = (
                        interaction.guild.get_role(role_ids[0])
                        if interaction.guild
                        else None
                    )

                    if role is None:
                        raise ValueError(
                            "That winner role was not found in this server."
                        )

                    giveaway["winner_role"] = role.id
                else:
                    giveaway["winner_role"] = None

                if self.dm_input.value.strip():
                    giveaway["winner_dm"] = self.dm_input.value.strip()

                image_url = self.image_input.value.strip()

                if image_url and not image_url.startswith(
                    ("https://", "http://")
                ):
                    raise ValueError(
                        "Image URL must start with http:// or https://."
                    )

                giveaway["image_url"] = image_url

                if self.colour_input.value.strip():
                    colour_value = (
                        self.colour_input.value.strip().replace("#", "")
                    )

                    if not re.fullmatch(
                        r"[0-9a-fA-F]{6}", colour_value
                    ):
                        raise ValueError(
                            "Colour must be a valid 6-digit hex code."
                        )

                    giveaway["colour"] = "#" + colour_value.upper()

            store.save()

            await interaction.response.send_message(
                "✅ Giveaway settings updated.",
                ephemeral=True,
            )

            await self.cog.refresh_message(giveaway)

        except ValueError as error:
            await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )

        except discord.HTTPException:
            message = (
                "Discord could not update the giveaway. "
                "Check the bot's permissions."
            )

            if not interaction.response.is_done():
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )


# =========================================================
# EDIT DASHBOARD
# =========================================================

class GiveawayEditView(discord.ui.View):
    def __init__(self, cog, giveaway_id: str, editor_id: int):
        super().__init__(timeout=300)

        self.cog = cog
        self.giveaway_id = giveaway_id
        self.editor_id = editor_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        if interaction.user.id != self.editor_id:
            await interaction.response.send_message(
                "This editing dashboard belongs to someone else.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Name, prize and duration",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def basic(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            GiveawayEditModal(
                self.cog,
                self.giveaway_id,
                "basic",
                self.editor_id,
            )
        )

    @discord.ui.button(
        label="Roles and extra entries",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def requirements(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            GiveawayEditModal(
                self.cog,
                self.giveaway_id,
                "requirements",
                self.editor_id,
            )
        )

    @discord.ui.button(
        label="Messages, channel and host",
        style=discord.ButtonStyle.primary,
        row=2,
    )
    async def more(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            GiveawayEditModal(
                self.cog,
                self.giveaway_id,
                "more",
                self.editor_id,
            )
        )

    @discord.ui.button(
        label="Winner role, DM, image and colour",
        style=discord.ButtonStyle.primary,
        row=3,
    )
    async def extras(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            GiveawayEditModal(
                self.cog,
                self.giveaway_id,
                "extras",
                self.editor_id,
            )
        )

    @discord.ui.button(
        label="Close editor",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def close_editor(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Giveaway editor closed.",
            embed=None,
            view=None,
        )


# =========================================================
# GIVEAWAY COG
# =========================================================

class GiveawayCog(
    commands.GroupCog,
    group_name="giveaway",
    group_description="Create and manage giveaways.",
):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register persistent join buttons after a restart.
        for giveaway_id, giveaway in list(store.data.items()):
            if not isinstance(giveaway, dict):
                continue

            if giveaway.get("ended"):
                continue

            if not valid_giveaway_record(giveaway):
                continue

            try:
                self.bot.add_view(
                    GiveawayJoinView(self, giveaway_id),
                    message_id=int(giveaway["message_id"]),
                )
            except (TypeError, ValueError, discord.HTTPException):
                pass

        self.expiry_loop.start()

    def cog_unload(self):
        self.expiry_loop.cancel()

    async def get_channel_safely(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)

        if channel is not None:
            return channel

        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def refresh_message(self, giveaway: dict):
        if giveaway.get("ended"):
            return

        channel_id = giveaway.get("channel_id")
        message_id = giveaway.get("message_id")

        if not channel_id or not message_id:
            return

        try:
            channel = await self.get_channel_safely(int(channel_id))

            if channel is None:
                return

            message = await channel.fetch_message(int(message_id))

            await message.edit(
                embed=make_embed(giveaway),
                view=GiveawayJoinView(self, giveaway["id"]),
            )

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
            AttributeError,
        ):
            pass

    async def can_manage(
        self,
        interaction: discord.Interaction,
        giveaway: dict,
    ) -> bool:

        if interaction.user.guild_permissions.manage_guild:
            return True

        if interaction.user.id == int(giveaway.get("host_id", 0)):
            return True

        await interaction.response.send_message(
            "You must be the giveaway host or have Manage Server permission.",
            ephemeral=True,
        )
        return False

    async def finish_giveaway(self, giveaway_id: str):
        giveaway = store.data.get(giveaway_id)

        if not isinstance(giveaway, dict) or giveaway.get("ended"):
            return []

        # IMPORTANT: validate old/incomplete records before indexing fields.
        # This prevents KeyError crashes in the background expiry task.
        if not valid_giveaway_record(giveaway):
            giveaway["ended"] = True
            giveaway["ended_at"] = time.time()
            giveaway["error"] = (
                "Incomplete giveaway record: missing or invalid "
                "guild_id, channel_id, message_id, ends_at or prize."
            )
            store.save()
            return []

        guild_id = int(giveaway["guild_id"])
        channel_id = int(giveaway["channel_id"])
        message_id = int(giveaway["message_id"])

        guild = self.bot.get_guild(guild_id)

        # Do not mark it ended if the bot has not loaded the guild.
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return []

        # Mark as ended only after validating the record and guild.
        giveaway["ended"] = True
        giveaway["ended_at"] = time.time()
        store.save()

        # fetch_guild returns a Guild object, but members may not be cached.
        # The configured members intent is needed for cached winner selection.
        winners = draw_winners(
            giveaway,
            guild,
            int(giveaway.get("winners", 1)),
        )

        winner_ids = [member.id for member in winners]
        giveaway["previous_winners"] = winner_ids
        store.save()

        channel = await self.get_channel_safely(channel_id)

        winner_text = (
            ", ".join(member.mention for member in winners)
            if winners
            else "Nobody won. There were no eligible entrants."
        )

        if channel:
            try:
                await channel.send(
                    f"🎉 **Giveaway ended!**\n"
                    f"**Prize:** {giveaway.get('prize', 'Unknown prize')}\n"
                    f"**Winner(s):** {winner_text}"
                )
            except discord.HTTPException:
                pass

        winner_role_id = giveaway.get("winner_role")

        if winner_role_id:
            role = guild.get_role(int(winner_role_id))

            if role:
                for winner in winners:
                    try:
                        await winner.add_roles(
                            role,
                            reason="Giveaway winner",
                        )
                    except discord.HTTPException:
                        pass

        dm_message = giveaway.get(
            "winner_dm",
            "Congratulations! You won **{prize}**!",
        )

        for winner in winners:
            try:
                await winner.send(
                    dm_message.format(
                        prize=giveaway.get("prize", "the giveaway"),
                        title=giveaway.get("title", "Giveaway"),
                        server=guild.name,
                    )
                )
            except (
                discord.Forbidden,
                discord.HTTPException,
                KeyError,
                ValueError,
            ):
                pass

        # Safely update the original giveaway post.
        # Missing message IDs can no longer crash the expiry loop.
        if channel and message_id:
            try:
                message = await channel.fetch_message(message_id)

                ended_embed = make_embed(giveaway)
                ended_embed.title = "🏁 GIVEAWAY ENDED"
                ended_embed.add_field(
                    name="🏆 Winner(s)",
                    value=winner_text,
                    inline=False,
                )

                await message.edit(
                    embed=ended_embed,
                    view=None,
                )

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass

        return winners

    @tasks.loop(seconds=20)
    async def expiry_loop(self):
        now = time.time()

        for giveaway_id, giveaway in list(store.data.items()):
            try:
                if not isinstance(giveaway, dict):
                    continue

                if giveaway.get("ended"):
                    continue

                # Skip broken old records instead of crashing the task.
                if not valid_giveaway_record(giveaway):
                    continue

                if float(giveaway["ends_at"]) <= now:
                    await self.finish_giveaway(giveaway_id)

            except Exception:
                import logging
                logging.exception(
                    "Failed to process giveaway expiry for ID %s",
                    giveaway_id,
                )

    @expiry_loop.before_loop
    async def before_expiry_loop(self):
        await self.bot.wait_until_ready()

    # -----------------------------------------------------
    # CREATE
    # -----------------------------------------------------

    @app_commands.command(
        name="create",
        description="Create a giveaway with your chosen settings.",
    )
    @app_commands.describe(
        prize="What people can win",
        duration="For example: 30m, 2h, 3d, 1w",
        winners="Number of winners",
        channel="Where the giveaway should be posted",
        title="Giveaway name/title",
        description="Extra information shown in the giveaway",
        extra_entries="Bonus tickets for each eligible entrant",
        required_roles="Role IDs or mentions; entrants need every listed role",
        bypass_roles="Roles that bypass all requirements",
        daily_messages="Messages required in the last 24 hours",
        weekly_messages="Messages required in the last 7 days",
        monthly_messages="Messages required in the last 30 days",
        total_messages="Total tracked messages required",
        winner_role="Role given to each winner",
        winner_dm="Message sent to each winner; use {prize}, {title}, {server}",
        image_url="Optional image URL",
        host="Giveaway host; defaults to you",
        colour="Embed colour as a hex value, e.g. #FFD700",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 100],
        channel: discord.TextChannel,
        title: str = "🎉 GIVEAWAY 🎉",
        description: str = "",
        extra_entries: app_commands.Range[int, 0, 100] = 0,
        required_roles: str = "",
        bypass_roles: str = "",
        daily_messages: app_commands.Range[int, 0, 100000] = 0,
        weekly_messages: app_commands.Range[int, 0, 100000] = 0,
        monthly_messages: app_commands.Range[int, 0, 100000] = 0,
        total_messages: app_commands.Range[int, 0, 1000000] = 0,
        winner_role: Optional[discord.Role] = None,
        winner_dm: str = "Congratulations! You won **{prize}**!",
        image_url: str = "",
        host: Optional[discord.Member] = None,
        colour: str = "#FFD700",
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Giveaways can only be created inside a server.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None and self.bot.user:
            bot_member = interaction.guild.get_member(self.bot.user.id)

        if bot_member is None:
            await interaction.response.send_message(
                "I couldn't check my permissions in that channel.",
                ephemeral=True,
            )
            return

        permissions = channel.permissions_for(bot_member)

        if not permissions.send_messages or not permissions.embed_links:
            await interaction.response.send_message(
                "I need permission to send messages and embed links in that channel.",
                ephemeral=True,
            )
            return

        try:
            duration_seconds = parse_duration(duration)
        except ValueError as error:
            await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )
            return

        colour_value = colour.strip().replace("#", "")

        if not re.fullmatch(r"[0-9a-fA-F]{6}", colour_value):
            await interaction.response.send_message(
                "Colour must be a 6-digit hex value, such as `#FFD700`.",
                ephemeral=True,
            )
            return

        if len(prize) > 200 or len(title) > 256 or len(description) > 1000:
            await interaction.response.send_message(
                "The title, prize or description is too long.",
                ephemeral=True,
            )
            return

        if image_url and not image_url.startswith(("https://", "http://")):
            await interaction.response.send_message(
                "Image URL must start with http:// or https://.",
                ephemeral=True,
            )
            return

        giveaway_id = (
            str(int(time.time() * 1000))
            + str(random.randint(100, 999))
        )

        host = host or interaction.user

        giveaway = {
            "id": giveaway_id,
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "message_id": 0,
            "host_id": host.id,

            "title": title,
            "description": description,
            "prize": prize,
            "colour": "#" + colour_value.upper(),
            "image_url": image_url,

            "winners": int(winners),
            "extra_entries": int(extra_entries),

            "required_roles": parse_role_ids(required_roles),
            "bypass_roles": parse_role_ids(bypass_roles),

            "daily_messages": int(daily_messages),
            "weekly_messages": int(weekly_messages),
            "monthly_messages": int(monthly_messages),
            "total_messages": int(total_messages),

            "winner_role": winner_role.id if winner_role else None,
            "winner_dm": winner_dm,

            "created_at": time.time(),
            "ends_at": time.time() + duration_seconds,
            "ended": False,

            "entries": {},
            "previous_winners": [],
        }

        store.data[giveaway_id] = giveaway
        store.save()

        try:
            message = await channel.send(
                embed=make_embed(giveaway),
                view=GiveawayJoinView(self, giveaway_id),
            )

        except discord.HTTPException:
            store.data.pop(giveaway_id, None)
            store.save()

            await interaction.response.send_message(
                "I couldn't post the giveaway. Check my channel permissions.",
                ephemeral=True,
            )
            return

        giveaway["message_id"] = message.id
        store.save()

        # Register the persistent view against the posted message.
        self.bot.add_view(
            GiveawayJoinView(self, giveaway_id),
            message_id=message.id,
        )

        await interaction.response.send_message(
            f"✅ Giveaway created in {channel.mention}!\n"
            f"Giveaway ID: `{giveaway_id}`",
            ephemeral=True,
        )

    # -----------------------------------------------------
    # EDIT
    # -----------------------------------------------------

    @app_commands.command(
        name="edit",
        description="Open the full editing dashboard for an active giveaway.",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
    ):
        giveaway = store.data.get(giveaway_id)

        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message(
                "I couldn't find an active giveaway with that ID.",
                ephemeral=True,
            )
            return

        if interaction.guild_id != int(giveaway.get("guild_id", 0)):
            await interaction.response.send_message(
                "That giveaway belongs to another server.",
                ephemeral=True,
            )
            return

        if not await self.can_manage(interaction, giveaway):
            return

        embed = discord.Embed(
            title="Giveaway editing dashboard",
            description=(
                f"Editing **{giveaway.get('title', 'Giveaway')}**\n\n"
                "Choose a section below to edit its settings.\n\n"
                "• Name, prize, description, duration and winners\n"
                "• Extra entries, required roles and bypass roles\n"
                "• Daily, weekly, monthly and total message requirements\n"
                "• Giveaway channel and host\n"
                "• Winner role, winner DM, image and embed colour"
            ),
            colour=discord.Colour.blurple(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=GiveawayEditView(
                self,
                giveaway_id,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    # -----------------------------------------------------
    # END
    # -----------------------------------------------------

    @app_commands.command(
        name="end",
        description="End an active giveaway immediately.",
    )
    async def end(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
    ):
        giveaway = store.data.get(giveaway_id)

        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message(
                "That giveaway does not exist or has already ended.",
                ephemeral=True,
            )
            return

        if interaction.guild_id != int(giveaway.get("guild_id", 0)):
            await interaction.response.send_message(
                "That giveaway belongs to another server.",
                ephemeral=True,
            )
            return

        if not await self.can_manage(interaction, giveaway):
            return

        await interaction.response.defer(ephemeral=True)

        await self.finish_giveaway(giveaway_id)

        await interaction.followup.send(
            "🏁 Giveaway ended.",
            ephemeral=True,
        )

    # -----------------------------------------------------
    # REROLL
    # -----------------------------------------------------

    @app_commands.command(
        name="reroll",
        description="Choose a new winner from an ended giveaway.",
    )
    async def reroll(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
        winners: app_commands.Range[int, 1, 100] = 1,
    ):
        giveaway = store.data.get(giveaway_id)

        if not giveaway or not giveaway.get("ended"):
            await interaction.response.send_message(
                "That giveaway does not exist or has not ended yet.",
                ephemeral=True,
            )
            return

        if interaction.guild_id != int(giveaway.get("guild_id", 0)):
            await interaction.response.send_message(
                "That giveaway belongs to another server.",
                ephemeral=True,
            )
            return

        if not await self.can_manage(interaction, giveaway):
            return

        guild = interaction.guild

        if guild is None:
            await interaction.response.send_message(
                "This command must be used in the server.",
                ephemeral=True,
            )
            return

        excluded = {
            int(user_id)
            for user_id in giveaway.get("previous_winners", [])
        }

        new_winners = draw_winners(
            giveaway,
            guild,
            int(winners),
            excluded=excluded,
        )

        if not new_winners:
            await interaction.response.send_message(
                "There are no other eligible entrants to reroll.",
                ephemeral=True,
            )
            return

        new_ids = [member.id for member in new_winners]

        giveaway.setdefault("previous_winners", []).extend(new_ids)
        store.save()

        winner_text = ", ".join(
            member.mention for member in new_winners
        )

        channel = await self.get_channel_safely(
            int(giveaway.get("channel_id", 0))
        )

        if channel:
            try:
                await channel.send(
                    f"🔄 **Giveaway reroll!**\n"
                    f"**Prize:** {giveaway.get('prize', 'Unknown prize')}\n"
                    f"**New winner(s):** {winner_text}"
                )
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            f"New winner(s): {winner_text}",
            ephemeral=True,
        )

    # -----------------------------------------------------
    # MESSAGE TRACKING
    # -----------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        store.add_message(
            message.guild.id,
            message.author.id,
        )


# =========================================================
# EXTENSION SETUP
# =========================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))