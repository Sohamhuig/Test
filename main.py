import os
import asyncio
import discord
import shutil
import re
import random
import sys
import time
import requests
import json

from utils.helpers import (
    clear_console,
    resource_path,
    get_env_path,
    load_instructions,
    load_config,
)
from utils.db import init_db, get_channels, get_ignored_users
from utils.error_notifications import webhook_log
from colorama import init, Fore, Style

init()

def check_config():
    env_path = resource_path("config/.env")
    config_path = resource_path("config/config.yaml")
    if not os.path.exists(env_path) or not os.path.exists(config_path):
        print("Config files are not setup! Running setup...")
        import utils.setup as setup
        setup.create_config()

def check_for_update():
    url = "https://api.github.com/repos/Najmul190/Discord-AI-Selfbot/releases/latest"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()["tag_name"]
    else:
        return None

current_version = "v2.0.1"
latest_version = check_for_update()
update_available = latest_version and latest_version != current_version

if update_available:
    print(
        f"{Fore.RED}A new version of the AI Selfbot is available! Please update to {latest_version} at: \nhttps://github.com/Najmul190/Discord-AI-Selfbot/releases/latest{Style.RESET_ALL}"
    )
    time.sleep(5)

check_config()
config = load_config()

from utils.ai import init_ai
from dotenv import load_dotenv
from discord.ext import commands
from utils.ai import generate_response, generate_response_image
from utils.split_response import split_response
from datetime import datetime
from collections import deque
from asyncio import Lock

env_path = get_env_path()
load_dotenv(dotenv_path=env_path, override=True)

init_db()
init_ai()

# Multi-token support
TOKENS = []
token_env = os.getenv("DISCORD_TOKEN")
if token_env:
    TOKENS.append(token_env)

# Load additional tokens
for i in range(2, 11):  # Support up to 10 tokens
    token = os.getenv(f"DISCORD_TOKEN_{i}")
    if token:
        TOKENS.append(token)

if not TOKENS:
    print(f"{Fore.RED}No Discord tokens found! Please check your .env file.{Style.RESET_ALL}")
    sys.exit(1)

print(f"{Fore.GREEN}Found {len(TOKENS)} token(s){Style.RESET_ALL}")

PREFIX = config["bot"]["prefix"]
OWNER_ID = config["bot"]["owner_id"]
TRIGGER = config["bot"]["trigger"].lower().split(",")
DISABLE_MENTIONS = config["bot"]["disable_mentions"]

# Global variables for multi-bot coordination
all_bots = []
bot_conversations = {}  # Track which bot is talking to which user
last_responder = {}     # Track last bot that responded in each channel

class MultiBotManager:
    def __init__(self):
        self.bots = []
        self.active_talks = {}  # channel_id: set of bot_ids talking

    def add_bot(self, bot):
        self.bots.append(bot)

    def get_available_bot(self, channel_id, exclude_user_id=None):
        # Get a bot that's not currently talking to this user
        available = []
        for bot in self.bots:
            if bot.user and bot.user.id not in bot_conversations.get(exclude_user_id, set()):
                available.append(bot)
        return random.choice(available) if available else random.choice(self.bots)

    async def join_server(self, invite_link):
        results = []
        for bot in self.bots:
            try:
                invite = await bot.fetch_invite(invite_link)
                await invite.accept()
                results.append(f"✅ {bot.user.name} joined successfully")
            except Exception as e:
                results.append(f"❌ {bot.user.name} failed: {str(e)}")
        return results

    async def leave_server(self, guild_id):
        results = []
        for bot in self.bots:
            try:
                guild = bot.get_guild(guild_id)
                if guild:
                    await guild.leave()
                    results.append(f"✅ {bot.user.name} left {guild.name}")
                else:
                    results.append(f"❌ {bot.user.name} not in that server")
            except Exception as e:
                results.append(f"❌ {bot.user.name} error: {str(e)}")
        return results

multi_bot_manager = MultiBotManager()

