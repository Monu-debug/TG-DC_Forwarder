import os
import sys
import yaml
import asyncio
import logging
from telethon import TelegramClient, events
from forwarder import DiscordForwarder

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

async def main():
    logger.info("Starting Telegram to Discord Forwarder...")
    config = load_config()
    
    telegram_cfg = config.get("telegram", {})
    api_id = telegram_cfg.get("api_id")
    api_hash = telegram_cfg.get("api_hash")
    phone = telegram_cfg.get("phone")
    
    if not api_id or not api_hash:
        logger.error("Telegram api_id and api_hash must be set in config.yaml!")
        sys.exit(1)
        
    mappings = config.get("mappings", [])
    if not mappings:
        logger.error("No channel mappings defined in config.yaml!")
        sys.exit(1)

    # Initialize Discord Forwarder
    forwarder = DiscordForwarder(config)
    
    # Initialize Telethon Client
    session_name = "telegram_forwarder_session"
    client = TelegramClient(session_name, api_id, api_hash)
    
    logger.info("Connecting to Telegram...")
    await client.start(phone=phone)
    logger.info("Successfully authenticated with Telegram.")
    
    # Resolve all channels to get IDs and verify access
    channel_to_webhook = {}
    for mapping in mappings:
        channel_name = mapping.get("telegram_channel")
        webhook_key = mapping.get("webhook_key")
        
        if not channel_name or not webhook_key:
            logger.warning(f"Skipping invalid mapping: {mapping}")
            continue
            
        try:
            # If the user put a string representing a number, convert it
            if isinstance(channel_name, str) and (channel_name.startswith('-') or channel_name.isdigit()):
                try:
                    channel_name = int(channel_name)
                except ValueError:
                    pass

            entity = await client.get_entity(channel_name)
            channel_to_webhook[entity.id] = (entity, webhook_key)
            logger.info(f"Mapped Channel: '{entity.title}' ({entity.id}) -> Webhook key: '{webhook_key}'")
        except Exception as e:
            logger.error(f"Failed to resolve channel '{channel_name}': {e}")
            logger.error("Please verify that the name/ID is correct and your Telegram account has access to it.")
            
    if not channel_to_webhook:
        logger.error("No Telegram channels could be resolved! Exiting.")
        await client.disconnect()
        sys.exit(1)
        
    # Register listeners
    channel_ids = list(channel_to_webhook.keys())
    
    @client.on(events.NewMessage(chats=channel_ids))
    async def handler(event):
        chat_id = event.chat_id
        
        # Telethon's event.chat_id is normally the raw channel/chat ID (int)
        # Look up key directly or check if we need to resolve it
        mapped_item = channel_to_webhook.get(chat_id)
        if not mapped_item:
            # Fallback check for Peer objects or absolute value match
            for resolved_id, item in channel_to_webhook.items():
                # Compare absolute values since Telethon sometimes handles negative/positive signs differently
                # depending on whether it's a channel, group, or user ID
                if abs(resolved_id) == abs(chat_id) or str(resolved_id) in str(chat_id):
                    mapped_item = item
                    break
            
        if not mapped_item:
            logger.debug(f"Received message from unmapped chat ID {chat_id}, ignoring.")
            return
            
        entity, webhook_key = mapped_item
        logger.info(f"New message detected in '{entity.title}' ({chat_id}). Forwarding...")
        await forwarder.forward_message(client, entity, event.message, webhook_key)

    logger.info("Listening for new messages... Press Ctrl+C to exit.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
