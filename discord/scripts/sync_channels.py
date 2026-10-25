import discord
from discord.ext import commands
import os

# Supplied at run time, never committed. See bot_token.py for why.
from bot_token import load_token
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

CONTENT_DIR = os.path.join(os.path.dirname(__file__), "..", "channels")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}!")
    print("Syncing channels with markdown files (Smart Diff)...")

    files_to_sync = {
        "rules": "rules.md",
        "getting-started": "getting-started.md",
        "architecture": "architecture.md",
        "faq": "faq.md",
        "webui-dev": "webui.md",
        "networking": "networking.md"
    }

    for guild in bot.guilds:
        for channel_name, filename in files_to_sync.items():
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if not channel:
                print(f"Channel #{channel_name} not found, skipping...")
                continue

            file_path = os.path.join(CONTENT_DIR, filename)
            if not os.path.exists(file_path):
                print(f"File {file_path} not found, skipping...")
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if not content.strip():
                continue

            # 1. Chunk content properly
            chunks = []
            current_chunk = ""
            for line in content.split('\n'):
                if len(current_chunk) + len(line) + 1 > 1900:
                    if current_chunk.strip():
                        chunks.append(current_chunk.strip())
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            if current_chunk.strip():
                chunks.append(current_chunk.strip())

            print(f"Syncing #{channel_name} (Smart Diff)...")
            
            # 2. Fetch existing messages sent by the bot
            messages = []
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author == bot.user:
                    messages.append(msg)
                    
            # 3. Compare and Diff Sync
            for i, chunk in enumerate(chunks):
                if i < len(messages):
                    # We have an existing message, check if it needs editing
                    if messages[i].content != chunk:
                        print(f"  -> Editing message {i+1}/{len(chunks)} in #{channel_name}")
                        await messages[i].edit(content=chunk)
                    else:
                        print(f"  -> Skipping unchanged message {i+1}/{len(chunks)} in #{channel_name}")
                else:
                    # We have more chunks than existing messages, send new
                    print(f"  -> Sending new message {i+1}/{len(chunks)} in #{channel_name}")
                    await channel.send(chunk)
            
            # 4. Prune trailing messages if the document got shorter
            if len(messages) > len(chunks):
                for excess_msg in messages[len(chunks):]:
                    print(f"  -> Deleting trailing message in #{channel_name}")
                    await excess_msg.delete()

    print("Sync complete! Shutting down...")
    await bot.close()

if __name__ == "__main__":
    bot.run(load_token())
