
import os
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv


# ============================================================
# ENVIRONMENT AND CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MODERATION_IMAGE_URL = os.getenv("MODERATION_IMAGE_URL", "")

GUILD_IDS = [
    int(value.strip())
    for value in os.getenv("GUILD_IDS", "").split(",")
    if value.strip().isdigit()
]

CONFIG_FILE = "bot_config.json"
WARNINGS_FILE = "warnings.json"
TICKETS_FILE = "tickets.json"

SUPERUSER_ID = 1307023189561573431
DEFAULT_PREFIX = "!"

commands_synced = False


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("stasis_bot")


# ============================================================
# JSON HELPERS
# ============================================================

def save_json(filename, data):
    temporary_file = filename + ".tmp"

    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)

        os.replace(temporary_file, filename)

    except OSError:
        logger.exception("Could not save %s", filename)


def load_json(filename, default):
    if not os.path.exists(filename):
        result = default.copy() if isinstance(default, dict) else default
        save_json(filename, result)
        return result

    try:
        with open(filename, "r", encoding="utf-8") as file:
            content = file.read().strip()

            if not content:
                raise json.JSONDecodeError("Empty file", "", 0)

            return json.loads(content)

    except (json.JSONDecodeError, OSError):
        logger.warning("Resetting invalid file: %s", filename)

        result = default.copy() if isinstance(default, dict) else default
        save_json(filename, result)
        return result


config = load_json(CONFIG_FILE, {})
warnings_data = load_json(WARNINGS_FILE, {})
tickets_data = load_json(TICKETS_FILE, {})

for key in (
    "prefixes",
    "log_channels",
    "admin_roles",
    "member_roles",
    "superusers",
    "permissions",
    "ticket_settings",
    "ticket_pings"
):
    config.setdefault(key, {})


def save_config():
    save_json(CONFIG_FILE, config)


def save_warnings():
    save_json(WARNINGS_FILE, warnings_data)


def save_tickets():
    save_json(TICKETS_FILE, tickets_data)


# ============================================================
# PERMISSION AUTOCOMPLETE
# ============================================================

PERMISSION_COMMANDS = [
    "ticket",
    "ticketping",
    "reactionrole",
    "clear",
    "kick",
    "ban",
    "unban",
    "timeout",
    "untimeout",
    "warn",
    "warnings",
    "unwarn",
    "lock",
    "unlock",
    "slowmode",
    "promote",
    "demote",
    "setlogchannel",
    "adminrole",
    "member",
    "superuser",
    "permission",
    "prefix"
]


async def permission_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return [
        app_commands.Choice(name=command, value=command)
        for command in PERMISSION_COMMANDS
        if current.lower() in command.lower()
    ][:25]


# ============================================================
# BOT SETUP
# ============================================================

async def get_prefix(bot, message):
    if message.guild is None:
        return DEFAULT_PREFIX

    return config.get("prefixes", {}).get(
        str(message.guild.id),
        DEFAULT_PREFIX
    )


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None
)


# ============================================================
# PERMISSIONS
# ============================================================

def is_superuser(user_id):
    return user_id == SUPERUSER_ID


def has_configured_role(member, role_type):

    role_id = config.get(role_type, {}).get(
        str(member.guild.id)
    )

    if not role_id:
        return False

    try:
        role_id = int(role_id)
    except (TypeError, ValueError):
        return False

    return any(role.id == role_id for role in member.roles)


def command_access(command_name):

    async def predicate(ctx):

        if ctx.guild is None:
            return False

        if is_superuser(ctx.author.id):
            return True

        if ctx.author.id == ctx.guild.owner_id:
            return True

        if not isinstance(ctx.author, discord.Member):
            return False

        if ctx.author.guild_permissions.administrator:
            return True

        if has_configured_role(ctx.author, "admin_roles"):
            return True

        server_superusers = config.get(
            "superusers", {}
        ).get(str(ctx.guild.id), [])

        if ctx.author.id in [int(x) for x in server_superusers]:
            return True

        allowed = config.get(
            "permissions", {}
        ).get(str(ctx.guild.id), {}).get(command_name, [])

        return ctx.author.id in [int(x) for x in allowed]

    return commands.check(predicate)


# ============================================================
# GENERAL HELPERS
# ============================================================

def make_embed(
    title,
    description=None,
    color=discord.Color.blurple()
):

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    if MODERATION_IMAGE_URL:
        embed.set_thumbnail(url=MODERATION_IMAGE_URL)

    return embed


async def respond(ctx, content=None, *, embed=None, ephemeral=False):

    if isinstance(ctx, discord.Interaction):

        if ctx.response.is_done():
            await ctx.followup.send(
                content,
                embed=embed,
                ephemeral=ephemeral
            )
        else:
            await ctx.response.send_message(
                content,
                embed=embed,
                ephemeral=ephemeral
            )

    else:
        await ctx.send(content, embed=embed)


def get_role(guild, role_id):

    if not role_id:
        return None

    try:
        return guild.get_role(int(role_id))
    except (TypeError, ValueError):
        return None


async def send_log(guild, embed):

    if guild is None:
        return

    guild_id = str(guild.id)

    channel_id = config.get(
        "log_channels", {}
    ).get(guild_id)

    if not channel_id:
        channel_id = config.get(
            "ticket_settings", {}
        ).get(guild_id, {}).get("log_channel_id")

    if not channel_id:
        return

    try:
        channel = guild.get_channel(int(channel_id))

        if channel is None:
            channel = await guild.fetch_channel(int(channel_id))

        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        TypeError,
        ValueError
    ):
        logger.exception("Could not send log")


# ============================================================
# TICKET SETTINGS
# ============================================================

def get_ticket_settings(guild):

    return config.get(
        "ticket_settings", {}
    ).get(str(guild.id), {})


def get_ticket_staff_role(guild):

    settings = get_ticket_settings(guild)

    return get_role(
        guild,
        settings.get("staff_role_id")
    )


def get_ticket_category(guild):

    settings = get_ticket_settings(guild)

    category_id = settings.get("category_id")

    if not category_id:
        return None

    try:
        category = guild.get_channel(int(category_id))
    except (TypeError, ValueError):
        return None

    if isinstance(category, discord.CategoryChannel):
        return category

    return None


def get_ticket_ping_roles(guild, ticket_type):

    guild_pings = config.get(
        "ticket_pings", {}
    ).get(str(guild.id), {})

    role_ids = guild_pings.get(ticket_type, [])

    if not isinstance(role_ids, list):
        role_ids = [role_ids]

    roles = []

    for role_id in role_ids:
        role = get_role(guild, role_id)

        if role and role not in roles:
            roles.append(role)

    return roles


# ============================================================
# TICKET TYPES
# ============================================================

