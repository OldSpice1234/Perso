import discord
from discord.ext import commands, tasks
import json
import os
import random
from datetime import datetime, timedelta, timezone

# --- Configuration ---
# Set these in your hosting service (e.g., Fly.io Secrets)
TOKEN = os.environ.get('DISCORD_TOKEN')
CHECKIN_CHANNEL_ID = int(os.environ.get('CHECKIN_CHANNEL_ID', 0))
CHECKIN_EMOJI = "✅"
CURRENCY_SYMBOL = "$" # Change this to £, €, etc. if needed

# --- File Paths (Fly.io needs a persistent data directory) ---
# In your fly.toml, you will mount a volume to /data
DATA_DIR = '/data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
REWARDS_FILE = 'rewards_config.json' 
CONTENT_FILE = 'gamble_aware_content.json'

# --- Bot Setup ---
class GamblingRecoveryBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        # Load data when the bot starts
        self.user_data = self.load_data(USERS_FILE)
        # These config files are read-only after start, so they don't need to be in the data dir
        self.rewards_config = self.load_data(REWARDS_FILE)
        self.gamble_aware_content = self.load_data(CONTENT_FILE)
        
        # Ensure the data directory exists for Fly.io volumes
        if not os.path.exists(DATA_DIR):
            print(f"Data directory {DATA_DIR} not found. Creating it. Make sure a volume is mounted here in production.")
            os.makedirs(DATA_DIR)
        
        print(f"Loaded {len(self.user_data)} users.")
        if not self.rewards_config: print("Warning: rewards_config.json is empty or not found.")
        if not self.gamble_aware_content: print("Warning: gamble_aware_content.json is empty or not found.")
        
        # Start background tasks
        post_daily_checkin_message.start()

    def load_data(self, file_path):
        """Loads data from a JSON file."""
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def save_data(self):
        """Saves user data to a JSON file."""
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.user_data, f, indent=4)

bot = GamblingRecoveryBot()

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    print(f"Watching for check-ins in channel ID: {CHECKIN_CHANNEL_ID}")

@bot.event
async def on_raw_reaction_add(payload):
    """Handles reactions for daily check-ins."""
    if payload.user_id == bot.user.id or payload.channel_id != CHECKIN_CHANNEL_ID or str(payload.emoji) != CHECKIN_EMOJI:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    member = guild.get_member(payload.user_id)
    if not member: return

    user_id_str = str(payload.user_id)

    if user_id_str not in bot.user_data:
        try:
            await member.send("You need to start the challenge first! Use the `!start` command in the server.")
        except discord.Forbidden: pass
        return

    u_data = bot.user_data[user_id_str]
    now_utc = datetime.now(timezone.utc)
    last_checkin_date_obj = datetime.fromisoformat(u_data.get('last_checkin_date', "1970-01-01T00:00:00+00:00"))

    if now_utc.date() <= last_checkin_date_obj.date():
        try:
            await member.send(f"You've already checked in for {now_utc.strftime('%Y-%m-%d')}!")
        except discord.Forbidden: pass
        return

    # Process Check-in
    u_data['last_checkin_date'] = now_utc.isoformat()
    if (now_utc.date() - last_checkin_date_obj.date()).days == 1:
        u_data['current_streak_days'] += 1
    else:
        u_data['current_streak_days'] = 1
    u_data['longest_streak_days'] = max(u_data.get('longest_streak_days', 0), u_data['current_streak_days'])
    u_data['total_days_gambling_free'] = u_data.get('total_days_gambling_free', 0) + 1
    
    start_date_obj = datetime.fromisoformat(u_data['start_date'])
    challenge_day = (now_utc.date() - start_date_obj.date()).days + 1

    # Reward Logic
    unlocked_rewards_messages = []
    def get_random_reward(reward_list):
        return random.choice(reward_list) if isinstance(reward_list, list) and reward_list else reward_list

    # Daily
    daily_reward_key = f"day_{challenge_day}"
    daily_rewards_list = bot.rewards_config.get('daily_rewards', {}).get(daily_reward_key)
    if daily_rewards_list and daily_reward_key not in u_data.get('rewards_unlocked', []):
        unlocked_rewards_messages.append(f"Day {challenge_day}: {get_random_reward(daily_rewards_list)}")
        u_data.setdefault('rewards_unlocked', []).append(daily_reward_key)

    # Weekly/Monthly/Yearly based on streak
    for category, period, period_name in [('weekly_rewards', 7, 'Week'), ('monthly_rewards', 30, 'Month'), ('yearly_rewards', 365, 'Year')]:
        for key, reward_list in bot.rewards_config.get(category, {}).items():
            num = int(key.split('_')[1])
            if u_data['current_streak_days'] >= (num * period) and key not in u_data.get('rewards_unlocked', []):
                unlocked_rewards_messages.append(f"{period_name} {num} Streak: {get_random_reward(reward_list)}")
                u_data.setdefault('rewards_unlocked', []).append(key)

    bot.save_data()

    # Send Confirmation and Reward DM
    embed = discord.Embed(title=f"✅ Check-in Successful!", color=discord.Color.green())
    embed.description=f"Amazing job, {member.mention}! You're on day **{challenge_day}** of your journey.\n" \
                      f"Current Streak: **{u_data['current_streak_days']}** day(s)."
    if unlocked_rewards_messages:
        embed.add_field(name="✨ Reward Ideas Unlocked ✨", value="\n".join(f"🏆 {msg}" for msg in unlocked_rewards_messages), inline=False)
    
    embed.set_footer(text="Keep up the incredible work! One day at a time.")
    try:
        await member.send(embed=embed)
    except discord.Forbidden: print(f"Could not DM {member.display_name}.")
    
    try:
        message = await bot.get_channel(payload.channel_id).fetch_message(payload.message_id)
        await message.remove_reaction(payload.emoji, member)
    except Exception as e: pass

