import asyncio
import logging
import re
import secrets

import discord
from discord import app_commands
from discord.ext import commands

import embeds
import emojiutils
import qrph
import templating
from storage import Store

log = logging.getLogger(__name__)

FIELD_LIMIT = 200
NOTES_LIMIT = 500
FORMAT_LIMIT = 2000
LABEL_LIMIT = 80
MAX_METHODS = 5
UPLOAD_TIMEOUT = 60

DEFAULT_CONFIRM_FORMAT = (
    "**order confirmation**\n"
    "\n"
    "order : {item}\n"
    "amount : {price}\n"
    "quantity : {quantity}\n"
    "details/notes : {notes}"
)
DEFAULT_FOOTER = "kindly make sure all information is complete and correct before proceeding."
DEFAULT_CONFIRM_BUTTON = "yes, proceed"
DEFAULT_RECEIVED = (
    "**confirmation received**\n"
    "\n"
    "kindly choose your payment option and remain patient for the owner to respond."
)
DEFAULT_GCASH_BUTTON = "gcash"
DEFAULT_GCASH_TEXT = (
    "**gcash info**\n"
    "\n"
    "no. 09xx xxx xxxx\n"
    "initials : x.x\n"
    "amount to send : {amount}\n"
    "\n"
    "> make sure you've read the tos\n"
    "> always send a receipt\n"
    "> no receipt = no transaction"
)

DEFAULT_PING = "one last check, :c_heart: {user}"

SAMPLE_ORDER = {
    "item": "pinned post",
    "price": "₱250.00",
    "quantity": "1",
    "notes": "rushed",
}

FIELDS = ("item", "price", "quantity", "notes", "user", "amount")

ALIASES = {
    "item": "item", "order": "item", "product": "item",
    "price": "price", "cost": "price", "total": "price",
    "quantity": "quantity", "qty": "quantity",
    "notes": "notes", "note": "notes", "details": "notes", "extra": "notes",
    "details/notes": "notes",
    "user": "user", "customer": "user", "buyer": "user",
    "amount": "amount", "sending": "amount", "sent": "amount", "paid": "amount",
}

IMAGE_URL = re.compile(
    r"(?<![(\[<])\bhttps?://[^\s<>()\[\]]+?"
    r"\.(?:png|jpe?g|gif|webp|avif)"
    r"(?:\?[^\s<>()\[\]]*)?",
    re.IGNORECASE,
)

CUSTOM_EMOJI = re.compile(r"^(<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>)\s*(.*)$", re.DOTALL)
SHORTCODE = re.compile(r"^:([A-Za-z0-9_~]{2,32}):\s*(.*)$", re.DOTALL)

_store = Store("confirmation_config.json")
config = _store.load()


def save_config():
    _store.save(config)


def new_method(label=None, text=None):
    return {
        "id": secrets.token_hex(4),
        "button": label or DEFAULT_GCASH_BUTTON,
        "text": text or DEFAULT_GCASH_TEXT,
        "qr": "",
    }


def defaults():
    return {
        "ping": DEFAULT_PING,
        "confirm_format": DEFAULT_CONFIRM_FORMAT,
        "footer": DEFAULT_FOOTER,
        "confirm_button": DEFAULT_CONFIRM_BUTTON,
        "received_format": DEFAULT_RECEIVED,
        "methods": [new_method()],
    }


def migrate(settings):
    if not settings.get("methods"):
        settings["methods"] = [new_method(
            settings.pop("gcash_button", None),
            settings.pop("gcash_text", None),
        )]
    settings.pop("gcash_button", None)
    settings.pop("gcash_text", None)
    for method in settings["methods"]:
        method.setdefault("id", secrets.token_hex(4))
        method.setdefault("button", DEFAULT_GCASH_BUTTON)
        method.setdefault("text", DEFAULT_GCASH_TEXT)
        method.setdefault("qr", "")
    return settings


