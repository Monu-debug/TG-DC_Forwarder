import os
import sys
import yaml
import asyncio
import logging
import threading
import streamlit as st
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from forwarder import DiscordForwarder
import discord
from discord.ext import commands
from discord import app_commands
from typing import List

# Set page configuration
st.set_page_config(
    page_title="Telegram to Discord Forwarder",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Premium CSS Styling
st.markdown("""
    <style>
        .reportview-container {
            background: #0e1117;
        }
        .main {
            background-color: #0e1117;
            color: #ffffff;
        }
        div.stButton > button {
            background-color: #5865F2;
            color: white;
            border-radius: 6px;
            border: none;
            padding: 0.5rem 1rem;
            transition: all 0.2s ease-in-out;
        }
        div.stButton > button:hover {
            background-color: #4752C4;
            transform: scale(1.02);
        }
        .status-box {
            padding: 1.5rem;
            border-radius: 10px;
            margin-bottom: 1.5rem;
            border: 1px solid #30363d;
        }
        .status-running {
            background-color: rgba(35, 165, 90, 0.15);
            border-color: #23a55a;
        }
        .status-stopped {
            background-color: rgba(128, 128, 128, 0.15);
            border-color: #808080;
        }
        .status-auth {
            background-color: rgba(240, 178, 50, 0.15);
            border-color: #f0b232;
        }
        .status-error {
            background-color: rgba(242, 63, 66, 0.15);
            border-color: #f23f42;
        }
    </style>
""", unsafe_allow_html=True)

# Logger setup
logger = logging.getLogger("telegram_discord_forwarder_streamlit")

class BotManager:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.client = None
        self.forwarder = None
        self.status = "Stopped"  # "Stopped", "Starting", "Running", "Need Phone", "Need Code", "Need 2FA", "Error"
        
        self.phone = None
        self.code = None
        self.password = None
        self.error_msg = None
        
        self.phone_submitted = None
        self.code_submitted = None
        self.password_submitted = None
        
        self.channel_to_webhook = {}
        self.active_telegram_handler = None
        self.discord_task = None
        self.cached_telegram_channels = []
        self.logs = []
        
    def log(self, message: str, level=logging.INFO):
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} [{logging.getLevelName(level)}] {message}"
        
        if level == logging.ERROR:
            logger.error(message)
        elif level == logging.WARNING:
            logger.warning(message)
        else:
            logger.info(message)
            
        self.logs.append(log_entry)
        if len(self.logs) > 200:
            self.logs.pop(0)

    def start_bot(self):
        if self.status in ["Running", "Starting", "Need Phone", "Need Code", "Need 2FA"]:
            return
        
        self.status = "Starting"
        self.log("Starting background bot manager...")
        self.thread = threading.Thread(target=self._run_thread, daemon=True)
        self.thread.start()

    def _run_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.phone_submitted = asyncio.Event()
        self.code_submitted = asyncio.Event()
        self.password_submitted = asyncio.Event()
        
        try:
            self.loop.run_until_complete(self._main_loop())
        except Exception as e:
            self.status = "Error"
            self.error_msg = str(e)
            self.log(f"Critical error in main loop: {e}", logging.ERROR)

    async def cache_telegram_dialogs(self):
        """Retrieves and caches Telegram channels the account is joined to."""
        if not self.client or not self.client.is_connected():
            return
            
        try:
            self.log("Caching Telegram channels for autocomplete...")
            channels = []
            async for dialog in self.client.iter_dialogs():
                if dialog.is_channel or dialog.is_group:
                    entity = dialog.entity
                    title = getattr(entity, 'title', 'Unknown Title')
                    username = getattr(entity, 'username', '') or ''
                    channels.append({
                        "title": title,
                        "id": str(entity.id),
                        "username": username
                    })
            self.cached_telegram_channels = channels
            self.log(f"Cached {len(channels)} Telegram chats/channels.")
        except Exception as e:
            self.log(f"Error caching Telegram channels: {e}", logging.ERROR)

    async def _main_loop(self):
        config = None
        
        try:
            if len(st.secrets) > 0 and "telegram" in st.secrets:
                self.log("Loading configuration from Streamlit Cloud secrets...")
                config = st.secrets.to_dict()
        except Exception as e:
            self.log(f"Streamlit secrets not configured or readable: {e}", logging.DEBUG)

        if not config:
            if not os.path.exists("config.yaml"):
                self.status = "Error"
                self.error_msg = "Configuration missing: config.yaml not found and st.secrets not configured!"
                self.log(self.error_msg, logging.ERROR)
                return

            try:
                self.log("Loading configuration from local config.yaml...")
                with open("config.yaml", "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
            except Exception as e:
                self.status = "Error"
                self.error_msg = f"Error reading config.yaml: {e}"
                self.log(self.error_msg, logging.ERROR)
                return

        telegram_cfg = config.get("telegram", {})
        api_id = telegram_cfg.get("api_id")
        api_hash = telegram_cfg.get("api_hash")
        phone_val = telegram_cfg.get("phone")
        
        discord_cfg = config.get("discord", {})
        discord_token = discord_cfg.get("bot_token")
        
        if not api_id or not api_hash:
            self.status = "Error"
            self.error_msg = "api_id and api_hash must be set in config!"
            self.log(self.error_msg, logging.ERROR)
            return

        # Initialize Telethon Client
        self.client = TelegramClient("telegram_forwarder_session", api_id, api_hash)
        self.forwarder = DiscordForwarder(config)

        self.log("Connecting to Telegram...")
        await self.client.connect()
        
        # Check authentication status
        if not await self.client.is_user_authorized():
            self.log("Telegram session not authorized. Authentication needed.")
            
            # 1. Obtain Phone Number
            if not phone_val:
                self.status = "Need Phone"
                self.log("Waiting for phone number from Streamlit dashboard...")
                await self.phone_submitted.wait()
                self.phone_submitted.clear()
                phone_val = self.phone
            
            self.log(f"Sending OTP verification code request to {phone_val}...")
            try:
                sent_code = await self.client.send_code_request(phone_val)
                phone_code_hash = sent_code.phone_code_hash
            except Exception as e:
                self.status = "Error"
                self.error_msg = f"Error sending verification code: {e}"
                self.log(self.error_msg, logging.ERROR)
                await self.client.disconnect()
                return

            # 2. Obtain Verification Code
            self.status = "Need Code"
            self.log("Verification code sent. Waiting for code from Streamlit dashboard...")
            await self.code_submitted.wait()
            self.code_submitted.clear()
            code_val = self.code

            # 3. Sign in
            try:
                await self.client.sign_in(phone=phone_val, code=code_val, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                self.log("Two-factor authentication (2FA) is enabled on this account. Password required.")
                self.status = "Need 2FA"
                await self.password_submitted.wait()
                self.password_submitted.clear()
                pwd_val = self.password
                
                try:
                    await self.client.sign_in(password=pwd_val)
                except Exception as e:
                    self.status = "Error"
                    self.error_msg = f"Failed to authenticate with 2FA password: {e}"
                    self.log(self.error_msg, logging.ERROR)
                    await self.client.disconnect()
                    return
            except Exception as e:
                self.status = "Error"
                self.error_msg = f"Sign in failed: {e}"
                self.log(self.error_msg, logging.ERROR)
                await self.client.disconnect()
                return

        self.log("Telegram client successfully authenticated!")
        
        # Cache dialogs for autocomplete
        asyncio.create_task(self.cache_telegram_dialogs())
        
        # Resolve mappings
        self.channel_to_webhook = {}
        for mapping in self.forwarder.get_all_mappings():
            channel_name = mapping.get("telegram_channel")
            webhook_key = mapping.get("webhook_key")
            
            try:
                if isinstance(channel_name, str) and (channel_name.startswith('-') or channel_name.isdigit()):
                    channel_name = int(channel_name)
                
                entity = await self.client.get_entity(channel_name)
                self.channel_to_webhook[entity.id] = (entity, webhook_key)
                self.log(f"Subscribed Channel: '{entity.title}' ({entity.id}) -> Webhook Key: '{webhook_key}'")
            except Exception as e:
                self.log(f"Failed to resolve Telegram channel '{channel_name}': {e}", logging.ERROR)

        # Register message listener
        await self.update_tg_listeners()

        # Start Discord Bot task in background if token is provided
        self.discord_task = None
        if discord_token:
            self.log("Starting Discord command bot...")
            self.discord_task = asyncio.create_task(discord_bot.start(discord_token))
        else:
            self.log("No discord.bot_token found. Running in Webhook-only mode.", logging.WARNING)

        self.status = "Running"
        self.log("Forwarder is actively listening for new messages. Web app is operational!")
        await self.client.run_until_disconnected()

    async def update_tg_listeners(self):
        """Reloads the Telethon message listeners based on active mapping."""
        if self.active_telegram_handler:
            try:
                self.client.remove_event_handler(self.active_telegram_handler)
            except Exception:
                pass
                
        channel_ids = list(self.channel_to_webhook.keys())
        if not channel_ids:
            self.active_telegram_handler = None
            return

        async def handler(event):
            chat_id = event.chat_id
            mapped_item = self.channel_to_webhook.get(chat_id)
            if not mapped_item:
                for resolved_id, item in self.channel_to_webhook.items():
                    if abs(resolved_id) == abs(chat_id) or str(resolved_id) in str(chat_id):
                        mapped_item = item
                        break
            if not mapped_item:
                return
            
            entity, webhook_key = mapped_item
            self.log(f"Detected new message in '{entity.title}'. Forwarding...")
            await self.forwarder.forward_message(self.client, entity, event.message, webhook_key)

        self.client.add_event_handler(handler, events.NewMessage(chats=channel_ids))
        self.active_telegram_handler = handler
        self.log(f"Telethon registered message listeners for {len(channel_ids)} channels.")

    def submit_phone(self, phone):
        self.phone = phone
        if self.loop and self.phone_submitted:
            self.loop.call_soon_threadsafe(self.phone_submitted.set)

    def submit_code(self, code):
        self.code = code
        if self.loop and self.code_submitted:
            self.loop.call_soon_threadsafe(self.code_submitted.set)

    def submit_password(self, password):
        self.password = password
        if self.loop and self.password_submitted:
            self.loop.call_soon_threadsafe(self.password_submitted.set)

    async def _stop(self):
        if self.client:
            self.log("Disconnecting client from Telegram...")
            await self.client.disconnect()
        if self.discord_task:
            self.log("Stopping Discord command bot...")
            await discord_bot.close()
            await self.discord_task
            self.discord_task = None
        self.status = "Stopped"
        self.log("Forwarder has been stopped.")

    def stop_bot(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._stop(), self.loop)

# Singleton manager getter
@st.cache_resource
def get_bot_manager():
    return BotManager()

# Retrieve manager instance
manager = get_bot_manager()

# --- DISCORD COMMAND BOT IMPLEMENTATION ---
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

# Helpers to construct output embeds
async def build_status_embed():
    tg_connected = manager.client.is_connected() if manager.client else False
    tg_auth = await manager.client.is_user_authorized() if manager.client and tg_connected else False
    
    embed = discord.Embed(
        title="🔄 Forwarder Status Report",
        color=discord.Color.green() if (tg_connected and tg_auth) else discord.Color.red()
    )
    embed.add_field(name="Telegram Service", value="Connected & Authorized ✅" if (tg_connected and tg_auth) else "Disconnected ❌", inline=True)
    embed.add_field(name="Discord Command Bot", value="Online ✅", inline=True)
    embed.add_field(name="Active Mapped Channels", value=f"{len(manager.channel_to_webhook)} channel(s)", inline=False)
    return embed

def build_list_embed():
    if not manager.channel_to_webhook:
        return None
    embed = discord.Embed(title="📋 Active Channel Mappings", color=discord.Color.blue())
    for ch_id, (entity, webhook_key) in manager.channel_to_webhook.items():
        title = getattr(entity, 'title', f"ID: {ch_id}")
        webhook_url = manager.forwarder.webhooks.get(webhook_key, "Unknown URL")
        masked_url = webhook_url[:45] + "..." if len(webhook_url) > 45 else webhook_url
        embed.add_field(
            name=f"📢 {title}",
            value=f"• **Telegram ID:** `{ch_id}`\n• **Webhook Key:** `{webhook_key}`\n• **Webhook URL:** `{masked_url}`",
            inline=False
        )
    return embed

async def get_or_create_webhook(channel: discord.TextChannel) -> str:
    permissions = channel.permissions_for(channel.guild.me)
    if not permissions.manage_webhooks:
        raise PermissionError(
            "Bot lacks 'Manage Webhooks' permission in that channel. "
            "Please grant this permission to the bot or manually create a webhook and provide the Webhook URL."
        )
        
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.user == discord_bot.user:
            return wh.url
            
    new_wh = await channel.create_webhook(name="Telegram Forwarder Link")
    return new_wh.url

async def resolve_discord_channel(guild: discord.Guild, input_str: str) -> discord.TextChannel:
    clean_id = input_str.strip("<#> ")
    if clean_id.isdigit():
        channel = guild.get_channel(int(clean_id))
        if isinstance(channel, discord.TextChannel):
            return channel
            
    channel = discord.utils.get(guild.text_channels, name=input_str.lstrip('#'))
    if channel:
        return channel
        
    raise ValueError(f"Could not find a text channel named or matching '{input_str}' in this server.")

@discord_bot.event
async def on_ready():
    manager.log(f"Discord Command Bot connected as {discord_bot.user}")
    try:
        manager.log("Synchronizing slash commands globally...")
        synced = await discord_bot.tree.sync()
        manager.log(f"Successfully synchronized {len(synced)} slash command(s).")
    except Exception as e:
        manager.log(f"Failed to synchronize slash commands: {e}", logging.ERROR)

# --- PREFIX COMMANDS ---

@discord_bot.command(name="status")
async def cmd_status(ctx):
    """Shows forwarder connection status."""
    embed = await build_status_embed()
    await ctx.send(embed=embed)

@discord_bot.command(name="list")
async def cmd_list(ctx):
    """Lists all mapped Telegram channels."""
    embed = build_list_embed()
    if not embed:
        await ctx.send("No Telegram channels are currently mapped to forward messages.")
        return
    await ctx.send(embed=embed)

@discord_bot.command(name="add")
async def cmd_add(ctx, telegram_channel: str, discord_target: str):
    """Maps a new Telegram channel. Usage: !add <id> <channel_or_webhook>"""
    webhook_url = None
    if discord_target.startswith("https://discord.com/api/webhooks/"):
        webhook_url = discord_target
    else:
        try:
            channel = await resolve_discord_channel(ctx.guild, discord_target)
            webhook_url = await get_or_create_webhook(channel)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            return
            
    await ctx.send(f"⏳ Attempting to subscribe to Telegram channel `{telegram_channel}`...")
    
    resolved = telegram_channel
    if telegram_channel.startswith('-') or telegram_channel.isdigit():
        try:
            resolved = int(telegram_channel)
        except ValueError:
            pass
            
    try:
        future = asyncio.run_coroutine_threadsafe(manager.client.get_entity(resolved), manager.loop)
        entity = await asyncio.wrap_future(future)
        
        webhook_key = manager.forwarder.add_mapping(entity.id, webhook_url)
        manager.channel_to_webhook[entity.id] = (entity, webhook_key)
        
        future_listeners = asyncio.run_coroutine_threadsafe(manager.update_tg_listeners(), manager.loop)
        await asyncio.wrap_future(future_listeners)
        
        embed = discord.Embed(title="✅ Mapping Added Successfully", color=discord.Color.green())
        embed.add_field(name="Telegram Channel", value=f"**{entity.title}** (`{entity.id}`)", inline=False)
        embed.add_field(name="Target Webhook Key", value=f"`{webhook_key}`", inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        manager.log(f"Failed to add mapping via Discord command: {e}", logging.ERROR)
        await ctx.send(f"❌ Error: Could not resolve channel `{telegram_channel}`. Reason: `{e}`")

@discord_bot.command(name="remove")
async def cmd_remove(ctx, telegram_channel: str):
    """Removes a mapped Telegram channel. Usage: !remove <id_or_title>"""
    matched_id = None
    matched_entity = None
    target_str = str(telegram_channel)
    
    for ch_id, (entity, _) in manager.channel_to_webhook.items():
        if str(ch_id) == target_str or str(ch_id).replace('-100', '') == target_str:
            matched_id = ch_id
            matched_entity = entity
            break
        if hasattr(entity, 'username') and entity.username and entity.username.lower() == target_str.lower().lstrip('@'):
            matched_id = ch_id
            matched_entity = entity
            break
        if hasattr(entity, 'title') and entity.title.lower() == target_str.lower():
            matched_id = ch_id
            matched_entity = entity
            break
            
    if not matched_id:
        await ctx.send(f"❌ Error: Could not find '{telegram_channel}' in active mappings.")
        return

    title = matched_entity.title if matched_entity else f"ID: {matched_id}"
    success = manager.forwarder.remove_mapping(matched_id)
    
    if success:
        manager.channel_to_webhook.pop(matched_id, None)
        future_listeners = asyncio.run_coroutine_threadsafe(manager.update_tg_listeners(), manager.loop)
        await asyncio.wrap_future(future_listeners)
        await ctx.send(f"✅ Successfully removed forwarding mapping for **{title}** (`{matched_id}`).")
    else:
        await ctx.send(f"❌ Error: Failed to remove mapping. Mappings defined in settings/secrets must be removed there directly.")

# --- SLASH COMMANDS (WITH AUTOCOMPLETE & NATIVE CHANNEL SELECTORS) ---

@discord_bot.tree.command(name="status", description="Shows forwarder connection status and system health")
async def slash_status(interaction: discord.Interaction):
    embed = await build_status_embed()
    await interaction.response.send_message(embed=embed)

@discord_bot.tree.command(name="list", description="Lists all mapped Telegram channels and webhooks")
async def slash_list(interaction: discord.Interaction):
    embed = build_list_embed()
    if not embed:
        await interaction.response.send_message("No Telegram channels are currently mapped to forward messages.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@discord_bot.tree.command(name="add", description="Maps a new Telegram channel to a Discord Channel")
@app_commands.describe(
    telegram_channel="Select or type the Telegram channel (autofills from your joined channels)",
    discord_channel="Select the text channel where messages should be forwarded"
)
async def slash_add(interaction: discord.Interaction, telegram_channel: str, discord_channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=False)
    
    try:
        webhook_url = await get_or_create_webhook(discord_channel)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return
            
    resolved = telegram_channel
    if telegram_channel.startswith('-') or telegram_channel.isdigit():
        try:
            resolved = int(telegram_channel)
        except ValueError:
            pass
            
    try:
        future = asyncio.run_coroutine_threadsafe(manager.client.get_entity(resolved), manager.loop)
        entity = await asyncio.wrap_future(future)
        
        webhook_key = manager.forwarder.add_mapping(entity.id, webhook_url)
        manager.channel_to_webhook[entity.id] = (entity, webhook_key)
        
        future_listeners = asyncio.run_coroutine_threadsafe(manager.update_tg_listeners(), manager.loop)
        await asyncio.wrap_future(future_listeners)
        
        embed = discord.Embed(title="✅ Mapping Added Successfully", color=discord.Color.green())
        embed.add_field(name="Telegram Channel", value=f"**{entity.title}** (`{entity.id}`)", inline=False)
        embed.add_field(name="Discord Channel", value=discord_channel.mention, inline=True)
        await interaction.followup.send(embed=embed)
        
        # Reload dialogs cache
        asyncio.run_coroutine_threadsafe(manager.cache_telegram_dialogs(), manager.loop)
    except Exception as e:
        manager.log(f"Failed to add mapping via slash command: {e}", logging.ERROR)
        await interaction.followup.send(f"❌ Error: Could not resolve channel `{telegram_channel}`. Reason: `{e}`")

@discord_bot.tree.command(name="remove", description="Removes a mapped Telegram channel mapping")
@app_commands.describe(
    telegram_channel="Select the mapped Telegram channel to remove"
)
async def slash_remove(interaction: discord.Interaction, telegram_channel: str):
    await interaction.response.defer(ephemeral=False)
    matched_id = None
    matched_entity = None
    target_str = str(telegram_channel)
    
    for ch_id, (entity, _) in manager.channel_to_webhook.items():
        if str(ch_id) == target_str or str(ch_id).replace('-100', '') == target_str:
            matched_id = ch_id
            matched_entity = entity
            break
            
    if not matched_id:
        await interaction.followup.send(f"❌ Error: Could not find '{telegram_channel}' in active mappings.")
        return

    title = matched_entity.title if matched_entity else f"ID: {matched_id}"
    success = manager.forwarder.remove_mapping(matched_id)
    
    if success:
        manager.channel_to_webhook.pop(matched_id, None)
        future_listeners = asyncio.run_coroutine_threadsafe(manager.update_tg_listeners(), manager.loop)
        await asyncio.wrap_future(future_listeners)
        await interaction.followup.send(f"✅ Successfully removed forwarding mapping for **{title}** (`{matched_id}`).")
    else:
        await interaction.followup.send(f"❌ Error: Failed to remove mapping. Mappings defined in settings/secrets must be removed there directly.")

# --- AUTOCOMPLETE PROVIDERS ---

@slash_add.autocomplete('telegram_channel')
async def add_telegram_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    choices = []
    search = current.lower()
    count = 0
    for ch in manager.cached_telegram_channels:
        title = ch["title"]
        username = ch["username"]
        ch_id = ch["id"]
        if search in title.lower() or (username and search in username.lower()) or search in ch_id:
            display_name = f"{title} (@{username})" if username else title
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."
            choices.append(app_commands.Choice(name=display_name, value=ch_id))
            count += 1
            if count >= 25:
                break
    return choices

@slash_remove.autocomplete('telegram_channel')
async def remove_telegram_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    choices = []
    search = current.lower()
    count = 0
    for ch_id, (entity, _) in manager.channel_to_webhook.items():
        title = getattr(entity, 'title', f"ID: {ch_id}")
        username = getattr(entity, 'username', '') or ''
        if search in title.lower() or (username and search in username.lower()) or search in str(ch_id):
            display_name = f"{title} (ID: {ch_id})"
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."
            choices.append(app_commands.Choice(name=display_name, value=str(ch_id)))
            count += 1
            if count >= 25:
                break
    return choices

# --- WEB APP INTERFACE ---

st.title("🔄 Telegram to Discord Channel Forwarder")
st.markdown("Use this web interface to run, monitor, and configure your message forwarder.")

status_colors = {
    "Stopped": ("status-stopped", "Stopped ⚪"),
    "Starting": ("status-auth", "Starting 🟡"),
    "Running": ("status-running", "Running 🟢"),
    "Need Phone": ("status-auth", "Authentication Required: Enter Phone 📱"),
    "Need Code": ("status-auth", "Authentication Required: Enter OTP Code 🔑"),
    "Need 2FA": ("status-auth", "Authentication Required: Enter 2FA Password 🔐"),
    "Error": ("status-error", f"Error 🔴")
}

style_class, status_label = status_colors.get(manager.status, ("status-stopped", manager.status))
if manager.status == "Error" and manager.error_msg:
    status_label = f"Error: {manager.error_msg} 🔴"

st.markdown(f"""
    <div class="status-box {style_class}">
        <h4 style="margin:0; padding:0; font-weight: 500;">Current Status: {status_label}</h4>
    </div>
""", unsafe_allow_html=True)

col1, col2 = st.columns([1, 2])

with col1:
    st.header("🎛️ Control Panel")
    
    col_start, col_stop = st.columns(2)
    with col_start:
        if st.button("▶️ Start Bot", use_container_width=True, disabled=(manager.status != "Stopped" and manager.status != "Error")):
            manager.start_bot()
            st.rerun()
    with col_stop:
        if st.button("⏹️ Stop Bot", use_container_width=True, disabled=(manager.status == "Stopped")):
            manager.stop_bot()
            st.rerun()

    st.markdown("---")

    if manager.status == "Need Phone":
        st.subheader("Enter Phone Number")
        phone_input = st.text_input("Phone (with country code, e.g. +917499224791)", key="phone_input")
        if st.button("Send Code", key="btn_phone"):
            if phone_input:
                manager.submit_phone(phone_input)
                st.info("Sending code request...")
                st.rerun()
            else:
                st.error("Please enter a valid phone number.")

    elif manager.status == "Need Code":
        st.subheader("Enter Verification Code")
        st.info("Check your Telegram app for the verification code.")
        code_input = st.text_input("OTP Verification Code", key="code_input")
        if st.button("Verify Code", key="btn_code"):
            if code_input:
                manager.submit_code(code_input)
                st.info("Verifying...")
                st.rerun()
            else:
                st.error("Please enter the verification code.")

    elif manager.status == "Need 2FA":
        st.subheader("Enter 2FA Password")
        st.warning("Two-Factor Authentication is enabled on this Telegram account.")
        password_input = st.text_input("2FA Password", type="password", key="password_input")
        if st.button("Submit Password", key="btn_pass"):
            if password_input:
                manager.submit_password(password_input)
                st.info("Submitting password...")
                st.rerun()
            else:
                st.error("Please enter your 2FA password.")

    st.subheader("📝 Mapped Configurations")
    if os.path.exists("config.yaml") or (len(st.secrets) > 0 and "telegram" in st.secrets):
        try:
            config = st.secrets.to_dict() if (len(st.secrets) > 0 and "telegram" in st.secrets) else None
            if not config:
                with open("config.yaml", "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    
            st.write("**Telegram Phone:**", config.get("telegram", {}).get("phone", "N/A"))
            st.write("**Discord Bot:**", "Configured ✅" if config.get("discord", {}).get("bot_token") else "Not Configured ❌")
            
            st.write(f"**Active Subscriptions ({len(manager.channel_to_webhook)}):**")
            for ch_id, (entity, webhook_key) in manager.channel_to_webhook.items():
                title = getattr(entity, 'title', f"ID: {ch_id}")
                st.markdown(f"- **{title}** (`{ch_id}`) ➡️ `{webhook_key}`")
        except Exception as e:
            st.error(f"Error loading settings view: {e}")
    else:
        st.error("Configuration file/secrets missing.")

with col2:
    st.header("📋 Activity Log")
    
    if st.button("🔄 Refresh Logs"):
        st.rerun()
        
    log_text = "\n".join(manager.logs) if manager.logs else "No activity logs yet. Start the bot to begin tracking logs."
    st.text_area("Console output logs", value=log_text, height=450, key="logs_area")

if manager.status in ["Starting", "Need Phone", "Need Code", "Need 2FA"]:
    import time
    time.sleep(2)
    st.rerun()
