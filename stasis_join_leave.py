import discord
from discord import app_commands
from discord.ext import commands

import json
import logging
import os
import tempfile


# ============================================================
# CONFIGURATION
# ============================================================

CONFIG_FILE = "stasis_join_leave_config.json"

logger = logging.getLogger("stasis_join_leave")


# ============================================================
# CONFIG FILE FUNCTIONS
# ============================================================

def load_config():
    """Load join/leave settings from the JSON file."""
    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except (json.JSONDecodeError, OSError):
        logger.exception("Could not load the join/leave configuration.")
        return {}


def save_config(config):
    """Save settings safely using an atomic file replacement."""
    directory = os.path.dirname(os.path.abspath(CONFIG_FILE))

    fd, temporary_file = tempfile.mkstemp(
        prefix="stasis_join_leave_",
        suffix=".tmp",
        dir=directory
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=4)

        os.replace(temporary_file, CONFIG_FILE)

    except Exception:
        try:
            os.remove(temporary_file)
        except OSError:
            pass

        raise


def get_guild_config(guild_id):
    """Get settings for one server."""
    config = load_config()
    return config.get(str(guild_id), {})


# ============================================================
# EMBED DESIGN
# ============================================================

def get_server_logo(guild):
    """Use the server icon as the STASIS logo."""
    if guild.icon:
        return guild.icon.url

    return None


