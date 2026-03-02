import os
import time
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional, List, Tuple

import discord
from discord import app_commands

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")

XP_COOLDOWN_SECONDS = 50
XP_MIN = 12
XP_MAX = 22
MIN_MESSAGE_LENGTH = 10

@dataclass
class Rank:
    name: str
    image_path: str

RANKS: List[Rank] = [
    Rank("Initiate",   "ranks/01_initiate.png"),
    Rank("Seeker",     "ranks/02_seeker.png"),
    Rank("Alchemist",  "ranks/03_alchemist.png"),
    Rank("Arcanist",   "ranks/04_arcanist.png"),
    Rank("Ritualist",  "ranks/05_ritualist.png"),
    Rank("Emissary",   "ranks/06_emissary.png"),
    Rank("Archon",     "ranks/07_archon.png"),
    Rank("Oracle",     "ranks/08_oracle.png"),
    Rank("Phantom",    "ranks/09_phantom.png"),
    Rank("Ascendant",  "ranks/10_ascendant.png"),
    Rank("Eternus",    "ranks/11_eternus.png"),
]

SIGIL_NAMES = {
    1: "Mark of the Veil",
    2: "Echo of the Archons",
    3: "Oath of the Ritual",
    4: "Flame of the Oracle",
    5: "Crown of the Phantom",
    6: "Eternal Covenant",
}

def get_sigil_name(prestige: int) -> str:
    if prestige in SIGIL_NAMES:
        return f"Sigil {prestige} — {SIGIL_NAMES[prestige]}"
    return f"Sigil {prestige}"

def clamp_rank_index(level: int) -> int:
    return max(0, min(level, len(RANKS) - 1))

def progress_bar(current: int, total: int, size: int = 18) -> str:
    if total <= 0:
        return "░" * size
    ratio = max(0.0, min(1.0, current / total))
    filled = int(ratio * size)
    return "█" * filled + "░" * (size - filled)

def xp_needed_for_next_level(level: int, prestige: int) -> int:
    base = 180 + (level * 110)
    mult = 1.0 + (prestige * 0.18)
    return int(base * mult)

# =========================
# DATABASE
# =========================

DB_PATH = "levels.db"

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 0,
            prestige INTEGER NOT NULL DEFAULT 0,
            last_xp_ts INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    con.close()

def get_user(user_id: int) -> Tuple[int, int, int, int]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT xp, level, prestige, last_xp_ts FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users(user_id, xp, level, prestige, last_xp_ts) VALUES (?, 0, 0, 0, 0)",
            (user_id,)
        )
        con.commit()
        con.close()
        return (0, 0, 0, 0)
    con.close()
    return row

def set_user(user_id: int, xp: int, level: int, prestige: int, last_ts: int):
    con = db()
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET xp=?, level=?, prestige=?, last_xp_ts=? WHERE user_id=?",
        (xp, level, prestige, last_ts, user_id)
    )
    con.commit()
    con.close()

def top_users(limit: int = 10):
    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT user_id, prestige, level, xp FROM users ORDER BY prestige DESC, level DESC, xp DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    con.close()
    return rows

# =========================
# DISCORD
# =========================

intents = discord.Intents.default()
intents.message_content = True

class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = Client()

async def announce_rankup(member, level, prestige, channel):
    idx = clamp_rank_index(level)
    rank = RANKS[idx]

    embed = discord.Embed(
        title="⚜️ RANK UP ⚜️",
        description=f"{member.mention} ascendió a **{rank.name.upper()}**"
    )

    if os.path.exists(rank.image_path):
        file = discord.File(rank.image_path)
        embed.set_image(url=f"attachment://{os.path.basename(rank.image_path)}")
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)

async def announce_prestige(member, prestige, channel):
    embed = discord.Embed(
        title="✦ ASCENSION COMPLETE ✦",
        description=(
            f"{member.mention} trascendió el plano mortal.\n\n"
            f"Nuevo estado: **{get_sigil_name(prestige)}**\n"
            f"El ciclo comienza nuevamente en **INITIATE**."
        )
    )

    eternus_img = RANKS[-1].image_path
    if os.path.exists(eternus_img):
        file = discord.File(eternus_img)
        embed.set_image(url=f"attachment://{os.path.basename(eternus_img)}")
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)

@client.event
async def on_ready():
    init_db()
    print(f"Bot listo como {client.user}")

@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if len(message.content.strip()) < MIN_MESSAGE_LENGTH:
        return

    xp, level, prestige, last_ts = get_user(message.author.id)

    now = int(time.time())
    if now - last_ts < XP_COOLDOWN_SECONDS:
        return

    xp += random.randint(XP_MIN, XP_MAX)
    last_ts = now

    max_level = len(RANKS) - 1
    leveled = False
    prestiged = False

    while xp >= xp_needed_for_next_level(level, prestige):
        xp -= xp_needed_for_next_level(level, prestige)

        if level >= max_level:
            prestige += 1
            level = 0
            xp = 0
            prestiged = True
            break
        else:
            level += 1
            leveled = True

    set_user(message.author.id, xp, level, prestige, last_ts)

    if prestiged:
        await announce_prestige(message.author, prestige, message.channel)
    elif leveled:
        await announce_rankup(message.author, level, prestige, message.channel)

@client.tree.command(name="rank", description="Muestra tu rango")
async def rank_cmd(interaction: discord.Interaction):
    xp, level, prestige, _ = get_user(interaction.user.id)
    rank = RANKS[clamp_rank_index(level)]
    needed = xp_needed_for_next_level(level, prestige)
    bar = progress_bar(xp, needed)

    embed = discord.Embed(
        title=f"🏅 {interaction.user.display_name}",
        description=(
            f"**Rango:** {rank.name}\n"
            f"**Sigil:** {get_sigil_name(prestige) if prestige > 0 else '—'}\n"
            f"**XP:** {xp}/{needed}\n"
            f"`{bar}`"
        )
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="top", description="Leaderboard")
async def top_cmd(interaction: discord.Interaction):
    rows = top_users()
    text = ""

    for i, (uid, pres, lvl, xp) in enumerate(rows, 1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else "User"
        text += f"{i}. {name} — {RANKS[clamp_rank_index(lvl)].name}\n"

    embed = discord.Embed(title="🏆 Leaderboard", description=text or "Sin datos aún.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

client.run(TOKEN)
