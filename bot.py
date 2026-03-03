import os
import time
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

DB_PATH = "data.db"
DEFAULT_PREFIX = "dl!"

# Default rank-up channel (tu #discord-rangos)
DEFAULT_RANK_UP_CHANNEL_ID = 1477135861127839884

# XP settings
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_COOLDOWN_SECONDS = 45

# Ranks
LEVELS_PER_RANK = 10
MAX_RANK_INDEX = 10  # 11 ranks (0..10)
MAX_LEVEL_PER_PRESTIGE = (MAX_RANK_INDEX + 1) * LEVELS_PER_RANK  # 110

RANKS = [
    ("Initiate",  "ranks/01_initiate.png"),
    ("Seeker",    "ranks/02_seeker.png"),
    ("Alchemist", "ranks/03_alchemist.png"),
    ("Arcanist",  "ranks/04_arcanist.png"),
    ("Ritualist", "ranks/05_ritualist.png"),
    ("Emissary",  "ranks/06_emissary.png"),
    ("Archon",    "ranks/07_archon.png"),
    ("Oracle",    "ranks/08_oracle.png"),
    ("Phantom",   "ranks/09_phantom.png"),
    ("Ascendant", "ranks/10_ascendant.png"),
    ("Eternus",   "ranks/11_eternus.png"),
]

# Colores por rango (simple pero queda brutal)
RANK_COLORS = [
    0x5B5B5B,  # Initiate
    0x3B6EA5,  # Seeker
    0x2E8B57,  # Alchemist
    0x6A5ACD,  # Arcanist
    0x8B4513,  # Ritualist
    0xB8860B,  # Emissary
    0xC0C0C0,  # Archon
    0x00CED1,  # Oracle
    0xFF1493,  # Phantom
    0xFF8C00,  # Ascendant
    0xE6E6FA,  # Eternus
]

# =========================
# HELPERS
# =========================

def clamp(n: int, a: int, b: int) -> int:
    return max(a, min(b, n))

def xp_required_for_next_level(level: int) -> int:
    # curva suave (normal-lenta)
    return 100 + 35 * level + 5 * (level ** 2)

def rank_index_from_level(level: int) -> int:
    idx = (level - 1) // LEVELS_PER_RANK
    return clamp(idx, 0, MAX_RANK_INDEX)

def rank_name_from_level(level: int) -> str:
    return RANKS[rank_index_from_level(level)][0]

def rank_image_from_level(level: int) -> str:
    return RANKS[rank_index_from_level(level)][1]

def color_from_level(level: int) -> int:
    return RANK_COLORS[rank_index_from_level(level)]

@dataclass
class UserState:
    user_id: int
    xp: int
    level: int
    prestige: int
    last_xp_ts: int

# =========================
# DATABASE
# =========================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def _try_alter(conn: sqlite3.Connection, sql: str):
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass

def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                xp          INTEGER NOT NULL DEFAULT 0,
                level       INTEGER NOT NULL DEFAULT 1,
                prestige    INTEGER NOT NULL DEFAULT 0,
                last_xp_ts  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id        INTEGER PRIMARY KEY,
                prefix          TEXT NOT NULL DEFAULT 'dl!',
                rankup_channel_id INTEGER,
                announce_origin INTEGER NOT NULL DEFAULT 1,
                announce_rankchannel INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Migraciones si tu DB vieja no tenía columnas
        _try_alter(conn, "ALTER TABLE settings ADD COLUMN rankup_channel_id INTEGER")
        _try_alter(conn, "ALTER TABLE settings ADD COLUMN announce_origin INTEGER NOT NULL DEFAULT 1")
        _try_alter(conn, "ALTER TABLE settings ADD COLUMN announce_rankchannel INTEGER NOT NULL DEFAULT 1")

def get_or_create_user(user_id: int) -> UserState:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT user_id, xp, level, prestige, last_xp_ts FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if row:
            return UserState(*row)
        conn.execute(
            "INSERT INTO users(user_id, xp, level, prestige, last_xp_ts) VALUES (?,0,1,0,0)",
            (user_id,)
        )
        return UserState(user_id=user_id, xp=0, level=1, prestige=0, last_xp_ts=0)