TICKET_TYPES = {

    "support": {
        "label": "Support",
        "emoji": "🎫",
        "description": "Get help with something.",
        "style": discord.ButtonStyle.primary,
        "questions": [
            ("What do you need help with?", "Describe your issue."),
            ("What have you tried?", "Tell us what you have tried.")
        ]
    },

    "bug": {
        "label": "Bug Report",
        "emoji": "🐛",
        "description": "Report a bug or issue.",
        "style": discord.ButtonStyle.success,
        "questions": [
            ("What is the bug?", "Describe the bug."),
            ("How can we reproduce it?", "Steps to reproduce the bug.")
        ]
    },

    "appeal": {
        "label": "Appeal",
        "emoji": "⚖️",
        "description": "Appeal a punishment.",
        "style": discord.ButtonStyle.success,
        "questions": [
            ("What punishment are you appealing?", "Describe the punishment."),
            ("Why should it be appealed?", "Explain your appeal.")
        ]
    },

    "player_report": {
        "label": "Player Report",
        "emoji": "🚨",
        "description": "Report another player.",
        "style": discord.ButtonStyle.danger,
        "questions": [
            ("Who are you reporting?", "Username or user ID."),
            ("What happened?", "Describe what happened."),
            ("Do you have evidence?", "Links or other evidence.")
        ]
    },

    "staff_report": {
        "label": "Staff Report",
        "emoji": "🛡️",
        "description": "Report a staff member.",
        "style": discord.ButtonStyle.danger,
        "questions": [
            ("Who are you reporting?", "Staff member's username."),
            ("What happened?", "Describe what happened."),
            ("Do you have evidence?", "Links or other evidence.")
        ]
    },

    "media": {
        "label": "Media",
        "emoji": "📸",
        "description": "Media-related questions.",
        "style": discord.ButtonStyle.secondary,
        "questions": [
            ("What is your request?", "Describe your media request."),
            ("Additional information", "Anything else we should know?")
        ]
    }

}


TICKET_TYPE_CHOICES = [
    app_commands.Choice(name="Support", value="support"),
    app_commands.Choice(name="Bug Report", value="bug"),
    app_commands.Choice(name="Appeal", value="appeal"),
    app_commands.Choice(name="Player Report", value="player_report"),
    app_commands.Choice(name="Staff Report", value="staff_report"),
    app_commands.Choice(name="Media", value="media")
]


# ============================================================
# TICKET CREATION MODAL
# ============================================================

class TicketModal(discord.ui.Modal):

    def __init__(self, ticket_type):

        self.ticket_type = ticket_type
        info = TICKET_TYPES[ticket_type]

        super().__init__(title=f"{info['label']} Ticket")

        self.inputs = []

        for label, placeholder in info["questions"]:

            text_input = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000
            )

            self.inputs.append(text_input)
            self.add_item(text_input)

    async def on_submit(self, interaction):

        guild = interaction.guild
        user = interaction.user

        if guild is None:
            return await interaction.response.send_message(
                "Tickets can only be created inside a server.",
                ephemeral=True
            )

        settings = get_ticket_settings(guild)

        if not settings:
            return await interaction.response.send_message(
                "Tickets have not been configured. Ask an administrator to run `/ticket`.",
                ephemeral=True
            )

        category = get_ticket_category(guild)
        staff_role = get_ticket_staff_role(guild)

        if category is None or staff_role is None:
            return await interaction.response.send_message(
                "The ticket category or staff role is missing. Please configure `/ticket` again.",
                ephemeral=True
            )

        guild_tickets = tickets_data.setdefault(str(guild.id), {})

        existing_ticket = next(
            (
                ticket_id
                for ticket_id, ticket in guild_tickets.items()
                if int(ticket.get("owner_id", 0)) == user.id
                and not ticket.get("closed", False)
            ),
            None
        )

        if existing_ticket:
            existing_channel = guild.get_channel(int(existing_ticket))

            if existing_channel:
                return await interaction.response.send_message(
                    f"You already have an open ticket: {existing_channel.mention}",
                    ephemeral=True
                )

        safe_name = "".join(
            character.lower() if character.isalnum() else "-"
            for character in user.name
        ).strip("-")

        channel_name = f"{self.ticket_type}-{safe_name}"[:90]

        ping_roles = get_ticket_ping_roles(
            guild,
            self.ticket_type
        )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),

            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),

            staff_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True
            )
        }

        for ping_role in ping_roles:
            overwrites[ping_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            )

        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True
            )

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket owner: {user.id} | Type: {self.ticket_type}"
            )

        except discord.Forbidden:
            return await interaction.response.send_message(
                "I do not have permission to create ticket channels.",
                ephemeral=True
            )

        except discord.HTTPException:
            logger.exception("Could not create ticket channel")

            return await interaction.response.send_message(
                "Something went wrong while creating your ticket.",
                ephemeral=True
            )

        answers = {
            text_input.label: text_input.value
            for text_input in self.inputs
        }

        guild_tickets[str(channel.id)] = {
            "owner_id": user.id,
            "ticket_type": self.ticket_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "closed": False
        }

        save_tickets()

        info = TICKET_TYPES[self.ticket_type]

        embed = discord.Embed(
            title=f"{info['emoji']} {info['label']} Ticket",
            description=(
                f"Welcome {user.mention}!\n\n"
                "A member of the staff team will assist you shortly."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Ticket Creator",
            value=f"{user.mention} (`{user.id}`)",
            inline=False
        )

        for question, answer in answers.items():
            embed.add_field(
                name=question,
                value=answer[:1024],
                inline=False
            )

        mentions = [staff_role.mention]

        for role in ping_roles:
            if role.id != staff_role.id:
                mentions.append(role.mention)

        await channel.send(
            content=" ".join(mentions),
            embed=embed,
            view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False
            )
        )

        await interaction.response.send_message(
            f"Your ticket has been created: {channel.mention}",
            ephemeral=True
        )

        await send_log(
            guild,
            make_embed(
                "Ticket Created",
                f"Ticket: {channel.mention}\n"
                f"Creator: {user.mention}\n"
                f"Type: {info['label']}",
                discord.Color.green()
            )
        )


# ============================================================
# TICKET PANEL BUTTONS
# ============================================================

class TicketButton(discord.ui.Button):

    def __init__(self, ticket_type, label, emoji, style):

        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"ticket:create:{ticket_type}"
        )

        self.ticket_type = ticket_type

    async def callback(self, interaction):
        await interaction.response.send_modal(
            TicketModal(self.ticket_type)
        )


class TicketPanelView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

        for ticket_type, info in TICKET_TYPES.items():
            self.add_item(
                TicketButton(
                    ticket_type,
                    info["label"],
                    info["emoji"],
                    info["style"]
                )
            )


