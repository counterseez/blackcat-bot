import os
import discord
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput
import sqlite3
import threading
import atexit
from pathlib import Path

# ===== ตั้งค่า =====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== DATABASE (SQLite) =====
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "points.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
lock = threading.Lock()

# ===== POINT TABLE =====
cursor.execute("""
CREATE TABLE IF NOT EXISTS points (
    user_id TEXT PRIMARY KEY,
    points INTEGER NOT NULL DEFAULT 0
)
""")

# ===== REDEEM LOG TABLE =====
cursor.execute("""
CREATE TABLE IF NOT EXISTS redeem_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    points_used INTEGER NOT NULL,
    reward_baht INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    ticket_channel_id TEXT,
    approved_by TEXT,
    approved_at TEXT,
    rejected_by TEXT,
    rejected_at TEXT,
    redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()

# 🔧 FIX COLUMN (กัน error)
def fix_redeem_table():
    cursor.execute("PRAGMA table_info(redeem_logs)")
    columns = [col[1] for col in cursor.fetchall()]

    if "ticket_channel_id" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN ticket_channel_id TEXT")

    if "status" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN status TEXT DEFAULT 'pending'")

    if "approved_by" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN approved_by TEXT")

    if "approved_at" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN approved_at TEXT")

    if "rejected_by" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN rejected_by TEXT")

    if "rejected_at" not in columns:
        cursor.execute("ALTER TABLE redeem_logs ADD COLUMN rejected_at TEXT")

    conn.commit()

fix_redeem_table()


@atexit.register
@atexit.register
def close_db():
    try:
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_ticket_owner_id(channel_name: str):
    if channel_name.startswith("ticket-"):
        try:
            return int(channel_name.replace("ticket-", ""))
        except ValueError:
            return None
    return None


def get_rank(user_id):
    with lock:
        cursor.execute("SELECT user_id, points FROM points ORDER BY points DESC, user_id ASC")
        data = cursor.fetchall()

    for index, (uid, pts) in enumerate(data, start=1):
        if str(user_id) == uid:
            return index
    return len(data) + 1


def get_level(points):
    level = points // 100
    exp = points % 100
    return level, exp


def get_exp_bar(exp):
    total = 10
    filled = int(exp / 10)
    return "▰" * filled + "▱" * (total - filled)


# ===== ระบบแต้ม =====
def add_point(user_id, amount):
    user_id = str(user_id)

    with lock:
        cursor.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()

        if result is None:
            cursor.execute(
                "INSERT INTO points (user_id, points) VALUES (?, ?)",
                (user_id, amount)
            )
        else:
            new_points = result[0] + amount
            cursor.execute(
                "UPDATE points SET points = ? WHERE user_id = ?",
                (new_points, user_id)
            )

        conn.commit()


def check_point(user_id):
    user_id = str(user_id)

    with lock:
        cursor.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()

    if result is None:
        return 0
    return result[0]

from datetime import datetime

def remove_point(user_id, amount):
    user_id = str(user_id)

    with lock:
        cursor.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()

        if result is None:
            return False, 0

        current_points = result[0]

        if current_points < amount:
            return False, current_points

        new_points = current_points - amount

        cursor.execute(
            "UPDATE points SET points = ? WHERE user_id = ?",
            (new_points, user_id)
        )
        conn.commit()

    return True, new_points


def add_redeem_log(user_id, points_used, reward_baht):
    user_id = str(user_id)

    with lock:
        cursor.execute("""
            INSERT INTO redeem_logs (user_id, points_used, reward_baht)
            VALUES (?, ?, ?)
        """, (user_id, points_used, reward_baht))
        conn.commit()
        return cursor.lastrowid


def set_redeem_ticket(log_id, channel_id):
    with lock:
        cursor.execute("""
            UPDATE redeem_logs
            SET ticket_channel_id = ?
            WHERE id = ?
        """, (str(channel_id), log_id))
        conn.commit()


def approve_redeem(log_id, admin_id):
    with lock:
        cursor.execute("""
            UPDATE redeem_logs
            SET status = 'approved',
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
        """, (str(admin_id), datetime.now().isoformat(), log_id))
        conn.commit()


def reject_redeem(log_id, admin_id):
    with lock:
        cursor.execute("""
            UPDATE redeem_logs
            SET status = 'rejected',
                rejected_by = ?,
                rejected_at = ?
            WHERE id = ?
        """, (str(admin_id), datetime.now().isoformat(), log_id))
        conn.commit()


def get_user_redeem_logs(user_id, limit=10):
    user_id = str(user_id)

    with lock:
        cursor.execute("""
            SELECT points_used, reward_baht, redeemed_at
            FROM redeem_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        return cursor.fetchall()
    
async def process_redeem(interaction, required_points, reward):
    user = interaction.user
    guild = interaction.guild

    pts = check_point(user.id)

    if guild is None:
        await interaction.response.send_message(
            "❌ ใช้ได้เฉพาะในเซิร์ฟเวอร์",
            ephemeral=True
        )
        return

    if pts < required_points:
        await interaction.response.send_message(
            f"❌ แต้มไม่พอ\nตอนนี้มี {pts} แต้ม\nต้องใช้ {required_points} แต้ม",
            ephemeral=True
        )
        return

    success, new_points = remove_point(user.id, required_points)

    if not success:
        await interaction.response.send_message(
            "❌ แลกไม่สำเร็จ",
            ephemeral=True
        )
        return

    log_id = add_redeem_log(user.id, required_points, reward)

    me = guild.me
    if me is None:
        await interaction.response.send_message(
            "❌ ไม่พบบอทในเซิร์ฟเวอร์",
            ephemeral=True
        )
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            attach_files=True
        ),
        me: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            manage_channels=True
        ),
    }

    admin_role = discord.utils.get(guild.roles, name="Admin")
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            manage_messages=True
        )

    channel = await guild.create_text_channel(
        name=f"redeem-{user.id}-{log_id}",
        overwrites=overwrites
    )

    set_redeem_ticket(log_id, channel.id)

    embed = discord.Embed(
        title="🎁 คำขอแลกเงิน",
        description=(
            f"{user.mention} ใช้ **{required_points} แต้ม** แลก **{reward} บาท**\n\n"
            f"💰 แต้มคงเหลือ: **{new_points} แต้ม**\n"
            f"🧾 เลขรายการ: **#{log_id}**"
        ),
        color=0x8A2BE2
    )

    embed.set_footer(text="BlackCat Store 🐾")

    await channel.send(
        content=user.mention,
        embed=embed,
        view=RedeemTicketAdminView(user.id, log_id)
    )

    await interaction.response.send_message(
        f"✅ เปิด Ticket แล้ว: {channel.mention}",
        ephemeral=True
    )
    
    

