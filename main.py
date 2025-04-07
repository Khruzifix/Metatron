from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file
import os
import sys
import asyncio
import discord
from discord.ext import commands, tasks
import requests
from lxml import html
import time
import logging
from functools import lru_cache
import sqlite3
import re
import json
from typing import Dict, List, Tuple, Optional
import requests_cache
import psutil

# --------------------------
# Keep-Alive Server (MUST BE FIRST)
# --------------------------
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()


# --------------------------
# Configuration & Constants
# --------------------------
TOKEN = os.getenv('DISCORD_TOKEN')  # Get token from environment variables
DB_FILE = "guild_tracker.db"  # SQLite database file
CACHE_NAME = "aqw_cache"  # Requests cache name

# Formatting
EMBED_COLOR = 0x5865F2
MEMBERS_PER_FIELD = 15
MAX_FIELDS_PER_EMBED = 3

# Rate limiting
REQUEST_DELAY = 3  # 3s between checks (AQW can handle this if cache is working)
MAX_RETRIES = 1  # Faster failover (rely on next cycle)
RETRY_DELAY = 5  # Shorter retry window

# Task configuration
CHECK_LIMIT = 15  # Members per cycle
TASK_INTERVAL = 2  # minutes

# --------------------------
# Logging Setup
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'),
              logging.StreamHandler()])
logger = logging.getLogger(__name__)


# --------------------------
# Database Setup
# --------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS servers
                 (server_id TEXT PRIMARY KEY,
                  admin_role_id TEXT,
                  list_channel_id TEXT,
                  log_channel_id TEXT,
                  guild_name TEXT,
                  autoremove BOOLEAN)''')

    c.execute('''CREATE TABLE IF NOT EXISTS members
                 (server_id TEXT,
                  name TEXT,
                  normalized_name TEXT,
                  status TEXT DEFAULT '',
                  last_checked INTEGER,
                  PRIMARY KEY (server_id, normalized_name))''')

    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (server_id TEXT,
                  message_id TEXT,
                  embed_index INTEGER,
                  PRIMARY KEY (server_id, message_id))''')

    conn.commit()
    conn.close()


init_db()
requests_cache.install_cache(CACHE_NAME, expire_after=300)

# --------------------------
# Bot Initialization
# --------------------------
keep_alive()
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='-',
                   intents=intents,
                   help_command=None,
                   case_insensitive=True)


