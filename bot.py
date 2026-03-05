import os
import time
import random
import sqlite3
import asyncio
import base64
import shutil
from dataclasses import dataclass
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# =========================
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# GitHub backup
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")          # classic token con scope "repo"
GITHUB_REPO = os.getenv("GITHUB_REPO")            # ej: "spindashh/deadlock-rank-bot"
GITHUB_BACKUP_PATH = "backup/data.db"             # se sube aquí en tu repo

# DB
DB_PATH = "data.db"
DB_TMP_COPY = "data_upload_copy.db"

# Prefijo NO-común para evitar choques con otros bots
DEFAULT_PREFIX = "dl!"

# XP settings (mensajes)
MIN_CHARS_FOR_XP = 10
XP_PER_MESSAGE_MIN = 12
XP_PER_MESSAGE_MAX = 20
XP_COOLDOWN_SECONDS = 45

# VOICE XP (cada 3 min)
VOICE_TICK_SECONDS = 180
VOICE_XP_MIN = 10
VOICE_XP_MAX = 18

# Canal donde se anuncian rank ups (pon tu canal discord-rangos)
ANNOUNCE_CHANNEL_ID = 1477135861127839884

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

# Cada cuánto guardar a GitHub (minutos)
BACKUP_EVERY_SECONDS = 300  # 5 min

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
# GITHUB BACKUP/RESTORE
# =========================

def _gh_ok() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def _gh_file_url(path: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"

async def github_download_db_if_exists():
    """
    Si hay backup/data.db en GitHub, lo baja y lo pone como data.db.
    """
    if not _gh_ok():
        print("[backup] GitHub vars missing. Skipping restore.")
        return

    import requests  # local import para no romper si no se usa

    url = _gh_file_url(GITHUB_BACKUP_PATH)

    try:
        r = requests.get(url, headers=_gh_headers(), timeout=20)
        if r.status_code == 404:
            print("[backup] No backup found on GitHub (first run).")
            return
        r.raise_for_status()
        data = r.json()
        content_b64 = data.get("content", "")
        if not content_b64:
            print("[backup] Backup exists but content empty?")
            return

        raw = base64.b64decode(content_b64)
        with open(DB_PATH, "wb") as f:
            f.write(raw)

        print("[backup] Restored data.db from GitHub.")
    except Exception as e:
        print(f"[backup] Restore failed: {e}")

def _db_checkpoint_and_copy():
    """
    Asegura que WAL se vuelque y copia a un archivo estable para subir.
    """
    if not os.path.exists(DB_PATH):
        return False

    try:
        with db_connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL);")
    except Exception:
        pass

    try:
        shutil.copyfile(DB_PATH, DB_TMP_COPY)
        return True
    except Exception:
        return False

async def github_upload_db():
    """
    Sube DB_TMP_COPY -> backup/data.db (create/update).
    """
    if not _gh_ok():
        return

    if not _db_checkpoint_and_copy():
        return

    import requests  # local import

    try:
        # leer archivo
        with open(DB_TMP_COPY, "rb") as f:
            raw = f.read()

        b64 = base64.b64encode(raw).decode("utf-8")
        url = _gh_file_url(GITHUB_BACKUP_PATH)

        # ver si existe para obtener sha
        sha = None
        r = requests.get(url, headers=_gh_headers(), timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            r.raise_for_status()

        payload = {
            "message": "Auto-backup data.db",
            "content": b64,
        }
        if sha:
            payload["sha"] = sha

        r2 = requests.put(url, headers=_gh_headers(), json=payload, timeout=30)
        r2.raise_for_status()
        print("[backup] Uploaded data.db to GitHub.")
    except Exception as e:
        print(f"[backup] Upload failed: {e}")
    finally:
        try:
            if os.path.exists(DB_TMP_COPY):
                os.remove(DB_TMP_COPY)
        except Exception:
            pass

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_EVERY_SECONDS)
        await github_upload_db()

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

# =========================
# CORE XP LOGIC
# =========================

async def try_add_xp_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
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

    await apply_level_logic_and_announce(message.guild, message.author.id, source_channel=message.channel, state=state)

async def apply_level_logic_and_announce(guild: discord.Guild, user_id: int, source_channel, state: UserState):
    leveled_up = False
    old_level = state.level
    old_rank = rank_name_from_level(state.level)
    old_prestige = state.prestige

    # Level up loop
    while True:
        need = xp_required_for_next_level(state.level)
        if state.xp >= need:
            state.xp -= need
            state.level += 1
            leveled_up = True
        else:
            break

    # Prestige
    prestiged = False
    if state.level > MAX_LEVEL_PER_PRESTIGE:
        state.prestige += 1
        state.level = 1
        state.xp = 0
        prestiged = True

    update_user(state)

    if leveled_up or prestiged:
        new_rank = rank_name_from_level(state.level)

        # siempre anunciar en el canal fijo si existe
        announce_channel = guild.get_channel(ANNOUNCE_CHANNEL_ID)
        channel = announce_channel or source_channel

        await announce_levelup(
            channel=channel,
            member=guild.get_member(user_id) or source_channel.guild.get_member(user_id),
            old_level=old_level,
            new_level=state.level,
            old_rank=old_rank,
            new_rank=new_rank,
            prestige=state.prestige,
            prestiged=prestiged,
            old_prestige=old_prestige
        )

async def announce_levelup(
    channel: discord.abc.Messageable,
    member: discord.abc.User,
    old_level: int,
    new_level: int,
    old_rank: str,
    new_rank: str,
    prestige: int,
    prestiged: bool,
    old_prestige: int
):
    if not member:
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
# VOICE XP (cada 3 min)
# =========================

def is_in_valid_voice(member: discord.Member) -> bool:
    vs = member.voice
    if not vs or not vs.channel:
        return False
    if member.bot:
        return False
    # opcional: si está deafened/afk etc. (puedes ajustar)
    return True

async def voice_xp_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                for member in guild.members:
                    if not is_in_valid_voice(member):
                        continue
                    state = get_or_create_user(member.id)
                    gained = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)
                    state.xp += gained
                    # NOTA: no usamos last_xp_ts para voice (para que voice no bloquee mensajes)
                    await apply_level_logic_and_announce(guild, member.id, source_channel=guild.system_channel or guild.text_channels[0], state=state)
        except Exception:
            pass

        await asyncio.sleep(VOICE_TICK_SECONDS)

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass

    # arrancar loops
    if _gh_ok():
        bot.loop.create_task(backup_loop())
    bot.loop.create_task(voice_xp_loop())

    print(f"Bot listo como {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    await try_add_xp_message(message)
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
        f"- `{prefix}setprefix <nuevo>` → cambia el prefijo (admin)\n\n"
        "También tienes slash commands: **/rank** y **/leaderboard**"
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

# =========================
# MAIN
# =========================

def ensure_env():
    if not DISCORD_TOKEN:
        raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")
    # GitHub vars son opcionales, pero recomendadas
    if not (_gh_ok()):
        print("[backup] TIP: agrega GITHUB_TOKEN y GITHUB_REPO para persistencia real en Railway.")

async def startup_restore():
    # si no hay data.db local, intentar restaurar de GitHub
    if not os.path.exists(DB_PATH):
        await github_download_db_if_exists()

if __name__ == "__main__":
    ensure_env()

    # restore -> init db -> run
    asyncio.run(startup_restore())
    db_init()
    bot.run(DISCORD_TOKEN)