def ensure_config(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = defaults()
    settings = config[key]
    for field, value in defaults().items():
        settings.setdefault(field, value)
    return migrate(settings)


def settings_for(guild_id):
    return migrate(config.get(str(guild_id)) or defaults())


def find_method(settings, method_id):
    for method in settings.get("methods", []):
        if method["id"] == method_id:
            return method
    return None


def order_values(order, author_id, amount=""):
    return {
        "item": order.get("item", ""),
        "price": order.get("price", ""),
        "quantity": order.get("quantity", ""),
        "notes": order.get("notes") or "none",
        "user": f"<@{author_id}>" if author_id else "",
        "amount": amount or "",
    }


def render(template, order, author_id, guild, amount=""):
    return templating.render(template, order_values(order, author_id, amount), ALIASES, guild)


def clean_amount(raw):
    text = (raw or "").strip().replace(",", "").replace("₱", "").replace("PHP", "")
    text = text.lstrip("Pp").strip()
    if not qrph.AMOUNT.match(text):
        raise qrph.QRError("enter a plain number like 30 or 30.00")
    if float(text) <= 0:
        raise qrph.QRError("amount has to be more than zero.")
    return text


def split_image(body):
    matches = list(IMAGE_URL.finditer(body))
    if not matches:
        return body, None
    last = matches[-1]
    trimmed = body[: last.start()] + body[last.end():]
    return trimmed.strip(), last.group(0)


def split_button_label(raw, guild, fallback):
    text = (raw or "").strip()
    if not text:
        return None, fallback[:LABEL_LIMIT]

    match = CUSTOM_EMOJI.match(text)
    if match:
        try:
            emoji = discord.PartialEmoji.from_str(match.group(1))
        except (ValueError, TypeError):
            emoji = None
        return emoji, (match.group(2).strip() or fallback)[:LABEL_LIMIT]

    match = SHORTCODE.match(text)
    if match:
        found = emojiutils.find_named(guild, match.group(1)) if guild else None
        return (found if found else None), (match.group(2).strip() or fallback)[:LABEL_LIMIT]

    head, _, rest = text.partition(" ")
    if emojiutils.valid_shape(head) and not head.startswith(("<", ":")):
        return head, (rest.strip() or fallback)[:LABEL_LIMIT]

    return None, text[:LABEL_LIMIT]


def apply_label(button, raw, guild, fallback):
    emoji, label = split_button_label(raw, guild, fallback)
    button.label = label
    if emoji is not None:
        button.emoji = emoji


class ConfirmRow(discord.ui.ActionRow):
    def __init__(self, parent, raw, guild):
        super().__init__()
        self.owner = parent
        apply_label(self.go, raw, guild, "confirm order")

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def go(self, interaction, button):
        await self.owner.confirm(interaction)


class ConfirmView(discord.ui.LayoutView):
    def __init__(self, settings, order, author_id, guild):
        super().__init__(timeout=600)
        self.settings = settings
        self.order = order
        self.author_id = author_id
        self.guild = guild
        self.build()

    def build(self):
        self.clear_items()
        ping = render(self.settings.get("ping") or "", self.order, self.author_id, self.guild).strip()
        if ping:
            self.add_item(discord.ui.TextDisplay(ping[:2000]))

        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay(
            render(self.settings["confirm_format"], self.order, self.author_id, self.guild)[:4000]
        ))
        footer = (self.settings.get("footer") or "").strip()
        if footer:
            box.add_item(discord.ui.TextDisplay(footer[:1000]))
        box.add_item(discord.ui.Separator())
        box.add_item(ConfirmRow(self, self.settings.get("confirm_button"), self.guild))
        self.add_item(box)

    async def confirm(self, interaction):
        view = PaymentView(self.settings, self.order, self.author_id, interaction.guild)
        await interaction.response.edit_message(view=view)