class TicketDropdown(discord.ui.Select):

    def __init__(self):

        options = [
            discord.SelectOption(
                label=info["label"],
                value=ticket_type,
                emoji=info["emoji"],
                description=info["description"]
            )
            for ticket_type, info in TICKET_TYPES.items()
        ]

        super().__init__(
            placeholder="Choose a ticket type...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket:dropdown"
        )

    async def callback(self, interaction):
        await interaction.response.send_modal(
            TicketModal(self.values[0])
        )


class TicketDropdownView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())


# ============================================================
# TICKET CLOSE MODAL
# ============================================================

class TicketCloseModal(discord.ui.Modal):

    def __init__(self):

        super().__init__(title="Close Ticket")

        self.reason = discord.ui.TextInput(
            label="Reason for closing",
            placeholder="Enter the reason for closing this ticket...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000
        )

        self.add_item(self.reason)

    async def on_submit(self, interaction):

        guild = interaction.guild
        channel = interaction.channel

        if guild is None or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "This can only be used inside a ticket channel.",
                ephemeral=True
            )

        ticket = tickets_data.get(
            str(guild.id), {}
        ).get(str(channel.id))

        if not ticket:
            return await interaction.response.send_message(
                "This channel is not registered as a ticket.",
                ephemeral=True
            )

        if ticket.get("closed"):
            return await interaction.response.send_message(
                "This ticket is already closed.",
                ephemeral=True
            )

        staff_role = get_ticket_staff_role(guild)

        is_staff = (
            isinstance(interaction.user, discord.Member)
            and (
                interaction.user.guild_permissions.administrator
                or interaction.user.guild_permissions.manage_channels
                or interaction.user.id == guild.owner_id
                or is_superuser(interaction.user.id)
                or (
                    staff_role is not None
                    and staff_role in interaction.user.roles
                )
            )
        )

        if not is_staff:
            return await interaction.response.send_message(
                "You do not have permission to close this ticket.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "Closing ticket...",
            ephemeral=True
        )

        owner_id = ticket.get("owner_id")
        owner = guild.get_member(int(owner_id)) if owner_id else None

        ticket["closed"] = True
        ticket["closed_at"] = datetime.now(timezone.utc).isoformat()
        ticket["closed_by"] = interaction.user.id
        ticket["close_reason"] = self.reason.value

        save_tickets()

        if owner:
            try:
                dm_embed = make_embed(
                    "🔒 Your Ticket Has Been Closed",
                    (
                        f"Your ticket in **{guild.name}** has been closed.\n\n"
                        f"**Closed by:** {interaction.user}\n"
                        f"**Reason:** {self.reason.value}\n\n"
                        "If you still need assistance, you can open a new ticket."
                    ),
                    discord.Color.red()
                )

                dm_embed.set_footer(text=f"{guild.name} Support")

                await owner.send(embed=dm_embed)

            except discord.Forbidden:
                pass

        await send_log(
            guild,
            make_embed(
                "Ticket Closed",
                f"Channel: {channel.mention}\n"
                f"Closed by: {interaction.user.mention}\n"
                f"Reason: {self.reason.value}",
                discord.Color.red()
            )
        )

        try:
            await channel.edit(
                name=f"closed-{channel.name}"[:100]
            )
        except discord.HTTPException:
            logger.exception("Could not rename closed ticket")

        if owner:
            try:
                await channel.set_permissions(
                    owner,
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True
                )
            except discord.HTTPException:
                logger.exception("Could not update ticket owner permissions")

        await channel.send(
            embed=make_embed(
                "🔒 Ticket Closed",
                f"Closed by: {interaction.user.mention}\n"
                f"Reason: {self.reason.value}\n\n"
                "Ticket staff can delete this ticket using the button below.",
                discord.Color.red()
            ),
            view=TicketDeleteView()
        )


# ============================================================
# TICKET CLOSE BUTTON
# ============================================================

class TicketCloseView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close"
    )
    async def close_ticket(self, interaction, button):
        await interaction.response.send_modal(
            TicketCloseModal()
        )


# ============================================================
# TICKET DELETE CONFIRMATION
# ============================================================

def can_manage_ticket(interaction):

    guild = interaction.guild

    if guild is None or not isinstance(interaction.user, discord.Member):
        return False

    member = interaction.user
    staff_role = get_ticket_staff_role(guild)

    return (
        member.guild_permissions.administrator
        or member.id == guild.owner_id
        or is_superuser(member.id)
        or (
            staff_role is not None
            and staff_role in member.roles
        )
    )


class TicketDeleteConfirmView(discord.ui.View):

    def __init__(self, channel_id, guild_id):
        super().__init__(timeout=60)

        self.channel_id = channel_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction):

        if not can_manage_ticket(interaction):
            await interaction.response.send_message(
                "You do not have permission to delete this ticket.",
                ephemeral=True
            )
            return False

        if (
            interaction.guild_id != self.guild_id
            or interaction.channel_id != self.channel_id
        ):
            await interaction.response.send_message(
                "This confirmation belongs to a different ticket.",
                ephemeral=True
            )
            return False

        return True

    @discord.ui.button(
        label="Confirm Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):

        guild = interaction.guild
        channel = guild.get_channel(self.channel_id)

        if channel is None:
            return await interaction.response.send_message(
                "This ticket channel no longer exists.",
                ephemeral=True
            )

        ticket = tickets_data.get(
            str(guild.id), {}
        ).get(str(channel.id))

        if not ticket or not ticket.get("closed", False):
            return await interaction.response.send_message(
                "This is not a registered closed ticket.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "🗑️ Confirmed. Deleting the ticket channel..."
        )

        await send_log(
            guild,
            make_embed(
                "Ticket Deleted",
                f"**Ticket:** `{channel.name}`\n"
                f"**Deleted by:** {interaction.user.mention}\n"
                f"**Ticket creator ID:** `{ticket.get('owner_id', 'Unknown')}`",
                discord.Color.red()
            )
        )

        tickets_data.get(str(guild.id), {}).pop(
            str(channel.id), None
        )
        save_tickets()

        try:
            await channel.delete(
                reason=f"Ticket deletion confirmed by {interaction.user}"
            )
        except discord.Forbidden:
            logger.exception("Missing permission to delete ticket channel")
        except discord.HTTPException:
            logger.exception("Could not delete ticket channel")

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(self, interaction, button):

        await interaction.response.edit_message(
            content="Ticket deletion cancelled. The channel has been kept.",
            embed=None,
            view=None
        )


class TicketDeleteView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Delete Ticket",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:delete"
    )
    async def delete_ticket(self, interaction, button):

        guild = interaction.guild
        channel = interaction.channel

        if guild is None or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "This button can only be used in a ticket channel.",
                ephemeral=True
            )

        ticket = tickets_data.get(
            str(guild.id), {}
        ).get(str(channel.id))

        if not ticket:
            return await interaction.response.send_message(
                "This channel is not registered as a ticket.",
                ephemeral=True
            )

        if not ticket.get("closed", False):
            return await interaction.response.send_message(
                "You must close the ticket before deleting it.",
                ephemeral=True
            )

        if not can_manage_ticket(interaction):
            return await interaction.response.send_message(
                "Only ticket staff can delete this ticket.",
                ephemeral=True
            )

        embed = make_embed(
            "⚠️ Confirm Ticket Deletion",
            (
                "Are you sure you want to permanently delete this ticket?\n\n"
                "**This action cannot be undone.**\n"
                "The channel and its message history will be deleted."
            ),
            discord.Color.red()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TicketDeleteConfirmView(
                channel.id,
                guild.id
            ),
            ephemeral=True
        )


