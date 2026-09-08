import logging

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from storage import Store

log = logging.getLogger(__name__)

MAX_RULES = 10
KEYWORD_LIMIT = 100

_store = Store("rolestrip_config.json")
config = _store.load()


def save_config():
    _store.save(config)


def rules_for(guild_id):
    rules = config.get(str(guild_id))
    return rules if isinstance(rules, list) else []


def set_rules(guild_id, rules):
    config[str(guild_id)] = rules
    save_config()


def describe(guild, rule):
    channel = guild.get_channel(rule["channel"])
    role = guild.get_role(rule["role"])
    where = channel.mention if channel else f"deleted channel ({rule['channel']})"
    what = role.mention if role else f"deleted role ({rule['role']})"
    return f"in {where}, saying `{rule['keyword']}` removes {what}"


def blocked_reason(guild, role):
    me = guild.me
    if role.is_default():
        return "that is the everyone role."
    if role.managed:
        return "that role is managed by an integration, so i cannot remove it."
    if not me.guild_permissions.manage_roles:
        return "i need the manage roles permission."
    if role >= me.top_role:
        return "that role sits above mine, so i cannot remove it. move my role higher."
    return None


class RoleStrip(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await embeds.send(ctx, embeds.error("this command only works in a server."))
            return
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error("you need manage roles permission for that.", title="Not allowed"),
            )
            return
        if isinstance(error, commands.BadArgument):
            await embeds.send(ctx, embeds.error(str(error)))
            return
        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(ctx, embeds.error("something broke on my end. it has been logged."))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        rules = rules_for(message.guild.id)
        if not rules:
            return

        content = message.content.lower()
        if not content:
            return

        channel_ids = {message.channel.id}
        parent = getattr(message.channel, "parent_id", None)
        if parent is not None:
            channel_ids.add(parent)

        member = message.author
        removed = []
        for rule in rules:
            if rule["channel"] not in channel_ids:
                continue
            if rule["keyword"] not in content:
                continue
            role = message.guild.get_role(rule["role"])
            if role is None or role not in member.roles:
                continue
            if blocked_reason(message.guild, role) is not None:
                continue
            removed.append(role)

        if not removed:
            return

        try:
            await member.remove_roles(*removed, reason="rolestrip keyword match")
        except discord.Forbidden:
            log.warning("missing permission to strip roles in %s", message.guild.id)
        except discord.HTTPException as error:
            log.warning("failed to strip roles in %s: %s", message.guild.id, error)

    @commands.hybrid_group(
        name="rolestrip",
        aliases=["strip"],
        invoke_without_command=True,
        fallback="list",
        description="Show the keyword rules that remove roles.",
    )
    @commands.guild_only()
    async def rolestrip(self, ctx):
        rules = rules_for(ctx.guild.id)
        if not rules:
            await embeds.send(ctx, embeds.notice("no rules yet. add one with `rolestrip add`."))
            return
        lines = [f"`{i}` {describe(ctx.guild, rule)}" for i, rule in enumerate(rules, 1)]
        await embeds.send(ctx, embeds.build("\n".join(lines)[:4096], title="Role strip rules"))

    @rolestrip.command(name="add", description="Remove a role when someone says a keyword.")
    @app_commands.describe(
        channel="where to watch",
        role="the role to take away",
        keyword="the text to look for, not case sensitive",
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def add(self, ctx, channel: discord.TextChannel, role: discord.Role, *, keyword: str):
        keyword = keyword.strip().lower()
        if not keyword:
            await embeds.send(ctx, embeds.error("give me a keyword to look for."))
            return
        if len(keyword) > KEYWORD_LIMIT:
            await embeds.send(ctx, embeds.error(f"keep the keyword under {KEYWORD_LIMIT} characters."))
            return

        reason = blocked_reason(ctx.guild, role)
        if reason is not None:
            await embeds.send(ctx, embeds.error(reason))
            return

        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            await embeds.send(ctx, embeds.error("that role is not below yours."))
            return

        rules = rules_for(ctx.guild.id)
        if len(rules) >= MAX_RULES:
            await embeds.send(ctx, embeds.error(f"you can only have {MAX_RULES} rules."))
            return
        for rule in rules:
            if rule["channel"] == channel.id and rule["keyword"] == keyword and rule["role"] == role.id:
                await embeds.send(ctx, embeds.error("that exact rule already exists."))
                return

        rules.append({"channel": channel.id, "role": role.id, "keyword": keyword})
        set_rules(ctx.guild.id, rules)
        await embeds.send(ctx, embeds.notice(f"added. {describe(ctx.guild, rules[-1])}"))

    @rolestrip.command(name="remove", description="Delete a rule by its number.")
    @app_commands.describe(number="the number shown in rolestrip list")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def remove(self, ctx, number: int):
        rules = rules_for(ctx.guild.id)
        if not 1 <= number <= len(rules):
            await embeds.send(ctx, embeds.error("no rule with that number."))
            return
        gone = rules.pop(number - 1)
        set_rules(ctx.guild.id, rules)
        await embeds.send(ctx, embeds.notice(f"removed. {describe(ctx.guild, gone)}"))

    @rolestrip.command(name="clear", description="Delete every rule in this server.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def clear(self, ctx):
        if not rules_for(ctx.guild.id):
            await embeds.send(ctx, embeds.error("there is nothing to clear."))
            return
        set_rules(ctx.guild.id, [])
        await embeds.send(ctx, embeds.notice("cleared every rule."))


async def setup(bot):
    await bot.add_cog(RoleStrip(bot))