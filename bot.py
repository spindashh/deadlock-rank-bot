import os
import time
import json
import base64
import sqlite3
import asyncio
from typing import Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands

# =========================
# CONFIG (ENV VARS)
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# GitHub backup (Contents API)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # classic token con scope repo
GITHUB_REPO = os.getenv("GITHUB_REPO")    # ej: "spindashh/deadlock-rank-bot"
GITHUB_BACKUP_PATH = os.getenv("GITHUB_BACKUP_PATH", "backup/data.db")
BACKUP_EVERY_SECONDS = int(os.getenv("BACKUP_EVERY_SECONDS", "300"))  # 5 min default

# Rankups channel
RANKUP_CHANNEL_ID = int(os.getenv("RANKUP_CHANNEL_ID", "0"))

# DB file
DB_PATH = os.getenv("DB_PATH", "data.db")

# XP tuning (mensajes)
MSG_XP_MIN = int(os.getenv("MSG_XP_MIN", "8"))
MSG_XP_MAX = int(os.getenv("MSG_XP_MAX", "16"))
MSG_COOLDOWN_SECONDS = int(os.getenv("MSG_COOLDOWN_SECONDS", "45"))

# XP tuning (voice)
VOICE_XP_EVERY_SECONDS = int(os.getenv("VOICE_XP_EVERY_SECONDS", "180"))  # 3 min
VOICE_XP_PER_TICK = int(os.getenv("VOICE_XP_PER_TICK", "12"))

# Level curve
XP_BASE = int(os.getenv("XP_BASE", "250"))
XP_STEP = int(os.getenv("XP_STEP", "60"))

# Max level (ETERNUS)
MAX_LEVEL = int(os.getenv("MAX_LEVEL", "110"))

# =========================
# DISCORD SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

def dynamic_prefix(bot: commands.Bot, message: discord.Message):
    return "!"

bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

