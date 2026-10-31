"""
AI server-builder bot.

@mention the bot and describe what you want. It proposes a plan (roles,
categories, channels, embeds, dropdown menus with images), you press
"Build it", and it does the work. Admin-only.
"""
import asyncio
import io
import json
import os
import re
import uuid

import discord
from aiohttp import web
from discord import app_commands
from openai import AsyncOpenAI

TOKEN = os.environ["DISCORD_TOKEN"]
# Any OpenAI-compatible provider works. Defaults to Google Gemini's free tier.
MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
ai = AsyncOpenAI(base_url=os.environ.get("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
                 api_key=os.environ["LLM_API_KEY"])
DATA_CHANNEL = "ai-bot-data"  # private channel where dropdown configs are stored (free persistence)

HIST: dict = {}  # channel_id -> [{"role","content"}]
ATT: dict = {}   # channel_id -> {filename: bytes} (images the admin uploaded)

SYSTEM = """You are a Discord server architect inside a Discord bot. An admin chats with you and you build their server.

How you work:
- Chat briefly and casually. Ask at most one short clarifying question, and only if truly needed. Otherwise propose a plan right away.
- To build, call tools. The admin sees your calls as a plan and presses a button to approve, so just call them.
- Order matters: create roles first, then categories, then channels, then embeds and menus.
- Use the exact same names every time you refer to a role, category or channel.
- Make it look good: tasteful emoji in names and titles, consistent hex colors, short punchy descriptions.
- Use create_role_menu for self-assign roles (colors, pings, interests, pronouns) and create_info_menu for FAQ / rules / info dropdowns where picking an option shows a private embed.
- Images: use an https link the admin gave you, or 'attachment:<filename>' when they uploaded one (the filename is shown in their message). Never invent image links.
- Keep each plan under 40 tool calls. If the server is bigger, do it in phases and say so.
- Be careful with deletes and 'admin' permission presets; only use them when asked.
"""

STR = {"type": "string"}
BOOL = {"type": "boolean"}
NAMES = {"type": "array", "items": STR}
COLOR = {"type": "string", "description": "Hex color like #5865F2"}
IMG = {"type": "string", "description": "https link, or 'attachment:<filename>' for an image the admin uploaded"}
EMBED = {"title": STR, "description": STR, "color": COLOR, "image": IMG, "thumbnail": IMG, "footer": STR}


def obj(props, req=()):
    o = {"type": "object", "properties": props}
    if req:
        o["required"] = list(req)
    return o


def tool(name, desc, props, req=()):
    return {"name": name, "description": desc, "input_schema": obj(props, req)}


OPT_ROLE = obj({"label": STR, "role": {"type": "string", "description": "Role name (created if missing)"},
                "emoji": {"type": "string", "description": "One unicode emoji"}, "description": STR}, ["label", "role"])
OPT_INFO = obj({"label": STR, "emoji": STR, "description": {"type": "string", "description": "Short text under the label"},
                "response_title": STR, "response_text": STR,
                "response_image": {"type": "string", "description": "https link only"}}, ["label", "response_text"])

TOOLS = [
    tool("create_role", "Create a role.", {
        "name": STR, "color": COLOR, "hoist": BOOL, "mentionable": BOOL,
        "permissions": {"type": "string", "enum": ["none", "mod", "admin"]}}, ["name"]),
    tool("create_category", "Create a category. visible_to makes it private to those role names.", {
        "name": STR, "visible_to": NAMES, "read_only": BOOL}, ["name"]),
    tool("create_channel", "Create a channel (category is created if missing). visible_to makes it private to those roles; read_only stops @everyone from sending.", {
        "name": STR, "kind": {"type": "string", "enum": ["text", "voice", "announcement", "forum"]},
        "category": STR, "topic": STR, "visible_to": NAMES, "read_only": BOOL}, ["name"]),
    tool("send_embed", "Post a styled embed (banner image, thumbnail, fields) in a text channel.", {
        "channel": STR, **EMBED,
        "fields": {"type": "array", "items": obj({"name": STR, "value": STR, "inline": BOOL}, ["name", "value"])}}, ["channel"]),
    tool("create_role_menu", "Post an embed with a dropdown that gives/removes roles. Persistent.", {
        "channel": STR, **EMBED, "placeholder": STR,
        "max_choices": {"type": "integer", "description": "1 = pick one, higher = multi-select"},
        "options": {"type": "array", "items": OPT_ROLE}}, ["channel", "options"]),
    tool("create_info_menu", "Post an embed with a dropdown; picking an option shows a private embed (FAQ, rules, info). Persistent.", {
        "channel": STR, **EMBED, "placeholder": STR,
        "options": {"type": "array", "items": OPT_INFO}}, ["channel", "options"]),
    tool("delete_channel", "Delete a channel by name.", {"name": STR}, ["name"]),
    tool("delete_role", "Delete a role by name.", {"name": STR}, ["name"]),
]

TOOLS_OAI = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in TOOLS]