# --- Tasks ---
@tasks.loop(hours=24)
async def post_daily_checkin_message():
    channel = bot.get_channel(CHECKIN_CHANNEL_ID)
    if not channel: return
    
    embed = discord.Embed(title="🌟 Daily Gambling-Free Check-in 🌟", color=discord.Color.blue())
    embed.description = f"It's a new day! If you've stayed strong, react with {CHECKIN_EMOJI} to log your progress.\n\n" \
                        "You are strong, capable, and not alone.\n\n" \
                        "**Commands for Support:**\n" \
                        "`!mystats` - View your progress.\n" \
                        "`!whyquit` - Reminders & information.\n" \
                        "`!panic` - Immediate help for urges."
    embed.set_footer(text=f"Today is {datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}")
    try:
        message = await channel.send(embed=embed)
        await message.add_reaction(CHECKIN_EMOJI)
    except Exception as e: print(f"Could not post daily message: {e}")

@post_daily_checkin_message.before_loop
async def before_daily_task():
    await bot.wait_until_ready()

# --- Bot Commands ---
@bot.command(name='start', help='Starts your gambling-free challenge.')
async def start_challenge(ctx):
    user_id_str = str(ctx.author.id)
    if user_id_str in bot.user_data:
        await ctx.send(f"{ctx.author.mention}, you've already started the challenge. Use `!mystats` to see your progress.")
        return

    bot.user_data[user_id_str] = {
        'username': ctx.author.name, 'start_date': datetime.now(timezone.utc).isoformat(),
        'last_checkin_date': "1970-01-01T00:00:00+00:00", 'current_streak_days': 0, 
        'longest_streak_days': 0, 'total_days_gambling_free': 0, 'savings': 0.0,
        'rewards_unlocked': [], 'journal_entries': []
    }
    bot.save_data()
    embed = discord.Embed(title="🎉 Challenge Started! You've Got This! 🎉", color=discord.Color.purple())
    embed.description = f"Congratulations, {ctx.author.mention}, on taking this brave and vital step.\n\n" \
                        f"Check in daily by reacting to the message in <#{CHECKIN_CHANNEL_ID}>.\n\n" \
                        "Remember your strength. You are not alone."
    await ctx.send(embed=embed)

@bot.command(name='mystats', help='Shows your current progress in the challenge.')
async def mystats(ctx):
    user_id_str = str(ctx.author.id)
    if user_id_str not in bot.user_data:
        await ctx.send(f"You haven't started yet, {ctx.author.mention}. Use `!start` to begin your journey.")
        return

    u_data = bot.user_data[user_id_str]
    start_date_obj = datetime.fromisoformat(u_data['start_date'])
    challenge_day = (datetime.now(timezone.utc).date() - start_date_obj.date()).days + 1

    embed = discord.Embed(title=f"📊 {ctx.author.display_name}'s Recovery Journey 📊", color=discord.Color.gold())
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    
    stats_view = discord.ui.View()
    stats_view.add_item(discord.ui.Button(label="Why Quit?", style=discord.ButtonStyle.secondary, custom_id="whyquit_view"))
    stats_view.add_item(discord.ui.Button(label="Resources", style=discord.ButtonStyle.danger, custom_id="resources_view"))

    embed.add_field(name="🚀 Challenge Day", value=f"Day **{challenge_day}**")
    embed.add_field(name="🔥 Current Streak", value=f"**{u_data.get('current_streak_days', 0)}** days")
    embed.add_field(name="🏆 Longest Streak", value=f"**{u_data.get('longest_streak_days', 0)}** days")
    embed.add_field(name="💰 Est. Savings", value=f"**{CURRENCY_SYMBOL}{u_data.get('savings', 0):.2f}**")
    
    await ctx.send(embed=embed, view=stats_view)