def update_user(state: UserState):
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET xp=?, level=?, prestige=?, last_xp_ts=? WHERE user_id=?",
            (state.xp, state.level, state.prestige, state.last_xp_ts, state.user_id)
        )

def set_user_progress(user_id: int, prestige: int, level: int, xp: int = 0):
    st = get_or_create_user(user_id)
    st.prestige = max(0, int(prestige))
    st.level = clamp(int(level), 1, MAX_LEVEL_PER_PRESTIGE)
    st.xp = max(0, int(xp))
    st.last_xp_ts = 0
    update_user(st)

def add_user_xp(user_id: int, amount: int):
    st = get_or_create_user(user_id)
    st.xp += max(0, int(amount))

    leveled_up = False
    old_level = st.level
    old_prestige = st.prestige

    while True:
        need = xp_required_for_next_level(st.level)
        if st.xp >= need:
            st.xp -= need
            st.level += 1
            leveled_up = True
        else:
            break

    if st.level > MAX_LEVEL_PER_PRESTIGE:
        st.prestige += 1
        st.level = 1
        st.xp = 0

    update_user(st)
    return st, (st.level != old_level or st.prestige != old_prestige)

def top_users(limit: int = 10) -> List[Tuple[int, int, int, int]]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, prestige, level, xp FROM users "
            "ORDER BY prestige DESC, level DESC, xp DESC LIMIT ?",
            (limit,)
        ).fetchall()

def _get_setting_row(guild_id: int):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT prefix, rankup_channel_id, announce_origin, announce_rankchannel FROM settings WHERE guild_id=?",
            (guild_id,)
        ).fetchone()
        if row:
            return row
        conn.execute(
            "INSERT INTO settings(guild_id, prefix, rankup_channel_id, announce_origin, announce_rankchannel) VALUES (?,?,?,?,?)",
            (guild_id, DEFAULT_PREFIX, DEFAULT_RANK_UP_CHANNEL_ID, 1, 1)
        )
        return (DEFAULT_PREFIX, DEFAULT_RANK_UP_CHANNEL_ID, 1, 1)

def get_guild_prefix(guild_id: int) -> str:
    return _get_setting_row(guild_id)[0]

def set_guild_prefix(guild_id: int, prefix: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(guild_id, prefix, rankup_channel_id, announce_origin, announce_rankchannel) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix",
            (guild_id, prefix, DEFAULT_RANK_UP_CHANNEL_ID, 1, 1)
        )

def get_rankup_channel_id(guild_id: int) -> int:
    cid = _get_setting_row(guild_id)[1]
    return int(cid) if cid else DEFAULT_RANK_UP_CHANNEL_ID

def set_rankup_channel_id(guild_id: int, channel_id: int):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(guild_id, prefix, rankup_channel_id, announce_origin, announce_rankchannel) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET rankup_channel_id=excluded.rankup_channel_id",
            (guild_id, get_guild_prefix(guild_id), int(channel_id), 1, 1)
        )

def get_announce_flags(guild_id: int) -> Tuple[bool, bool]:
    _, _, ao, arc = _get_setting_row(guild_id)
    return (bool(ao), bool(arc))

def set_announce_origin(guild_id: int, enabled: bool):
    with db_connect() as conn:
        conn.execute(
            "UPDATE settings SET announce_origin=? WHERE guild_id=?",
            (1 if enabled else 0, guild_id)
        )

def set_announce_rankchannel(guild_id: int, enabled: bool):
    with db_connect() as conn:
        conn.execute(
            "UPDATE settings SET announce_rankchannel=? WHERE guild_id=?",
            (1 if enabled else 0, guild_id)
        )

# =========================
# DISCORD BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

async def dynamic_prefix(bot: commands.Bot, message: discord.Message):
    if message.guild:
        return get_guild_prefix(message.guild.id)
    return DEFAULT_PREFIX

bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

async def fetch_channel_safe(channel_id: int) -> Optional[discord.abc.Messageable]:
    ch = bot.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None

# =========================
# CORE XP LOGIC
# =========================

