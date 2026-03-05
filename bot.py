import os
import time
import json
import base64
import random
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================
# ENV / CONFIG
# =========================

def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "", "null", "None") else default

DISCORD_TOKEN = get_env("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

# Canal donde se anuncian los rank ups (tu id por defecto)
RANKUP_CHANNEL_ID = int(get_env("RANKUP_CHANNEL_ID", "1477135861127839884"))

# GitHub backup
GITHUB_TOKEN = get_env("GITHUB_TOKEN")  # opcional pero recomendado
GITHUB_REPO = get_env("GITHUB_REPO")    # ej: "spindashh/deadlock-rank-bot"
GITHUB_BRANCH = get_env("GITHUB_BRANCH", "main")
GITHUB_BACKUP_PATH = get_env("GITHUB_BACKUP_PATH", "backup/data_backup.json")

DB_PATH = "data.db"
DEFAULT_PREFIX = "dl!"

# XP por mensajes
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_MSG_COOLDOWN_SECONDS = 45

# XP por voz (cada 3 min)
VOICE_XP_EVERY_SECONDS = 180
VOICE_XP_MIN = 8
VOICE_XP_MAX = 14

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
    last_msg_xp_ts: int
    last_voice_xp_ts: int

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
                user_id          INTEGER PRIMARY KEY,
                xp               INTEGER NOT NULL DEFAULT 0,
                level            INTEGER NOT NULL DEFAULT 1,
                prestige         INTEGER NOT NULL DEFAULT 0,
                last_msg_xp_ts   INTEGER NOT NULL DEFAULT 0,
                last_voice_xp_ts INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id        INTEGER PRIMARY KEY,
                prefix          TEXT NOT NULL
            )
        """)

        # Migración por si venías de una DB vieja (sin campos nuevos)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "last_msg_xp_ts" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_msg_xp_ts INTEGER NOT NULL DEFAULT 0")
        if "last_voice_xp_ts" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_voice_xp_ts INTEGER NOT NULL DEFAULT 0")

def get_or_create_user(user_id: int) -> UserState:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT user_id, xp, level, prestige, last_msg_xp_ts, last_voice_xp_ts FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if row:
            return UserState(*row)
        conn.execute(
            "INSERT INTO users(user_id, xp, level, prestige, last_msg_xp_ts, last_voice_xp_ts) VALUES (?,0,1,0,0,0)",
            (user_id,)
        )
        return UserState(user_id=user_id, xp=0, level=1, prestige=0, last_msg_xp_ts=0, last_voice_xp_ts=0)

def update_user(state: UserState):
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET xp=?, level=?, prestige=?, last_msg_xp_ts=?, last_voice_xp_ts=? WHERE user_id=?",
            (state.xp, state.level, state.prestige, state.last_msg_xp_ts, state.last_voice_xp_ts, state.user_id)
        )

def user_count() -> int:
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0

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
# GITHUB BACKUP (JSON)
# =========================

def _gh_api(url: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        raise RuntimeError("GitHub backup no configurado (faltan GITHUB_TOKEN o GITHUB_REPO).")

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("User-Agent", "deadlock-rank-bot")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

def github_get_file(path: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    try:
        return _gh_api(url, "GET")
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
    except URLError:
        return None

def github_put_file(path: str, content_bytes: bytes, message: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    existing = github_get_file(path)
    sha = existing.get("sha") if existing else None

    b64 = base64.b64encode(content_bytes).decode("utf-8")
    payload = {
        "message": message,
        "content": b64,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    _gh_api(url, "PUT", payload)

def export_db_to_json() -> Dict[str, Any]:
    with db_connect() as conn:
        users = conn.execute(
            "SELECT user_id, xp, level, prestige, last_msg_xp_ts, last_voice_xp_ts FROM users"
        ).fetchall()
        settings = conn.execute(
            "SELECT guild_id, prefix FROM settings"
        ).fetchall()

    return {
        "version": 1,
        "ts": int(time.time()),
        "users": [
            {
                "user_id": int(u[0]),
                "xp": int(u[1]),
                "level": int(u[2]),
                "prestige": int(u[3]),
                "last_msg_xp_ts": int(u[4]),
                "last_voice_xp_ts": int(u[5]),
            }
            for u in users
        ],
        "settings": [
            {"guild_id": int(s[0]), "prefix": str(s[1])}
            for s in settings
        ]
    }

def import_json_to_db(payload: Dict[str, Any]):
    users = payload.get("users", [])
    settings = payload.get("settings", [])

    with db_connect() as conn:
        for u in users:
            conn.execute(
                """
                INSERT INTO users(user_id, xp, level, prestige, last_msg_xp_ts, last_voice_xp_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  xp=excluded.xp,
                  level=excluded.level,
                  prestige=excluded.prestige,
                  last_msg_xp_ts=excluded.last_msg_xp_ts,
                  last_voice_xp_ts=excluded.last_voice_xp_ts
                """,
                (
                    int(u["user_id"]),
                    int(u.get("xp", 0)),
                    int(u.get("level", 1)),
                    int(u.get("prestige", 0)),
                    int(u.get("last_msg_xp_ts", 0)),
                    int(u.get("last_voice_xp_ts", 0)),
                )
            )

        for s in settings:
            conn.execute(
                """
                INSERT INTO settings(guild_id, prefix)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET prefix=excluded.prefix
                """,
                (int(s["guild_id"]), str(s.get("prefix", DEFAULT_PREFIX)))
            )

async def maybe_restore_from_github():
    """
    Si la DB está vacía (típico después de restart sin volumen),
    intentamos restaurar desde GitHub.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return

    # Si hay datos, no tocamos nada
    if user_count() > 0:
        return

    file_obj = github_get_file(GITHUB_BACKUP_PATH)
    if not file_obj:
        return

    try:
        content_b64 = file_obj.get("content", "")
        content_bytes = base64.b64decode(content_b64)
        payload = json.loads(content_bytes.decode("utf-8"))
        import_json_to_db(payload)
        print("[RESTORE] DB restaurada desde GitHub backup.")
    except Exception as e:
        print("[RESTORE] Falló restore:", e)

async def backup_to_github():
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return
    try:
        payload = export_db_to_json()
        content_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        github_put_file(
            GITHUB_BACKUP_PATH,
            content_bytes,
            message=f"bot backup @ {payload['ts']}"
        )
        print("[BACKUP] OK -> GitHub")
    except Exception as e:
        print("[BACKUP] FAIL:", e)

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

def get_rankup_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(RANKUP_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None

# =========================
# XP LOGIC
# =========================

async def award_xp(user_id: int, gained: int, source: str, announce_guild: Optional[discord.Guild] = None):
    state = get_or_create_user(user_id)

    old_level = state.level
    old_rank = rank_name_from_level(state.level)
    old_prestige = state.prestige

    state.xp += gained

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

    update_user(state)

    if (leveled_up or prestiged) and announce_guild:
        ch = get_rankup_channel(announce_guild)
        if ch:
            member = announce_guild.get_member(user_id)
            if member:
                new_rank = rank_name_from_level(state.level)
                await announce_levelup(
                    channel=ch,
                    member=member,
                    old_level=old_level,
                    new_level=state.level,
                    old_rank=old_rank,
                    new_rank=new_rank,
                    prestige=state.prestige,
                    prestiged=prestiged,
                    old_prestige=old_prestige,
                    source=source
                )

async def try_add_msg_xp(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_CHARS_FOR_XP:
        return

    state = get_or_create_user(message.author.id)
    now = int(time.time())
    if now - state.last_msg_xp_ts < XP_MSG_COOLDOWN_SECONDS:
        return

    gained = random.randint(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
    state.last_msg_xp_ts = now
    update_user(state)  # guardamos cooldown
    await award_xp(message.author.id, gained, source="chat", announce_guild=message.guild)

@tasks.loop(seconds=VOICE_XP_EVERY_SECONDS)
async def voice_xp_tick():
    """
    Cada 3 min:
    si el usuario está en VC, gana XP.
    """
    await bot.wait_until_ready()
    now = int(time.time())

    for guild in bot.guilds:
        # junta todos los miembros en voz (sin bots)
        in_voice: List[discord.Member] = []
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    in_voice.append(m)

        # dedup por si
        seen = set()
        for m in in_voice:
            if m.id in seen:
                continue
            seen.add(m.id)

            st = get_or_create_user(m.id)
            if now - st.last_voice_xp_ts < VOICE_XP_EVERY_SECONDS:
                continue

            gained = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)
            st.last_voice_xp_ts = now
            update_user(st)  # guardamos cooldown de voz
            await award_xp(m.id, gained, source="voice", announce_guild=guild)

async def announce_levelup(
    channel: discord.abc.Messageable,
    member: discord.abc.User,
    old_level: int,
    new_level: int,
    old_rank: str,
    new_rank: str,
    prestige: int,
    prestiged: bool,
    old_prestige: int,
    source: str
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

    footer = "Deadlock Chat Ranks • XP por actividad"
    if source == "voice":
        footer += " • voz"
    elif source == "chat":
        footer += " • chat"

    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    embed.set_footer(text=footer)

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
# EVENTS
# =========================

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print(f"Bot listo como {bot.user}")

    # restore si hace falta
    await maybe_restore_from_github()

    # arrancar tasks
    if not voice_xp_tick.is_running():
        voice_xp_tick.start()
    if not github_backup_tick.is_running():
        github_backup_tick.start()

@bot.event
async def on_message(message: discord.Message):
    await try_add_msg_xp(message)
    await bot.process_commands(message)

# =========================
# BACKUP TASK
# =========================

@tasks.loop(minutes=5)
async def github_backup_tick():
    await bot.wait_until_ready()
    await backup_to_github()

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
        f"- `{prefix}backup` → fuerza backup a GitHub (admin)\n\n"
        "Slash:\n"
        "- `/rank` (público)\n"
        "- `/rankme` (privado)\n"
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

@bot.command(name="backup")
@commands.has_permissions(administrator=True)
async def backup_cmd(ctx: commands.Context):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        await ctx.reply("No está configurado GitHub backup (GITHUB_TOKEN / GITHUB_REPO).", mention_author=False)
        return
    await backup_to_github()
    await ctx.reply("✅ Backup forzado a GitHub.", mention_author=False)

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

@bot.tree.command(name="rankme", description="Muestra tu rango/nivel/xp (solo tú)")
async def rankme_slash(interaction: discord.Interaction):
    st = get_or_create_user(interaction.user.id)
    rank = rank_name_from_level(st.level)
    need = xp_required_for_next_level(st.level)

    embed = discord.Embed(
        title=f"{interaction.user.display_name} • {rank}",
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

@bot.tree.command(name="maxme", description="Te pone rango max (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def maxme_slash(interaction: discord.Interaction, prestige: Optional[int] = 0):
    st = get_or_create_user(interaction.user.id)
    st.prestige = int(prestige or 0)
    st.level = MAX_LEVEL_PER_PRESTIGE
    st.xp = 0
    update_user(st)
    await interaction.response.send_message(
        f"✅ Hecho. {interaction.user.mention} ahora está en **Prestige {st.prestige} • Lv {st.level} ({rank_name_from_level(st.level)})**",
        ephemeral=True
    )

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN)