# --- NEW COMMANDS ---
@bot.command(name='addsavings', help=f'Log money you saved by not gambling. E.g., !addsavings 50')
async def addsavings(ctx, amount: float):
    user_id_str = str(ctx.author.id)
    if user_id_str not in bot.user_data:
        await ctx.send("Please start the challenge with `!start` before logging savings.")
        return
    if amount <= 0:
        await ctx.send("Please enter a positive amount.")
        return
        
    u_data = bot.user_data[user_id_str]
    u_data['savings'] = u_data.get('savings', 0.0) + amount
    bot.save_data()
    
    await ctx.message.add_reaction("👍")
    await ctx.send(f"Great job! Added **{CURRENCY_SYMBOL}{amount:.2f}** to your savings. Your new total saved is **{CURRENCY_SYMBOL}{u_data['savings']:.2f}**.", delete_after=10)

@bot.command(name='journal', help='Privately log your thoughts and feelings. Your entry is DMed to you for confirmation.')
async def journal(ctx, *, entry: str):
    user_id_str = str(ctx.author.id)
    if user_id_str not in bot.user_data:
        await ctx.send("Please start the challenge with `!start` before journaling.")
        return

    u_data = bot.user_data[user_id_str]
    journal_entry = {
        'date': datetime.now(timezone.utc).isoformat(),
        'entry': entry
    }
    u_data.setdefault('journal_entries', []).append(journal_entry)
    bot.save_data()

    embed = discord.Embed(title="Journal Entry Saved", color=discord.Color.dark_grey())
    embed.description = f"Your thoughts from **{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC** have been privately saved."
    embed.add_field(name="Your Entry", value=f"_{entry}_")
    embed.set_footer(text="Use !myjournal to see your last 5 entries.")
    
    try:
        await ctx.author.send(embed=embed)
        await ctx.message.reply("I've saved your private journal entry and sent you a copy in DMs.", delete_after=10)
        await ctx.message.delete(delay=10)
    except discord.Forbidden:
        await ctx.reply("I couldn't DM you, but your entry is saved. Please enable DMs for this server for full functionality.")

@bot.command(name='myjournal', help='Shows your last 5 private journal entries in your DMs.')
async def myjournal(ctx):
    user_id_str = str(ctx.author.id)
    if user_id_str not in bot.user_data or not bot.user_data[user_id_str].get('journal_entries'):
        await ctx.send("You have no journal entries. Use `!journal [your thoughts]` to create one.")
        return

    entries = bot.user_data[user_id_str].get('journal_entries', [])[-5:] # Get last 5
    embed = discord.Embed(title="Your Last 5 Journal Entries", color=discord.Color.dark_grey())
    if not entries:
        embed.description = "You haven't written any journal entries yet."
    else:
        for entry in entries:
            entry_date = datetime.fromisoformat(entry['date'])
            embed.add_field(
                name=f"🗓️ {entry_date.strftime('%Y-%m-%d %H:%M')} UTC",
                value=f"_{entry['entry'][:1000]}_", # Truncate long entries
                inline=False
            )
    try:
        await ctx.author.send(embed=embed)
        await ctx.message.reply("I've sent your recent journal entries to your DMs.", delete_after=10)
    except discord.Forbidden:
        await ctx.reply("I can't DM you! Please enable DMs for this server to use this feature.")