async def try_add_xp(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_CHARS_FOR_XP:
        return

    state = get_or_create_user(message.author.id)
    now = int(time.time())

    if now - state.last_xp_ts < XP_COOLDOWN_SECONDS:
        return

    gained = random.randint(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
    state.last_xp_ts = now
    state.xp += gained

    leveled_up = False
    old_level = state.level
    old_rank = rank_name_from_level(state.level)
    old_prestige = state.prestige

    while True:
        need = xp_required_for_next_level(state.level)
        if state.xp >= need:
            state.xp -= need
            state.level += 1
            leveled_up = True
        else:
            break

    prestiged = False
    if state.level > MAX_LEVEL_PER_PRESTIGE:
        state.prestige += 1
        state.level = 1
        state.xp = 0
        prestiged = True

    update_user(state)

    if leveled_up or prestiged:
        new_rank = rank_name_from_level(state.level)
        await announce_levelup(
            origin_channel=message.channel,
            guild=message.guild,
            member=message.author,
            old_level=old_level,
            new_level=state.level,
            old_rank=old_rank,
            new_rank=new_rank,
            prestige=state.prestige,
            prestiged=prestiged,
            old_prestige=old_prestige
        )

async def announce_levelup(
    origin_channel: discord.abc.Messageable,
    guild: discord.Guild,
    member: discord.abc.User,
    old_level: int,
    new_level: int,
    old_rank: str,
    new_rank: str,
    prestige: int,
    prestiged: bool,
    old_prestige: int
):
    # Flags
    announce_origin, announce_rankchannel = get_announce_flags(guild.id)
    rankup_channel_id = get_rankup_channel_id(guild.id)
    rankup_channel = await fetch_channel_safe(rankup_channel_id)

    # Mensajes especiales
    reached_eternus = (rank_index_from_level(new_level) == MAX_RANK_INDEX and new_level == MAX_LEVEL_PER_PRESTIGE)

    if prestiged:
        title = "🜂 PRESTIGE UNLOCKED"
        desc = (
            f"{member.mention} rompió el ciclo. **Prestige {old_prestige} → {prestige}**\n"
            "La rueda gira de nuevo…"
        )
    else:
        if new_rank != old_rank:
            title = "⚡ RANK ASCENSION"
            desc = f"{member.mention} ascendió: **{old_rank} → {new_rank}** (Lv {old_level} → {new_level})"
        else:
            title = "⚡ LEVEL UP"
            desc = f"{member.mention} subió a **Lv {new_level}** (**{new_rank}**)"

        if reached_eternus:
            desc += "\n\n**👑 ETERNUS ACHIEVED** — el chat ya no puede contenerte."

    embed = discord.Embed(title=title, description=desc, color=discord.Color(color_from_level(new_level)))
    embed.set_footer(text="Deadlock Chat Ranks • XP por actividad")

    img_path = rank_image_from_level(new_level)
    has_img = os.path.exists(img_path)
    file1: Optional[discord.File] = None
    if has_img:
        file1 = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")

    async def send_to(channel: discord.abc.Messageable):
        try:
            if has_img:
                f = discord.File(img_path, filename=os.path.basename(img_path))
                await channel.send(embed=embed, file=f)
            else:
                await channel.send(embed=embed)
        except Exception:
            pass

    # 1) canal donde subió
    if announce_origin:
        await send_to(origin_channel)

    # 2) canal #discord-rangos (si es distinto)
    if announce_rankchannel and rankup_channel and getattr(rankup_channel, "id", None) != getattr(origin_channel, "id", None):
        await send_to(rankup_channel)

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print(f"Bot listo como {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    await try_add_xp(message)
    await bot.process_commands(message)

# =========================
# TEXT COMMANDS
# =========================

@bot.command(name="commands")
async def commands_list(ctx: commands.Context):
    prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
    ao, arc = get_announce_flags(ctx.guild.id) if ctx.guild else (True, True)
    cid = get_rankup_channel_id(ctx.guild.id) if ctx.guild else DEFAULT_RANK_UP_CHANNEL_ID

    msg = (
        f"**Comandos ({prefix})**\n"
        f"- `{prefix}rank` → tu rango / nivel / xp\n"
        f"- `{prefix}top` → leaderboard\n"
        f"- `{prefix}setprefix <nuevo>` → cambia el prefijo (admin)\n"
        f"- `{prefix}givexp <cantidad> [@user]` → da XP (admin)\n"
        f"- `{prefix}maxme` → ponerte Lv 110 (admin)\n"
        f"- `{prefix}setrankchannel #canal` → set canal rank-ups (admin)\n"
        f"- `{prefix}toggleorigin` → on/off rank-ups en canal donde sube (admin)\n"
        f"- `{prefix}togglerankchannel` → on/off rank-ups en canal fijo (admin)\n\n"
        f"**Ajustes**\n"
        f"- Rank-up channel id: `{cid}`\n"
        f"- announce_origin: `{ao}` | announce_rankchannel: `{arc}`\n\n"
        "Slash: **/rank /leaderboard /givexp /maxme /setrankchannel /toggleorigin /togglerankchannel**"
    )
    await ctx.reply(msg, mention_author=False)

@bot.command(name="rank")
async def rank_text(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    st = get_or_create_user(member.id)
    rank = rank_name_from_level(st.level)
    need = xp_required_for_next_level(st.level)

    embed = discord.Embed(
        title=f"{member.display_name} • {rank}",
        description=f"Prestige **{st.prestige}**\nLv **{st.level}** • XP **{st.xp}/{need}**",
        color=discord.Color(color_from_level(st.level))
    )

    img_path = rank_image_from_level(st.level)
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")
        await ctx.reply(embed=embed, file=file, mention_author=False)
    else:
        await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="top")
async def top_text(ctx: commands.Context):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        user = ctx.guild.get_member(uid) if ctx.guild else None
        name = user.display_name if user else f"<@{uid}>"
        lines.append(f"**{i}.** {name} — P{p} • Lv{lvl} • {rank_name_from_level(lvl)}")
    await ctx.reply("🏆 **Leaderboard**\n" + "\n".join(lines), mention_author=False)

@bot.command(name="setprefix")
@commands.has_permissions(manage_guild=True)
async def setprefix_cmd(ctx: commands.Context, new_prefix: str):
    if len(new_prefix) > 8:
        await ctx.reply("Muy largo. Usa algo corto (ej: `dl!` `dl.` `d!`).", mention_author=False)
        return
    set_guild_prefix(ctx.guild.id, new_prefix)
    await ctx.reply(f"Listo. Nuevo prefijo: `{new_prefix}`", mention_author=False)

@bot.command(name="givexp")
@commands.has_permissions(manage_guild=True)
async def givexp_cmd(ctx: commands.Context, amount: int, member: Optional[discord.Member] = None):
    member = member or ctx.author

    before = get_or_create_user(member.id)
    old_level, old_rank, old_prestige = before.level, rank_name_from_level(before.level), before.prestige

    after, changed = add_user_xp(member.id, amount)

    await ctx.reply(
        f"✅ XP dado a **{member.display_name}**: +{amount}\n"
        f"Ahora: P{after.prestige} • Lv {after.level} • XP {after.xp}/{xp_required_for_next_level(after.level)}",
        mention_author=False
    )

    if changed:
        await announce_levelup(
            origin_channel=ctx.channel,
            guild=ctx.guild,
            member=member,
            old_level=old_level,
            new_level=after.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(after.level),
            prestige=after.prestige,
            prestiged=(after.prestige != old_prestige),
            old_prestige=old_prestige
        )

@bot.command(name="maxme")
@commands.has_permissions(manage_guild=True)
async def maxme_cmd(ctx: commands.Context):
    set_user_progress(ctx.author.id, prestige=0, level=MAX_LEVEL_PER_PRESTIGE, xp=0)
    await ctx.reply(
        f"✅ Listo: ahora eres **{rank_name_from_level(MAX_LEVEL_PER_PRESTIGE)}** (Lv {MAX_LEVEL_PER_PRESTIGE}).",
        mention_author=False
    )

@bot.command(name="setrankchannel")
@commands.has_permissions(manage_guild=True)
async def setrankchannel_cmd(ctx: commands.Context, channel: discord.TextChannel):
    set_rankup_channel_id(ctx.guild.id, channel.id)
    await ctx.reply(f"✅ Rank-ups ahora también van a: {channel.mention}", mention_author=False)

@bot.command(name="toggleorigin")
@commands.has_permissions(manage_guild=True)
async def toggleorigin_cmd(ctx: commands.Context):
    ao, arc = get_announce_flags(ctx.guild.id)
    set_announce_origin(ctx.guild.id, not ao)
    await ctx.reply(f"✅ announce_origin = `{not ao}`", mention_author=False)

@bot.command(name="togglerankchannel")
@commands.has_permissions(manage_guild=True)
async def togglerankchannel_cmd(ctx: commands.Context):
    ao, arc = get_announce_flags(ctx.guild.id)
    set_announce_rankchannel(ctx.guild.id, not arc)
    await ctx.reply(f"✅ announce_rankchannel = `{not arc}`", mention_author=False)

# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(name="rank", description="Muestra tu rango/nivel/xp")
async def rank_slash(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    st = get_or_create_user(user.id)
    rank = rank_name_from_level(st.level)
    need = xp_required_for_next_level(st.level)

    embed = discord.Embed(
        title=f"{user.display_name} • {rank}",
        description=f"Prestige **{st.prestige}**\nLv **{st.level}** • XP **{st.xp}/{need}**",
        color=discord.Color(color_from_level(st.level))
    )

    img_path = rank_image_from_level(st.level)
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")
        await interaction.response.send_message(embed=embed, file=file)
    else:
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Top 10")
async def leaderboard_slash(interaction: discord.Interaction):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        lines.append(f"**{i}.** <@{uid}> — P{p} • Lv{lvl} • {rank_name_from_level(lvl)}")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="givexp", description="Da XP (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def givexp_slash(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
    user = user or interaction.user

    before = get_or_create_user(user.id)
    old_level, old_rank, old_prestige = before.level, rank_name_from_level(before.level), before.prestige

    after, changed = add_user_xp(user.id, amount)
    await interaction.response.send_message(
        f"✅ XP dado a **{user.display_name}**: +{amount}\n"
        f"Ahora: P{after.prestige} • Lv {after.level} • XP {after.xp}/{xp_required_for_next_level(after.level)}",
        ephemeral=True
    )

    if changed and interaction.channel and interaction.guild:
        await announce_levelup(
            origin_channel=interaction.channel,
            guild=interaction.guild,
            member=user,
            old_level=old_level,
            new_level=after.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(after.level),
            prestige=after.prestige,
            prestiged=(after.prestige != old_prestige),
            old_prestige=old_prestige
        )

@bot.tree.command(name="maxme", description="Ponte rango máximo (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def maxme_slash(interaction: discord.Interaction):
    set_user_progress(interaction.user.id, prestige=0, level=MAX_LEVEL_PER_PRESTIGE, xp=0)
    await interaction.response.send_message(
        f"✅ Listo: ahora eres **{rank_name_from_level(MAX_LEVEL_PER_PRESTIGE)}** (Lv {MAX_LEVEL_PER_PRESTIGE}).",
        ephemeral=True
    )

@bot.tree.command(name="setrankchannel", description="Set canal de rank-ups (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setrankchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Solo en servidor.", ephemeral=True)
        return
    set_rankup_channel_id(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Rank-ups ahora también van a: {channel.mention}", ephemeral=True)

@bot.tree.command(name="toggleorigin", description="On/Off rank-ups en el canal donde sube (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def toggleorigin_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Solo en servidor.", ephemeral=True)
        return
    ao, arc = get_announce_flags(interaction.guild.id)
    set_announce_origin(interaction.guild.id, not ao)
    await interaction.response.send_message(f"✅ announce_origin = `{not ao}`", ephemeral=True)

@bot.tree.command(name="togglerankchannel", description="On/Off rank-ups en el canal fijo (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def togglerankchannel_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Solo en servidor.", ephemeral=True)
        return
    ao, arc = get_announce_flags(interaction.guild.id)
    set_announce_rankchannel(interaction.guild.id, not arc)
    await interaction.response.send_message(f"✅ announce_rankchannel = `{not arc}`", ephemeral=True)

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