class MethodRow(discord.ui.ActionRow):
    def __init__(self, parent, methods, guild):
        super().__init__()
        self.owner = parent
        for method in methods[:MAX_METHODS]:
            button = discord.ui.Button(style=discord.ButtonStyle.secondary)
            apply_label(button, method.get("button"), guild, "pay")
            button.callback = self.handler(method)
            self.add_item(button)

    def handler(self, method):
        async def callback(interaction):
            await interaction.response.send_modal(AmountModal(self.owner, method))
        return callback


class PaymentView(discord.ui.LayoutView):
    def __init__(self, settings, order, author_id, guild):
        super().__init__(timeout=600)
        self.settings = settings
        self.order = order
        self.author_id = author_id
        self.guild = guild
        self.build()

    def build(self):
        self.clear_items()
        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay(
            render(self.settings["received_format"], self.order, self.author_id, self.guild)[:4000]
        ))
        methods = self.settings.get("methods") or []
        if methods:
            box.add_item(discord.ui.Separator())
            box.add_item(MethodRow(self, methods, self.guild))
        self.add_item(box)


class AmountModal(discord.ui.Modal):
    def __init__(self, parent, method):
        super().__init__(title="how much are you sending?")
        self.parent = parent
        self.method = method
        self.amount = discord.ui.TextInput(
            label="amount",
            placeholder="ex. 100, 100.01. no peso sign.",
            max_length=13,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            amount = clean_amount(self.amount.value)
        except qrph.QRError as error:
            await interaction.response.send_message(embed=embeds.error(str(error)), ephemeral=True)
            return

        payload = (self.method.get("qr") or "").strip()
        image = None
        if payload:
            try:
                image = qrph.render(qrph.set_amount(payload, amount))
            except qrph.QRError as error:
                log.warning("qr build failed for method %s: %s", self.method.get("id"), error)
                image = None

        view = MethodBox(
            self.method,
            self.parent.order,
            self.parent.author_id,
            interaction.guild,
            amount,
            attached=image is not None,
        )
        kwargs = {"view": view, "allowed_mentions": discord.AllowedMentions.none()}
        if image is not None:
            kwargs["file"] = discord.File(image, filename="payment_qr.png")
        await interaction.response.send_message(**kwargs)


class MethodBox(discord.ui.LayoutView):
    def __init__(self, method, order, author_id, guild, amount, attached=False):
        super().__init__(timeout=None)
        box = discord.ui.Container()
        body = render(method.get("text") or "", order, author_id, guild, amount)
        body, image_url = split_image(body)
        if body.strip():
            box.add_item(discord.ui.TextDisplay(body[:4000]))
        if attached:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media="attachment://payment_qr.png")
            box.add_item(gallery)
        elif image_url:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=image_url)
            box.add_item(gallery)
        self.add_item(box)