@bot.command(name='panic', help='Get immediate help for an urge to gamble.')
async def panic(ctx):
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Why Quit?", style=discord.ButtonStyle.secondary, custom_id="whyquit_view"))
    view.add_item(discord.ui.Button(label="Find a Distraction", style=discord.ButtonStyle.primary, custom_id="distraction_view"))
    view.add_item(discord.ui.Button(label="Get Help", style=discord.ButtonStyle.danger, custom_id="resources_view"))
    
    embed = discord.Embed(title="🛑 Stop. Breathe. You Are In Control. 🛑", color=discord.Color.red())
    embed.description = "This urge is temporary. It will pass. You are stronger than it.\n\n" \
                        "Acknowledge the feeling, but don't act on it. Use the buttons below for immediate support."
    await ctx.send(embed=embed, view=view, ephemeral=True) # Ephemeral makes the message only visible to the user


# --- UI Views and Callbacks ---
class GambleAwareView(discord.ui.View):
    def __init__(self, content_data):
        super().__init__(timeout=300)
        self.content_data = content_data
        self.current_category_key = list(content_data.keys())[0]
        self.current_page = 0
        self.update_view()

    def update_view(self):
        self.clear_items()
        
        options = [discord.SelectOption(label=cat_data.get("title"), value=key, default=key==self.current_category_key) for key, cat_data in self.content_data.items()]
        self.add_item(discord.ui.Select(placeholder="Choose a category...", options=options, custom_id="category_select"))

        _, points = self._get_category_content()
        if len(points) > 1:
            self.add_item(discord.ui.Button(label="⬅️", style=discord.ButtonStyle.secondary, custom_id="prev_page", disabled=(self.current_page == 0)))
            self.add_item(discord.ui.Button(label="➡️", style=discord.ButtonStyle.secondary, custom_id="next_page", disabled=(self.current_page >= len(points) - 1)))
    
    def _get_category_content(self):
        category = self.content_data.get(self.current_category_key, {})
        return category.get("title", "N/A"), category.get("points", [{"title": "N/A", "text": "N/A"}])

    def create_embed(self):
        category_title, points = self._get_category_content()
        point_content = points[self.current_page]
        embed = discord.Embed(title=f"💡 {category_title} 💡", description=f"**{point_content.get('title')}**", color=discord.Color.teal())
        embed.add_field(name="Details", value=point_content.get('text'), inline=False)
        if len(points) > 1: embed.set_footer(text=f"Page {self.current_page + 1}/{len(points)}")
        return embed

    async def handle_interaction(self, interaction: discord.Interaction):
        # This is a unified handler for all button/select interactions on this view
        custom_id = interaction.data['custom_id']
        if custom_id == 'category_select':
            self.current_category_key = interaction.data['values'][0]
            self.current_page = 0
        elif custom_id == 'prev_page':
            if self.current_page > 0: self.current_page -= 1
        elif custom_id == 'next_page':
            _, points = self._get_category_content()
            if self.current_page < len(points) - 1: self.current_page += 1
        
        self.update_view()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

@bot.command(name='whyquit', help='Information and reasons to quit gambling.')
async def whyquit(ctx):
    if not bot.gamble_aware_content: return await ctx.send("Content not loaded.")
    view = GambleAwareView(bot.gamble_aware_content)
    embed = view.create_embed()
    await ctx.send(embed=embed, view=view)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Central interaction handler for buttons that aren't part of a persistent view class
    custom_id = interaction.data.get("custom_id")
    
    if custom_id in ["whyquit_view", "distraction_view", "resources_view"]:
        await interaction.response.defer() # Acknowledge the interaction
        
        if custom_id == "whyquit_view":
            view = GambleAwareView(bot.gamble_aware_content)
            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        elif custom_id == "distraction_view":
            distractions = bot.gamble_aware_content.get("coping_mechanisms", {}).get("points", [])
            if distractions:
                distraction = random.choice(distractions)
                embed = discord.Embed(title=f"Try This Now: {distraction['title']}", description=distraction['text'], color=discord.Color.orange())
                await interaction.followup.send(embed=embed, ephemeral=True)
                
        elif custom_id == "resources_view":
            resources = bot.gamble_aware_content.get("external_resources", {}).get("points", [])
            if resources:
                embed = discord.Embed(title="🆘 Professional Help & Resources", description="You are not alone. Reaching out is a sign of strength.", color=discord.Color.dark_red())
                for res in resources:
                    embed.add_field(name=res['title'], value=res['text'], inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
    
    # Let the bot process other interactions like commands
    # await bot.process_application_commands(interaction) # if using slash commands


# --- Run Bot ---
if __name__ == "__main__":
    if not TOKEN or CHECKIN_CHANNEL_ID == 0:
        print("FATAL ERROR: DISCORD_TOKEN or CHECKIN_CHANNEL_ID not set in environment variables.")
    else:
        bot.run(TOKEN)
