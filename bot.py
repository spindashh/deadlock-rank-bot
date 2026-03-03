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

# En Railway/Render/etc crea una variable de entorno:
# DISCORD_TOKEN = tu_token
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

DB_PATH = "data.db"

# Canal donde SIEMPRE se anuncian los rank-ups (tu canal discord-rangos)
RANKUP_CHANNEL_ID = 1477135861127839884

# Prefijo NO-común para evitar choques con otros bots
DEFAULT_PREFIX = "dl!"

# XP por mensajes
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_COOLDOWN_SECONDS = 45

# XP por voz
VOICE_XP_ENABLED = True
VOICE_XP_PER_MIN = 5
VOICE_MIN_SECONDS = 60
VOICE_REQUIRE_2_USERS = True
VOICE_IGNORE_MUTED = True

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
    # curva suave
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

# =========================
# DISCORD BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

async def dynamic_prefix(bot: commands.Bot, message: discord.Message):
    if message.guild:
        return get_guild_prefix(message.guild.id)
    return DEFAULT_PREFIX

bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

# sesiones de voz
voice_sessions = {}  # user_id -> (guild_id, channel_id, join_ts)

# =========================
# ANNOUNCE / TARGET CHANNEL
# =========================

def get_rankup_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(RANKUP_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        fetched = bot.get_channel(RANKUP_CHANNEL_ID)
        if isinstance(fetched, discord.TextChannel):
            return fetched
    except Exception:
        pass
    return None

async def announce_levelup(
    guild: discord.Guild,
    fallback_channel: discord.abc.Messageable,
    member: discord.abc.User,
    old_level: int,
    new_level: int,
    old_rank: str,
    new_rank: str,
    prestige: int,
    prestiged: bool,
    old_prestige: int
):
    channel = get_rankup_channel(guild) or fallback_channel

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

    try:
        if file:
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)
    except Exception:
        pass

# =========================
# XP LOGIC (MESSAGES)
# =========================

