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

# Railway (o cualquier host): pon DISCORD_TOKEN en variables de entorno
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

DB_PATH = "data.db"

# Prefijo NO-común
DEFAULT_PREFIX = "dl!"

# Canal fijo para rank-ups
RANK_UP_CHANNEL_ID = 1477135861127839884  # #discord-rangos

# XP settings
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_COOLDOWN_SECONDS = 45

# Ranks (Deadlock)
LEVELS_PER_RANK = 10
MAX_RANK_INDEX = 10  # 0..10 = 11 rangos
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

# =========================
# HELPERS
# =========================

def clamp(n: int, a: int, b: int) -> int:
    return max(a, min(b, n))

def xp_required_for_next_level(level: int) -> int:
    return 100 + 35 * level + 5 * (level ** 2)

def rank_index_from_level(level: int) -> int:
    idx = (level - 1) // LEVELS_PER_RANK
    return clamp(idx, 0, MAX_RANK_INDEX)

def rank_name_from_level(level: int) -> str:
    return RANKS[rank_index_from_level(level)][0]

def rank_image_from_level(level: int) -> str:
    return RANKS[rank_index_from_level(level)][1]

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
                prefix          TEXT NOT NULL
            )
        """)

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
    # asegura que exista
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
    while True:
        need = xp_required_for_next_level(st.level)
        if st.xp >= need:
            st.xp -= need
            st.level += 1
            leveled_up = True
        else:
            break

    # Prestige
    if st.level > MAX_LEVEL_PER_PRESTIGE:
        st.prestige += 1
        st.level = 1
        st.xp = 0

    update_user(st)
    return st, leveled_up

def get_guild_prefix(guild_id: int) -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT prefix FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
        return row[0] if row else DEFAULT_PREFIX

def set_guild_prefix(guild_id: int, prefix: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(guild_id, prefix) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix",
            (guild_id, prefix)
        )

def top_users(limit: int = 10) -> List[Tuple[int, int, int, int]]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, prestige, level, xp FROM users "
            "ORDER BY prestige DESC, level DESC, xp DESC LIMIT ?",
            (limit,)
        ).fetchall()

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

async def get_rankup_channel(guild: Optional[discord.Guild]) -> Optional[discord.abc.Messageable]:
    # Primero intenta cache
    ch = bot.get_channel(RANK_UP_CHANNEL_ID)
    if ch:
        return ch
    # Si no está en cache, intenta fetch
    try:
        return await bot.fetch_channel(RANK_UP_CHANNEL_ID)
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
    guild: Optional[discord.Guild],
    member: discord.abc.User,
    old_level: int,
    new_level: int,
    old_rank: str,
    new_rank: str,
    prestige: int,
    prestiged: bool,
    old_prestige: int
):
    if prestiged:
        title = "🜂 PRESTIGE UNLOCKED"
        desc = f"{member.mention} trascendió el ciclo. **Prestige {old_prestige} → {prestige}**.\nReiniciando el rito…"
    else:
        title = "⚡ RANK UP"
        if new_rank != old_rank:
            desc = f"{member.mention} ascendió: **{old_rank} → {new_rank}** (Lv {old_level} → {new_level})"
        else:
            desc = f"{member.mention} subió a **Lv {new_level}** (**{new_rank}**)"

    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    embed.set_footer(text="Deadlock Chat Ranks • XP por actividad")

    img_path = rank_image_from_level(new_level)
    file: Optional[discord.File] = None
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")

    # destino extra
    rankup_channel = await get_rankup_channel(guild)

    # manda al canal original
    try:
        if file:
            await origin_channel.send(embed=embed, file=file)
        else:
            await origin_channel.send(embed=embed)
    except Exception:
        pass

    # manda también a #discord-rangos (si existe y no es el mismo canal)
    try:
        if rankup_channel and getattr(rankup_channel, "id", None) != getattr(origin_channel, "id", None):
            if file:
                # recrea el file (discord no reusa el mismo archivo bien a veces)
                file2 = discord.File(img_path, filename=os.path.basename(img_path)) if os.path.exists(img_path) else None
                if file2:
                    await rankup_channel.send(embed=embed, file=file2)
                else:
                    await rankup_channel.send(embed=embed)
            else:
                await rankup_channel.send(embed=embed)
    except Exception:
        pass

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
    msg = (
        f"**Comandos ({prefix})**\n"
        f"- `{prefix}rank` → tu rango / nivel / xp\n"
        f"- `{prefix}top` → leaderboard\n"
        f"- `{prefix}setprefix <nuevo>` → cambia el prefijo (admin)\n"
        f"- `{prefix}givexp <cantidad> [@user]` → da XP (admin)\n"
        f"- `{prefix}maxme` → ponerte rango máximo (admin)\n\n"
        "También tienes slash commands: **/rank** **/leaderboard** **/givexp** **/maxme**"
    )
    await ctx.reply(msg, mention_author=False)

@bot.command(name="rank")
async def rank_text(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    st = get_or_create_user(member.id)
    rank = rank_name_from_level(st.level)
    need = xp_required_for_next_level(st.level)
    await ctx.reply(
        f"**{member.display_name}** • Prestige **{st.prestige}** • **{rank}**\n"
        f"Lv **{st.level}** • XP **{st.xp}/{need}**",
        mention_author=False
    )

@bot.command(name="top")
async def top_text(ctx: commands.Context):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        user = ctx.guild.get_member(uid) if ctx.guild else None
        name = user.display_name if user else f"<@{uid}>"
        rname = rank_name_from_level(lvl)
        lines.append(f"**{i}.** {name} — P{p} • Lv{lvl} • {rname}")
    await ctx.reply("🏆 **Leaderboard**\n" + "\n".join(lines), mention_author=False)

@bot.command(name="setprefix")
@commands.has_permissions(manage_guild=True)
async def setprefix_cmd(ctx: commands.Context, new_prefix: str):
    if len(new_prefix) > 8:
        await ctx.reply("Muy largo. Usa algo corto (ej: `dl!` `dl.` `d!`).", mention_author=False)
        return
    set_guild_prefix(ctx.guild.id, new_prefix)
    await ctx.reply(f"Listo. Nuevo prefijo: `{new_prefix}`", mention_author=False)

@setprefix_cmd.error
async def setprefix_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Necesitas **Manage Server** para cambiar el prefijo.", mention_author=False)

@bot.command(name="givexp")
@commands.has_permissions(manage_guild=True)
async def givexp_cmd(ctx: commands.Context, amount: int, member: Optional[discord.Member] = None):
    member = member or ctx.author
    st_before = get_or_create_user(member.id)
    old_level = st_before.level
    old_rank = rank_name_from_level(st_before.level)
    old_prestige = st_before.prestige

    st_after, leveled = add_user_xp(member.id, amount)

    await ctx.reply(
        f"✅ XP dado a **{member.display_name}**: +{amount}\n"
        f"Ahora: P{st_after.prestige} • Lv {st_after.level} • XP {st_after.xp}/{xp_required_for_next_level(st_after.level)}",
        mention_author=False
    )

    # Si subió de nivel por el give, anunciar también
    if (st_after.level != old_level) or (st_after.prestige != old_prestige):
        await announce_levelup(
            origin_channel=ctx.channel,
            guild=ctx.guild,
            member=member,
            old_level=old_level,
            new_level=st_after.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(st_after.level),
            prestige=st_after.prestige,
            prestiged=(st_after.prestige != old_prestige),
            old_prestige=old_prestige
        )

@givexp_cmd.error
async def givexp_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Necesitas **Manage Server** para usar esto.", mention_author=False)
    else:
        await ctx.reply("Uso: `dl!givexp <cantidad> [@user]`", mention_author=False)

@bot.command(name="maxme")
@commands.has_permissions(manage_guild=True)
async def maxme_cmd(ctx: commands.Context):
    # “rango maximo” dentro del sistema: Eternus = Lv 110
    set_user_progress(ctx.author.id, prestige=0, level=MAX_LEVEL_PER_PRESTIGE, xp=0)
    await ctx.reply(
        f"✅ Listo: **{ctx.author.display_name}** ahora es **{rank_name_from_level(MAX_LEVEL_PER_PRESTIGE)}** (Lv {MAX_LEVEL_PER_PRESTIGE}).",
        mention_author=False
    )

@maxme_cmd.error
async def maxme_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Necesitas **Manage Server** para usar esto.", mention_author=False)

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
        color=discord.Color.blurple()
    )
    img_path = rank_image_from_level(st.level)
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")
        await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="leaderboard", description="Top 10 global del bot")
async def leaderboard_slash(interaction: discord.Interaction):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        lines.append(f"**{i}.** <@{uid}> — P{p} • Lv{lvl} • {rank_name_from_level(lvl)}")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setprefix", description="Cambia el prefijo de comandos de texto (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setprefix_slash(interaction: discord.Interaction, new_prefix: str):
    if len(new_prefix) > 8:
        await interaction.response.send_message("Muy largo. Usa algo corto (ej: `dl!` `dl.` `d!`).", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Solo en servidor.", ephemeral=True)
        return
    set_guild_prefix(interaction.guild.id, new_prefix)
    await interaction.response.send_message(f"Listo. Nuevo prefijo: `{new_prefix}`", ephemeral=True)

@bot.tree.command(name="givexp", description="Da XP (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def givexp_slash(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
    user = user or interaction.user

    st_before = get_or_create_user(user.id)
    old_level = st_before.level
    old_rank = rank_name_from_level(st_before.level)
    old_prestige = st_before.prestige

    st_after, _ = add_user_xp(user.id, amount)

    await interaction.response.send_message(
        f"✅ XP dado a **{user.display_name}**: +{amount}\n"
        f"Ahora: P{st_after.prestige} • Lv {st_after.level} • XP {st_after.xp}/{xp_required_for_next_level(st_after.level)}",
        ephemeral=True
    )

    if interaction.channel and (st_after.level != old_level or st_after.prestige != old_prestige):
        await announce_levelup(
            origin_channel=interaction.channel,
            guild=interaction.guild,
            member=user,
            old_level=old_level,
            new_level=st_after.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(st_after.level),
            prestige=st_after.prestige,
            prestiged=(st_after.prestige != old_prestige),
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

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
