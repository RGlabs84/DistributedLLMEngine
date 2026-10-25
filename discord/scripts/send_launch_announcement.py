"""
One-shot: post the test-network launch announcement to #announcements via DLNE-bot.

Run:  python discord/scripts/send_launch_announcement.py
Token/channel conventions match sync_channels.py (same bot, same server layout).
"""
import sys

import discord

# Supplied at run time, never committed. See bot_token.py for why.
from bot_token import load_token
ANNOUNCEMENT = """@everyone

# 🚨 THE DLNE TEST NETWORK IS LAUNCHING SOON 🚨

The wait is almost over. The **Distributed LLM Network Engine** is entering its live **test network phase** — real nodes, real tensor-splitting, real distributed inference across the swarm. ⚡

## 🔥 16GB OF HBM2 SEEDING THE SWARM
We are not launching empty. The network goes live pre-seeded with **16GB of HBM2** — datacenter-class high-bandwidth memory pumping raw tensor throughput into the pool from day one. That's the seed. Your hardware is the harvest. 🌱

## 🕸️ What this means for you
🐝 **Workers** — your idle VRAM stacks directly on top of the HBM2 seed pool
👑 **Coordinator hub** — WebUI, dynamic load balancing, and encrypted WireGuard tunnels, live
🛡️ **Redundancy failover** — standby brains keep the swarm alive if nodes drop
🎮 **Smart Yielding** — you game, we pause. Zero interruptions. Ever.

## 🚨 ALPHA TESTERS: THIS IS YOUR CALL-UP
The test network launch is where we prove it all. NVIDIA rigs, old mining frames, single-GPU gamers — **every card counts.**

Watch this channel. The launch signal drops here first. 📡

**Seed compute. Pull compute. Together, we build the swarm.** 🤝"""


class AnnounceClient(discord.Client):
    async def on_ready(self):
        sent = False
        for guild in self.guilds:
            channel = discord.utils.get(guild.text_channels, name="announcements")
            if channel is None:
                print(f"[skip] no #announcements in guild '{guild.name}'")
                continue
            await channel.send(
                ANNOUNCEMENT,
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
            print(f"[sent] #announcements in guild '{guild.name}'")
            sent = True
        await self.close()
        if not sent:
            print("ERROR: announcement was not sent to any guild.")
            sys.exit(1)


intents = discord.Intents.default()
client = AnnounceClient(intents=intents)
client.run(load_token())