DESC = {
    "create_role": lambda a: f"🎭 Role **{a.get('name')}**" + (" ⚠️ administrator" if a.get("permissions") == "admin" else ""),
    "create_category": lambda a: f"📁 Category **{a.get('name')}**" + (" (private)" if a.get("visible_to") else ""),
    "create_channel": lambda a: f"{'🔊' if a.get('kind') == 'voice' else '💬'} {a.get('kind', 'text')} channel **{a.get('name')}**"
                                + (f" in {a['category']}" if a.get("category") else ""),
    "send_embed": lambda a: f"🖼️ Embed in **#{a.get('channel')}**: {a.get('title', '')}",
    "create_role_menu": lambda a: f"🎛️ Role dropdown in **#{a.get('channel')}** ({len(a.get('options', []))} options)",
    "create_info_menu": lambda a: f"📖 Info dropdown in **#{a.get('channel')}** ({len(a.get('options', []))} options)",
    "delete_channel": lambda a: f"🗑️ Delete channel **{a.get('name')}** ⚠️",
    "delete_role": lambda a: f"🗑️ Delete role **{a.get('name')}** ⚠️",
}


def describe(n, a):
    try:
        return DESC[n](a)
    except Exception:
        return n


# ---------- helpers ----------
def norm(s):
    return s.lstrip("#").lower().replace(" ", "-")


def clip(s, n):
    return s[:n] if s else None


def parse_color(c):
    try:
        return discord.Color(int((c or "#5865F2").lstrip("#"), 16))
    except ValueError:
        return discord.Color.blurple()


def clean_emoji(e):
    if not e:
        return None
    e = e.strip()
    return e if len(e) <= 8 and not re.search(r"[A-Za-z0-9]", e) else None


def find_role(g, name):
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), g.roles)


def find_cat(g, name):
    return discord.utils.find(lambda c: c.name.lower() == name.lower(), g.categories)


def find_chan(g, name, cat=None):
    n = norm(name)
    for c in g.channels:
        if isinstance(c, discord.CategoryChannel):
            continue
        if norm(c.name) == n and (cat is None or c.category == cat):
            return c


def text_target(g, name):
    c = find_chan(g, name)
    if not isinstance(c, discord.TextChannel):
        raise ValueError(f"text channel '{name}' not found")
    return c


def make_overwrites(g, visible_to, read_only):
    ow, base = {}, {}
    if visible_to:
        base["view_channel"] = False
    if read_only:
        base["send_messages"] = False
    if base:
        ow[g.default_role] = discord.PermissionOverwrite(**base)
    for rn in visible_to or []:
        r = find_role(g, rn)
        if not r:
            raise ValueError(f"role '{rn}' not found (create it first)")
        ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return ow


def resolve_image(spec, files, store):
    if not spec:
        return None
    if spec.startswith("attachment:"):
        name = spec[len("attachment:"):]
        if name not in store:
            raise ValueError(f"no uploaded image named '{name}'")
        if not any(f.filename == name for f in files):
            files.append(discord.File(io.BytesIO(store[name]), filename=name))
        return f"attachment://{name}"
    if spec.startswith(("http://", "https://")):
        return spec
    raise ValueError(f"bad image '{spec}'")


def build_embed(a, files, store):
    e = discord.Embed(title=a.get("title"), description=a.get("description"), color=parse_color(a.get("color")))
    if img := resolve_image(a.get("image"), files, store):
        e.set_image(url=img)
    if th := resolve_image(a.get("thumbnail"), files, store):
        e.set_thumbnail(url=th)
    if a.get("footer"):
        e.set_footer(text=a["footer"])
    for f in a.get("fields") or []:
        e.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    return e