# ============================================================
# REACTION ROLE BUTTONS - PERSISTENT AFTER RESTARTS
# ============================================================

class ReactionRoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"reactionrole:(?P<role_id>[0-9]+)"
):

    def __init__(self, role_id: int, label="Get Role", emoji=None):

        self.role_id = int(role_id)

        button = discord.ui.Button(
            label=label[:80],
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"reactionrole:{self.role_id}"
        )

        super().__init__(button)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):

        return cls(
            int(match["role_id"]),
            label=item.label or "Get Role",
            emoji=item.emoji
        )

    async def callback(self, interaction: discord.Interaction):

        guild = interaction.guild

        if guild is None:
            return await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True
            )

        role = guild.get_role(self.role_id)

        if role is None:
            return await interaction.response.send_message(
                "This role no longer exists. Please contact an administrator.",
                ephemeral=True
            )

        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Could not find your server membership.",
                ephemeral=True
            )

        member = interaction.user
        bot_member = guild.me

        if bot_member is None:
            return await interaction.response.send_message(
                "I could not find my server member information.",
                ephemeral=True
            )

        if not bot_member.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "I need the Manage Roles permission.",
                ephemeral=True
            )

        if role.is_default() or role.managed:
            return await interaction.response.send_message(
                "This role cannot be assigned using reaction roles.",
                ephemeral=True
            )

        if role >= bot_member.top_role:
            return await interaction.response.send_message(
                "I cannot manage that role. Move my bot role above it.",
                ephemeral=True
            )

        try:

            if role in member.roles:

                await member.remove_roles(
                    role,
                    reason="Reaction role button toggled off"
                )

                await interaction.response.send_message(
                    f"➖ You no longer have {role.mention}.",
                    ephemeral=True
                )

            else:

                await member.add_roles(
                    role,
                    reason="Reaction role button clicked"
                )

                await interaction.response.send_message(
                    f"✅ You received {role.mention}!",
                    ephemeral=True
                )

        except discord.Forbidden:

            await interaction.response.send_message(
                "I cannot manage that role. Check my role permissions and hierarchy.",
                ephemeral=True
            )

        except discord.HTTPException:

            await interaction.response.send_message(
                "Something went wrong while updating your role.",
                ephemeral=True
            )


class ReactionRoleView(discord.ui.View):

    def __init__(self, role_configs):

        super().__init__(timeout=None)

        for role_config in role_configs:

            self.add_item(
                ReactionRoleButton(
                    role_config["role"].id,
                    label=role_config["label"],
                    emoji=role_config["emoji"]
                )
            )


# ============================================================
# READY EVENT
# ============================================================

@bot.event
async def on_ready():

    global commands_synced

    logger.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id
    )

    if not getattr(bot, "persistent_views_registered", False):

        bot.add_view(TicketPanelView())
        bot.add_view(TicketDropdownView())
        bot.add_view(TicketCloseView())
        bot.add_view(TicketDeleteView())

        bot.add_dynamic_items(ReactionRoleButton)

        bot.persistent_views_registered = True

    if not commands_synced:

        sync_success = True

        for guild_id in GUILD_IDS:

            guild_object = discord.Object(id=guild_id)

            try:
                bot.tree.copy_global_to(guild=guild_object)
                await bot.tree.sync(guild=guild_object)

                logger.info("Synced commands to guild %s", guild_id)

            except discord.HTTPException:
                sync_success = False
                logger.exception("Could not sync commands to guild %s", guild_id)

        commands_synced = sync_success


# ============================================================
# MESSAGE EVENT
# ============================================================

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    if message.guild and "shit" in message.content.lower():

        try:
            await message.delete()

            await message.channel.send(
                f"{message.author.mention}, please keep the chat appropriate.",
                delete_after=5
            )

        except discord.Forbidden:
            pass

    await bot.process_commands(message)


# ============================================================
# ERROR HANDLING
# ============================================================

@bot.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure):
        return await respond(
            ctx,
            "You do not have permission to use this command."
        )

    if isinstance(error, commands.MissingRequiredArgument):
        return await respond(
            ctx,
            f"Missing argument: `{error.param.name}`."
        )

    if isinstance(error, commands.BadArgument):
        return await respond(
            ctx,
            "One or more arguments are invalid."
        )

    logger.error(
        "Command error in %s: %s",
        getattr(ctx.command, "qualified_name", "unknown"),
        error,
        exc_info=error
    )

    await respond(ctx, "An unexpected error occurred.")


@bot.tree.error
async def on_app_command_error(interaction, error):

    if isinstance(error, app_commands.CheckFailure):
        message = "You do not have permission to use this command."
    else:
        logger.error(
            "Application command error: %s",
            error,
            exc_info=error
        )
        message = "An unexpected error occurred."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True
            )
    except discord.HTTPException:
        pass


# ============================================================
# GENERAL COMMANDS
# ============================================================

@bot.hybrid_command(
    name="ping",
    description="Check the bot's latency."
)
async def ping(ctx):

    latency = round(bot.latency * 1000)

    await respond(
        ctx,
        embed=make_embed(
            "🏓 Pong!",
            f"Latency: `{latency}ms`"
        )
    )


