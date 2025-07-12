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
    print("No Discord tokens found!")
    sys.exit(1)

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

    # Unique personality variations for each bot
    personality_variants = [
        "You tend to be more enthusiastic and use more exclamations!",
        "You're more laid-back and use casual expressions like 'chill', 'cool', etc.",
        "You're curious and ask lots of follow-up questions.",
        "You're witty and use more humor in conversations.",
        "You're supportive and encouraging in your responses.",
        "You're direct and get straight to the point.",
        "You're thoughtful and often provide detailed explanations.",
        "You're energetic and use lots of emojis in speech patterns.",
        "You're calm and measured in your responses.",
        "You're friendly but slightly sarcastic in a playful way."
    ]

    if bot_index < len(personality_variants):
        bot.personality_variant = personality_variants[bot_index]
    else:
        bot.personality_variant = random.choice(personality_variants)

    CONVERSATION_TIMEOUT = 150.0
    SPAM_MESSAGE_THRESHOLD = 5
    SPAM_TIME_WINDOW = 10.0
    COOLDOWN_DURATION = 60.0
    MAX_HISTORY = 15

    def get_terminal_size():
        columns, _ = shutil.get_terminal_size()
        return columns

    def create_border(char="═"):
        width = get_terminal_size()
        return char * (width - 2)

    def print_header():
        width = get_terminal_size()
        border = create_border()
        title = f"AI Selfbot {bot_index + 1} by Najmul"
        padding = " " * ((width - len(title) - 2) // 2)
        print(f"{Fore.CYAN}╔{border}╗")
        print(f"║{padding}{Style.BRIGHT}{title}{Style.NORMAL}{padding}║")
        print(f"╚{border}╝{Style.RESET_ALL}")

    def print_separator():
        print(f"{Fore.CYAN}{create_border('─')}{Style.RESET_ALL}")

    @bot.event
    async def on_ready():
        if config["bot"]["owner_id"] == 123456789012345678:
            print(f"{Fore.RED}Error: Please set a valid owner_id in config.yaml{Style.RESET_ALL}")
            await bot.close()
            sys.exit(1)

        if config["bot"]["owner_id"] == bot.user.id:
            print(f"{Fore.RED}Error: owner_id in config.yaml cannot be the same as the bot account's user ID{Style.RESET_ALL}")
            await bot.close()
            sys.exit(1)

        bot.selfbot_id = bot.user.id
        multi_bot_manager.add_bot(bot)

        if bot_index == 0:  # Only print once for the first bot
            clear_console()
            print_header()
            print(f"Multi-Bot AI Selfbot successfully started with {len(TOKENS)} bots.\n")

            if update_available:
                print(f"{Fore.RED}A new version of the AI Selfbot is available! Please update to {latest_version} at: \nhttps://github.com/Najmul190/Discord-AI-Selfbot/releases/latest{Style.RESET_ALL}\n")

            if len(bot.active_channels) > 0:
                print("Active in the following channels:")
                for channel_id in bot.active_channels:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            print(f"- #{channel.name} in {channel.guild.name}")
                        except Exception:
                            pass
            else:
                print(f"Bots are currently not active in any channel, use {PREFIX}toggleactive command to activate them in a channel.")

            print(f"\n{Fore.LIGHTBLACK_EX}Join the Discord server for support and news on updates: https://discord.gg/yUWmzQBV4P{Style.RESET_ALL}")
            print_separator()

        print(f"Bot {bot_index + 1} ({bot.user.name}) is ready!")

    @bot.event
    async def setup_hook():
        await load_extensions(bot)

    # Multi-bot commands (only on first bot)
    if bot_index == 0:
        @bot.command(name="join")
        async def join_server(ctx, invite_link):
            if ctx.author.id == bot.owner_id:
                await ctx.send("🔄 Attempting to join server with all bots...")
                results = await multi_bot_manager.join_server(invite_link)
                response = "\n".join(results)
                await ctx.send(f"```{response}```")

        @bot.command(name="leave")
        async def leave_server(ctx, guild_id: int = None):
            if ctx.author.id == bot.owner_id:
                if guild_id is None:
                    guild_id = ctx.guild.id

                await ctx.send("🔄 Attempting to leave server with all bots...")
                results = await multi_bot_manager.leave_server(guild_id)
                response = "\n".join(results)
                await ctx.send(f"```{response}```")

        @bot.command(name="talk")
        async def start_talking(ctx, channel_id: int = None):
            if ctx.author.id == bot.owner_id:
                if channel_id is None:
                    channel_id = ctx.channel.id

                # Activate all bots in this channel
                for current_bot in multi_bot_manager.bots:
                    current_bot.active_channels.add(channel_id)

                multi_bot_manager.active_talks[channel_id] = set(bot.user.id for bot in multi_bot_manager.bots if bot.user)

                await ctx.send(f"🤖 All bots are now active in <#{channel_id}> and will engage in natural conversations!")

    def should_ignore_message(message):
        return (
            message.author.id in bot.ignore_users
            or message.author.id == bot.selfbot_id
            or message.author.bot
        )

    def is_trigger_message(message):
        mentioned = (
            bot.user.mentioned_in(message)
            and "@everyone" not in message.content
            and "@here" not in message.content
        )
        replied_to = (
            message.reference
            and message.reference.resolved
            and message.reference.resolved.author.id == bot.selfbot_id
        )
        is_dm = isinstance(message.channel, discord.DMChannel) and bot.allow_dm
        is_group_dm = isinstance(message.channel, discord.GroupChannel) and bot.allow_gc

        conv_key = f"{message.author.id}-{message.channel.id}"
        in_conversation = (
            conv_key in bot.active_conversations
            and time.time() - bot.active_conversations[conv_key] < CONVERSATION_TIMEOUT
            and bot.hold_conversation
        )

        content_has_trigger = any(
            re.search(rf"\b{re.escape(keyword)}\b", message.content.lower())
            for keyword in TRIGGER
        )

        is_active_channel = message.channel.id in bot.active_channels and bot.respond_without_trigger

        # Multi-bot logic: only respond if this bot should handle this message
        should_respond = False
        if (content_has_trigger or mentioned or replied_to or is_dm or is_group_dm or in_conversation or is_active_channel):

            # Check if another bot recently responded to this user
            user_conv_key = f"{message.author.id}"
            last_bot = bot_conversations.get(user_conv_key)

            if last_bot != bot.selfbot_id or random.random() < 0.3:  # 30% chance to switch bots
                # Probability-based selection to avoid same bot always responding
                available_bots = [b for b in multi_bot_manager.bots if b.user and b.user.id != last_bot]
                if available_bots and random.random() < 0.7:  # 70% chance to use different bot
                    selected_bot = random.choice(available_bots)
                    if selected_bot.user.id == bot.selfbot_id:
                        should_respond = True
                        bot_conversations[user_conv_key] = bot.selfbot_id
                elif bot.selfbot_id == last_bot:
                    should_respond = True
                else:
                    # First time interaction or fallback
                    if random.random() < (1.0 / len(multi_bot_manager.bots)):
                        should_respond = True
                        bot_conversations[user_conv_key] = bot.selfbot_id

        if should_respond:
            bot.active_conversations[conv_key] = time.time()

        return should_respond

    def update_message_history(bot, author_id, message_content, is_bot_response=False):
        key = f"{author_id}"
        if key not in bot.message_history:
            bot.message_history[key] = []

        role = "assistant" if is_bot_response else "user"
        bot.message_history[key].append({"role": role, "content": message_content})
        bot.message_history[key] = bot.message_history[key][-MAX_HISTORY:]

    async def generate_response_and_reply(message, prompt, history, image_url=None):
        # Add personality variant to instructions
        enhanced_instructions = bot.instructions + f"\n\nPersonality note: {bot.personality_variant}"

        if not bot.realistic_typing:
            async with message.channel.typing():
                if image_url:
                    response = await generate_response_image(
                        prompt, enhanced_instructions, image_url, history
                    )
                else:
                    response = await generate_response(prompt, enhanced_instructions, history)
        else:
            if image_url:
                response = await generate_response_image(
                    prompt, enhanced_instructions, image_url, history
                )
            else:
                response = await generate_response(prompt, enhanced_instructions, history)

        chunks = split_response(response)

        for chunk in chunks:
                # Add server emojis randomly to responses
                if hasattr(message.channel, 'guild') and message.channel.guild.emojis:
                    server_emojis = [str(emoji) for emoji in message.channel.guild.emojis]
                    if server_emojis and random.random() < 0.3:  # 30% chance to add server emoji
                        emoji_to_add = random.choice(server_emojis)
                        chunk += f" {emoji_to_add}"

                # Add standard emojis more frequently
                if random.random() < 0.6:  # 60% chance to add emoji
                    indian_emojis = ["😊", "😄", "🤔", "👍", "❤️", "🔥", "💯", "😎", "🙏", "✨", "👋", "😁", "🎉", "💪"]
                    chunk += f" {random.choice(indian_emojis)}"

                if DISABLE_MENTIONS:
                    # Don't disable mentions completely, but make them safer
                    chunk = chunk.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")

                if bot.anti_age_ban:
                    chunk = re.sub(
                        r"(?<!\d)([0-9]|1[0-2])(?!\d)|\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
                        "\u200b",
                        chunk,
                        flags=re.IGNORECASE,
                    )

                print(f'{datetime.now().strftime("[%H:%M:%S]")} Bot {bot_index + 1} ({bot.user.name}): {message.author.name}: {prompt}')
                print(f'{datetime.now().strftime("[%H:%M:%S]")} Bot {bot_index + 1} responding: {chunk}')
                print_separator()

            try:
                if bot.realistic_typing:
                    await asyncio.sleep(random.randint(10, 30))
                    async with message.channel.typing():
                        characters_per_second = random.uniform(5.0, 6.0)
                        await asyncio.sleep(int(len(chunk) / characters_per_second))

                try:
                    if isinstance(message.channel, discord.DMChannel):
                        sent_message = await message.channel.send(chunk)
                    else:
                        sent_message = await message.reply(
                            chunk,
                            mention_author=(True if config["bot"]["reply_ping"] else False),
                        )

                    conv_key = f"{message.author.id}-{message.channel.id}"
                    bot.active_conversations[conv_key] = time.time()

                except discord.errors.HTTPException as e:
                    print(f"{datetime.now().strftime('[%H:%M:%S]')} Bot {bot_index + 1} error replying to message")
                    await webhook_log(message, e)
                except discord.errors.Forbidden:
                    print(f"{datetime.now().strftime('[%H:%M:%S]')} Bot {bot_index + 1} missing permissions")
                except Exception as e:
                    print(f"{datetime.now().strftime('[%H:%M:%S]')} Bot {bot_index + 1} error: {e}")

            except discord.errors.Forbidden:
                print(f"{datetime.now().strftime('[%H:%M:%S]')} Bot {bot_index + 1} missing permissions to send message")

        return response

    @bot.event
    async def on_message(message):
        if should_ignore_message(message) and not message.author.id == bot.owner_id:
            return

        if message.content.startswith(PREFIX):
            await bot.process_commands(message)
            return

        channel_id = message.channel.id
        user_id = message.author.id
        current_time = time.time()

        batch_key = f"{user_id}-{channel_id}"
        is_followup = batch_key in bot.user_message_batches
        is_trigger = is_trigger_message(message)

        if (is_trigger or (is_followup and bot.hold_conversation)) and not bot.paused:
            if user_id in bot.user_cooldowns:
                cooldown_end = bot.user_cooldowns[user_id]
                if current_time < cooldown_end:
                    return
                else:
                    del bot.user_cooldowns[user_id]

            if user_id not in bot.user_message_counts:
                bot.user_message_counts[user_id] = []

            bot.user_message_counts[user_id] = [
                timestamp
                for timestamp in bot.user_message_counts[user_id]
                if current_time - timestamp < SPAM_TIME_WINDOW
            ]

            bot.user_message_counts[user_id].append(current_time)

            if len(bot.user_message_counts[user_id]) > SPAM_MESSAGE_THRESHOLD:
                bot.user_cooldowns[user_id] = current_time + COOLDOWN_DURATION
                print(f"{datetime.now().strftime('[%H:%M:%S]')} Bot {bot_index + 1}: User {message.author.name} cooldown")
                bot.user_message_counts[user_id] = []
                return

            if channel_id not in bot.message_queues:
                bot.message_queues[channel_id] = deque()
                bot.processing_locks[channel_id] = Lock()

            bot.message_queues[channel_id].append(message)

            if not bot.processing_locks[channel_id].locked():
                asyncio.create_task(process_message_queue(channel_id))

    async def process_message_queue(channel_id):
        async with bot.processing_locks[channel_id]:
            while bot.message_queues[channel_id]:
                message = bot.message_queues[channel_id].popleft()
                batch_key = f"{message.author.id}-{channel_id}"
                current_time = time.time()

                if bot.batch_messages:
                    if batch_key not in bot.user_message_batches:
                        first_image_url = (
                            message.attachments[0].url if message.attachments else None
                        )
                        bot.user_message_batches[batch_key] = {
                            "messages": [],
                            "last_time": current_time,
                            "image_url": first_image_url,
                        }
                        bot.user_message_batches[batch_key]["messages"].append(message)

                        await asyncio.sleep(bot.batch_wait_time)

                        while bot.message_queues[channel_id]:
                            next_message = bot.message_queues[channel_id][0]
                            if (
                                next_message.author.id == message.author.id
                                and not next_message.content.startswith(PREFIX)
                            ):
                                next_message = bot.message_queues[channel_id].popleft()
                                if next_message.content not in [
                                    m.content
                                    for m in bot.user_message_batches[batch_key]["messages"]
                                ]:
                                    bot.user_message_batches[batch_key]["messages"].append(
                                        next_message
                                    )

                                if (
                                    not bot.user_message_batches[batch_key]["image_url"]
                                    and next_message.attachments
                                ):
                                    bot.user_message_batches[batch_key]["image_url"] = (
                                        next_message.attachments[0].url
                                    )
                            else:
                                break

                        messages_to_process = bot.user_message_batches[batch_key]["messages"]
                        seen = set()
                        unique_messages = []
                        for msg in messages_to_process:
                            if msg.content not in seen:
                                seen.add(msg.content)
                                unique_messages.append(msg)

                        combined_content = "\n".join(msg.content for msg in unique_messages)
                        message_to_reply_to = unique_messages[-1]
                        image_url = bot.user_message_batches[batch_key]["image_url"]

                        del bot.user_message_batches[batch_key]
                else:
                    combined_content = message.content
                    message_to_reply_to = message
                    image_url = message.attachments[0].url if message.attachments else None

                for mention in message_to_reply_to.mentions:
                    combined_content = combined_content.replace(
                        f"<@{mention.id}>", f"@{mention.display_name}"
                    )

                history = bot.message_history.get(f"{message_to_reply_to.author.id}", [])

                if message_to_reply_to.channel.id in bot.active_channels or (
                    isinstance(message_to_reply_to.channel, discord.DMChannel)
                    and bot.allow_dm
                ):
                    response = await generate_response_and_reply(
                        message_to_reply_to, combined_content, history, image_url
                    )
                    update_message_history(bot, message_to_reply_to.author.id, combined_content, is_bot_response=False)
                    update_message_history(bot, message_to_reply_to.author.id, response, is_bot_response=True)

    return bot

async def load_extensions(bot):
    if getattr(sys, "frozen", False):
        cogs_dir = os.path.join(sys._MEIPASS, "cogs")
    else:
        cogs_dir = os.path.join(os.path.abspath("."), "cogs")

    if not os.path.exists(cogs_dir):
        print(f"Warning: Cogs directory not found at {cogs_dir}. Skipping cog loading.")
        return

    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py"):
            cog_name = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(cog_name)
            except Exception as e:
                print(f"Error loading cog {cog_name}: {e}")

async def run_all_bots():
    tasks = []
    for i, token in enumerate(TOKENS):
        bot = create_bot(token, i)
        all_bots.append(bot)
        task = asyncio.create_task(bot.start(token, reconnect=True))
        tasks.append(task)

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(run_all_bots())