# ---------- dropdown menus (persistent) ----------
class MenuSelect(discord.ui.Select):
    def __init__(self, data):
        opts = [discord.SelectOption(label=o["label"][:100], value=o["value"],
                                     description=clip(o.get("description"), 100), emoji=o.get("emoji"))
                for o in data["options"]]
        super().__init__(custom_id=data["cid"], placeholder=clip(data.get("placeholder"), 150) or "Choose...",
                         min_values=data["min"], max_values=data["max"], options=opts)
        self.data = data

    async def callback(self, i: discord.Interaction):
        if self.data["kind"] == "roles":
            menu_roles = {int(o["value"]) for o in self.data["options"]}
            chosen = {int(v) for v in self.values}
            have = {r.id for r in i.user.roles}
            add = [i.guild.get_role(r) for r in chosen - have]
            rem = [i.guild.get_role(r) for r in (menu_roles - chosen) & have]
            try:
                if any(add):
                    await i.user.add_roles(*[r for r in add if r], reason="Role menu")
                if any(rem):
                    await i.user.remove_roles(*[r for r in rem if r], reason="Role menu")
            except discord.Forbidden:
                return await i.response.send_message("I can't manage one of those roles (it's above my role).", ephemeral=True)
            names = ", ".join(r.mention for r in (i.guild.get_role(x) for x in chosen) if r) or "none"
            await i.response.send_message(f"✅ Your roles from this menu: {names}", ephemeral=True)
        else:
            r = self.data["responses"][int(self.values[0])]
            e = discord.Embed(title=r.get("title"), description=r.get("text"), color=parse_color(self.data.get("color")))
            if r.get("image"):
                e.set_image(url=r["image"])
            await i.response.send_message(embed=e, ephemeral=True)


def menu_view(data):
    v = discord.ui.View(timeout=None)
    v.add_item(MenuSelect(data))
    return v


async def data_channel(g, create=False):
    ch = discord.utils.get(g.text_channels, name=DATA_CHANNEL)
    if not ch and create:
        ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
              g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                read_message_history=True)}
        ch = await g.create_text_channel(DATA_CHANNEL, overwrites=ow, topic="Bot storage for dropdown menus. Do not delete.")
    return ch


async def save_menu(g, message_id, data):
    ch = await data_channel(g, create=True)
    await ch.send(content=str(message_id),
                  file=discord.File(io.BytesIO(json.dumps(data).encode()), filename="menu.json"))


async def load_menus(g):
    ch = await data_channel(g)
    if not ch:
        return
    async for m in ch.history(limit=None):
        if m.attachments and m.content.isdigit():
            try:
                bot.add_view(menu_view(json.loads(await m.attachments[0].read())), message_id=int(m.content))
            except Exception as e:
                print("menu load failed:", e)


async def post_menu(ch, a, data, store):
    files = []
    embed = build_embed(a, files, store)
    msg = await ch.send(embed=embed, view=menu_view(data), files=files)
    await save_menu(ch.guild, msg.id, data)
    return f"posted dropdown in {ch.mention}"


# ---------- tool handlers ----------
async def h_create_role(g, a, st):
    if find_role(g, a["name"]):
        return f"role {a['name']} already exists, skipped"
    perms = {"none": discord.Permissions.none(),
             "mod": discord.Permissions(kick_members=True, ban_members=True, manage_messages=True, moderate_members=True,
                                        mute_members=True, deafen_members=True, move_members=True, view_audit_log=True),
             "admin": discord.Permissions(administrator=True)}[a.get("permissions", "none")]
    r = await g.create_role(name=a["name"], colour=parse_color(a.get("color")), hoist=bool(a.get("hoist")),
                            mentionable=bool(a.get("mentionable")), permissions=perms, reason="AI builder")
    return f"created role {r.mention}"


async def h_create_category(g, a, st):
    if find_cat(g, a["name"]):
        return f"category {a['name']} already exists, skipped"
    ow = make_overwrites(g, a.get("visible_to"), a.get("read_only"))
    c = await g.create_category(a["name"], overwrites=ow, reason="AI builder")
    return f"created category {c.name}"