async def try_add_xp_from_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_CHARS_FOR_XP:
        return

    st = get_or_create_user(message.author.id)
    now = int(time.time())

    if now - st.last_xp_ts < XP_COOLDOWN_SECONDS:
        return

    gained = random.randint(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
    st.last_xp_ts = now
    st.xp += gained

    leveled_up = False
    old_level = st.level
    old_rank = rank_name_from_level(st.level)
    old_prestige = st.prestige

    # level loop
    while True:
        need = xp_required_for_next_level(st.level)
        if st.xp >= need:
            st.xp -= need
            st.level += 1
            leveled_up = True
        else:
            break

    # prestige
    prestiged = False
    if st.level > MAX_LEVEL_PER_PRESTIGE:
        st.prestige += 1
        st.level = 1
        st.xp = 0
        prestiged = True

    update_user(st)

    if leveled_up or prestiged:
        await announce_levelup(
            guild=message.guild,
            fallback_channel=message.channel,
            member=message.author,
            old_level=old_level,
            new_level=st.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(st.level),
            prestige=st.prestige,
            prestiged=prestiged,
            old_prestige=old_prestige
        )

# =========================
# XP LOGIC (VOICE)
# =========================

def voice_valid_for_xp(vs: discord.VoiceState) -> bool:
    if not vs or not vs.channel:
        return False
    if VOICE_IGNORE_MUTED and (vs.self_mute or vs.self_deaf or vs.mute or vs.deaf):
        return False
    if VOICE_REQUIRE_2_USERS:
        humans = [m for m in vs.channel.members if not m.bot]
        if len(humans) < 2:
            return False
    return True

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not VOICE_XP_ENABLED or member.bot or not member.guild:
        return

    now = int(time.time())

    # cerrar sesión anterior si era válida
    if voice_valid_for_xp(before):
        if member.id in voice_sessions:
            _, _, join_ts = voice_sessions[member.id]
            dur = now - join_ts
            if dur >= VOICE_MIN_SECONDS:
                mins = dur // 60
                if mins > 0:
                    st = get_or_create_user(member.id)
                    st.xp += mins * VOICE_XP_PER_MIN

                    # si quieres que voz también haga level-ups, descomenta:
                    leveled_up = False
                    old_level = st.level
                    old_rank = rank_name_from_level(st.level)
                    old_prestige = st.prestige

                    while True:
                        need = xp_required_for_next_level(st.level)
                        if st.xp >= need:
                            st.xp -= need
                            st.level += 1
                            leveled_up = True
                        else:
                            break

                    prestiged = False
                    if st.level > MAX_LEVEL_PER_PRESTIGE:
                        st.prestige += 1
                        st.level = 1
                        st.xp = 0
                        prestiged = True

                    update_user(st)

                    if leveled_up or prestiged:
                        # manda anuncio al canal de rangos
                        ch = get_rankup_channel(member.guild)
                        fallback = ch or (member.guild.system_channel or None)
                        if fallback:
                            await announce_levelup(
                                guild=member.guild,
                                fallback_channel=fallback,
                                member=member,
                                old_level=old_level,
                                new_level=st.level,
                                old_rank=old_rank,
                                new_rank=rank_name_from_level(st.level),
                                prestige=st.prestige,
                                prestiged=prestiged,
                                old_prestige=old_prestige
                            )
        voice_sessions.pop(member.id, None)

    # abrir nueva sesión si ahora es válida
    if voice_valid_for_xp(after):
        voice_sessions[member.id] = (member.guild.id, after.channel.id, now)

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
    await try_add_xp_from_message(message)
    await bot.process_commands(message)

# =========================
# TEXT COMMANDS (dl!)
# =========================

@bot.command(name="commands")
async def commands_list(ctx: commands.Context):
    prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
    msg = (
        f"**Comandos ({prefix})**\n"
        f"- `{prefix}rank` → tu rango / nivel / xp (público)\n"
        f"- `{prefix}top` → leaderboard (público)\n"
        f"- `{prefix}setprefix <nuevo>` → cambia el prefijo (admin)\n"
        f"- `{prefix}maxme` → te pone max rank (admin)\n"
        f"- `{prefix}givexp @user <cantidad>` → dar XP manual (admin)\n\n"
        "Slash commands:\n"
        "- `/rank` (público)\n"
        "- `/leaderboard`\n"
        "- `/setprefix` (admin)\n"
        "- `/maxme` (admin)\n"
        "- `/givexp` (admin)\n"
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
@commands.has_permissions(administrator=True)
async def maxme_cmd(ctx: commands.Context):
    st = get_or_create_user(ctx.author.id)
    st.prestige = 0
    st.level = MAX_LEVEL_PER_PRESTIGE
    st.xp = 0
    update_user(st)
    await ctx.reply("Listo. Te puse en **max rank** 😈", mention_author=False)

@bot.command(name="givexp")
@commands.has_permissions(administrator=True)
async def givexp_cmd(ctx: commands.Context, member: discord.Member, amount: int):
    amount = max(0, amount)
    st = get_or_create_user(member.id)
    st.xp += amount

    leveled_up = False
    old_level = st.level
    old_rank = rank_name_from_level(st.level)
    old_prestige = st.prestige

    while True:
        need = xp_required_for_next_level(st.level)
        if st.xp >= need:
            st.xp -= need
            st.level += 1
            leveled_up = True
        else:
            break

    prestiged = False
    if st.level > MAX_LEVEL_PER_PRESTIGE:
        st.prestige += 1
        st.level = 1
        st.xp = 0
        prestiged = True

    update_user(st)

    await ctx.reply(f"OK: le di **{amount} XP** a {member.mention}.", mention_author=False)

    if leveled_up or prestiged:
        await announce_levelup(
            guild=ctx.guild,
            fallback_channel=ctx.channel,
            member=member,
            old_level=old_level,
            new_level=st.level,
            old_rank=old_rank,
            new_rank=rank_name_from_level(st.level),
            prestige=st.prestige,
            prestiged=prestiged,
            old_prestige=old_prestige
        )

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
        await interaction.response.send_message(embed=embed, file=file, ephemeral=False)  # <- PUBLICO
    else:
        await interaction.response.send_message(embed=embed, ephemeral=False)  # <- PUBLICO

@bot.tree.command(name="leaderboard", description="Top 10 del server")
async def leaderboard_slash(interaction: discord.Interaction):
    rows = top_users(10)
    lines = []
    for i, (uid, p, lvl, xp) in enumerate(rows, start=1):
        lines.append(f"**{i}.** <@{uid}> — P{p} • Lv{lvl} • {rank_name_from_level(lvl)}")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=False)

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

@bot.tree.command(name="maxme", description="Te pone max rank (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def maxme_slash(interaction: discord.Interaction):
    st = get_or_create_user(interaction.user.id)
    st.prestige = 0
    st.level = MAX_LEVEL_PER_PRESTIGE
    st.xp = 0
    update_user(st)
    await interaction.response.send_message("Listo. Te puse en **max rank** 😈", ephemeral=True)

@bot.tree.command(name="givexp", description="Dar XP manual (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def givexp_slash(interaction: discord.Interaction, user: discord.Member, amount: int):
    amount = max(0, amount)
    st = get_or_create_user(user.id)
    st.xp += amount

    leveled_up = False
    old_level = st.level
    old_rank = rank_name_from_level(st.level)
    old_prestige = st.prestige

    while True:
        need = xp_required_for_next_level(st.level)
        if st.xp >= need:
            st.xp -= need
            st.level += 1
            leveled_up = True
        else:
            break

    prestiged = False
    if st.level > MAX_LEVEL_PER_PRESTIGE:
        st.prestige += 1
        st.level = 1
        st.xp = 0
        prestiged = True

    update_user(st)

    await interaction.response.send_message(f"OK: le di **{amount} XP** a {user.mention}.", ephemeral=True)

    if leveled_up or prestiged and interaction.guild:
        # anunciar en canal de rangos
        ch = get_rankup_channel(interaction.guild)
        if ch:
            await announce_levelup(
                guild=interaction.guild,
                fallback_channel=ch,
                member=user,
                old_level=old_level,
                new_level=st.level,
                old_rank=old_rank,
                new_rank=rank_name_from_level(st.level),
                prestige=st.prestige,
                prestiged=prestiged,
                old_prestige=old_prestige
            )

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
