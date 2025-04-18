import os
import json
import logging
import datetime
import requests
import sys
import locale
import oauthlib.oauth2.rfc6749.errors
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_session import Session
from requests_oauthlib import OAuth2Session
import discord
from discord.ext import commands
import asyncio
import threading
from threading import Lock
from dotenv import load_dotenv
from nlp_processor import process_text
from slip_processor import process_slip_image
import qrcode
from io import BytesIO
import base64
from PIL import Image
import psutil  # เพิ่มการนำเข้า psutil

# ตั้งค่า locale ให้รองรับภาษาไทย
try:
    locale.setlocale(locale.LC_ALL, 'th_TH.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_ALL, 'Thai_Thailand.utf8')
    except:
        pass

# ยกเว้น HTTPS สำหรับ OAuth2 (ใช้ในการพัฒนาเท่านั้น)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
load_dotenv()

# ตั้งค่า logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Discord OAuth2 settings
OAUTH2_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
OAUTH2_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '').strip()
OAUTH2_REDIRECT_URI = os.getenv('OAUTH2_REDIRECT_URI', 'http://localhost:5000/callback').strip()

AUTHORIZATION_BASE_URL = 'https://discord.com/api/oauth2/authorize'
TOKEN_URL = 'https://discord.com/api/oauth2/token'
API_BASE_URL = 'https://discord.com/api'

# ตั้งค่า Discord bot
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix='!', intents=intents)

# สร้าง Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "default_secret_key_replace_this")
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './.flask_session/'
Session(app)

# ตั้งค่าตัวแปรสำหรับเก็บข้อมูลการตั้งค่า
CONFIG_FILE = 'config.json'
DEFAULT_CONFIG = {
    "categories": {},
    "messages": {},
    "category_mapping": {},
    "products": [],
    "intents": {
        "unknown": {
            "responses": ["ขอโทษค่ะ ฉันไม่แน่ใจว่าเข้าใจถูกมั้ย ลองบอกใหม่ได้มั้ยคะ? 😊"]
        }
    },
    "channel_name_mappings": {},
    "role_mappings": [],
    "welcome_messages": {}
}

config = DEFAULT_CONFIG.copy()

