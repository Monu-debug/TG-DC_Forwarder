import os
import re
import json
import uuid
import logging
import asyncio
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
        
        self.media_upload_cache = {}
        self.active_uploads = {}
        self.active_status_messages = {}



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

    async def _download_media_robust(self, client, message, file_path: str, progress_callback=None) -> Optional[str]:
        """Downloads media from Telegram with a chunk timeout and retry-resume logic."""
        total_size = message.file.size if message.file else 0
        max_retries = 5
        
        # Ensure target directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        for attempt in range(max_retries):
            offset = 0
            if os.path.exists(file_path):
                offset = os.path.getsize(file_path)
                
            logger.info(f"Downloading media (attempt {attempt + 1}/{max_retries}) starting at offset {offset / (1024*1024):.1f}MB of {total_size / (1024*1024):.1f}MB...")
            
            mode = "ab" if offset > 0 else "wb"
            try:
                with open(file_path, mode) as fd:
                    download_iter = client.iter_download(
                        message.media, 
                        offset=offset, 
                        request_size=1024 * 1024
                    )
                    
                    while True:
                        try:
                            # 30-second timeout on fetching next chunk (prevents infinite TCP read blocks)
                            chunk = await asyncio.wait_for(download_iter.__anext__(), timeout=30.0)
                            fd.write(chunk)
                            offset += len(chunk)
                            if progress_callback:
                                progress_callback(offset, total_size)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            logger.warning(f"Download chunk timed out (30s) at offset {offset / (1024*1024):.1f}MB. Retrying...")
                            raise ConnectionError("Download chunk timeout")
                            
                # Succeeded
                return file_path
                
            except Exception as e:
                logger.error(f"Download attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
                    return None


    async def upload_to_external_storage(self, file_path: str, file_size: int) -> Optional[str]:
        """Uploads file to Catbox (<=200MB), Litterbox (200MB - 1GB) or PixelDrain (1GB - 3GB) to bypass Discord upload limits."""
        # Catbox (up to 200MB)
        if file_size <= 200 * 1024 * 1024:
            url = "https://catbox.moe/user/api.php"
            data = aiohttp.FormData()
            data.add_field("reqtype", "fileupload")
            try:
                with open(file_path, "rb") as f:
                    data.add_field("fileToUpload", f, filename=os.path.basename(file_path))
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, data=data) as resp:
                            if resp.status == 200:
                                res_url = await resp.text()
                                return res_url.strip()
                            else:
                                logger.error(f"Catbox upload failed with status {resp.status}")
            except Exception as e:
                logger.error(f"Catbox upload error: {e}")
                
        # Litterbox (200MB - 1GB)
        elif file_size <= 1024 * 1024 * 1024:
            url = "https://litterbox.catbox.moe/resources/internals/api.php"
            data = aiohttp.FormData()
            data.add_field("reqtype", "fileupload")
            data.add_field("time", "72h") # Keep file for 3 days
            try:
                with open(file_path, "rb") as f:
                    data.add_field("fileToUpload", f, filename=os.path.basename(file_path))
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, data=data) as resp:
                            if resp.status == 200:
                                res_url = await resp.text()
                                return res_url.strip()
                            else:
                                logger.error(f"Litterbox upload failed with status {resp.status}")
            except Exception as e:
                logger.error(f"Litterbox upload error: {e}")

        # PixelDrain (1GB - 3GB)
        elif file_size <= 3 * 1024 * 1024 * 1024:
            url = "https://pixeldrain.com/api/file"
            api_key = os.environ.get("PIXELDRAIN_API_KEY") or self.settings.get("pixeldrain_api_key", "")
            auth = aiohttp.BasicAuth(login="", password=api_key) if api_key else None
            
            try:
                async with aiohttp.ClientSession() as session:
                    data = aiohttp.FormData()
                    with open(file_path, "rb") as f:
                        data.add_field("file", f, filename=os.path.basename(file_path))
                        async with session.post(url, data=data, auth=auth) as resp:
                            if resp.status in (200, 201):
                                res_json = await resp.json()
                                file_id = res_json.get("id")
                                if file_id:
                                    return f"https://pixeldrain.com/api/file/{file_id}"
                            else:
                                error_text = await resp.text()
                                logger.error(f"PixelDrain upload failed with status {resp.status}: {error_text}")
                                if resp.status == 401:
                                    logger.error("Note: PixelDrain requires an API key for cloud uploads. Please set the PIXELDRAIN_API_KEY env var.")
            except Exception as e:
                logger.error(f"PixelDrain upload error: {e}")
        return None

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
        use_external_hosting = self.settings.get("use_external_hosting", True)

        if has_media:
            file_size_bytes = message.file.size if message.file else 0
            max_bytes = self.settings.get("download_max_size_mb", 25) * 1024 * 1024
            
            if file_size_bytes > max_bytes:
                if use_external_hosting and file_size_bytes <= 3 * 1024 * 1024 * 1024:
                    size_mb = file_size_bytes / (1024 * 1024)
                    cache_key = f"{entity.id}_{message.id}"
                    
                    # 1. Post a temporary status message to Discord Webhook for this specific task
                    webhook_id, webhook_token = self._parse_webhook_url(webhook_url)
                    status_msg_id = None
                    if webhook_id and webhook_token:
                        try:
                            payload = {
                                "username": username,
                                "content": f"⏳ **Forwarding large media file** (`{size_mb:.1f}MB`)... (Queued/Downloading)"
                            }
                            if avatar_url:
                                payload["avatar_url"] = avatar_url
                            async with aiohttp.ClientSession() as session:
                                async with session.post(f"{webhook_url}?wait=true", json=payload) as p_resp:
                                    if p_resp.status in (200, 201):
                                        p_data = await p_resp.json()
                                        status_msg_id = p_data.get("id")
                        except Exception as pe:
                            logger.error(f"Failed to post status placeholder to Discord: {pe}")

                    # Register this status message for real-time progress updates
                    status_info = (webhook_id, webhook_token, status_msg_id)
                    if cache_key not in self.active_status_messages:
                        self.active_status_messages[cache_key] = []
                    if status_msg_id:
                        self.active_status_messages[cache_key].append(status_info)
                        
                    uploaded_url = self.media_upload_cache.get(cache_key)
                    
                    if uploaded_url:
                        logger.info(f"Using cached upload URL for media: {uploaded_url}")
                        media_text = f"\n\n🎥 **[Play/Download Media]({uploaded_url})**\n{uploaded_url}"
                        if content_parts:
                            content_parts[-1] += media_text
                        else:
                            content_parts.append(media_text)
                            
                        # Delete the status placeholder for this task since it's already done
                        if status_msg_id and webhook_id and webhook_token:
                            try:
                                async with aiohttp.ClientSession() as session:
                                    delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{status_msg_id}"
                                    await session.delete(delete_url)
                            except Exception as de:
                                logger.error(f"Failed to delete status placeholder from Discord: {de}")
                    elif cache_key in self.active_uploads:
                        logger.info("Media is currently being uploaded by another task. Waiting for it to complete...")
                        try:
                            uploaded_url = await self.active_uploads[cache_key]
                            logger.info(f"Active upload finished. Reusing URL: {uploaded_url}")
                            if uploaded_url:
                                media_text = f"\n\n🎥 **[Play/Download Media]({uploaded_url})**\n{uploaded_url}"
                                if content_parts:
                                    content_parts[-1] += media_text
                                else:
                                    content_parts.append(media_text)
                            else:
                                warning = f"\n\n*⚠️ [Media attachment omitted: size {size_mb:.1f}MB exceeds Discord's file size limit]*"
                                if content_parts:
                                    content_parts[-1] += warning
                                else:
                                    content_parts.append(warning)
                        except Exception as e:
                            logger.error(f"Error waiting for concurrent upload task: {e}")
                            warning = f"\n\n*⚠️ [Media attachment omitted due to transfer error]*"
                            if content_parts:
                                content_parts[-1] += warning
                            else:
                                content_parts.append(warning)
                        finally:
                            # Delete the status placeholder for this task since we finished waiting
                            if status_msg_id and webhook_id and webhook_token:
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{status_msg_id}"
                                        await session.delete(delete_url)
                                except Exception as de:
                                    logger.error(f"Failed to delete status placeholder from Discord: {de}")
                    else:
                        # This task is the primary uploader
                        fut = asyncio.get_running_loop().create_future()
                        self.active_uploads[cache_key] = fut
                        
                        try:
                            logger.info(f"File size {size_mb:.1f}MB exceeds Discord limit. Downloading and uploading to external hosting...")
                            
                            # 2. Download from Telegram with progress callback
                            temp_filename = f"media_{entity.id}_{message.id}"
                            
                            # Throttled progress callback to log every 10% and update Discord in real-time
                            last_percent = [-10]
                            def progress_callback(current, total):
                                percent = int((current / total) * 100) if total else 0
                                if percent >= last_percent[0] + 10:
                                    last_percent[0] = percent
                                    logger.info(f"Telegram Download: {current / (1024*1024):.1f}MB / {total / (1024*1024):.1f}MB ({percent}%)")
                                    
                                    # Update all active Discord placeholder messages in real-time
                                    status_list = self.active_status_messages.get(cache_key, [])
                                    for wid, wtok, mid in status_list:
                                        if mid:
                                            async def update_discord_status(w_id, w_tok, m_id):
                                                try:
                                                    edit_url = f"https://discord.com/api/webhooks/{w_id}/{w_tok}/messages/{m_id}"
                                                    payload = {
                                                        "content": f"⏳ **Forwarding large media file** (`{size_mb:.1f}MB`)... Progress: **{percent}%**"
                                                    }
                                                    async with aiohttp.ClientSession() as session:
                                                        await session.patch(edit_url, json=payload)
                                                except Exception as de:
                                                    logger.error(f"Failed to edit status progress on Discord: {de}")
                                                    
                                            asyncio.create_task(update_discord_status(wid, wtok, mid))


                            media_path = await self._download_media_robust(
                                client,
                                message,
                                os.path.join(self.temp_dir, temp_filename),
                                progress_callback=progress_callback
                            )
                            
                            if media_path:
                                uploaded_url = await self.upload_to_external_storage(media_path, file_size_bytes)
                                if uploaded_url:
                                    logger.info(f"Successfully uploaded media to external host: {uploaded_url}")
                                    self.media_upload_cache[cache_key] = uploaded_url
                                    fut.set_result(uploaded_url)
                                    # Append direct link to let Discord embed it as a playable video
                                    media_text = f"\n\n🎥 **[Play/Download Media]({uploaded_url})**\n{uploaded_url}"
                                    if content_parts:
                                        content_parts[-1] += media_text
                                    else:
                                        content_parts.append(media_text)
                                else:
                                    fut.set_result(None)
                                    # External hosting failed, show fallback warning
                                    warning = f"\n\n*⚠️ [Media attachment omitted: size {size_mb:.1f}MB exceeds Discord's file size limit]*"
                                    if content_parts:
                                        content_parts[-1] += warning
                                    else:
                                        content_parts.append(warning)
                                
                                # Cleanup local file
                                if os.path.exists(media_path):
                                    try:
                                        os.remove(media_path)
                                    except Exception:
                                        pass
                                media_path = None
                            else:
                                fut.set_result(None)
                                has_media = False

                            # 3. Delete the temporary status message from Discord Webhook for the primary uploader
                            if status_msg_id and webhook_id and webhook_token:
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{status_msg_id}"
                                        await session.delete(delete_url)
                                except Exception as de:
                                    logger.error(f"Failed to delete status placeholder from Discord: {de}")
                        except Exception as exc:
                            fut.set_exception(exc)
                            raise exc
                        finally:
                            self.active_uploads.pop(cache_key, None)
                            self.active_status_messages.pop(cache_key, None)
                    
                    has_media = False
                else:
                    warning = f"\n\n*⚠️ [Media attachment omitted: size {file_size_bytes / (1024*1024):.1f}MB exceeds local hosting or external limits]*"
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
