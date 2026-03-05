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
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# GitHub backup (Contents API)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")          # classic token con scope repo
GITHUB_REPO = os.getenv("GITHUB_REPO")            # ej: "spindashh/deadlock-rank-bot"
GITHUB_BACKUP_PATH = os.getenv("GITHUB_BACKUP_PATH", "backup/data.db")  # en tu repo
BACKUP_EVERY_SECONDS = int(os.getenv("BACKUP_EVERY_SECONDS", "300"))     # 5 min default

# Rankups channel
RANKUP_CHANNEL_ID = int(os.getenv("RANKUP_CHANNEL_ID", "0"))

# DB file
DB_PATH = os.getenv("DB_PATH", "data.db")

# XP tuning
MSG_XP_MIN = int(os.getenv("MSG_XP_MIN", "8"))
MSG_XP_MAX = int(os.getenv("MSG_XP_MAX", "16"))
MSG_COOLDOWN_SECONDS = int(os.getenv("MSG_COOLDOWN_SECONDS", "45"))

VOICE_XP_EVERY_SECONDS = int(os.getenv("VOICE_XP_EVERY_SECONDS", "180"))  # 3 min
VOICE_XP_PER_TICK = int(os.getenv("VOICE_XP_PER_TICK", "12"))

# Level curve
# XP needed for next level: base + (level * step)
XP_BASE = int(os.getenv("XP_BASE", "250"))
XP_STEP = int(os.getenv("XP_STEP", "60"))

# Max level (para /maxme)
MAX_LEVEL = int(os.getenv("MAX_LEVEL", "30"))

# =========================
# DISCORD SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

def dynamic_prefix(bot: commands.Bot, message: discord.Message):
    return "!"  # por si quieres comandos prefijo; los slash son los importantes

bot = commands.Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)

# =========================
# DB
# =========================

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_init():
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

RANK_TITLES = [
    (1,  "Initiate"),
    (3,  "Seeker"),
    (5,  "Alchemist"),
    (8,  "Arcanist"),
    (11, "Ritualist"),
    (15, "Emissary"),
    (20, "Archon"),
    (25, "Oracle"),
    (30, "Phantom"),
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
    # Asume que tienes ranks/01_initiate.png ... etc
    if level >= 30: return "ranks/09_phantom.png"
    if level >= 25: return "ranks/08_oracle.png"
    if level >= 20: return "ranks/07_archon.png"
    if level >= 15: return "ranks/06_emissary.png"
    if level >= 11: return "ranks/05_ritualist.png"
    if level >= 8:  return "ranks/04_arcanist.png"
    if level >= 5:  return "ranks/03_alchemist.png"
    if level >= 3:  return "ranks/02_seeker.png"
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
    """
    Download backup/data.db from GitHub if it exists, write to DB_PATH.
    Returns True if downloaded, False if not found or not configured.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO and GITHUB_BACKUP_PATH):
        return False

    import requests  # needs in requirements.txt

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
    """
    Upload local DB_PATH to GitHub at GITHUB_BACKUP_PATH.
    Returns True on success.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO and GITHUB_BACKUP_PATH):
        return False

    import requests  # needs in requirements.txt

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

        payload = {
            "message": "Auto-backup data.db",
            "content": b64
        }
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
    # ✅ NO dejar que un fallo de GitHub tumbe el bot y cause restart-loop
    try:
        await github_upload_db()
    except Exception as e:
        print("[backup] error:", e)

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    db_init()
    await github_download_db_if_exists()
    db_init()

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
    if message.author.bot:
        return
    if not message.guild:
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
# VOICE XP LOOP
# =========================

def eligible_voice_member(m: discord.Member) -> bool:
    if m.bot:
        return False
    if not m.voice:
        return False
    if not m.voice.channel:
        return False
    if m.voice.self_deaf or m.voice.deaf:
        return False
    return True

@tasks.loop(seconds=VOICE_XP_EVERY_SECONDS)
async def voice_xp_loop():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not eligible_voice_member(member):
                    continue

                st = get_or_create_user(member.id)
                old_level = int(st["level"])
                xp, level, prestige, leveled_up = apply_xp_and_levelup(member.id, VOICE_XP_PER_TICK)
                if leveled_up and level != old_level:
                    await announce_rankup(member, level, prestige)

# =========================
# SLASH COMMANDS
# =========================

def make_rank_embed(member: discord.Member, st: sqlite3.Row) -> discord.Embed:
    lvl = int(st["level"])
    xp = int(st["xp"])
    prestige = int(st["prestige"])
    rank = rank_name_from_level(lvl)
    need = xp_required_for_next_level(lvl)

    embed = discord.Embed(
        title=f"{member.display_name} • {rank}",
        description=f"Prestige **{prestige}**\nLv **{lvl}** • XP **{xp}/{need}**",
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
    # ✅ admins (permiso administrador)
    return member.guild_permissions.administrator

@bot.tree.command(name="maxme", description="(Admin) Te pone en el nivel máximo")
async def maxme_slash(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("No pude validar tus permisos.", ephemeral=True)
        return

    if not is_admin(interaction.user):
        await interaction.response.send_message("⛔ Solo un **Admin** puede usar este comando.", ephemeral=True)
        return

    update_user(interaction.user.id, level=MAX_LEVEL, xp=0)
    title = rank_name_from_level(MAX_LEVEL)
    await interaction.response.send_message(
        f"👑 Listo. Ahora eres **Lv {MAX_LEVEL}** ({title}).",
        ephemeral=True
    )

@bot.tree.command(name="backupnow", description="(Admin) Fuerza un backup ahora mismo")
async def backupnow_slash(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("⛔ Solo un **Admin** puede usar esto.", ephemeral=True)
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