@bot.hybrid_command(
    name="serverinfo",
    description="Show server information."
)
async def serverinfo(ctx):

    guild = ctx.guild

    if guild is None:
        return await respond(ctx, "Use this command in a server.")

    embed = make_embed(
        f"Server Information: {guild.name}",
        f"**Owner:** {guild.owner.mention if guild.owner else 'Unknown'}\n"
        f"**Members:** {guild.member_count}\n"
        f"**Channels:** {len(guild.channels)}\n"
        f"**Roles:** {len(guild.roles)}\n"
        f"**Created:** <t:{int(guild.created_at.timestamp())}:F>"
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    await respond(ctx, embed=embed)


# ============================================================
# CONFIGURATION COMMANDS
# ============================================================

@bot.hybrid_command(
    name="prefix",
    description="Change the server prefix."
)
@command_access("prefix")
async def prefix(ctx, new_prefix: str):

    if ctx.guild is None:
        return await respond(ctx, "Use this command in a server.")

    if len(new_prefix) > 5:
        return await respond(ctx, "The prefix cannot exceed 5 characters.")

    config["prefixes"][str(ctx.guild.id)] = new_prefix
    save_config()

    await respond(ctx, f"Prefix changed to `{new_prefix}`.")


@bot.hybrid_command(
    name="setlogchannel",
    description="Set the server's log channel."
)
@command_access("setlogchannel")
async def setlogchannel(ctx, channel: discord.TextChannel):

    config["log_channels"][str(ctx.guild.id)] = channel.id
    save_config()

    await respond(ctx, f"Log channel set to {channel.mention}.")


@bot.hybrid_command(
    name="adminrole",
    description="Set the administrator role."
)
@command_access("adminrole")
async def adminrole(ctx, role: discord.Role):

    config["admin_roles"][str(ctx.guild.id)] = role.id
    save_config()

    await respond(ctx, f"Administrator role set to {role.mention}.")


@bot.hybrid_command(
    name="member",
    description="Set the member role."
)
@command_access("member")
async def member(ctx, role: discord.Role):

    config["member_roles"][str(ctx.guild.id)] = role.id
    save_config()

    await respond(ctx, f"Member role set to {role.mention}.")


@bot.hybrid_command(
    name="superuser",
    description="Add or remove a server superuser."
)
@command_access("superuser")
@app_commands.choices(
    action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove")
    ]
)
async def superuser(ctx, action: str, user: discord.Member):

    guild_id = str(ctx.guild.id)

    config["superusers"].setdefault(guild_id, [])

    users = config["superusers"][guild_id]

    if action == "add":

        if user.id not in [int(value) for value in users]:
            users.append(user.id)

        message = f"{user.mention} was added as a superuser."

    else:

        users[:] = [
            value for value in users
            if int(value) != user.id
        ]

        message = f"{user.mention} was removed as a superuser."

    save_config()

    await respond(ctx, message)


# ============================================================
# PERMISSION COMMAND
# ============================================================

@bot.hybrid_command(
    name="permission",
    description="Allow or deny a user access to a command."
)
@command_access("permission")
@app_commands.choices(
    action=[
        app_commands.Choice(name="Allow", value="allow"),
        app_commands.Choice(name="Deny", value="deny")
    ]
)
@app_commands.autocomplete(command=permission_autocomplete)
async def permission(ctx, action: str, command: str, user: discord.Member):

    if command not in PERMISSION_COMMANDS:
        return await respond(ctx, "That command is not configurable.")

    guild_id = str(ctx.guild.id)

    config["permissions"].setdefault(guild_id, {})
    config["permissions"][guild_id].setdefault(command, [])

    users = config["permissions"][guild_id][command]

    if action == "allow":

        if user.id not in [int(value) for value in users]:
            users.append(user.id)

        message = f"{user.mention} can now use `{command}`."

    else:

        users[:] = [
            value for value in users
            if int(value) != user.id
        ]

        message = f"{user.mention} can no longer use `{command}`."

    save_config()

    await respond(ctx, message)


# ============================================================
# ROLE COMMANDS
# ============================================================

@bot.hybrid_command(
    name="promote",
    description="Give a member the member role."
)
@command_access("promote")
async def promote(ctx, member: discord.Member):

    role = get_role(
        ctx.guild,
        config.get("member_roles", {}).get(str(ctx.guild.id))
    )

    if role is None:
        return await respond(ctx, "No member role has been configured.")

    try:
        await member.add_roles(
            role,
            reason=f"Promoted by {ctx.author}"
        )

        await respond(ctx, f"{member.mention} has been promoted.")

    except discord.Forbidden:
        await respond(ctx, "I cannot manage that role.")


@bot.hybrid_command(
    name="demote",
    description="Remove the member role."
)
@command_access("demote")
async def demote(ctx, member: discord.Member):

    role = get_role(
        ctx.guild,
        config.get("member_roles", {}).get(str(ctx.guild.id))
    )

    if role is None:
        return await respond(ctx, "No member role has been configured.")

    try:
        await member.remove_roles(
            role,
            reason=f"Demoted by {ctx.author}"
        )

        await respond(ctx, f"{member.mention} has been demoted.")

    except discord.Forbidden:
        await respond(ctx, "I cannot manage that role.")


# ============================================================
# MODERATION COMMANDS
# ============================================================

@bot.hybrid_command(
    name="clear",
    description="Delete messages."
)
@command_access("clear")
async def clear(ctx, amount: int):

    if amount < 1 or amount > 100:
        return await respond(ctx, "Choose an amount between 1 and 100.")

    if not isinstance(ctx.channel, discord.TextChannel):
        return

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)

        message = await ctx.send(
            f"Deleted {max(0, len(deleted) - 1)} messages."
        )

        await message.delete(delay=5)

    except discord.Forbidden:
        await respond(ctx, "I do not have permission to delete messages.")


@bot.hybrid_command(
    name="kick",
    description="Kick a member."
)
@command_access("kick")
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):

    if member.id == ctx.author.id:
        return await respond(ctx, "You cannot kick yourself.")

    try:
        await member.kick(reason=reason)

        await respond(
            ctx,
            f"{member} has been kicked. Reason: {reason}"
        )

        await send_log(
            ctx.guild,
            make_embed(
                "Member Kicked",
                f"Member: {member.mention}\n"
                f"Moderator: {ctx.author.mention}\n"
                f"Reason: {reason}",
                discord.Color.orange()
            )
        )

    except discord.Forbidden:
        await respond(ctx, "I cannot kick that member.")


@bot.hybrid_command(
    name="ban",
    description="Ban a member."
)
@command_access("ban")
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):

    if member.id == ctx.author.id:
        return await respond(ctx, "You cannot ban yourself.")

    try:
        await member.ban(reason=reason)

        await respond(
            ctx,
            f"{member} has been banned. Reason: {reason}"
        )

        await send_log(
            ctx.guild,
            make_embed(
                "Member Banned",
                f"Member: {member.mention}\n"
                f"Moderator: {ctx.author.mention}\n"
                f"Reason: {reason}",
                discord.Color.red()
            )
        )

    except discord.Forbidden:
        await respond(ctx, "I cannot ban that member.")


@bot.hybrid_command(
    name="unban",
    description="Unban a user by ID."
)
@command_access("unban")
async def unban(ctx, user_id: str, *, reason="No reason provided"):

    try:
        user = await bot.fetch_user(int(user_id))

        await ctx.guild.unban(user, reason=reason)

        await respond(ctx, f"{user} has been unbanned.")

        await send_log(
            ctx.guild,
            make_embed(
                "Member Unbanned",
                f"User: {user} (`{user.id}`)\n"
                f"Moderator: {ctx.author.mention}\n"
                f"Reason: {reason}",
                discord.Color.green()
            )
        )

    except ValueError:
        await respond(ctx, "Please provide a valid user ID.")

    except discord.NotFound:
        await respond(ctx, "That user is not banned.")

    except discord.Forbidden:
        await respond(ctx, "I cannot unban that user.")