# คลาสสำหรับจัดการล็อก
class LogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []
        self.max_logs = 1000

    def emit(self, record):
        log_entry = {
            'time': datetime.datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        self.logs.append(log_entry)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

class StdoutCapture:
    def __init__(self, logger):
        self.logger = logger
        self.original_stdout = sys.stdout
        
    def write(self, message):
        if message.strip():
            self.logger.info(f"[STDOUT] {message.strip()}")
        self.original_stdout.write(message)
        
    def flush(self):
        self.original_stdout.flush()

log_handler = LogHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(log_handler)
sys.stdout = StdoutCapture(logger)

GUILD_ID = None
channel_counter = 0

def load_config():
    global config, GUILD_ID
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(loaded_config)
                logger.info("โหลด config.json สำเร็จ")
        else:
            logger.warning("config.json not found, using default config")
            config = DEFAULT_CONFIG.copy()
                
        if GUILD_ID is None:
            GUILD_ID = int(os.getenv("GUILD_ID", "0"))
    except Exception as e:
        logger.error(f"Error loading config: {str(e)}")
        config = DEFAULT_CONFIG.copy()

def save_config():
    try:
        logger.info(f"กำลังบันทึกการตั้งค่าลงใน {CONFIG_FILE}")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logger.info("บันทึกไฟล์ config.json สำเร็จ")
        return True
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการบันทึกไฟล์ config: {str(e)}")
        return False

# PromptPay QR Code Generation (รองรับรูปแบบเบอร์ xxx-xxx-xxxx)
def generate_promptpay_qr(identifier, amount):
    try:
        # Clean identifier
        identifier_cleaned = identifier.replace('-', '')

        if len(identifier_cleaned) == 10 and identifier_cleaned.isdigit():
            # Assume phone number
            if identifier_cleaned.startswith('0'):
                identifier_cleaned = identifier_cleaned[1:]  # Remove leading 0
            identifier_formatted = '66' + identifier_cleaned
            identifier_padded = identifier_formatted.zfill(13)  # Pad to 13 digits
            subtag = "01"  # Subtag for phone number
        elif len(identifier_cleaned) == 13 and identifier_cleaned.isdigit():
            # Assume national ID
            identifier_padded = identifier_cleaned  # Use as is
            subtag = "02"  # Subtag for national ID
        else:
            raise ValueError("Identifier ต้องเป็นเบอร์โทรศัพท์ 10 หลักหรือเลขบัตรประชาชน 13 หลัก")

        # Check amount
        if amount <= 0:
            raise ValueError("จำนวนเงินต้องมากกว่า 0")

        # Create payload
        payload = "000201"  # Payload Format Indicator
        payload += "010212"  # Point of Initiation Method (12 = Dynamic QR)
        # Tag 29: Merchant Account Information
        aid = "A000000677010111"
        merchant_info = f"00{len(aid):02d}{aid}{subtag}13{identifier_padded}"
        payload += f"29{len(merchant_info):02d}{merchant_info}"
        payload += "5802TH"  # Country Code (TH)
        payload += "5303764"  # Currency Code (764 = THB)
        # Add amount
        amount_str = f"{amount:.2f}"
        payload += f"54{len(amount_str):02d}{amount_str}"  # Transaction Amount
        # Add CRC16 checksum
        payload += "6304"  # Checksum field
        checksum = calculate_crc(payload.encode('ascii'))
        payload += f"{hex(checksum)[2:].zfill(4).upper()}"

        # Create QR Code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=5,
            border=4,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        buffered.seek(0)
        logger.info(f"สร้าง QR Code สำเร็จสำหรับ {identifier} จำนวน {amount} บาท")
        return buffered
    except Exception as e:
        logger.error(f"Error generating PromptPay QR: {str(e)}")
        return None
    
# CRC16 Calculation
def calculate_crc(data: bytes) -> int:
    crc = 0xFFFF
    polynomial = 0x1021
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ polynomial
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

# Discord Bot Event Handlers
@bot.event
async def on_ready():
    logger.info(f"บอทเริ่มทำงานแล้ว: {bot.user.name}#{bot.user.discriminator}")
    try:
        await register_commands()
        await update_categories_info()
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดเมื่อบอทพร้อมทำงาน: {str(e)}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return
    
    if message.attachments:
        for attachment in message.attachments:
            if attachment.content_type.startswith('image/'):
                amount, bank_type, is_valid, response = await process_slip_image(attachment.url, str(message.author.id))
                await message.channel.send(response)
                return
    
    user_id = str(message.author.id)
    result = await process_text(message.content, user_id)
    await message.channel.send(result["response"])

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        logger.error(f"ไม่พบเซิร์ฟเวอร์ที่มี ID: {payload.guild_id}")
        return

    member = guild.get_member(payload.user_id)
    if not member:
        logger.error(f"ไม่พบสมาชิกที่มี ID: {payload.user_id}")
        return

    channel = guild.get_channel(payload.channel_id)
    if not channel:
        logger.error(f"ไม่พบช่องที่มี ID: {payload.channel_id}")
        return

    message = await channel.fetch_message(payload.message_id)
    if message.author != bot.user:
        return

    # ตรวจสอบการตั้งค่าการจัดการยศ
    for mapping in config.get("role_mappings", []):
        if str(channel.category_id) == mapping["category_id"]:
            emoji = str(payload.emoji)
            if emoji not in ['✅', '❌']:
                continue
            action = mapping["action"]
            role_id = mapping["role_id"]
            if not role_id:
                continue
            # ตรวจสอบว่า member ไม่มีสิทธิ์ admin
            if member.guild_permissions.administrator:
                await channel.send(f"{member.mention} คุณมีสิทธิ์ administrator ไม่สามารถเพิ่ม/ถอนยศได้")
                return
            try:
                role = guild.get_role(int(role_id))
                if not role:
                    await channel.send("ไม่พบยศที่ระบุในเซิร์ฟเวอร์")
                    return
                if emoji == '✅' and action == "add":
                    await member.add_roles(role)
                    await channel.send(f"เพิ่มยศ {role.name} ให้ {member.mention} เรียบร้อยแล้ว!")
                    logger.info(f"Added role {role_id} to {member.id} in channel {channel.id}")
                elif emoji == '❌' and action == "remove":
                    await member.remove_roles(role)
                    await channel.send(f"ถอนยศ {role.name} จาก {member.mention} เรียบร้อยแล้ว!")
                    logger.info(f"Removed role {role_id} from {member.id} in channel {channel.id}")
            except Exception as e:
                logger.error(f"Error handling role reaction: {str(e)}")
                await channel.send(f"เกิดข้อผิดพลาดในการจัดการยศ: {str(e)}")

@bot.event
async def on_guild_channel_create(channel):
    global channel_counter
    logger.info(f"มีการสร้างช่องใหม่: {channel.name} (ID: {channel.id})")
    channel_counter += 1
    await process_new_channel(channel)

async def process_new_channel(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    channel_name_lower = channel.name.lower()
    for category_name, category_data in config["category_mapping"].items():
        if "keywords" in category_data and any(keyword.lower() in channel_name_lower for keyword in category_data["keywords"]):
            category_id = category_data["category_id_1"]
            if category_id:
                try:
                    category = bot.get_channel(int(category_id))
                    if category and isinstance(category, discord.CategoryChannel):
                        await channel.edit(category=category)
                        logger.info(f"ย้ายช่อง {channel.name} ไปยัง Category {category.name}")
                        return
                except Exception as e:
                    logger.error(f"ไม่สามารถย้ายช่อง {channel.name} ได้: {str(e)}")
                    backup_category_id = category_data.get("category_id_2")
                    if backup_category_id:
                        try:
                            backup_category = bot.get_channel(int(backup_category_id))
                            if backup_category and isinstance(category, discord.CategoryChannel):
                                await channel.edit(category=backup_category)
                                logger.info(f"ย้ายช่อง {channel.name} ไปยัง Category สำรอง {backup_category.name}")
                                return
                        except Exception as e2:
                            logger.error(f"ไม่สามารถย้ายช่องไปยัง Category สำรองได้: {str(e2)}")

    for name, data in config.get("channel_name_mappings", {}).items():
        if str(channel.category_id) == data["category_id"]:
            try:
                new_name = data["custom_text"]
                if data["name_format"] == "date_prefix":
                    today = datetime.datetime.now().strftime("%d-%m")
                    new_name = f"{today}-{new_name}"
                elif data["name_format"] == "ticket_date_prefix":
                    today = datetime.datetime.now().strftime("%d-%m")
                    ticket_number = f"🎫{channel_counter:04d}"
                    new_name = f"{ticket_number}-{today}-{new_name}"
                await channel.edit(name=new_name)
                logger.info(f"เปลี่ยนชื่อช่อง {channel.name} เป็น {new_name}")
                break
            except Exception as e:
                logger.error(f"ไม่สามารถเปลี่ยนชื่อช่อง {channel.name} ได้: {str(e)}")

    # ส่งข้อความต้อนรับและตัวเลือกการชำระเงิน
    welcome_data = config.get("welcome_messages", {}).get(str(channel.category_id))
    if welcome_data and welcome_data.get("message") and welcome_data.get("options"):
        try:
            message = welcome_data["message"]
            options = welcome_data["options"]
            select_menu = discord.ui.Select(
                placeholder="เลือกตัวเลือกการชำระเงิน",
                options=[
                    discord.SelectOption(label=option["text"], value=str(i))
                    for i, option in enumerate(options)
                ]
            )

            async def select_callback(interaction):
                selected_index = int(select_menu.values[0])
                selected_option = options[selected_index]
                amount = selected_option["amount"]
                identifier = welcome_data["identifier"]
                qr_data = generate_promptpay_qr(identifier, amount)
                if qr_data:
                    await interaction.response.send_message(
                        content=f"ชำระเงิน {amount} บาทสำหรับ {selected_option['text']}",
                        file=discord.File(qr_data, filename="qrcode.png")
                    )
                else:
                    await interaction.response.send_message("ไม่สามารถสร้าง QR Code ได้ กรุณาติดต่อแอดมิน")

            select_menu.callback = select_callback
            view = discord.ui.View()
            view.add_item(select_menu)
            await channel.send(message, view=view)
            logger.info(f"ส่งข้อความต้อนรับและตัวเลือกการชำระเงินในช่อง {channel.name}")
        except Exception as e:
            logger.error(f"ไม่สามารถส่งข้อความต้อนรับในช่อง {channel.name}: {str(e)}")
            await channel.send("เกิดข้อผิดพลาดในการส่งข้อความต้อนรับ กรุณาติดต่อแอดมิน")

config_update_lock = Lock()

async def update_categories_info():
    global GUILD_ID
    with config_update_lock:
        try:
            await bot.wait_until_ready()
            guild = bot.get_guild(GUILD_ID)
            if not guild:
                logger.error(f"ไม่พบเซิร์ฟเวอร์ที่มี ID: {GUILD_ID}")
                if len(bot.guilds) > 0:
                    guild = bot.guilds[0]
                    GUILD_ID = guild.id
                    logger.info(f"ใช้เซิร์ฟเวอร์แรกที่พบแทน: {guild.name} (ID: {guild.id})")
                else:
                    logger.error("ไม่พบเซิร์ฟเวอร์ที่บอทเข้าถึงได้")
                    return
            categories = {str(category.id): category.name for category in guild.categories}
            config["categories"] = categories
            save_config()
            logger.info("อัปเดตข้อมูลหมวดหมู่และบันทึกลง config.json สำเร็จ")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการอัปเดตข้อมูลหมวดหมู่: {str(e)}")

command_register_lock = Lock()

async def register_commands():
    with command_register_lock:
        try:
            for command_name in list(config.get('messages', {}).keys()):
                if command_name.startswith('!'):
                    command_name = command_name[1:]
                if command_name in bot.all_commands:
                    bot.remove_command(command_name)
            
            for command_name, command_data in config.get('messages', {}).items():
                if command_name.startswith('!'):
                    command_name = command_name[1:]
                response_text = command_data.get("text", "")
                change_channel = command_data.get("change_channel", False)
                allow_additional_text = command_data.get("allow_additional_text", False)
                command_func = create_command_function(command_name, response_text, change_channel, allow_additional_text)
                bot.command(name=command_name)(command_func)
                logger.info(f"ลงทะเบียนคำสั่ง !{command_name} สำเร็จ")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการลงทะเบียนคำสั่ง: {str(e)}")

def create_command_function(cmd_name, response, should_change, allow_additional_text):
    async def command_func(ctx, additional_text: str = None):
        try:
            await ctx.send(response)
            if should_change:
                channel = ctx.channel
                current_name = channel.name
                new_name = current_name
                suffixes = [key for key in config["messages"].keys()]
                parts = new_name.split("-")
                if len(parts) > 1 and parts[-1] in suffixes:
                    new_name = "-".join(parts[:-1])
                elif len(parts) > 2 and parts[-2] in suffixes:
                    new_name = "-".join(parts[:-2])
                if allow_additional_text and additional_text:
                    new_name = f"{new_name}-{additional_text}-{cmd_name}"
                else:
                    new_name = f"{new_name}-{cmd_name}"
                if new_name != current_name:
                    try:
                        await channel.edit(name=new_name)
                        logger.info(f"เปลี่ยนชื่อช่องจาก '{current_name}' เป็น '{new_name}'")
                    except Exception as e:
                        logger.error(f"ไม่สามารถเปลี่ยนชื่อช่องได้: {str(e)}")

            # ตรวจสอบการจัดการยศ
            for mapping in config.get("role_mappings", []):
                if str(ctx.channel.category_id) == mapping["category_id"] and cmd_name == mapping["command"]:
                    action = mapping["action"]
                    role_id = mapping["role_id"]
                    if not role_id:
                        await ctx.send("ไม่พบยศที่ระบุในเซิร์ฟเวอร์")
                        return
                    guild = ctx.guild
                    role = guild.get_role(int(role_id))
                    if not role:
                        await ctx.send("ไม่พบยศที่ระบุในเซิร์ฟเวอร์")
                        return
                    message_content = (
                        f"กด ✅ เพื่อรับยศ: {role.name}" if action == "add"
                        else f"กด ❌ เพื่อถอนยศ: {role.name}"
                    )
                    message = await ctx.send(message_content)
                    await message.add_reaction("✅" if action == "add" else "❌")
                    logger.info(f"Sent role reaction message for command !{cmd_name} in channel {ctx.channel.id}")

        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในคำสั่ง !{cmd_name}: {str(e)}")
    command_func.__name__ = f"command_{cmd_name}"
    return command_func

@bot.command()
async def scan_channels(ctx):
    try:
        guild = ctx.guild
        moved_channels = 0
        for channel in guild.text_channels:
            channel_name_lower = channel.name.lower()
            for category_name, category_data in config["category_mapping"].items():
                if "keywords" in category_data and any(keyword.lower() in channel_name_lower for keyword in category_data["keywords"]):
                    category_id = category_data["category_id_1"]
                    if category_id:
                        category = bot.get_channel(int(category_id))
                        if category and isinstance(category, discord.CategoryChannel):
                            await channel.edit(category=category)
                            moved_channels += 1
                            logger.info(f"✅ ย้ายช่อง {channel.name} ไปยัง Category {category.name}")
                            break
        await ctx.send(f"✅ **ย้ายช่องทั้งหมด {moved_channels} ช่องเรียบร้อยแล้ว!**" if moved_channels > 0 else "⚠️ **ไม่พบช่องที่ต้องย้าย!**")
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในคำสั่ง !scan_channels: {str(e)}")
        await ctx.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")

# OAuth2 Helper Functions
def token_updater(token):
    session['oauth2_token'] = token

def make_session(token=None, state=None, scope=None):
    return OAuth2Session(
        client_id=OAUTH2_CLIENT_ID,
        token=token,
        state=state,
        scope=scope,
        redirect_uri=OAUTH2_REDIRECT_URI,
        auto_refresh_kwargs={
            'client_id': OAUTH2_CLIENT_ID,
            'client_secret': OAUTH2_CLIENT_SECRET
        },
        auto_refresh_url=TOKEN_URL,
        token_updater=token_updater
    )

# Flask Routes - Authentication
@app.route('/login')
def login():
    scope = ['identify', 'guilds']
    discord = make_session(scope=scope)
    authorization_url, state = discord.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth2_state'] = state
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    if request.values.get('error'):
        flash(f"ข้อผิดพลาดจาก Discord: {request.values.get('error')}", "danger")
        return redirect(url_for('index'))
    
    if 'oauth2_state' not in session:
        flash("เซสชันหมดอายุหรือไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "warning")
        return redirect(url_for('index'))
    
    try:
        discord = make_session(state=session['oauth2_state'])
        token = discord.fetch_token(
            TOKEN_URL,
            client_secret=OAUTH2_CLIENT_SECRET,
            authorization_response=request.url
        )
        session['oauth2_token'] = token
        logger.info("ผู้ใช้เข้าสู่ระบบสำเร็จ")
        return redirect(url_for('servers'))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในขั้นตอนการตรวจสอบสิทธิ์: {str(e)}")
        flash("เกิดข้อผิดพลาดในการตรวจสอบสิทธิ์ กรุณาลองอีกครั้ง", "danger")
        return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('oauth2_token', None)
    return redirect(url_for('index'))

# Flask Routes - Main Pages
@app.route('/')
def index():
    if 'oauth2_token' in session:
        return redirect(url_for('servers'))
    return render_template('login.html')

@app.route('/servers')
def servers():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    
    try:
        discord = make_session(token=session['oauth2_token'])
        try:
            user_guilds = discord.get(f'{API_BASE_URL}/users/@me/guilds').json()
            user_info = discord.get(f'{API_BASE_URL}/users/@me').json()
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            logger.warning("Discord token หมดอายุและไม่สามารถรีเฟรชได้")
            session.pop('oauth2_token', None)
            flash("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่อีกครั้ง", "warning")
            return redirect(url_for('login'))
        except (oauthlib.oauth2.rfc6749.errors.InvalidGrantError, 
                oauthlib.oauth2.rfc6749.errors.OAuth2Error) as e:
            logger.error(f"OAuth2 error: {str(e)}")
            session.pop('oauth2_token', None)
            flash("เกิดข้อผิดพลาดในการตรวจสอบสิทธิ์ กรุณาเข้าสู่ระบบใหม่", "danger")
            return redirect(url_for('login'))
        
        bot_guilds = [guild.id for guild in bot.guilds]
        accessible_guilds = []
        for guild in user_guilds:
            permissions = int(guild['permissions'])
            has_admin = (permissions & 0x8) == 0x8
            has_manage = (permissions & 0x20) == 0x20
            guild_in_bot = int(guild['id']) in bot_guilds
            guild['accessible'] = guild_in_bot and (has_admin or has_manage)
            accessible_guilds.append(guild)
        return render_template('servers.html', guilds=accessible_guilds, user=user_info)
    
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในหน้า servers: {str(e)}")
        flash(f"เกิดข้อผิดพลาด: {str(e)}", "danger")
        return redirect(url_for('index'))

@app.route('/dashboard/<guild_id>')
def dashboard(guild_id):
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    guild = bot.get_guild(int(guild_id))
    if not guild:
        flash("บอทไม่ได้อยู่ในเซิร์ฟเวอร์นี้", "danger")
        return redirect(url_for('servers'))
    discord = make_session(token=session['oauth2_token'])
    user_guilds = discord.get(f'{API_BASE_URL}/users/@me/guilds').json()
    user_in_guild = any(user_guild['id'] == guild_id and (int(user_guild['permissions']) & 0x28) for user_guild in user_guilds)
    if not user_in_guild:
        flash("คุณไม่มีสิทธิ์เข้าถึงเซิร์ฟเวอร์นี้", "danger")
        return redirect(url_for('servers'))
    guild_info = {
        'id': guild.id,
        'name': guild.name,
        'icon': guild.icon.url if guild.icon else None,
        'text_channels': len(guild.text_channels),
        'voice_channels': len(guild.voice_channels),
        'categories': len(guild.categories),
        'members': guild.member_count
    }
    return render_template('dashboard.html', guild=guild_info)

@app.route('/logs')
def logs():
    return render_template('logs.html', logs=log_handler.logs)

@app.route('/channels')
def channels():
    return render_template('channels.html')

@app.route('/chat')
def chat():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html')

@app.route('/system_monitor')
def system_monitor():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    return render_template('system_monitor.html')

# Flask Routes - Settings
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        try:
            if 'category_mapping' in request.form:
                new_mapping = json.loads(request.form['category_mapping'])
                config['category_mapping'] = new_mapping
            if 'messages' in request.form:
                new_messages = json.loads(request.form['messages'])
                for cmd in new_messages:
                    if "allow_additional_text" not in new_messages[cmd]:
                        new_messages[cmd]["allow_additional_text"] = False
                config['messages'] = new_messages
                asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
            save_config()
            flash("บันทึกการตั้งค่าเรียบร้อยแล้ว", "success")
            asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
            return redirect(url_for('settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('settings'))
    return render_template('settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/categories', methods=['GET', 'POST'])
def category_settings():
    if request.method == 'POST':
        try:
            if 'category_mapping' in request.form:
                new_mapping = json.loads(request.form['category_mapping'])
                config['category_mapping'] = new_mapping
                save_config()
                flash("บันทึกการตั้งค่าหมวดหมู่เรียบร้อยแล้ว", "success")
                asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
                return redirect(url_for('category_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าหมวดหมู่: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าหมวดหมู่: {str(e)}", "danger")
            return redirect(url_for('category_settings'))
    return render_template('category_settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/commands', methods=['GET', 'POST'])
def command_settings():
    if request.method == 'POST':
        try:
            if 'messages' in request.form:
                new_messages = json.loads(request.form['messages'])
                for cmd in new_messages:
                    if "allow_additional_text" not in new_messages[cmd]:
                        new_messages[cmd]["allow_additional_text"] = False
                config['messages'] = new_messages
                save_config()
                flash("บันทึกการตั้งค่าคำสั่งเรียบร้อยแล้ว", "success")
                asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
                return redirect(url_for('command_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าคำสั่ง: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าคำสั่ง: {str(e)}", "danger")
            return redirect(url_for('command_settings'))
    return render_template('command_settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/intents', methods=['GET', 'POST'])
def intent_settings():
    if request.method == 'POST':
        try:
            if 'intents' in request.form:
                new_intents = json.loads(request.form['intents'])
                logger.info(f"Received intents: {new_intents}")
                config['intents'] = new_intents
                save_config()
                flash("บันทึกการตั้งค่าเจตนาเรียบร้อยแล้ว", "success")
            else:
                logger.warning("No intents found in form data")
                flash("ไม่พบข้อมูลเจตนาในฟอร์ม", "warning")
            return redirect(url_for('intent_settings'))
        except Exception as e:
            logger.error(f"Error saving intent settings: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('intent_settings'))
    return render_template('intent_settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/channel_names', methods=['GET', 'POST'])
def channel_name_settings():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            if 'channel_name_mappings' in request.form:
                new_mappings = json.loads(request.form['channel_name_mappings'])
                config['channel_name_mappings'] = {
                    mapping['name']: {
                        'category_id': mapping['category_id'],
                        'name_format': mapping['name_format'],
                        'custom_text': mapping['custom_text']
                    } for mapping in new_mappings
                }
                if save_config():
                    flash("บันทึกการตั้งชื่อช่องเรียบร้อยแล้ว", "success")
                    logger.info("บันทึกการตั้งชื่อช่องสำเร็จ")
                else:
                    flash("เกิดข้อผิดพลาดในการบันทึกไฟล์ config.json", "danger")
                    logger.error("ไม่สามารถบันทึกไฟล์ config.json ได้")
                return redirect(url_for('channel_name_settings'))
            else:
                flash("ไม่พบข้อมูลการตั้งชื่อช่องในฟอร์ม", "warning")
                logger.warning("ไม่พบ channel_name_mappings ในฟอร์ม")
                return redirect(url_for('channel_name_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งชื่อช่อง: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งชื่อช่อง: {str(e)}", "danger")
            return redirect(url_for('channel_name_settings'))
    
    return render_template('manage_channel_names.html', config=config, now=datetime.datetime.now())

@app.route('/settings/roles', methods=['GET', 'POST'])
def role_settings():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    
    try:
        future = asyncio.run_coroutine_threadsafe(get_guild_roles(), bot.loop)
        roles = future.result(timeout=10)
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงยศ: {str(e)}")
        flash(f"ไม่สามารถดึงข้อมูลยศได้: {str(e)}", "danger")
        roles = []

    if request.method == 'POST':
        try:
            if 'role_mappings' in request.form:
                new_mappings = json.loads(request.form['role_mappings'])
                config['role_mappings'] = new_mappings
                if save_config():
                    flash("บันทึกการตั้งค่าการจัดการยศเรียบร้อยแล้ว", "success")
                    logger.info("บันทึกการตั้งค่าการจัดการยศสำเร็จ")
                else:
                    flash("เกิดข้อผิดพลาดในการบันทึกไฟล์ config.json", "danger")
                    logger.error("ไม่สามารถบันทึกไฟล์ config.json ได้")
                return redirect(url_for('role_settings'))
            else:
                flash("ไม่พบข้อมูลการตั้งค่าการจัดการยศในฟอร์ม", "warning")
                logger.warning("ไม่พบ role_mappings ในฟอร์ม")
                return redirect(url_for('role_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าการจัดการยศ: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('role_settings'))
    
    return render_template('manage_roles.html', config=config, roles=roles, now=datetime.datetime.now())

@app.route('/settings/welcome_messages', methods=['GET', 'POST'])
def welcome_message_settings():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            if 'welcome_messages' in request.form:
                new_messages = json.loads(request.form['welcome_messages'])
                config['welcome_messages'] = {
                    str(category_id): {
                        'message': msg_data['message'],
                        'identifier': msg_data['identifier'],
                        'options': msg_data['options']
                    } for category_id, msg_data in new_messages.items()
                }
                if save_config():
                    flash("บันทึกการตั้งค่าข้อความต้อนรับเรียบร้อยแล้ว", "success")
                    logger.info("บันทึกการตั้งค่าข้อความต้อนรับสำเร็จ")
                else:
                    flash("เกิดข้อผิดพลาดในการบันทึกไฟล์ config.json", "danger")
                    logger.error("ไม่สามารถบันทึกไฟล์ config.json ได้")
                return redirect(url_for('welcome_message_settings'))
            else:
                flash("ไม่พบข้อมูลข้อความต้อนรับในฟอร์ม", "warning")
                logger.warning("ไม่พบ welcome_messages ในฟอร์ม")
                return redirect(url_for('welcome_message_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าข้อความต้อนรับ: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('welcome_message_settings'))
    
    return render_template('welcome_message_settings.html', config=config, now=datetime.datetime.now())

# Flask Routes - API Endpoints
@app.route('/api/logs')
def api_logs():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    logs_page = log_handler.logs[start_idx:end_idx]
    return jsonify({'logs': logs_page, 'total': len(log_handler.logs), 'page': page, 'per_page': per_page})

@app.route('/api/categories')
def get_categories():
    try:
        return jsonify(config["categories"])
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงข้อมูลหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/categories/refresh', methods=['POST'])
def refresh_categories():
    try:
        asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
        return jsonify({"success": True, "message": "เริ่มรีเฟรชข้อมูลหมวดหมู่แล้ว"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการรีเฟรชหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/category_mapping', methods=['GET'])
def get_category_mapping():
    try:
        return jsonify(config["category_mapping"])
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงข้อมูลการแมปหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/category_mapping', methods=['POST'])
def update_category_mapping():
    try:
        data = request.json
        if not data or "name" not in data:
            logger.warning("ข้อมูลการแมปหมวดหมู่ไม่ถูกต้อง")
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        category_name = data["name"]
        if category_name not in config["category_mapping"]:
            config["category_mapping"][category_name] = {"category_id_1": "", "category_id_2": "", "keywords": []}
        config["category_mapping"][category_name].update({k: data[k] for k in data if k in ["category_id_1", "category_id_2", "keywords"]})
        save_config()
        logger.info(f"อัปเดตการแมปหมวดหมู่ {category_name} สำเร็จ")
        return jsonify({"success": True, "message": "อัปเดตหมวดหมู่สำเร็จ"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการอัปเดตหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/category_mapping/<category_name>', methods=['DELETE'])
def delete_category_mapping(category_name):
    try:
        if category_name in config["category_mapping"]:
            del config["category_mapping"][category_name]
            save_config()
            logger.info(f"ลบหมวดหมู่ {category_name} สำเร็จ")
            return jsonify({"success": True, "message": f"ลบหมวดหมู่ {category_name} สำเร็จ"})
        logger.warning(f"ไม่พบหมวดหมู่ {category_name}")
        return jsonify({"success": False, "message": "ไม่พบหมวดหมู่นี้"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการลบหมวดหมู่ {category_name}: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/messages', methods=['GET'])
def get_messages():
    try:
        return jsonify(config["messages"])
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงข้อมูลคำสั่ง: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/messages', methods=['POST'])
def update_messages():
    try:
        data = request.json
        if not data or "name" not in data:
            logger.warning("ข้อมูลคำสั่งไม่ถูกต้อง")
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        command_name = data["name"].lstrip('!')
        config["messages"][command_name] = {
            "text": data.get("text", ""),
            "change_channel": data.get("change_channel", False),
            "allow_additional_text": data.get("allow_additional_text", False)
        }
        save_config()
        asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
        logger.info(f"อัปเดตคำสั่ง !{command_name} สำเร็จ")
        return jsonify({"success": True, "message": f"อัปเดตคำสั่ง !{command_name} สำเร็จ"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการอัปเดตคำสั่ง: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/messages/<command_name>', methods=['DELETE'])
def delete_message(command_name):
    try:
        command_name = command_name.lstrip('!')
        if command_name in config["messages"]:
            del config["messages"][command_name]
            save_config()
            asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
            logger.info(f"ลบคำสั่ง !{command_name} สำเร็จ")
            return jsonify({"success": True, "message": f"ลบคำสั่ง !{command_name} สำเร็จ"})
        logger.warning(f"ไม่พบคำสั่ง !{command_name}")
        return jsonify({"success": False, "message": "ไม่พบคำสั่งนี้"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการลบคำสั่ง !{command_name}: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/intents', methods=['GET'])
def get_intents():
    try:
        return jsonify(config["intents"])
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงข้อมูลเจตนา: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/intents', methods=['POST'])
def update_intents():
    try:
        data = request.json
        if not data or "name" not in data:
            logger.warning("ข้อมูลเจตนาไม่ถูกต้อง")
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        intent_name = data["name"]
        if intent_name not in config["intents"]:
            config["intents"][intent_name] = {"keywords": [], "responses": []}
        config["intents"][intent_name].update({
            "keywords": data.get("keywords", []),
            "responses": data.get("responses", [])
        })
        save_config()
        logger.info(f"อัปเดตเจตนา {intent_name} สำเร็จ")
        return jsonify({"success": True, "message": f"อัปเดตเจตนา {intent_name} สำเร็จ"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการอัปเดตเจตนา: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/intents/<intent_name>', methods=['DELETE'])
def delete_intent(intent_name):
    try:
        if intent_name in config["intents"]:
            del config["intents"][intent_name]
            save_config()
            logger.info(f"ลบเจตนา {intent_name} สำเร็จ")
            return jsonify({"success": True, "message": f"ลบเจตนา {intent_name} สำเร็จ"})
        logger.warning(f"ไม่พบเจตนา {intent_name}")
        return jsonify({"success": False, "message": "ไม่พบเจตนานี้"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการลบเจตนา {intent_name}: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/channels/count')
def get_channels_count():
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.error("ไม่พบเซิร์ฟเวอร์")
            return jsonify({'success': False, 'message': 'ไม่พบเซิร์ฟเวอร์', 'count': 0}), 404
        text_channels_count = len(guild.text_channels)
        voice_channels_count = len(guild.voice_channels)
        categories_count = len(guild.categories)
        channels_by_category = {category.name: len(category.channels) for category in guild.categories}
        logger.info("ดึงข้อมูลจำนวนช่องสำเร็จ")
        return jsonify({
            'success': True,
            'text_channels_count': text_channels_count,
            'voice_channels_count': voice_channels_count,
            'categories_count': categories_count,
            'total_channels': text_channels_count + voice_channels_count,
            'channels_by_category': channels_by_category
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการนับจำนวนช่อง: {str(e)}")
        return jsonify({'success': False, 'message': str(e), 'count': 0}), 500

@app.route('/api/chat', methods=['POST'])
def chat_with_bot():
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            logger.warning("ไม่พบข้อความในคำขอแชท")
            return jsonify({'success': False, 'message': 'กรุณาส่งข้อความ'}), 400
        
        message = data['message']
        user_id = session.get('oauth2_token', {}).get('user_id', 'web_user')
        
        result = asyncio.run(process_text(message, user_id))
        
        load_config()
        product_name = None
        if result.get("product_id"):
            product = next((p for p in config["products"] if p["id"] == result.get("product_id")), None)
            product_name = product["name"] if product else None
        
        logger.info(f"ประมวลผลแชทสำเร็จสำหรับผู้ใช้ {user_id}")
        return jsonify({
            'success': True,
            'reply': result["response"],
            'tokens': result["tokens"],
            'intent': result.get("intent"),
            'product_name': product_name
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการสนทนากับบอท: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/generate_qr', methods=['POST'])
def generate_qr():
    try:
        data = request.get_json()
        if not data or 'identifier' not in data or 'amount' not in data:
            logger.warning("ข้อมูลสำหรับสร้าง QR ไม่ครบถ้วน")
            return jsonify({'success': False, 'message': 'กรุณาระบุ identifier และ amount'}), 400
        
        identifier = data['identifier']
        amount = float(data['amount'])
        
        # ตรวจสอบและแปลงรูปแบบเบอร์โทรศัพท์
        identifier_cleaned = identifier.replace('-', '')
        if not identifier_cleaned.isdigit() or len(identifier_cleaned) != 10:
            logger.warning("identifier ไม่ถูกต้อง")
            return jsonify({'success': False, 'message': 'identifier ต้องเป็นเบอร์โทรศัพท์ประเทศไทย 10 หลัก (เช่น 095-431-8865)'}), 400

        qr_buffer = generate_promptpay_qr(identifier, amount)
        if not qr_buffer:
            return jsonify({'success': False, 'message': 'ไม่สามารถสร้าง QR code ได้'}), 500

        qr_data = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')
        logger.info(f"สร้าง QR code สำเร็จสำหรับ {identifier} จำนวน {amount} บาท")
        return jsonify({
            'success': True,
            'qr_data': qr_data,
            'transaction_id': f"trans_{int(datetime.datetime.now().timestamp())}",
            'payment_method': 'promptpay'
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการสร้าง QR code: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/server-status')
def server_status():
    try:
        return jsonify({'online': bot.is_ready()})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการตรวจสอบสถานะเซิร์ฟเวอร์: {str(e)}")
        return jsonify({'online': False}), 500

@app.route('/api/system_stats')
def system_stats():
    try:
        # ดึงข้อมูล CPU และ Memory
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_total = memory.total / (1024 ** 3)  # แปลงเป็น GB
        memory_used = memory.used / (1024 ** 3)    # แปลงเป็น GB
        memory_percent = memory.percent
        return jsonify({
            'success': True,
            'cpu_percent': cpu_percent,
            'memory_total': round(memory_total, 2),
            'memory_used': round(memory_used, 2),
            'memory_percent': memory_percent
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงข้อมูลระบบ: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

async def get_guild_roles():
    try:
        await bot.wait_until_ready()
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.error(f"ไม่พบเซิร์ฟเวอร์ที่มี ID: {GUILD_ID}")
            return []
        roles = [{"id": str(role.id), "name": role.name} for role in guild.roles if not role.permissions.administrator]
        logger.info("ดึงข้อมูลยศสำเร็จ")
        return roles
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการดึงยศ: {str(e)}")
        return []

def run_bot():
    try:
        logger.info(f"Starting bot with TOKEN: {TOKEN[:10]}...")
        logger.info(f"Using GUILD_ID: {GUILD_ID if GUILD_ID is not None else 'Not set yet'}")
        asyncio.run(bot.start(TOKEN))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการเริ่มบอท: {str(e)}")
        raise

if __name__ == '__main__':
    load_config()
    loop = asyncio.get_event_loop()
    
    def run_flask():
        try:
            logger.info("Starting Flask server...")
            app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการเริ่ม Flask server: {str(e)}")
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    try:
        logger.info(f"Starting bot with TOKEN: {TOKEN[:10]}...")
        logger.info(f"Using GUILD_ID: {GUILD_ID if GUILD_ID is not None else 'Not set yet'}")
        loop.run_until_complete(bot.start(TOKEN))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการเริ่มบอท: {str(e)}")
    finally:
        try:
            loop.run_until_complete(bot.close())
            logger.info("บอทปิดการทำงานเรียบร้อย")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการปิดบอท: {str(e)}")
        loop.close()
        logger.info("Event loop ปิดเรียบร้อย")