# --------------------------
# Data Access Layer
# --------------------------
class Database:

    @staticmethod
    def get_server_config(server_id: str) -> Optional[Dict]:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM servers WHERE server_id = ?', (server_id, ))
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        return {
            "admin_role_id": row[1],
            "list_channel_id": row[2],
            "log_channel_id": row[3],
            "guild_name": row[4],
            "autoremove": bool(row[5])
        }

    @staticmethod
    def save_server_config(server_id: str, config: Dict) -> None:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''INSERT OR REPLACE INTO servers 
                     VALUES (?, ?, ?, ?, ?, ?)''',
            (server_id, config.get("admin_role_id"),
             config.get("list_channel_id"), config.get("log_channel_id"),
             config.get("guild_name"), config.get("autoremove", False)))
        conn.commit()
        conn.close()

    @staticmethod
    def get_members(server_id: str) -> List[Tuple[str, str]]:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''SELECT name, status FROM members 
                     WHERE server_id = ?
                     ORDER BY name COLLATE NOCASE''', (server_id, ))
        members = c.fetchall()
        conn.close()
        return members

    @staticmethod
    def add_member(server_id: str, name: str) -> None:
        normalized = name.lower()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''INSERT OR IGNORE INTO members 
                     VALUES (?, ?, ?, ?, ?)''',
            (server_id, name, normalized, '', int(time.time())))
        conn.commit()
        conn.close()

    @staticmethod
    def remove_member(server_id: str, name: str) -> None:
        normalized = name.lower()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''DELETE FROM members 
                     WHERE server_id = ? AND normalized_name = ?''',
            (server_id, normalized))
        conn.commit()
        conn.close()

    @staticmethod
    def member_exists(server_id: str, name: str) -> bool:
        normalized = name.lower()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''SELECT 1 FROM members 
                     WHERE server_id = ? AND normalized_name = ?''',
            (server_id, normalized))
        exists = c.fetchone() is not None
        conn.close()
        return exists

    @staticmethod
    def save_message_ids(server_id: str, message_ids: List[int]) -> None:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Clear existing messages
        c.execute('DELETE FROM messages WHERE server_id = ?', (server_id, ))

        # Insert new ones
        for idx, msg_id in enumerate(message_ids):
            c.execute(
                '''INSERT INTO messages 
                         VALUES (?, ?, ?)''', (server_id, str(msg_id), idx))

        conn.commit()
        conn.close()

    @staticmethod
    def get_message_ids(server_id: str) -> List[int]:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''SELECT message_id FROM messages 
                     WHERE server_id = ?
                     ORDER BY embed_index''', (server_id, ))
        rows = c.fetchall()
        conn.close()
        return [int(row[0]) for row in rows] if rows else []

    @staticmethod
    def update_last_checked(server_id: str, name: str) -> None:
        normalized = name.lower()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''UPDATE members 
                     SET last_checked = ?
                     WHERE server_id = ? AND normalized_name = ?''',
            (int(time.time()), server_id, normalized))
        conn.commit()
        conn.close()


# --------------------------
# AQW Utilities
# --------------------------
class AQWUtils:

    @staticmethod
    @lru_cache(maxsize=1000)
    def check_aqw_guild(name: str, aqw_guild_name: str) -> bool:
        headers = {
            'User-Agent':
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        for attempt in range(MAX_RETRIES):
            try:
                url = f"https://account.aq.com/CharPage?id={name}"
                response = requests.get(url, headers=headers, timeout=15)
                response.raise_for_status()

                tree = html.fromstring(response.content)
                guild_element = tree.xpath(
                    '//div[@style="line-height: 85%"]/label[contains(text(),"Guild:")]/following-sibling::text()[1]'
                )
                return guild_element and guild_element[0].strip().lower(
                ) == aqw_guild_name.lower()

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    logger.warning(
                        f"Rate limited, retrying in {RETRY_DELAY} seconds... (Attempt {attempt+1}/{MAX_RETRIES})"
                    )
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                logger.error(f"Check error for {name}: {e}")
                return False
            except Exception as e:
                logger.error(f"Check error for {name}: {e}")
                return False
   
    @staticmethod
    @lru_cache(maxsize=100)
    def get_character_id(name: str) -> Optional[int]:
        """Get AQW character ID from name by checking page source"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.5'
        }

        try:
            response = requests.get(
                f"https://account.aq.com/CharPage?id={name}",
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            content = response.text

            # Directly target the ccid variable we found
            ccid_match = re.search(r'var ccid\s*=\s*(\d+);', content)
            if ccid_match:
                return int(ccid_match.group(1))

            # Fallback to other methods if needed
            json_match = re.search(r'<script id="jsonData" type="application/json">(.*?)</script>', content, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    return int(data.get('CharID'))
                except (json.JSONDecodeError, AttributeError, ValueError):
                    pass

            logger.warning(f"No ID found for {name} in page source")
            return None

        except Exception as e:
            logger.error(f"ID lookup failed for {name}: {str(e)}")
            return None
        
# --------------------------
# Discord Utilities
# --------------------------
class DiscordUtils:

    @staticmethod
    def create_list_embeds(
            guild_name: str, members: List[Tuple[str,
                                                 str]]) -> List[discord.Embed]:
        embeds = []
        members_per_embed = MEMBERS_PER_FIELD * MAX_FIELDS_PER_EMBED
        chunks = [
            members[i:i + members_per_embed]
            for i in range(0, len(members), members_per_embed)
        ]

        for chunk_idx, chunk in enumerate(chunks):
            embed = discord.Embed(title=f"⚔️ {guild_name.upper()} MEMBER LIST",
                                  color=EMBED_COLOR)
            embed.set_footer(text=f"Page {chunk_idx+1} of {len(chunks)}")

            column_size = len(chunk) // MAX_FIELDS_PER_EMBED + 1
            for i in range(MAX_FIELDS_PER_EMBED):
                start = i * column_size
                end = start + column_size
                field_members = chunk[start:end]

                if not field_members:
                    continue

                value = "\n".join(
                    f"`{(j+1)+(i*column_size)+(chunk_idx*members_per_embed):>3}` {name}"
                    for j, (name, _) in enumerate(field_members))

                embed.add_field(name=f"Group {i+1}", value=value, inline=True)

            embeds.append(embed)

        return embeds

    @staticmethod
    async def update_list(bot: commands.Bot, server_id: str) -> None:
        try:
            config = Database.get_server_config(server_id)
            if not config:
                return

            members = Database.get_members(server_id)
            channel = bot.get_channel(int(config["list_channel_id"]))
            if not channel:
                return

            embeds = DiscordUtils.create_list_embeds(config["guild_name"],
                                                     members)
            existing_message_ids = Database.get_message_ids(server_id)
            new_message_ids = []

            # Process messages in order (page 1 first)
            for idx, embed in enumerate(embeds):
                try:
                    if idx > 0:  # Add delay between edits
                        await asyncio.sleep(2.5)  # 2.5s between embed updates
                    # Try to edit existing messages first
                    if idx < len(existing_message_ids):
                        msg = await channel.fetch_message(
                            existing_message_ids[idx])
                        await msg.edit(embed=embed)
                        new_message_ids.append(msg.id)
                    else:
                        # If no existing message, create new one at bottom
                        msg = await channel.send(embed=embed)
                        new_message_ids.append(msg.id)
                except discord.NotFound:
                    # Message was deleted, create new one
                    msg = await channel.send(embed=embed)
                    new_message_ids.append(msg.id)
                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limited
                        retry_after = e.retry_after or 5
                        logger.warning(
                            f"Rate limited editing embed {idx+1}/{len(embeds)}. Retrying in {retry_after}s"
                        )
                        await asyncio.sleep(retry_after + 1)
                        continue  # Retry this specific embed
                    logger.error(f"Error editing message: {e}")

            # Delete excess messages from the bottom up
            for msg_id in existing_message_ids[len(embeds):]:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limited
                        retry_after = e.retry_after or 5
                        logger.warning(
                            f"Rate limited deleting message {msg_id}. Retrying in {retry_after}s"
                        )
                        await asyncio.sleep(retry_after + 1)
                        continue  # Retry this deletion
                    logger.error(f"Error deleting message: {e}")

            # Save the new message IDs
            Database.save_message_ids(server_id, new_message_ids)

        except Exception as e:
            logger.error(f"Update error in {server_id}: {e}")

    @staticmethod
    async def log_action(bot: commands.Bot, server_id: str,
                         action: str) -> None:
        try:
            config = Database.get_server_config(server_id)
            if not config or not config.get("log_channel_id"):
                return

            channel = bot.get_channel(int(config["log_channel_id"]))
            if not channel:
                logger.warning(f"Log channel not found in server {server_id}")
                return

            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.send_messages:
                logger.warning(
                    f"Missing Send Messages permission in log channel {channel.id}"
                )
                return

            timestamp = time.strftime('%Y-%m-%d %H:%M')
            try:
                await channel.send(f"`{timestamp}` {action}")
            except discord.Forbidden:
                logger.warning(
                    f"Missing access to log channel {channel.id} in server {server_id}"
                )
            except Exception as e:
                logger.error(f"Logging error in server {server_id}: {str(e)}")
        except Exception as e:
            logger.error(
                f"Logging system error in server {server_id}: {str(e)}")


# --------------------------
# Command Checks
# --------------------------
def in_list_channel():

    async def predicate(ctx):
        server_id = str(ctx.guild.id)
        config = Database.get_server_config(server_id)
        if not config or ctx.channel.id != int(config["list_channel_id"]):
            try:
                await ctx.message.delete()
            except:
                pass
            return False
        return True

    return commands.check(predicate)


def is_admin():

    async def predicate(ctx):
        server_id = str(ctx.guild.id)
        config = Database.get_server_config(server_id)
        if not config:
            return False

        if ctx.author.guild_permissions.administrator:
            return True

        admin_role_id = config.get("admin_role_id")
        if admin_role_id:
            role = discord.utils.get(ctx.author.roles, id=int(admin_role_id))
            if role:
                return True

        await ctx.send(
            "❌ You need the admin role or permissions to use this command!",
            delete_after=5)
        return False

    return commands.check(predicate)


# --------------------------
# Bot Commands
# --------------------------
@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Guild Tracker Bot Commands",
                          description="Manage your AQW guild member list",
                          color=EMBED_COLOR)

    commands_list = {
        "-setup": "Initialize bot (Admin only)",
        "-settings": "Show current configuration",
        "-changelist #channel": "Change list channel",
        "-changelog #channel": "Change log channel",
        "-changeguild Name": "Update guild name",
        "-autoremove [on/off]": "Toggle auto-removal",
        "-add [names]": "Add multiple members",
        "-remove [name]": "Remove a member",
        "-refresh": "Force update all statuses",
        "-help": "Show this help"
    }

    for cmd, desc in commands_list.items():
        embed.add_field(name=cmd, value=desc, inline=False)

    await ctx.send(embed=embed, delete_after=30)


@bot.command()
async def settings(ctx):
    server_id = str(ctx.guild.id)
    config = Database.get_server_config(server_id)
    if not config:
        return await ctx.send("❌ Server not configured! Use `-setup` first.",
                              delete_after=5)

    embed = discord.Embed(title="⚙️ Server Settings", color=EMBED_COLOR)

    admin_role = ctx.guild.get_role(int(
        config["admin_role_id"])) if config["admin_role_id"] else None
    list_channel = bot.get_channel(int(config["list_channel_id"]))
    log_channel = bot.get_channel(int(config["log_channel_id"]))

    embed.add_field(name="Admin Role",
                    value=admin_role.mention if admin_role else "Not set",
                    inline=False)
    embed.add_field(name="Guild Name",
                    value=config["guild_name"],
                    inline=False)
    embed.add_field(
        name="List Channel",
        value=list_channel.mention if list_channel else "Not found",
        inline=True)
    embed.add_field(name="Log Channel",
                    value=log_channel.mention if log_channel else "Not found",
                    inline=True)
    embed.add_field(name="Auto-Remove",
                    value="🔴 ON" if config["autoremove"] else "🟢 OFF",
                    inline=False)

    await ctx.send(embed=embed, delete_after=30)


@bot.command()
@is_admin()
async def changelist(ctx, channel: discord.TextChannel):
    server_id = str(ctx.guild.id)
    config = Database.get_server_config(server_id)
    if not config:
        return await ctx.send("❌ Server not configured! Use `-setup` first.",
                              delete_after=5)

    config["list_channel_id"] = str(channel.id)
    Database.save_server_config(server_id, config)
    await DiscordUtils.update_list(bot, server_id)
    await DiscordUtils.log_action(
        bot, server_id, f"📝 List channel changed to #{channel.name}")
    await ctx.send(f"✅ List channel updated to {channel.mention}",
                   delete_after=10)


@bot.command()
@is_admin()
async def changelog(ctx, channel: discord.TextChannel):
    server_id = str(ctx.guild.id)
    config = Database.get_server_config(server_id)
    if not config:
        return await ctx.send("❌ Server not configured! Use `-setup` first.",
                              delete_after=5)

    config["log_channel_id"] = str(channel.id)
    Database.save_server_config(server_id, config)
    await DiscordUtils.log_action(bot, server_id,
                                  f"📝 Log channel changed to #{channel.name}")
    await ctx.send(f"✅ Log channel updated to {channel.mention}",
                   delete_after=10)


@bot.command()
@is_admin()
async def changeguild(ctx, *, new_name: str):
    server_id = str(ctx.guild.id)
    config = Database.get_server_config(server_id)
    if not config:
        return await ctx.send("❌ Server not configured! Use `-setup` first.",
                              delete_after=5)

    config["guild_name"] = new_name
    config["autoremove"] = False
    Database.save_server_config(server_id, config)
    await DiscordUtils.update_list(bot, server_id)
    await DiscordUtils.log_action(bot, server_id,
                                  f"🏰 Guild name changed to {new_name}")
    await ctx.send(
        f"✅ Guild name updated to `{new_name}`. Auto-remove disabled for safety.",
        delete_after=15)


@bot.command()
@in_list_channel()
@commands.has_permissions(administrator=False)
async def autoremove(ctx, mode: str):
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    server_id = str(ctx.guild.id)
    mode = mode.lower()

    if mode not in ["on", "off"]:
        return

    config = Database.get_server_config(server_id)
    if not config:
        return

    config["autoremove"] = (mode == "on")
    Database.save_server_config(server_id, config)

    status = "🔴 AUTO-REMOVE: ON" if config[
        "autoremove"] else "🟢 AUTO-REMOVE: OFF"
    try:
        msg = await ctx.send(f"{status}\nApplies to all operations.",
                             delete_after=5)
        await asyncio.sleep(5)
        await msg.delete()
    except discord.NotFound:
        pass
    await DiscordUtils.log_action(bot, server_id,
                                  f"⚙️ Auto-remove set to {mode.upper()}")


@tasks.loop(minutes=TASK_INTERVAL)
async def live_guild_check():
    start_time = time.time()
    total_checked = 0
    successful_checks = 0

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT server_id FROM servers WHERE autoremove = 1')

        for server_id in [row[0] for row in c.fetchall()]:
            config = Database.get_server_config(server_id)
            if not config:
                continue

            # Get members due for checking
            c.execute(
                '''SELECT name FROM members 
                         WHERE server_id = ?
                         ORDER BY last_checked ASC
                         LIMIT ?''', (server_id, CHECK_LIMIT))

            for name, in c.fetchall():
                try:
                    # Throttle check to avoid Discord rate limits
                    if total_checked % 5 == 0 and total_checked > 0:
                        await asyncio.sleep(REQUEST_DELAY * 2
                                            )  # Extra breathing room

                    in_guild = AQWUtils.check_aqw_guild(
                        name, config["guild_name"])
                    total_checked += 1
                    successful_checks += 1

                    if not in_guild and config["autoremove"]:
                        Database.remove_member(server_id, name)
                        await DiscordUtils.log_action(
                            bot, server_id, f"🚫 Auto-removed {name}")
                    else:
                        Database.update_last_checked(server_id, name)

                    # Natural pacing
                    await asyncio.sleep(REQUEST_DELAY)

                except Exception as e:
                    logger.warning(
                        f"Check paused for {RETRY_DELAY}s (Error on {name}: {str(e)})"
                    )
                    await asyncio.sleep(RETRY_DELAY)

        # Update member list embeds in batches
        if successful_checks > 0:
            await DiscordUtils.update_list(bot, server_id)

        logger.info(
            f"Processed {total_checked} members ({successful_checks} successful) "
            f"in {time.time()-start_time:.1f}s")

    except Exception as e:
        logger.error(f"Check cycle failed: {str(e)}")
    finally:
        conn.close()


@bot.command()
@in_list_channel()
async def add(ctx, *, names: str):
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    server_id = str(ctx.guild.id)
    names = [name.strip() for name in names.split(",") if name.strip()]

    if not names:
        return

    msg = await ctx.send(f"🔍 Processing {len(names)} names...",
                         delete_after=10)
    results = []
    config = Database.get_server_config(server_id)

    if not config:
        return await msg.edit(content="❌ Server not configured!",
                              delete_after=5)

    for name in names:
        try:
            in_guild = AQWUtils.check_aqw_guild(name, config["guild_name"])

            if in_guild:
                if not Database.member_exists(server_id, name):
                    Database.add_member(server_id, name)
                    results.append(f"✅ {name}")
                    await DiscordUtils.log_action(bot, server_id,
                                                  f"➕ Added {name}")
                else:
                    results.append(f"⚠️ {name} (already in list)")
                    await DiscordUtils.log_action(
                        bot, server_id, f"⚠️ Duplicate entry {name}")
            elif config["autoremove"] and Database.member_exists(
                    server_id, name):
                Database.remove_member(server_id, name)
                results.append(f"🚫 {name} (removed)")
                await DiscordUtils.log_action(bot, server_id,
                                              f"🚫 Auto-removed {name}")
            else:
                results.append(f"⚠️ {name} (non-member)")
                await DiscordUtils.log_action(bot, server_id,
                                              f"⚠️ Added non-member {name}")

            await asyncio.sleep(REQUEST_DELAY)
        except Exception as e:
            results.append(f"❌ {name} (error)")
            await DiscordUtils.log_action(bot, server_id,
                                          f"❌ Error adding {name}: {str(e)}")

    response = "Results:\n" + "\n".join(results[:15])
    if len(results) > 15:
        response += f"\n...and {len(results)-15} more"

    try:
        await msg.edit(content=response, delete_after=15)
    except discord.NotFound:
        pass

    await DiscordUtils.update_list(bot, server_id)


@bot.command()
@in_list_channel()
async def remove(ctx, *, name: str):
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    server_id = str(ctx.guild.id)
    msg = await ctx.send(f"⏳ Removing {name}...", delete_after=3)

    try:
        if Database.member_exists(server_id, name):
            Database.remove_member(server_id, name)
            await DiscordUtils.update_list(bot, server_id)
            await DiscordUtils.log_action(bot, server_id, f"➖ Removed {name}")
            await msg.edit(content=f"✅ Removed {name}", delete_after=3)
        else:
            await msg.edit(content="❌ Member not found!", delete_after=3)
    except Exception as e:
        await msg.edit(content=f"❌ Error: {str(e)}", delete_after=5)
        await DiscordUtils.log_action(bot, server_id,
                                      f"❌ Error removing {name}: {str(e)}")


# --------------------------
# New ID Command
# --------------------------
@bot.command()
@commands.cooldown(1, 5, commands.BucketType.user)
async def id(ctx, *, character_name: str):
    """Get AQW character ID"""
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    async with ctx.typing():
        char_id = AQWUtils.get_character_id(character_name.strip())

        if char_id:
            await ctx.send(
                f"🆔 **{character_name}**'s Character ID: `{char_id}`",  # Removed the profile link
                delete_after=25
            )
            await DiscordUtils.log_action(
                bot, 
                str(ctx.guild.id), 
                f"ID lookup: {character_name} → {char_id}"
            )
        else:
            await ctx.send(
                f"❌ Could not find ID for {character_name}", 
                delete_after=10
            )

@bot.command()
@in_list_channel()
async def refresh(ctx):
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    server_id = str(ctx.guild.id)
    msg = await ctx.send("🔄 Force refreshing...", delete_after=3)

    try:
        config = Database.get_server_config(server_id)
        if not config:
            return await msg.edit(content="❌ Server not configured!",
                                  delete_after=5)

        changed = False
        removed_count = 0
        marked_count = 0
        members = Database.get_members(server_id)

        for name, status in members:
            in_guild = AQWUtils.check_aqw_guild(name, config["guild_name"])

            if not in_guild and config["autoremove"]:
                Database.remove_member(server_id, name)
                changed = True
                removed_count += 1
                await DiscordUtils.log_action(bot, server_id,
                                              f"🚫 Auto-removed {name}")
            elif not in_guild:
                changed = True
                marked_count += 1
                await DiscordUtils.log_action(bot, server_id,
                                              f"⚠️ {name} status updated")

            await asyncio.sleep(REQUEST_DELAY)

        if changed:
            await DiscordUtils.update_list(bot, server_id)
            log_msg = []
            if removed_count > 0:
                log_msg.append(f"♻️ Removed {removed_count} members")
            if marked_count > 0:
                log_msg.append(f"⚠️ Marked {marked_count} non-members")

            await DiscordUtils.log_action(bot, server_id, "\n".join(log_msg))
            await msg.edit(content="✅ Refresh complete with changes!",
                           delete_after=5)
        else:
            await msg.edit(content="✅ All statuses current!", delete_after=5)

    except Exception as e:
        await msg.edit(content=f"❌ Error: {str(e)}", delete_after=5)
        await DiscordUtils.log_action(bot, server_id,
                                      f"❌ Refresh failed: {str(e)}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass

    server_id = str(ctx.guild.id)

    try:
        steps = [
            "🔒 Mention the **admin role** for bot management:",
            "🏰 Enter your exact AQW guild name:",
            "📝 Mention the **list channel** where the member list will appear:",
            "📋 Mention the **log channel** for action tracking:"
        ]

        msg = await ctx.send(steps[0])

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        # Admin Role
        response = await bot.wait_for('message', check=check, timeout=60)
        if not response.role_mentions:
            return await msg.edit(content="❌ No role mentioned!",
                                  delete_after=5)
        admin_role = response.role_mentions[0]
        await response.delete()

        # Guild Name
        await msg.edit(
            content=f"✅ Admin role: {admin_role.mention}\n{steps[1]}")
        response = await bot.wait_for('message', check=check, timeout=60)
        aqw_guild_name = response.content.strip()
        await response.delete()

        # List Channel
        await msg.edit(content=f"✅ Guild name: {aqw_guild_name}\n{steps[2]}")
        response = await bot.wait_for('message', check=check, timeout=60)
        if not response.channel_mentions:
            return await msg.edit(content="❌ No channel mentioned!",
                                  delete_after=5)
        list_channel = response.channel_mentions[0]
        await response.delete()

        # Log Channel
        await msg.edit(
            content=f"✅ List channel: {list_channel.mention}\n{steps[3]}")
        response = await bot.wait_for('message', check=check, timeout=60)
        if not response.channel_mentions:
            return await msg.edit(content="❌ No channel mentioned!",
                                  delete_after=5)
        log_channel = response.channel_mentions[0]
        await response.delete()

        config = {
            "admin_role_id": str(admin_role.id),
            "guild_name": aqw_guild_name,
            "list_channel_id": str(list_channel.id),
            "log_channel_id": str(log_channel.id),
            "autoremove": False
        }

        Database.save_server_config(server_id, config)

        await DiscordUtils.update_list(bot, server_id)
        await DiscordUtils.log_action(bot, server_id, "⚙️ Bot initialized")
        final_msg = f"""✅ Setup complete!
• Admin role: {admin_role.mention}
• Guild: {aqw_guild_name}
• List channel: {list_channel.mention}
• Log channel: {log_channel.mention}
Use `-autoremove on` to enable auto-cleanup"""
        await msg.edit(content=final_msg, delete_after=15)
    except asyncio.TimeoutError:
        await msg.edit(content="⌛ Setup timed out", delete_after=5)

# ======================
# Maintenance Commands
# ======================
@bot.command()
@is_admin()
async def restart(ctx):
    """Hot-reload the bot without downtime"""
    await ctx.send("🔄 Rebooting bot...")
    os.execv(sys.executable, ['python'] + sys.argv)


@bot.command()
@is_admin()
async def logs(ctx, lines: int = 50):
    """Get recent logs
    Usage: -logs [number of lines]
    """
    try:
        with open('bot.log', 'r') as f:
            log_lines = f.readlines()[-lines:]
        await ctx.send(f"```\n{''.join(log_lines)}\n```", delete_after=120)
    except Exception as e:
        await ctx.send(f"❌ Error reading logs: {e}", delete_after=10)


# ======================
# Monitoring Commands
# ======================
@bot.command()
async def status(ctx):
    """Show real-time bot health stats"""
    try:
        conn = sqlite3.connect(DB_FILE)
        members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        servers = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
        conn.close()

        embed = discord.Embed(title="🤖 Bot Status Dashboard", color=0x00ff00)

        # System Stats
        embed.add_field(
            name="System",
            value=f"CPU: {psutil.cpu_percent()}%\n"
            f"RAM: {psutil.virtual_memory().percent}% used\n"
            f"Uptime: {time.time() - psutil.Process().create_time():.0f}s")

        # Database Stats
        embed.add_field(
            name="Database",
            value=f"Servers: {servers}\n"
            f"Members: {members}\n"
            f"Last backup: {max(os.listdir('backups'), default='Never')}")

        # Bot Performance
        embed.add_field(
            name="Performance",
            value=f"Check rate: {CHECK_LIMIT}/{TASK_INTERVAL}min\n"
            f"API delay: {REQUEST_DELAY}s\n"
            f"Last cycle: {getattr(live_guild_check, 'last_run', 'Never')}")

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ Status error: {str(e)}", delete_after=10)

        
# --------------------------
# Event Handlers
# --------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, discord.NotFound)):
        return
    logger.error(f"Command error from {ctx.author}: {str(error)}")


# ======================
# Auto-Backup Setup (KEEP ONLY ONE)
# ======================
@tasks.loop(hours=24)
async def daily_backup():
    import subprocess
    try:
        subprocess.run(["python", "backup.py"], check=True)
    except Exception as e:
        logger.error(f"Backup failed: {str(e)}")

# ======================
# Auto-Restart System (KEEP ONLY ONE)
# ======================
async def restart_check():
    """Checks every hour if bot is frozen"""
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(3600)
        if not bot.is_ready():
            logger.critical("Bot frozen - restarting!")
            os.execv(sys.executable, ['python'] + sys.argv)

@bot.event
async def on_ready():
    """MODIFIED VERSION - combines old and new"""
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    logger.info("🔄 Auto-restart system activated (hourly checks)")
    daily_backup.start()
    live_guild_check.start()
    bot.loop.create_task(restart_check())

    # Original server initialization
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT server_id FROM servers')
    server_ids = [row[0] for row in c.fetchall()]
    conn.close()

    for server_id in server_ids:
        try:
            await DiscordUtils.update_list(bot, server_id)
        except Exception as e:
            logger.error(f"Failed to initialize list for server {server_id}: {str(e)}")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