# ===== ปุ่มเช็คแต้ม =====
class PointView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📟『 เช็คแต้ม 』",
        style=discord.ButtonStyle.green,
        custom_id="check_point_btn"
    )
    async def check(self, interaction: discord.Interaction, button: Button):
        user = interaction.user
        pts = check_point(user.id)
        rank = get_rank(user.id)
        level, exp = get_level(pts)
        bar = get_exp_bar(exp)

        embed = discord.Embed(
            title="💸 BlackCat Wallet",
            description=(
                "━━━━━━━━━━━━━━━━━━\n"
                "💳 **BLACKCAT WALLET SYSTEM**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "💸 กดปุ่มด้านล่างเพื่อเช็คแต้ม หรือแลกเงิน\n"
                "🎁 ใช้ 8 แต้ม แลก 30 บาท ได้ทันที"
            ),
            color=0x8A2BE2
        )

        embed.set_image(url="https://cdn.discordapp.com/attachments/1000452582092845177/1492635264961745076/check_point_ss.gif?ex=69dc0c6a&is=69dabaea&hm=31ca158831b507cb7bf87cc6b3b503658d8337ada8fbc7425d3031f54e2873bd&")
        embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)

        embed.add_field(name="💰 ยอดแต้ม", value=f"```{pts} แต้ม```", inline=False)
        embed.add_field(name="🏆 อันดับ", value=f"```#{rank}```", inline=True)
        embed.add_field(name="🎮 เลเวล", value=f"```Lv.{level}```", inline=True)
        embed.add_field(name="📊 ความคืบหน้า", value=f"```{bar} ({exp}/100)```", inline=False)
        embed.add_field(name="🎁 สิทธิพิเศษ", value="```ใช้ 8 แต้ม แลก 30 บาท```", inline=False)

        embed.set_footer(text="BlackCat Store 🐾")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
            label="💸 แลกแต้ม", 
            style=discord.ButtonStyle.blurple,
            custom_id="open_redeem_btn"
    )
    async def open_redeem(self, interaction: discord.Interaction, button: Button):

        embed = discord.Embed(
            title="<a:4484pinkarrow:1120379420704780348> ระบบแลกแต้ม",
            description= "เลือกจำนวนแต้มที่ต้องการ",
            color=0x8A2BE2
    )

        await interaction.response.send_message(
            embed=embed,
            view=RedeemMenuView(),
            ephemeral=True
    )

 