@bot.hybrid_command(
    name="timeout",
    description="Timeout a member."
)
@command_access("timeout")
async def timeout(
    ctx,
    member: discord.Member,
    minutes: int,
    *,
    reason="No reason provided"
):

    if minutes < 1 or minutes > 40320:
        return await respond(
            ctx,
            "Timeout must be between 1 and 40320 minutes."
        )

    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)

        await member.timeout(until, reason=reason)

        await respond(
            ctx,
            f"{member.mention} has been timed out for {minutes} minutes."
        )

        await send_log(
            ctx.guild,
            make_embed(
                "Member Timed Out",
                f"Member: {member.mention}\n"
                f"Moderator: {ctx.author.mention}\n"
                f"Duration: {minutes} minutes\n"
                f"Reason: {reason}",
                discord.Color.orange()
            )
        )

    except discord.Forbidden:
        await respond(ctx, "I cannot timeout that member.")


@bot.hybrid_command(
    name="untimeout",
    description="Remove a member's timeout."
)
@command_access("untimeout")
async def untimeout(ctx, member: discord.Member):

    try:
        await member.timeout(
            None,
            reason=f"Timeout removed by {ctx.author}"
        )

        await respond(
            ctx,
            f"{member.mention}'s timeout has been removed."
        )

    except discord.Forbidden:
        await respond(ctx, "I cannot remove that member's timeout.")


@bot.hybrid_command(
    name="warn",
    description="Warn a member."
)
@command_access("warn")
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):

    guild_id = str(ctx.guild.id)
    user_id = str(member.id)

    warnings_data.setdefault(guild_id, {})
    warnings_data[guild_id].setdefault(user_id, [])

    warnings_data[guild_id][user_id].append({
        "reason": reason,
        "moderator": ctx.author.id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    save_warnings()

    count = len(warnings_data[guild_id][user_id])

    await respond(
        ctx,
        f"{member.mention} has been warned. They now have {count} warning(s)."
    )

    try:
        await member.send(
            f"You have been warned in **{ctx.guild.name}**.\n"
            f"Reason: {reason}"
        )
    except discord.Forbidden:
        pass

    await send_log(
        ctx.guild,
        make_embed(
            "Member Warned",
            f"Member: {member.mention}\n"
            f"Moderator: {ctx.author.mention}\n"
            f"Reason: {reason}",
            discord.Color.orange()
        )
    )


@bot.hybrid_command(
    name="warnings",
    description="View a member's warnings."
)
@command_access("warnings")
async def warnings(ctx, member: discord.Member):

    user_warnings = warnings_data.get(
        str(ctx.guild.id), {}
    ).get(str(member.id), [])

    if not user_warnings:
        return await respond(ctx, f"{member.mention} has no warnings.")

    embed = make_embed(
        f"Warnings for {member}",
        color=discord.Color.orange()
    )

    for index, warning in enumerate(user_warnings, start=1):
        embed.add_field(
            name=f"Warning {index}",
            value=(
                f"**Reason:** {warning.get('reason', 'Unknown')}\n"
                f"**Moderator ID:** {warning.get('moderator', 'Unknown')}\n"
                f"**Date:** {warning.get('timestamp', 'Unknown')}"
            ),
            inline=False
        )

    await respond(ctx, embed=embed)


@bot.hybrid_command(
    name="unwarn",
    description="Remove a warning."
)
@command_access("unwarn")
async def unwarn(ctx, member: discord.Member, warning_number: int):

    guild_id = str(ctx.guild.id)
    user_id = str(member.id)

    user_warnings = warnings_data.get(
        guild_id, {}
    ).get(user_id, [])

    if warning_number < 1 or warning_number > len(user_warnings):
        return await respond(ctx, "That warning number does not exist.")

    removed = user_warnings.pop(warning_number - 1)

    save_warnings()

    await respond(
        ctx,
        f"Removed warning {warning_number} from {member.mention}."
    )

    await send_log(
        ctx.guild,
        make_embed(
            "Warning Removed",
            f"Member: {member.mention}\n"
            f"Moderator: {ctx.author.mention}\n"
            f"Removed reason: {removed.get('reason', 'Unknown')}",
            discord.Color.green()
        )
    )


# ============================================================
# CHANNEL MANAGEMENT
# ============================================================

@bot.hybrid_command(
    name="lock",
    description="Lock the current channel."
)
@command_access("lock")
async def lock(ctx):

    if not isinstance(ctx.channel, discord.TextChannel):
        return await respond(ctx, "This command requires a text channel.")

    try:
        await ctx.channel.set_permissions(
            ctx.guild.default_role,
            send_messages=False
        )

        await respond(ctx, "🔒 This channel has been locked.")

    except discord.Forbidden:
        await respond(ctx, "I cannot lock this channel.")


@bot.hybrid_command(
    name="unlock",
    description="Unlock the current channel."
)
@command_access("unlock")
async def unlock(ctx):

    if not isinstance(ctx.channel, discord.TextChannel):
        return await respond(ctx, "This command requires a text channel.")

    try:
        await ctx.channel.set_permissions(
            ctx.guild.default_role,
            send_messages=None
        )

        await respond(ctx, "🔓 This channel has been unlocked.")

    except discord.Forbidden:
        await respond(ctx, "I cannot unlock this channel.")


@bot.hybrid_command(
    name="slowmode",
    description="Set channel slowmode."
)
@command_access("slowmode")
async def slowmode(ctx, seconds: int):

    if seconds < 0 or seconds > 21600:
        return await respond(
            ctx,
            "Slowmode must be between 0 and 21600 seconds."
        )

    try:
        await ctx.channel.edit(slowmode_delay=seconds)

        await respond(ctx, f"Slowmode set to {seconds} second(s).")

    except discord.Forbidden:
        await respond(ctx, "I cannot change this channel's slowmode.")


# ============================================================
# TICKET CONFIGURATION COMMAND
# ============================================================

@bot.hybrid_command(
    name="ticket",
    description="Configure and send the ticket panel."
)
@command_access("ticket")
@app_commands.choices(
    style=[
        app_commands.Choice(name="Buttons", value="buttons"),
        app_commands.Choice(name="Dropdown", value="dropdown")
    ]
)
@app_commands.describe(
    style="Choose buttons or a dropdown menu.",
    staffrole="The role that handles tickets.",
    channel="The channel where the ticket panel will be posted.",
    logchannel="The channel where ticket logs will be sent.",
    categorychannel="The category where tickets will be created."
)
async def ticket(
    ctx,
    style: str,
    staffrole: discord.Role,
    channel: discord.TextChannel,
    logchannel: discord.TextChannel,
    categorychannel: discord.CategoryChannel
):

    if ctx.guild is None:
        return await respond(ctx, "This command must be used in a server.")

    if style not in ("buttons", "dropdown"):
        return await respond(ctx, "Choose buttons or dropdown.")

    if staffrole.is_default():
        return await respond(ctx, "You cannot use @everyone as staff role.")

    guild_id = str(ctx.guild.id)

    config["ticket_settings"][guild_id] = {
        "style": style,
        "staff_role_id": staffrole.id,
        "panel_channel_id": channel.id,
        "log_channel_id": logchannel.id,
        "category_id": categorychannel.id
    }

    config["log_channels"][guild_id] = logchannel.id

    save_config()

    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Choose the type of ticket you want to create below.\n\n"
            "Please provide as much information as possible so our staff "
            "team can help you."
        ),
        color=discord.Color.blurple()
    )

    view = (
        TicketPanelView()
        if style == "buttons"
        else TicketDropdownView()
    )

    try:
        await channel.send(embed=embed, view=view)

    except discord.Forbidden:
        return await respond(
            ctx,
            "Settings were saved, but I cannot send the panel there."
        )

    except discord.HTTPException:
        logger.exception("Could not send ticket panel")

        return await respond(
            ctx,
            "Settings were saved, but Discord returned an error sending the panel."
        )

    await respond(
        ctx,
        embed=make_embed(
            "Ticket System Configured",
            f"**Style:** {style.title()}\n"
            f"**Staff role:** {staffrole.mention}\n"
            f"**Panel channel:** {channel.mention}\n"
            f"**Log channel:** {logchannel.mention}\n"
            f"**Ticket category:** {categorychannel.name}\n\n"
            f"The ticket panel has been sent to {channel.mention}.",
            discord.Color.green()
        )
    )