def create_welcome_embed(member):
    guild = member.guild

    embed = discord.Embed(
        title="Welcome to STASIS",
        description=(
            f"Welcome {member.mention} to **{guild.name}**.\n\n"
            "We're glad to have you here. Please take a moment to "
            "familiarise yourself with the server and its rules."
        ),
        color=discord.Color.from_rgb(88, 101, 242)
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    logo = get_server_logo(guild)

    if logo:
        embed.set_author(
            name="STASIS",
            icon_url=logo
        )

    embed.add_field(
        name="Member",
        value=member.mention,
        inline=True
    )

    embed.add_field(
        name="Member Count",
        value=f"{guild.member_count:,}",
        inline=True
    )

    embed.add_field(
        name="Account Created",
        value=f"<t:{int(member.created_at.timestamp())}:R>",
        inline=False
    )

    embed.set_footer(
        text="STASIS • Member Updates"
    )

    embed.timestamp = discord.utils.utcnow()

    return embed


def create_farewell_embed(member):
    guild = member.guild

    remaining_members = max(
        0,
        (guild.member_count or 1) - 1
    )

    embed = discord.Embed(
        title="Member Departed",
        description=(
            f"**{discord.utils.escape_markdown(member.display_name)}** "
            f"has left **{discord.utils.escape_markdown(guild.name)}**.\n\n"
            "We appreciate the time they spent in the community."
        ),
        color=discord.Color.from_rgb(75, 75, 85)
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    logo = get_server_logo(guild)

    if logo:
        embed.set_author(
            name="STASIS",
            icon_url=logo
        )

    embed.add_field(
        name="Member",
        value=discord.utils.escape_markdown(member.name),
        inline=True
    )

    embed.add_field(
        name="Members Remaining",
        value=f"{remaining_members:,}",
        inline=True
    )

    embed.set_footer(
        text="STASIS • Member Updates"
    )

    embed.timestamp = discord.utils.utcnow()

    return embed


# ============================================================
# JOIN / LEAVE COG
# ============================================================

class JoinLeaveMessageCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    # --------------------------------------------------------
    # PERMISSION CHECK
    # --------------------------------------------------------

    @staticmethod
    def has_management_permission(interaction):
        permissions = interaction.user.guild_permissions

        return (
            permissions.manage_guild
            or permissions.administrator
        )

    # --------------------------------------------------------
    # SETUP COMMAND
    # --------------------------------------------------------

    @app_commands.command(
        name="joinleavemessage",
        description="Configure the STASIS welcome and farewell messages."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        joinmessage="Enable or disable welcome messages.",
        leavemessage="Enable or disable farewell messages.",
        channel="The channel where messages will be sent."
    )
    @app_commands.choices(
        joinmessage=[
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="No", value="no")
        ],
        leavemessage=[
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="No", value="no")
        ]
    )
    async def joinleavemessage(
        self,
        interaction: discord.Interaction,
        joinmessage: str,
        leavemessage: str,
        channel: discord.TextChannel
    ):

        if not self.has_management_permission(interaction):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this command.",
                ephemeral=True
            )
            return

        bot_permissions = channel.permissions_for(
            interaction.guild.me
        )

        if not bot_permissions.send_messages:
            await interaction.response.send_message(
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True
            )
            return

        if not bot_permissions.embed_links:
            await interaction.response.send_message(
                f"I need the **Embed Links** permission in {channel.mention}.",
                ephemeral=True
            )
            return

        config = load_config()

        config[str(interaction.guild.id)] = {
            "channel_id": channel.id,
            "join_enabled": joinmessage == "yes",
            "leave_enabled": leavemessage == "yes"
        }

        try:
            save_config(config)

        except OSError:
            logger.exception("Failed to save join/leave configuration.")

            await interaction.response.send_message(
                "I couldn't save the configuration. Please check my file permissions.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Join & Leave Settings Updated",
            description=(
                "Your STASIS member update settings have been saved."
            ),
            color=discord.Color.from_rgb(88, 101, 242)
        )

        embed.add_field(
            name="Welcome Messages",
            value="Enabled" if joinmessage == "yes" else "Disabled",
            inline=True
        )

        embed.add_field(
            name="Farewell Messages",
            value="Enabled" if leavemessage == "yes" else "Disabled",
            inline=True
        )

        embed.add_field(
            name="Message Channel",
            value=channel.mention,
            inline=False
        )

        logo = get_server_logo(interaction.guild)

        if logo:
            embed.set_author(
                name="STASIS",
                icon_url=logo
            )

        embed.set_footer(
            text="STASIS • Configuration"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    # --------------------------------------------------------
    # WELCOME EVENT
    # --------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member):

        settings = get_guild_config(member.guild.id)

        if not settings.get("join_enabled", False):
            return

        channel_id = settings.get("channel_id")

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not find the configured welcome channel."
                )
                return

        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(
                embed=create_welcome_embed(member)
            )

        except discord.Forbidden:
            logger.warning(
                "Missing permissions to send a welcome message."
            )

        except discord.HTTPException:
            logger.exception(
                "Failed to send a welcome message."
            )

    # --------------------------------------------------------
    # FAREWELL EVENT
    # --------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member):

        settings = get_guild_config(member.guild.id)

        if not settings.get("leave_enabled", False):
            return

        channel_id = settings.get("channel_id")

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not find the configured farewell channel."
                )
                return

        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(
                embed=create_farewell_embed(member)
            )

        except discord.Forbidden:
            logger.warning(
                "Missing permissions to send a farewell message."
            )

        except discord.HTTPException:
            logger.exception(
                "Failed to send a farewell message."
            )

    # --------------------------------------------------------
    # WELCOME MESSAGE TEST
    # --------------------------------------------------------

    @app_commands.command(
        name="joinmessagetest",
        description="Preview the STASIS welcome message."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def joinmessagetest(
        self,
        interaction: discord.Interaction
    ):

        if not self.has_management_permission(interaction):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this command.",
                ephemeral=True
            )
            return

        embed = create_welcome_embed(interaction.user)

        await interaction.response.send_message(
            content="**Welcome message preview:**",
            embed=embed,
            ephemeral=True
        )

    # --------------------------------------------------------
    # FAREWELL MESSAGE TEST
    # --------------------------------------------------------

    @app_commands.command(
        name="leavemessagetest",
        description="Preview the STASIS farewell message."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leavemessagetest(
        self,
        interaction: discord.Interaction
    ):

        if not self.has_management_permission(interaction):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this command.",
                ephemeral=True
            )
            return

        embed = create_farewell_embed(interaction.user)

        await interaction.response.send_message(
            content="**Farewell message preview:**",
            embed=embed,
            ephemeral=True
        )


# ============================================================
# EXTENSION SETUP
# ============================================================

async def setup(bot):
    await bot.add_cog(JoinLeaveMessageCog(bot))