# ===== ปิด Ticket =====
class CloseTicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 ปิด Ticket",
        style=discord.ButtonStyle.red,
        custom_id="close_ticket_btn"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        await interaction.response.send_message("🔒 ปิด Ticket แล้ว", ephemeral=True)
        await interaction.channel.delete()

class RedeemTicketAdminView(View):
    def __init__(self, user_id: int, log_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.log_id = log_id

    @discord.ui.button(label="✅ ยืนยันจ่ายแล้ว", style=discord.ButtonStyle.green)
    async def approve_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        approve_redeem(self.log_id, interaction.user.id)

        pts = check_point(self.user_id)

        embed = discord.Embed(
            title="🎉 แลกสำเร็จ",
            description=(
                f"💸 <@{self.user_id}> ได้รับเงินแล้ว\n\n"
                f"💰 แต้มคงเหลือ: **{pts} แต้ม**"
            ),
            color=0x57F287
         )

        await interaction.response.send_message("✅ ยืนยันเรียบร้อย")

        await interaction.channel.send(
            content=f"<@{self.user_id}>",
            embed=embed
        )

        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @discord.ui.button(label="❌ ปฏิเสธ", style=discord.ButtonStyle.red)
    async def reject_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        reject_redeem(self.log_id, interaction.user.id)

        await interaction.response.send_message("❌ ปฏิเสธแล้ว")
        await interaction.channel.send(f"❌ <@{self.user_id}> รายการถูกปฏิเสธ")

# ===== VerifyView =====
class VerifyView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ ยืนยันตัวตน",
        style=discord.ButtonStyle.success,
        custom_id="verify_button"
    )
    async def verify_button(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("❌ ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
            return

        role = discord.utils.get(guild.roles, name="Verified")
        if role is None:
            await interaction.response.send_message("❌ ไม่พบยศชื่อ Verified", ephemeral=True)
            return

        if role in user.roles:
            await interaction.response.send_message("✅ คุณยืนยันตัวตนแล้ว", ephemeral=True)
            return

        try:
            await user.add_roles(role, reason="Verified by button")
            await interaction.response.send_message("✅ ยืนยันตัวตนสำเร็จ ยินดีต้อนรับเข้าสู่เซิร์ฟเวอร์ 💜", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ บอทไม่มีสิทธิ์ให้ยศนี้", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("❌ เกิดข้อผิดพลาดในการให้ยศ", ephemeral=True)


@bot.command()
async def verifypanel(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    embed = discord.Embed(
        title="✅ ระบบยืนยันตัวตน",
        description="กดปุ่มด้านล่างเพื่อรับยศยืนยันตัวตน",
        color=0x57F287
    )
    embed.add_field(
        name="📌 หมายเหตุ",
        value="เมื่อกดยืนยันแล้ว คุณจะสามารถเข้าถึงห้องที่กำหนดไว้ได้",
        inline=False
    )
    embed.set_footer(text="BlackCat Verify System")

    await ctx.send(embed=embed, view=VerifyView())




class RedeemMenuView(View):
    def __init__(self):
        super().__init__(timeout=60)  # อันนี้ไม่ต้อง persistent

    @discord.ui.button(
        label="🎁 8 แต้ม = 30 บาท",
        style=discord.ButtonStyle.green,
        custom_id="redeem_8_btn"
    )
    async def redeem_8(self, interaction: discord.Interaction, button: Button):
        await process_redeem(interaction, 8, 30)

    @discord.ui.button(
        label="🎁 16 แต้ม = 60 บาท",
        style=discord.ButtonStyle.green,
        custom_id="redeem_16_btn"
    )
    async def redeem_16(self, interaction: discord.Interaction, button: Button):
        await process_redeem(interaction, 16, 60)

# ===== Modal ใส่แต้ม =====
class TopupModal(Modal, title="ใส่จำนวนแต้ม"):
    amount = TextInput(
        label="จำนวนแต้ม",
        placeholder="เช่น 100",
        min_length=1,
        max_length=6
    )

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("❌ ใส่ตัวเลขเท่านั้น", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("❌ จำนวนแต้มต้องมากกว่า 0", ephemeral=True)
            return

        add_point(self.user_id, amount)
        pts = check_point(self.user_id)

        await interaction.response.send_message(
            f"✅ เติม {amount} แต้มให้ <@{self.user_id}> สำเร็จ",
            ephemeral=True
        )

        await interaction.channel.send(
            f"💸 <@{self.user_id}> ได้รับ {amount} แต้ม (รวม {pts})",
            view=CloseTicketView()
        )


# ===== ปุ่มแอด =====
class AdminConfirmView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id

    # ✅ ยืนยัน
    @discord.ui.button(label="✅ ยืนยันสลิป", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        await interaction.response.send_modal(TopupModal(self.user_id))

    # ❌ สลิปผิด
    @discord.ui.button(label="❌ สลิปผิด", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ แอดเท่านั้น", ephemeral=True)
            return

        await interaction.response.send_message("❌ ปฏิเสธสลิปแล้ว", ephemeral=True)

        await interaction.channel.send(
            f"❌ <@{self.user_id}> สลิปไม่ถูกต้อง กรุณาส่งใหม่"
            
        )
        await message.channel.send(
            "📸 **ตรวจสอบสลิปด้านล่าง**\n\n"
            "✅ ยืนยัน = เติมแต้ม\n"
            "❌ สลิปผิด = ให้ส่งใหม่",
        view=AdminConfirmView(owner_id)
        )

# ===== หน้าร้าน =====
class StoreView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💵『 เติมเงิน 』",
        style=discord.ButtonStyle.green,
        custom_id="store_topup_btn"
)
    
    async def topup(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("❌ ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
            return

        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-{user.id}")
        if existing_channel:
            await interaction.response.send_message("❌ คุณมี Ticket อยู่แล้ว", ephemeral=True)
            return

        me = guild.me
        if me is None:
            await interaction.response.send_message("❌ ไม่พบบอทในเซิร์ฟเวอร์", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }

        admin_role = discord.utils.get(guild.roles, name="Admin")
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_messages=True
            )

        channel = await guild.create_text_channel(
            name=f"ticket-{user.id}",
            overwrites=overwrites
        )

        await interaction.response.send_message(f"✅ เปิด Ticket: {channel.mention}", ephemeral=True)

        embed = discord.Embed(
            title=" <a:coronafachera:1120400723893571705> **เติมเงิน** <a:coronafachera:1120400723893571705>",
            description=" <a:354100downarrow:1492618243184001186> **สแกน QR แล้วส่งสลิปในห้องนี้** <a:354100downarrow:1492618243184001186>",
            color=0x8A2BE2
        )

        embed.set_image(url="https://i.ibb.co/k6vBJ0Z9/004999020995030-20250502-202557.webp")
        await channel.send(content=user.mention, embed=embed)

class TicketPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💬 สอบถาม",
        style=discord.ButtonStyle.green,
        custom_id="buy_ticket_btn"
    )
    async def buy_ticket(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("❌ ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
            return

        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-buy-{user.id}")
        if existing_channel:
            await interaction.response.send_message("❌ คุณมี Ticket ซื้อสินค้าอยู่แล้ว", ephemeral=True)
            return

        me = guild.me
        if me is None:
            await interaction.response.send_message("❌ ไม่พบบอทในเซิร์ฟเวอร์", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }

        admin_role = discord.utils.get(guild.roles, name="Admin")
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_messages=True
            )

        channel = await guild.create_text_channel(
            name=f"ticket-buy-{user.id}",
            overwrites=overwrites
        )

        embed = discord.Embed(
            title=" <a:Party:1492208851699761303> Ticket สอบถาม <a:cinnamonwave:1120400698891325530>",
            description=(
                f"{user.mention}\n\n"
                " <a:5435blueishufo:1120385263684812982> สอบถามเพิ่มเติมได้ที่ห้องนี้\n"
                " <:aestheticplanet9:1120400668411314246> แอดมินจะเข้ามาตอบกลับให้โดยเร็วที่สุด"
            ),
            color=0x8A2BE2
        )
        embed.set_footer(text="BlackCat Store 🐾")

        await channel.send(embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ เปิด Ticket เรียบร้อย: {channel.mention}", ephemeral=True)

    @discord.ui.button(
        label="🚨 แจ้งปัญหา",
        style=discord.ButtonStyle.red,
        custom_id="report_ticket_btn"
    )
    async def report_ticket(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            await interaction.response.send_message("❌ ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
            return

        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-report-{user.id}")
        if existing_channel:
            await interaction.response.send_message("❌ คุณมี Ticket แจ้งปัญหาอยู่แล้ว", ephemeral=True)
            return

        me = guild.me
        if me is None:
            await interaction.response.send_message("❌ ไม่พบบอทในเซิร์ฟเวอร์", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }

        admin_role = discord.utils.get(guild.roles, name="Admin")
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_messages=True
            )

        channel = await guild.create_text_channel(
            name=f"ticket-report-{user.id}",
            overwrites=overwrites
        )

        embed = discord.Embed(
            title="<a:blacksirenalert2:1120400678846730301> Ticket แจ้งปัญหา",
            description=(
                f"{user.mention}\n\n"
                "<a:6544_heartarrow_purple:1120400637763518664> กรุณาอธิบายปัญหาที่พบในห้องนี้\n"
                "<a:4533_heartarrow_pink:1120400625763618948> หากมีรูปหรือหลักฐาน สามารถส่งได้เลย"
            ),
            color=0xED4245
        )
        embed.set_footer(text="BlackCat Store 🐾")

        await channel.send(embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ เปิด Ticket เรียบร้อย: {channel.mention}", ephemeral=True)

# ===== รับสลิป =====
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild and message.channel.name.startswith("ticket-"):
        owner_id = get_ticket_owner_id(message.channel.name)

        if owner_id is not None and message.author.id == owner_id and message.attachments:
            await message.channel.send(
                " <a:31830redloading:1494226259826311179> **กรุณารอสักครู่** <a:31830redloading:1494226259826311179>\n "
                "⠀⠀รอแอดมินยืนยัน⠀⠀",
                view=AdminConfirmView(owner_id)
            )

    await bot.process_commands(message)


# ===== คำสั่ง =====
@bot.command()
async def  point(ctx):
    embed = discord.Embed(
        title="<a:blackheart26:1120400673528360980> **BLACKCAT WALLET SYSTEM** <a:blackheart26:1120400673528360980>",
        description= 
    "❰ <a:529977coin:1492631678462464040> ❱ POINT BALANCE \n\n"
    "❰ <:630034mythic:1492631620514091259> ❱ RANK SYSTEM\n\n"
    "❰ <:114361playing:1492631641896779886> ❱ LEVEL PROGRESS\n\n"
    "❰ <a:25801:1492632503947624488> ❱ อัปเดตแบบเรียลไทม์ \n\n"
    "❰ <a:dbdailybox:1120400738150002728> ❱ แลกรางวัลได้ทันที",
        color=0x8A2BE2
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1000452582092845177/1492635264961745076/check_point_ss.gif?ex=69dc0c6a&is=69dabaea&hm=31ca158831b507cb7bf87cc6b3b503658d8337ada8fbc7425d3031f54e2873bd&")
    embed.set_footer(text="BlackCat Store 🐾")
    await ctx.send(embed=embed, view=PointView())


@bot.command()
async def โปร(ctx):
    embed = discord.Embed(
        title="🐈‍⬛ BlackCat Store",
        description="💜 เติมไว ปลอดภัย 100%\n💜 บริการ 24 ชม.",
        color=0x6A0DAD
    )
    await ctx.send(embed=embed)


@bot.command()
async def ร้าน(ctx):
    embed = discord.Embed(
        title=" <a:blackheart26:1120400673528360980> **BlackCat Store**  <a:blackheart26:1120400673528360980> ",
        description=" ❰ <a:307408kirby:1492617910634676335> ❱ กดเติมเงินด้านล่าง ❰ <a:354100downarrow:1492618243184001186> ❱\n\n  ❰ <a:4484pinkarrow:1120379420704780348> ❱ ส่งสลิปใน Ticket ได้เลยครับ  ❰<a:9182galaxystar2:1120385283880407050>❱",
        color=0x2B0D3A
    )
    embed.set_image(url="https://media.discordapp.net/attachments/1000452582092845177/1492633121840173309/topup_ss.gif?ex=69dc0a6c&is=69dab8ec&hm=d253b48a8916f8bf3eddfa632c3b6165809667db329efef68b394949ca96911f&=")
    embed.set_footer(text="BlackCat Store 🐾")
    await ctx.send(embed=embed, view=StoreView())


@bot.command()
async def top(ctx):
    with lock:
        cursor.execute("SELECT user_id, points FROM points ORDER BY points DESC, user_id ASC LIMIT 10")
        data = cursor.fetchall()

    embed = discord.Embed(title="🏆 อันดับแต้มสูงสุด", color=0xFFD700)

    if not data:
        embed.description = "ยังไม่มีข้อมูล"
    else:
        lines = []
        for i, (user_id, points) in enumerate(data, start=1):
            lines.append(f"**#{i}** <@{user_id}> — `{points}` แต้ม")
        embed.description = "\n".join(lines)

    await ctx.send(embed=embed)


@bot.command()
async def addpoint(ctx, member: discord.Member, amount: int):
    # เช็คว่าเป็นแอดมินไหม
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    # เช็คจำนวนแต้ม
    if amount <= 0:
        await ctx.send("❌ จำนวนแต้มต้องมากกว่า 0")
        return

    # เพิ่มแต้ม
    add_point(member.id, amount)
    pts = check_point(member.id)

    embed = discord.Embed(
        title="💸 เติมแต้มสำเร็จ",
        description=f"เพิ่ม {amount} แต้มให้ {member.mention}",
        color=0x00ff99
    )

    embed.add_field(
        name="💰 แต้มรวม",
        value=f"```{pts} แต้ม```",
        inline=False
    )

    await ctx.send(embed=embed)

@addpoint.error
async def addpoint_error(ctx, error):
    await ctx.send("❌ ใช้คำสั่งแบบนี้: !addpoint @user จำนวนแต้ม")

@bot.command()
async def removepoint(ctx, member: discord.Member, amount: int):
    # เช็คแอดมิน
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    # เช็คจำนวน
    if amount <= 0:
        await ctx.send("❌ จำนวนแต้มต้องมากกว่า 0")
        return

    # เช็คแต้มปัจจุบัน
    current = check_point(member.id)

    # กันติดลบ
    if amount > current:
        amount = current

    # ลดแต้ม (ใช้ add_point ติดลบ)
    add_point(member.id, -amount)
    pts = check_point(member.id)

    embed = discord.Embed(
        title="💸 ลดแต้มสำเร็จ",
        description=f"ลด {amount} แต้มจาก {member.mention}",
        color=0xff4444
    )

    embed.add_field(
        name="💰 แต้มคงเหลือ",
        value=f"```{pts} แต้ม```",
        inline=False
    )

    await ctx.send(embed=embed)

@removepoint.error
async def removepoint_error(ctx, error):
    await ctx.send("❌ ใช้คำสั่งแบบนี้: !removepoint @user จำนวนแต้ม")

@bot.command()
async def verify(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    embed = discord.Embed(
        title="<a:blackheart26:1120400673528360980> **BlackCat Verify System** <a:blackheart26:1120400673528360980>",
        description=(
            "<:aestheticplanet9:1120400668411314246> ยินดีต้อนรับสู่เซิร์ฟเวอร์ของเรา <a:9182galaxystar2:1120385283880407050>\n\n"
            "<a:307408kirby:1492617910634676335> กรุณากดปุ่มด้านล่างเพื่อ **ยืนยันตัวตน** <a:307408kirby:1492617910634676335>\n\n"
        ),
        color=0x8A2BE2
    )

    embed.add_field(
        name=" <a:9935catkeyboard:1120386440975634472> **วิธีการยืนยัน**",
        value="กดปุ่ม <a:check2:1120400694676037713> `ยืนยันตัวตน` ",
        inline=False
    )

    embed.add_field(
        name="<a:869575peachthecat:1492196945639510166> **หลังยืนยันแล้ว**",
        value="คุณจะได้รับยศ `Verified` สามารถเข้าห้องหลักได้",
        inline=False
    )

    embed.add_field(
        name="<a:blacksirenalert2:1120400678846730301> **หมายเหตุ**",
        value="หากกดแล้วไม่ได้ยศ <a:4484pinkarrow:1120379420704780348> ให้กด Ticket แจ้งปัญหา <a:Party:1492208851699761303> <#1099654087504560188> ",
        inline=False
    )

    embed.set_image(url="https://media.discordapp.net/attachments/1000452582092845177/1492633802105688215/verify_ss.gif?ex=69dc0b0e&is=69dab98e&hm=35f426c76718328e8cfc274aef0e4ed6148cb8a116200e777ee57012343539f4&=")
    embed.set_footer(text="BlackCat Store • Verify Panel")

    await ctx.send(embed=embed, view=VerifyView())

@bot.command()
async def panel(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    embed = discord.Embed(
        title=" <a:Party:1492208851699761303> **Ticket สอบถาม** <a:Party:1492208851699761303>\n\n",
        description=(
            "❰ <a:6544_heartarrow_purple:1120400637763518664> ❱ **กรุณากดให้ตรงตามหัวข้อที่ต้องการ**\n\n"
            "❰ <a:dbdailybox:1120400738150002728> ❱ **กรุณาอย่าเปิด Ticket เล่น**\n\n"
            "❰ <a:blacksirenalert2:1120400678846730301> ❱ **อ่านก่อนกด Tickets**<#1382711013836587048>\n\n"
            "❰ <a:9935catkeyboard:1120386440975634472> ❱ **หากแอดมินไม่ตอบทันที กรุณารอสักครู่\n**"
        ),
        color=0x5B2D91
    )

    

    embed.set_image(url="https://i.ibb.co/Jw9XtQqt/ticket-ss.gif")
    embed.set_footer(text="BlackCat Store • Ticket Panel")

    await ctx.send(embed=embed, view=TicketPanelView())

@bot.command()
async def redeempanel(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ ใช้ได้เฉพาะแอดมิน")
        return

    embed = discord.Embed(
        title="🎁 ระบบแลกเงิน",
        description=(
            "━━━━━━━━━━━━━━━━━━\n"
            "💸 **แลก 8 แต้ม รับทันที 30 บาท**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📌 สะสมแต้มให้ครบแล้วกดปุ่มด้านล่าง\n"
            "✅ แลกได้ไม่จำกัด ขอแค่แต้มพอ\n"
            "📝 ทุกการแลกจะถูกบันทึกในระบบ"
        ),
        color=0x8A2BE2
    )
    embed.set_footer(text="BlackCat Store 🐾")

    await ctx.send(embed=embed, view=RedeemMenuView())

@bot.command()
async def redeemhistory(ctx):
    logs = get_user_redeem_logs(ctx.author.id, limit=10)

    embed = discord.Embed(
        title="📝 ประวัติการแลกเงิน",
        description="รายการล่าสุดของคุณ",
        color=0x8A2BE2
    )

    if not logs:
        embed.description = "ยังไม่มีประวัติการแลก"
    else:
        lines = []
        for i, (points_used, reward_baht, redeemed_at) in enumerate(logs, start=1):
            lines.append(
                f"**#{i}** ใช้ `{points_used}` แต้ม แลก `{reward_baht}` บาท\n"
                f"⏰ `{redeemed_at}`"
            )
        embed.description = "\n\n".join(lines)

    embed.set_footer(text="BlackCat Store 🐾")
    await ctx.send(embed=embed)

@bot.command()
async def test(ctx):
    await ctx.send("ทำงานแล้ว!")

# ===== ออนไลน์ =====
@bot.event
async def on_ready():
    if not hasattr(bot, "persistent_views_added"):
        bot.add_view(PointView())
        bot.add_view(CloseTicketView())
        bot.add_view(StoreView())
        bot.add_view(VerifyView())
        bot.add_view(TicketPanelView())

        bot.persistent_views_added = True

    print(f"บอทออนไลน์: {bot.user}")

# ===== TOKEN =====

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("ไม่พบ DISCORD_TOKEN")

bot.run(TOKEN)