# ============================================================
# TICKET PING COMMAND
# ============================================================

@bot.hybrid_command(
    name="ticketping",
    description="Add or remove roles pinged for a ticket type."
)
@command_access("ticketping")
@app_commands.choices(
    ticket_type=TICKET_TYPE_CHOICES,
    action=[
        app_commands.Choice(name="Add role", value="add"),
        app_commands.Choice(name="Remove role", value="remove")
    ]
)
@app_commands.describe(
    ticket_type="The ticket type for this ping role.",
    action="Whether to add or remove the role.",
    role="The role to add or remove."
)
async def ticketping(ctx, ticket_type: str, action: str, role: discord.Role):

    if ctx.guild is None:
        return await respond(ctx, "Use this command in a server.")

    if ticket_type not in TICKET_TYPES:
        return await respond(ctx, "Invalid ticket type.")

    if action not in ("add", "remove"):
        return await respond(ctx, "Choose add or remove.")

    if role.is_default():
        return await respond(ctx, "You cannot configure @everyone.")

    guild_id = str(ctx.guild.id)

    config["ticket_pings"].setdefault(guild_id, {})
    config["ticket_pings"][guild_id].setdefault(ticket_type, [])

    role_ids = config["ticket_pings"][guild_id][ticket_type]

    if not isinstance(role_ids, list):
        role_ids = [role_ids]
        config["ticket_pings"][guild_id][ticket_type] = role_ids

    if action == "add":

        if role.id in [int(value) for value in role_ids]:
            return await respond(ctx, f"{role.mention} is already configured.")

        role_ids.append(role.id)

        message = (
            f"Added {role.mention} to the ping roles for "
            f"**{TICKET_TYPES[ticket_type]['label']}** tickets."
        )

    else:

        if role.id not in [int(value) for value in role_ids]:
            return await respond(ctx, f"{role.mention} is not configured.")

        role_ids[:] = [
            value for value in role_ids
            if int(value) != role.id
        ]

        message = (
            f"Removed {role.mention} from the ping roles for "
            f"**{TICKET_TYPES[ticket_type]['label']}** tickets."
        )

    save_config()

    await respond(ctx, message)


# ============================================================
# REACTION ROLE COMMAND - UP TO 7 BUTTONS
# ============================================================

