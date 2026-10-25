import discord
import os
import asyncio

# =======================================================
# DLNE STANDALONE INVITE GENERATOR
# Use this script to quickly generate or refresh the permanent 
# invite link to the #getting-started channel without running 
# the full server setup/sync.
# =======================================================

# Supplied at run time, never committed. See bot_token.py for why.
from bot_token import load_token
intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user.name}!")
    
    for guild in client.guilds:
        target_channel = discord.utils.get(guild.text_channels, name="getting-started")
        if not target_channel:
            print(f"❌ Could not find #getting-started channel in {guild.name}")
            continue
            
        print(f"🔄 Searching for existing permalink in {guild.name}...")
        existing_invites = await target_channel.invites()
        invite = next((inv for inv in existing_invites if inv.inviter == client.user and inv.max_age == 0), None)
        
        if invite:
            print(f"✅ Found existing permanent invite: {invite.url}")
        else:
            print(f"🚀 Creating new permanent invite for #getting-started...")
            invite = await target_channel.create_invite(max_age=0, max_uses=0, unique=False)
            print(f"✅ Created permanent invite: {invite.url}")
            
        print(f"🧹 Sweeping server for old/duplicate bot invites...")
        all_invites = await guild.invites()
        deleted_count = 0
        for inv in all_invites:
            if inv.inviter == client.user and inv.url != invite.url:
                await inv.delete()
                deleted_count += 1
                
        if deleted_count > 0:
            print(f"✅ Cleaned up {deleted_count} old bot invites.")
        else:
            print(f"✅ No old invites found to clean up.")
            
    print("Done! Shutting down...")
    await client.close()

if __name__ == "__main__":
    client.run(load_token())