async def h_create_channel(g, a, st):
    cat = None
    if a.get("category"):
        cat = find_cat(g, a["category"]) or await g.create_category(a["category"], reason="AI builder")
    if find_chan(g, a["name"], cat):
        return f"channel {a['name']} already exists, skipped"
    kw = {"name": a["name"], "category": cat, "reason": "AI builder"}
    ow = make_overwrites(g, a.get("visible_to"), a.get("read_only"))
    if ow:
        kw["overwrites"] = ow
    kind = a.get("kind", "text")
    if kind == "voice":
        ch = await g.create_voice_channel(**kw)
    elif kind == "forum":
        ch = await g.create_forum(topic=a.get("topic"), **kw)
    elif kind == "announcement":
        ch = await g.create_text_channel(topic=a.get("topic"), news=True, **kw)
    else:
        ch = await g.create_text_channel(topic=a.get("topic"), **kw)
    return f"created {ch.mention}"


async def h_send_embed(g, a, st):
    ch = text_target(g, a["channel"])
    files = []
    await ch.send(embed=build_embed(a, files, st), files=files)
    return f"posted embed in {ch.mention}"


async def h_role_menu(g, a, st):
    ch = text_target(g, a["channel"])
    opts = a["options"]
    if not 1 <= len(opts) <= 25:
        raise ValueError("a dropdown needs 1-25 options")
    data_opts = []
    for o in opts:
        role = find_role(g, o["role"]) or await g.create_role(name=o["role"], reason="Role menu")
        data_opts.append({"label": o["label"], "value": str(role.id), "emoji": clean_emoji(o.get("emoji")),
                          "description": o.get("description")})
    mx = max(1, min(int(a.get("max_choices") or 1), len(data_opts)))
    data = {"kind": "roles", "cid": "menu:" + uuid.uuid4().hex[:12], "placeholder": a.get("placeholder"),
            "min": 0, "max": mx, "options": data_opts, "color": a.get("color")}
    return await post_menu(ch, a, data, st)


async def h_info_menu(g, a, st):
    ch = text_target(g, a["channel"])
    opts = a["options"]
    if not 1 <= len(opts) <= 25:
        raise ValueError("a dropdown needs 1-25 options")
    data_opts, responses = [], []
    for n, o in enumerate(opts):
        data_opts.append({"label": o["label"], "value": str(n), "emoji": clean_emoji(o.get("emoji")),
                          "description": o.get("description")})
        img = o.get("response_image")
        responses.append({"title": o.get("response_title") or o["label"], "text": o["response_text"],
                          "image": img if img and img.startswith("http") else None})
    data = {"kind": "info", "cid": "menu:" + uuid.uuid4().hex[:12], "placeholder": a.get("placeholder"),
            "min": 1, "max": 1, "options": data_opts, "responses": responses, "color": a.get("color")}
    return await post_menu(ch, a, data, st)


async def h_delete_channel(g, a, st):
    c = find_chan(g, a["name"])
    if not c:
        return f"channel {a['name']} not found, skipped"
    await c.delete(reason="AI builder")
    return f"deleted channel {a['name']}"


async def h_delete_role(g, a, st):
    r = find_role(g, a["name"])
    if not r:
        return f"role {a['name']} not found, skipped"
    await r.delete(reason="AI builder")
    return f"deleted role {a['name']}"


HANDLERS = {"create_role": h_create_role, "create_category": h_create_category, "create_channel": h_create_channel,
            "send_embed": h_send_embed, "create_role_menu": h_role_menu, "create_info_menu": h_info_menu,
            "delete_channel": h_delete_channel, "delete_role": h_delete_role}


# ---------- confirm UI ----------
class Confirm(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=600)
        self.author_id, self.ok = author_id, None

    async def interaction_check(self, i):
        if i.user.id != self.author_id:
            await i.response.send_message("This plan isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.success, emoji="✅")
    async def go(self, i, b):
        self.ok = True
        await i.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, i, b):
        self.ok = False
        await i.response.defer()
        self.stop()


def snapshot(g):
    lines = [f"Server: {g.name}"]
    for c in g.categories:
        lines.append(f"[{c.name}] " + ", ".join(ch.name for ch in c.channels))
    loose = [ch.name for ch in g.channels if ch.category is None and not isinstance(ch, discord.CategoryChannel)
             and ch.name != DATA_CHANNEL]
    if loose:
        lines.append("No category: " + ", ".join(loose))
    lines.append("Roles: " + ", ".join(r.name for r in g.roles if not r.is_default() and not r.managed))
    return "Current server layout:\n" + "\n".join(lines)[:3500]


