import os
import sys
import yaml
import asyncio
import logging
from telethon import TelegramClient, events
from forwarder import DiscordForwarder
import discord
from discord.ext import commands
from discord import app_commands

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("forwarder.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("telegram_discord_forwarder")

# Global reference objects
tg_client = None
forwarder_instance = None
channel_to_webhook = {}
active_telegram_handler = None

def load_config() -> dict:
    config_path = "config.yaml"
    if not os.path.exists(config_path):
        logger.error(f"Configuration file '{config_path}' not found!")
        sys.exit(1)
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Error reading config.yaml: {e}")
        sys.exit(1)

# Initialize Discord Command Bot
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

@discord_bot.event
async def on_ready():
    logger.info(f"Discord Command Bot connected as {discord_bot.user}")
    try:
        logger.info("Synchronizing slash commands globally...")
        synced = await discord_bot.tree.sync()
        logger.info(f"Successfully synchronized {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to synchronize slash commands: {e}")

# Helper helper to generate status embed
async def build_status_embed():
    global tg_client, channel_to_webhook
    tg_connected = tg_client.is_connected() if tg_client else False
    tg_auth = await tg_client.is_user_authorized() if tg_client and tg_connected else False
    
    embed = discord.Embed(
        title="🔄 Forwarder Status Report",
        color=discord.Color.green() if (tg_connected and tg_auth) else discord.Color.red()
    )
    embed.add_field(name="Telegram Service", value="Connected & Authorized ✅" if (tg_connected and tg_auth) else "Disconnected ❌", inline=True)
    embed.add_field(name="Discord Command Bot", value="Online ✅", inline=True)
    embed.add_field(name="Active Mapped Channels", value=f"{len(channel_to_webhook)} channel(s)", inline=False)
    return embed

# Helper helper to generate mappings list embed
def build_list_embed():
    global channel_to_webhook
    if not channel_to_webhook:
        return None
        
    embed = discord.Embed(title="📋 Active Channel Mappings", color=discord.Color.blue())
    for ch_id, (entity, webhook_key) in channel_to_webhook.items():
        title = getattr(entity, 'title', f"ID: {ch_id}")
        webhook_url = forwarder_instance.webhooks.get(webhook_key, "Unknown URL")
        masked_url = webhook_url[:45] + "..." if len(webhook_url) > 45 else webhook_url
        embed.add_field(
            name=f"📢 {title}",
            value=f"• **Telegram ID:** `{ch_id}`\n• **Webhook Key:** `{webhook_key}`\n• **Webhook URL:** `{masked_url}`",
            inline=False
        )
    return embed

async def get_or_create_webhook(channel: discord.TextChannel) -> str:
    """Attempts to find or create a webhook for the specified channel."""
    # Check permissions
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
            
    # Create new webhook if not found
    new_wh = await channel.create_webhook(name="Telegram Forwarder Link")
    return new_wh.url

async def resolve_discord_channel(guild: discord.Guild, input_str: str) -> discord.TextChannel:
    """Resolves a channel from a string (ID, Mention, or Name)."""
    # Try mention or ID format
    clean_id = input_str.strip("<#> ")
    if clean_id.isdigit():
        channel = guild.get_channel(int(clean_id))
        if isinstance(channel, discord.TextChannel):
            return channel
            
    # Try name matching
    channel = discord.utils.get(guild.text_channels, name=input_str.lstrip('#'))
    if channel:
        return channel
        
    raise ValueError(f"Could not find a text channel named or matching '{input_str}' in this server.")

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
    """Maps a new Telegram channel.
    Usage: !add <telegram_channel_id_or_user> <discord_channel_name_or_mention_or_webhook_url>
    """
    global tg_client, forwarder_instance, channel_to_webhook
    
    webhook_url = None
    
    if discord_target.startswith("https://discord.com/api/webhooks/"):
        webhook_url = discord_target
    else:
        # Resolve to channel and create/fetch webhook
        try:
            channel = await resolve_discord_channel(ctx.guild, discord_target)
            webhook_url = await get_or_create_webhook(channel)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            return
            
    await ctx.send(f"⏳ Attempting to resolve and subscribe to Telegram channel `{telegram_channel}`...")
    
    resolved = telegram_channel
    if telegram_channel.startswith('-') or telegram_channel.isdigit():
        try:
            resolved = int(telegram_channel)
        except ValueError:
            pass
            
    try:
        entity = await tg_client.get_entity(resolved)
        webhook_key = forwarder_instance.add_mapping(entity.id, webhook_url)
        channel_to_webhook[entity.id] = (entity, webhook_key)
        await update_tg_listeners()
        
        embed = discord.Embed(title="✅ Mapping Added Successfully", color=discord.Color.green())
        embed.add_field(name="Telegram Channel", value=f"**{entity.title}** (`{entity.id}`)", inline=False)
        embed.add_field(name="Target Webhook Key", value=f"`{webhook_key}`", inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to add mapping: {e}")
        await ctx.send(f"❌ Error: Could not resolve channel `{telegram_channel}`. Reason: `{e}`")

@discord_bot.command(name="remove")
async def cmd_remove(ctx, telegram_channel: str):
    """Removes a mapped Telegram channel. Usage: !remove <id_or_title>"""
    matched_id = None
    matched_entity = None
    target_str = str(telegram_channel)
    
    for ch_id, (entity, _) in channel_to_webhook.items():
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
    success = forwarder_instance.remove_mapping(matched_id)
    if success:
        channel_to_webhook.pop(matched_id, None)
        await update_tg_listeners()
        await ctx.send(f"✅ Successfully removed forwarding mapping for **{title}** (`{matched_id}`).")
    else:
        await ctx.send(f"❌ Error: Failed to remove mapping. Config-defined channels must be removed directly inside settings.")

# --- SLASH COMMANDS ---

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

@discord_bot.tree.command(name="add", description="Maps a new Telegram channel to a Discord Channel or Webhook")
@app_commands.describe(
    telegram_channel="Telegram channel username or numeric ID",
    discord_target="Discord channel name, ID, mention, or a raw Webhook URL"
)
async def slash_add(interaction: discord.Interaction, telegram_channel: str, discord_target: str):
    await interaction.response.defer(ephemeral=False)
    
    webhook_url = None
    if discord_target.startswith("https://discord.com/api/webhooks/"):
        webhook_url = discord_target
    else:
        try:
            channel = await resolve_discord_channel(interaction.guild, discord_target)
            webhook_url = await get_or_create_webhook(channel)
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
        entity = await tg_client.get_entity(resolved)
        webhook_key = forwarder_instance.add_mapping(entity.id, webhook_url)
        channel_to_webhook[entity.id] = (entity, webhook_key)
        await update_tg_listeners()
        
        embed = discord.Embed(title="✅ Mapping Added Successfully", color=discord.Color.green())
        embed.add_field(name="Telegram Channel", value=f"**{entity.title}** (`{entity.id}`)", inline=False)
        embed.add_field(name="Target Webhook Key", value=f"`{webhook_key}`", inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to add mapping via slash command: {e}")
        await interaction.followup.send(f"❌ Error: Could not resolve channel `{telegram_channel}`. Reason: `{e}`")

@discord_bot.tree.command(name="remove", description="Removes a mapped Telegram channel mapping")
@app_commands.describe(
    telegram_channel="Telegram channel username, ID, or title to remove"
)
async def slash_remove(interaction: discord.Interaction, telegram_channel: str):
    await interaction.response.defer(ephemeral=False)
    matched_id = None
    matched_entity = None
    target_str = str(telegram_channel)
    
    for ch_id, (entity, _) in channel_to_webhook.items():
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
        await interaction.followup.send(f"❌ Error: Could not find '{telegram_channel}' in active mappings.")
        return

    title = matched_entity.title if matched_entity else f"ID: {matched_id}"
    success = forwarder_instance.remove_mapping(matched_id)
    if success:
        channel_to_webhook.pop(matched_id, None)
        await update_tg_listeners()
        await interaction.followup.send(f"✅ Successfully removed forwarding mapping for **{title}** (`{matched_id}`).")
    else:
        await interaction.followup.send(f"❌ Error: Failed to remove mapping. Config-defined channels must be removed directly inside settings.")

# --- UTILS AND DRIVER ---

async def update_tg_listeners():
    global tg_client, active_telegram_handler, channel_to_webhook
    
    if active_telegram_handler:
        try:
            tg_client.remove_event_handler(active_telegram_handler)
        except Exception:
            pass
            
    channel_ids = list(channel_to_webhook.keys())
    if not channel_ids:
        active_telegram_handler = None
        return

    async def handler(event):
        chat_id = event.chat_id
        mapped_item = channel_to_webhook.get(chat_id)
        if not mapped_item:
            for resolved_id, item in channel_to_webhook.items():
                if abs(resolved_id) == abs(chat_id) or str(resolved_id) in str(chat_id):
                    mapped_item = item
                    break
        if not mapped_item:
            return
            
        entity, webhook_key = mapped_item
        logger.info(f"New message in '{entity.title}' ({chat_id}). Forwarding...")
        await forwarder_instance.forward_message(tg_client, entity, event.message, webhook_key)

    tg_client.add_event_handler(handler, events.NewMessage(chats=channel_ids))
    active_telegram_handler = handler
    logger.info(f"Telethon registered message listeners for {len(channel_ids)} channels.")

async def main():
    global tg_client, forwarder_instance, channel_to_webhook
    logger.info("Starting Telegram & Discord Integrated Forwarder...")
    
    config = load_config()
    telegram_cfg = config.get("telegram", {})
    api_id = telegram_cfg.get("api_id")
    api_hash = telegram_cfg.get("api_hash")
    phone = telegram_cfg.get("phone")
    
    discord_cfg = config.get("discord", {})
    discord_token = discord_cfg.get("bot_token")
    
    if not api_id or not api_hash:
        logger.error("Telegram api_id and api_hash must be set in config.yaml!")
        sys.exit(1)
        
    forwarder_instance = DiscordForwarder(config)
    session_name = "telegram_forwarder_session"
    tg_client = TelegramClient(session_name, api_id, api_hash)
    
    logger.info("Connecting to Telegram...")
    await tg_client.start(phone=phone)
    logger.info("Successfully authenticated with Telegram.")
    
    for mapping in forwarder_instance.get_all_mappings():
        channel_name = mapping.get("telegram_channel")
        webhook_key = mapping.get("webhook_key")
        if not channel_name or not webhook_key:
            continue
            
        try:
            if isinstance(channel_name, str) and (channel_name.startswith('-') or channel_name.isdigit()):
                channel_name = int(channel_name)
            entity = await tg_client.get_entity(channel_name)
            channel_to_webhook[entity.id] = (entity, webhook_key)
            logger.info(f"Mapped Channel: '{entity.title}' ({entity.id}) -> Webhook: '{webhook_key}'")
        except Exception as e:
            logger.error(f"Failed to resolve channel '{channel_name}': {e}")
            
    await update_tg_listeners()
    
    discord_task = None
    if discord_token:
        logger.info("Starting Discord command bot...")
        discord_task = asyncio.create_task(discord_bot.start(discord_token))
    else:
        logger.warning("No discord.bot_token found. Running in Webhook-only mode.")
        
    try:
        logger.info("Listening for new messages...")
        await tg_client.run_until_disconnected()
    finally:
        if discord_task:
            logger.info("Stopping Discord command bot...")
            await discord_bot.close()
            await discord_task
        logger.info("Application shut down.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
