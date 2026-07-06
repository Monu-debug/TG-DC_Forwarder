import os
import re
import json
import logging
import aiohttp
from typing import Dict, Optional, Tuple

logger = logging.getLogger("telegram_discord_forwarder")

class DiscordForwarder:
    def __init__(self, config: dict):
        self.config = config
        self.webhooks = config.get("discord_webhooks", {})
        self.settings = config.get("settings", {})
        self.temp_dir = self.settings.get("temp_dir", "./temp")
        self.use_telegram_profile = self.settings.get("use_telegram_profile", True)
        self.cache_file = os.path.join(self.temp_dir, "avatar_cache.json")
        
        # Ensure temp directory exists
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Load avatar cache (maps channel ID string to Discord CDN URL)
        self.avatar_cache = self._load_cache()

    def _load_cache(self) -> Dict[str, str]:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading avatar cache: {e}")
        return {}

    def _save_cache(self):
        try:
            with open(self.cache_file, "w") as f:
                json.dump(self.avatar_cache, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving avatar cache: {e}")

    def _parse_webhook_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract webhook ID and token from URL."""
        match = re.search(r"discord\.com/api/webhooks/(\d+)/([\w-]+)", url)
        if match:
            return match.group(1), match.group(2)
        return None, None

    async def get_channel_avatar(self, client, entity, webhook_url: str) -> Optional[str]:
        """Gets or uploads the channel profile photo to use as the Discord avatar."""
        channel_id = str(entity.id)
        
        # Check cache first
        if channel_id in self.avatar_cache:
            return self.avatar_cache[channel_id]

        logger.info(f"Retrieving profile photo for channel {entity.title} ({channel_id})...")
        photo_path = os.path.join(self.temp_dir, f"avatar_{channel_id}.jpg")
        
        try:
            # Download profile photo from Telegram
            path = await client.download_profile_photo(entity, file=photo_path)
            if not path:
                logger.info(f"No profile photo found for channel {entity.title}")
                return None

            # Upload photo to Discord webhook to get CDN URL
            webhook_id, webhook_token = self._parse_webhook_url(webhook_url)
            if not webhook_id or not webhook_token:
                return None

            # Send attachment to webhook and wait for response to get URL
            async with aiohttp.ClientSession() as session:
                url = f"{webhook_url}?wait=true"
                data = aiohttp.FormData()
                
                # Payload metadata
                payload = {
                    "username": f"System - {entity.title}",
                    "content": f"🔄 Synchronizing profile image for {entity.title}..."
                }
                data.add_field("payload_json", json.dumps(payload))
                
                with open(photo_path, "rb") as f:
                    data.add_field("file", f.read(), filename="avatar.jpg", content_type="image/jpeg")

                async with session.post(url, data=data) as resp:
                    if resp.status in (200, 201):
                        resp_data = await resp.json()
                        attachments = resp_data.get("attachments", [])
                        if attachments:
                            cdn_url = attachments[0].get("url")
                            self.avatar_cache[channel_id] = cdn_url
                            self._save_cache()
                            logger.info(f"Successfully cached avatar for {entity.title}: {cdn_url}")
                            
                            # Clean up the setup message to keep the channel clean
                            msg_id = resp_data.get("id")
                            if msg_id:
                                delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{msg_id}"
                                async with session.delete(delete_url) as del_resp:
                                    if del_resp.status == 204:
                                        logger.debug("Successfully cleaned up avatar sync message.")
                                    else:
                                        logger.warning(f"Failed to delete setup message: {del_resp.status}")
                            
                            return cdn_url
                    else:
                        logger.error(f"Failed to upload profile photo to Discord: {resp.status}")
        except Exception as e:
            logger.error(f"Error handling avatar synchronization: {e}")
        finally:
            if os.path.exists(photo_path):
                try:
                    os.remove(photo_path)
                except Exception:
                    pass
        return None

    def clean_markdown(self, text: str) -> str:
        """Converts some Telegram entities and handles text replacements if necessary."""
        if not text:
            return ""
        # Discord doesn't support nested markdown links the same way, but supports standard [text](url).
        # We also want to replace @mentions or links if we need to, but general markdown is fine.
        return text

    async def forward_message(self, client, entity, message, webhook_key: str):
        """Processes a Telegram message and forwards it to the mapped Discord Webhook."""
        webhook_url = self.webhooks.get(webhook_key)
        if not webhook_url:
            logger.error(f"No webhook URL found for key '{webhook_key}'")
            return

        # Prepare branding
        username = entity.title if hasattr(entity, 'title') else "Telegram Channel"
        avatar_url = None
        if self.use_telegram_profile:
            avatar_url = await self.get_channel_avatar(client, entity, webhook_url)

        # Prepare message content
        content = self.clean_markdown(message.text)
        
        # Max Discord text message length is 2000 characters
        # If text is too long, we will split it
        content_parts = []
        if len(content) > 1950:
            # Simple chunking by lines/paragraphs
            paragraphs = content.split('\n')
            current_part = ""
            for p in paragraphs:
                if len(current_part) + len(p) + 1 > 1900:
                    content_parts.append(current_part)
                    current_part = p
                else:
                    current_part = f"{current_part}\n{p}" if current_part else p
            if current_part:
                content_parts.append(current_part)
        else:
            if content:
                content_parts.append(content)

        # Handle media attachments
        media_path = None
        file_name = None
        has_media = message.media is not None and self.settings.get("forward_media", True)

        if has_media:
            # Check document/file size before downloading
            file_size_bytes = 0
            if message.file:
                file_size_bytes = message.file.size
            
            max_bytes = self.settings.get("download_max_size_mb", 25) * 1024 * 1024
            if file_size_bytes > max_bytes:
                warning = f"\n\n*⚠️ [Media attachment omitted: size {file_size_bytes / (1024*1024):.1f}MB exceeds Discord's file size limit]*"
                if content_parts:
                    content_parts[-1] += warning
                else:
                    content_parts.append(warning)
                has_media = False
            else:
                logger.info(f"Downloading media for message {message.id} ({file_size_bytes / 1024:.1f} KB)...")
                # Add unique suffix to avoid collision
                temp_filename = f"media_{entity.id}_{message.id}"
                media_path = await client.download_media(message, file=os.path.join(self.temp_dir, temp_filename))
                if media_path:
                    file_name = os.path.basename(media_path)
                    # If telethon appends extension, keep it
                    logger.info(f"Downloaded media to {media_path}")
                else:
                    logger.warning("Failed to download media.")
                    has_media = False

        # Send to Discord Webhook
        async with aiohttp.ClientSession() as session:
            try:
                # If there are multiple parts (due to long text), send them in sequence
                for i, part in enumerate(content_parts):
                    # Attach the file only on the last chunk (or first if it's the only one)
                    is_last_chunk = (i == len(content_parts) - 1)
                    
                    data = aiohttp.FormData()
                    payload = {
                        "username": username
                    }
                    if avatar_url:
                        payload["avatar_url"] = avatar_url

                    # Add text content if present
                    if part:
                        payload["content"] = part
                    elif not has_media:
                        # Discord webhooks require content OR file
                        payload["content"] = "[Empty message or unsupported content]"

                    data.add_field("payload_json", json.dumps(payload))

                    # Attach file if last chunk and media downloaded successfully
                    opened_file = None
                    if is_last_chunk and has_media and media_path and os.path.exists(media_path):
                        opened_file = open(media_path, "rb")
                        data.add_field("file", opened_file, filename=file_name)

                    async with session.post(webhook_url, data=data) as resp:
                        if resp.status not in (200, 204):
                            error_text = await resp.text()
                            logger.error(f"Failed to forward message chunk {i} to Discord webhook. Status: {resp.status}, Response: {error_text}")
                        else:
                            logger.info(f"Message chunk {i} successfully forwarded to Discord.")
                    
                    if opened_file:
                        opened_file.close()

            except Exception as e:
                logger.error(f"Error posting to Discord webhook: {e}", exc_info=True)
            finally:
                # Clean up downloaded file
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                        logger.info(f"Cleaned up temporary media file: {media_path}")
                    except Exception as clean_err:
                        logger.warning(f"Error removing temporary media file {media_path}: {clean_err}")
