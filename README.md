# Telegram to Discord Message Forwarder

This script forwards messages from multiple Telegram channels to specified Discord channels using a Telegram User Session (via Telethon) and Discord Webhooks.

It features **dynamic branding**—the script automatically sets the webhook's username and avatar to match the source Telegram channel, making the forwarded messages look native. It also supports forwarding media attachments (images, video, documents).

---

## Prerequisites

- **Python 3.8+** (your current system has Python 3.14.6)
- A Telegram account
- A Discord server where you have permission to manage webhooks

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
To connect to Telegram, you need to register a developer application:
1. Log in to your Telegram account at [my.telegram.org](https://my.telegram.org).
2. Go to **API development tools**.
3. Create a new application (you can fill in any title and short name).
4. Note your **App api_id** and **App api_hash**.

### 4. Create a Discord Webhook
1. In your Discord server, right-click a text channel and select **Edit Channel**.
2. Go to **Integrations** > **Webhooks** > **Create Webhook** (or **New Webhook**).
3. Copy the **Webhook URL**.

### 5. Configure the Application
Open `config.yaml` and update it with your credentials and channel mappings:

```yaml
telegram:
  api_id: 1234567               # Change to your API ID (must be an integer, no quotes)
  api_hash: "your_api_hash"     # Change to your API Hash (keep inside quotes)
  phone: "+1234567890"          # Change to your phone number with country code

discord_webhooks:
  default: "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL_HERE"
  # You can add more webhooks here with different keys (e.g. general, news, etc.)

mappings:
  - telegram_channel: "telegram" # Public username (without '@') or private channel numeric ID
    webhook_key: "default"       # Maps to 'default' webhook above
```

#### Finding Private Channel IDs:
If you want to forward from a private channel:
1. Ensure your Telegram account is joined to the channel.
2. In Telegram Web or using a client, share or forward a message from the private channel, or use a bot like `@raw_infobot` to find the channel ID.
3. Private channel IDs in Telethon typically look like integers starting with `-100` (e.g., `-1001593847291`). Add this numeric ID directly in the config mappings.

---

## Running the Forwarder

Run the script using:

```bash
python main.py
```

### First-Run Authentication
On the first run, the script will prompt you in the console to complete the login:
1. **Phone Number**: Confirm or enter the phone number associated with your Telegram account (including country code).
2. **Verification Code**: Enter the code sent to your Telegram app (not SMS).
3. **Two-Factor Authentication (Optional)**: If you have 2FA enabled, enter your password.

Once authenticated, a session file named `telegram_forwarder_session.session` is created. **Do not share this file** as it contains active login tokens. Subsequent runs will use this session file and log in automatically without prompts.

---

## Running in the Background (Production)

To keep the script running permanently:

### On Windows (Task Scheduler or Background Script)
You can run it in a hidden window using a simple VBS script or run it as a background task. 

Alternative using PowerShell:
```powershell
Start-Process -FilePath "python" -ArgumentList "main.py" -WindowStyle Hidden
```

### On Linux (systemd / pm2)
You can easily wrap it in a systemd service or PM2 process manager:
```bash
pm2 start main.py --name "tg-discord-forwarder" --interpreter python3
```