def create_bot(token, bot_index):
    bot = commands.Bot(command_prefix=PREFIX, help_command=None)
    bot.owner_id = OWNER_ID
    bot.active_channels = set(get_channels())
    bot.ignore_users = get_ignored_users()
    bot.message_history = {}
    bot.paused = False
    bot.allow_dm = config["bot"]["allow_dm"]
    bot.allow_gc = config["bot"]["allow_gc"]
    bot.help_command_enabled = config["bot"]["help_command_enabled"]
    bot.realistic_typing = config["bot"]["realistic_typing"]
    bot.anti_age_ban = config["bot"]["anti_age_ban"]
    bot.batch_messages = config["bot"]["batch_messages"]
    bot.batch_wait_time = float(config["bot"]["batch_wait_time"])
    bot.hold_conversation = config["bot"]["hold_conversation"]
    bot.user_message_counts = {}
    bot.user_cooldowns = {}
    bot.respond_without_trigger = config["bot"]["respond_without_trigger"]
    bot.instructions = load_instructions()
    bot.message_queues = {}
    bot.processing_locks = {}
    bot.user_message_batches = {}
    bot.active_conversations = {}
    bot.bot_index = bot_index
    bot.selfbot_id = None
    bot.console_mode = False
    bot.talk_channels = set()  # Channels where bot acts like a human

    # Unique personality variations for each bot
    personality_variants = [
        "You tend to be more enthusiastic and use more exclamations!",
        "You're more laid-back and use casual expressions like 'chill', 'cool', etc.",
        "You're curious and ask lots of follow-up questions.",
        "You're witty and use more humor in conversations.",
        "You're supportive and encouraging in your responses.",
        "You're direct and get straight to the point.",
        "You're analytical and provide detailed explanations."
    ]

    # Assign personality variant to bot
    if bot_index < len(personality_variants):
        bot.personality = personality_variants[bot_index]
    else:
        bot.personality = personality_variants[bot_index % len(personality_variants)]

    @bot.command(name="leave")
    async def leave_server(ctx, guild_id: int = None):
        if ctx.author.id == bot.owner_id:
            if guild_id is None:
                guild_id = ctx.guild.id

            await ctx.send("🔄 Attempting to leave server with all bots...")
            results = await multi_bot_manager.leave_server(guild_id)
            response = "\n".join(results)
            await ctx.send(f"```{response}```")

    @bot.event
    async def on_ready():
        print(f"{Fore.GREEN}Bot {bot.bot_index + 1} logged in as {bot.user}{Style.RESET_ALL}")

        # Load all cogs
        for filename in os.listdir("cogs"):
            if filename.endswith(".py"):
                try:
                    await bot.load_extension(f"cogs.{filename[:-3]}")
                except Exception as e:
                    print(f"{Fore.RED}Failed to load {filename}: {e}{Style.RESET_ALL}")

        # Start console listener for the first bot only
        if bot.bot_index == 0:
            asyncio.create_task(console_listener(bot))

    @bot.event
    async def on_message(message):
        # Don't respond to self or other bots
        if message.author == bot.user or message.author.bot:
            return

        # Process commands first
        await bot.process_commands(message)

        # Check if this is a talk channel
        if hasattr(bot, 'talk_channels') and message.channel.id in bot.talk_channels:
            # Only respond to some messages, not all (like a real human)
            should_respond = False

            # Always respond if mentioned
            if bot.user.mentioned_in(message):
                should_respond = True
            # Sometimes respond to questions
            elif any(word in message.content.lower() for word in ['?', 'kya', 'what', 'how', 'why', 'when', 'where']):
                should_respond = random.random() < 0.4  # 40% chance
            # Sometimes respond to greetings
            elif any(word in message.content.lower() for word in ['hi', 'hello', 'hey', 'sup', 'wassup', 'namaste', 'good morning', 'good night']):
                should_respond = random.random() < 0.6  # 60% chance
            # Sometimes just randomly respond to keep conversation going
            elif len(message.content) > 10:  # Don't respond to very short messages
                should_respond = random.random() < 0.15  # 15% chance for normal messages

            if not should_respond:
                return

            try:
                # Add some natural delay
                async with message.channel.typing():
                    if bot.realistic_typing:
                        typing_time = len(message.content) * 0.02 + random.uniform(1, 3)
                        await asyncio.sleep(min(typing_time, 5))

                    # Use the instructions from instructions.txt directly
                    response = await generate_response(
                        message.content, 
                        "",  # Let the AI file handle instructions
                        history=None
                    )

                    if response and len(response.strip()) > 0:
                        # Don't send error messages to channel
                        if "Sorry, I couldn't generate a response" in response:
                            # Send error notification to owner via DM
                            try:
                                owner = await bot.fetch_user(bot.owner_id)
                                await owner.send(f"⚠️ API Error in {message.guild.name} #{message.channel.name}: Failed to generate response. Check your API key.")
                            except:
                                pass
                            print(f"❌ Failed to generate response in {message.channel.name}")
                            return

                        # Split long responses
                        chunks = split_response(response)
                        for chunk in chunks:
                            if DISABLE_MENTIONS:
                                chunk = re.sub(r'<@[!&]?(\d+)>', r'@\1', chunk)
                            await message.channel.send(chunk)
                            if len(chunks) > 1:
                                await asyncio.sleep(1)

                        print(f"🗣️ Responded in talk channel {message.channel.name}: {response[:50]}...")

            except Exception as e:
                print(f"❌ Error in talk channel response: {e}")
                await webhook_log(message, e)

    async def console_listener(bot):
        """Listen for console commands"""
        import sys
        import select

        while True:
            try:
                await asyncio.sleep(1)

                if not bot.console_mode:
                    continue

                # Check if there's input available (non-blocking)
                if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                    line = input().strip()

                    if line.startswith('send '):
                        parts = line.split(' ', 2)
                        if len(parts) >= 3:
                            try:
                                user_id = int(parts[1])
                                message = parts[2]
                                user = await bot.fetch_user(user_id)
                                if user:
                                    await user.send(message)
                                    print(f"✅ Message sent to {user.name}")
                                else:
                                    print("❌ User not found")
                            except ValueError:
                                print("❌ Invalid user ID")
                            except Exception as e:
                                print(f"❌ Error: {e}")
                        else:
                            print("❌ Usage: send <user_id> <message>")

                    elif line.startswith('broadcast '):
                        message = line[10:]  # Remove 'broadcast '
                        sent_count = 0
                        for channel_id in bot.active_channels:
                            try:
                                channel = bot.get_channel(channel_id)
                                if channel:
                                    await channel.send(message)
                                    sent_count += 1
                            except:
                                pass
                        print(f"✅ Broadcast sent to {sent_count} channels")

                    elif line == 'exit':
                        bot.console_mode = False
                        print("🎮 Console mode disabled")

                    else:
                        print("❌ Unknown command. Available: send, broadcast, exit")

            except Exception as e:
                if bot.console_mode:
                    print(f"Console error: {e}")
                await asyncio.sleep(5)

    return bot

async def main():
    """Main function to start all bots"""
    clear_console()
    print(f"{Fore.CYAN}Starting Discord AI Multi-Bot...{Style.RESET_ALL}")

    if not TOKENS:
        print(f"{Fore.RED}No tokens found! Please check your configuration.{Style.RESET_ALL}")
        return

    # Create bots for each token
    for i, token in enumerate(TOKENS):
        try:
            bot = create_bot(token, i)
            multi_bot_manager.add_bot(bot)
            print(f"{Fore.GREEN}Created bot {i+1}/{len(TOKENS)}{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}Failed to create bot {i+1}: {e}{Style.RESET_ALL}")

    # Start all bots
    tasks = []
    for i, bot in enumerate(multi_bot_manager.bots):
        token = TOKENS[i]
        task = asyncio.create_task(bot.start(token))
        tasks.append(task)

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Shutting down bots...{Style.RESET_ALL}")
        for bot in multi_bot_manager.bots:
            await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Bot stopped.{Style.RESET_ALL}")