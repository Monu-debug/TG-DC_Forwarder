# Telegram to Discord Message Forwarder (Integrated Webhook & Bot Version)

This script forwards messages from multiple Telegram channels to specified Discord channels. 

It uses **both a Discord Webhook and a Discord Command Bot** in parallel:
1. **Discord Webhooks** forward messages. This retains **dynamic branding**—the messages appear in Discord with the actual Telegram channel's name and profile photo.
2. **Discord Bot** listens for commands directly inside Discord (like `!add`, `!remove`, `!list`, `!status`), allowing you to manage mappings dynamically without restarting or editing config files.

---

## Prerequisites

- **Python 3.8+** (your current system has Python 3.14.6)
- A Telegram account
- A Discord server where you have permission to manage webhooks and invite bots.

---

## Setup Instructions

### 1. Clone/Locate the Project
The project files are located in:
`C:\Users\Monu\.gemini\antigravity\scratch\telegram-discord-forwarder\`

### 2. Install Dependencies
Open your terminal (PowerShell, Command Prompt, or Bash) in the project directory and run:

```bash
# Optional but recommended: Create a virtual environment
python -m venv venv
venv\Scripts\activate      # On Windows
source venv/bin/activate   # On Linux/macOS

# Install required packages
python -m pip install -r requirements.txt
```

### 3. Retrieve Telegram API Credentials
1. Log in to your Telegram account at [my.telegram.org](https://my.telegram.org).
2. Go to **API development tools**.
3. Create a new application and note your **App api_id** and **App api_hash**.

### 4. Create your Discord Webhook (For forwarding)
1. In your Discord server, right-click a text channel and select **Edit Channel**.
2. Go to **Integrations** > **Webhooks** > **Create Webhook** and copy the **Webhook URL**.

### 5. Create your Discord Bot (For managing mappings)
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**, name it, and click **Create**.
3. Go to the **Bot** tab on the left. Click **Reset Token** (or **Copy Token**) and save your **Bot Token**.
4. Scroll down to **Privileged Gateway Intents** and enable **Message Content Intent** (click **Save Changes**).
5. Go to **OAuth2** tab > **URL Generator**:
   - Select scope: `bot`
   - Select bot permissions: `Send Messages`, `Embed Links`, `Attach Files`
6. Copy the URL at the bottom, open it in your browser, and invite the bot to your Discord server.

### 6. Configure the Application
Open `config.yaml` and update it with your credentials:

```yaml
telegram:
  api_id: 1234567               # Change to your Telegram API ID
  api_hash: "your_api_hash"     # Change to your Telegram API Hash
  phone: "+1234567890"          # Change to your phone number

discord:
  bot_token: "YOUR_DISCORD_BOT_TOKEN_HERE" # Put your Discord Bot Token here

discord_webhooks:
  default: "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL_HERE"
```

---

## Discord Chat Commands

Your Discord Bot listens for the following commands in any text channel it has access to:

* **`!status`**: Displays connection health for Telegram and Discord, and tells you how many channels are active.
* **`!list`**: Lists all active Telegram channels being forwarded and their target webhook paths.
* **`!add <telegram_channel_username_or_id> <discord_webhook_url>`**: Dynamically maps and subscribes to a new Telegram channel.
  * *Example:* `!add -1003827118873 https://discord.com/api/webhooks/...`
  * *Example:* `!add durov https://discord.com/api/webhooks/...`
* **`!remove <telegram_channel_username_or_id_or_title>`**: Dynamically unsubscribes and removes a mapping.
  * *Example:* `!remove -1003827118873` or `!remove durov`

*Note: Dynamic mappings added or removed via Discord chat commands are automatically persisted to a local `dynamic_mappings.json` file.*

---

## Hosting on Streamlit Cloud (24/7 Free Hosting)

1. Push your repository to GitHub (ensure it is **Private**).
2. Go to [share.streamlit.io](https://share.streamlit.io) and deploy your app selecting the `main` branch and setting the main file path to `app.py`.
3. In your Streamlit App settings, go to the **Secrets** tab and paste your configuration in TOML format:

```toml
# Telegram API Credentials
[telegram]
api_id = 20861184
api_hash = "90455d0cdedfe6bb16bd716e2438703b"
phone = "+917499224791"

# Discord Bot Token (For commands)
[discord]
bot_token = "YOUR_DISCORD_BOT_TOKEN_HERE"

# Discord Webhook Configurations (For forwarding)
[discord_webhooks]
default = "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL_HERE"

# Base mappings
[[mappings]]
telegram_channel = "-1003827118873"
webhook_key = "default"

# General Settings
[settings]
forward_media = true
download_max_size_mb = 25
temp_dir = "./temp"
use_telegram_profile = true
```
4. Save the secrets and click **Start Bot** in your Streamlit dashboard. Authenticate your Telegram session on-screen during the first run.
