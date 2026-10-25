import discord
from discord.ext import commands
import asyncio
import os

# Supplied at run time, never committed. See bot_token.py for why.
from bot_token import load_token
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class RoleButton(discord.ui.Button):
    def __init__(self, label, emoji, style, role_name, custom_id):
        super().__init__(label=label, emoji=emoji, style=style, custom_id=custom_id)
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction):
        role = discord.utils.get(interaction.guild.roles, name=self.role_name)
        if not role:
            await interaction.response.send_message(f"Role {self.role_name} not found!", ephemeral=True)
            return
            
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"Removed {self.role_name} role!", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"Granted {self.role_name} role!", ephemeral=True)

class OnboardingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        # Hardware Nodes
        self.add_item(RoleButton("NVIDIA (CUDA)", "🟢", discord.ButtonStyle.success, "🟢 CUDA Node", "role_cuda"))
        self.add_item(RoleButton("AMD/Intel (Vulkan)", "🔴", discord.ButtonStyle.danger, "🔴 Vulkan Node", "role_vulkan"))
        self.add_item(RoleButton("CPU Only", "💻", discord.ButtonStyle.secondary, "💻 CPU Node", "role_cpu"))
        
        # Intent Roles
        self.add_item(RoleButton("Swarm Worker", "🐝", discord.ButtonStyle.primary, "🐝 Worker", "role_worker"))
        self.add_item(RoleButton("Coordinator", "👑", discord.ButtonStyle.primary, "👑 Coordinator", "role_coord"))
        self.add_item(RoleButton("Alpha Tester", "🚨", discord.ButtonStyle.primary, "🚨 Alpha Tester", "role_alpha"))
        self.add_item(RoleButton("Developer", "🛠️", discord.ButtonStyle.primary, "🛠️ Developer", "role_dev"))

@bot.event
async def on_ready():
    bot.add_view(OnboardingView())
    print(f"✅ Logged in as {bot.user.name}!")
    print("Type !setup_dlne to build the server layout.")
    print("Type !deploy_onboarding to post the interactive welcome menu in #getting-started.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_dlne(ctx):
    guild = ctx.guild
    await ctx.send("🚀 **Initializing Project DLNE Server Setup...** Please wait.")

    roles_to_create = {
        "👑 Core Dev": discord.Color.gold(),
        "🛠️ Developer": discord.Color.teal(),
        "🛡️ Moderator": discord.Color.blue(),
        "🚨 Alpha Tester": discord.Color.red(),
        "👑 Coordinator": discord.Color.purple(),
        "🐝 Worker": discord.Color.green(),
        "🟢 CUDA Node": discord.Color.dark_green(),
        "🔴 Vulkan Node": discord.Color.dark_red(),
        "💻 CPU Node": discord.Color.light_grey(),
    }
    
    # Prune deprecated/unmanaged roles
    allowed_roles = list(roles_to_create.keys())
    for role in guild.roles:
        if role.name not in allowed_roles and not role.managed and not role.is_default():
            try:
                await role.delete(reason="DLNE Server Setup Sync")
            except (discord.Forbidden, discord.HTTPException):
                pass
    
    for role_name, color in roles_to_create.items():
        existing_role = discord.utils.get(guild.roles, name=role_name)
        if not existing_role:
            await guild.create_role(name=role_name, color=color, hoist=True, mentionable=True)

    categories_and_channels = {
        "📢 INFORMATION": [
            ("rules", discord.ChannelType.text),
            ("announcements", discord.ChannelType.text),
            ("faq", discord.ChannelType.text),
            ("getting-started", discord.ChannelType.text)
        ],
        "🐝 SWARM CHAT": [
            ("general", discord.ChannelType.text),
            ("hardware-flex", discord.ChannelType.text),
            ("off-topic", discord.ChannelType.text),
            ("Swarm Voice", discord.ChannelType.voice)
        ],
        "🛠️ DEVELOPMENT": [
            ("github-feed", discord.ChannelType.text),
            ("architecture", discord.ChannelType.text),
            ("webui-dev", discord.ChannelType.text),
            ("networking", discord.ChannelType.text)
        ],
        "🚨 ALPHA TESTING": [
            ("alpha-chat", discord.ChannelType.text),
            ("bug-reports", discord.ChannelType.text),
            ("node-support", discord.ChannelType.text),
            ("Alpha Voice", discord.ChannelType.voice)
        ]
    }

    # Prune unknown channels and categories
    allowed_categories = list(categories_and_channels.keys())
    allowed_channels = []
    for cats in categories_and_channels.values():
        for ch, _ in cats:
            allowed_channels.append(ch)
            
    for cat in guild.categories:
        if cat.name not in allowed_categories:
            try:
                await cat.delete(reason="DLNE Server Setup Sync")
            except (discord.Forbidden, discord.HTTPException):
                pass
                
    for ch in guild.channels:
        if type(ch) != discord.CategoryChannel and ch.name not in allowed_channels:
            try:
                await ch.delete(reason="DLNE Server Setup Sync")
            except (discord.Forbidden, discord.HTTPException):
                pass

    for cat_name, channels in categories_and_channels.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)
        
        for ch_name, ch_type in channels:
            existing_channel = discord.utils.get(guild.channels, name=ch_name)
            if not existing_channel:
                if ch_type == discord.ChannelType.text:
                    await guild.create_text_channel(ch_name, category=category)
                elif ch_type == discord.ChannelType.voice:
                    await guild.create_voice_channel(ch_name, category=category)

    # Fetch or Generate permanent invite link
    target_channel = discord.utils.get(guild.text_channels, name="getting-started")
    old_channel = discord.utils.get(guild.text_channels, name="rules")
    
    if not target_channel:
        target_channel = ctx.channel
        
    # Delete old permalinks to #rules
    if old_channel:
        old_invites = await old_channel.invites()
        for inv in old_invites:
            if inv.inviter == bot.user:
                await inv.delete()
        
    existing_invites = await target_channel.invites()
    invite = next((inv for inv in existing_invites if inv.inviter == bot.user and inv.max_age == 0), None)
    
    if not invite:
        invite = await target_channel.create_invite(max_age=0, max_uses=0, unique=False)
    
    await ctx.send(f"✅ **DLNE Server Infrastructure successfully built!**\n🔗 Permanent Invite Link: {invite.url}\nRun `!deploy_onboarding` to spawn the interactive role menu!")

@bot.command()
@commands.has_permissions(administrator=True)
async def deploy_onboarding(ctx):
    target_channel = discord.utils.get(ctx.guild.text_channels, name="getting-started")
    if not target_channel:
        target_channel = ctx.channel
        
    embed = discord.Embed(
        title="Welcome to the DLNE Swarm!",
        description="Please select your hardware node type and your intended role within the network below. Click a button to instantly gain the role! (You can select multiple)",
        color=discord.Color.blue()
    )
    
    # Prune any old onboarding menus sent by the bot to prevent duplicates
    async for msg in target_channel.history(limit=50):
        if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "Welcome to the DLNE Swarm!":
            await msg.delete()
    
    await target_channel.send(embed=embed, view=OnboardingView())
    await ctx.send(f"✅ Interactive Onboarding menu deployed in {target_channel.mention}!")

if __name__ == "__main__":
    bot.run(load_token())
