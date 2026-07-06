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
        
        self.logs = []
        
    def log(self, message: str, level=logging.INFO):
        # Format the log line
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} [{logging.getLevelName(level)}] {message}"
        
        # Log to Python logging system
        if level == logging.ERROR:
            logger.error(message)
        elif level == logging.WARNING:
            logger.warning(message)
        else:
            logger.info(message)
            
        # Store in list for GUI display
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

    async def _main_loop(self):
        config = None
        
        # Try loading from Streamlit secrets first (for cloud hosting security)
        try:
            if len(st.secrets) > 0 and "telegram" in st.secrets:
                self.log("Loading configuration from Streamlit Cloud secrets...")
                # Convert st.secrets (which is a Streamlit object) to a standard dict
                config = st.secrets.to_dict()
        except Exception as e:
            self.log(f"Streamlit secrets not configured or readable: {e}", logging.DEBUG)

        # Fallback to local config.yaml
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
        
        if not api_id or not api_hash:
            self.status = "Error"
            self.error_msg = "api_id and api_hash must be set in config.yaml!"
            self.log(self.error_msg, logging.ERROR)
            return

        # Initialize Telethon Client inside the background thread loop
        # Note: Session file is saved in the working directory
        self.client = TelegramClient("telegram_forwarder_session", api_id, api_hash)
        self.forwarder = DiscordForwarder(config)

        self.log("Connecting to Telegram...")
        await self.client.connect()
        
        # Check authentication status
        if not await self.client.is_user_authorized():
            self.log("User session not authorized. Verification needed.")
            
            # 1. Obtain Phone Number
            phone_val = telegram_cfg.get("phone")
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
        
        # Resolve mappings
        mappings = config.get("mappings", [])
        channel_to_webhook = {}
        for mapping in mappings:
            channel_name = mapping.get("telegram_channel")
            webhook_key = mapping.get("webhook_key")
            
            try:
                # Convert string number representation to int
                if isinstance(channel_name, str) and (channel_name.startswith('-') or channel_name.isdigit()):
                    try:
                        channel_name = int(channel_name)
                    except ValueError:
                        pass
                
                entity = await self.client.get_entity(channel_name)
                channel_to_webhook[entity.id] = (entity, webhook_key)
                self.log(f"Subscribed Channel: '{entity.title}' ({entity.id}) -> Webhook Key: '{webhook_key}'")
            except Exception as e:
                self.log(f"Failed to resolve Telegram channel '{channel_name}': {e}", logging.ERROR)

        if not channel_to_webhook:
            self.status = "Error"
            self.error_msg = "Could not resolve any Telegram channels. Check logs."
            self.log(self.error_msg, logging.ERROR)
            await self.client.disconnect()
            return

        # Register message listener
        channel_ids = list(channel_to_webhook.keys())
        
        @self.client.on(events.NewMessage(chats=channel_ids))
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
            self.log(f"Detected new message in '{entity.title}'. Forwarding...")
            await self.forwarder.forward_message(self.client, entity, event.message, webhook_key)

        self.status = "Running"
        self.log("Forwarder is actively listening for new messages. Web app is operational!")
        await self.client.run_until_disconnected()

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

# --- WEB APP INTERFACE ---

# Header Section
st.title("🔄 Telegram to Discord Channel Forwarder")
st.markdown("Use this web interface to run, monitor, and configure your message forwarder.")

# Status Bar
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

# Layout Setup
col1, col2 = st.columns([1, 2])

with col1:
    st.header("🎛️ Control Panel")
    
    # Start / Stop Buttons
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

    # Interactive Authentication Dialogs
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

    # View Configurations
    st.subheader("📝 Mapped Configurations")
    if os.path.exists("config.yaml"):
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            st.write("**Telegram Account:**", cfg.get("telegram", {}).get("phone", "N/A"))
            
            mappings = cfg.get("mappings", [])
            st.write(f"**Mapped Channels ({len(mappings)}):**")
            for m in mappings:
                st.markdown(f"- `{m.get('telegram_channel')}` ➡️ `{m.get('webhook_key')}`")
        except Exception as e:
            st.error(f"Error loading settings view: {e}")
    else:
        st.error("config.yaml not found.")

with col2:
    st.header("📋 Activity Log")
    
    # Auto refresh trigger button
    if st.button("🔄 Refresh Logs"):
        st.rerun()
        
    # Log display text box
    log_text = "\n".join(manager.logs) if manager.logs else "No activity logs yet. Start the bot to begin tracking logs."
    st.text_area("Console output logs", value=log_text, height=450, key="logs_area")

# Auto-refresh mechanism (checks every 5 seconds if bot is starting up or in transit)
if manager.status in ["Starting", "Need Phone", "Need Code", "Need 2FA"]:
    # Simple delay mechanism to auto refresh UI when changing auth status
    import time
    time.sleep(2)
    st.rerun()