def trimmed(h):
    h = h[-20:]
    while h and h[0]["role"] != "user":
        h.pop(0)
    return h


# ---------- bot ----------
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # enable "Message Content Intent" in the dev portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        port = os.environ.get("PORT")  # lets a free Render web service stay pingable
        if port:
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="ok"))
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", int(port)).start()


bot = Bot()


@bot.event
async def on_ready():
    if getattr(bot, "menus_loaded", False):
        return
    bot.menus_loaded = True
    for g in bot.guilds:
        await load_menus(g)
    print(f"Online as {bot.user}")


@bot.tree.command(description="Forget this channel's conversation with the AI")
@app_commands.default_permissions(administrator=True)
async def reset(i: discord.Interaction):
    HIST.pop(i.channel_id, None)
    await i.response.send_message("Fresh start.", ephemeral=True)


@bot.event
async def on_message(m: discord.Message):
    if m.author.bot or not m.guild or bot.user not in m.mentions:
        return
    if not m.author.guild_permissions.administrator:
        return await m.reply("Only server admins can talk to me about building stuff.")

    store = ATT.setdefault(m.channel.id, {})
    notes = []
    for a in m.attachments:
        if (a.content_type or "").startswith("image/") and a.size < 8_000_000:
            name = re.sub(r"[^\w.\-]", "_", a.filename)
            store[name] = await a.read()
            notes.append(f"[uploaded image: attachment:{name}]")
    text = re.sub(rf"<@!?{bot.user.id}>", "", m.content).strip()
    hist = HIST.setdefault(m.channel.id, [])
    hist.append({"role": "user", "content": (text + "\n" + "\n".join(notes)).strip() or "hi"})

    try:
        async with m.channel.typing():
            resp = await ai.chat.completions.create(
                model=MODEL, tools=TOOLS_OAI,
                messages=[{"role": "system", "content": SYSTEM + "\n\n" + snapshot(m.guild)}] + trimmed(hist))
    except Exception as e:
        hist.pop()
        if "429" in str(e):
            return await m.reply("The free AI tier is rate limited right now. Try again in a minute.")
        return await m.reply(f"AI error: {e}")

    msg = resp.choices[0].message
    say = (msg.content or "").strip()
    actions = []
    for tc in msg.tool_calls or []:
        try:
            if tc.function.name in HANDLERS:
                actions.append((tc.function.name, json.loads(tc.function.arguments or "{}")))
        except json.JSONDecodeError:
            pass

    if not actions:
        hist.append({"role": "assistant", "content": say or "..."})
        return await m.reply((say or "...")[:1900])

    plan = "\n".join(f"{n}. {describe(nm, a)}" for n, (nm, a) in enumerate(actions, 1))
    hist.append({"role": "assistant", "content": (say + f"\n[Proposed plan]\n{plan}").strip()})
    view = Confirm(m.author.id)
    plan_msg = await m.reply(say[:1900] or None, view=view,
                             embed=discord.Embed(title="Proposed plan", description=plan[:4000], color=discord.Color.blurple()))
    await view.wait()

    if not view.ok:
        hist.append({"role": "user", "content": "[system note: plan was cancelled or expired, nothing was built]"})
        return await plan_msg.edit(view=None, embed=discord.Embed(
            title="Cancelled" if view.ok is False else "Plan expired", color=discord.Color.light_grey()))

    await plan_msg.edit(view=None, embed=discord.Embed(title="Building...", description=plan[:4000], color=discord.Color.gold()))
    results = []
    for nm, a in actions:
        try:
            results.append("✅ " + await HANDLERS[nm](m.guild, a, store))
        except discord.Forbidden:
            results.append(f"❌ {describe(nm, a)}: missing permissions (give me Administrator, or Manage Channels + Manage Roles)")
        except Exception as e:
            results.append(f"❌ {describe(nm, a)}: {e}")
        await asyncio.sleep(0.4)
    out = "\n".join(results)
    hist.append({"role": "user", "content": f"[system note: plan executed]\n{out}"})
    await plan_msg.edit(embed=discord.Embed(title="Done", description=out[:4000], color=discord.Color.green()))


bot.run(TOKEN)