@bot.hybrid_command(
    name="reactionrole",
    description="Create a reaction role panel with multiple buttons."
)
@command_access("reactionrole")
@app_commands.describe(
    headertext="The title of the reaction role panel.",
    description="The description shown in the panel.",
    role1="Role for button 1.",
    emoji1="Emoji for button 1.",
    buttontext1="Button text for button 1 (maximum 80 characters).",
    role2="Role for button 2 (optional).",
    emoji2="Emoji for button 2.",
    buttontext2="Button text for button 2.",
    role3="Role for button 3 (optional).",
    emoji3="Emoji for button 3.",
    buttontext3="Button text for button 3.",
    role4="Role for button 4 (optional).",
    emoji4="Emoji for button 4.",
    buttontext4="Button text for button 4.",
    role5="Role for button 5 (optional).",
    emoji5="Emoji for button 5.",
    buttontext5="Button text for button 5.",
    role6="Role for button 6 (optional).",
    emoji6="Emoji for button 6.",
    buttontext6="Button text for button 6.",
    role7="Role for button 7 (optional).",
    emoji7="Emoji for button 7.",
    buttontext7="Button text for button 7.",
    channel="Channel where the panel will be sent. Optional."
)
async def reactionrole(
    ctx,
    headertext: str,
    description: str,

    role1: discord.Role,
    emoji1: str,
    buttontext1: str,

    role2: Optional[discord.Role] = None,
    emoji2: Optional[str] = None,
    buttontext2: Optional[str] = None,

    role3: Optional[discord.Role] = None,
    emoji3: Optional[str] = None,
    buttontext3: Optional[str] = None,

    role4: Optional[discord.Role] = None,
    emoji4: Optional[str] = None,
    buttontext4: Optional[str] = None,

    role5: Optional[discord.Role] = None,
    emoji5: Optional[str] = None,
    buttontext5: Optional[str] = None,

    role6: Optional[discord.Role] = None,
    emoji6: Optional[str] = None,
    buttontext6: Optional[str] = None,

    role7: Optional[discord.Role] = None,
    emoji7: Optional[str] = None,
    buttontext7: Optional[str] = None,

    channel: Optional[discord.TextChannel] = None
):

    if ctx.guild is None:
        return await respond(ctx, "Use this command inside a server.")

    if not headertext.strip():
        return await respond(ctx, "Please provide a panel title.")

    if not description.strip():
        return await respond(ctx, "Please provide a panel description.")

    if len(headertext) > 256:
        return await respond(ctx, "The header cannot exceed 256 characters.")

    if len(description) > 4000:
        return await respond(ctx, "The description cannot exceed 4000 characters.")

    bot_member = ctx.guild.me

    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        return await respond(ctx, "I need the Manage Roles permission.")

    raw_buttons = [
        (role1, emoji1, buttontext1),
        (role2, emoji2, buttontext2),
        (role3, emoji3, buttontext3),
        (role4, emoji4, buttontext4),
        (role5, emoji5, buttontext5),
        (role6, emoji6, buttontext6),
        (role7, emoji7, buttontext7)
    ]

    role_configs = []
    used_role_ids = set()

    for index, (role, emoji, button_text) in enumerate(raw_buttons, start=1):

        # Ignore a completely empty optional button.
        if role is None and emoji is None and button_text is None:
            continue

        # Do not allow partially configured buttons.
        if role is None or not emoji or not button_text:
            return await respond(
                ctx,
                f"Button {index} is incomplete. Provide its role, emoji, and button text."
            )

        if role.is_default() or role.managed:
            return await respond(
                ctx,
                f"{role.mention} cannot be used as a reaction role."
            )

        if role >= bot_member.top_role:
            return await respond(
                ctx,
                f"I cannot manage {role.mention}. Move my bot role above it."
            )

        if role.id in used_role_ids:
            return await respond(
                ctx,
                f"You selected {role.mention} more than once. Each button needs a different role."
            )

        used_role_ids.add(role.id)

        if not emoji.strip():
            return await respond(
                ctx,
                f"Button {index} needs an emoji."
            )

        if not button_text.strip():
            return await respond(
                ctx,
                f"Button {index} needs button text."
            )

        # Discord's button label maximum is 80 characters.
        button_text = button_text.strip()[:80]

        try:
            # Support custom Discord emoji markup as well as Unicode emoji.
            parsed_emoji = (
                discord.PartialEmoji.from_str(emoji)
                if emoji.startswith("<")
                else emoji
            )

        except (TypeError, ValueError):
            return await respond(
                ctx,
                f"The emoji for button {index} is invalid."
            )

        role_configs.append({
            "role": role,
            "emoji": parsed_emoji,
            "label": button_text
        })

    if not role_configs:
        return await respond(ctx, "You need to configure at least one button.")

    target_channel = channel or ctx.channel

    if not isinstance(target_channel, discord.TextChannel):
        return await respond(ctx, "Please choose a valid text channel.")

    embed = discord.Embed(
        title=headertext,
        description=description,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    role_list = []

    for index, role_config in enumerate(role_configs, start=1):

        role = role_config["role"]

        role_list.append(
            f"{role_config['emoji']} **{role_config['label']}** — {role.mention}"
        )

    embed.add_field(
        name="Available Roles",
        value="\n".join(role_list),
        inline=False
    )

    embed.set_footer(
        text="Click a button to receive its role. Click it again to remove the role."
    )

    view = ReactionRoleView(role_configs)

    try:
        await target_channel.send(
            embed=embed,
            view=view
        )

    except discord.Forbidden:
        return await respond(
            ctx,
            "I cannot send messages in that channel."
        )

    except discord.HTTPException:
        logger.exception("Could not create reaction role panel")

        return await respond(
            ctx,
            "Discord returned an error while creating the panel."
        )

    await respond(
        ctx,
        f"✅ Reaction role panel with **{len(role_configs)} button(s)** created in {target_channel.mention}!"
    )


# ============================================================
# HELP COMMAND
# ============================================================

@bot.hybrid_command(
    name="help",
    description="Show bot commands."
)
async def help_command(ctx):

    embed = make_embed(
        "Stasis SMP Bot Help",
        "Here are the available commands."
    )

    embed.add_field(
        name="General",
        value=(
            "`ping` - Check bot latency\n"
            "`serverinfo` - Show server information\n"
            "`help` - Show this help message"
        ),
        inline=False
    )

    embed.add_field(
        name="Configuration",
        value=(
            "`prefix` - Change server prefix\n"
            "`setlogchannel` - Set log channel\n"
            "`adminrole` - Set administrator role\n"
            "`member` - Set member role\n"
            "`superuser` - Manage server superusers\n"
            "`permission` - Configure command permissions"
        ),
        inline=False
    )

    embed.add_field(
        name="Tickets",
        value=(
            "`ticket` - Configure ticket panel\n"
            "`ticketping` - Configure ticket notification roles"
        ),
        inline=False
    )

    embed.add_field(
        name="Reaction Roles",
        value=(
            "`reactionrole` - Create a panel with up to 7 role buttons"
        ),
        inline=False
    )

    embed.add_field(
        name="Roles",
        value=(
            "`promote` - Give member role\n"
            "`demote` - Remove member role"
        ),
        inline=False
    )

    embed.add_field(
        name="Moderation",
        value=(
            "`clear` - Delete messages\n"
            "`kick` - Kick a member\n"
            "`ban` - Ban a member\n"
            "`unban` - Unban a user\n"
            "`timeout` - Timeout a member\n"
            "`untimeout` - Remove a timeout\n"
            "`warn` - Warn a member\n"
            "`warnings` - View warnings\n"
            "`unwarn` - Remove a warning"
        ),
        inline=False
    )

    embed.add_field(
        name="Channel Management",
        value=(
            "`lock` - Lock a channel\n"
            "`unlock` - Unlock a channel\n"
            "`slowmode` - Set slowmode"
        ),
        inline=False
    )

    await respond(ctx, embed=embed)


# ============================================================
# RENDER HTTP SERVER
# ============================================================

async def handle_http(reader, writer):
    """Respond to HTTP requests so Render detects an open port."""

    try:
        await reader.read(4096)

        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Length: 17\r\n"
            "Connection: close\r\n"
            "\r\n"
            "STASIS Bot Online"
        )

        writer.write(response.encode("utf-8"))
        await writer.drain()

    except (ConnectionError, asyncio.CancelledError):
        pass

    finally:
        writer.close()

        with suppress(Exception):
            await writer.wait_closed()


async def start_http_server():
    """Start the HTTP server on Render's assigned port."""

    port = int(os.getenv("PORT", "10000"))

    server = await asyncio.start_server(
        handle_http,
        host="0.0.0.0",
        port=port
    )

    logger.info("HTTP server listening on port %s", port)

    return server


# ============================================================
# START BOT
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Add it to your Render environment variables."
    )


async def main():

    # Start HTTP server before starting Discord.
    http_server = await start_http_server()

    try:
        async with bot:

            # Load giveaway extension
            await bot.load_extension(
                "giveaway_reactionrole_extension"
            )

            # Load Stasis welcome and farewell extension
            await bot.load_extension(
                "stasis_join_leave"
            )

            logger.info("All extensions loaded successfully.")

            # Start Discord bot
            await bot.start(TOKEN)

    finally:
        http_server.close()
        await http_server.wait_closed()

        logger.info("HTTP server stopped.")


if __name__ == "__main__":
    asyncio.run(main())