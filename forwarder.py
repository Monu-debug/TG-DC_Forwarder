import os
import re
import json
import uuid
import logging
import aiohttp
from typing import Dict, Optional, Tuple, List

logger = logging.getLogger("telegram_discord_forwarder")

class DiscordForwarder:
    def __init__(self, config: dict):
        self.config = config
        self.settings = config.get("settings", {})
        self.temp_dir = self.settings.get("temp_dir", "./temp")
        self.use_telegram_profile = self.settings.get("use_telegram_profile", True)
        
        # Files for caching and dynamic settings
        self.cache_file = os.path.join(self.temp_dir, "avatar_cache.json")
        self.dynamic_config_file = "dynamic_mappings.json"
        
        # Ensure temp directory exists
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Initialize active webhooks and mappings
        self.webhooks = dict(config.get("discord_webhooks", {}))
        self.mappings = list(config.get("mappings", []))
        
        # Load dynamic configurations
        self.dynamic_config = self._load_dynamic_config()
        self._merge_dynamic_config()
        
        # Load avatar cache
        self.avatar_cache = self._load_cache()

    def _load_dynamic_config(self) -> dict:
        if os.path.exists(self.dynamic_config_file):
            try:
                with open(self.dynamic_config_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading dynamic mappings: {e}")
        return {"discord_webhooks": {}, "mappings": []}

    def _save_dynamic_config(self):
        try:
            with open(self.dynamic_config_file, "w") as f:
                json.dump(self.dynamic_config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving dynamic mappings: {e}")

    def _merge_dynamic_config(self):
        # Merge webhooks
        for key, url in self.dynamic_config.get("discord_webhooks", {}).items():
            self.webhooks[key] = url
        
        # Merge mappings (avoid duplicates)
        base_channels = {str(m.get("telegram_channel")) for m in self.mappings}
        for m in self.dynamic_config.get("mappings", []):
            ch = str(m.get("telegram_channel"))
            if ch not in base_channels:
                self.mappings.append(m)

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

    def add_mapping(self, telegram_channel: str, webhook_url: str) -> str:
        """Adds a new channel mapping dynamically and saves to dynamic_mappings.json"""
        # Find if webhook URL already exists under a key
        webhook_key = None
        for key, url in self.webhooks.items():
            if url == webhook_url:
                webhook_key = key
                break
        
        if not webhook_key:
            # Generate unique dynamic key
            webhook_key = f"dyn_{uuid.uuid4().hex[:8]}"
            self.webhooks[webhook_key] = webhook_url
            self.dynamic_config["discord_webhooks"][webhook_key] = webhook_url

        # Remove existing mapping for this channel to prevent duplicates
        self.remove_mapping(telegram_channel)

        # Append new mapping
        mapping = {
            "telegram_channel": str(telegram_channel),
            "webhook_key": webhook_key
        }
        self.mappings.append(mapping)
        self.dynamic_config["mappings"].append(mapping)
        
        self._save_dynamic_config()
        logger.info(f"Added dynamic mapping: Telegram {telegram_channel} -> Discord Key {webhook_key}")
        return webhook_key

    def remove_mapping(self, telegram_channel: str) -> bool:
        """Removes a channel mapping dynamically and saves to dynamic_mappings.json"""
        target = str(telegram_channel)
        
        # Check active mappings
        has_base = any(str(m.get("telegram_channel")) == target for m in self.config.get("mappings", []))
        if has_base:
            logger.warning(f"Cannot remove channel '{telegram_channel}' from Discord command because it is defined in config.yaml / Streamlit secrets. Please edit your config file/secrets to remove it.")
            # Remove it from active run mappings anyway
            self.mappings = [m for m in self.mappings if str(m.get("telegram_channel")) != target]
            return True
            
        # Check dynamic config mappings
        old_len = len(self.dynamic_config["mappings"])
        self.dynamic_config["mappings"] = [m for m in self.dynamic_config["mappings"] if str(m.get("telegram_channel")) != target]
        
        # Update active run mappings
        self.mappings = [m for m in self.mappings if str(m.get("telegram_channel")) != target]
        
        if len(self.dynamic_config["mappings"]) < old_len:
            self._save_dynamic_config()
            logger.info(f"Removed dynamic mapping for Telegram channel: {telegram_channel}")
            return True
        return False

    def get_all_mappings(self) -> List[dict]:
        return self.mappings

    def _parse_webhook_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        match = re.search(r"discord\.com/api/webhooks/(\d+)/([\w-]+)", url)
        if match:
            return match.group(1), match.group(2)
        return None, None

    async def get_channel_avatar(self, client, entity, webhook_url: str) -> Optional[str]:
        channel_id = str(entity.id)
        if channel_id in self.avatar_cache:
            return self.avatar_cache[channel_id]

        logger.info(f"Retrieving profile photo for channel {entity.title} ({channel_id})...")
        photo_path = os.path.join(self.temp_dir, f"avatar_{channel_id}.jpg")
        
        try:
            path = await client.download_profile_photo(entity, file=photo_path)
            if not path:
                logger.info(f"No profile photo found for channel {entity.title}")
                return None

            webhook_id, webhook_token = self._parse_webhook_url(webhook_url)
            if not webhook_id or not webhook_token:
                return None

            async with aiohttp.ClientSession() as session:
                url = f"{webhook_url}?wait=true"
                data = aiohttp.FormData()
                
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
                            
                            # Clean up
                            msg_id = resp_data.get("id")
                            if msg_id:
                                delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{msg_id}"
                                async with session.delete(delete_url) as del_resp:
                                    pass
                            return cdn_url
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
        return text if text else ""

    async def forward_message(self, client, entity, message, webhook_key: str):
        webhook_url = self.webhooks.get(webhook_key)
        if not webhook_url:
            logger.error(f"No webhook URL found for key '{webhook_key}'")
            return

        username = entity.title if hasattr(entity, 'title') else "Telegram Channel"
        avatar_url = None
        if self.use_telegram_profile:
            avatar_url = await self.get_channel_avatar(client, entity, webhook_url)

        content = self.clean_markdown(message.text)
        
        # Chunk text if exceeds Discord length limits
        content_parts = []
        if len(content) > 1950:
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

        media_path = None
        file_name = None
        has_media = message.media is not None and self.settings.get("forward_media", True)

        if has_media:
            file_size_bytes = message.file.size if message.file else 0
            max_bytes = self.settings.get("download_max_size_mb", 25) * 1024 * 1024
            if file_size_bytes > max_bytes:
                warning = f"\n\n*⚠️ [Media attachment omitted: size {file_size_bytes / (1024*1024):.1f}MB exceeds Discord's file size limit]*"
                if content_parts:
                    content_parts[-1] += warning
                else:
                    content_parts.append(warning)
                has_media = False
            else:
                temp_filename = f"media_{entity.id}_{message.id}"
                media_path = await client.download_media(message, file=os.path.join(self.temp_dir, temp_filename))
                if media_path:
                    file_name = os.path.basename(media_path)
                else:
                    has_media = False

        # Post to Discord
        async with aiohttp.ClientSession() as session:
            try:
                for i, part in enumerate(content_parts):
                    is_last_chunk = (i == len(content_parts) - 1)
                    data = aiohttp.FormData()
                    
                    payload = {
                        "username": username
                    }
                    if avatar_url:
                        payload["avatar_url"] = avatar_url

                    if part:
                        payload["content"] = part
                    elif not has_media:
                        payload["content"] = "[Empty message]"

                    data.add_field("payload_json", json.dumps(payload))

                    opened_file = None
                    if is_last_chunk and has_media and media_path and os.path.exists(media_path):
                        opened_file = open(media_path, "rb")
                        data.add_field("file", opened_file, filename=file_name)

                    async with session.post(webhook_url, data=data) as resp:
                        if resp.status not in (200, 204):
                            error_text = await resp.text()
                            logger.error(f"Failed webhook forward. Status: {resp.status}, Response: {error_text}")
                    
                    if opened_file:
                        opened_file.close()
            except Exception as e:
                logger.error(f"Error posting webhook: {e}", exc_info=True)
            finally:
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except Exception:
                        pass
