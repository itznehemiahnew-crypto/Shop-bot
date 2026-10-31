# AI Server Builder Bot (100% free setup)

@mention the bot and tell it what you want. It plans, you press "Build it".
Builds roles, categories, channels, styled embeds (banner/thumbnail images),
and persistent dropdown menus (self-assign roles, FAQ/info menus).
Admin-only. Upload an image in the same message to use it as a banner.

## 1. Free AI key
Get a free Gemini key (no card) at https://aistudio.google.com -> Get API key.
Other free OpenAI-compatible providers also work (Groq, OpenRouter, ...):
set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL.

## 2. Discord setup
1. discord.com/developers > New Application > Bot > copy token.
2. Bot tab > enable **Message Content Intent**.
3. OAuth2 > URL Generator: scopes `bot` + `applications.commands`,
   permission **Administrator**. Invite it.

## 3a. Run on your own PC (free, easiest)
    pip install -r requirements.txt
    export DISCORD_TOKEN=...      (Windows: set DISCORD_TOKEN=...)
    export LLM_API_KEY=...
    python bot.py
The bot is online while the window is open.

## 3b. Deploy on Render free
Push to GitHub > New > Blueprint (reads render.yaml) > set DISCORD_TOKEN and LLM_API_KEY.
Free web services sleep after ~15 min without web traffic, so make a free
monitor at uptimerobot.com that pings your Render URL every 5 minutes.
Dropdown configs are saved in a private #ai-bot-data channel, so they
survive restarts. Don't delete that channel.

## Use
    @Bot build me a gaming server: rules, announcements, general chat, voice,
    a staff-only category, and a color-roles dropdown with this banner [image]

/reset clears the conversation for that channel.