# =========================
# DB HELPERS
# =========================

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    """
    Crea tabla y aplica migraciones para DB viejas
    (esto evita el crash: 'no column named last_msg_ts')
    """
    with db_connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            prestige INTEGER NOT NULL DEFAULT 0,
            last_msg_ts REAL NOT NULL DEFAULT 0
        )
        """)
        con.commit()

        # ---- MIGRATIONS (para DB viejas) ----
        cur = con.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in cur.fetchall()}

        # Si tu DB vieja no tenía last_msg_ts, se la agregamos.
        if "last_msg_ts" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN last_msg_ts REAL NOT NULL DEFAULT 0")
            con.commit()
            print("[db] Migrated: added column last_msg_ts")

def get_or_create_user(user_id: int) -> sqlite3.Row:
    with db_connect() as con:
        cur = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            return row

        con.execute(
            "INSERT INTO users (user_id, xp, level, prestige, last_msg_ts) VALUES (?, 0, 1, 0, 0)",
            (user_id,)
        )
        con.commit()

        cur = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def update_user(
    user_id: int,
    *,
    xp: Optional[int] = None,
    level: Optional[int] = None,
    prestige: Optional[int] = None,
    last_msg_ts: Optional[float] = None
):
    fields = []
    values = []

    if xp is not None:
        fields.append("xp=?")
        values.append(int(xp))
    if level is not None:
        fields.append("level=?")
        values.append(int(level))
    if prestige is not None:
        fields.append("prestige=?")
        values.append(int(prestige))
    if last_msg_ts is not None:
        fields.append("last_msg_ts=?")
        values.append(float(last_msg_ts))

    if not fields:
        return

    values.append(user_id)
    with db_connect() as con:
        con.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", tuple(values))
        con.commit()

def top_users(limit: int = 10) -> List[sqlite3.Row]:
    with db_connect() as con:
        cur = con.execute("""
            SELECT * FROM users
            ORDER BY prestige DESC, level DESC, xp DESC
            LIMIT ?
        """, (limit,))
        return cur.fetchall()

# =========================
# RANK LOGIC
# =========================
# Rangos cada ~10 niveles, max = ETERNUS (lvl 110)
# Tus imágenes: ranks/01_initiate.png ... ranks/11_eternus.png

RANK_TITLES = [
    (1,   "Initiate"),
    (10,  "Seeker"),
    (20,  "Alchemist"),
    (30,  "Arcanist"),
    (40,  "Ritualist"),
    (50,  "Emissary"),
    (60,  "Archon"),
    (70,  "Oracle"),
    (80,  "Phantom"),
    (90,  "Ascendant"),
    (110, "Eternus"),
]

def rank_name_from_level(level: int) -> str:
    name = "Initiate"
    for lvl, title in RANK_TITLES:
        if level >= lvl:
            name = title
    return name

def xp_required_for_next_level(level: int) -> int:
    return XP_BASE + (level * XP_STEP)

def rank_image_from_level(level: int) -> str:
    # Ajustado a tus archivos
    if level >= 110: return "ranks/11_eternus.png"
    if level >= 90:  return "ranks/10_ascendant.png"
    if level >= 80:  return "ranks/09_phantom.png"
    if level >= 70:  return "ranks/08_oracle.png"
    if level >= 60:  return "ranks/07_archon.png"
    if level >= 50:  return "ranks/06_emissary.png"
    if level >= 40:  return "ranks/05_ritualist.png"
    if level >= 30:  return "ranks/04_arcanist.png"
    if level >= 20:  return "ranks/03_alchemist.png"
    if level >= 10:  return "ranks/02_seeker.png"
    return "ranks/01_initiate.png"

async def announce_rankup(member: discord.Member, new_level: int, prestige: int):
    if not RANKUP_CHANNEL_ID:
        return
    ch = member.guild.get_channel(RANKUP_CHANNEL_ID)
    if not ch:
        return
    try:
        title = rank_name_from_level(new_level)
        await ch.send(f"🔺 {member.mention} subió a **Lv {new_level}** ({title}) | Prestige **{prestige}**")
    except Exception:
        pass

def apply_xp_and_levelup(user_id: int, add_xp: int) -> Tuple[int, int, int, bool]:
    """
    Returns: (new_xp, new_level, prestige, leveled_up)
    """
    st = get_or_create_user(user_id)
    xp = int(st["xp"]) + int(add_xp)
    level = int(st["level"])
    prestige = int(st["prestige"])
    leveled_up = False

    while level < MAX_LEVEL:
        need = xp_required_for_next_level(level)
        if xp >= need:
            xp -= need
            level += 1
            leveled_up = True
        else:
            break

    update_user(user_id, xp=xp, level=level, prestige=prestige)
    return xp, level, prestige, leveled_up

# =========================
# GITHUB BACKUP / RESTORE
# =========================

def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

async def github_download_db_if_exists() -> bool:
    if not (GITHUB_TOKEN and GITHUB_REPO and GITHUB_BACKUP_PATH):
        return False

    import requests

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_PATH}"
    try:
        r = requests.get(url, headers=github_headers(), timeout=20)
        if r.status_code == 404:
            print("[backup] No backup found on GitHub (first run).")
            return False
        r.raise_for_status()
        data = r.json()
        content_b64 = data.get("content", "")
        if not content_b64:
            return False
        raw = base64.b64decode(content_b64)
        with open(DB_PATH, "wb") as f:
            f.write(raw)
        print("[backup] Restored data.db from GitHub.")
        return True
    except Exception as e:
        print(f"[backup] Restore failed: {e}")
        return False

async def github_upload_db() -> bool:
    if not (GITHUB_TOKEN and GITHUB_REPO and GITHUB_BACKUP_PATH):
        return False

    import requests

    if not os.path.exists(DB_PATH):
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_PATH}"
    try:
        sha = None
        get_r = requests.get(url, headers=github_headers(), timeout=20)
        if get_r.status_code == 200:
            sha = get_r.json().get("sha")
        elif get_r.status_code != 404:
            get_r.raise_for_status()

        with open(DB_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {"message": "Auto-backup data.db", "content": b64}
        if sha:
            payload["sha"] = sha

        put_r = requests.put(url, headers=github_headers(), data=json.dumps(payload), timeout=25)
        put_r.raise_for_status()
        print("[backup] Uploaded data.db to GitHub.")
        return True
    except Exception as e:
        print(f"[backup] Upload failed: {e}")
        return False

@tasks.loop(seconds=BACKUP_EVERY_SECONDS)
async def backup_loop():
    try:
        await github_upload_db()
    except Exception as e:
        print(f"[backup] Loop error: {e}")

# =========================
# VOICE XP LOOP
# =========================

def eligible_voice_member(m: discord.Member) -> bool:
    if m.bot:
        return False
    if not m.voice or not m.voice.channel:
        return False
    # opcional: no dar XP si está deaf
    if m.voice.self_deaf or m.voice.deaf:
        return False
    return True

@tasks.loop(seconds=VOICE_XP_EVERY_SECONDS)
async def voice_xp_loop():
    # IMPORTANTE: try por guild/miembro para que NO muera el task
    for guild in bot.guilds:
        try:
            for vc in guild.voice_channels:
                for member in vc.members:
                    try:
                        if not eligible_voice_member(member):
                            continue

                        st = get_or_create_user(member.id)
                        old_level = int(st["level"])
                        xp, level, prestige, leveled_up = apply_xp_and_levelup(member.id, VOICE_XP_PER_TICK)

                        if leveled_up and level != old_level:
                            await announce_rankup(member, level, prestige)
                    except Exception as e:
                        print(f"[voice] member error {member.id}: {e}")
        except Exception as e:
            print(f"[voice] guild error {guild.id}: {e}")

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    # 1) restore db si existe
    await github_download_db_if_exists()

    # 2) init + migraciones
    db_init()

    # 3) sync commands
    try:
        synced = await bot.tree.sync()
        print(f"[sync] Synced {len(synced)} commands.")
    except Exception as e:
        print(f"[sync] Failed: {e}")

    if not backup_loop.is_running():
        backup_loop.start()

    if not voice_xp_loop.is_running():
        voice_xp_loop.start()

    print(f"Bot listo como {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    st = get_or_create_user(user_id)
    now = time.time()

    last_ts = float(st["last_msg_ts"])
    if (now - last_ts) < MSG_COOLDOWN_SECONDS:
        return

    import random
    add = random.randint(MSG_XP_MIN, MSG_XP_MAX)
    update_user(user_id, last_msg_ts=now)

    old_level = int(st["level"])
    xp, level, prestige, leveled_up = apply_xp_and_levelup(user_id, add)
    if leveled_up and level != old_level:
        await announce_rankup(message.author, level, prestige)

# =========================
# SLASH COMMANDS
# =========================

def make_rank_embed(member: discord.Member, st: sqlite3.Row) -> discord.Embed:
    lvl = int(st["level"])
    xp = int(st["xp"])
    prestige = int(st["prestige"])
    rank = rank_name_from_level(lvl)
    need = xp_required_for_next_level(lvl) if lvl < MAX_LEVEL else 0

    desc = f"Prestige **{prestige}**\nLv **{lvl}**"
    if lvl < MAX_LEVEL:
        desc += f" • XP **{xp}/{need}**"
    else:
        desc += f" • XP **MAX**"

    embed = discord.Embed(
        title=f"{member.display_name} • {rank}",
        description=desc,
        color=discord.Color.purple()
    )
    embed.set_footer(text="Deadlock Chat Ranks")
    return embed

@bot.tree.command(name="rank", description="Muestra tu rango/nivel/xp")
@app_commands.describe(user="Ver el rank de otra persona (opcional)")
async def rank_slash(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    st = get_or_create_user(user.id)
    embed = make_rank_embed(user, st)

    img_path = rank_image_from_level(int(st["level"]))
    if os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        embed.set_thumbnail(url=f"attachment://{os.path.basename(img_path)}")
        await interaction.response.send_message(embed=embed, file=file, ephemeral=False)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="leaderboard", description="Top ranks del server")
async def leaderboard_slash(interaction: discord.Interaction):
    rows = top_users(10)
    lines = []
    for i, r in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(r["user_id"])) if interaction.guild else None
        name = member.mention if member else f"<@{int(r['user_id'])}>"
        lvl = int(r["level"])
        prestige = int(r["prestige"])
        title = rank_name_from_level(lvl)
        lines.append(f"{i}. {name} — P{prestige} • Lv{lvl} • {title}")

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(lines) if lines else "No hay datos aún.",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)

def is_admin(member: discord.Member) -> bool:
    # Admin = permiso Administrator (lo que tú pediste)
    return member.guild_permissions.administrator

@bot.tree.command(name="maxme", description="(Admin) Te pone en el nivel máximo")
async def maxme_slash(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("No pude validar permisos.", ephemeral=True)
        return

    if not is_admin(interaction.user):
        await interaction.response.send_message("⛔ Solo **admins** pueden usar este comando.", ephemeral=True)
        return

    update_user(interaction.user.id, level=MAX_LEVEL, xp=0)
    title = rank_name_from_level(MAX_LEVEL)
    await interaction.response.send_message(f"👑 Listo. Ahora eres **Lv {MAX_LEVEL}** ({title}).", ephemeral=True)

@bot.tree.command(name="backupnow", description="(Admin) Fuerza un backup ahora mismo")
async def backupnow_slash(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("⛔ Solo **admins** pueden usar esto.", ephemeral=True)
        return

    ok = await github_upload_db()
    await interaction.response.send_message(
        "✅ Backup hecho." if ok else "⚠️ No se pudo hacer el backup (revisa env vars/logs).",
        ephemeral=True
    )

# =========================
# STARTUP
# =========================

if not DISCORD_TOKEN:
    raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN")

bot.run(DISCORD_TOKEN)
