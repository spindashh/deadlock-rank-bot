import os
import time
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================
# CONFIG
# =========================

# Railway / Deploy: usa variable de entorno
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN or TOKEN.strip() == "":
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

DB_PATH = "data.db"

# Prefijo NO-común para evitar choques con otros bots
DEFAULT_PREFIX = "dl!"

# Canal fijo para anunciar rank ups (tu canal discord-rangos)
LEVELUP_CHANNEL_ID = 1477135861127839884

# XP por mensajes
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_COOLDOWN_SECONDS = 45  # 1 tick cada 45s por usuario

# VOICE XP
VOICE_XP_ENABLED = True
VOICE_TICK_SECONDS = 180          # ✅ cada 3 minutos
VOICE_XP_MIN = 10
VOICE_XP_MAX = 16
VOICE_REQUIRE_2_HUMANS = True     # evita farm solo
VOICE_BLOCK_DEAF = True           # si está deaf/self_deaf no cuenta

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
    # curva estable
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
    last_xp_ts: int  # cooldown mensajes

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
        # Voice tick tracking (NO toca users)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice (
                user_id        INTEGER PRIMARY KEY,
                guild_id       INTEGER NOT NULL DEFAULT 0,
                channel_id     INTEGER NOT NULL DEFAULT 0,
                last_tick_ts   INTEGER NOT NULL DEFAULT 0
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
        rows = conn.execute(
            "SELECT user_id, prestige, level, xp FROM users "
            "ORDER BY prestige DESC, level DESC, xp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return rows

# ---- Voice DB helpers ----

def voice_upsert(user_id: int, guild_id: int, channel_id: int, last_tick_ts: int):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO voice(user_id, guild_id, channel_id, last_tick_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET guild_id=excluded.guild_id, channel_id=excluded.channel_id, last_tick_ts=excluded.last_tick_ts",
            (user_id, guild_id, channel_id, last_tick_ts)
        )

def voice_delete(user_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM voice WHERE user_id=?", (user_id,))

def voice_get(user_id: int) -> Optional[Tuple[int, int, int, int]]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, guild_id, channel_id, last_tick_ts FROM voice WHERE user_id=?",
            (user_id,)
        ).fetchone()

# =========================
# DISCORD BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

async def dynamic_prefix(bot: commands.Bot, message: discord.Message):
    if message.guild:
        return get_guild_prefix(message.guild.id)
    return DEFAULT_PREFIX

bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

def get_levelup_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(LEVELUP_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None

# =========================
# RANKUP ANNOUNCE
# =========================

async def announce_levelup(
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
    ch = get_levelup_channel(guild)
    if not ch:
        return

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
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")
        try:
            await ch.send(embed=embed, file=file)
        except Exception:
            pass
    else:
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

# =========================
# XP CORE
# =========================

def apply_xp_and_levels(state: UserState, gained_xp: int) -> Tuple[bool, bool, int, str, int]:
    """
    returns:
      leveled_up, prestiged, old_level, old_rank, old_prestige
    """
    old_level = state.level
    old_rank = rank_name_from_level(state.level)
    old_prestige = state.prestige

    state.xp += gained_xp

    leveled_up = False
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

    return leveled_up, prestiged, old_level, old_rank, old_prestige

async def try_add_xp_from_message(message: discord.Message):
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

    leveled_up, prestiged, old_level, old_rank, old_prestige = apply_xp_and_levels(state, gained)
    update_user(state)

    if leveled_up or prestiged:
        new_rank = rank_name_from_level(state.level)
        await announce_levelup(
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

# =========================
# VOICE XP
# =========================

def humans_in_voice(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)

def eligible_voice_member(m: discord.Member) -> bool:
    if m.bot:
        return False
    vs = m.voice
    if not vs or not vs.channel:
        return False
    if VOICE_BLOCK_DEAF and (vs.self_deaf or vs.deaf):
        return False
    return True

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not VOICE_XP_ENABLED:
        return
    if member.bot or not member.guild:
        return

    now = int(time.time())

    # Entró
    if before.channel is None and after.channel is not None:
        voice_upsert(member.id, member.guild.id, after.channel.id, now)
        return

    # Salió
    if before.channel is not None and after.channel is None:
        voice_delete(member.id)
        return

    # Cambio de canal
    if before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        voice_upsert(member.id, member.guild.id, after.channel.id, now)
        return

@tasks.loop(seconds=VOICE_TICK_SECONDS)
async def voice_xp_loop():
    if not VOICE_XP_ENABLED:
        return

    now = int(time.time())

    for guild in bot.guilds:
        # recorremos canales de voz y sus miembros (no depende de guild.members)
        for vc in guild.voice_channels:
            if VOICE_REQUIRE_2_HUMANS and humans_in_voice(vc) < 2:
                continue

            for m in vc.members:
                if not eligible_voice_member(m):
                    continue

                row = voice_get(m.id)
                if not row:
                    voice_upsert(m.id, guild.id, vc.id, now)
                    continue

                _, g_id, ch_id, last_tick = row

                # si cambió algo, actualiza y sigue
                if g_id != guild.id or ch_id != vc.id:
                    voice_upsert(m.id, guild.id, vc.id, now)
                    continue

                if now - last_tick < VOICE_TICK_SECONDS:
                    continue

                gained = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)
                st = get_or_create_user(m.id)

                leveled_up, prestiged, old_level, old_rank, old_prestige = apply_xp_and_levels(st, gained)
                update_user(st)
                voice_upsert(m.id, guild.id, vc.id, now)

                if leveled_up or prestiged:
                    new_rank = rank_name_from_level(st.level)
                    await announce_levelup(
                        guild=guild,
                        member=m,
                        old_level=old_level,
                        new_level=st.level,
                        old_rank=old_rank,
                        new_rank=new_rank,
                        prestige=st.prestige,
                        prestiged=prestiged,
                        old_prestige=old_prestige
                    )

@voice_xp_loop.before_loop
async def before_voice_xp_loop():
    await bot.wait_until_ready()

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass

    if VOICE_XP_ENABLED and not voice_xp_loop.is_running():
        voice_xp_loop.start()

    print(f"Bot listo como {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    await try_add_xp_from_message(message)
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
        f"- `{prefix}maxme` → te pone max rank (solo admin)\n\n"
        "Slash:\n"
        "- `/rank` (público)\n"
        "- `/leaderboard`\n"
        "- `/setprefix` (admin)\n"
        "- `/maxme` (admin)\n"
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

@bot.command(name="maxme")
@commands.has_permissions(manage_guild=True)
async def maxme_cmd(ctx: commands.Context):
    st = get_or_create_user(ctx.author.id)
    st.prestige = 0
    st.level = MAX_LEVEL_PER_PRESTIGE
    st.xp = 0
    update_user(st)
    await ctx.reply(f"Listo. Te puse en **Lv {st.level} ({rank_name_from_level(st.level)})**.", mention_author=False)

@maxme_cmd.error
async def maxme_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("Solo admin (Manage Server).", mention_author=False)

# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(name="rank", description="Muestra tu rango/nivel/xp (público)")
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
        await interaction.response.send_message(embed=embed, file=file, ephemeral=False)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="leaderboard", description="Top 10 del server")
async def leaderboard_slash(interaction: discord.Interaction):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        lines.append(f"**{i}.** <@{uid}> — P{p} • Lv{lvl} • {rank_name_from_level(lvl)}")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="setprefix", description="Cambia el prefijo (admin)")
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

@bot.tree.command(name="maxme", description="Te pone max level (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def maxme_slash(interaction: discord.Interaction):
    st = get_or_create_user(interaction.user.id)
    st.prestige = 0
    st.level = MAX_LEVEL_PER_PRESTIGE
    st.xp = 0
    update_user(st)
    await interaction.response.send_message(
        f"Listo. Te puse en **Lv {st.level} ({rank_name_from_level(st.level)})**.",
        ephemeral=True
    )

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