class FieldModal(discord.ui.Modal):
    def __init__(self, panel, target, field, label, multiline=False, required=True, limit=FORMAT_LIMIT):
        super().__init__(title=label[:45])
        self.panel = panel
        self.target = target
        self.field = field
        self.required = required
        self.input = discord.ui.TextInput(
            label=label[:45],
            style=discord.TextStyle.paragraph if multiline else discord.TextStyle.short,
            default=(target.get(field) or "")[:limit],
            max_length=limit,
            required=required,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        value = self.input.value.strip()
        if not value and self.required:
            await interaction.response.send_message(
                embed=embeds.error("that cannot be empty."), ephemeral=True
            )
            return
        self.target[self.field] = value
        save_config()
        await self.panel.refresh(interaction)


class MethodPanel(discord.ui.View):
    def __init__(self, parent, method):
        super().__init__(timeout=300)
        self.parent = parent
        self.ctx = parent.ctx
        self.method = method

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("this panel isn't yours."), ephemeral=True
        )
        return False

    def status_embed(self):
        qr = "uploaded" if (self.method.get("qr") or "").strip() else "none, text only"
        lines = [
            "**payment method**",
            "",
            f"**button** : {self.method['button']}",
            f"**qr** : {qr}",
            "",
            "**text preview**",
            (self.method.get("text") or "")[:800],
            "",
            "tip : put `{amount}` in the text to show what the buyer typed. "
            "leave the qr empty and only this text gets sent.",
        ]
        return embeds.build("\n".join(lines)[:4096])

    async def refresh(self, interaction=None):
        embed = self.status_embed()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        elif interaction is not None:
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="button label", style=discord.ButtonStyle.secondary, row=0)
    async def edit_label(self, interaction, button):
        await interaction.response.send_modal(
            FieldModal(self, self.method, "button", "button label", limit=LABEL_LIMIT)
        )

    @discord.ui.button(label="text", style=discord.ButtonStyle.secondary, row=0)
    async def edit_text(self, interaction, button):
        await interaction.response.send_modal(
            FieldModal(self, self.method, "text", "payment text", multiline=True)
        )

    @discord.ui.button(label="upload qr", style=discord.ButtonStyle.secondary, row=1)
    async def upload_qr(self, interaction, button):
        await interaction.response.send_message(
            embed=embeds.build(
                f"send the qr image in this channel within {UPLOAD_TIMEOUT} seconds.\n"
                "use the plain gcash receive qr with no amount on it."
            ),
            ephemeral=True,
        )

        def check(message):
            return (
                message.author.id == self.ctx.author.id
                and message.channel.id == self.ctx.channel.id
                and message.attachments
            )

        try:
            message = await self.ctx.bot.wait_for("message", check=check, timeout=UPLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(embed=embeds.error("timed out, nothing saved."), ephemeral=True)
            return

        try:
            data = await message.attachments[0].read()
            payload = qrph.read_image(data)
            qrph.validate(payload)
        except qrph.QRError as error:
            await interaction.followup.send(embed=embeds.error(str(error)), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send(embed=embeds.error("could not download that file."), ephemeral=True)
            return

        self.method["qr"] = payload
        save_config()
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await interaction.followup.send(embed=embeds.build("qr saved."), ephemeral=True)
        await self.refresh(interaction)

    @discord.ui.button(label="clear qr", style=discord.ButtonStyle.secondary, row=1)
    async def clear_qr(self, interaction, button):
        self.method["qr"] = ""
        save_config()
        await self.refresh(interaction)

    @discord.ui.button(label="delete method", style=discord.ButtonStyle.danger, row=2)
    async def delete_method(self, interaction, button):
        methods = self.parent.settings["methods"]
        if len(methods) <= 1:
            await interaction.response.send_message(
                embed=embeds.error("you need at least one payment method."), ephemeral=True
            )
            return
        self.parent.settings["methods"] = [m for m in methods if m["id"] != self.method["id"]]
        save_config()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embeds.build("method deleted."), view=self)
        await self.parent.refresh()


class MethodSelect(discord.ui.Select):
    def __init__(self, panel):
        methods = panel.settings.get("methods") or []
        options = [
            discord.SelectOption(label=(m.get("button") or "method")[:100], value=m["id"])
            for m in methods
        ] or [discord.SelectOption(label="none yet", value="none")]
        super().__init__(placeholder="edit a payment method", options=options, row=3)
        self.panel = panel

    async def callback(self, interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        method = find_method(self.panel.settings, self.values[0])
        if method is None:
            await interaction.response.send_message(
                embed=embeds.error("that method is gone."), ephemeral=True
            )
            return
        view = MethodPanel(self.panel, method)
        await interaction.response.send_message(
            embed=view.status_embed(), view=view, ephemeral=True
        )


class SetupView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.settings = settings
        self.message = None
        self.selector = None
        self.rebuild()

    def rebuild(self):
        if self.selector is not None:
            self.remove_item(self.selector)
        self.selector = MethodSelect(self)
        self.add_item(self.selector)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("this panel isn't yours."), ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def status_embed(self):
        settings = self.settings
        preview = render(settings["confirm_format"], SAMPLE_ORDER, self.ctx.author.id, self.ctx.guild)
        methods = settings.get("methods") or []
        listed = ", ".join(m.get("button") or "method" for m in methods) or "none"
        lines = [
            "**confirmation setup**",
            "",
            f"**confirm button** : {settings['confirm_button']}",
            f"**payment methods** ({len(methods)}/{MAX_METHODS}) : {listed}",
            "",
            "**confirm box preview**",
            preview[:800],
            "",
            "tip : start any button with an emoji like `:heart: confirm`. pick a method "
            "below to change its text or upload a qr.",
        ]
        return embeds.build("\n".join(lines)[:4096])

    async def refresh(self, interaction=None):
        self.rebuild()
        embed = self.status_embed()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
            return
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def edit(self, interaction, field, label, multiline=False, required=True, limit=FORMAT_LIMIT):
        await interaction.response.send_modal(
            FieldModal(self, self.settings, field, label, multiline=multiline, required=required, limit=limit)
        )

    @discord.ui.button(label="ping", style=discord.ButtonStyle.secondary, row=0)
    async def ping(self, interaction, button):
        await self.edit(interaction, "ping", "ping line (use {user})", required=False, limit=200)

    @discord.ui.button(label="confirm format", style=discord.ButtonStyle.secondary, row=0)
    async def confirm_format(self, interaction, button):
        await self.edit(interaction, "confirm_format", "confirm format", multiline=True)

    @discord.ui.button(label="footer", style=discord.ButtonStyle.secondary, row=0)
    async def footer(self, interaction, button):
        await self.edit(interaction, "footer", "footer line", multiline=True, required=False, limit=1000)

    @discord.ui.button(label="confirm button", style=discord.ButtonStyle.secondary, row=1)
    async def confirm_button(self, interaction, button):
        await self.edit(interaction, "confirm_button", "confirm button label", limit=LABEL_LIMIT)

    @discord.ui.button(label="received format", style=discord.ButtonStyle.secondary, row=1)
    async def received_format(self, interaction, button):
        await self.edit(interaction, "received_format", "received format", multiline=True)

    @discord.ui.button(label="add method", style=discord.ButtonStyle.success, row=2)
    async def add_method(self, interaction, button):
        methods = self.settings.setdefault("methods", [])
        if len(methods) >= MAX_METHODS:
            await interaction.response.send_message(
                embed=embeds.error(f"you can only have {MAX_METHODS} payment methods."), ephemeral=True
            )
            return
        methods.append(new_method(f"method {len(methods) + 1}"))
        save_config()
        await self.refresh(interaction)


class Confirmation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await embeds.send(ctx, embeds.error("this command only works in a server."))
            return
        if isinstance(error, commands.MissingPermissions):
            await embeds.send(
                ctx,
                embeds.error("you need manage server permission for that.", title="Not allowed"),
            )
            return
        log.exception("Unhandled error in %s", ctx.command, exc_info=error)
        await embeds.send(ctx, embeds.error("something broke on my end. it has been logged."))

    @commands.hybrid_group(
        name="confirmation",
        aliases=["confirm"],
        invoke_without_command=True,
        fallback="new",
        description="Fill in and confirm your order.",
    )
    @app_commands.describe(
        item="what you're ordering",
        price="how much it costs",
        quantity="how many",
        notes="any extra notes (optional)",
    )
    @commands.guild_only()
    async def confirmation(self, ctx, item: str, price: str, quantity: str, *, notes: str = None):
        settings = settings_for(ctx.guild.id)
        order = {
            "item": item.strip(),
            "price": price.strip(),
            "quantity": quantity.strip(),
            "notes": (notes or "").strip(),
        }
        view = ConfirmView(settings, order, ctx.author.id, ctx.guild)
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )

    @confirmation.command(name="setup", description="Set up the confirmation formats.")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def setup_confirmation(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()
        view = SetupView(ctx, settings)
        view.message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(Confirmation(bot))