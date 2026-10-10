import requests
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
import json
import time
import re
import logging
import os
import sys
import random
import string
import threading
from datetime import datetime, timedelta
from telebot import TeleBot, types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, MessageEntity
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import hashlib
try:
    from dotenv import load_dotenv  # optional: reads secrets from .env
    load_dotenv()
except ImportError:
    pass
import html as _html
from collections import OrderedDict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── HTTP session — defined early so make_api_request can use it anywhere ──
def create_session():
    _sess = requests.Session()
    _adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=Retry(total=0),  # no adapter retries — fallbacks handled in code
        pool_block=False
    )
    _sess.mount('http://', _adapter)
    _sess.mount('https://', _adapter)
    _sess.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive'
    })
    return _sess

session = create_session()

# ============================================================
# CONTACT STORAGE — MongoDB only (Turso removed)
# ============================================================

# Premium emoji IDs for the green captcha button
CAPTCHA_BTN_EMOJI_1 = "5864127571754489150"
CAPTCHA_BTN_EMOJI_2 = "6080263490163973583"

# ── Contact-gate state dicts ────────────────────────────────────
_LAST_CAPCHA_MESSAGE: dict = {}   # uid -> captcha prompt message_id
_LAST_FJ_MESSAGE:     dict = {}   # uid -> force-join message_id
_PENDING_REF:         dict = {}   # uid -> referral id (survives the gate)


def _is_contact_verified(uid: int) -> bool:
    """
    Check if user is verified — MongoDB only.
    True if phone field is present and non-empty.
    """
    try:
        doc = _col_users.find_one({"user_id": uid}, {"phone": 1, "contact_verified": 1})
        if doc and (doc.get("phone") or doc.get("contact_verified")):
            return True
    except Exception as e:
        print(f"[IS_VERIFIED_MONGO] {e}")
    return False


def _mark_contact_verified(uid: int, phone: str):
    """
    Mark user verified — MongoDB only.
    upsert=True so phone saves even if user doc does not exist yet.
    """
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _col_users.update_one(
            {"user_id": uid},
            {
                "$set": {
                    "phone": phone,
                    "phone_verified_at": now,
                    "contact_verified": True,
                },
                "$setOnInsert": {
                    "username": "N/A",
                    "first_name": "Unknown",
                    "credits": 0,
                    "is_admin": 0,
                    "referred_by": None,
                    "referral_count": 0,
                    "join_date": now,
                    "last_active": now,
                    "total_commands": 0,
                    "joined_channel1": 0,
                    "joined_channel2": 0,
                    "joined_channel3": 0,
                    "is_blocked": 0,
                },
            },
            upsert=True,
        )
        print(f"[MARK_VERIFIED] uid={uid} phone={phone} saved")
    except Exception as e:
        print(f"[MARK_VERIFIED_MONGO] {e}")


def _save_pending_ref(uid: int, ref):
    """Stash referral so it survives the contact-gate redirect."""
    if ref:
        _PENDING_REF[uid] = ref


def _KB(text: str, request_contact: bool = False, style: str = None,
        icon_custom_emoji_id: str = None, **kwargs):
    """
    Build a ReplyKeyboardButton with optional API 9.4 style/icon.
    NOTE: HTML tags are NOT rendered inside ReplyKeyboard button text.
    Use icon_custom_emoji_id for premium emoji on the button.
    """
    try:
        btn = types.KeyboardButton(
            text=text,
            request_contact=request_contact,
            icon_custom_emoji_id=icon_custom_emoji_id,
            **kwargs,
        )
    except TypeError:
        # Older pyTelegramBotAPI without icon_custom_emoji_id support
        btn = types.KeyboardButton(text=text, request_contact=request_contact)
    if style:
        try:
            btn.style = style
        except Exception:
            pass
    return btn


# ── Turso removed — MongoDB is sole store — stubs prevent crashes ──
TURSO_URL    = ""
TURSO_TOKEN  = ""
TURSO_HEADERS = {}

def turso_execute(sql: str, args=None):
    return {}

def turso_init():
    print("✅ Turso removed — MongoDB only")

def turso_is_verified(user_id: int) -> bool:
    return False

def turso_save_contact(user_id: int, phone: str):
    pass
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ===== PREMIUM EMOJI IDs =====
PREMIUM_EMOJI_IDS = [
"4956420859771225351",
"4958626617535497157",
"4958734459869332468",
"4956282853882069908",
"4956247175588742152",
"5296491800423340716",
"4956739572114392015",
"4956606007221421405",
"4958617898751886363",
"4956290155326473271",
"4958714479681471536",
"4956461073550017373",
"4956719506027185156",
"5116414868357907335",
"4958587679361991667",
"4956560549287560231",
"4956539525422646612",
"6127546175240280564",
"6127558265573218459",
"5084613633418199991",
"5098265504796115765",
"5116296632203216188",
"6147565374289220368",
"6147464060305676048",
"6235459831302460476",
"6235355429237430006",
"6129434968713076807",
"6129828611055689014",
"6129700535130922338"
]

def get_random_premium_emoji_id():
    return random.choice(PREMIUM_EMOJI_IDS)

# ============================================================
# PREMIUM EMOJI MAP — NUMBER INFO SPECIFIC
# ============================================================
NUM_EMOJIS = {
    "header_divider": "5467641505525016018",   # ➿
    "phone_title":    "6172503852785342178",   # 📞
    "status_ok":      "6129812419028982717",   # ✅
    "query":          "5379999674193172777",   # 🔭
    "records":        "5197269100878907942",   # ✍️
    "record_badge":   "5249231689695115145",   # 📰
    "number":         "6026092115631543342",   # 📲
    "name":           "5425124408786197150",   # 👤
    "father":         "5258513401784573443",   # 👥
    "alt_num":        "5453965363286925977",   # 📞
    "circle":         "5895476993314524652",   # 🔎
    "address":        "4958485609464202497",   # 🏡
    "id_card":        "5985796637971191405",   # 💳
    "email":          "5303416490295304868",   # 📧
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
}
# ============================================================
# PREMIUM EMOJI MAP — AADHAR INFO SPECIFIC
# ============================================================
ADHR_EMOJIS = {
    "header_divider": "5467641505525016018",   # ➿
    "adhr_title":     "6030753228889526497",   # 🪪
    "status_ok":      "6071022434234930063",   # ✅
    "query":          "5379999674193172777",   # 🔭
    "records":        "5197269100878907942",   # ✍️
    "record_badge":   "5249231689695115145",   # 📰
    "mobile":         "6026092115631543342",   # 📲
    "name":           "5425124408786197150",   # 👤
    "circle":         "5375365992891312468",   # 🛜
    "address":        "4958485609464202497",   # 🏡
    "id_card":        "5985796637971191405",   # 💳
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5931368295545443065",   # 📊
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
    "father":         "5258513401784573443",   # 👥
    "email":          "5303416490295304868",   # 📧
}

def build_adhr_divider() -> str:
    d = te(ADHR_EMOJIS["header_divider"], "➿")
    return (d * 8) + "\n"

# ============================================================
# PREMIUM EMOJI MAP — VEHICLE / RC SPECIFIC
# ============================================================
VECH_EMOJIS = {
    "header_divider": "5467641505525016018",   # ➿
    "veh_title":      "5323272789824654613",   # 🪪
    "rc_number":      "6093653759378858331",   # 🚗
    "source":         "5798417071796653494",   # 💎
    "status_ok":      "6129812419028982717",   # ✅
    "vehicle":        "5312322066328853156",   # 🚗
    "make":           "6077707920198338212",   # 🏭
    "mfg_year":       "6021757076290675447",   # 🗓️
    "fuel":           "5359588280205605798",   # ⚡️
    "vclass":         "6226775738142432221",   # 👿
    "cc":             "4904936030232117798",   # ⚙️
    "seating":        "5260732577026696524",   # 🐸
    "colour":         "5926764846518376076",   # 📋
    "body":           "6233262422954613958",   # 🏎
    "owner":          "5832263552982851814",   # 👨‍🦰
    "mobile":         "6104960836311195547",   # 📲
    "address":        "4958485609464202497",   # 🏡
    "registration":   "5226929552319594190",   # 0️⃣
    "reg_date":       "5472279086657199080",   # 🗓️
    "rto":            "6303001048185309018",   # 🗺
    "rto_code":       "5422449357125350081",   # 🔢
    "financer":       "5264733042710181045",   # 🏢
    "commercial":     "5796205953913196373",   # 💎
    "insurance":      "5188688567970308198",   # 🏎
    "ins_status":     "6300604035527346062",   # 🛡
    "technical":      "5341715473882955310",   # ⚙️
    "chassis":        "5258023599419171861",   # 🔧
    "engine":         "4967657001023701985",   # ⚙️
    "norms":          "4904936030232117798",   # ⚙️
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
    "query":          "5379999674193172777",   # 🔭
    "records":        "5197269100878907942",   # ✍️
    "record_badge":   "5249231689695115145",   # 📰
}

def build_vech_divider() -> str:
    d = te(VECH_EMOJIS["header_divider"], "➿")
    return (d * 12) + "\n"


# ============================================================
# PREMIUM EMOJI MAP — INSTAGRAM SPECIFIC
# ============================================================
INSTA_EMOJIS = {
    "header_divider": "5467641505525016018",   # ➿
    "insta_title":    "6197411888553270114",   # 📸
    "username":       "6122805733936342496",   # 👤
    "name":           "6183633702187176982",   # 👑
    "user_id":        "5193037303961920951",   # 🆔
    "status":         "6035239240625820254",   # 🔒
    "acc_type":       "6091402470666279677",   # 💼
    "joined":         "5348227245599105972",   # 📅
    "profile":        "6305370152145787193",   # ❔
    "followers":      "5890937706803894250",   # 👥
    "following":      "5310095671246733180",   # 🔄
    "posts":          "5301276827782755360",   # 📱
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
}

def build_insta_divider() -> str:
    d = te(INSTA_EMOJIS["header_divider"], "➿")
    return (d * 11) + "\n"


def te(emoji_id: str, fallback: str = "⭐") -> str:
    """Shorthand: build a <tg-emoji> tag. Must use double quotes — Telegram HTML parser rejects single quotes."""
    return '<tg-emoji emoji-id="' + emoji_id + '">' + fallback + '</tg-emoji>'

def build_num_divider() -> str:
    d = te(NUM_EMOJIS["header_divider"], "➿")
    return (d * 8) + "\n"


# ===== PREMIUM EMOJI REPLACEMENT SYSTEM =====
EMOJI_TO_PREMIUM = {
    "⚠": "4956420859771225351",
    "⚡": "4958626617535497157",
    "✅": "4958734459869332468",
    "✏": "4956282853882069908",
    "✨": "4956247175588742152",
    "❌": "5296491800423340716",
    "❓": "4956739572114392015",
    "❤": "4956606007221421405",
    "➕": "4958617898751886363",
    "➖": "4956290155326473271",
    "➡": "4958714479681471536",
    "🌍": "4956461073550017373",
    "🌐": "4956719506027185156",
    "🌟": "5116414868357907335",
    "🎂": "4958587679361991667",
    "🎉": "4956560549287560231",
    "🎥": "4956539525422646612",
    "🎨": "6127546175240280564",
    "🎫": "6127558265573218459",
    "🎬": "5084613633418199991",
    "🎮": "5098265504796115765",
    "🏆": "5116414868357907335",
    "🏙": "5116296632203216188",
    "🏠": "6147565374289220368",
    "🏦": "6147464060305676048",
    "🐙": "6235459831302460476",
    "👋": "6235355429237430006",
    "👑": "6129434968713076807",
    "👤": "6129828611055689014",
    "👥": "6129700535130922338",
    "👦": "4956420859771225351",
    "👧": "4958626617535497157",
    "👨": "4958734459869332468",
    "👩": "4956282853882069908",
    "💡": "4956247175588742152",
    "💬": "5296491800423340716",
    "💰": "4956739572114392015",
    "💳": "4956606007221421405",
    "📄": "4958617898751886363",
    "📅": "4956290155326473271",
    "📈": "4958714479681471536",
    "📊": "4956461073550017373",
    "📋": "4956719506027185156",
    "📌": "5116414868357907335",
    "📍": "4958587679361991667",
    "📚": "4956560549287560231",
    "📛": "4956539525422646612",
    "📝": "6127546175240280564",
    "📞": "6127558265573218459",
    "📢": "5084613633418199991",
    "📥": "5098265504796115765",
    "📧": "5116414868357907335",
    "📨": "5116296632203216188",
    "📮": "6147565374289220368",
    "📱": "6147464060305676048",
    "📸": "6235459831302460476",
    "📺": "6235355429237430006",
    "🔄": "6129434968713076807",
    "🔍": "6129828611055689014",
    "🔐": "6129700535130922338",
    "🔑": "4956420859771225351",
    "🔒": "4958626617535497157",
    "🔓": "4958734459869332468",
    "🔔": "4956282853882069908",
    "🔗": "4956247175588742152",
    "🔘": "5296491800423340716",
    "🔙": "4956739572114392015",
    "🔢": "4956606007221421405",
    "🔥": "4958617898751886363",
    "🔧": "4956290155326473271",
    "🕐": "4958714479681471536",
    "🖼": "4956461073550017373",
    "🗑": "4956719506027185156",
    "🗺": "5116414868357907335",
    "🚗": "4958587679361991667",
    "🚫": "4956560549287560231",
    "🛑": "4956539525422646612",
    "🟢": "6127546175240280564",
    "🧪": "6127558265573218459",
    "⭐": "5116414868357907335",
    "🆔": "6129828611055689014",
    "🆕": "6129700535130922338",
    "🎰": "4956560549287560231",
}

def replace_with_premium_emojis(text):
    """Replace all free emojis in text with random premium Telegram emoji tags.
    Existing <tg-emoji> tags are preserved untouched."""
    if not text or not isinstance(text, str):
        return text

    import re

    # Step 1: Protect existing <tg-emoji> tags
    tg_emoji_pattern = re.compile(r'<tg-emoji emoji-id=["\'"]\d+["\'"]>[^<]*</tg-emoji>')
    placeholders = []
    def protect(match):
        placeholders.append(match.group(0))
        return f"[[P_{len(placeholders)-1}]]"
    text = tg_emoji_pattern.sub(protect, text)

    # Step 2: Replace free emojis with premium tags
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"   # Emoticons 😀-🙏
        "\U0001F300-\U0001F5FF"   # Misc Symbols & Pictographs 🌀-🗿
        "\U0001F680-\U0001F6FF"   # Transport & Map 🚀-🛿
        "\U0001F1E0-\U0001F1FF"   # Flags 🇠-🇿
        "\U00002702-\U000027B0"   # Dingbats ✂-➰
        "\U0001F900-\U0001F9FF"   # Supplemental Symbols & Pictographs
        "\U0001FA00-\U0001FA6F"   # Chess Symbols
        "\U0001FA70-\U0001FAFF"   # Symbols & Pictographs Extended-A
        "\U00002600-\U000026FF"   # Misc Symbols ☀-⛿
        "\U00002B00-\U00002BFF"   # Misc Symbols & Arrows
        "\U00002300-\U000023FF"   # Misc Technical
        "\U0001F7E0-\U0001F7FF"   # Geometric Shapes Extended
        "\U0000200D"                # ZWJ
        "\U000024C2-\U000024FF"   # Enclosed Alphanumerics Ⓜ-Ⓩ
        "\U0001F000-\U0001F02F"   # Mahjong Tiles
        "\U0001F0A0-\U0001F0FF"   # Playing Cards
        "\U0001F200-\U0001F2FF"   # Enclosed Ideographic Supplement
        "]+", flags=re.UNICODE
    )

    def replace_emoji(match):
        random_id = get_random_premium_emoji_id()
        return '<tg-emoji emoji-id="' + random_id + '">⭐</tg-emoji>'

    text = emoji_pattern.sub(replace_emoji, text)

    # Step 3: Restore protected tags
    for i, original in enumerate(placeholders):
        text = text.replace(f"[[P_{i}]]", original)

    return text

def truncate_text(text, max_length=10000):
    """Truncate text safely without breaking HTML tags"""
    if not text:
        return text
    if len(text) <= max_length:
        return text

    truncate_msg = "\n\n<blockquote>... (truncated - download JSON for full data)</blockquote>"
    available = max_length - len(truncate_msg)

    # Find last complete </blockquote> before max_length
    search_text = text[:available]
    last_close = search_text.rfind('</blockquote>')

    if last_close > 0:
        cut_point = last_close + len('</blockquote>')
        return text[:cut_point] + truncate_msg
    else:
        return text[:available] + truncate_msg


def _sanitise_html(text: str) -> str:
    """
    Protect Telegram HTML parse_mode from raw exception strings.
    Leaves valid Telegram HTML tags untouched; escapes bare < > & that
    are NOT part of a recognised Telegram HTML construct.
    Known safe tags: b, i, u, s, code, pre, a, blockquote, tg-emoji,
                     tg-spoiler, br, span.
    """
    import re as _re
    SAFE = re.compile(
        r'</?(?:b|i|u|s|code|pre|a|blockquote|tg-emoji|tg-spoiler|br|span)'
        r'(?:\s[^<>]*)?>',
        _re.IGNORECASE
    )
    result = []
    pos = 0
    for m in SAFE.finditer(text):
        # escape everything before this safe tag
        chunk = text[pos:m.start()]
        chunk = chunk.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        result.append(chunk)
        result.append(m.group())          # keep the safe tag as-is
        pos = m.end()
    # tail after last safe tag
    tail = text[pos:]
    tail = tail.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    result.append(tail)
    return ''.join(result)


def send_with_premium(
    chat_id,
    text,
    reply_markup=None,
    parse_mode="HTML",
    reply_to_message_id=None,
    disable_notification=False,
    protect_content=False
):
    if parse_mode == "HTML":
        text = _sanitise_html(str(text))
    text = replace_with_premium_emojis(text)

    if "<tg-emoji" not in text:
        premium_emoji_id = get_random_premium_emoji_id()
        text = text + ' <tg-emoji emoji-id="' + premium_emoji_id + '">⭐</tg-emoji>'
    return bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        reply_to_message_id=reply_to_message_id,
        disable_notification=disable_notification,
        protect_content=protect_content
    )

def edit_with_premium(chat_id, message_id, text, reply_markup=None, parse_mode='HTML'):
    if parse_mode == "HTML":
        text = _sanitise_html(str(text))
    text = replace_with_premium_emojis(text)

    if "<tg-emoji" not in text:
        premium_emoji_id = get_random_premium_emoji_id()
        text = text + ' <tg-emoji emoji-id="' + premium_emoji_id + '">⭐</tg-emoji>'

    try:
        return bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"Premium emoji edit failed: {e}")
        try:
            return bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup
            )
        except:
            return None


SEARCH_FRAMES = [
    '<tg-emoji emoji-id="5219943216781995020">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▱▱▱▱▱▱▱▱▱▱ 0%</blockquote>',
    '<tg-emoji emoji-id="5244820603663296299">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▱▱▱▱▱▱▱▱▱ 10%</blockquote>',
    '<tg-emoji emoji-id="5334811859415477124">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▱▱▱▱▱▱▱▱ 20%</blockquote>',
    '<tg-emoji emoji-id="5289862389552919154">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▱▱▱▱▱▱▱ 30%</blockquote>',
    '<tg-emoji emoji-id="5391034312759980875">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▱▱▱▱▱▱ 40%</blockquote>',
    '<tg-emoji emoji-id="5431374840332302296">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▱▱▱▱▱ 50%</blockquote>',
    '<tg-emoji emoji-id="5237761614458933049">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▰▱▱▱▱ 60%</blockquote>',
    '<tg-emoji emoji-id="5258274739041883702">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▰▰▱▱▱ 70%</blockquote>',
    '<tg-emoji emoji-id="5219943216781995020">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▰▰▰▱▱ 80%</blockquote>',
    '<tg-emoji emoji-id="5244820603663296299">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▰▰▰▰▱ 90%</blockquote>',
    '<tg-emoji emoji-id="5258274739041883702">⚡</tg-emoji> 𝗦𝗲𝗮𝗿𝗰𝗵𝗶𝗻𝗴\n\n<blockquote>▰▰▰▰▰▰▰▰▰▰ 100%</blockquote>',
]

def send_searching_animation(chat_id):
    """Send animated search loader. Returns (msg, stop_event) — call stop_event.set() when API done."""
    stop_event = threading.Event()
    try:
        msg = bot.send_message(
            chat_id=chat_id,
            text=SEARCH_FRAMES[0],
            parse_mode="HTML"
        )

        def _animate(msg_obj, stop):
            for frame in SEARCH_FRAMES[1:]:
                time.sleep(0.5)
                if stop.is_set():
                    break
                try:
                    bot.edit_message_text(
                        chat_id=msg_obj.chat.id,
                        message_id=msg_obj.message_id,
                        text=frame,
                        parse_mode="HTML"
                    )
                except Exception:
                    break

        t = threading.Thread(target=_animate, args=(msg, stop_event), daemon=True)
        t.start()

        # attach stop_event to msg object so caller can reach it
        msg._stop_event = stop_event
        return msg

    except Exception as e:
        logger.error(f"search animation error: {e}")
        fake_msg = bot.send_message(chat_id=chat_id, text="🔍 Searching...", parse_mode="HTML")
        fake_msg._stop_event = threading.Event()
        return fake_msg


def stop_searching_animation(chat_id, message_id, final_text, reply_markup=None):
    """Kill animation and replace with result."""
    try:
        return edit_with_premium(chat_id, message_id, final_text, reply_markup=reply_markup)
    except Exception:
        return send_with_premium(chat_id, final_text, reply_markup=reply_markup)



# ============================================================
# BUTTON FUNCTIONS WITH STYLES AND **kwargs SUPPORT
# ============================================================

# ── TELEGRAM BOT API 9.4 — BUTTON STYLE SUPPORT ─────────────
# style="primary" = Blue  | style="success" = Green | style="danger" = Red
# icon_custom_emoji_id  = premium emoji shown as button icon
# NOTE: style + icon_custom_emoji_id work on BOTH InlineKeyboard and ReplyKeyboard
#       but <tg-emoji> HTML tags are NOT rendered inside ReplyKeyboard button text.

def _make_btn(text, callback_data=None, url=None, style=None, **kwargs):
    """
    Build InlineKeyboardButton with API 9.4 style + optional premium emoji icon.
    Pass icon_custom_emoji_id='EMOJI_ID' as kwarg for premium emoji button icons.
    """
    if callback_data is None and url is None:
        callback_data = "noop"
    # icon_custom_emoji_id — pass directly to InlineKeyboardButton if supported
    try:
        btn = InlineKeyboardButton(text=text, callback_data=callback_data, url=url, **kwargs)
    except TypeError:
        # older telebot — strip unsupported kwargs gracefully
        safe_kwargs = {k: v for k, v in kwargs.items()
                       if k not in ("icon_custom_emoji_id",)}
        btn = InlineKeyboardButton(text=text, callback_data=callback_data, url=url, **safe_kwargs)
    # API 9.4 style attribute
    if style:
        try:
            btn.style = style
        except Exception:
            pass
    return btn


def create_primary_button(text, callback_data=None, url=None, **kwargs):
    """Blue button — style=primary (API 9.4)"""
    return _make_btn(text, callback_data=callback_data, url=url, style="primary", **kwargs)

def create_success_button(text, callback_data=None, url=None, **kwargs):
    """Green button — style=success (API 9.4)"""
    return _make_btn(text, callback_data=callback_data, url=url, style="success", **kwargs)

def create_danger_button(text, callback_data=None, url=None, **kwargs):
    """Red button — style=danger (API 9.4)"""
    return _make_btn(text, callback_data=callback_data, url=url, style="danger", **kwargs)

def create_default_button(text, callback_data=None, url=None, **kwargs):
    """Default button — no style"""
    return _make_btn(text, callback_data=callback_data, url=url, **kwargs)

class SimpleCache:
    def __init__(self, maxsize=100, ttl=300):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = OrderedDict()
        self.timestamps = {}

    def get(self, key):
        if key in self.cache:
            if time.time() - self.timestamps[key] < self.ttl:
                value = self.cache.pop(key)
                self.cache[key] = value
                return value
            else:
                del self.cache[key]
                del self.timestamps[key]
        return None

    def set(self, key, value):
        if key in self.cache:
            del self.cache[key]
        elif len(self.cache) >= self.maxsize:
            oldest = next(iter(self.cache))
            del self.cache[oldest]
            del self.timestamps[oldest]
        self.cache[key] = value
        self.timestamps[key] = time.time()

    def clear(self):
        self.cache.clear()
        self.timestamps.clear()

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")   # ← BOT TOKEN YAHAN DALO
ADMIN_ID = int(os.getenv("ADMIN_ID", "8994408644"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@flirts_x")

# ── Channel Broadcast Feature ──────────────────────────────────
# Set this to your channel's chat_id (negative number, e.g. -1001234567890)
# Bot must be admin in that channel.
# Any post sent to the channel will auto-broadcast as a forward to all users + groups.
BROADCAST_CHANNEL_ID = int(os.getenv("BROADCAST_CHANNEL_ID")) if os.getenv("BROADCAST_CHANNEL_ID", "").strip() else None   # <-- fill this in, e.g. -1001234567890

API_BASE_URL = os.getenv("API_BASE_URL", "PASTE_YOUR_MAIN_API_BASE_URL")   # ← MAIN API BASE URL
API_KEY = os.getenv("API_KEY", "PASTE_YOUR_API_KEY")   # ← API KEY

FORCE_JOIN_ENABLED = True

JSON_FILES = {}
NUM_PAGINATION = {}   # user_id -> {"pages": [(txt, markup), ...], "json_file": path}
LAST_PROCESSED_UPDATE_ID = 0
API_CACHE = SimpleCache(maxsize=100, ttl=300)
EXECUTOR = ThreadPoolExecutor(max_workers=10)

POSTS_CACHE = {}

API_ENDPOINTS = {
    "tgid": "/api?key={key}&type=tg&term={query}",
    "num": "/api/number?key={key}&num={query}",
    "name": "/api/name?key={key}&name={query}",
    "pan": "/api/pan?key={key}&pan={query}",
    "vech": "/api/vehicle?key={key}&vehicle={query}",
    "vechrc": "/api/rc?key={key}&owner={query}",
    "ip": "/api/ip?key={key}&ip={query}",
    "pin": "/api/pincode?key={key}&pin={query}",
    "git": "/api/git?key={key}&username={query}",
    "bgmi": "/api/bgmi?key={key}&uid={query}",
    "ff": "/api/ff?key={key}&uid={query}",
    "adhr": "/api/aadhar?key={key}&num={query}",
    "upi": "/api/upi?key={key}&upi={query}",
    "ifsc": "/api/ifsc?key={key}&ifsc={query}",
    "family": "PASTE_API_HERE&id={query}"   # ← API OF FAMILY INFO,
}

NEW_USER_CREDITS = 10
REFERRAL_BONUS_REFERRER = 5
REFERRAL_BONUS_NEW = 10
COMMAND_COST = 1
AUTO_DELETE_TIME = 120

# ── MongoDB ───────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://USERNAME:PASSWORD@YOUR-CLUSTER.mongodb.net/?retryWrites=true&w=majority")   # ← APNI MONGODB URI DALO

print("=" * 60)
print("⏳ Connecting to MongoDB Atlas...")
try:
    _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    # Force an actual connection check
    _mongo_client.admin.command("ping")
    print("✅ MongoDB Connected Successfully!")
    try:
        print(f"   Host : {_mongo_client.address}")
    except Exception:
        print("   Host : (connected)")
    print(f"   DB   : osint_bot")
except Exception as _mongo_err:
    print(f"❌ MongoDB Connection FAILED: {_mongo_err}")
    print("   Bot cannot start without database. Fix the URI or network.")
    sys.exit(1)
print("=" * 60)

_db = _mongo_client["osint_bot"]

# Collections
_col_users       = _db["users"]
_col_referrals   = _db["referrals"]
_col_codes       = _db["redeem_codes"]
_col_code_usage  = _db["code_usage"]
_col_channels    = _db["force_join_channels"]
_col_api_config  = _db["api_config"]
_col_media_vid   = _db["media_videos"]
_col_media_photo = _db["media_photos"]
_col_media_set   = _db["media_settings"]
_col_groups      = _db["group_chats"]

# Indexes
try:
    _col_users.create_index("user_id", unique=True)
    _col_codes.create_index("code", unique=True)
    _col_code_usage.create_index([("code", ASCENDING), ("user_id", ASCENDING)], unique=True)
    _col_channels.create_index("url", unique=True)
    _col_api_config.create_index("command_name", unique=True)
    _col_groups.create_index("chat_id", unique=True)
    _col_media_vid.create_index("file_id")
    _col_media_photo.create_index("file_id", unique=True)
    print("✅ MongoDB Indexes Ready")
except Exception as _idx_err:
    print(f"⚠️  Index creation warning: {_idx_err}")

db_lock = Lock()   # kept for compat — Mongo driver is thread-safe
# ──────────────────────────────────────────────────────────────

bot = TeleBot(BOT_TOKEN, num_threads=20)

BOT_LOCKED = False
BOT_START_TIME = datetime.now()
BLOCKED_USERS = set()

API_STATUS = {
    "tgid": {"working": True, "message": None},
    "num": {"working": True, "message": None},
    "num2": {"working": True, "message": None},
    "name": {"working": True, "message": None},
    "pan": {"working": True, "message": None},
    "vech": {"working": True, "message": None},
    "vechrc": {"working": True, "message": None},
    "ip": {"working": True, "message": None},
    "pin": {"working": True, "message": None},
    "git": {"working": True, "message": None},
    "bgmi": {"working": True, "message": None},
    "ff": {"working": True, "message": None},
    "adhr": {"working": True, "message": None},
    "upi": {"working": True, "message": None},
    "ifsc": {"working": True, "message": None},
    "insta": {"working": True, "message": None},
    "ai": {"working": True, "message": None},
    "family": {"working": True, "message": None},
    "leak": {"working": True, "message": None},
    "snap": {"working": True, "message": None},
}

def check_api_status(command):
    status = API_STATUS.get(command, {"working": True, "message": None})
    return status["working"], status.get("message")

def set_api_status(command, working, message=None):
    if command in API_STATUS:
        API_STATUS[command]["working"] = working
        API_STATUS[command]["message"] = message
        return True
    return False

MEDIA_MODE = "both"

def init_media_tables():
    """Media collections created at module load."""
    print("Media gallery collections ready")

def load_media_mode():
    global MEDIA_MODE
    try:
        doc = _col_media_set.find_one({"setting_key": "media_mode"})
        if doc:
            MEDIA_MODE = doc["setting_value"]
        else:
            MEDIA_MODE = "both"
            _col_media_set.insert_one({
                "setting_key": "media_mode", "setting_value": "both",
                "updated_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        print(f"Media mode loaded: {MEDIA_MODE}")
    except Exception as e:
        print(f"Load media mode error: {e}")

def set_media_mode(mode, admin_id):
    global MEDIA_MODE
    if mode not in ["video", "photo", "both"]:
        return False, "Invalid mode! Use: video, photo, or both"
    try:
        _col_media_set.update_one(
            {"setting_key": "media_mode"},
            {"$set": {"setting_value": mode, "updated_by": admin_id,
                      "updated_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True
        )
        MEDIA_MODE = mode
        return True, f"Media mode set to: {mode.upper()}"
    except Exception as e:
        return False, f"Error: {e}"

def add_video(file_id, description, admin_id):
    try:
        _col_media_vid.insert_one({
            "file_id": file_id, "description": description, "added_by": admin_id,
            "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "is_active": 1
        })
        return True, "Video added!"
    except Exception as e:
        print(f"Add video error: {e}")
        return False, f"Error: {str(e)}"

def add_photo(file_id, description, admin_id):
    try:
        if _col_media_photo.find_one({"file_id": file_id}):
            return False, "Photo already exists! (Duplicate)"
        _col_media_photo.insert_one({
            "file_id": file_id, "description": description, "added_by": admin_id,
            "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "is_active": 1
        })
        return True, "Photo added successfully!"
    except DuplicateKeyError:
        return False, "Photo already exists! (Duplicate)"
    except Exception as e:
        return False, f"Error: {e}"

def delete_video(video_id, admin_id):
    from bson import ObjectId
    try:
        res = _col_media_vid.delete_one({"_id": ObjectId(str(video_id))})
        if res.deleted_count:
            return True, f"Deleted {res.deleted_count} video(s)!"
        return False, "Video not found!"
    except Exception as e:
        return False, f"Error: {e}"

def delete_photo(photo_id, admin_id):
    from bson import ObjectId
    try:
        res = _col_media_photo.delete_one({"_id": ObjectId(str(photo_id))})
        if res.deleted_count:
            return True, "Deleted photo successfully!"
        return False, "Photo not found!"
    except Exception as e:
        return False, f"Error: {e}"

def get_random_video():
    try:
        docs = list(_col_media_vid.aggregate([
            {"$match": {"is_active": 1, "description": {"$ne": None}}},
            {"$sample": {"size": 1}}
        ]))
        if docs:
            return {"file_id": docs[0]["file_id"], "description": docs[0].get("description") or "Video", "type": "video"}
        return None
    except Exception as e:
        print(f"Get random video error: {e}")
        return None

def get_random_photo():
    try:
        docs = list(_col_media_photo.aggregate([
            {"$match": {"is_active": 1}},
            {"$sample": {"size": 1}}
        ]))
        if docs:
            return {"file_id": docs[0]["file_id"], "description": docs[0].get("description") or "Photo", "type": "photo"}
        return None
    except Exception as e:
        print(f"Get random photo error: {e}")
        return None

def get_all_videos():
    try:
        docs = list(_col_media_vid.find({"is_active": 1}).sort("_id", -1))
        return [(str(d["_id"]), d["file_id"], d.get("description", "Video"), d.get("added_by"), d.get("added_date")) for d in docs]
    except Exception as e:
        print(f"Get all videos error: {e}")
        return []

def get_all_photos():
    try:
        docs = list(_col_media_photo.find({"is_active": 1}).sort("_id", -1))
        return [(str(d["_id"]), d["file_id"], d.get("description", "Photo"), d.get("added_by"), d.get("added_date")) for d in docs]
    except Exception as e:
        print(f"Get all photos error: {e}")
        return []

def get_video_count():
    try:
        return _col_media_vid.count_documents({"is_active": 1})
    except:
        return 0

def get_photo_count():
    try:
        return _col_media_photo.count_documents({"is_active": 1})
    except:
        return 0

def send_random_media(chat_id, reply_to_message_id=None):
    try:
        if MEDIA_MODE == "video":
            video = get_random_video()
            if video:
                vcap = replace_with_premium_emojis(f"🎬 {video['description']}")
                bot.send_video(chat_id, video["file_id"], caption=vcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                return True
        elif MEDIA_MODE == "photo":
            photo = get_random_photo()
            if photo:
                pcap = replace_with_premium_emojis(f"🖼️ {photo['description']}")
                bot.send_photo(chat_id, photo["file_id"], caption=pcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                return True
        elif MEDIA_MODE == "both":
            video_count = get_video_count()
            photo_count = get_photo_count()
            if video_count > 0 and photo_count > 0:
                if random.choice([True, False]):
                    video = get_random_video()
                    if video:
                        vcap = replace_with_premium_emojis(f"🎬 {video['description']}")
                        bot.send_video(chat_id, video["file_id"], caption=vcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                        return True
                else:
                    photo = get_random_photo()
                    if photo:
                        pcap = replace_with_premium_emojis(f"🖼️ {photo['description']}")
                        bot.send_photo(chat_id, photo["file_id"], caption=pcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                        return True
            elif video_count > 0:
                video = get_random_video()
                if video:
                    vcap = replace_with_premium_emojis(f"🎬 {video['description']}")
                    bot.send_video(chat_id, video["file_id"], caption=vcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                    return True
            elif photo_count > 0:
                photo = get_random_photo()
                if photo:
                    pcap = replace_with_premium_emojis(f"🖼️ {photo['description']}")
                    bot.send_photo(chat_id, photo["file_id"], caption=pcap, parse_mode='HTML', reply_to_message_id=reply_to_message_id)
                    return True
    except Exception as e:
        print(f"Send random media error: {e}")
    return False


def init_database():
    """MongoDB collections + indexes created at module load."""
    print("MongoDB Database Ready")

def load_blocked_users():
    global BLOCKED_USERS
    try:
        docs = _col_users.find({"is_blocked": 1}, {"user_id": 1})
        BLOCKED_USERS = {doc["user_id"] for doc in docs}
        print(f"Loaded {len(BLOCKED_USERS)} blocked users")
    except Exception as e:
        print(f"Error loading blocked users: {e}")

def block_user_in_db(user_id):
    try:
        _col_users.update_one({"user_id": user_id}, {"$set": {"is_blocked": 1}})
        BLOCKED_USERS.add(user_id)
        return True
    except Exception:
        return False

def unblock_user_in_db(user_id):
    global BLOCKED_USERS
    try:
        _col_users.update_one({"user_id": user_id}, {"$set": {"is_blocked": 0}})
        BLOCKED_USERS.discard(user_id)
        return True
    except Exception:
        return False

def check_user_access(user_id):
    if user_id == ADMIN_ID:
        return True
    if user_id in BLOCKED_USERS:
        return False
    if BOT_LOCKED:
        return False
    return True

def get_access_error_message(user_id):
    if user_id in BLOCKED_USERS:
        return f"""
BANNED

YOU HAVE BEEN BANNED

Contact Admin: {OWNER_USERNAME}
"""
    elif BOT_LOCKED:
        return """
BOT LOCKED

BOT IS LOCKED BY ADMIN

Please wait for admin to unlock the bot
"""
    return None
def get_db():
    """Legacy stub — MongoDB used directly. Returns None."""
    return None

def get_user(user_id):
    try:
        return _col_users.find_one({"user_id": user_id})
    except Exception:
        return None

def create_user(user_id, username, first_name, referred_by=None):
    try:
        if _col_users.find_one({"user_id": user_id}):
            return False
        join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        credits = NEW_USER_CREDITS
        is_admin_flag = 1 if user_id == ADMIN_ID else 0
        ref_id = None
        if referred_by and referred_by != user_id:
            referrer = _col_users.find_one({"user_id": referred_by})
            if referrer:
                _col_users.update_one({"user_id": referred_by},
                    {"$inc": {"credits": REFERRAL_BONUS_REFERRER, "referral_count": 1}})
                _col_referrals.insert_one({
                    "referrer_id": referred_by, "referred_id": user_id,
                    "date": join_date, "credits_given": REFERRAL_BONUS_REFERRER
                })
                credits += REFERRAL_BONUS_NEW
                ref_id = referred_by
        _col_users.insert_one({
            "user_id": user_id, "username": username or "N/A",
            "first_name": first_name or "User", "credits": credits,
            "is_admin": is_admin_flag, "referred_by": ref_id,
            "referral_count": 0, "join_date": join_date, "last_active": join_date,
            "total_commands": 0, "joined_channel1": 0, "joined_channel2": 0,
            "joined_channel3": 0, "is_blocked": 0,
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception:
        return False

def get_credits(user_id):
    try:
        doc = _col_users.find_one({"user_id": user_id}, {"credits": 1})
        return doc["credits"] if doc else 0
    except Exception:
        return 0

def deduct_credit(user_id):
    try:
        doc = _col_users.find_one({"user_id": user_id}, {"credits": 1})
        if not doc:
            return True
        if doc["credits"] <= 0:
            return False
        _col_users.update_one({"user_id": user_id}, {
            "$inc": {"credits": -1, "total_commands": 1},
            "$set": {"last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        })
        return True
    except Exception:
        return True

def add_credits(user_id, amount):
    try:
        _col_users.update_one({"user_id": user_id}, {"$inc": {"credits": amount}})
        return True
    except Exception:
        return False

def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True
    try:
        doc = _col_users.find_one({"user_id": user_id}, {"is_admin": 1})
        return bool(doc and doc.get("is_admin") == 1)
    except Exception:
        return False

def check_code_used_by_user(code, user_id):
    try:
        return bool(_col_code_usage.find_one({"code": code.upper(), "user_id": user_id}))
    except Exception as e:
        print(f"Check code usage error: {e}")
        return False

def record_code_usage(code, user_id):
    try:
        _col_code_usage.insert_one({
            "code": code.upper(), "user_id": user_id,
            "used_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        return True
    except DuplicateKeyError:
        return True
    except Exception as e:
        print(f"Record code usage error: {e}")
        return False

def redeem_credits(user_id, code):
    try:
        if not _col_users.find_one({"user_id": user_id}):
            return False, "User not found! Please /start first."
        if _col_code_usage.find_one({"code": code, "user_id": user_id}):
            return False, "You have already used this redeem code! Each code can be used only once per user."
        doc = _col_codes.find_one({"code": code})
        if not doc:
            return False, "Invalid code! Code does not exist."
        if not doc.get("is_active", 1):
            return False, "Code is inactive! Contact admin."
        expiry_date = doc.get("expiry_date")
        if expiry_date:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d %H:%M:%S")
            if expiry < datetime.now():
                return False, "Code expired!"
        if doc.get("used_count", 0) >= doc["max_uses"]:
            return False, "Code max usage reached! This code has been fully used."
        credits = doc["credits"]
        _col_users.update_one({"user_id": user_id}, {"$inc": {"credits": credits}})
        try:
            _col_code_usage.insert_one({
                "code": code, "user_id": user_id,
                "used_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        except DuplicateKeyError:
            pass
        _col_codes.update_one({"code": code}, {"$inc": {"used_count": 1}})
        new_balance = get_credits(user_id)
        return True, f"Successfully redeemed {credits} credits! New balance: {new_balance}"
    except Exception as e:
        print(f"Redeem error: {e}")
        return False, f"Error: {str(e)}"

def create_redeem_code(code, credits, max_uses, expiry_date, admin_id):
    try:
        _col_codes.insert_one({
            "code": code.upper(), "credits": credits, "max_uses": max_uses,
            "used_count": 0, "expiry_date": expiry_date, "created_by": admin_id,
            "created_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "is_active": 1
        })
        return True, f"Code created: {code}"
    except DuplicateKeyError:
        return False, "Code already exists!"
    except Exception as e:
        return False, str(e)

def delete_redeem_code(code, admin_id):
    try:
        doc = _col_codes.find_one({"code": code.upper(), "is_active": 1})
        if not doc:
            return False, "Code not found or already inactive!"
        _col_codes.update_one({"code": code.upper()}, {"$set": {"is_active": 0}})
        return True, (f"Code `{doc['code']}` deleted successfully!\n\n"
                      f"Stats:\nCredits: {doc['credits']}\nMax Uses: {doc['max_uses']}\n"
                      f"Used: {doc.get('used_count', 0)} times")
    except Exception as e:
        return False, f"Error: {str(e)}"

def get_bot_stats():
    try:
        stats = {}
        stats['total_users'] = _col_users.count_documents({})
        agg = list(_col_users.aggregate([{"$group": {
            "_id": None,
            "total_credits":   {"$sum": "$credits"},
            "total_commands":  {"$sum": "$total_commands"},
            "total_referrals": {"$sum": "$referral_count"},
        }}]))
        if agg:
            stats['total_credits']   = agg[0].get("total_credits", 0)
            stats['total_commands']  = agg[0].get("total_commands", 0)
            stats['total_referrals'] = agg[0].get("total_referrals", 0)
        else:
            stats['total_credits'] = stats['total_commands'] = stats['total_referrals'] = 0
        today = datetime.now().strftime("%Y-%m-%d")
        stats['today_users'] = _col_users.count_documents({"join_date": {"$regex": f"^{today}"}})
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        stats['active_users'] = _col_users.count_documents({"last_active": {"$gt": yesterday}})
        return stats
    except Exception:
        return {}

PROMOTION_CHANNELS = []

def get_all_group_chats():
    try:
        docs = _col_groups.find({"is_active": 1}, {"chat_id": 1})
        return [d["chat_id"] for d in docs]
    except Exception as e:
        print(f"Error fetching group chats: {e}")
        return []

def load_channels_from_db():
    global PROMOTION_CHANNELS
    try:
        docs = list(_col_channels.find({"enabled": 1}))
        if docs:
            PROMOTION_CHANNELS = [{
                "url": d["url"],
                "username": d["username"],
                "enabled": bool(d.get("enabled", 1)),
                "is_private": bool(d.get("is_private", 0)),
                "join_by_request": bool(d.get("join_by_request", 0)),
            } for d in docs]
        else:
            default_channels = [
                ("https://t.me/bynxgc", "bynxgc", True),
                ("https://t.me/Osintxarmy", "Osintxarmy", True),
                ("https://t.me/osint_by_nx", "OSINT_BY_NX", True)
            ]
            for url, username, enabled in default_channels:
                try:
                    _col_channels.insert_one({
                        "url": url, "username": username, "enabled": 1,
                        "added_by": ADMIN_ID, "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                except DuplicateKeyError:
                    pass
            PROMOTION_CHANNELS = [{"url": url, "username": username, "enabled": enabled} for url, username, enabled in default_channels]
        print(f"Loaded {len(PROMOTION_CHANNELS)} force join channels")
    except Exception as e:
        print(f"Error loading channels: {e}")

def add_force_join_channel(url, username, admin_id):
    try:
        _col_channels.insert_one({
            "url": url, "username": username, "enabled": 1,
            "added_by": admin_id, "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        PROMOTION_CHANNELS.append({"url": url, "username": username, "enabled": True})
        return True, "Channel added successfully!"
    except DuplicateKeyError:
        return False, "Channel already exists!"
    except Exception as e:
        return False, f"Error: {str(e)}"

def remove_force_join_channel(url_or_username, admin_id):
    global PROMOTION_CHANNELS
    try:
        uname = url_or_username.replace("@", "")
        doc = _col_channels.find_one({"$or": [{"url": url_or_username}, {"username": uname}]})
        if not doc:
            return False, "Channel not found!"
        _col_channels.delete_one({"_id": doc["_id"]})
        PROMOTION_CHANNELS = [ch for ch in PROMOTION_CHANNELS if ch["url"] != doc["url"] and ch["username"] != doc["username"]]
        return True, f"Channel removed: {doc['url']}"
    except Exception as e:
        return False, f"Error: {str(e)}"

def list_force_join_channels():
    try:
        docs = list(_col_channels.find({}).sort("_id", 1))
        return [(str(d["_id"]), d["url"], d["username"], d.get("enabled", 1), d.get("added_by"), d.get("added_date")) for d in docs]
    except Exception as e:
        print(f"Error listing channels: {e}")
        return []

def _channel_chat_ref(channel):
    """
    Return the Telegram chat reference to pass to get_chat_member.
    - Public channel  → "@username"
    - Private channel → numeric id (int)
    """
    uname = channel.get('username', '')
    if channel.get('is_private') or (uname and uname.lstrip('-').isdigit()):
        try:
            return int(uname)
        except ValueError:
            return f"@{uname}"
    return f"@{uname}"

# Tracks users who have sent a join request (for join_by_request channels)
# {user_id: set(channel_username)}
JOIN_REQUESTS_SENT = {}

def check_subscription(user_id):
    if not FORCE_JOIN_ENABLED:
        return True
    if user_id == ADMIN_ID:
        return True
    if not PROMOTION_CHANNELS:
        load_channels_from_db()
    if not PROMOTION_CHANNELS:
        return True
    for channel in PROMOTION_CHANNELS:
        if not channel.get('enabled', True):
            continue

        # ── join_by_request mode ──
        # Bot cannot verify pending requests via Telegram API.
        # Check our local registry: if user is recorded as having sent request → pass
        if channel.get('join_by_request'):
            uname = channel.get('username', '')
            user_sent = JOIN_REQUESTS_SENT.get(user_id, set())
            if uname not in user_sent:
                return False   # hasn't clicked join yet
            continue           # recorded → pass

        # ── normal / private channel ──
        try:
            ref    = _channel_chat_ref(channel)
            member = bot.get_chat_member(ref, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            err = str(e).lower()
            # "user not found" / "participant not found" → definitely not a member
            if 'not found' in err or 'participant' in err or 'user_id_invalid' in err:
                return False
            # bot not admin in private channel → can't verify → block to be safe
            if channel.get('is_private'):
                return False
            return False
    return True

def get_missing_channels(user_id):
    missing = []
    if not FORCE_JOIN_ENABLED:
        return missing
    if user_id == ADMIN_ID:
        return missing
    if not PROMOTION_CHANNELS:
        load_channels_from_db()
    for channel in PROMOTION_CHANNELS:
        if not channel.get('enabled', True):
            continue

        # ── join_by_request mode ──
        if channel.get('join_by_request'):
            uname = channel.get('username', '')
            user_sent = JOIN_REQUESTS_SENT.get(user_id, set())
            if uname not in user_sent:
                missing.append(channel['url'])
            continue

        # ── normal / private channel ──
        try:
            ref    = _channel_chat_ref(channel)
            member = bot.get_chat_member(ref, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                missing.append(channel['url'])
        except Exception as e:
            # any failure = treat as not joined (strict)
            missing.append(channel['url'])
    return missing

def force_join_message(chat_id, user_id):
    missing = get_missing_channels(user_id)
    if not missing:
        return None
    channels_text = "\n".join([f"{i+1}. {ch}" for i, ch in enumerate(missing)])
    msg = f"""
FORCE JOIN REQUIRED

 YOU MUST JOIN ALL CHANNELS TO USE THIS BOT!

CHANNELS TO JOIN:
{channels_text}

 After joining all channels, click the button below to verify!
"""
    markup = InlineKeyboardMarkup(row_width=1)
    for channel in missing:
        markup.add(create_primary_button("Join Channel", url=channel, icon_custom_emoji_id="5219899949281453881"))
    markup.add(create_success_button("I've Joined All", callback_data="check_join", icon_custom_emoji_id="5339247212012528642"))
    try:
        return send_with_premium(chat_id, msg, reply_markup=markup)
    except Exception as e:
        print(f"Force join message error: {e}")
        return None

def _enforce_verify_gate(message):
    """
    Central verify gate — called by enforce_force_join before any command runs.
    Returns True  → user is verified (or admin), command may proceed.
    Returns False → gate shown, command must stop.

    GC  : shows inline "Verify in Private" button (60 s auto-delete).
    PVT : shows contact-share button via send_contact_verification_prompt().
    Admin/owner skip always.
    """
    uid     = message.from_user.id
    chat_id = message.chat.id

    # Admin / owner — always pass
    if is_admin(uid):
        return True

    # Already verified — pass
    if _is_contact_verified(uid):
        return True

    # ── GROUP / SUPERGROUP ───────────────────────────────────────────
    if message.chat.type in ("group", "supergroup"):
        try:
            bot_me = bot.get_me()
            bot_uname = bot_me.username
        except Exception:
            bot_uname = None

        if bot_uname:
            deep_link = f"https://t.me/{bot_uname}?start=verify"
            markup = types.InlineKeyboardMarkup()
            btn = types.InlineKeyboardButton(
                text="📱 ᴠᴇʀɪꜰʏ ɪɴ ᴘʀɪᴠᴀᴛᴇ",
                url=deep_link,
            )
            try:
                btn.style = "success"
            except Exception:
                pass
            markup.add(btn)

            grp_text = (
                "<blockquote>"
                f"{_cv_pe('robot', '🤖')} <b>ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ʀᴇQᴜɪʀᴇᴅ</b>\n\n"
                f"{_cv_pe('shield', '🛡️')} Bot use karne ke liye pehle "
                "<b>private chat mein verify</b> karo.\n\n"
                f"{_cv_pe('phone', '📱')} Neeche button dabao aur apna contact share karo."
                "</blockquote>"
            )
            try:
                sent = send_with_premium(chat_id, grp_text, reply_markup=markup)
                if sent:
                    def _del_grp(cid=chat_id, mid=sent.message_id):
                        import time as _t; _t.sleep(60)
                        try: bot.delete_message(cid, mid)
                        except Exception: pass
                    threading.Thread(target=_del_grp, daemon=True).start()
            except Exception as exc:
                print(f"[VERIFY_GATE_GC] {exc}")
        return False   # block command

    # ── PRIVATE CHAT ─────────────────────────────────────────────────
    # Save what command they tried so we can resume after verify
    try:
        _PENDING_REF[uid] = getattr(message, "text", "/start")
    except Exception:
        pass
    send_contact_verification_prompt(uid)
    return False   # block command


def enforce_force_join(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if user_id == ADMIN_ID:
        return True

    # ── Verify gate: must verify before any command ──────────────────
    if not _enforce_verify_gate(message):
        return False

    if not check_subscription(user_id):
        force_join_message(chat_id, user_id)
        return False
    return True

def schedule_deletion(message_id, chat_id):
    def delete():
        time.sleep(AUTO_DELETE_TIME)
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
    thread = threading.Thread(target=delete)
    thread.daemon = True
    thread.start()

def extract_referral(text):
    if text and 'ref_' in text:
        try:
            return int(text.split('ref_')[1])
        except:
            return None
    return None

def send_result_with_json(chat_id, result_text, json_file, message_id=None, edit=True):
    final_text = result_text
    file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
    JSON_FILES[file_id] = json_file

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(create_primary_button("Download JSON", callback_data=f"dljson_{file_id}"))
    markup.add(create_default_button("Back", callback_data="back_to_result"))

    try:
        if message_id and edit:
            try:
                sent = edit_with_premium(chat_id, message_id, final_text, reply_markup=markup)
                schedule_deletion(sent.message_id, chat_id)
                return sent
            except:
                pass
        sent = send_with_premium(chat_id, final_text, reply_markup=markup)
        schedule_deletion(sent.message_id, chat_id)
        return sent
    except Exception as e:
        print(f"Error sending result: {e}")
        return None

def send_result(chat_id, result_text, wait_message=None, edit=True):
    final_text = replace_with_premium_emojis(result_text)
    if "<tg-emoji" not in final_text:
        premium_emoji_id = get_random_premium_emoji_id()
        final_text = final_text + '<tg-emoji emoji-id="' + premium_emoji_id + '">⭐</tg-emoji>'
    try:
        if wait_message and edit:
            try:
                sent = edit_with_premium(chat_id, wait_message.message_id, final_text)
                if sent:
                    schedule_deletion(sent.message_id, chat_id)
                    return sent
            except Exception as e:
                print(f"Edit failed, sending new message: {e}")
                try:
                    bot.delete_message(chat_id, wait_message.message_id)
                except:
                    pass
        sent = send_with_premium(chat_id, final_text)
        schedule_deletion(sent.message_id, chat_id)
        return sent
    except Exception as e:
        print(f"Error sending result: {e}")
        return None

load_channels_from_db()

def validate_username(u):
    if not u: return False
    u = u.strip().replace('@', '')
    return bool(re.match(r'^[a-zA-Z0-9_]{5,32}$', u))

def validate_phone(p):
    if not p: return False
    p = re.sub(r'[^\d+]', '', p)
    return bool(re.match(r'^\+?[0-9]{10,15}$', p))

def validate_vehicle(v):
    if not v: return False
    v = v.strip().upper().replace(' ', '')
    return bool(re.match(r'^[A-Z]{2}[0-9]{1,2}[A-Z]{1,2}[0-9]{1,4}$', v))

def validate_upi(upi):
    if not upi: return False
    upi = upi.strip().lower()
    return bool(re.match(r'^[a-zA-Z0-9._-]+@[a-zA-Z0-9]+$', upi))

def validate_ifsc(ifsc):
    if not ifsc: return False
    ifsc = ifsc.strip().upper()
    return bool(re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', ifsc))

def validate_aadhar(a):
    if not a: return False
    a = re.sub(r'[^\d]', '', a)
    return bool(re.match(r'^\d{12}$', a))

def validate_pan(pan):
    if not pan: return False
    pan = pan.strip().upper()
    return bool(re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$', pan))

def validate_ip(ip):
    if not ip: return False
    return bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip))

def validate_pincode(pin):
    if not pin: return False
    pin = re.sub(r'[^\d]', '', pin)
    return bool(re.match(r'^\d{6}$', pin))

def validate_channel_url(url):
    if not url: return False
    patterns = [r'^https?://t\.me/[a-zA-Z0-9_]+$', r'^https?://telegram\.me/[a-zA-Z0-9_]+$', r'^@[a-zA-Z0-9_]+$']
    for pattern in patterns:
        if re.match(pattern, url):
            return True
    return False


def is_group_chat(message):
    return message.chat.type in ['group', 'supergroup', 'channel']

def extract_username_from_url(url):
    url = url.strip()
    if url.startswith('@'):
        return url[1:]
    elif 't.me/' in url:
        return url.split('t.me/')[-1]
    elif 'telegram.me/' in url:
        return url.split('telegram.me/')[-1]
    return url

def get_emoji_for_key(key):
    key_lower = key.lower()
    if 'name' in key_lower: return '👤'
    elif 'phone' in key_lower or 'number' in key_lower or 'mobile' in key_lower: return '📞'
    elif 'email' in key_lower: return '📧'
    elif 'address' in key_lower: return '🏠'
    elif 'city' in key_lower or 'state' in key_lower: return '📍'
    elif 'bank' in key_lower: return '🏦'
    elif 'ifsc' in key_lower: return '🔢'
    elif 'vehicle' in key_lower or 'car' in key_lower: return '🚗'
    elif 'owner' in key_lower: return '👨'
    elif 'aadhar' in key_lower: return '🆔'
    elif 'pan' in key_lower: return '📄'
    elif 'upi' in key_lower: return '💳'
    elif 'instagram' in key_lower or 'insta' in key_lower: return '📸'
    elif 'followers' in key_lower: return '👑'
    elif 'ip' in key_lower: return '🌐'
    elif 'pincode' in key_lower: return '📮'
    elif 'git' in key_lower: return '🐙'
    elif 'bgmi' in key_lower: return '🎮'
    elif 'ff' in key_lower: return '🔥'
    elif 'prompt' in key_lower or 'image' in key_lower: return '🎨'
    elif 'generated' in key_lower: return '🖼️'
    elif 'father' in key_lower: return '👨'
    elif 'circle' in key_lower: return '🌍'
    elif 'alt' in key_lower or 'alternate' in key_lower: return '📱'
    elif 'id' in key_lower: return '🆔'
    elif 'truecaller' in key_lower: return '👤'
    elif 'country' in key_lower: return '🌍'
    elif 'code' in key_lower: return '🔢'
    elif 'total' in key_lower: return '📊'
    elif 'result' in key_lower: return '📋'
    else: return '•'

print("Validation functions loaded")

def get_api_endpoint(command_name):
    try:
        doc = _col_api_config.find_one({"command_name": command_name})
        return doc["api_endpoint"] if doc else None
    except:
        return None

def set_api_endpoint(command_name, endpoint, admin_id):
    try:
        _col_api_config.update_one(
            {"command_name": command_name},
            {"$set": {"api_endpoint": endpoint, "updated_by": admin_id,
                      "updated_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True
        )
        return True, f"API endpoint updated for /{command_name}"
    except Exception as e:
        return False, f"Error: {str(e)}"

def list_all_apis():
    apis = []
    for cmd, endpoint in API_ENDPOINTS.items():
        apis.append({"command": cmd, "endpoint": endpoint, "is_custom": False})
    try:
        for doc in _col_api_config.find({}):
            apis.append({"command": doc["command_name"], "endpoint": doc["api_endpoint"], "is_custom": True})
    except:
        pass
    return apis

def clean_text(text):
    if isinstance(text, str):
        text = text.replace("@DEVELOPERSTAKEBOT", "").strip()
        text = text.replace("@ and @TATANIUM_ANSH", "@ftgamer2")
        text = text.replace("@InvalidAyush", "").strip()
        text = text.replace("InvalidAyush", "ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ").strip()
        text = text.replace("developer", "⏤͟͟͞͞𝗦ᴀ𝗛ɪ𝗟").strip()
        text = text.replace("Developer", "⏤͟͟͞͞𝗦ᴀ𝗛ɪ𝗟").strip()
        text = text.replace("@InvalidAyush ❤️🔥⚡", "⏤͟͟͞͞𝗦ᴀ𝗛ɪ𝗟 ❤️🔥⚡").strip()
        text = text.replace("❤️🔥⚡", "").strip()
        words = text.split()
        cleaned_words = []
        for word in words:
            if word.startswith('@') and word not in ['@ftgamer2', '@BMW_AURA0', '@flirts_x']:
                continue
            cleaned_words.append(word)
        return ' '.join(cleaned_words)
    return text

def clean_dict(data):
    if isinstance(data, dict):
        cleaned = {}
        for key, value in data.items():
            if key.lower() in ['channel', 'developer', 'by', 'tag', 'key_expiry', 'response_time_ms', 'cached', 'cached_at', 'owner', 'credit', 'message']:
                continue
            if isinstance(value, str):
                cleaned[key] = clean_text(value)
            elif isinstance(value, dict):
                cleaned[key] = clean_dict(value)
            elif isinstance(value, list):
                cleaned[key] = [clean_dict(item) if isinstance(item, dict) else (clean_text(item) if isinstance(item, str) else item) for item in value]
            else:
                cleaned[key] = value
        cleaned['developer'] = 'ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ'
        cleaned['contact'] = 'http://t.me/flirts_x'
        return cleaned
    elif isinstance(data, list):
        return [clean_dict(item) if isinstance(item, dict) else (clean_text(item) if isinstance(item, str) else item) for item in data]
    elif isinstance(data, str):
        return clean_text(data)
    return data

def is_empty_response(data):
    if not data:
        return True
    if isinstance(data, dict):
        if data.get('status') == 'error' or data.get('error'):
            return True
        all_empty = True
        for key, value in data.items():
            if key in ['status', 'by', 'channel']:
                continue
            if value and str(value).strip() and str(value).lower() not in ['none', 'null', '']:
                all_empty = False
                break
        return all_empty
    return False

def make_api_request(url, timeout=10):
    """
    timeout = read timeout in seconds.
    Connect timeout is always 5s — if server is unreachable, fail fast
    rather than blocking the entire thread for 20-60 seconds.
    Adapter retries are disabled; fallbacks are handled per-caller.
    """
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cached = API_CACHE.get(cache_key)
    if cached:
        return cached
    try:
        # (connect_timeout, read_timeout) — short connect, generous read
        response = session.get(url, timeout=(5, timeout), verify=False)
        if response.status_code == 200:
            try:
                data = response.json()
                if isinstance(data, dict) and data.get('status') != 'error':
                    API_CACHE.set(cache_key, data)
                return data
            except Exception:
                return {"status": "error", "message": "Invalid JSON response"}
        return {"status": "error", "message": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def call_phone_api(phone):
    """
    API: PASTE_YOUR_NUM_INFO_API_HERE
    Response: { "success": true, "number": "...", "total": N,
                "results": [{ "mobile", "name", "address", "circle",
                               "alt", "fname", "id", "email",
                               "truecaller_name" }, ...] }
    """
    phone = re.sub(r'[^\d+]', '', phone).lstrip('+')
    try:
        url = f"PASTE_API_HERE/api/num?number={phone}"   # ← API OF NUM INFO
        data = make_api_request(url, timeout=20)

        if not isinstance(data, dict):
            return {"status": "error", "message": "NO DATA FOUND"}
        if not data.get("success"):
            return {"status": "error", "message": data.get("message", "NO DATA FOUND")}

        # New API uses "results", old used "records"
        raw = data.get("results", data.get("records", []))
        if not raw:
            return {"status": "error", "message": "NO DATA FOUND"}

        records = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            # field name normalisation: new API uses alt/fname, old used alt_number/father
            alt_val = (r.get("alt") or r.get("alt_number") or
                       r.get("alternate") or r.get("altNumber") or "N/A")
            fname   = (r.get("fname") or r.get("father") or
                       r.get("father_name") or r.get("fatherName") or "N/A")
            tc_name = r.get("truecaller_name", "N/A")
            name    = r.get("name", "N/A")
            # ignore placeholder truecaller labels
            if tc_name and tc_name.lower() in ("test label", "n/a", "none", ""):
                tc_name = "N/A"
            records.append({
                "record_no":       len(records) + 1,
                "mobile":          str(r.get("mobile", "N/A")).strip(),
                "name":            name,
                "truecaller_name": tc_name,
                "father_name":     fname,
                "address":         str(r.get("address", "N/A")).replace("!", ", "),
                "circle":          r.get("circle", "N/A"),
                "alternate":       alt_val,
                "id":              str(r.get("id", "N/A")),
                "email":           (r.get("email") if r.get("email") and
                                    r.get("email") not in ("N/A","default@unspecify.com") else "N/A"),
            })

        return clean_dict({
            "status":  "success",
            "number":  data.get("number", phone),
            "total":   data.get("total", data.get("total_records", len(records))),
            "records": records,
        })

    except Exception as e:
        return {"status": "error", "message": str(e)}

def call_tg_api(query):
    """
    Primary API: PASTE_YOUR_TG_INFO_API_HERE
    Response shape:
      { "success": true,
        "response": { "data": [ { "user_id": "...", "username": "@...",
                                   "name": "...", "number": "...",
                                   "country": "...", "country_code": "..." } ] } }
    Falls back to old APIs on failure.
    """
    query = str(query).strip()
    query = query.replace("@", "").replace("t.me/", "").replace("telegram.me/", "")
    is_user_id = query.isdigit()

    def _parse_new_api(data, original_query):
        """Parse the new 100.31 /api/tg response."""
        if not isinstance(data, dict) or not data.get("success"):
            return None
        inner = data.get("response", {})
        records = inner.get("data", [])
        if not records:
            return None
        r = records[0]
        tgid     = str(r.get("user_id",  "")).strip() or "N/A"
        username = str(r.get("username", "")).strip().lstrip("@") or "N/A"
        phone    = str(r.get("number",   "")).strip() or None
        name     = str(r.get("name",     "")).strip() or "N/A"
        country  = str(r.get("country",  "")).strip() or "N/A"
        cc       = str(r.get("country_code", "")).strip() or "N/A"
        if not phone:
            return None
        return {
            "status":       "success",
            "query":        original_query,
            "found":        True,
            "tgid":         tgid,
            "username":     username,
            "name":         name,
            "phone":        phone,
            "country":      country,
            "country_code": cc,
            "msg":          "Details fetched",
            "provided_by":  "ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ",
            "telegram":     "@flirts_x",
        }

    def _parse_old_api(data, original_query):
        if not isinstance(data, dict) or data.get("success") != True:
            return None
        msg = data.get("msg", "")
        if "not found" in msg.lower() or "no data" in msg.lower():
            return None
        phone = data.get("number") or None
        if not phone:
            return None
        tgid = None
        for field in ["tg_id", "user_id", "id", "userid", "userId", "UID"]:
            if field in data and data[field] is not None:
                tgid = data[field]; break
        username = None
        for field in ["username", "user_name", "uname", "tg_username"]:
            if field in data and data[field] is not None:
                username = data[field]; break
        return {
            "status":       "success",
            "query":        original_query,
            "found":        True,
            "tgid":         str(tgid) if tgid else "N/A",
            "username":     str(username) if username else "N/A",
            "name":         "N/A",
            "phone":        str(phone),
            "country":      str(data.get("country") or "N/A"),
            "country_code": str(data.get("country_code") or "N/A"),
            "msg":          msg or "Details fetched",
            "provided_by":  "ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ",
            "telegram":     "@flirts_x",
        }

    # ── Attempt 1: felix (tg_id numeric only) ─────────────────────
    if is_user_id:
        try:
            url_felix = f"PASTE_API_HERE/tg={query}"   # ← API OF TG INFO (FALLBACK 1)
            data_felix = make_api_request(url_felix, timeout=15)
            if isinstance(data_felix, dict):
                phone_f = str(data_felix.get("number", "")).strip()
                tgid_f  = str(data_felix.get("tg_id", query)).strip()
                msg_f   = str(data_felix.get("msg", "")).strip()
                if phone_f and "not found" not in msg_f.lower() and "no data" not in msg_f.lower():
                    cc_f = str(data_felix.get("country_code", "N/A")).strip()
                    # country_code may be "+43" — strip the + for display
                    phone_full = f"{cc_f}{phone_f}" if not phone_f.startswith("+") else phone_f
                    return {
                        "status":       "success",
                        "query":        query,
                        "found":        True,
                        "tgid":         tgid_f,
                        "username":     "N/A",
                        "name":         "N/A",
                        "phone":        phone_full,
                        "number":       phone_f,
                        "country":      str(data_felix.get("country", "N/A")).strip(),
                        "country_code": cc_f,
                        "msg":          msg_f or "Details fetched",
                        "provided_by":  "ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ",
                        "telegram":     "@flirts_x",
                    }
        except Exception as e_f:
            print(f"[TGID] felix error: {e_f}")

    # ── Attempt 2: new local API ────────────────────────────────────
    try:
        param = query if is_user_id else query
        url1 = f"PASTE_API_HERE/api/tg?username={param}"   # ← API OF TG INFO (PRIMARY)
        data1 = make_api_request(url1, timeout=15)
        result = _parse_new_api(data1, query)
        if result:
            return result
    except Exception as e1:
        print(f"[TGID] primary API error: {e1}")

    # ── Attempt 3: fallback API ────────────────────────────────────────────
    try:
        prefix = "" if is_user_id else "@"
        url2 = f"PASTE_API_HERE?query={prefix}{query}"   # ← API OF TG INFO (FALLBACK 2)
        data2 = make_api_request(url2, timeout=15)
        result = _parse_old_api(data2, query)
        if result:
            return result
    except Exception as e2:
        print(f"[TGID] Attempt 2 error: {e2}")

    # ── Attempt 5: fallback API ──────────────────────────────────────
    try:
        prefix = "" if is_user_id else "@"
        url3 = f"PASTE_API_HERE/info={prefix}{query}"   # ← API OF TG INFO (FALLBACK 3)
        data3 = make_api_request(url3, timeout=15)
        result = _parse_old_api(data3, query)
        if result:
            return result
    except Exception as e3:
        print(f"[TGID] Attempt 3 error: {e3}")

    # ── Attempt 6: fallback API ────────────────────────────────────────
    try:
        prefix = "" if is_user_id else "@"
        url4 = f"PASTE_API_HERE/term={prefix}{query}"   # ← API OF TG INFO (FALLBACK 4)
        data4 = make_api_request(url4, timeout=15)
        if isinstance(data4, dict) and data4.get("success") == True:
            msg = data4.get("msg", "")
            if "not found" not in msg.lower() and "no data" not in msg.lower():
                phone = data4.get("number") or data4.get("n") or None
                tgid  = data4.get("tg_id") or data4.get("id") or None
                if phone:
                    return {
                        "status":       "success",
                        "query":        query,
                        "found":        True,
                        "tgid":         str(tgid) if tgid else "N/A",
                        "username":     "N/A",
                        "name":         "N/A",
                        "phone":        str(phone),
                        "country":      str(data4.get("country") or data4.get("c") or "N/A"),
                        "country_code": str(data4.get("country_code") or data4.get("cc") or "N/A"),
                        "msg":          msg or "Details fetched",
                        "provided_by":  "ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ",
                        "telegram":     "@flirts_x",
                    }
    except Exception as e4:
        print(f"[TGID] Attempt 4 error: {e4}")

    # ── Attempt 7: resolve UID → username → retry new API ──────────
    if is_user_id:
        try:
            chat = bot.get_chat(int(query))
            if chat and chat.username:
                uname = chat.username
                url5 = f"PASTE_API_HERE/api/tg?username={uname}"   # ← API OF TG INFO (UID RESOLVE)
                data5 = make_api_request(url5, timeout=15)
                result = _parse_new_api(data5, query)
                if result:
                    result["note"] = f"Resolved UID → @{uname}"
                    return result
        except Exception as e5:
            print(f"[TGID] Attempt 5 error: {e5}")

    return {"status": "error", "message": "NO DATA FOUND"}
def call_aadhar_api(aadhar_number):
    """
    Primary:  PASTE_YOUR_AADHAR_INFO_API_HERE
    Response: { "success": true, "aadhar": "...", "total": N,
                "results": [{mobile, name, address, circle, aadhar}, ...] }
    Fallback: old 100.31 and felix endpoints.
    """
    aadhar = re.sub(r'[^\d]', '', aadhar_number)
    new_ip = "PASTE_API_SERVER_URL"   # ← APNI API SERVER URL

    def _normalise(raw, aadhar_val):
        # new API uses "results", old APIs used "records"
        raw_list = raw.get("results", raw.get("records", []))
        if not isinstance(raw_list, list) or not raw_list:
            return None
        records = []
        for r in raw_list:
            fname = (r.get("father") or r.get("father_name") or
                     r.get("fname") or r.get("fatherName") or "N/A")
            records.append({
                "record_no":   len(records) + 1,
                "mobile":      str(r.get("mobile", "N/A")).strip(),
                "name":        r.get("name", "N/A"),
                "father_name": fname,
                "address":     str(r.get("address", "N/A")).replace("!", ", "),
                "circle":      r.get("circle", "N/A"),
                "aadhar":      r.get("aadhar", aadhar_val),
                "email":       r.get("email", "N/A") if r.get("email") else "N/A",
            })
        return clean_dict({
            "status":  "success",
            "aadhar":  aadhar_val,
            "total":   raw.get("total", raw.get("total_records", len(records))),
            "records": records,
        })

    # ── Attempt 1: primary API ───────────────────────────────
    try:
        url1 = f"{new_ip}/api/adhar?adhar={aadhar}"   # ← API OF AADHAR INFO (PRIMARY)
        data1 = make_api_request(url1, timeout=20)
        if isinstance(data1, dict) and data1.get("success"):
            result = _normalise(data1, aadhar)
            if result:
                return result
    except Exception as e1:
        print(f"[ADHR] new API error: {e1}")

    # ── Attempt 2: old 100.31 dead — removed ───────────────────

    # ── Attempt 3: felix fallback ───────────────────────────────
    try:
        url3 = f"PASTE_API_HERE?adhar={aadhar}"   # ← API OF AADHAR INFO (FALLBACK)
        data3 = make_api_request(url3, timeout=15)
        if isinstance(data3, dict) and (data3.get("status") is True or data3.get("success") is True):
            inner3 = data3.get("data", data3)
            if isinstance(inner3, dict):
                result = _normalise(inner3, aadhar)
                if result:
                    return result
    except Exception as e3:
        print(f"[ADHR] felix error: {e3}")

    return {"status": "error", "message": "NO DATA FOUND"}
def build_api_url(endpoint_template, query):
    """Replace only {placeholder} tokens — never bare substrings inside URLs."""
    import re as _re
    url = endpoint_template
    url = url.replace("{key}", API_KEY)
    url = url.replace("{query}", str(query))
    # replace any remaining {word} placeholder with the query value
    url = _re.sub(r'\{[^}]+\}', str(query), url)
    return url

def call_name_api(name):
    try:
        custom_endpoint = get_api_endpoint('name')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, name)
        else:
            url = f"{API_BASE_URL}/api/name?key={API_KEY}&name={name}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_pan_api(pan):
    pan = pan.strip().upper()
    try:
        custom_endpoint = get_api_endpoint('pan')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, pan)
        else:
            url = f"{API_BASE_URL}/api/pan?key={API_KEY}&pan={pan}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_vehicle_api(vehicle):
    """
    Primary: PASTE_YOUR_VECH_INFO_API_HERE
    Response (flat): { success, vehicle, chassis_number, engine_number,
                       mobile_number, owner_name, response_time_ms }
    Fallback: old 100.31 endpoint, then custom/base endpoint.
    """
    vehicle = vehicle.strip().upper().replace(' ', '')
    new_ip = "PASTE_API_SERVER_URL"   # ← APNI API SERVER URL

    def _normalise_veh(raw):
        """Normalise flat new-API response to a consistent dict."""
        if not isinstance(raw, dict) or not raw.get("success"):
            return None
        d = {
            "status":         "success",
            "vehicle":        str(raw.get("vehicle", "N/A")),
            "owner_name":     str(raw.get("owner_name", raw.get("owner", "N/A"))),
            "mobile_number":  str(raw.get("mobile_number", raw.get("mobile", "N/A"))),
            "chassis_number": str(raw.get("chassis_number", "N/A")),
            "engine_number":  str(raw.get("engine_number", "N/A")),
        }
        return d

    # ── Attempt 1: primary API ───────────────────────────────
    try:
        url1 = f"{new_ip}/api/veh?vehicle={vehicle}"   # ← API OF VECH INFO (PRIMARY)
        data1 = make_api_request(url1, timeout=20)
        r = _normalise_veh(data1)
        if r:
            return clean_dict(r)
    except Exception as e1:
        print(f"[VECH] new API error: {e1}")

    # ── Attempt 2: old 100.31 dead — removed ───────────────────

    # ── Attempt 3: custom / base endpoint ───────────────────────
    try:
        custom_endpoint = get_api_endpoint('vech')
        url3 = build_api_url(custom_endpoint, vehicle) if custom_endpoint else f"{API_BASE_URL}/api/vehicle?key={API_KEY}&vehicle={vehicle}"
        data3 = make_api_request(url3, timeout=15)
        if isinstance(data3, dict):
            data3.pop('channel', None)
            if not is_empty_response(data3):
                return clean_dict(data3)
    except Exception as e3:
        print(f"[VECH] base API error: {e3}")

    return {"status": "error", "message": "NO DATA FOUND"}

def call_rc_api(vehicle):
    vehicle = vehicle.strip().upper().replace(' ', '')
    try:
        # PASTE YOUR NEW API HERE
        url = f"PASTE_API_HERE/api/rc?vehicle={vehicle}"   # ← API OF VECHRC INFO (PRIMARY)
        data = make_api_request(url, timeout=20)
        if isinstance(data, dict) and data.get('status') != 'error' and not is_empty_response(data):
            return clean_dict(data)
    except Exception as e:
        print(f"New RC API failed: {e}")

    try:
        new_url = f"PASTE_API_HERE/number={vehicle}"   # ← API OF VECHRC INFO (FALLBACK)
        data = make_api_request(new_url, timeout=20)
        if isinstance(data, dict) and data.get('status') != 'error' and not is_empty_response(data):
            return clean_dict(data)
    except Exception as e:
        print(f"Fallback RC API failed: {e}")

    try:
        custom_endpoint = get_api_endpoint('vechrc')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, vehicle)
        else:
            url = f"{API_BASE_URL}/api/rc?key={API_KEY}&owner={vehicle}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def save_result_to_json(command, query, result):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"result_{command}_{query}_{timestamp}.json"
        filepath = os.path.join("/tmp", filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        return filepath
    except Exception as e:
        print(f"Error saving JSON: {e}")
        return None

def send_multi_result(chat_id, message_parts, wait_message=None, json_file=None):
    if not message_parts:
        return None
    if wait_message:
        try:
            bot.delete_message(chat_id, wait_message.message_id)
        except:
            pass
    sent_messages = []
    for idx, part in enumerate(message_parts):
        markup = None
        if idx == len(message_parts) - 1 and json_file:
            file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
            JSON_FILES[file_id] = json_file
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(create_primary_button("📥 Download JSON", callback_data=f"dljson_{file_id}"))
            markup.add(create_default_button("Back", callback_data="back_to_result"))
        try:
            msg = send_with_premium(chat_id, part, reply_markup=markup)
            if msg:
                sent_messages.append(msg)
                schedule_deletion(msg.message_id, chat_id)
        except Exception as e:
            print(f"Error sending part {idx+1}: {e}")
    return sent_messages[-1] if sent_messages else None

def call_num2_api(phone):
    phone = re.sub(r'[^\d+]', '', phone).lstrip('+')
    try:
        url = f"PASTE_API_HERE/api/num2?number={phone}"   # ← API OF NUM2 INFO
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if isinstance(data, dict) and data.get('results'):
                cleaned_data = {
                    "status": "success",
                    "number": data.get('number', phone),
                    "total": data.get('total', 0),
                    "records": []
                }
                for idx, result in enumerate(data.get('results', []), 1):
                    alt_val = (result.get('alt')
                               or result.get('alternate')
                               or result.get('alt_number')
                               or result.get('altNumber')
                               or result.get('alt_num')
                               or 'N/A')
                    fname_val = (result.get('fname')
                                 or result.get('father_name')
                                 or result.get('father')
                                 or result.get('fatherName')
                                 or result.get('Father Name')
                                 or 'N/A')
                    # aadhar — try multiple possible field names from API
                    aadhar_val = (result.get('aadhar')
                                  or result.get('adhar')
                                  or result.get('aadhaar')
                                  or result.get('aadhar_number')
                                  or result.get('adharCard')
                                  or 'N/A')
                    record = {
                        "record_no": idx,
                        "mobile": result.get('mobile', 'N/A'),
                        "name": result.get('name', 'N/A'),
                        "address": str(result.get('address', 'N/A')).replace('!', ', '),
                        "circle": result.get('circle', 'N/A'),
                        "alternate": alt_val,
                        "father_name": fname_val,
                        "id": result.get('id', 'N/A'),
                        "email": result.get('email', 'Not Available'),
                        "truecaller_name": result.get('truecaller_name', 'N/A'),
                        "aadhar": str(aadhar_val) if aadhar_val and aadhar_val != 'N/A' else 'N/A',
                    }
                    cleaned_data['records'].append(record)
                return clean_dict(cleaned_data)
            elif data.get('success') == True and data.get('total', 0) == 0:
                return {"status": "error", "message": "NO DATA FOUND"}
            return {"status": "error", "message": "NO DATA FOUND"}
        return {"status": "error", "message": "NO DATA FOUND"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def call_family_api(aadhar):
    """
    Primary: PASTE_YOUR_FAMILY_INFO_API_HERE
    Response: { success, aadhaar, aadhaar_masked, ration_card, scheme,
                state, district, address, rc, fps_id, total_members,
                members: [{name, relation, member_id, uid}, ...] }
    Falls back to old IP on failure.
    """
    aadhar = re.sub(r'[^\d]', '', str(aadhar))

    # ── Attempt 1: primary API ────────────────────────
    try:
        url1 = f"PASTE_API_HERE?id={aadhar}"   # ← API OF FAMILY INFO (PRIMARY)
        data1 = make_api_request(url1, timeout=15)
        if isinstance(data1, dict) and data1.get("success"):
            data1.pop("by", None); data1.pop("channel", None)
            data1.pop("response_time_ms", None)
            data1["status"] = "success"
            return clean_dict(data1)
    except Exception as e1:
        print(f"[FAMILY] new API error: {e1}")

    # ── Attempt 2: fallback API ──────────────────────────
    try:
        url2 = f"PASTE_API_HERE?aadhaar={aadhar}"   # ← API OF FAMILY INFO (FALLBACK)
        data2 = make_api_request(url2, timeout=15)
        if isinstance(data2, dict):
            data2.pop("channel", None)
            if not is_empty_response(data2):
                return clean_dict(data2)
    except Exception as e2:
        print(f"[FAMILY] old API error: {e2}")

    return {"status": "error", "message": "NO DATA FOUND"}

def call_ip_api(ip):
    try:
        custom_endpoint = get_api_endpoint('ip')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, ip)
        else:
            url = f"{API_BASE_URL}/api/ip?key={API_KEY}&ip={ip}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_pincode_api(pin):
    try:
        custom_endpoint = get_api_endpoint('pin')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, pin)
        else:
            url = f"{API_BASE_URL}/api/pincode?key={API_KEY}&pin={pin}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_git_api(username):
    try:
        custom_endpoint = get_api_endpoint('git')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, username)
        else:
            url = f"{API_BASE_URL}/api/git?key={API_KEY}&username={username}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_bgmi_api(uid):
    try:
        custom_endpoint = get_api_endpoint('bgmi')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, uid)
        else:
            url = f"{API_BASE_URL}/api/bgmi?key={API_KEY}&uid={uid}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_ff_api(uid):
    try:
        custom_endpoint = get_api_endpoint('ff')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, uid)
        else:
            url = f"{API_BASE_URL}/api/ff?key={API_KEY}&uid={uid}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_upi_api(upi_id):
    """
    Primary: PASTE_YOUR_UPI_INFO_API_HERE
    Response: { "success": true, "upi_id", "name", "username", "handle",
                "valid", "account_type", "bank", "bank_id", "app",
                "handle_bank" }
    """
    upi_id = upi_id.strip()
    # ── Attempt 1: primary API ─────────────────────────────────
    try:
        url1 = f"PASTE_API_HERE?upi={upi_id}"   # ← API OF UPI INFO
        data1 = make_api_request(url1, timeout=15)
        if isinstance(data1, dict) and data1.get("success"):
            data1.pop("by", None); data1.pop("channel", None)
            data1["status"] = "success"
            return clean_dict(data1)
    except Exception as e1:
        print(f"[UPI] ftosint error: {e1}")

    # ── Attempt 2: old custom/base endpoint ─────────────────────
    try:
        custom_endpoint = get_api_endpoint('upi')
        url2 = build_api_url(custom_endpoint, upi_id) if custom_endpoint else f"{API_BASE_URL}/api/upi?key={API_KEY}&upi={upi_id}"
        data2 = make_api_request(url2, timeout=15)
        if isinstance(data2, dict):
            data2.pop('channel', None)
            if not is_empty_response(data2):
                data2["status"] = "success"
                return clean_dict(data2)
    except Exception as e2:
        print(f"[UPI] base error: {e2}")

    return {"status": "error", "message": "NO DATA FOUND"}

def call_ifsc_api(ifsc):
    try:
        custom_endpoint = get_api_endpoint('ifsc')
        if custom_endpoint:
            url = build_api_url(custom_endpoint, ifsc)
        else:
            url = f"{API_BASE_URL}/api/ifsc?key={API_KEY}&ifsc={ifsc}"
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if 'channel' in data: del data['channel']
            if is_empty_response(data):
                return {"status": "error", "message": "NO DATA FOUND"}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except:
        return {"status": "error", "message": "NO DATA FOUND"}

def call_insta_api(username):
    try:
        url = f"PASTE_API_HERE/api/insta?username={username}"   # ← API OF INSTA INFO
        data = make_api_request(url, timeout=20)
        if isinstance(data, dict):
            if data.get('success') == False or data.get('status') == 'error':
                return {"status": "error", "message": data.get('message', 'API Error')}
            return clean_dict(data)
        return {"status": "error", "message": "NO DATA FOUND"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def call_leak_api(phone):
    """
    New API: PASTE_YOUR_LEAK_INFO_API_HERE
    Response: { "records": [{Phone, Phone2-10, Adres, Adres2-8, FullName,
                              FatherName, DocumentNumber, Region}, ...] }
    Falls back to old 100.31 endpoint on failure.
    """
    phone = re.sub(r'[^\d+]', '', phone).lstrip('+')
    new_ip = "PASTE_API_SERVER_URL"   # ← APNI API SERVER URL

    def _parse_chain_records(chain_block, source_label):
        """Parse one chain/icmr block into normalised record dicts."""
        raw_recs = chain_block.get("records", [])
        title    = chain_block.get("title", source_label)
        out = []
        for r in raw_recs:
            if not isinstance(r, dict):
                continue
            # phones — new API goes up to Phone15
            phones = []
            for k in (["Phone"] + [f"Phone{i}" for i in range(2, 16)]):
                v = str(r.get(k, "")).strip()
                if v and v.upper() not in ("NA", "N/A", "") and len(v) >= 7:
                    phones.append(v)
            # addresses — up to Adres8
            addrs = []
            for k in (["Adres"] + [f"Adres{i}" for i in range(2, 9)]):
                v = str(r.get(k, "")).strip()
                if v:
                    addrs.append(v.replace("!", ", "))
            # ICMR records use lowercase keys
            name    = r.get("FullName") or r.get("name", "N/A")
            father  = r.get("FatherName") or r.get("father", "N/A")
            doc_no  = str(r.get("DocumentNumber", r.get("aadhar", "N/A"))).strip()
            region  = str(r.get("Region", r.get("state", "N/A"))).strip()
            age_val = str(r.get("age", "N/A")).strip()
            gender  = str(r.get("gender", "N/A")).strip()
            district= str(r.get("district", "N/A")).strip()

            rec = {
                "source":          title,
                "phones":          ", ".join(phones) if phones else "N/A",
                "name":            name,
                "father_name":     father,
                "address":         addrs[0] if addrs else r.get("address", "N/A"),
                "all_addresses":   " | ".join(addrs[1:]) if len(addrs) > 1 else "",
                "document_number": doc_no,
                "region":          region,
            }
            if age_val    not in ("N/A", "None", ""): rec["age"]      = age_val
            if gender     not in ("N/A", "None", ""): rec["gender"]   = gender
            if district   not in ("N/A", "None", ""): rec["district"] = district
            out.append(rec)
        return out

    def _normalise_leak(raw, query_num):
        """
        New API: response has top-level "chain" and "icmr" blocks.
        Old API: top-level "records" list.
        Merge all into one flat list.
        """
        all_records = []

        # ── New API: chain block ────────────────────────────────
        chain = raw.get("chain")
        if isinstance(chain, dict):
            all_records.extend(_parse_chain_records(chain, "HiTeckGroop"))

        # ── New API: icmr block ─────────────────────────────────
        icmr = raw.get("icmr")
        if isinstance(icmr, dict):
            all_records.extend(_parse_chain_records(icmr, "ICMR 2025"))

        # ── Old flat API: top-level records/results ─────────────
        if not all_records:
            flat = raw.get("records", raw.get("results", raw.get("data", [])))
            if isinstance(flat, list):
                all_records.extend(_parse_chain_records({"records": flat}, "LeakDB"))

        if not all_records:
            return None

        return {
            "status":  "success",
            "number":  query_num,
            "total":   len(all_records),
            "records": all_records,
        }

    # ── Attempt 1: primary API ───────────────────────────────
    try:
        url1 = f"{new_ip}/api/leak?number={phone}"   # ← API OF LEAK INFO
        data1 = make_api_request(url1, timeout=20)
        if isinstance(data1, dict) and data1.get("success") is not False:
            result = _normalise_leak(data1, phone)
            if result:
                return clean_dict(result)
    except Exception as e1:
        print(f"[LEAK] new API error: {e1}")

    # ── Attempt 2: old 100.31 dead — removed ───────────────────

    return {"status": "error", "message": "NO DATA FOUND"}

def format_leak_result(result, query, credits_left):
    """Format /leak response — all data in blockquote, paginated if > 1024 chars."""
    import time as _time
    e = NUM_EMOJIS
    divider = build_num_divider()
    start_ts = _time.time()

    # ── error ──
    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['phone_title'], '📞')}  <b>𝗟𝗘𝗔𝗞 𝗗𝗕 𝗟𝗢𝗢𝗞𝗨𝗣</b>  {te(e['phone_title'], '📞')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>  : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return [(txt, None)]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    # ── flatten whatever array the API returns ──
    records = []
    if isinstance(result.get("results"), list):
        records = result["results"]
    elif isinstance(result.get("data"), list):
        records = result["data"]
    elif isinstance(result.get("records"), list):
        records = result["records"]
    elif isinstance(result, list):
        records = result

    # if still empty, try to render the raw dict as a single record
    if not records:
        if isinstance(result, dict) and result.get("status") != "error":
            records = [result]
        else:
            return _err("NO DATA FOUND")

    total = len(records)

    RECORDS_PER_PAGE = 3
    pages_data = [records[i:i+RECORDS_PER_PAGE] for i in range(0, total, RECORDS_PER_PAGE)]
    total_pages = len(pages_data)

    SKIP_KEYS = {'status', 'success', 'channel', 'message', 'by', 'credit', 'latency',
                 'response_metadata', 'developer', 'contact', 'key_expiry', 'owner'}

    def _fv(val):
        """Format a field value — return cleaned string or dash."""
        if val is None:
            return "—"
        s = str(val).strip()
        return s if s and s.lower() not in ("none", "null", "", "n/a", "na", "-") else "—"

    def _render_record(rec, idx, abs_total):
        """Render a single record dict into HTML lines inside a <blockquote>."""
        lines = [
            f"━━━━━━━━━━━━━━━━━━",
            f"{te(e['record_badge'], '📰')} <b>Rᴇᴄᴏʀᴅ {idx}/{abs_total}</b>",
            f"━━━━━━━━━━━━━━━━━━",
            f"<blockquote>",
        ]
        if not isinstance(rec, dict):
            lines.append(str(rec))
            lines.append("</blockquote>")
            return "\n".join(lines)

        # priority fields first
        PRIORITY = [
            # ── Normalised fields from primary API ──────────────
            ("phones",          e["number"],       "📲", "Pʜᴏɴᴇꜱ"),
            ("name",            e["name"],         "👤", "Nᴀᴍᴇ"),
            ("father_name",     e["father"],       "👥", "Fᴀᴛʜᴇʀ"),
            ("address",         e["address"],      "🏡", "Aᴅᴅʀᴇꜱꜱ"),
            ("all_addresses",   e["address"],      "🏡", "Aʟʟ Aᴅᴅʀᴇꜱꜱᴇꜱ"),
            ("document_number", e["id_card"],      "💳", "Dᴏᴄᴜᴍᴇɴᴛ No"),
            ("region",          e["circle"],       "🔎", "Rᴇɢɪᴏɴ"),
            # ── Legacy / old API fields ────────────────────────────
            ("mobile",          e["number"],       "📲", "Mᴏʙɪʟᴇ"),
            ("number",          e["number"],       "📲", "Nᴜᴍʙᴇʀ"),
            ("phone",           e["number"],       "📲", "Pʜᴏɴᴇ"),
            ("fname",           e["name"],         "👤", "Fɪʀꜱᴛ Nᴀᴍᴇ"),
            ("first_name",      e["name"],         "👤", "Fɪʀꜱᴛ Nᴀᴍᴇ"),
            ("last_name",       e["name"],         "👤", "Lᴀꜱᴛ Nᴀᴍᴇ"),
            ("father",          e["father"],       "👥", "Fᴀᴛʜᴇʀ"),
            ("email",           e["email"],        "📧", "Eᴍᴀɪʟ"),
            ("city",            e["circle"],       "🔎", "Cɪᴛʏ"),
            ("state",           e["circle"],       "🔎", "Sᴛᴀᴛᴇ"),
            ("pincode",         e["circle"],       "🔎", "Pɪɴᴄᴏᴅᴇ"),
            ("dob",             e["id_card"],      "💳", "DOB"),
            ("id",              e["id_card"],      "💳", "ID"),
            ("aadhar",          e["id_card"],      "💳", "Aᴀᴅʜᴀʀ"),
            ("pan",             e["id_card"],      "💳", "PAN"),
            ("alternate",       e["alt_num"],      "📞", "Aʟᴛ Nᴜᴍ"),
            ("alt",             e["alt_num"],      "📞", "Aʟᴛ Nᴜᴍ"),
            ("circle",          e["circle"],       "🔎", "Cɪʀᴄʟᴇ"),
            ("source",          e["record_badge"], "📰", "Sᴏᴜʀᴄᴇ"),
            ("breach",          e["record_badge"], "📰", "Bʀᴇᴀᴄʜ"),
            ("age",             e["id_card"],      "🗓️", "Aɢᴇ"),
            ("gender",          e["name"],         "👤", "Gᴇɴᴅᴇʀ"),
            ("district",        e["circle"],       "🔎", "Dɪꜱᴛʀɪᴄᴛ"),
            ("password",        e["id_card"],      "💳", "Pᴀꜱꜱᴡᴏʀᴅ"),
            ("hash",            e["id_card"],      "💳", "Hᴀꜱʜ"),
            ("username",        e["name"],         "👤", "Uꜱᴇʀɴᴀᴍᴇ"),
        ]
        rendered_keys = set()
        for field, emoji_key, fb, label in PRIORITY:
            if field in rec and field not in rendered_keys:
                v = _fv(rec[field])
                if v != "—":
                    lines.append(f"{te(emoji_key, fb)} <b>{label}</b>: {v}")
                    rendered_keys.add(field)

        # remaining keys not already shown
        for k, v in rec.items():
            if k in rendered_keys or k in SKIP_KEYS:
                continue
            sv = _fv(v)
            if sv != "—":
                display = k.replace("_", " ").title()
                lines.append(f"{te(e['record_badge'], '📰')} <b>{display}</b>: {sv}")

        lines.append("</blockquote>")
        return "\n".join(lines)

    out_pages = []
    for page_idx, page_records in enumerate(pages_data):
        page_num = page_idx + 1
        record_start = page_idx * RECORDS_PER_PAGE + 1

        header = (
            divider
            + f"{te(e['phone_title'], '📞')}  <b>𝗟𝗘𝗔𝗞 𝗗𝗕 𝗟𝗢𝗢𝗞𝗨𝗣</b>  "
            f"{te(e['phone_title'], '📞')}\n"
            + divider
            + f"\n<blockquote>"
            f"{te(e['status_ok'], '✅')} Lᴇᴀᴋᴇᴅ Dᴀᴛᴀʙᴀꜱᴇ ✅\n\n"
            f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>   : <code>{query}</code>\n"
            f"{te(e['records'], '✍️')} <b>Rᴇᴄᴏʀᴅꜱ</b> : {total} | Page {page_num}/{total_pages}"
            f"</blockquote>\n\n"
        )

        body_parts = []
        for r_idx, record in enumerate(page_records):
            abs_num = record_start + r_idx
            body_parts.append(_render_record(record, abs_num, total))

        elapsed = round(_time.time() - start_ts, 2)
        try:
            bot_username = f"@{bot.get_me().username}"
        except Exception:
            bot_username = "@YourBot"

        footer = (
            divider
            + f"{te(e['speed'], '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
            + f"{te(e['bot_icon'], '🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'], '🌟')}\n"
            + divider
        )

        full_txt = header + "\n".join(body_parts) + "\n\n" + footer

        # ── paginate if > 4096 ──
        if len(full_txt) <= 4096:
            markup = None
            if total_pages > 1:
                markup = InlineKeyboardMarkup(row_width=3)
                btns = []
                if page_idx > 0:
                    btns.append(create_primary_button("◀ Prev", callback_data=f"leak_page_{page_idx - 1}_{0}"))
                btns.append(create_success_button(f"{page_num}/{total_pages}", callback_data="noop"))
                if page_idx < total_pages - 1:
                    btns.append(create_danger_button("Next ▶", callback_data=f"leak_page_{page_idx + 1}_{0}"))
                markup.row(*btns)
            out_pages.append((full_txt, markup))
        else:
            # split at record boundaries
            sub_parts = [header]
            for part in body_parts:
                candidate = "".join(sub_parts) + part + "\n\n"
                if len(candidate + footer) > 4096 and len(sub_parts) > 1:
                    sub_parts.append(footer)
                    out_pages.append(("".join(sub_parts), None))
                    sub_parts = [header, part + "\n\n"]
                else:
                    sub_parts.append(part + "\n\n")
            sub_parts.append(footer)
            out_pages.append(("".join(sub_parts), None))

    return out_pages if out_pages else [(_err("NO DATA FOUND")[0][0], None)]

def format_number_result(result, query, credits_left):
    import time as _time

    divider = build_num_divider()
    e = NUM_EMOJIS

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['phone_title'], '📞')}  <b>𝗡𝗨𝗠𝗕𝗘𝗥 𝗜𝗡𝗙𝗢</b>  {te(e['phone_title'], '📞')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} Qᴜᴇʀʏ  : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ: <b>{credits_left}</b>\n"
            + divider
        )
        return [(txt, None)]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    records = result.get("records", [])
    total   = result.get("total", len(records))

    if not records:
        return _err("NO DATA FOUND")

    RECORDS_PER_PAGE = 2
    pages_data = [records[i:i+RECORDS_PER_PAGE] for i in range(0, len(records), RECORDS_PER_PAGE)]
    total_pages = len(pages_data)

    start_ts = _time.time()
    out = []

    for page_idx, page_records in enumerate(pages_data):
        page_num    = page_idx + 1
        record_start = page_idx * RECORDS_PER_PAGE + 1

        txt  = divider
        txt += (
            f"{te(e['phone_title'], '📞')}  <b>𝗡𝗨𝗠𝗕𝗘𝗥 𝗜𝗡𝗙𝗢</b>  "
            f"{te(e['phone_title'], '📞')}\n"
        )
        txt += divider

        txt += (
            f"\n<blockquote>"
            f"{te(e['status_ok'], '✅')} Nᴜᴍʙᴇʀ Dᴀᴛᴀʙᴀꜱᴇ ✅\n\n"
            f"{te(e['query'],   '🔭')} Qᴜᴇʀʏ    : <code>{result.get('number', query)}</code>\n"
            f"{te(e['records'], '✍️')} Rᴇᴄᴏʀᴅꜱ  : {total} | Page {page_num}/{total_pages}"
            f"</blockquote>\n\n"
        )

        for r_idx, record in enumerate(page_records):
            abs_num = record_start + r_idx
            txt += (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{te(e['record_badge'], '📰')} Rᴇᴄᴏʀᴅ {abs_num}/{total}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )

            def _field(val):
                return val if val and str(val).strip() not in ("N/A", "None", "null", "") else "—"

            txt += f"<blockquote>"
            txt += f"{te(e['number'],  '📲')} <b>Nᴜᴍʙᴇʀ</b>    : <code>{_field(record.get('mobile'))}</code>\n"
            txt += f"{te(e['name'],    '👤')} <b>Nᴀᴍᴇ</b>      : {_field(record.get('name'))}\n"
            _tc = _field(record.get('truecaller_name'))
            if _tc != "—": txt += f"{te(e['name'], '📛')} <b>TC Nᴀᴍᴇ</b>   : {_tc}\n"
            txt += f"{te(e['father'],  '👥')} <b>Fᴀᴛʜᴇʀ</b>    : {_field(record.get('father_name'))}\n"
            txt += f"{te(e['alt_num'], '📞')} <b>Aʟᴛ Nᴜᴍ</b>   : <code>{_field(record.get('alternate'))}</code>\n"
            txt += f"{te(e['circle'],  '🔎')} <b>Cɪʀᴄʟᴇ</b>    : {_field(record.get('circle'))}\n"
            txt += f"{te(e['address'], '🏡')} <b>Aᴅᴅʀᴇꜱꜱ</b>  : {_field(record.get('address'))}\n"
            txt += f"{te(e['id_card'], '💳')} <b>Iᴅ</b>        : <code>{_field(record.get('id'))}</code>\n"
            txt += f"{te(e['email'],   '📧')} <b>Eᴍᴀɪʟ</b>     : {_field(record.get('email'))}\n"
            txt += f"</blockquote>\n\n"

        elapsed = round(_time.time() - start_ts, 2)
        bot_username = "@YourBot"
        try:
            bot_username = f"@{bot.get_me().username}"
        except Exception:
            pass

        txt += divider
        txt += (
            f"{te(e['speed'],    '🚀')} ʀᴇꜱᴩᴏɴꜱᴇ ᴛɪᴍᴇ: {elapsed}s\n"
            f"{te(e['credits'],  '🧱')} ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ: <b>{credits_left}</b>\n"
        )
        txt += divider
        txt += (
            f"{te(e['bot_icon'], '🤖')} Pᴏᴡᴇʀᴇᴅ Bʏ: <b>{bot_username}</b>  "
            f"{te(e['star'], '🌟')}\n"
        )
        txt += divider

        markup = None
        if total_pages > 1:
            markup = InlineKeyboardMarkup(row_width=3)
            buttons = []

            if page_num > 1:
                buttons.append(
                    create_primary_button(
                        "Prev",
                        callback_data=f"num_page_{page_num - 1}_{query}",
                        icon_custom_emoji_id="5246794802560774143"
                    )
                )

            buttons.append(
                create_success_button(
                    f"{page_num}/{total_pages}",
                    callback_data="num_page_noop",
                    icon_custom_emoji_id="5422537279400868395"
                )
            )

            if page_num < total_pages:
                buttons.append(
                    create_danger_button(
                        "Next",
                        callback_data=f"num_page_{page_num + 1}_{query}",
                        icon_custom_emoji_id="6192763368139397161"
                    )
                )

            markup.row(*buttons)

            if page_num == total_pages:
                markup.add(
                    InlineKeyboardButton("Download JSON", callback_data="num_dl_json")
                )

        out.append((txt, markup))

    return out
def format_aadhar_result(result, query, credits_left):
    import time as _time

    e       = ADHR_EMOJIS
    divider = build_adhr_divider()

    def _field(val):
        return val if val and str(val).strip() not in ("N/A", "None", "null", "", "—") else "—"

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['adhr_title'], '🪪')}  <b>𝗔𝗔𝗗𝗛𝗔𝗥 𝗜𝗡𝗙𝗢</b>  {te(e['adhr_title'], '🪪')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>  : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '📊')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return [(txt, None)]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    records = result.get("records", [])
    total   = result.get("total", len(records))

    if not records:
        return _err("NO DATA FOUND")

    RECORDS_PER_PAGE = 4
    pages_data  = [records[i:i + RECORDS_PER_PAGE] for i in range(0, len(records), RECORDS_PER_PAGE)]
    total_pages = len(pages_data)
    start_ts    = _time.time()
    out         = []

    for page_idx, page_records in enumerate(pages_data):
        page_num     = page_idx + 1
        record_start = page_idx * RECORDS_PER_PAGE + 1

        # ── header ──
        txt  = divider
        txt += f"{te(e['adhr_title'], '🪪')}  <b>𝗔𝗔𝗗𝗛𝗔𝗥 𝗜𝗡𝗙𝗢</b>  {te(e['adhr_title'], '🪪')}\n"
        txt += divider
        txt += (
            f"<blockquote>"
            f"{te(e['status_ok'], '✅')} Aᴀᴅʜᴀʀ Dᴀᴛᴀʙᴀꜱᴇ ✅\n\n"
            f"{te(e['query'],   '🔭')} <b>Qᴜᴇʀʏ</b>   : <code>{query}</code>\n"
            f"{te(e['records'], '✍️')} <b>Rᴇᴄᴏʀᴅꜱ</b> : {total} | Page {page_num}/{total_pages}"
            f"</blockquote>\n\n"
        )

        # ── records ──
        for r_idx, record in enumerate(page_records):
            abs_num   = record_start + r_idx
            email_val = _field(record.get('email'))

            txt += (
                f"<blockquote>"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{te(e['record_badge'], '📰')} <b>Rᴇᴄᴏʀᴅ {abs_num}/{total}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{te(e['mobile'],  '📲')} <b>Mᴏʙɪʟᴇ</b>   : <code>{_field(record.get('mobile'))}</code>\n"
                f"{te(e['name'],    '👤')} <b>Nᴀᴍᴇ</b>     : {_field(record.get('name'))}\n"
                f"{te(e['father'],  '👥')} <b>Fᴀᴛʜᴇʀ</b>   : {_field(record.get('father_name'))}\n"
                f"{te(e['circle'],  '🛜')} <b>Cɪʀᴄʟᴇ</b>   : {_field(record.get('circle'))}\n"
                f"{te(e['address'], '🏡')} <b>Aᴅᴅʀᴇꜱꜱ</b> : {_field(record.get('address'))}\n"
                f"{te(e['id_card'], '💳')} <b>Aᴀᴅʜᴀᴀʀ</b>  : <code>{_field(record.get('aadhar'))}</code>\n"
                + (f"{te(e['email'], '📧')} <b>Eᴍᴀɪʟ</b>    : {email_val}\n" if email_val != "—" else "")
                + f"</blockquote>\n\n"
            )

        # ── footer ──
        elapsed      = round(_time.time() - start_ts, 2)
        bot_username = "@YourBot"
        try:
            bot_username = f"@{bot.get_me().username}"
        except Exception:
            pass

        txt += divider
        txt += (
            f"<blockquote>"
            f"{te(e['speed'],    '🚀')} <b>ʀᴇꜱᴩᴏɴꜱᴇ ᴛɪᴍᴇ</b>:- {elapsed}s\n"
            f"{te(e['credits'],  '📊')} <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ</b>:- {credits_left}\n"
            f"</blockquote>\n"
        )
        txt += divider
        txt += (
            f"<blockquote>"
            f"{te(e['bot_icon'], '🤖')} Pᴏᴡᴇʀᴇᴅ Bʏ: <b>{bot_username}</b>  {te(e['star'], '🌟')}\n"
            f"</blockquote>\n"
        )
        txt += divider

        # ── pagination buttons ──
        markup = None
        if total_pages > 1:
            markup  = InlineKeyboardMarkup(row_width=3)
            buttons = []
            if page_num > 1:
                buttons.append(create_primary_button(
                    "Prev",
                    callback_data=f"num_page_{page_num - 1}_{query}",
                    icon_custom_emoji_id="5246794802560774143"
                ))
            buttons.append(create_success_button(
                f"{page_num}/{total_pages}",
                callback_data="num_page_noop",
                icon_custom_emoji_id="5422537279400868395"
            ))
            if page_num < total_pages:
                buttons.append(create_danger_button(
                    "Next",
                    callback_data=f"num_page_{page_num + 1}_{query}",
                    icon_custom_emoji_id="6192763368139397161"
                ))
            markup.row(*buttons)
            if page_num == total_pages:
                markup.add(InlineKeyboardButton("Download JSON", callback_data="num_dl_json"))

        out.append((txt, markup))

    return out


# ============================================================
# VEHICLE / RC FORMATTER WITH PAGINATION
# ============================================================

VECH_PAGINATION = {}   # user_id -> {"pages": [str], "current": int, "query": str, "json_file": str}

def _vfield(data, *keys, default="N/A"):
    """Safely extract nested dict values."""
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k)
        else:
            return default
    return data if data is not None and str(data).strip() not in ("", "None", "null", "N/A") else default

def _vfmt(val):
    return str(val) if val and str(val).strip() not in ("", "None", "null", "N/A", "—") else "N/A"

def format_vehicle_result_v2(result, query, credits_left):
    """
    Format /vech response.
    Handles both new flat API (success/vehicle/owner_name/mobile_number/
    chassis_number/engine_number) and old nested API structures.
    All output in <blockquote> blocks, premium emoji, paginated if > 1024 chars.
    """
    import time as _time
    e = VECH_EMOJIS
    divider = build_vech_divider()

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['veh_title'], '🪪')}  <b>𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢</b>  {te(e['veh_title'], '🪪')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>   : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return [txt]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    def _f(val):
        v = str(val).strip() if val is not None else ""
        return v if v and v.lower() not in ("n/a", "none", "null", "—", "") else "—"

    start_ts = _time.time()

    # ── New flat API response ────────────────────────────────────────
    # Keys: status, vehicle, owner_name, mobile_number, chassis_number, engine_number
    if result.get("vehicle") and (result.get("owner_name") or result.get("mobile_number")):
        owner      = _f(result.get("owner_name"))
        mobile     = _f(result.get("mobile_number"))
        chassis    = _f(result.get("chassis_number"))
        engine     = _f(result.get("engine_number"))
        rc_num     = _f(result.get("vehicle", query))
        elapsed    = round(_time.time() - start_ts, 2)
        try:
            bot_username = "@" + bot.get_me().username
        except Exception:
            bot_username = "@YourBot"

        txt = (
            divider
            + f"{te(e['veh_title'], '🪪')}  <b>𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢</b>  {te(e['veh_title'], '🪪')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'],   '🔭')} <b>Qᴜᴇʀʏ</b>      : <code>{query}</code>\n"
            + f"{te(e['status_ok'],'✅')} <b>Sᴛᴀᴛᴜꜱ</b>     : ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ✅\n"
            + f"</blockquote>\n\n"
            + f"<blockquote>"
            + f"{te(e['rc_number'],  '🚗')} <b>Vᴇʜɪᴄʟᴇ</b>    : <code>{rc_num}</code>\n"
            + f"{te(e['owner'],      '👨‍🦰')} <b>Oᴡɴᴇʀ</b>      : {owner}\n"
            + f"{te(e['mobile'],     '📲')} <b>Mᴏʙɪʟᴇ</b>     : <code>{mobile}</code>\n"
            + f"{te(e['chassis'],    '🔧')} <b>Cʜᴀꜱꜱɪꜱ</b>    : <code>{chassis}</code>\n"
            + f"{te(e['engine'],     '⚙️')} <b>Eɴɢɪɴᴇ</b>     : <code>{engine}</code>\n"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
            + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
            + divider
        )
        return [txt]

    # ── Old nested API response — parse deep structure ───────────────
    d = result
    if "result" in result and isinstance(result["result"], dict):
        d = result["result"]
    if "data" in d and isinstance(d["data"], dict):
        d = d["data"]

    mobile_top = str(d.get("mobile", "")).strip() if isinstance(d, dict) else ""

    s1 = {}; s2 = {}; a2 = {}
    try: s1 = d["vehicle_info"]["results"]["api1"]["result"]["source1"]["details"]
    except Exception: pass
    try: s2 = d["vehicle_info"]["results"]["api1"]["result"]["source2"]["data"]["Nexus2"]
    except Exception: pass
    try: a2 = d["vehicle_info"]["results"]["api2"]["data"]["data"]
    except Exception: pass

    def _g(*keys, srcs=(s1, s2, a2), default="N/A"):
        for src in srcs:
            if not isinstance(src, dict): continue
            val = src
            for k in keys:
                val = val.get(k) if isinstance(val, dict) else None
                if val is None: break
            if val is not None and str(val).strip() not in ("", "None", "null", "N/A", "—"):
                return str(val).strip()
        return default

    def _g2(key, srcs=(s1, s2, a2), default="N/A"):
        for src in srcs:
            if not isinstance(src, dict): continue
            for k in (key, " " + key, key + " ", " " + key + " "):
                if k in src:
                    v = src[k]
                    if v is not None and str(v).strip() not in ("", "None", "null", "N/A", "—"):
                        return str(v).strip()
        return default

    owner      = _g2("Reg Owner") or _g2("Owner Name") or _g2("owner")
    mobile_num = mobile_top or _g2("Mobile Number") or _g2("Mobile No")
    chassis    = _g2("Chassis Number") or _g2("Chassis No") or _g2("chassis")
    engine     = _g2("Engine Number") or _g2("Engine No") or _g2("engine")
    reg_date   = _g2("Registration Date") or _g2("Reg Date")
    fuel_type  = _g2("Fuel Type") or _g2("fuel_type")
    vehicle_cls= _g2("Vehicle Category") or _g2("Class of Vehicle")
    mfg_yr     = _g2("Year of Manufacture") or _g2("Manufacture Year")
    color      = _g2("Colour") or _g2("Color")
    ins_co     = _g2("Insurance Company Name") or _g2("Insurance Company")
    ins_valid  = _g2("Insurance Upto") or _g2("Insurance Valid")
    rto_name   = _g2("RTO Name") or _g2("Registered RTO")
    state      = _g2("State") or _g2("state")
    father     = _g2("Father") or _g2("father_name")
    address    = _g2("Present Address") or _g2("address")

    elapsed = round(_time.time() - start_ts, 2)
    try:
        bot_username = "@" + bot.get_me().username
    except Exception:
        bot_username = "@YourBot"

    def _row(emoji_key, fb, label, val):
        if val and val not in ("N/A", "—"):
            return f"{te(e.get(emoji_key, e['query']), fb)} <b>{label}</b>: {val}\n"
        return ""

    txt = (
        divider
        + f"{te(e['veh_title'], '🪪')}  <b>𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢</b>  {te(e['veh_title'], '🪪')}\n"
        + divider
        + f"\n<blockquote>"
        + f"{te(e['query'],   '🔭')} <b>Qᴜᴇʀʏ</b>   : <code>{query}</code>\n"
        + f"{te(e['status_ok'],'✅')} <b>Sᴛᴀᴛᴜꜱ</b>  : ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ✅\n"
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + _row('rc_number', '🚗', 'Vᴇʜɪᴄʟᴇ', query)
        + _row('owner', '👨‍🦰', 'Oᴡɴᴇʀ', owner)
        + _row('mobile', '📲', 'Mᴏʙɪʟᴇ', mobile_num)
        + _row('chassis', '🔧', 'Cʜᴀꜱꜱɪꜱ', chassis)
        + _row('engine', '⚙️', 'Eɴɢɪɴᴇ', engine)
        + _row('fuel', '⛽', 'Fᴜᴇʟ', fuel_type)
        + _row('vclass', '🚗', 'Cʟᴀꜱꜱ', vehicle_cls)
        + _row('mfg_year', '📅', 'Mꜰɢ Yᴇᴀʀ', mfg_yr)
        + _row('reg_date', '📅', 'Rᴇɢ Dᴀᴛᴇ', reg_date)
        + _row('colour', '🎨', 'Cᴏʟᴏᴜʀ', color)
        + _row('father', '👥', 'Fᴀᴛʜᴇʀ', father)
        + _row('address', '🏡', 'Aᴅᴅʀᴇꜱꜱ', address)
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + _row('insurance', '🛡', 'Iɴꜱᴜʀᴀɴᴄᴇ', ins_co)
        + _row('ins_status', '📅', 'Iɴꜱ Vᴀʟɪᴅ', ins_valid)
        + _row('rto_code', '🗺', 'RTO', rto_name)
        + _row('registration', '📍', 'Sᴛᴀᴛᴇ', state)
        + f"</blockquote>\n\n"
        + divider
        + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
        + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
        + divider
        + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
        + divider
    )
    return [txt]

def format_rc_result_v2(result, query, credits_left):
    """Format /vechrc response — parses flat sectioned JSON."""
    import time as _time
    e = VECH_EMOJIS
    divider = build_vech_divider()

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['veh_title'], '🪪')}  <b>𝗥𝗖 𝗜𝗡𝗙𝗢</b>  {te(e['veh_title'], '🪪')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>  : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return [txt]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    d = result
    if "result" in result and isinstance(result["result"], dict):
        d = result["result"]

    def _sec(name):
        return d.get(name, {}) if isinstance(d, dict) else {}

    def _v(val, default="N/A"):
        return str(val).strip() if val is not None and str(val).strip() not in ("", "None", "null", "N/A", "—") else default

    own = _sec("Ownership Details")
    veh = _sec("Vehicle Details")
    ins = _sec("Insurance Information")
    dates = _sec("Important Dates & Validity")
    other = _sec("Other Information")
    card = _sec("Basic Card Info")
    alert = _sec("Insurance Alert")
    reg_num = _v(d.get("registration_number", query))

    rc_num      = _v(own.get("Registration Number", reg_num))
    owner_name  = _v(own.get("Owner Name", card.get("Owner Name")))
    rto_name    = _v(own.get("Registered RTO"))

    make        = _v(veh.get("Model Name", card.get("Modal Name")))
    model       = _v(veh.get("Maker Model", veh.get("Model Name")))
    vclass      = _v(veh.get("Vehicle Class"))
    fuel        = _v(veh.get("Fuel Type"))
    chassis_no  = _v(veh.get("Chassis Number"))
    engine_no   = _v(veh.get("Engine Number"))

    ins_expiry  = _v(ins.get("Insurance Expiry", ins.get("If your insurance has expired, renew it")))
    ins_company = _v(ins.get("Insurance Company"))
    policy_no   = _v(ins.get("Insurance No"))

    reg_date    = _v(dates.get("Registration Date"))
    vehicle_age = _v(dates.get("Vehicle Age"))
    fitness     = _v(dates.get("Fitness Upto"))
    ins_upto    = _v(dates.get("Insurance Upto"))
    ins_alert   = _v(dates.get("Insurance Expiry In"))

    financer    = _v(other.get("Financer Name"))
    cc_val      = _v(other.get("Cubic Capacity"))
    seating     = _v(other.get("Seating Capacity"))
    permit      = _v(other.get("Permit Type"))
    blacklist   = _v(other.get("Blacklist Status"))
    noc         = _v(other.get("NOC Details"))

    city        = _v(card.get("City Name"))
    phone       = _v(card.get("Phone"))
    website     = _v(card.get("Website"))
    address     = _v(card.get("Address"))
    code        = _v(card.get("Code"))

    expired_days = _v(alert.get("Expired Days"))

    start_ts = _time.time()

    lines = []
    lines.append(divider)
    lines.append(f"{te(e['veh_title'], '🪪')}  <b>𝗥𝗖 𝗜𝗡𝗙𝗢</b>  {te(e['veh_title'], '🪪')}")
    lines.append(divider)
    lines.append("")
    lines.append(f"{te(e['rc_number'], '🚗')} <b>Rᴄ Nᴜᴍʙᴇʀ</b>  : <code>{rc_num}</code>")
    lines.append(f"{te(e['source'], '💎')} <b>Sᴏᴜʀᴄᴇ</b>    : ꜰᴇʟɪxᴀᴩɪ {te(e['status_ok'], '✅')}")
    lines.append("")

    # Vehicle Details
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{te(e['vehicle'], '🚗')} <b>Vᴇʜɪᴄʟᴇ Dᴇᴛᴀɪʟꜱ</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  {te(e['make'], '🏭')} <b>Mᴀᴋᴇ</b>         : {make}")
    lines.append(f"  {te(e['vehicle'], '🚗')} <b>Mᴏᴅᴇʟ</b>        : {model}")
    lines.append(f"  {te(e['vclass'], '👿')} <b>Cʟᴀꜱꜱ</b>         : {vclass}")
    lines.append(f"  {te(e['fuel'], '⚡️')} <b>Fᴜᴇʟ</b>         : {fuel}")
    lines.append(f"  {te(e['cc'], '⚙️')} <b>Cᴄ</b>            : {cc_val}")
    lines.append(f"  {te(e['seating'], '🐸')} <b>Sᴇᴀᴛɪɴɢ</b>      : {seating}")
    lines.append(f"  {te(e['chassis'], '🔧')} <b>Cʜᴀꜱꜱɪꜱ</b>     : <code>{chassis_no}</code>")
    lines.append(f"  {te(e['engine'], '⚙️')} <b>Eɴɢɪɴᴇ</b>       : <code>{engine_no}</code>")
    lines.append("")

    # Owner Info
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{te(e['body'], '🏎')} <b>Oᴡɴᴇʀ Iɴꜰᴏ</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  {te(e['owner'], '👨‍🦰')} <b>Nᴀᴍᴇ</b>         : {owner_name}")
    lines.append(f"  {te(e['address'], '🏡')} <b>Aᴅᴅʀᴇꜱꜱ</b>      : {address}")
    lines.append(f"  {te(e['rto'], '🗺')} <b>Cɪᴛʏ</b>          : {city}")
    lines.append(f"  {te(e['mobile'], '📲')} <b>Pʜᴏɴᴇ</b>        : {phone}")
    lines.append("")

    # Registration
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{te(e['registration'], '0️⃣')} <b>𝗥𝗘𝗚𝗜𝗦𝗧𝗥𝗔𝗧𝗜𝗢𝗡</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  {te(e['reg_date'], '🗓️')} <b>Rᴇɢ Dᴀᴛᴇ</b>     : {reg_date}")
    lines.append(f"  {te(e['rto'], '🗺')} <b>Rᴛᴏ</b>           : {rto_name}")
    lines.append(f"  {te(e['rto_code'], '🔢')} <b>Rᴛᴏ Cᴏᴅᴇ</b>    : {code}")
    lines.append(f"  {te(e['financer'], '🏢')} <b>Fɪɴᴀɴᴄᴇʀ</b>     : {financer}")
    lines.append(f"  {te(e['reg_date'], '🗓️')} <b>Fɪᴛɴᴇꜱꜱ Uᴘᴛᴏ</b> : {fitness}")
    lines.append(f"  {te(e['status_ok'], '✅')} <b>Bʟᴀᴄᴋʟɪꜱᴛ</b>   : {blacklist}")
    lines.append(f"  {te(e['status_ok'], '✅')} <b>NOC</b>          : {noc}")
    lines.append(f"  {te(e['reg_date'], '🗓️')} <b>Vᴇʜɪᴄʟᴇ Aɢᴇ</b>  : {vehicle_age}")
    lines.append("")

    # Insurance
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{te(e['insurance'], '🏎')} <b>𝗜𝗡𝗦𝗨𝗥𝗔𝗡𝗖𝗘</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  {te(e['ins_status'], '🛡')} <b>Sᴛᴀᴛᴜꜱ</b>        : {ins_alert}")
    lines.append(f"  {te(e['financer'], '🏢')} <b>Cᴏᴍᴩᴀɴʏ</b>      : {ins_company}")
    lines.append(f"  {te(e['colour'], '📋')} <b>Pᴏʟɪᴄʏ ɴᴏ.</b>  : <code>{policy_no}</code>")
    lines.append(f"  {te(e['reg_date'], '🗓️')} <b>Eхᴘɪʀʏ</b>       : {ins_expiry}")
    lines.append(f"  {te(e['reg_date'], '🗓️')} <b>Eхᴘɪʀᴇᴅ Dᴀʏꜱ</b> : {expired_days}")
    lines.append("")

    # Other Info
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{te(e['technical'], '⚙️')} <b>𝗢𝗧𝗛𝗘𝗥 𝗜𝗡𝗙𝗢</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  {te(e['commercial'], '💎')} <b>Pᴇʀᴍɪᴛ</b>       : {permit}")
    lines.append(f"  {te(e['rto'], '🗺')} <b>Wᴇʙꜱɪᴛᴇ</b>      : {website}")
    lines.append("")

    # Footer
    elapsed = round(_time.time() - start_ts, 2)
    try:
        bot_username = f"@{bot.get_me().username}"
    except Exception:
        bot_username = "@YourBot"

    lines.append(f"{te(e['credits'], '🧱')} <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ</b>:- {credits_left}")
    lines.append(divider)
    lines.append(f"{te(e['bot_icon'], '🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'], '🌟')}")
    lines.append(divider)

    full_text = "\n".join(lines)

    wrapped = f"<blockquote>{full_text}</blockquote>"
    if len(wrapped) <= 4096 and len(full_text) <= 3500:
        return [wrapped]

    # Split at section boundaries
    sec_markers = [
        "━━━━━━━━━━━━━━━━━━━━━\n  🏭 <b>Mᴀᴋᴇ</b>",         # vehicle details start
        "━━━━━━━━━━━━━━━━━━━━━\n  👨‍🦰 <b>Nᴀᴍᴇ</b>",         # owner info start
        "━━━━━━━━━━━━━━━━━━━━━\n  🗓️ <b>Rᴇɢ Dᴀᴛᴇ</b>",     # registration start
        "━━━━━━━━━━━━━━━━━━━━━\n  🛡 <b>Sᴛᴀᴛᴜꜱ</b>",        # insurance start
        "━━━━━━━━━━━━━━━━━━━━━\n  💎 <b>Pᴇʀᴍɪᴛ</b>",        # other info start
        "🧱 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ</b>",                           # footer start
    ]

    split_positions = [0]
    for marker in sec_markers:
        pos = full_text.find(marker)
        if pos != -1 and pos not in split_positions:
            split_positions.append(pos)
    split_positions.sort()
    split_positions.append(len(full_text))

    pages = []
    for i in range(len(split_positions) - 1):
        page_text = full_text[split_positions[i]:split_positions[i+1]].rstrip("\n")
        if page_text:
            pages.append(page_text)

    merged = []
    current = ""
    for page in pages:
        test = current + ("\n" if current else "") + page
        if len(test) <= 1024:
            current = test
        else:
            if current:
                merged.append(current)
            current = page
    if current:
        merged.append(current)

    final_pages = []
    for i, page in enumerate(merged, 1):
        if len(merged) > 1:
            page += f"\n{te(e['records'], '✍️')} <b>Page {i}/{len(merged)}</b>"
        final_pages.append(f"<blockquote>{page}</blockquote>")

    return final_pages if final_pages else [f"<blockquote>{full_text}</blockquote>"]


def send_paginated_vech(chat_id, user_id, pages, json_file, query, wait_message=None, command="vech"):
    """Send paginated vehicle/RC result with Prev/Next buttons."""
    if wait_message:
        try:
            bot.delete_message(chat_id, wait_message.message_id)
        except:
            pass

    VECH_PAGINATION[user_id] = {
        "pages": pages,
        "current": 0,
        "query": query,
        "json_file": json_file,
        "command": command,
    }

    _send_vech_page(chat_id, user_id, 0)


def _send_vech_page(chat_id, user_id, page_idx):
    state = VECH_PAGINATION.get(user_id)
    if not state:
        return

    pages = state["pages"]
    if page_idx < 0 or page_idx >= len(pages):
        return

    state["current"] = page_idx
    text = pages[page_idx]

    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []

    if len(pages) > 1:
        if page_idx > 0:
            buttons.append(create_primary_button(
                "◀ Prev",
                callback_data=f"vech_page_{page_idx - 1}_{user_id}",
                icon_custom_emoji_id="5246794802560774143"
            ))

        buttons.append(create_success_button(
            f"{page_idx + 1}/{len(pages)}",
            callback_data=f"vech_noop_{user_id}",
            icon_custom_emoji_id="5422537279400868395"
        ))

        if page_idx < len(pages) - 1:
            buttons.append(create_danger_button(
                "Next ▶",
                callback_data=f"vech_page_{page_idx + 1}_{user_id}",
                icon_custom_emoji_id="6192763368139397161"
            ))

        markup.row(*buttons)

    # JSON download on last page
    json_file = state.get("json_file")
    if page_idx == len(pages) - 1 and json_file:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))

    markup.add(create_default_button("🔙 Back", callback_data="back_to_result", icon_custom_emoji_id="5307517685846785869"))

    sent = send_with_premium(chat_id, text, reply_markup=markup)
    if sent:
        schedule_deletion(sent.message_id, chat_id)
        send_random_media(chat_id, sent.message_id)



def format_insta_result_v3(result, query, credits_left):
    """Format /insta response from sahilcc.dpdns.org API with premium emojis."""
    import time as _time
    e = INSTA_EMOJIS
    divider = build_insta_divider()
    start_ts = _time.time()

    def _err(msg_text):
        lines = []
        lines.append("<blockquote>")
        lines.append(divider)
        lines.append(te(e["insta_title"], "📸") + "  <b>𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>  " + te(e["insta_title"], "📸"))
        lines.append(divider)
        lines.append("")
        lines.append(te(e["username"], "⭐") + " <b>Qᴜᴇʀʏ</b>  : <code>" + str(query) + "</code>")
        lines.append(te(e["status"], "❌") + " " + str(msg_text))
        lines.append("")
        lines.append(divider)
        lines.append(te(e["credits"], "🧱") + " <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: " + str(credits_left))
        lines.append(divider)
        lines.append("</blockquote>")
        return chr(10).join(lines)

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    raw_profile = result.get("result", {}).get("profile", {}) if isinstance(result.get("result"), dict) else {}
    if not raw_profile:
        raw_profile = result.get("profile", {})
    if not raw_profile:
        return _err("NO DATA FOUND")

    p = raw_profile
    username      = str(p.get("username", query) or query)
    full_name     = str(p.get("full_name", "N/A") or "N/A")
    user_id       = str(p.get("id", "N/A") or "N/A")
    is_private    = bool(p.get("is_private", False))
    is_business   = bool(p.get("is_business_account", False))
    is_verified   = bool(p.get("is_verified", False))
    is_professional = bool(p.get("is_professional_account", False))
    joined_year   = str(p.get("account_creation_year", "N/A") or "N/A")
    followers     = p.get("followers", 0) or 0
    following     = p.get("following", 0) or 0
    posts_count   = p.get("posts", 0) or 0
    bio           = str(p.get("biography", "") or "")
    external_url  = str(p.get("external_url", "") or "")
    category_name = str(p.get("category_name", "") or "")

    if is_business:
        acc_type = "💼 ʙᴜꜱɪɴᴇꜱꜱ"
    elif is_professional:
        acc_type = "💼 ᴩʀᴏꜰᴇꜱꜱɪᴏɴᴀʟ"
    else:
        acc_type = "👤 ᴩᴇʀꜱᴏɴᴀʟ"

    status_icon = "🔒" if is_private else "🌍"
    status_text = "ᴩʀɪᴠᴀᴛᴇ" if is_private else "ᴩᴜʙʟɪᴄ"
    verified_badge = "✅" if is_verified else "❌"

    elapsed = round(_time.time() - start_ts, 2)
    try:
        bot_username = "@" + bot.get_me().username
    except Exception:
        bot_username = "@YourBot"

    lines = []
    lines.append("<blockquote>")
    # Header
    lines.append(divider)
    lines.append(te(e["insta_title"], "📸") + "  <b>𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>  " + te(e["insta_title"], "📸"))
    lines.append(divider)
    lines.append("")
    # Profile info
    lines.append(te(e["username"], "⭐") + " <b>ᴜꜱᴇʀɴᴀᴍᴇ</b>  » @" + username + " " + verified_badge)
    lines.append(te(e["name"], "👑") + " <b>Nᴀᴍᴇ</b>      » " + full_name)
    lines.append(te(e["user_id"], "🆔") + " <b>ᴜꜱᴇʀ ɪᴅ</b>   » " + user_id)
    lines.append(te(e["status"], "🔒") + " <b>ꜱᴛᴀᴛᴜꜱ</b>    » " + status_icon + " " + status_text)
    lines.append(te(e["acc_type"], "💼") + " <b>ᴀᴄᴄ ᴛʏᴩᴇ</b>  » " + acc_type)
    lines.append(te(e["joined"], "📅") + " <b>ᴊᴏɪɴᴇᴅ</b>     » " + joined_year)
    if bio:
        lines.append(te(e["name"], "👑") + " <b>ʙɪᴏ</b>        » " + bio[:150])
    if external_url:
        lines.append(te(e["profile"], "❔") + " <b>ʟɪɴᴋ</b>       » " + external_url[:50])
    if category_name and category_name.lower() != "none":
        lines.append(te(e["acc_type"], "💼") + " <b>ᴄᴀᴛᴇɢᴏʀʏ</b>  » " + category_name)
    lines.append("")
    # Stats
    lines.append(divider)
    lines.append(te(e["followers"], "👥") + " <b>ꜰᴏʟʟᴏᴡᴇʀꜱ</b> » " + format_number(followers))
    lines.append(te(e["following"], "🔄") + " <b>ꜰᴏʟʟᴏᴡɪɴɢ</b> » " + format_number(following))
    lines.append(te(e["posts"], "📱") + " <b>ᴩᴏꜱᴛꜱ</b>     » " + format_number(posts_count))
    lines.append("")
    # Footer
    lines.append(divider)
    lines.append(te(e["speed"], "🚀") + " <b>ʀᴇꜱᴩᴏɴꜱᴇ ᴛɪᴍᴇ</b>:- " + str(elapsed) + "s")
    lines.append(te(e["credits"], "🧱") + " <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ</b>:- " + str(credits_left))
    lines.append(divider)
    lines.append(te(e["bot_icon"], "🤖") + " <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>" + bot_username + "</b>  " + te(e["star"], "🌟"))
    lines.append(divider)
    lines.append("</blockquote>")
    return chr(10).join(lines)

# ── TGID formatter emoji dict ────────────────────────────────────
TG_EMOJIS = {
    "header_divider": "5467641505525016018",
    "tg_title":       "6323058096532024838",   # ✈️ telegram-style
    "query":          "5379999674193172777",   # 🔭
    "tgid":           "5985796637971191405",   # 💳 (id card)
    "username":       "5425124408786197150",   # 👤
    "name":           "6122805733936342496",   # 📛
    "phone":          "6147464060305676048",   # 📱
    "country":        "5379999674193172777",   # 🌍
    "cc":             "6035239240625820254",   # 🔒
    "status_ok":      "6129812419028982717",   # ✅
    "status_err":     "5296491800423340716",   # ❌
    "note":           "5423183595930422680",   # 💡
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
}


def format_tgid_result(result, query, credits_left):
    """Rich blockquote premium-emoji Telegram ID result formatter."""
    import time as _time
    e   = TG_EMOJIS
    div_e   = te(e["header_divider"], "➿")
    divider = (div_e * 10) + "\n"
    start_ts = _time.time()

    try: bot_username = "@" + bot.get_me().username
    except Exception: bot_username = "@YourBot"

    def _f(val):
        v = str(val).strip() if val else ""
        return v if v and v.lower() not in ("n/a", "none", "null", "—", "") else "—"

    def _err(msg_text):
        return (
            divider
            + f"{te(e['tg_title'], '✈️')}  <b>𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>  {te(e['tg_title'], '✈️')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'],      '🔭')} <b>Qᴜᴇʀʏ</b>  : <code>{query}</code>\n"
            + f"{te(e['status_err'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    if result.get("status") != "success":
        return _err("NO DATA FOUND")

    q            = _f(result.get("query", query))
    tgid         = _f(result.get("tgid"))
    username     = _f(result.get("username"))
    name         = _f(result.get("name"))
    phone        = _f(result.get("phone"))
    number       = _f(result.get("number", phone))   # local part
    country      = _f(result.get("country"))
    cc           = _f(result.get("country_code"))
    note         = _f(result.get("note"))
    query_type   = "🆔 User ID" if str(q).isdigit() else "👤 Username"
    elapsed      = round(_time.time() - start_ts, 2)

    txt = (
        divider
        + f"{te(e['tg_title'], '✈️')}  <b>𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>  {te(e['tg_title'], '✈️')}\n"
        + divider
        + f"\n<blockquote>"
        + f"{te(e['query'],     '🔭')} <b>Qᴜᴇʀʏ</b>       : <code>{q}</code>\n"
        + f"{te(e['status_ok'],'✅')} <b>Sᴛᴀᴛᴜꜱ</b>      : ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ✅\n"
        + f"{te(e['query'],     '🔍')} <b>Tʏᴩᴇ</b>        : {query_type}\n"
        + (f"{te(e['note'],     '💡')} <b>Nᴏᴛᴇ</b>        : {note}\n" if note != "—" else "")
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + (f"{te(e['tgid'],    '💳')} <b>TG ID</b>        : <code>{tgid}</code>\n"     if tgid     != "—" else "")
        + (f"{te(e['username'],'👤')} <b>Uꜱᴇʀɴᴀᴍᴇ</b>   : @{username}\n"              if username != "—" else "")
        + (f"{te(e['name'],    '📛')} <b>Nᴀᴍᴇ</b>        : {name}\n"                   if name     != "—" else "")
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + (f"{te(e['phone'],   '📱')} <b>Pʜᴏɴᴇ</b>       : <code>{phone}</code>\n"     if phone    != "—" else "")
        + (f"{te(e['phone'],   '📱')} <b>Lᴏᴄᴀʟ Nᴜᴍ</b>  : <code>{number}</code>\n"    if number not in ("—", phone) else "")
        + (f"{te(e['cc'],      '🔒')} <b>Cᴄ</b>          : {cc}\n"                      if cc       != "—" else "")
        + (f"{te(e['country'], '🌍')} <b>Cᴏᴜɴᴛʀʏ</b>    : {country}\n"                 if country  != "—" else "")
        + f"</blockquote>\n\n"
        + divider
        + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
        + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
        + divider
        + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
        + divider
    )
    return txt

# ── UPI formatter emoji dict ──────────────────────────────────────
UPI_EMOJIS = {
    "header_divider": "5467641505525016018",
    "upi_title":      "5985796637971191405",   # 💳
    "query":          "5379999674193172777",   # 🔭
    "name":           "5425124408786197150",   # 👤
    "bank":           "6305370152145787193",   # 🏦
    "upi":            "5985796637971191405",   # 💳
    "app":            "6122805733936342496",   # 📱
    "valid":          "6129812419028982717",   # ✅
    "invalid":        "5296491800423340716",   # ❌
    "type":           "5197269100878907942",   # ✍️
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
}


def format_upi_result(result, query, credits_left):
    """
    Rich blockquote UPI formatter.
    Response fields: upi_id, name, username, handle, valid,
                     account_type, bank, bank_id, app, handle_bank
    """
    import time as _time
    e = UPI_EMOJIS
    div_e = te(e["header_divider"], "➿")
    divider = (div_e * 10) + "\n"
    start_ts = _time.time()

    try: bot_username = "@" + bot.get_me().username
    except Exception: bot_username = "@YourBot"

    def _err(msg_text):
        return [(
            divider
            + f"{te(e['upi_title'], '💳')}  <b>𝗨𝗣𝗜 𝗜𝗗 𝗜𝗡𝗙𝗢</b>  {te(e['upi_title'], '💳')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'],   '🔭')} <b>Qᴜᴇʀʏ</b>  : <code>{query}</code>\n"
            + f"{te(e['invalid'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        , None)]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    def _f(val):
        v = str(val).strip() if val is not None else ""
        return v if v and v.lower() not in ("n/a", "none", "null", "—", "") else "—"

    upi_id    = _f(result.get("upi_id", query))
    name      = _f(result.get("name"))
    username  = _f(result.get("username"))
    handle    = _f(result.get("handle"))
    valid     = result.get("valid", False)
    acc_type  = _f(result.get("account_type"))
    bank      = _f(result.get("bank"))
    app       = _f(result.get("app"))
    hbank     = _f(result.get("handle_bank"))
    valid_str = f"{te(e['valid'], '✅')} ᴠᴀʟɪᴅ" if valid else f"{te(e['invalid'], '❌')} ɪɴᴠᴀʟɪᴅ"
    elapsed   = round(_time.time() - start_ts, 2)

    txt = (
        divider
        + f"{te(e['upi_title'], '💳')}  <b>𝗨𝗣𝗜 𝗜𝗗 𝗜𝗡𝗙𝗢</b>  {te(e['upi_title'], '💳')}\n"
        + divider
        + f"\n<blockquote>"
        + f"{te(e['query'],  '🔭')} <b>Qᴜᴇʀʏ</b>       : <code>{query}</code>\n"
        + f"{te(e['valid'],  '✅')} <b>Sᴛᴀᴛᴜꜱ</b>      : {valid_str}\n"
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + f"{te(e['upi'],   '💳')} <b>UPI ID</b>       : <code>{upi_id}</code>\n"
        + f"{te(e['name'],  '👤')} <b>Nᴀᴍᴇ</b>        : {name}\n"
        + (f"{te(e['name'], '👤')} <b>Uꜱᴇʀɴᴀᴍᴇ</b>   : {username}\n" if username != "—" else "")
        + (f"{te(e['upi'],  '💳')} <b>Hᴀɴᴅʟᴇ</b>      : @{handle}\n" if handle != "—" else "")
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + f"{te(e['bank'],  '🏦')} <b>Bᴀɴᴋ</b>        : {bank}\n"
        + (f"{te(e['bank'], '🏦')} <b>Hᴀɴᴅʟᴇ Bᴀɴᴋ</b>: {hbank}\n" if hbank not in ("—", bank) else "")
        + (f"{te(e['type'], '✍️')} <b>Aᴄᴄ Tʏᴩᴇ</b>   : {acc_type}\n" if acc_type != "—" else "")
        + (f"{te(e['app'],  '📱')} <b>Aᴩᴩ</b>         : {app}\n" if app != "—" else "")
        + f"</blockquote>\n\n"
        + divider
        + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
        + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
        + divider
        + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
        + divider
    )
    return [(txt, None)]


def format_api_result(result, title, query, credits_left):
    if isinstance(result, dict) and result.get('status') == 'error' and result.get('message') == 'NO DATA FOUND':
        msg = f"""
<blockquote>{title}</blockquote>

<blockquote>Query: {query}
❌ NO DATA FOUND</blockquote>

💰 Credits Left: {credits_left}
"""
        return msg

    SKIP_KEYS = {'status', 'channel', 'by', 'owner', 'credit', 'message', 'success', 'latency',
                 'response_metadata', 'makeData', 'modelData', 'variant', 'less_info', 'masked_name',
                 'developer', 'contact', 'tag', 'key_expiry'}

    DISPLAY_NAMES = {
        'rc_number': 'RC Number', 'registration_number': 'Registration Number',
        'fit_up_to': 'Fitness Valid Upto', 'registration_date': 'Registration Date',
        'owner_name': 'Owner Name', 'father_name': 'Father Name',
        'present_address': 'Present Address', 'mobile_number': 'Mobile Number',
        'vehicle_category': 'Vehicle Category', 'fuel_type': 'Fuel Type', 'color': 'Color',
        'insurance_company': 'Insurance Company', 'manufacturing_date': 'Manufacturing Date',
        'registered_at': 'Registered RTO', 'tax_upto': 'Tax Valid Upto',
        'user_id': 'User ID', 'username': 'Username', 'first_name': 'Name',
        'phone': 'Phone Number', 'email': 'Email', 'address': 'Address',
        'city': 'City', 'state': 'State', 'country': 'Country',
        'pincode': 'Pincode', 'pan': 'PAN Number', 'aadhar': 'Aadhar Number',
        'upi': 'UPI ID', 'ifsc': 'IFSC Code', 'bank': 'Bank Name',
        'branch': 'Branch', 'instagram': 'Instagram ID', 'followers': 'Followers',
        'following': 'Following', 'posts': 'Posts', 'bio': 'Bio',
        'ip': 'IP Address', 'location': 'Location', 'isp': 'ISP',
        'organization': 'Organization', 'asn': 'ASN'
    }

    all_data = []

    def extract_data(data, parent_key=""):
        if isinstance(data, dict):
            for key, value in data.items():
                if key in SKIP_KEYS:
                    continue
                display_key = DISPLAY_NAMES.get(key, key.replace('_', ' ').title())
                if parent_key:
                    display_key = f"{parent_key} {display_key}"
                if isinstance(value, dict):
                    extract_data(value, display_key)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            extract_data(item, display_key)
                        elif item and str(item).strip() and str(item).lower() not in ['none', 'null', '', 'n/a', 'na']:
                            all_data.append((display_key, str(item)))
                else:
                    if value and str(value).strip() and str(value).lower() not in ['none', 'null', '', 'n/a', 'na']:
                        all_data.append((display_key, str(value)))
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, dict):
                    extract_data(item, f"Record {idx + 1}")
                elif item and str(item).strip() and str(item).lower() not in ['none', 'null', '', 'n/a', 'na']:
                    all_data.append((f"Item {idx + 1}", str(item)))

    extract_data(result)
    seen = set()
    unique_data = []
    for key, value in all_data:
        if key not in seen:
            seen.add(key)
            unique_data.append((key, value))

    if unique_data:
        formatted = ""
        for key, value in unique_data:
            emoji = get_emoji_for_key(key)
            formatted += f"{emoji} {key}: {value}\n"
    else:
        formatted = "📋 No detailed data available"

    msg = f"""
<blockquote>{title}</blockquote>

<blockquote>Query: {query}
{formatted}</blockquote>

💰 Credits Left: {credits_left}
"""
    return msg

def format_number(n):
    try:
        n = int(n)
        if n >= 1000000:
            return f"{n/1000000:.1f}M"
        elif n >= 1000:
            return f"{n/1000:.1f}K"
        return str(n)
    except:
        return str(n)

def format_rc_result(result, query, credits_left):
    e = NUM_EMOJIS
    divider = build_num_divider()

    if isinstance(result, dict) and result.get('status') == 'error':
        lines = []
        lines.append(divider)
        lines.append(te(e['phone_title'], '📞') + "  <b>𝗥𝗖 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>  " + te(e['phone_title'], '📞'))
        lines.append(divider)
        lines.append("")
        lines.append("<blockquote>")
        lines.append(te(e['query'], '🔭') + " <b>Qᴜᴇʀʏ</b>  : <code>" + str(query) + "</code>")
        lines.append(te(e['status_ok'], '❌') + " " + str(result.get('message', 'NO DATA FOUND')))
        lines.append("</blockquote>")
        lines.append("")
        lines.append(divider)
        lines.append(te(e['credits'], '🧱') + " <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: " + str(credits_left))
        lines.append(divider)
        return [chr(10).join(lines)]

    if result.get('success') == True and result.get('vehicle_data'):
        vd = result.get('vehicle_data', {})
        rto = vd.get('rtoData', {})

        lines1 = []
        lines1.append(divider)
        lines1.append(te(e['phone_title'], '📞') + "  <b>𝗥𝗖 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>  " + te(e['phone_title'], '📞'))
        lines1.append(divider)
        lines1.append("")
        lines1.append("<blockquote>")
        lines1.append(te(e['number'], '📲') + " <b>Vᴇʜɪᴄʟᴇ</b>    : <code>" + str(result.get('vehicle_number', query)) + "</code>")
        lines1.append(te(e['id_card'], '💳') + " <b>Eɴɢɪɴᴇ</b>     : <code>" + str(result.get('engine_number', 'N/A')) + "</code>")
        lines1.append(te(e['id_card'], '💳') + " <b>Cʜᴀꜱꜱɪꜱ</b>    : <code>" + str(result.get('chassis_number', 'N/A')) + "</code>")
        lines1.append(te(e['alt_num'], '📞') + " <b>Mᴏʙɪʟᴇ</b>     : <code>" + str(result.get('mobile_number', 'N/A')) + "</code>")
        lines1.append("</blockquote>")
        lines1.append("")
        lines1.append("<blockquote>")
        lines1.append(te(e['name'], '👤') + " <b>Oᴡɴᴇʀ</b>      : " + str(vd.get('owner', 'N/A')))
        lines1.append(te(e['father'], '👥') + " <b>Fᴀᴛʜᴇʀ</b>     : " + str(vd.get('ownerFatherName', 'N/A')))
        lines1.append(te(e['address'], '🏡') + " <b>Aᴅᴅʀᴇꜱꜱ</b>   : " + str(vd.get('presentAddress', 'N/A')))
        lines1.append(te(e['circle'], '🔎') + " <b>Pɪɴᴄᴏᴅᴇ</b>    : " + str(vd.get('pincode', 'N/A')))
        lines1.append("</blockquote>")
        lines1.append("")
        lines1.append("<blockquote>")
        lines1.append(te(e['name'], '🚗') + " <b>Vᴇʜɪᴄʟᴇ</b>    : " + str(vd.get('vehicle', 'N/A')))
        lines1.append(te(e['name'], '🏭') + " <b>Mᴀɴᴜꜰᴀᴄᴛᴜʀᴇʀ</b> : " + str(vd.get('manufacturer', 'N/A')))
        lines1.append(te(e['name'], '⚙️') + " <b>Vᴀʀɪᴀɴᴛ</b>    : " + str(vd.get('variant', 'N/A')))
        lines1.append(te(e['name'], '⛽') + " <b>Fᴜᴇʟ</b>       : " + str(vd.get('fuelType', 'N/A')))
        lines1.append(te(e['name'], '📅') + " <b>Rᴇɢ Dᴀᴛᴇ</b>   : " + str(vd.get('regDate', 'N/A')))
        lines1.append(te(e['name'], '📅') + " <b>Mꜰɢ Yᴇᴀʀ</b>   : " + str(vd.get('manufacturerYear', 'N/A')))
        lines1.append("</blockquote>")
        lines1.append(divider)

        lines2 = []
        lines2.append(divider)
        lines2.append(te(e['phone_title'], '📞') + "  <b>𝗥𝗖 𝗗𝗘𝗧𝗔𝗜𝗟𝗦 (𝗖𝗼𝗻𝘁𝗶𝗻𝘂𝗲𝗱)</b>  " + te(e['phone_title'], '📞'))
        lines2.append(divider)
        lines2.append("")
        lines2.append("<blockquote>")
        lines2.append(te(e['name'], '🏛️') + " <b>RTO</b>        : " + str(rto.get('rtoName', 'N/A')))
        lines2.append(te(e['name'], '📍') + " <b>Sᴛᴀᴛᴇ</b>      : " + str(rto.get('statename', 'N/A')))
        lines2.append("</blockquote>")
        lines2.append("")
        lines2.append("<blockquote>")
        lines2.append(te(e['name'], '🏦') + " <b>Iɴꜱᴜʀᴀɴᴄᴇ</b>  : " + str(vd.get('insuranceCompanyName', 'N/A')))
        lines2.append(te(e['id_card'], '📋') + " <b>Pᴏʟɪᴄʏ</b>     : <code>" + str(vd.get('insurancePolicyNumber', 'N/A')) + "</code>")
        lines2.append(te(e['name'], '📅') + " <b>Vᴀʟɪᴅ Uᴘᴛᴏ</b> : " + str(vd.get('insuranceUpto', 'N/A')))
        lines2.append("</blockquote>")
        lines2.append("")
        lines2.append("<blockquote>")
        lines2.append(te(e['id_card'], '✅') + " <b>PUCC</b>       : <code>" + str(vd.get('puccNumber', 'N/A')) + "</code>")
        lines2.append(te(e['name'], '📅') + " <b>PUCC Vᴀʟɪᴅ</b> : " + str(vd.get('puccValidUpto', 'N/A')))
        lines2.append("</blockquote>")
        lines2.append("")
        lines2.append(divider)
        lines2.append(te(e['credits'], '🧱') + " <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: " + str(credits_left))
        lines2.append(divider)
        return [chr(10).join(lines1), chr(10).join(lines2)]

    return [format_api_result(result, "📋 RC DETAILS", query, credits_left)]

def auto_delete_post(chat_id, message_id, delay=120):
    def delete():
        time.sleep(delay)
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
    threading.Thread(target=delete, daemon=True).start()

def send_all_posts(chat_id, username, posts):
    if not posts:
        send_with_premium(chat_id, "❌ No posts found!")
        return

    info_text = f"📸 Sending {len(posts)} posts for @{username}... | ⏳ Please wait... | ⚠️ Posts will auto-delete in 2 minutes | ✅ Profile info will remain"
    info_msg = send_with_premium(chat_id, info_text)
    auto_delete_post(chat_id, info_msg.message_id, 30)

    for i, post in enumerate(posts, 1):
        image_url = post.get('img') or post.get('thumbnail')

        if image_url:
            caption = f"""
<b>📸 POST #{i}</b>

❤️ <b>Likes:</b> {format_number(post.get('likes', 0))}
💬 <b>Comments:</b> {format_number(post.get('comments', 0))}
📅 <b>Date:</b> {post.get('taken_at', 'Unknown')[:10] if post.get('taken_at') else 'Unknown'}

🔗 <a href="https://instagram.com/p/{post.get('code')}">View on Instagram</a>

⏰ <i>This post will auto-delete in 2 minutes</i>
"""
            try:
                cap = replace_with_premium_emojis(caption)
                sent_msg = bot.send_photo(chat_id, image_url, caption=cap, parse_mode='HTML')
                auto_delete_post(chat_id, sent_msg.message_id)
                time.sleep(0.5)
            except:
                post_text = f"🖼️ <a href='{image_url}'>Post #{i} - Click to view</a> | {caption}"
                sent_msg = send_with_premium(chat_id, post_text)
                auto_delete_post(chat_id, sent_msg.message_id)
                time.sleep(0.3)
        else:
            sent_msg = send_with_premium(chat_id, f"❌ Post #{i} - No image available")
            auto_delete_post(chat_id, sent_msg.message_id, 30)

    complete_text = f"✅ All {len(posts)} posts sent successfully for @{username}! | ⚠️ Posts will be deleted after 2 minutes | ✅ Profile info will remain in chat"
    complete_msg = send_with_premium(chat_id, complete_text)
    auto_delete_post(chat_id, complete_msg.message_id, 60)

def format_insta_result_v2(result, username, credits_left):
    e = NUM_EMOJIS
    divider = build_num_divider()

    if result.get('success') == False or result.get('status') == 'error':
        lines = []
        lines.append(divider)
        lines.append(te(e['phone_title'], '📞') + "  <b>𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗢𝗦𝗜𝗡𝗧</b>  " + te(e['phone_title'], '📞'))
        lines.append(divider)
        lines.append("")
        lines.append("<blockquote>")
        lines.append(te(e['status_ok'], '❌') + " " + str(result.get('message', 'NO DATA FOUND')))
        lines.append("</blockquote>")
        lines.append("")
        lines.append(divider)
        lines.append(te(e['credits'], '🧱') + " <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: " + str(credits_left))
        lines.append(divider)
        return chr(10).join(lines), None, []

    profile = result.get('profile', {})
    recent_posts = result.get('recent_posts', [])
    osint_data = result.get('osint', {})

    if not profile and result.get('username'):
        profile = result
    if not recent_posts:
        recent_posts = result.get('posts', result.get('media', []))
    if not osint_data:
        osint_data = result.get('osint_data', result.get('records', {}))

    username_display = profile.get('username') or username
    full_name = profile.get('name') or 'N/A'
    user_id_insta = profile.get('id') or 'N/A'
    bio = profile.get('bio') or 'No bio'
    followers = profile.get('followers') or 0
    following = profile.get('following') or 0
    posts_count = profile.get('posts') or 0
    is_verified = '✅' if profile.get('verified') else '❌'
    is_private = '🔒' if profile.get('private') else '🌍'
    profile_pic = profile.get('pic') or ''
    external_url = profile.get('external_url', '')
    business = '✅' if profile.get('is_business', False) else '❌'
    category = profile.get('category_name', 'N/A')

    lines = []
    lines.append(divider)
    lines.append(te(e['phone_title'], '📞') + "  <b>𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗢𝗦𝗜𝗡𝗧</b>  " + te(e['phone_title'], '📞'))
    lines.append(divider)
    lines.append("")
    lines.append("<blockquote>")
    lines.append(te(e['name'], '👤') + " <b>Uꜱᴇʀɴᴀᴍᴇ</b>  : @" + str(username_display))
    lines.append(te(e['name'], '📛') + " <b>Nᴀᴍᴇ</b>      : " + str(full_name if full_name else 'Not provided'))
    lines.append(te(e['id_card'], '🆔') + " <b>ID</b>        : " + str(user_id_insta))
    lines.append(te(e['status_ok'], '📝') + " <b>Bɪᴏ</b>       : " + str(bio[:300] if bio else 'No bio'))
    lines.append("")
    lines.append(te(e['records'], '📊') + " <b>𝗦𝗧𝗔𝗧𝗦</b>")
    lines.append(te(e['name'], '👥') + " <b>Fᴏʟʟᴏᴡᴇʀꜱ</b> : " + str(format_number(followers)))
    lines.append(te(e['name'], '➡️') + " <b>Fᴏʟʟᴏᴡɪɴɢ</b> : " + str(format_number(following)))
    lines.append(te(e['name'], '🖼️') + " <b>Pᴏꜱᴛꜱ</b>     : " + str(format_number(posts_count)))
    lines.append(te(e['status_ok'], '✅') + " <b>Vᴇʀɪꜰɪᴇᴅ</b>  : " + str(is_verified))
    lines.append(te(e['status_ok'], '🔒') + " <b>Pʀɪᴠᴀᴛᴇ</b>   : " + str(is_private))
    lines.append(te(e['name'], '🏢') + " <b>Bᴜꜱɪɴᴇꜱꜱ</b>  : " + str(business))
    lines.append(te(e['name'], '📂') + " <b>Cᴀᴛᴇɢᴏʀʏ</b>  : " + str(category))
    lines.append("</blockquote>")

    if external_url:
        lines.append("")
        lines.append("<blockquote>" + te(e['status_ok'], '🔗') + " <b>Exᴛᴇʀɴᴀʟ URL</b>: " + str(external_url) + "</blockquote>")

    if osint_data.get('available') and osint_data.get('records'):
        records = osint_data.get('records', [])
        lines.append("")
        lines.append("<blockquote>" + te(e['status_ok'], '🔍') + " <b>𝗢𝗦𝗜𝗡𝗧 𝗥𝗘𝗖𝗢𝗥𝗗𝗦</b> (" + str(len(records)) + " found)</blockquote>")
        for idx_rec, record in enumerate(records, 1):
            lines.append("")
            lines.append("<blockquote>" + te(e['record_badge'], '📰') + " <b>Rᴇᴄᴏʀᴅ #" + str(idx_rec) + "</b>")
            if record.get('name'): lines.append("   " + te(e['name'], '👤') + " <b>Nᴀᴍᴇ</b>: " + str(record.get('name')))
            if record.get('email'): lines.append("   " + te(e['email'], '📧') + " <b>Eᴍᴀɪʟ</b>: " + str(record.get('email')))
            if record.get('phone'): lines.append("   " + te(e['number'], '📲') + " <b>Pʜᴏɴᴇ</b>: " + str(record.get('phone')))
            if record.get('address'): lines.append("   " + te(e['address'], '🏡') + " <b>Aᴅᴅʀᴇꜱꜱ</b>: " + str(record.get('address')[:200]))
            if record.get('city'): lines.append("   " + te(e['circle'], '🏙️') + " <b>Cɪᴛʏ</b>: " + str(record.get('city')))
            if record.get('state'): lines.append("   " + te(e['circle'], '🗺️') + " <b>Sᴛᴀᴛᴇ</b>: " + str(record.get('state')))
            if record.get('dob'): lines.append("   " + te(e['name'], '🎂') + " <b>DOB</b>: " + str(record.get('dob')))
            if record.get('aadhar'): lines.append("   " + te(e['id_card'], '🆔') + " <b>Aᴀᴅʜᴀʀ</b>: " + str(record.get('aadhar')))
            if record.get('pan'): lines.append("   " + te(e['id_card'], '📄') + " <b>PAN</b>: " + str(record.get('pan')))
            lines.append("</blockquote>")
        lines.append("")
    else:
        lines.append("")
        lines.append("<blockquote>" + te(e['status_ok'], '🔍') + " <b>𝗢𝗦𝗜𝗡𝗧 𝗥𝗘??𝗢𝗥𝗗𝗦</b></blockquote>")
        lines.append("")
        lines.append(te(e['status_ok'], '❌') + " OSINT data not available")
        lines.append("")

    if recent_posts:
        lines.append("")
        lines.append("<blockquote>" + te(e['phone_title'], '📸') + " <b>𝗥𝗘𝗖𝗘𝗡𝗧 𝗣𝗢𝗦𝗧𝗦</b></blockquote>")
        lines.append("")
        lines.append(te(e['records'], '📊') + " <b>Tᴏᴛᴀʟ</b>: " + str(len(recent_posts)) + " posts")
        for idx_post, post in enumerate(recent_posts[:10], 1):
            likes = post.get('likes', post.get('like_count', 0))
            comments = post.get('comments', post.get('comment_count', 0))
            caption_text = post.get('caption', post.get('text', ''))[:50]
            post_line = "   " + te(e['phone_title'], '🖼️') + " <b>Pᴏꜱᴛ #" + str(idx_post) + "</b>: " + te(e['name'], '❤️') + str(format_number(likes)) + " " + te(e['name'], '💬') + str(format_number(comments))
            if caption_text: post_line += " - " + caption_text + "..."
            lines.append(post_line)
        if len(recent_posts) > 10:
            lines.append("   ... and " + str(len(recent_posts) - 10) + " more posts")
        lines.append(te(e['status_ok'], '💡') + " Click VIEW ALL POSTS button below!")
        lines.append("")
    else:
        lines.append("")
        lines.append("<blockquote>" + te(e['phone_title'], '📸') + " <b>𝗥𝗘𝗖𝗘𝗡𝗧 𝗣𝗢𝗦𝗧𝗦</b></blockquote>")
        lines.append("")
        lines.append(te(e['status_ok'], '❌') + " No recent posts found")
        lines.append("")

    lines.append(divider)
    lines.append(te(e['credits'], '🧱') + " <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: " + str(credits_left))
    lines.append(divider)

    return chr(10).join(lines), profile_pic, recent_posts

@bot.message_handler(commands=['tgid'])
def tgid_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id, """❌ Use: /tgid [username or user_id]

📝 Examples:
/tgid BMW_AURA1
/tgid @TATANIUM_ANSH
/tgid 8695946179

🔍 Get Telegram user phone info via username OR user ID
""")
        return

    query = parts[1].strip().replace('@', '')
    if 't.me/' in query:
        query = query.split('t.me/')[-1]

    if not query:
        send_with_premium(chat_id, "❌ Invalid username!")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    working, status_msg = check_api_status('tgid')
    if not working:
        send_with_premium(chat_id, f"""
<blockquote>🔍 TELEGRAM USER INFO</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass

    wait = send_searching_animation(chat_id)
    result = call_tg_api(query)
    wait._stop_event.set()

    formatted_msg = format_tgid_result(result, query, credits_left)
    json_file = save_result_to_json("tgid", query, result)

    if json_file:
        sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
    else:
        sent_msg = send_result(chat_id, formatted_msg, wait)

    if sent_msg:
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['num'])
def num_cmd(message):
    if not is_recent_command(message):
        return

    user_id  = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id,
            "❌ Use: /num [phone_number]\n\n"
            "📝 Examples:\n/num 9876543210\n/num +919876543210\n\n"
            "🔍 Get detailed information about any Indian phone number"
        )
        return

    phone = re.sub(r'[^\d+]', '', parts[1].strip())

    if not validate_phone(phone):
        send_with_premium(chat_id, "❌ Invalid phone number! Use 10-15 digits with optional +")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    working, status_msg = check_api_status('num')
    if not working:
        send_with_premium(chat_id, f"<blockquote>📱 PHONE NUMBER INFO</blockquote>\n\n"
                                    f"<blockquote>⚠️ API IS UNDER MAINTENANCE\n{status_msg or 'Try later'}</blockquote>")
        return

    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass

    wait = send_searching_animation(chat_id)
    result = call_phone_api(phone)
    wait._stop_event.set()

    pages     = format_number_result(result, phone, credits_left)
    json_file = save_result_to_json("num", phone, result)

    NUM_PAGINATION[user_id] = {"pages": pages, "json_file": json_file, "query": phone}

    first_txt, first_markup = pages[0]

    if len(pages) == 1 and json_file:
        if first_markup is None:
            first_markup = InlineKeyboardMarkup()
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        first_markup.add(InlineKeyboardButton("Download JSON", callback_data=f"dljson_{file_id}"))

    try:
        bot.delete_message(chat_id, wait.message_id)
    except Exception:
        pass

    sent_msg = send_with_premium(chat_id, first_txt, reply_markup=first_markup)
    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['adhr'])
def adhr_cmd(message):
    if not is_recent_command(message):
        return

    user_id   = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id,
            "❌ Use: /adhr [AADHAR_NUMBER]\n\n"
            "📝 Examples:\n/adhr 942660008471\n/adhr 393933081942\n\n"
            "🔍 Get detailed Aadhar linked information"
        )
        return

    aadhar = re.sub(r'[^\d]', '', parts[1].strip())

    if not validate_aadhar(aadhar):
        send_with_premium(message.chat.id, "❌ Invalid Aadhar number! Use 12 digits only!")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    working, status_msg = check_api_status('adhr')
    if not working:
        send_with_premium(message.chat.id,
            f"<blockquote>🪪 AADHAR INFO</blockquote>\n\n"
            f"<blockquote>⚠️ API IS UNDER MAINTENANCE\n{status_msg or 'Please try again later'}</blockquote>"
        )
        return

    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass

    wait = send_searching_animation(chat_id)
    result    = call_aadhar_api(aadhar)
    wait._stop_event.set()
    json_file = save_result_to_json("adhr", aadhar, result)
    pages     = format_aadhar_result(result, aadhar, credits_left)

    # store in NUM_PAGINATION so the existing num_page_ callback handles navigation
    NUM_PAGINATION[user_id] = {"pages": pages, "json_file": json_file, "query": aadhar}

    first_txt, first_markup = pages[0]

    if len(pages) == 1 and json_file:
        if first_markup is None:
            first_markup = InlineKeyboardMarkup()
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        first_markup.add(InlineKeyboardButton("Download JSON", callback_data=f"dljson_{file_id}"))

    try:
        bot.delete_message(chat_id, wait.message_id)
    except Exception:
        pass

    sent_msg = send_with_premium(chat_id, first_txt, reply_markup=first_markup)
    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['tguser'])
def tguser_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, """❌ Use: /tguser [username]

📝 Examples:
/tguser BMW_AURA1
/tguser @TATANIUM_ANSH

💡 Get any Telegram user's ID
""")
        return

    username = parts[1].strip()
    username = username.replace('@', '')
    if 't.me/' in username:
        username = username.split('t.me/')[-1]

    if not username:
        send_with_premium(message.chat.id, "❌ Invalid username!")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    bot.send_chat_action(chat_id, 'typing')
    wait = send_searching_animation(chat_id)

    try:
        chat = bot.get_chat(f"@{username}")
        wait._stop_event.set()
        target_user_id = chat.id

        msg = f"""
<blockquote>👤 TELEGRAM USER INFO</blockquote>

<blockquote>🔍 Username: @{username}
🆔 User ID: <code>{target_user_id}</code>
📝 First Name: {chat.first_name or 'N/A'}
📛 Last Name: {chat.last_name or 'N/A'}
👑 Is Bot: {'Yes' if chat.is_bot else 'No'}
📌 Chat Type: {chat.type}</blockquote>

💰 Credits Left: {credits_left}
"""
        json_data = {
            "username": username,
            "user_id": target_user_id,
            "first_name": chat.first_name,
            "last_name": chat.last_name,
            "is_bot": chat.is_bot,
            "chat_type": chat.type
        }
        json_file = save_result_to_json("tguser", username, json_data)

        if json_file:
            sent_msg = send_result_with_json(chat_id, msg, json_file, wait.message_id)
        else:
            sent_msg = send_result(chat_id, msg, wait)

        if sent_msg:
            send_random_media(chat_id, sent_msg.message_id)

    except Exception as e:
        error_msg_text = str(e)
        if "user not found" in error_msg_text.lower():
            error_msg_text = "User not found! Make sure the username exists."

        msg = f"""
<blockquote>👤 TELEGRAM USER INFO</blockquote>

<blockquote>🔍 Username: @{username}
❌ {error_msg_text}</blockquote>

💰 Credits Left: {credits_left}
"""
        sent_msg = send_result(chat_id, msg, wait)
        if sent_msg:
            send_random_media(chat_id, sent_msg.message_id)

# ══════════════════════════════════════════════════════════════════
# SNAPCHAT OSINT — call + format
# ══════════════════════════════════════════════════════════════════

def call_snap_api(username):
    """
    API: PASTE_YOUR_SNAP_INFO_API_HERE
    Response: { "success": true, "data": {
        "username", "exists", "is_verified", "has_stories",
        "profile_picture_url", "snapcode_url", "profile_url",
        "description", "story_count", "profile_completeness_score" } }
    """
    username = username.strip().lstrip('@').replace(' ', '_')
    try:
        url = f"PASTE_API_HERE/api/snap?username={username}"   # ← API OF SNAP INFO
        data = make_api_request(url, timeout=15)
        if isinstance(data, dict):
            if not data.get("success"):
                return {"status": "error", "message": data.get("message", "User not found")}
            inner = data.get("data", data)
            if not inner.get("exists"):
                return {"status": "error", "message": "Snapchat user not found"}
            inner.pop("by", None); inner.pop("owner", None)
            return {"status": "success", "data": inner}
        return {"status": "error", "message": "NO DATA FOUND"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


SNAP_EMOJIS = {
    "header_divider": "5467641505525016018",
    "snap_title":     "6235459831302460476",   # 📸 (ghost-ish)
    "username":       "6122805733936342496",   # 👤
    "verified":       "6129812419028982717",   # ✅
    "status":         "6035239240625820254",   # 🔒
    "story":          "5301276827782755360",   # 📱
    "link":           "6305370152145787193",   # ❔
    "score":          "5197269100878907942",   # ✍️
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
}


def format_snap_result(result, username, credits_left):
    """Format /snap Snapchat result with premium emoji, blockquote."""
    import time as _time
    e = SNAP_EMOJIS
    div_e = te(e["header_divider"], "➿")
    divider = (div_e * 10) + "\n"
    start_ts = _time.time()

    try: bot_username = "@" + bot.get_me().username
    except Exception: bot_username = "@YourBot"

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['snap_title'], '👻')}  <b>𝗦𝗡𝗔𝗣𝗖𝗛𝗔𝗧 𝗜𝗡𝗙𝗢</b>  {te(e['snap_title'], '👻')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['username'], '👤')} <b>Qᴜᴇʀʏ</b>   : <code>@{username}</code>\n"
            + f"{te(e['verified'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return (txt, "")

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    d = result.get("data", {})
    uname      = d.get("username", username)
    is_ver     = d.get("is_verified", False)
    has_story  = d.get("has_stories", False)
    story_cnt  = d.get("story_count", 0)
    score      = d.get("profile_completeness_score", "N/A")
    profile_url= d.get("profile_url", f"https://www.snapchat.com/add/{uname}")
    snapcode   = d.get("snapcode_url", "")
    pic_url    = d.get("profile_picture_url", d.get("direct_url", ""))
    desc       = d.get("description", "")

    ver_badge  = f"✅ ᴠᴇʀɪꜰɪᴇᴅ" if is_ver else "❌ ɴᴏᴛ ᴠᴇʀɪꜰɪᴇᴅ"
    story_txt  = f"✅ ({story_cnt} ꜱᴛᴏʀɪᴇꜱ)" if has_story else "❌ ɴᴏ ꜱᴛᴏʀɪᴇꜱ"
    elapsed    = round(_time.time() - start_ts, 2)

    txt = (
        divider
        + f"{te(e['snap_title'], '👻')}  <b>𝗦𝗡𝗔𝗣𝗖𝗛𝗔𝗧 𝗜𝗡𝗙𝗢</b>  {te(e['snap_title'], '👻')}\n"
        + divider
        + f"\n<blockquote>"
        + f"{te(e['username'], '👤')} <b>Uꜱᴇʀɴᴀᴍᴇ</b>  : @{uname}\n"
        + f"{te(e['verified'], '✅')} <b>Vᴇʀɪꜰɪᴇᴅ</b>   : {ver_badge}\n"
        + f"{te(e['story'],    '📱')} <b>Sᴛᴏʀɪᴇꜱ</b>    : {story_txt}\n"
        + f"{te(e['score'],    '✍️')} <b>Pʀᴏꜰɪʟᴇ %</b>  : {score}%\n"
        + (f"{te(e['link'],   '❔')} <b>Bɪᴏ</b>        : {desc[:200]}\n" if desc else "")
        + f"</blockquote>\n\n"
        + f"<blockquote>"
        + f"{te(e['link'],    '🔗')} <b>Pʀᴏꜰɪʟᴇ</b>    : <a href='{profile_url}'>ᴏᴩᴇɴ ꜱɴᴀᴩᴄʜᴀᴛ</a>\n"
        + (f"{te(e['link'],   '🔗')} <b>Sɴᴀᴩᴄᴏᴅᴇ</b>   : <a href='{snapcode}'>ꜱᴄᴀɴ ᴄᴏᴅᴇ</a>\n" if snapcode else "")
        + (f"{te(e['link'],   '🖼️')} <b>Pɪᴄ</b>         : <a href='{pic_url}'>ᴠɪᴇᴡ ᴩʜᴏᴛᴏ</a>\n" if pic_url else "")
        + f"</blockquote>\n\n"
        + divider
        + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
        + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
        + divider
        + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
        + divider
    )
    return txt, pic_url


# ══════════════════════════════════════════════════════════════════
# FAMILY RESULT FORMATTER — rich blockquote, pagination
# ══════════════════════════════════════════════════════════════════

FAM_EMOJIS = {
    "header_divider": "5467641505525016018",
    "fam_title":      "5386435923204382258",   # 👨‍👩‍👧‍👦
    "id_card":        "5985796637971191405",   # 💳
    "address":        "4958485609464202497",   # 🏡
    "name":           "5425124408786197150",   # 👤
    "relation":       "6129700535130922338",   # 👥
    "scheme":         "5197269100878907942",   # ✍️
    "state":          "5379999674193172777",   # 🔭
    "speed":          "6203911118964924015",   # 🚀
    "credits":        "5326049357332513437",   # 🧱
    "bot_icon":       "5355051922862653659",   # 🤖
    "star":           "6118385795976930086",   # 🌟
    "status_ok":      "6129812419028982717",   # ✅
    "query":          "5379999674193172777",   # 🔭
    "record_badge":   "5249231689695115145",   # 📰
}


def format_family_result(result, query, credits_left):
    """
    Format /family response with premium emoji + blockquote.
    Returns list of (text, markup) tuples for pagination.
    New API: members[], ration_card, scheme, state, district, address, total_members.
    Old API: generic fallback.
    """
    import time as _time
    e = FAM_EMOJIS
    div_e = te(e["header_divider"], "➿")
    divider = (div_e * 10) + "\n"
    start_ts = _time.time()

    try: bot_username = "@" + bot.get_me().username
    except Exception: bot_username = "@YourBot"

    def _f(val):
        v = str(val).strip() if val is not None else ""
        return v if v and v.lower() not in ("n/a", "none", "null", "—", "", "to be captured", "rc0000") else "—"

    def _err(msg_text):
        txt = (
            divider
            + f"{te(e['fam_title'], '👨‍👩‍👧‍👦')}  <b>𝗙𝗔𝗠𝗜𝗟𝗬 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>  {te(e['fam_title'], '👨‍👩‍👧‍👦')}\n"
            + divider
            + f"\n<blockquote>"
            + f"{te(e['query'], '🔭')} <b>Qᴜᴇʀʏ</b>   : <code>{query}</code>\n"
            + f"{te(e['status_ok'], '❌')} {msg_text}"
            + f"</blockquote>\n\n"
            + divider
            + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
            + divider
        )
        return [(txt, None)]

    if not result or result.get("status") == "error":
        return _err(result.get("message", "NO DATA FOUND") if result else "NO DATA FOUND")

    members      = result.get("members", [])
    total_m      = result.get("total_members", len(members))
    ration_card  = _f(result.get("ration_card"))
    scheme       = _f(result.get("scheme"))
    state        = _f(result.get("state"))
    district     = _f(result.get("district"))
    address      = _f(result.get("address"))
    rc_num       = _f(result.get("rc"))
    fps_id       = _f(result.get("fps_id"))
    aadhar_m     = _f(result.get("aadhaar_masked", result.get("aadhaar", query)))
    scheme_name  = _f(result.get("scheme"))
    onorc        = _f(result.get("allowed_onorc"))
    elapsed      = round(_time.time() - start_ts, 2)

    # ── Header info block ──────────────────────────────────────
    header = (
        divider
        + f"{te(e['fam_title'], '👨‍👩‍👧‍👦')}  <b>𝗙𝗔𝗠𝗜𝗟𝗬 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>  {te(e['fam_title'], '👨‍👩‍👧‍👦')}\n"
        + divider
        + f"\n<blockquote>"
        + f"{te(e['query'],   '🔭')} <b>Aᴀᴅʜᴀʀ</b>      : <code>{aadhar_m}</code>\n"
        + f"{te(e['status_ok'],'✅')} <b>Sᴛᴀᴛᴜꜱ</b>      : ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ✅\n"
        + (f"{te(e['id_card'],'💳')} <b>Rᴀᴛɪᴏɴ Cᴀʀᴅ</b>: <code>{ration_card}</code>\n" if ration_card != "—" else "")
        + (f"{te(e['scheme'],'✍️')} <b>Sᴄʜᴇᴍᴇ</b>       : {scheme_name}\n" if scheme_name != "—" else "")
        + (f"{te(e['state'], '🔭')} <b>Sᴛᴀᴛᴇ</b>        : {state}\n" if state != "—" else "")
        + (f"{te(e['state'], '🔭')} <b>Dɪꜱᴛʀɪᴄᴛ</b>     : {district}\n" if district != "—" else "")
        + (f"{te(e['address'],'🏡')} <b>Aᴅᴅʀᴇꜱꜱ</b>     : {address}\n" if address != "—" else "")
        + (f"{te(e['id_card'],'💳')} <b>FPS ID</b>       : <code>{fps_id}</code>\n" if fps_id != "—" else "")
        + (f"{te(e['scheme'],'✍️')} <b>ONORC</b>        : {onorc}\n" if onorc not in ("—","N/A") else "")
        + f"{te(e['record_badge'],'📰')} <b>Mᴇᴍʙᴇʀꜱ</b>    : {total_m}\n"
        + f"</blockquote>\n\n"
    )

    footer = (
        divider
        + f"{te(e['speed'],   '🚀')} <b>Sᴩᴇᴇᴅ</b>: {elapsed}s\n"
        + f"{te(e['credits'], '🧱')} <b>Cʀᴇᴅɪᴛꜱ Lᴇꜰᴛ</b>: {credits_left}\n"
        + divider
        + f"{te(e['bot_icon'],'🤖')} <b>Pᴏᴡᴇʀᴇᴅ Bʏ</b>: <b>{bot_username}</b>  {te(e['star'],'🌟')}\n"
        + divider
    )

    if not members:
        # No member data — use generic format_api_result style
        txt = header + footer
        return [(txt, None)]

    MEMBERS_PER_PAGE = 5
    pages_data = [members[i:i+MEMBERS_PER_PAGE] for i in range(0, len(members), MEMBERS_PER_PAGE)]
    total_pages = len(pages_data)
    out = []

    for page_idx, page_members in enumerate(pages_data):
        page_num  = page_idx + 1
        mem_start = page_idx * MEMBERS_PER_PAGE + 1

        body = ""
        for m_idx, m in enumerate(page_members):
            if not isinstance(m, dict):
                continue
            mem_name = _f(m.get("name"))
            mem_rel  = _f(m.get("relation"))
            mem_id   = _f(m.get("member_id"))
            has_uid  = "✅" if m.get("uid") == "Yes" else "❌"
            abs_num  = mem_start + m_idx
            body += (
                f"<blockquote>"
                + f"{te(e['record_badge'],'📰')} <b>Mᴇᴍʙᴇʀ {abs_num}/{total_m}</b>\n"
                + f"{te(e['name'],  '👤')} <b>Nᴀᴍᴇ</b>     : {mem_name}\n"
                + f"{te(e['relation'],'👥')} <b>Rᴇʟᴀᴛɪᴏɴ</b>: {mem_rel}\n"
                + (f"{te(e['id_card'],'💳')} <b>Mᴇᴍʙᴇʀ ID</b>: <code>{mem_id}</code>\n" if mem_id != "—" else "")
                + f"{te(e['status_ok'],'✅')} <b>Uɪᴅ</b>      : {has_uid}\n"
                + f"</blockquote>\n\n"
            )

        txt = header + body + footer

        markup = None
        if total_pages > 1:
            markup = InlineKeyboardMarkup(row_width=3)
            btns = []
            if page_num > 1:
                btns.append(create_primary_button(
                    "◀ Prev",
                    callback_data=f"num_page_{page_num - 1}_{query}",
                    icon_custom_emoji_id="5246794802560774143"
                ))
            btns.append(create_success_button(
                f"{page_num}/{total_pages}",
                callback_data="num_page_noop",
                icon_custom_emoji_id="5422537279400868395"
            ))
            if page_num < total_pages:
                btns.append(create_danger_button(
                    "Next ▶",
                    callback_data=f"num_page_{page_num + 1}_{query}",
                    icon_custom_emoji_id="6192763368139397161"
                ))
            markup.row(*btns)

        out.append((txt, markup))

    return out if out else _err("NO MEMBERS FOUND")


@bot.message_handler(commands=['leak'])
def leak_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id,
            "❌ Use: /leak [phone_number]\n\n"
            "📝 Examples:\n/leak 9876543210\n/leak +919876543210\n\n"
            "🔍 Search leaked database by phone number"
        )
        return

    phone = re.sub(r'[^\d+]', '', parts[1].strip())

    if not validate_phone(phone):
        send_with_premium(chat_id, "❌ Invalid phone number! Use 10-15 digits with optional +")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    working, status_msg = check_api_status('leak')
    if not working:
        send_with_premium(chat_id,
            f"<blockquote>🔍 LEAK DB LOOKUP</blockquote>\n\n"
            f"<blockquote>⚠️ API IS UNDER MAINTENANCE\n{status_msg or 'Please try again later'}</blockquote>"
        )
        return

    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass

    wait = send_searching_animation(chat_id)
    result    = call_leak_api(phone)
    wait._stop_event.set()

    pages     = format_leak_result(result, phone, credits_left)
    json_file = save_result_to_json("leak", phone, result)
    total_pages = len(pages)

    NUM_PAGINATION[user_id] = {"pages": pages, "json_file": json_file, "query": phone}

    first_txt, _ = pages[0]

    first_markup = InlineKeyboardMarkup(row_width=3)
    if total_pages > 1:
        btns = []
        btns.append(create_success_button(
            f"1/{total_pages}",
            callback_data="noop",
            icon_custom_emoji_id="5422537279400868395"
        ))
        btns.append(create_danger_button(
            "Next ▶",
            callback_data=f"leak_page_1_{user_id}",
            icon_custom_emoji_id="6192763368139397161"
        ))
        first_markup.row(*btns)
    elif json_file:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        first_markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))

    try:
        bot.delete_message(chat_id, wait.message_id)
    except Exception:
        pass

    markup_to_send = first_markup if first_markup.keyboard else None
    sent_msg = send_with_premium(chat_id, first_txt, reply_markup=markup_to_send)
    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)

# ---------- Contact-verification gate (drop-in) ---------------
# Matches the interface expected by send_contact_verification_prompt.
CAPTCHA_EMOJI = {
    # All IDs pulled from existing maps already in this file — no new IDs added.
    "robot":    "5355051922862653659",   # 🤖  NUM_EMOJIS["bot_icon"]
    "shield":   "6300604035527346062",   # 🛡  VECH_EMOJIS["ins_status"]
    "phone":    "6147464060305676048",   # 📱  EMOJI_TO_PREMIUM["📱"]
    "check":    "6129812419028982717",   # ✅  NUM_EMOJIS["status_ok"]
    "cross":    "5296491800423340716",   # ❌  EMOJI_TO_PREMIUM["❌"]
    "warning":  "4956420859771225351",   # ⚠️  EMOJI_TO_PREMIUM["⚠"]
    "spark":    "5359588280205605798",   # ⚡  VECH_EMOJIS["fuel"]
    "party":    "4956560549287560231",   # 🎉  EMOJI_TO_PREMIUM["🎉"]
    "calendar": "5348227245599105972",   # 📅  INSTA_EMOJIS["joined"]
}


def _cv_pe(name, fallback):
    """Premium emoji tag, single-quote safe for HTML."""
    eid = CAPTCHA_EMOJI.get(name)
    if not eid:
        return fallback
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def _cv_delete(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as exc:
        if not any(w in str(exc).lower() for w in
                   ("not found", "message to delete", "can't be deleted", "forbidden")):
            print(f"[CV_DELETE] {chat_id}/{message_id}: {exc}")


def _cv_delete_many(chat_id, message_ids):
    unique = []
    for mid in (message_ids or []):
        if mid and mid not in unique:
            unique.append(mid)
    if not unique:
        return
    try:
        bot.delete_messages(chat_id, unique)
        return
    except Exception:
        pass
    for mid in unique:
        _cv_delete(chat_id, mid)


def _cv_prompt_text():
    return (
        "<blockquote>"
        f"{_cv_pe('robot', '🤖')} <b>ᴠᴇʀɪꜰʏ ʏᴏᴜʀ ᴄᴏɴᴛᴀᴄᴛ</b>\n\n"
        f"{_cv_pe('shield', '🛡️')} Bot continue karne ke liye neeche button se "
        "<b>apna khud ka Telegram contact</b> share karo.\n\n"
        f"{_cv_pe('phone', '📱')} Sirf aapka contact verification ke liye use hoga."
        "</blockquote>"
    )


def send_contact_verification_prompt(uid, ref=None):
    """Show the verification prompt and track message_id for cleanup."""
    if ref:
        _save_pending_ref(uid, ref)

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(
        _KB(
            "📱 ᴛᴀᴘ ᴛᴏ ꜱʜᴀʀᴇ ᴄᴏɴᴛᴀᴄᴛ",
            style="success",
            icon_custom_emoji_id=CAPTCHA_EMOJI["phone"],
            request_contact=True,
        )
    )
    try:
        sent = send_with_premium(uid, _cv_prompt_text(), reply_markup=keyboard)
        if sent and getattr(sent, "message_id", None):
            _LAST_CAPCHA_MESSAGE[uid] = sent.message_id
        return sent
    except Exception as exc:
        print(f"[CV_PROMPT] {exc}")
        return None


@bot.message_handler(content_types=["contact"])
def h_contact_verify(msg):
    """Accept user's own Telegram contact — verify and clean old gate cards."""
    try:
        if msg.chat.type != "private" or not msg.contact:
            return

        uid = msg.from_user.id
        contact_uid = getattr(msg.contact, "user_id", None)

        # Reject forwarded / someone-else's contact
        if contact_uid and contact_uid != uid:
            bot.reply_to(
                msg,
                (
                    "<blockquote>"
                    f"{_cv_pe('cross', '❌')} <b>Invalid contact</b>\n"
                    "Please apna khud ka Telegram contact share karo."
                    "</blockquote>"
                ),
                parse_mode="HTML",
            )
            return

        phone = (getattr(msg.contact, "phone_number", None) or "").strip()
        if not phone:
            bot.reply_to(
                msg,
                (
                    "<blockquote>"
                    f"{_cv_pe('warning', '⚠️')} Contact number nahi mila. "
                    "Dobara button tap karke share karo."
                    "</blockquote>"
                ),
                parse_mode="HTML",
            )
            return

        # Persist verification — single clean save, upsert=True inside
        try:
            _mark_contact_verified(uid, phone)
        except Exception as exc:
            print(f"[CV_SAVE] {exc}")

        # Remove reply keyboard silently
        try:
            clr = bot.send_message(
                uid, "\u2060",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            _cv_delete(uid, getattr(clr, "message_id", None))
        except Exception as exc:
            print(f"[CV_KB_REMOVE] {exc}")

        # Restore pending command or referral
        pending = _PENDING_REF.pop(uid, None)
        if isinstance(pending, int):
            # It was a referral id saved by start_cmd gate
            msg.text = f"/start ref_{pending}"
            try:
                start_cmd(msg)
            except Exception as exc:
                print(f"[CV_RESTART_START] {exc}")
        elif isinstance(pending, str) and pending.startswith("/") and not pending.startswith("/start"):
            # It was a real command they tried — re-dispatch it
            msg.text = pending
            try:
                bot.process_new_messages([msg])
            except Exception as exc:
                print(f"[CV_RESTART_CMD] {exc}")
                # Fallback: just open start
                msg.text = "/start"
                try: start_cmd(msg)
                except Exception: pass
        else:
            # Default: open start
            msg.text = "/start"
            try:
                start_cmd(msg)
            except Exception as exc:
                print(f"[CV_RESTART] {exc}")

        # Cleanup old gate messages
        cleanup_ids = []
        old_fj  = _LAST_FJ_MESSAGE.pop(uid, None)
        old_cap = _LAST_CAPCHA_MESSAGE.pop(uid, None)
        if old_fj:
            cleanup_ids.append(old_fj)
        if old_cap:
            cleanup_ids.append(old_cap)
        cleanup_ids.append(getattr(msg, "message_id", None))
        _cv_delete_many(uid, cleanup_ids)

    except Exception as exc:
        print(f"[CONTACT_VERIFY] {exc}")

@bot.message_handler(commands=['start'])
def start_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(chat_id, error_msg)
        return

    username   = message.from_user.username or "N/A"
    first_name = message.from_user.first_name or "User"

    # ── STEP 1: Force-join check (channels must be joined first) ──
    if not enforce_force_join(message):
        return

    # ── STEP 2: Captcha gate — skip for admins / bot owner ───────
    if not is_admin(user_id) and not _is_contact_verified(user_id):
        # Save referral so it survives the contact gate redirect
        ref_val = extract_referral(message.text)
        _save_pending_ref(user_id, ref_val)
        send_contact_verification_prompt(user_id)
        return   # stop here — h_contact_verify will call start_cmd after verify

    # ── STEP 3: Already verified → proceed to normal welcome ──────

    referred = extract_referral(message.text)
    if not get_user(user_id):
        create_user(user_id, username, first_name, referred)
    credits = get_credits(user_id)

    bot_username = bot.get_me().username

    _s  = lambda eid, fb: te(eid, fb)

    # ── exact IDs from uploaded emoji list ──
    FIRE1  = "5370596264205366968"
    FIRE2  = "5372804697734260925"
    FIRE3  = "5372972326012857961"
    FIRE4  = "5370635893868606590"
    FIRE5  = "5370660697304741172"
    FIRE6  = "5372880070115341091"
    FIRE7  = "5370754155793102519"
    STAR1  = "5339247212012528642"
    STAR2  = "5339511094803195372"
    IDICO  = "5888781182249738113"
    BADGE  = "6219727185708582935"
    DIAM   = "6048770006810893912"
    PHONE  = "5318765591014678496"
    PLANE  = "5246931953751443616"
    PHONE2 = "5285238101344544669"
    GROUP  = "5424746868275944727"
    CARD1  = "5985796637971191405"
    CARD2  = "4967518033061872209"
    STAR3  = "5307517685846785869"
    CAR    = "6276097566316695791"
    FLAG   = "5807750375033278838"
    ATM    = "4967738760021148319"
    ROCKET = "5233279094531045380"
    PIN    = "5992344826715050001"
    LAPTOP = "5465637267626271756"
    FLAG2  = "6210718384431041299"
    FIRE8  = "6048398234441750217"
    SMILEY = "5008416661569536895"
    FAMILY = "5386435923204382258"
    SHRUG  = "5298773244101281960"
    PEN    = "6172477863938233258"

    # 8-fire divider row
    DIV = (
        _s(FIRE1,"🔥") + _s(FIRE2,"🔥") + _s(FIRE3,"🔥") + _s(FIRE3,"🔥") +
        _s(FIRE4,"🔥") + _s(FIRE5,"🔥") + _s(FIRE6,"🔥") + _s(FIRE7,"🔥") + "\n"
    )
    # star row
    STARS = _s(STAR1,"🌟") + " <b>WELCOME TO OSINT BOT</b> " + _s(STAR2,"🌟") + "\n"

    # ── start_cmd ──────────────────────────────────────────────
    msg = (
        f"<blockquote>"
        f"{DIV}"
        f"{STARS}"
        f"{DIV}"
        f"\n"
        f"{_s(SMILEY,'😀')} <b>Hᴇʏ !! {first_name}</b>\n"
        f"{_s(IDICO,'🆔')} <b>ID</b>: <code>{user_id}</code>\n"
        f"{_s(BADGE,'⚜️')} <b>Cʀᴇᴅɪᴛꜱ</b>: <b>{credits}</b>\n\n"
        f"{DIV}"
        f"{_s(DIAM,'💎')} <b>OSINT COMMANDS:</b>\n\n"
        f"{_s(PHONE,'☎️')} /num <code>9876543210</code> - Indian Phone\n"
        f"{_s(PLANE,'✈️')} /tgid <code>username</code> - Telegram info (Phone lookup)\n"
        f"{_s(PHONE2,'📞')} /num2 <code>9876543210</code> - Phone number info (Alternate API)\n"
        f"{_s(GROUP,'👥')} /name <code>abhiraaj</code> - Name to details\n"
        f"{_s(CARD1,'💳')} /pan <code>AXDPR2606K</code> - PAN Card info\n"
        f"{_s(CARD2,'💳')} /adhr <code>393933081942</code> - Aadhar info\n"
        f"{_s(STAR3,'🤩')} /vech <code>HR26DA1337</code> - Vehicle info\n"
        f"{_s(CAR,'🚘')} /vechrc <code>HR26EV0001</code> - RC info\n"
        f"{_s(FLAG,'🇮🇳')} /upi <code>id@bank</code> - UPI ID info\n"
        f"{_s(ATM,'🏧')} /ifsc <code>SBIN0001234</code> - Bank info\n"
        f"{_s(ROCKET,'🚀')} /ip <code>8.8.8.8</code> - IP info\n"
        f"{_s(PIN,'📍')} /pin <code>110001</code> - Pincode info\n"
        f"{_s(LAPTOP,'💻')} /git <code>username</code> - GitHub info\n"
        f"{_s(FLAG2,'🇮🇳')} /bgmi <code>5121439477</code> - BGMI info\n"
        f"{_s(FIRE8,'🔥')} /ff <code>123456789</code> - FreeFire info\n"
        f"{_s(SMILEY,'😀')} /insta <code>username</code> - Instagram info\n"
        f"{_s(FAMILY,'👨‍👩‍👧‍👦')} /family <code>701984830542</code> - Family Details (Ration Card)\n"
        f"{_s(SHRUG,'🤷‍♂️')} /leak <code>9876543210</code> - Leaked DB Lookup\n"
        f"{_s(SMILEY,'😀')} /snap <code>username</code> - Snapchat Profile Info\n"
        f"{_s(SHRUG,'🤷‍♂️')} /ai <code>prompt</code> - AI Image Generation\n\n"
        f"{_s(PEN,'✍️')} <b>OTHER COMMANDS:</b>\n\n"
        f"/redeem  - Rᴇᴅᴇᴇᴍ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/profile - Yᴏᴜʀ ᴩʀᴏꜰɪʟᴇ\n"
        f"/balance - Cʜᴇᴄᴋ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/refer - Rᴇꜰᴇʀʀᴀʟ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/help - Aʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ\n\n"
        f"{_s(STAR1,'🌟')} <b>Oᴡɴᴇʀ</b>: {OWNER_USERNAME}\n"
        f"</blockquote>"
    )


    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        create_primary_button(" Help", callback_data="help", icon_custom_emoji_id="5298773244101281960"),
        create_primary_button(" Profile", callback_data="profile", icon_custom_emoji_id="5424746868275944727"),
        create_primary_button(" Balance", callback_data="balance", icon_custom_emoji_id="6048770006810893912"),
        create_success_button(" Redeem", callback_data="redeem", icon_custom_emoji_id="6219727185708582935"),
    )
    markup.add(create_success_button("➕ ADD BOT TO GROUP", url=f"https://t.me/{bot_username}?startgroup=start"))

    if is_admin(user_id):
        markup.add(create_danger_button(" Admin Panel", callback_data="admin_panel", icon_custom_emoji_id="5339247212012528642"))

    try:
        sent = send_with_premium(chat_id, msg, parse_mode='HTML', reply_markup=markup)
        schedule_deletion(sent.message_id, chat_id)
    except Exception as e:
        print(f"Error in start command: {e}")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    credits = get_credits(user_id)
    bot_username = bot.get_me().username

    _h  = lambda eid, fb: te(eid, fb)

    # ── exact IDs from uploaded emoji list ──
    H_FIRE1  = "5370596264205366968"
    H_FIRE2  = "5372804697734260925"
    H_FIRE3  = "5372972326012857961"
    H_FIRE4  = "5370635893868606590"
    H_FIRE5  = "5370660697304741172"
    H_FIRE6  = "5372880070115341091"
    H_FIRE7  = "5370754155793102519"
    H_STAR1  = "5339247212012528642"
    H_STAR2  = "5339511094803195372"
    H_IDICO  = "5888781182249738113"
    H_BADGE  = "6219727185708582935"
    H_DIAM   = "6048770006810893912"
    H_PHONE  = "5318765591014678496"
    H_PLANE  = "5246931953751443616"
    H_PHONE2 = "5285238101344544669"
    H_GROUP  = "5424746868275944727"
    H_CARD1  = "5985796637971191405"
    H_CARD2  = "4967518033061872209"
    H_STAR3  = "5307517685846785869"
    H_CAR    = "6276097566316695791"
    H_FLAG   = "5807750375033278838"
    H_ATM    = "4967738760021148319"
    H_ROCKET = "5233279094531045380"
    H_PIN    = "5992344826715050001"
    H_LAPTOP = "5465637267626271756"
    H_FLAG2  = "6210718384431041299"
    H_FIRE8  = "6048398234441750217"
    H_SMILEY = "5008416661569536895"
    H_FAMILY = "5386435923204382258"
    H_SHRUG  = "5298773244101281960"
    H_PEN    = "6172477863938233258"

    HDV = (
        _h(H_FIRE1,"🔥") + _h(H_FIRE2,"🔥") + _h(H_FIRE3,"🔥") + _h(H_FIRE3,"🔥") +
        _h(H_FIRE4,"🔥") + _h(H_FIRE5,"🔥") + _h(H_FIRE6,"🔥") + _h(H_FIRE7,"🔥") + "\n"
    )
    H_STARS = _h(H_STAR1,"🌟") + " <b>WELCOME TO OSINT BOT</b> " + _h(H_STAR2,"🌟") + "\n"

# ── help_cmd ───────────────────────────────────────────────
    msg = (
        f"<blockquote>"
        f"{HDV}"
        f"{H_STARS}"
        f"{HDV}"
        f"\n"
        f"{_h(H_SMILEY,'😀')} <b>Hᴇʏ !!</b>\n"
        f"{_h(H_IDICO,'🆔')} <b>ID</b>: <code>{user_id}</code>\n"
        f"{_h(H_BADGE,'⚜️')} <b>Cʀᴇᴅɪᴛꜱ</b>: <b>{credits}</b>\n\n"
        f"{HDV}"
        f"{_h(H_DIAM,'💎')} <b>OSINT COMMANDS:</b>\n\n"
        f"{_h(H_PHONE,'☎️')} /num <code>9876543210</code> - Indian Phone\n"
        f"{_h(H_PLANE,'✈️')} /tgid <code>username</code> - Telegram info (Phone lookup)\n"
        f"{_h(H_PHONE2,'📞')} /num2 <code>9876543210</code> - Phone number info (Alternate API)\n"
        f"{_h(H_GROUP,'👥')} /name <code>abhiraaj</code> - Name to details\n"
        f"{_h(H_CARD1,'💳')} /pan <code>AXDPR2606K</code> - PAN Card info\n"
        f"{_h(H_CARD2,'💳')} /adhr <code>393933081942</code> - Aadhar info\n"
        f"{_h(H_STAR3,'🤩')} /vech <code>HR26DA1337</code> - Vehicle info\n"
        f"{_h(H_CAR,'🚘')} /vechrc <code>HR26EV0001</code> - RC info\n"
        f"{_h(H_FLAG,'🇮🇳')} /upi <code>id@bank</code> - UPI ID info\n"
        f"{_h(H_ATM,'🏧')} /ifsc <code>SBIN0001234</code> - Bank info\n"
        f"{_h(H_ROCKET,'🚀')} /ip <code>8.8.8.8</code> - IP info\n"
        f"{_h(H_PIN,'📍')} /pin <code>110001</code> - Pincode info\n"
        f"{_h(H_LAPTOP,'💻')} /git <code>username</code> - GitHub info\n"
        f"{_h(H_FLAG2,'🇮🇳')} /bgmi <code>5121439477</code> - BGMI info\n"
        f"{_h(H_FIRE8,'🔥')} /ff <code>123456789</code> - FreeFire info\n"
        f"{_h(H_SMILEY,'😀')} /insta <code>username</code> - Instagram info\n"
        f"{_h(H_FAMILY,'👨‍👩‍👧‍👦')} /family <code>701984830542</code> - Family Details (Ration Card)\n"
        f"{_h(H_SHRUG,'🤷‍♂️')} /ai <code>prompt</code> - AI Image Generation\n\n"
        f"{HDV}"
        f"{_h(H_PEN,'✍️')} <b>OTHER COMMANDS:</b>\n\n"
        f"/redeem  - Rᴇᴅᴇᴇᴍ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/profile - Yᴏᴜʀ ᴩʀᴏꜰɪʟᴇ\n"
        f"/balance - Cʜᴇᴄᴋ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/refer - Rᴇꜰᴇʀʀᴀʟ ᴄʀᴇᴅɪᴛꜱ\n"
        f"/help - Aʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ\n\n"
        f"{_h(H_STAR1,'🌟')} <b>Oᴡɴᴇʀ</b>: {OWNER_USERNAME}\n"
        f"</blockquote>"
    )

    markup = InlineKeyboardMarkup()
    markup.add(create_success_button("➕ ADD BOT TO GROUP", url=f"https://t.me/{bot_username}?startgroup=start"))

    try:
        sent = send_with_premium(chat_id, msg, parse_mode='HTML', reply_markup=markup)
        schedule_deletion(sent.message_id, chat_id)
    except Exception as e:
        print(f"Error in help command: {e}")

@bot.message_handler(commands=['profile'])
def profile_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    user_data = get_user(user_id)
    if not user_data:
        send_with_premium(chat_id, "❌ Please /start first!")
        return
    msg = f"""
👤 YOUR PROFILE

🆔 ID: <code>{user_data[0]}</code>
👤 Username: @{user_data[1] or 'N/A'}
📝 Name: {user_data[2] or 'User'}
💰 Credits: {user_data[3] or 0}
👥 Referrals: {user_data[6] or 0}
⚡ Commands Used: {user_data[9] or 0}
👑 Admin: {'✅' if user_data[4] == 1 or user_id == ADMIN_ID else '❌'}
📅 Joined: {str(user_data[7])[:10] if user_data[7] else 'N/A'}
"""
    try:
        sent = send_with_premium(chat_id, msg, parse_mode='HTML')
        schedule_deletion(sent.message_id, chat_id)
    except Exception as e:
        print(f"Error in profile command: {e}")

@bot.message_handler(commands=['balance'])
def balance_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    credits = get_credits(user_id)
    msg = f"""
💰 CREDIT BALANCE

💳 Credits: {credits}
"""
    try:
        sent = send_with_premium(chat_id, msg)
        schedule_deletion(sent.message_id, chat_id)
    except Exception as e:
        print(f"Error in balance command: {e}")

@bot.message_handler(commands=['refer'])
def refer_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    bot_user = bot.get_me().username
    link = f"https://t.me/{bot_user}?start=ref_{user_id}"
    msg = f"""
🔗 YOUR REFERRAL LINK

📨 <code>{link}</code>

📋 Rules:
• Friend gets: {REFERRAL_BONUS_NEW} credits
• You gets: {REFERRAL_BONUS_REFERRER} credits
"""
    try:
        sent = send_with_premium(chat_id, msg, parse_mode='HTML')
        schedule_deletion(sent.message_id, chat_id)
    except Exception as e:
        print(f"Error in refer command: {e}")

@bot.message_handler(commands=['redeem'])
def redeem_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split()
    if len(parts) < 2:
        msg = """❌ USE: /redeem CODE

💡 EXAMPLE: /redeem ABC123XYZ

⚠️ NOTE: Each code can be used only ONCE per user!

🎫 Get codes from admin: @flirts_x"""
        send_with_premium(chat_id, msg)
        return

    code = parts[1].strip().upper()
    wait_msg = send_with_premium(chat_id, f"🔍 Verifying code: {code}...")
    success, result = redeem_credits(user_id, code)

    if success:
        new_credits = get_credits(user_id)
        match = re.search(r'(\d+)', result)
        amount = match.group(1) if match else "10"

        final_msg = f"""✅ REDEMPTION SUCCESSFUL! ✅

🎫 Code: {code}
💰 Credits Added: +{amount}
💳 New Balance: {new_credits} credits

✨ Use /balance to check your credits
✨ Use /profile to see your stats"""

        try:
            edit_with_premium(chat_id, wait_msg.message_id, final_msg)
        except:
            bot.delete_message(chat_id, wait_msg.message_id)
            send_with_premium(chat_id, final_msg)
    else:
        final_msg = f"""❌ REDEMPTION FAILED ❌

🎫 Code: {code}
❌ Error: {result}

💡 Possible reasons:
• Invalid or expired code
• Code already used by you
• Code max usage reached
• Code doesn't exist

📞 Contact admin: {OWNER_USERNAME}"""

        try:
            edit_with_premium(chat_id, wait_msg.message_id, final_msg)
        except:
            bot.delete_message(chat_id, wait_msg.message_id)
            send_with_premium(chat_id, final_msg)

def handle_api_command(message, command_name, api_func, validate_func, title, query_processor=None):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id, f"❌ Use: /{command_name} [query]")
        return

    query = parts[1].strip()
    if query_processor:
        query = query_processor(query)
    if validate_func and not validate_func(query):
        send_with_premium(chat_id, f"❌ Invalid {command_name} format!")
        return

    working, status_msg = check_api_status(command_name)
    if not working:
        send_with_premium(chat_id, f"""
<blockquote>{title}</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass

    wait = send_searching_animation(chat_id)
    result = api_func(query)
    wait._stop_event.set()

    formatted_msg = format_api_result(result, title, query, credits_left)
    json_file = save_result_to_json(command_name, query, result)

    if json_file:
        sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
    else:
        sent_msg = send_result(chat_id, formatted_msg, wait)

    if sent_msg:
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['name'])
def name_cmd(message):
    handle_api_command(message, 'name', call_name_api, None, "👤 NAME SEARCH")

@bot.message_handler(commands=['pan'])
def pan_cmd(message):
    handle_api_command(message, 'pan', call_pan_api, validate_pan, "📄 PAN CARD INFO", lambda x: x.upper())

@bot.message_handler(commands=['vech'])
def vech_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id,
            "❌ Use: /vech [VEHICLE_NUMBER]\n\n"
            "📝 Examples:\n/vech MH12AB1234\n/vech DL01AB1234\n\n"
            "🔍 Get detailed vehicle information"
        )
        return

    vehicle = parts[1].strip().upper().replace(' ', '')
    if not validate_vehicle(vehicle):
        send_with_premium(chat_id, "❌ Invalid vehicle number format!")
        return

    working, status_msg = check_api_status('vech')
    if not working:
        send_with_premium(chat_id, f"""
<blockquote>🚗 VEHICLE INFO</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass

    wait = send_searching_animation(chat_id)
    result = call_vehicle_api(vehicle)
    wait._stop_event.set()

    pages = format_vehicle_result_v2(result, vehicle, credits_left)
    json_file = save_result_to_json("vech", vehicle, result)

    if len(pages) == 1:
        # Single page - send directly with JSON button
        markup = InlineKeyboardMarkup()
        if json_file:
            file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
            JSON_FILES[file_id] = json_file
            markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))
        markup.add(create_default_button("🔙 Back", callback_data="back_to_result", icon_custom_emoji_id="5307517685846785869"))
        try:
            bot.delete_message(chat_id, wait.message_id)
        except:
            pass
        sent = send_with_premium(chat_id, pages[0], reply_markup=markup)
        if sent:
            schedule_deletion(sent.message_id, chat_id)
            send_random_media(chat_id, sent.message_id)
    else:
        # Multi-page with pagination
        send_paginated_vech(chat_id, user_id, pages, json_file, vehicle, wait, command="vech")


@bot.message_handler(commands=['vechrc'])
def vechrc_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id,
            "❌ Use: /vechrc [VEHICLE_NUMBER]\n\n"
            "📝 Examples:\n/vechrc MH12AB1234\n/vechrc DL01AB1234\n\n"
            "🔍 Get detailed RC (Registration Certificate) information"
        )
        return

    vehicle = parts[1].strip().upper().replace(' ', '')
    if not validate_vehicle(vehicle):
        send_with_premium(chat_id, "❌ Invalid vehicle number format!")
        return

    working, status_msg = check_api_status('vechrc')
    if not working:
        send_with_premium(chat_id, f"""
<blockquote>📋 RC DETAILS</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass

    wait = send_searching_animation(chat_id)
    result = call_rc_api(vehicle)
    wait._stop_event.set()

    pages = format_rc_result_v2(result, vehicle, credits_left)
    json_file = save_result_to_json("vechrc", vehicle, result)

    if len(pages) == 1:
        markup = InlineKeyboardMarkup()
        if json_file:
            file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
            JSON_FILES[file_id] = json_file
            markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))
        markup.add(create_default_button("🔙 Back", callback_data="back_to_result", icon_custom_emoji_id="5307517685846785869"))
        try:
            bot.delete_message(chat_id, wait.message_id)
        except:
            pass
        sent = send_with_premium(chat_id, pages[0], reply_markup=markup)
        if sent:
            schedule_deletion(sent.message_id, chat_id)
            send_random_media(chat_id, sent.message_id)
    else:
        send_paginated_vech(chat_id, user_id, pages, json_file, vehicle, wait, command="vechrc")

@bot.message_handler(commands=['ip'])
def ip_cmd(message):
    handle_api_command(message, 'ip', call_ip_api, validate_ip, "🌐 IP ADDRESS INFO")

@bot.message_handler(commands=['pin'])
def pin_cmd(message):
    handle_api_command(message, 'pin', call_pincode_api, validate_pincode, "📮 PINCODE INFO")

@bot.message_handler(commands=['git'])
def git_cmd(message):
    handle_api_command(message, 'git', call_git_api, None, "🐙 GITHUB PROFILE", lambda x: x.replace('@', ''))

@bot.message_handler(commands=['bgmi'])
def bgmi_cmd(message):
    handle_api_command(message, 'bgmi', call_bgmi_api, None, "🎮 BGMI PLAYER INFO")

@bot.message_handler(commands=['ff'])
def ff_cmd(message):
    handle_api_command(message, 'ff', call_ff_api, None, "🔥 FREEFIRE PLAYER INFO")

@bot.message_handler(commands=['upi'])
def upi_cmd(message):
    # custom UPI handler — uses rich format_upi_result
    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    if not enforce_force_join(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id,
            "❌ Use: /upi [upi_id]\n\n📝 Examples:\n/upi elvishrao@okicici\n/upi user@paytm\n\n💳 Get UPI ID details")
        return
    upi_input = parts[1].strip()
    if not validate_upi(upi_input):
        send_with_premium(message.chat.id, "❌ Invalid UPI ID! Format: username@bank")
        return
    working, status_msg = check_api_status('upi')
    if not working:
        send_with_premium(message.chat.id, f"<blockquote>💳 UPI ID INFO</blockquote>\n<blockquote>⚠️ API MAINTENANCE\n{status_msg or 'Try again later'}</blockquote>")
        return
    chat_id = message.chat.id
    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!"); return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"
    try: bot.send_chat_action(chat_id, 'typing')
    except Exception: pass
    wait = send_searching_animation(chat_id)
    result = call_upi_api(upi_input)
    wait._stop_event.set()
    pages = format_upi_result(result, upi_input, credits_left)
    json_file = save_result_to_json("upi", upi_input, result)
    txt, markup = pages[0]
    if markup is None and json_file:
        markup = InlineKeyboardMarkup()
        fid = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[fid] = json_file
        markup.add(create_primary_button("📥 Download JSON", callback_data=f"dljson_{fid}", icon_custom_emoji_id="5465637267626271756"))
    try: bot.delete_message(chat_id, wait.message_id)
    except Exception: pass
    sent_msg = send_with_premium(chat_id, txt, reply_markup=markup)
    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['ifsc'])
def ifsc_cmd(message):
    handle_api_command(message, 'ifsc', call_ifsc_api, validate_ifsc, "🏦 BANK INFO", lambda x: x.upper())

@bot.message_handler(commands=['family'])
def family_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id,
            "❌ Use: /family [AADHAR_NUMBER]\n\n"
            "📝 Examples:\n/family 701984830542\n/family 377226634228\n\n"
            "👨‍👩‍👧‍👦 Get family details (Ration Card, Members) via Aadhar"
        )
        return

    aadhar_raw = re.sub(r'[^\d]', '', parts[1].strip())

    if not validate_aadhar(aadhar_raw):
        send_with_premium(message.chat.id, "❌ Invalid Aadhar number! Use 12 digits only.")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    working, status_msg = check_api_status('family')
    if not working:
        send_with_premium(message.chat.id, f"""
<blockquote>👨‍👩‍👧‍👦 FAMILY DETAILS</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    try: bot.send_chat_action(chat_id, 'typing')
    except Exception: pass

    wait = send_searching_animation(chat_id)
    result = call_family_api(aadhar_raw)
    wait._stop_event.set()
    pages = format_family_result(result, aadhar_raw, credits_left)
    json_file = save_result_to_json("family", aadhar_raw, result)

    NUM_PAGINATION[user_id] = {"pages": pages, "json_file": json_file, "query": aadhar_raw}
    first_txt, first_markup = pages[0]

    if len(pages) == 1 and json_file and first_markup is None:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        first_markup = InlineKeyboardMarkup()
        first_markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))

    try: bot.delete_message(chat_id, wait.message_id)
    except Exception: pass

    sent_msg = send_with_premium(chat_id, first_txt, reply_markup=first_markup)
    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['snap'])
def snap_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(chat_id,
            "❌ Use: /snap [snapchat_username]\n\n"
            "📝 Examples:\n/snap john_doe\n/snap @cooluser\n\n"
            "👻 Get Snapchat profile info"
        )
        return

    username = parts[1].strip().lstrip('@').replace(' ', '_')
    if not username:
        send_with_premium(chat_id, "❌ Invalid username!")
        return

    working, status_msg = check_api_status('snap')
    if not working:
        send_with_premium(chat_id,
            f"<blockquote>👻 SNAPCHAT INFO</blockquote>\n\n"
            f"<blockquote>⚠️ API IS UNDER MAINTENANCE\n{status_msg or 'Please try again later'}</blockquote>"
        )
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(chat_id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    try: bot.send_chat_action(chat_id, 'typing')
    except Exception: pass

    wait = send_searching_animation(chat_id)
    result = call_snap_api(username)
    wait._stop_event.set()

    formatted_msg, pic_url = format_snap_result(result, username, credits_left)
    json_file = save_result_to_json("snap", username, result)

    markup = InlineKeyboardMarkup(row_width=1)
    if json_file:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        markup.add(create_primary_button("📥 Download JSON",
            callback_data=f"dljson_{file_id}",
            icon_custom_emoji_id="5465637267626271756"))
    markup.add(create_default_button("🔙 Back",
        callback_data="back_to_result",
        icon_custom_emoji_id="5307517685846785869"))

    sent_msg = None
    if pic_url and pic_url.startswith("http"):
        try:
            sent_msg = bot.send_photo(chat_id, pic_url,
                caption=formatted_msg, reply_markup=markup, parse_mode='HTML')
            try: bot.delete_message(chat_id, wait.message_id)
            except Exception: pass
        except Exception:
            if json_file:
                sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
            else:
                sent_msg = send_result(chat_id, formatted_msg, wait)
    else:
        if json_file:
            sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
        else:
            sent_msg = send_result(chat_id, formatted_msg, wait)

    if sent_msg:
        schedule_deletion(sent_msg.message_id, chat_id)
        send_random_media(chat_id, sent_msg.message_id)


@bot.message_handler(commands=['ai'])
def ai_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    working, status_msg = check_api_status('ai')
    if not working:
        send_with_premium(message.chat.id, f"""
<blockquote>🎨 AI IMAGE GENERATION</blockquote>

<blockquote>⚠️ API IS UNDER MAINTENANCE/FIXING
{status_msg or 'Please try again later'}</blockquote>
""")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, """
🎨 AI IMAGE GENERATION

❌ Please provide a prompt!

📝 Usage: `/ai [prompt]`

<blockquote>✨ Examples:
/ai beautiful sunset over mountains
/ai cyberpunk city at night
/ai cute cat wearing sunglasses</blockquote>

⚡ Powered by Pollinations AI
""")
        return

    prompt = parts[1].strip()
    if len(prompt) < 3:
        send_with_premium(message.chat.id, "❌ Prompt too short! Please provide at least 3 characters.")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits! Get more via referral or redeem codes.")
            return

    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    bot.send_chat_action(chat_id, 'upload_photo')
    wait = send_searching_animation(chat_id)
    bot.send_chat_action(chat_id, 'upload_photo')

    seed = random.randint(1, 999999)
    encoded_prompt = requests.utils.quote(prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"

    try:
        img_response = requests.get(image_url, timeout=30, stream=True)
        if img_response.status_code == 200:
            content_type = img_response.headers.get('content-type', '')
            if 'image' in content_type:
                temp_file = f"temp_ai_{random.randint(10000, 99999)}.png"
                with open(temp_file, 'wb') as f:
                    for chunk in img_response.iter_content(chunk_size=8192):
                        f.write(chunk)
                with open(temp_file, 'rb') as f:
                    cap_text = f"""
🎨 AI IMAGE GENERATION

🔍 Prompt: {prompt}

💰 Credits Left: {credits_left}
"""
                    caption = replace_with_premium_emojis(cap_text)
                    sent_msg = bot.send_photo(chat_id, f, caption=caption, parse_mode='HTML')
                    send_random_media(chat_id, sent_msg.message_id)
                os.remove(temp_file)
                bot.delete_message(chat_id, wait.message_id)
            else:
                raise Exception(f"Not an image, content-type: {content_type}")
        else:
            raise Exception(f"HTTP {img_response.status_code}")
    except Exception as e:
        msg = f"""
🎨 AI IMAGE GENERATION

🔍 Prompt: {prompt}
🖼️ Click to view: {image_url}

💰 Credits Left: {credits_left}

💡 The image was generated but couldn't be downloaded.
   Click the link above to view it!
"""
        sent_msg = send_result(chat_id, msg, wait)
        if sent_msg:
            send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['num2'])
def num2_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, """❌ Use: /num2 [phone_number]

📝 Examples:
/num2 9876543210
/num2 +919876543210

🔍 Get detailed information about any phone number
""")
        return

    phone = parts[1].strip()
    phone = re.sub(r'[^\d+]', '', phone)

    if not validate_phone(phone):
        send_with_premium(message.chat.id, "❌ Invalid phone number! Use 10-15 digits with optional +")
        return

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits!")
            return
    credits_left = get_credits(user_id) if not is_group_chat(message) else "∞ (Group)"

    bot.send_chat_action(chat_id, 'typing')
    wait = send_searching_animation(chat_id)
    result = call_num2_api(phone)
    wait._stop_event.set()
    formatted_msg = format_api_result(result, "📱 PHONE NUMBER INFO (Alt)", phone, credits_left)
    json_file = save_result_to_json("num2", phone, result)

    if json_file:
        sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
    else:
        sent_msg = send_result(chat_id, formatted_msg, wait)

    if sent_msg:
        send_random_media(chat_id, sent_msg.message_id)

@bot.message_handler(commands=['insta'])
def insta_cmd(message):
    if not is_recent_command(message):
        return

    user_id = message.from_user.id
    error_msg = get_access_error_message(user_id)
    if error_msg:
        send_with_premium(message.chat.id, error_msg)
        return
    chat_id = message.chat.id

    if not enforce_force_join(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /insta [username]\n\nExample: <code>/insta just_sahil_621</code>")
        return

    username = parts[1].strip().replace('@', '')

    if not is_group_chat(message):
        if not deduct_credit(user_id):
            send_with_premium(message.chat.id, "❌ Insufficient credits!")
            return
        credits_left = get_credits(user_id)
    else:
        credits_left = "∞ (Group)"

    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass

    wait = send_searching_animation(chat_id)
    result = call_insta_api(username)
    wait._stop_event.set()

    if result.get('status') == 'error':
        msg = (
            "<blockquote>📸 INSTAGRAM INFO</blockquote>\n\n"
            "<blockquote>❌ Instagram API is currently down. Please try again later.</blockquote>\n\n"
            "💰 Credits Left: " + str(credits_left)
        )
        sent_msg = send_result(chat_id, msg, wait)
        if sent_msg:
            send_random_media(chat_id, sent_msg.message_id)
        return

    formatted_msg = format_insta_result_v3(result, username, credits_left)
    json_file = save_result_to_json("insta", username, result)

    # Extract profile pic URL from API response
    pic_url = ""
    raw_profile = result.get("result", {}).get("profile", {}) if isinstance(result.get("result"), dict) else {}
    if not raw_profile:
        raw_profile = result.get("profile", {})
    pic_url = str(raw_profile.get("profile_pic_url_hd", "") or "")

    markup = InlineKeyboardMarkup(row_width=1)
    if json_file:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        markup.add(create_primary_button("📥 Download JSON", callback_data="dljson_" + file_id, icon_custom_emoji_id="5465637267626271756"))
    markup.add(create_default_button("🔙 Back", callback_data="back_to_result", icon_custom_emoji_id="5307517685846785869"))

    sent_msg = None
    if pic_url and pic_url.startswith("http"):
        try:
            sent_msg = bot.send_photo(chat_id, pic_url, caption=formatted_msg, reply_markup=markup, parse_mode='HTML')
            try:
                bot.delete_message(chat_id, wait.message_id)
            except:
                pass
        except Exception as e:
            print(f"Insta photo send failed: {e}")
            if json_file:
                sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
            else:
                sent_msg = send_result(chat_id, formatted_msg, wait)
    else:
        if json_file:
            sent_msg = send_result_with_json(chat_id, formatted_msg, json_file, wait.message_id)
        else:
            sent_msg = send_result(chat_id, formatted_msg, wait)

    if sent_msg:
        send_random_media(chat_id, sent_msg.message_id)


def admin_panel(message):
    """Full admin panel with stats + all admin action buttons."""
    chat_id = message.chat.id
    stats   = get_bot_stats()

    text = (
        "<blockquote>"
        "<b>\U0001f451 ADMIN PANEL</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f465 <b>Total Users</b>   : <code>{stats.get('total_users', 0)}</code>\n"
        f"\U0001f4c5 <b>New Today</b>     : <code>{stats.get('today_users', 0)}</code>\n"
        f"\U0001f7e2 <b>Active (24h)</b>  : <code>{stats.get('active_users', 0)}</code>\n"
        f"\U0001f4b0 <b>Total Credits</b> : <code>{stats.get('total_credits', 0)}</code>\n"
        f"\u26a1 <b>Commands Used</b> : <code>{stats.get('total_commands', 0)}</code>\n"
        f"\U0001f517 <b>Referrals</b>     : <code>{stats.get('total_referrals', 0)}</code>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "</blockquote>"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        create_primary_button("📊 Stats",        callback_data="admin_stats",         icon_custom_emoji_id="5992344826715050001"),
        create_primary_button("👥 Users",         callback_data="admin_users",         icon_custom_emoji_id="5424746868275944727"),
    )
    markup.add(
        create_success_button("➕ Add Credits",   callback_data="admin_addcredits",   icon_custom_emoji_id="6048770006810893912"),
        create_danger_button( "➖ Remove Credits", callback_data="admin_removecredits", icon_custom_emoji_id="5370596264205366968"),
    )
    markup.add(
        create_success_button("✅ Unblock User",  callback_data="admin_unblock",      icon_custom_emoji_id="5339247212012528642"),
        create_danger_button( "🚫 Block User",    callback_data="admin_block",         icon_custom_emoji_id="5372880070115341091"),
    )
    markup.add(
        create_success_button("👑 Make Admin",    callback_data="admin_makeadmin",    icon_custom_emoji_id="6219727185708582935"),
        create_danger_button( "🗑 Remove Admin",  callback_data="admin_removeadmin",  icon_custom_emoji_id="5370635893868606590"),
    )
    markup.add(
        create_primary_button("📢 Broadcast",     callback_data="admin_broadcast_info", icon_custom_emoji_id="5233279094531045380"),
        create_primary_button("📡 Set BC Channel",callback_data="admin_setbc_info",  icon_custom_emoji_id="5246931953751443616"),
    )
    markup.add(
        create_primary_button("🔧 API Status",    callback_data="admin_api_status",  icon_custom_emoji_id="5465637267626271756"),
        create_primary_button("🖼 Add Media",     callback_data="admin_addmedia",    icon_custom_emoji_id="5008416661569536895"),
    )
    markup.add(
        create_danger_button("🔄 Reload Bot",     callback_data="admin_reload",       icon_custom_emoji_id="5370754155793102519"),
    )

    send_with_premium(chat_id, text, reply_markup=markup)

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    admin_panel(message)

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    stats = get_bot_stats()
    msg = f"""
📊 BOT STATISTICS

👑 Total Users: {stats.get('total_users', 0)}
📅 New Today: {stats.get('today_users', 0)}
🟢 Active (24h): {stats.get('active_users', 0)}
💰 Total Credits: {stats.get('total_credits', 0)}
⚡ Commands Used: {stats.get('total_commands', 0)}
👥 Total Referrals: {stats.get('total_referrals', 0)}
"""
    send_with_premium(message.chat.id, msg)

@bot.message_handler(commands=['users'])
def users_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    try:
        total   = _col_users.count_documents({})
        admins  = _col_users.count_documents({"is_admin": 1})
        blocked = _col_users.count_documents({"is_blocked": 1})
        msg = f"""
👥 USER STATISTICS

📊 Total Users: {total}
👑 Admins: {admins}
🚫 Blocked: {blocked}
✅ Active: {total - blocked}
"""
        send_with_premium(message.chat.id, msg)
    except Exception as e:
        send_with_premium(message.chat.id, f"❌ Error: {e}")

@bot.message_handler(commands=['add'])
def add_credits_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 3:
        send_with_premium(message.chat.id, "❌ Use: /add [user_id] [credits]")
        return
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
        if add_credits(target_id, amount):
            new_balance = get_credits(target_id)
            send_with_premium(message.chat.id, f"✅ Added {amount} credits to {target_id}\n💰 New Balance: {new_balance}")
            try:
                send_with_premium(target_id, f"✅ Admin added {amount} credits!\n💰 New Balance: {new_balance}")
            except:
                pass
        else:
            send_with_premium(message.chat.id, "❌ Failed to add credits!")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID or amount!")

@bot.message_handler(commands=['removecredit'])
def remove_credits_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 3:
        send_with_premium(message.chat.id, "❌ Use: /removecredit [user_id] [credits]")
        return
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
        with db_lock:
            _col_users.update_one({"user_id": target_id}, {"$inc": {"credits": -amount}})
            new_balance = get_credits(target_id)
            send_with_premium(message.chat.id, f"✅ Removed {amount} credits from {target_id}\n💰 New Balance: {new_balance}")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID or amount!")

@bot.message_handler(commands=['gen'])
def gen_code_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 4:
        send_with_premium(message.chat.id, "❌ Use: /gen [credits] [max_uses] [days]\nExample: /gen 50 10 30")
        return
    try:
        credits = int(parts[1])
        max_uses = int(parts[2])
        days = int(parts[3])
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        success, result = create_redeem_code(code, credits, max_uses, expiry_date, user_id)
        send_with_premium(message.chat.id, result)
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid numbers!")

@bot.message_handler(commands=['deletecode'])
def delete_code_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Usage: `/deletecode [code]`\n💡 Use `/allcodes` to see all codes")
        return
    success, result = delete_redeem_code(parts[1].strip().upper(), user_id)
    send_with_premium(message.chat.id, result)

@bot.message_handler(commands=['allcodes'])
def all_codes_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    try:
        docs = list(_col_codes.find({}).sort("_id", -1).limit(30))
        if not docs:
            send_with_premium(message.chat.id, "📋 No redeem codes found!")
            return
        msg = "🎫 REDEEM CODES\n\n"
        for doc in docs:
            status = "✅" if doc.get("is_active", 1) else "❌"
            msg += f"{status} `{doc['code']}`\n   💰 {doc['credits']} credits | Used: {doc.get('used_count',0)}/{doc['max_uses']}\n"
            if doc.get("expiry_date"):
                msg += f"   📅 Expires: {doc['expiry_date'][:10]}\n"
            msg += "\n"
        msg += "\n💡 Each code can be used only ONCE per user!\n💡 Use `/deletecode [code]` to delete a code"
        send_with_premium(message.chat.id, msg, parse_mode='HTML')
    except Exception as e:
        send_with_premium(message.chat.id, f"❌ Error: {e}")

@bot.message_handler(commands=['promote'])
def promote_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /promote [user_id]")
        return
    try:
        target_id = int(parts[1])
        if target_id == ADMIN_ID:
            send_with_premium(message.chat.id, "❌ Cannot promote main owner!")
            return
        try:
            _col_users.update_one({"user_id": target_id}, {"$set": {"is_admin": 1}})
            send_with_premium(message.chat.id, f"✅ User {target_id} is now an admin!")
            try:
                send_with_premium(target_id, "✅ You have been promoted to admin!")
            except:
                pass
        except Exception:
            send_with_premium(message.chat.id, "❌ Failed to promote user!")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID!")

@bot.message_handler(commands=['demote'])
def demote_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /demote [user_id]")
        return
    try:
        target_id = int(parts[1])
        if target_id == ADMIN_ID:
            send_with_premium(message.chat.id, "❌ Cannot demote main owner!")
            return
        try:
            _col_users.update_one({"user_id": target_id}, {"$set": {"is_admin": 0}})
            send_with_premium(message.chat.id, f"✅ User {target_id} is no longer an admin!")
            try:
                send_with_premium(target_id, "❌ You have been demoted from admin!")
            except:
                pass
        except Exception:
            send_with_premium(message.chat.id, "❌ Failed to demote user!")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID!")

@bot.message_handler(commands=['searchuser'])
def search_user_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /searchuser [id or username]")
        return
    query = parts[1].strip()
    try:
        if query.isdigit():
            user = _col_users.find_one({"user_id": int(query)})
        else:
            user = _col_users.find_one({"username": {"$regex": query, "$options": "i"}})
        if not user:
            send_with_premium(message.chat.id, "❌ User not found!")
            return
        msg = f"""
👤 USER DETAILS

🆔 ID: `{user['user_id']}`
👤 Username: @{user.get('username') or 'N/A'}
📝 Name: {user.get('first_name') or 'N/A'}
💰 Credits: {user.get('credits', 0)}
👑 Admin: {'✅' if user.get('is_admin') == 1 else '❌'}
👥 Referrals: {user.get('referral_count', 0)}
⚡ Commands: {user.get('total_commands', 0)}
🚫 Blocked: {'✅' if user.get('is_blocked') == 1 else '❌'}
📅 Joined: {str(user.get('join_date', 'N/A'))[:19]}
"""
        send_with_premium(message.chat.id, msg, parse_mode='HTML')
    except Exception as e:
        send_with_premium(message.chat.id, f"❌ Error: {e}")

@bot.message_handler(commands=['block'])
def block_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /block [user_id]")
        return
    try:
        target_id = int(parts[1])
        if target_id == ADMIN_ID:
            send_with_premium(message.chat.id, "❌ Cannot block owner!")
            return
        if block_user_in_db(target_id):
            send_with_premium(message.chat.id, f"✅ User {target_id} has been blocked!")
            try:
                send_with_premium(target_id, f"🚫 BANNED 🚫\n\n❌ YOU HAVE BEEN BANNED ❌\n\n📞 Contact Admin: {OWNER_USERNAME}")
            except:
                pass
        else:
            send_with_premium(message.chat.id, "❌ Failed to block user!")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID!")

@bot.message_handler(commands=['unblock'])
def unblock_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Use: /unblock [user_id]")
        return
    try:
        target_id = int(parts[1])
        if unblock_user_in_db(target_id):
            send_with_premium(message.chat.id, f"✅ User {target_id} has been unblocked!")
            try:
                send_with_premium(target_id, "✅ You have been unblocked! You can use the bot again.")
            except:
                pass
        else:
            send_with_premium(message.chat.id, "❌ Failed to unblock user!")
    except ValueError:
        send_with_premium(message.chat.id, "❌ Invalid user ID!")

@bot.message_handler(commands=['lock'])
def lock_bot_cmd(message):
    global BOT_LOCKED
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    BOT_LOCKED = True
    send_with_premium(message.chat.id, "🔒 BOT LOCKED 🔒\n\n🔐 BOT IS LOCKED BY ADMIN\n\n⏳ Only admins can use the bot now")

@bot.message_handler(commands=['unlock'])
def unlock_bot_cmd(message):
    global BOT_LOCKED
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    BOT_LOCKED = False
    send_with_premium(message.chat.id, "🔓 BOT UNLOCKED 🔓\n\n✅ BOT IS NOW UNLOCKED\n\n🌟 All users can now use the bot")

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return

    # ── mode detection ──────────────────────────────────────────
    # FORWARD mode  : /broadcast  (reply to any message)
    #   → bot.forward_message() — preserves "Forwarded from X" header
    # TEXT mode     : /broadcast <text>  (no reply)
    #   → send_with_premium() — plain text blast
    # ───────────────────────────────────────────────────────────

    use_forward   = False
    forward_from_chat = None
    forward_msg_id    = None
    broadcast_msg     = None

    if message.reply_to_message:
        replied = message.reply_to_message
        use_forward       = True
        forward_from_chat = replied.chat.id
        forward_msg_id    = replied.message_id
    else:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            send_with_premium(
                message.chat.id,
                "❌ Use:\n"
                "• <b>Reply</b> to any msg + <code>/broadcast</code> — forwards that msg (with Forwarded From tag)\n"
                "• <code>/broadcast &lt;text&gt;</code> — sends plain text blast",
                parse_mode="HTML"
            )
            return
        broadcast_msg = parts[1]

    # ── collect targets ─────────────────────────────────────────
    all_chats = set()
    try:
        for doc in _col_users.find({}, {"user_id": 1}):
            all_chats.add(doc["user_id"])
    except Exception:
        pass

    try:
        for gcid in get_all_group_chats():
            all_chats.add(gcid)
    except:
        pass

    success_count = 0
    fail_count    = 0
    total         = len(all_chats)
    progress_msg  = send_with_premium(
        message.chat.id,
        f"📢 Broadcast started...\n\n📊 Total: {total}\n⏳ Sending..."
    )

    for idx, chat_id in enumerate(all_chats, 1):
        try:
            if use_forward:
                # forward preserves original sender / channel header
                bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=forward_from_chat,
                    message_id=forward_msg_id,
                    disable_notification=True,
                )
            else:
                send_with_premium(chat_id, broadcast_msg, disable_notification=True)
            success_count += 1
        except Exception:
            fail_count += 1

        if idx % 10 == 0:
            try:
                progress_text = f"📢 Broadcast in progress...\n\n📊 Total: {total}\n✅ Sent: {success_count}\n❌ Failed: {fail_count}\n⏳ Progress: {idx}/{total}"
                edit_with_premium(message.chat.id, progress_msg.message_id, progress_text)
            except:
                pass
        time.sleep(0.1)

@bot.message_handler(commands=['broadcastbtn'])
def broadcast_btn_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return

    if not message.reply_to_message:
        send_with_premium(
            message.chat.id,
            "⭐ Reply to the message you want to broadcast.\n\n"
            "Use:\n"
            "<code>/broadcastbtn Button Text | https://example.com | success | EMOJI_ID</code>\n\n"
            "Styles: primary (blue), success (green), danger (red)\n"
            "Style and premium emoji ID are optional.",
            parse_mode="HTML"
        )
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(
            message.chat.id,
            "❌ Provide button details:\n\n"
            "<code>/broadcastbtn Button Text | https://example.com | success | EMOJI_ID</code>",
            parse_mode="HTML"
        )
        return

    # ── parse "Button Text | URL | style | emoji_id" ──
    args = [p.strip() for p in parts[1].split("|")]
    if len(args) < 2:
        send_with_premium(
            message.chat.id,
            "❌ Invalid format. Use:\n\n"
            "<code>/broadcastbtn Button Text | https://example.com | success | EMOJI_ID</code>",
            parse_mode="HTML"
        )
        return

    btn_text = args[0].strip()
    btn_url  = args[1].strip()

    if not btn_text or not btn_url:
        send_with_premium(message.chat.id, "❌ Button text and URL cannot be empty!")
        return
    if not (btn_url.startswith("http://") or btn_url.startswith("https://") or btn_url.startswith("tg://")):
        send_with_premium(message.chat.id, "❌ URL must start with http://, https:// or tg://")
        return

    style = args[2].strip().lower() if len(args) > 2 and args[2].strip() else "primary"
    if style not in ("primary", "success", "danger"):
        style = "primary"

    emoji_id = args[3].strip() if len(args) > 3 and args[3].strip() else None

    btn_kwargs = {"url": btn_url}
    if emoji_id:
        btn_kwargs["icon_custom_emoji_id"] = emoji_id

    if style == "success":
        btn = create_success_button(btn_text, **btn_kwargs)
    elif style == "danger":
        btn = create_danger_button(btn_text, **btn_kwargs)
    else:
        btn = create_primary_button(btn_text, **btn_kwargs)

    markup = InlineKeyboardMarkup()
    markup.add(btn)

    replied            = message.reply_to_message
    forward_from_chat  = replied.chat.id
    forward_msg_id     = replied.message_id

    # ── collect targets ──────────────────────────────────────────
    all_chats = set()
    try:
        for doc in _col_users.find({}, {"user_id": 1}):
            all_chats.add(doc["user_id"])
    except Exception:
        pass
    try:
        for gcid in get_all_group_chats():
            all_chats.add(gcid)
    except Exception:
        pass

    success_count = 0
    fail_count    = 0
    total         = len(all_chats)
    progress_msg  = send_with_premium(
        message.chat.id,
        f"📢 Broadcast (with button) started...\n\n📊 Total: {total}\n⏳ Sending..."
    )

    # try to copy the replied message's content + attach button.
    # copy_message preserves media/text without the "Forwarded from" header,
    # and lets us attach a reply_markup — forward_message cannot attach buttons.
    for idx, chat_id in enumerate(all_chats, 1):
        try:
            bot.copy_message(
                chat_id=chat_id,
                from_chat_id=forward_from_chat,
                message_id=forward_msg_id,
                reply_markup=markup,
                disable_notification=True,
            )
            success_count += 1
        except Exception:
            fail_count += 1

        if idx % 10 == 0:
            try:
                progress_text = (
                    f"📢 Broadcast (with button) in progress...\n\n"
                    f"📊 Total: {total}\n✅ Sent: {success_count}\n❌ Failed: {fail_count}\n"
                    f"⏳ Progress: {idx}/{total}"
                )
                edit_with_premium(message.chat.id, progress_msg.message_id, progress_text)
            except Exception:
                pass
        time.sleep(0.1)

    try:
        final_text = (
            f"✅ <b>Broadcast (with button) Complete!</b>\n\n"
            f"📊 Total: {total}\n✅ Sent: {success_count}\n❌ Failed: {fail_count}"
        )
        edit_with_premium(message.chat.id, progress_msg.message_id, final_text)
    except Exception:
        pass

@bot.message_handler(commands=['uptime'])
def uptime_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    uptime = datetime.now() - BOT_START_TIME
    msg = f"""
🕐 BOT UPTIME

📅 Days: {uptime.days}
⏰ Hours: {uptime.seconds // 3600}
⏱️ Minutes: {(uptime.seconds % 3600) // 60}
⚡ Seconds: {uptime.seconds % 60}
"""
    send_with_premium(message.chat.id, msg)

@bot.message_handler(commands=['commands'])
def commands_stats_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    try:
        top_users = list(_col_users.find(
            {"total_commands": {"$gt": 0}},
            {"user_id": 1, "username": 1, "total_commands": 1}
        ).sort("total_commands", -1).limit(10))
        agg = list(_col_users.aggregate([{"$group": {"_id": None, "total": {"$sum": "$total_commands"}}}]))
        total_commands = agg[0]["total"] if agg else 0
        msg = "📊 COMMAND USAGE STATS\n\n"
        msg += f"📈 Total Commands: {total_commands}\n\n🏆 TOP USERS:\n"
        for i, u in enumerate(top_users, 1):
            name = f"@{u['username']}" if u.get("username") else str(u["user_id"])
            msg += f"{i}. {name}: {u['total_commands']} commands\n"
        send_with_premium(message.chat.id, msg, parse_mode='HTML')
    except Exception as e:
        send_with_premium(message.chat.id, f"❌ Error: {e}")

@bot.message_handler(commands=['apistatus'])
def apistatus_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        send_with_premium(message.chat.id, """❌ Usage: `/apistatus [command] [status] [message]`""", parse_mode='HTML')
        return

    command = parts[1].lower()
    if command not in API_STATUS:
        send_with_premium(message.chat.id, f"❌ Invalid command!")
        return

    if len(parts) >= 3:
        status_parts = parts[2].split(maxsplit=1)
        status = status_parts[0].lower()
        msg = status_parts[1] if len(status_parts) > 1 else None

        if status == 'working':
            set_api_status(command, True, None)
            send_with_premium(message.chat.id, f"✅ API `/{command}` is now marked as WORKING")
        elif status == 'fixing':
            set_api_status(command, False, msg)
            send_with_premium(message.chat.id, f"⚠️ API `/{command}` is now marked as FIXING")

# _resolve_channel_from_message removed — /addc now uses chat_id + link directly


def add_force_join_channel_extended(url, username, admin_id, is_private=False, join_by_request=False):
    """Extended version that also stores private/join_request flags."""
    try:
        _col_channels.insert_one({
            "url": url,
            "username": username,
            "enabled": 1,
            "is_private": int(is_private),
            "join_by_request": int(join_by_request),
            "added_by": admin_id,
            "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        PROMOTION_CHANNELS.append({
            "url": url,
            "username": username,
            "enabled": True,
            "is_private": is_private,
            "join_by_request": join_by_request,
        })
        return True, "Channel added successfully!"
    except DuplicateKeyError:
        return False, "Channel already exists!"
    except Exception as e:
        return False, f"Error: {str(e)}"


# pending addc state: {user_id: step}
ADDC_STATE = {}
ADDC_LINK_CACHE = {}   # {token: {chat_id, link, is_private}} — holds exact admin-provided link

@bot.message_handler(commands=['addc'])
def add_channel_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    ADDC_STATE[user_id] = {"step": "chat_id"}
    send_with_premium(
        message.chat.id,
        "📋 <b>Step 1/2</b> — Send the <b>Chat ID</b>\n\n"
        "Example: <code>-1001234567890</code>\n\n"
        "To get Chat ID, forward a post from that channel to @userinfobot",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: (
    m.from_user and
    m.from_user.id in ADDC_STATE and
    ADDC_STATE[m.from_user.id].get("step") == "chat_id" and
    m.text and not m.text.startswith('/')
))
def addc_step_chatid(message):
    user_id = message.from_user.id
    raw = message.text.strip()
    try:
        chat_id_num = int(raw)
    except ValueError:
        send_with_premium(message.chat.id,
            "❌ Invalid Chat ID. Send a number like <code>-1001234567890</code>",
            parse_mode='HTML')
        return
    ADDC_STATE[user_id]["chat_id"] = chat_id_num
    ADDC_STATE[user_id]["step"]    = "link"
    send_with_premium(
        message.chat.id,
        "🔗 <b>Step 2/2</b> — Send the <b>Channel Link</b>\n\n"
        "Examples:\n"
        "• Public: <code>https://t.me/mychannel</code>\n"
        "• Private: <code>https://t.me/+abcXYZ123</code>",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: (
    m.from_user and
    m.from_user.id in ADDC_STATE and
    ADDC_STATE[m.from_user.id].get("step") == "link" and
    m.text and not m.text.startswith('/')
))
def addc_step_link(message):
    user_id = message.from_user.id
    link    = message.text.strip()
    state   = ADDC_STATE.pop(user_id, {})
    chat_id_num = state.get("chat_id")

    if not chat_id_num:
        send_with_premium(message.chat.id, "❌ Session expired. Run /addc again.")
        return

    if not link.startswith('http') and not link.startswith('t.me') and not link.startswith('@'):
        send_with_premium(message.chat.id, "❌ Invalid link. Send a valid t.me link.")
        return

    # detect private invite link (+hash / joinchat) vs public
    is_private = '+' in link or 'joinchat' in link

    # store the EXACT link the admin gave — this is authoritative, never regenerate it
    token = hashlib.md5(f"{user_id}{chat_id_num}{link}{time.time()}".encode()).hexdigest()[:10]
    ADDC_LINK_CACHE[token] = {
        "chat_id": chat_id_num,
        "link": link,
        "is_private": is_private,
    }

    markup = InlineKeyboardMarkup()
    markup.row(
        create_success_button("🌍 Normal Join",
            callback_data=f"addc_mode_{token}_0"),
        create_primary_button("🔐 Request to Join",
            callback_data=f"addc_mode_{token}_1")
    )
    markup.add(create_danger_button("❌ Cancel", callback_data="addc_skip"))

    send_with_premium(
        message.chat.id,
        f"⚙️ <b>Select Join Mode</b>\n\n"
        f"🔗 <b>Link</b>: <code>{link}</code>\n"
        f"🆔 <b>Chat ID</b>: <code>{chat_id_num}</code>\n\n"
        f"🌍 <b>Normal Join</b> — bot verifies membership\n"
        f"🔐 <b>Request to Join</b> — user sends request, bot lets through instantly",
        reply_markup=markup,
        parse_mode='HTML'
    )




@bot.chat_join_request_handler()
def handle_join_request(update):
    """
    Fires when a user sends a join request to any channel where the bot is admin.
    We record it in JOIN_REQUESTS_SENT so check_subscription passes for request-mode channels.
    Credit is NOT deducted here — that happens when user runs a command.
    """
    try:
        user_id  = update.from_user.id
        chat_id  = update.chat.id
        username = update.chat.username or str(chat_id)

        # record in memory
        if user_id not in JOIN_REQUESTS_SENT:
            JOIN_REQUESTS_SENT[user_id] = set()
        JOIN_REQUESTS_SENT[user_id].add(username)
        JOIN_REQUESTS_SENT[user_id].add(str(chat_id))   # also store numeric id

        print(f"[JOIN_REQUEST] user {user_id} → channel {username} ({chat_id})")
    except Exception as e:
        print(f"[JOIN_REQUEST] error: {e}")

@bot.message_handler(commands=['removec'])
def remove_channel_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(message.chat.id, "❌ Usage: `/removec [channel_url_or_username]`", parse_mode='HTML')
        return
    success, result = remove_force_join_channel(parts[1].strip(), user_id)
    send_with_premium(message.chat.id, result)

@bot.message_handler(commands=['listc'])
def list_channels_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    channels = list_force_join_channels()
    if not channels:
        send_with_premium(message.chat.id, "📋 No force join channels configured!")
        return
    msg = "\n📢 FORCE JOIN CHANNELS\n\n\n"
    for ch in channels:
        ch_id, url, username, enabled, added_by, added_date = ch
        msg += f"ID: `{ch_id}`\n📢 {url}\n👤 @{username}\n📊 {'✅ Active' if enabled else '❌ Disabled'}\n\n"
    send_with_premium(message.chat.id, msg, parse_mode='HTML')

@bot.message_handler(commands=['api'])
def list_apis_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    apis = list_all_apis()
    msg = "\n🔗 API ENDPOINTS\n\n\n"
    for api in apis:
        msg += f"📌 **/{api['command']}**\n   `{api['endpoint'][:80]}...`\n\n"
    send_with_premium(message.chat.id, msg, parse_mode='HTML')

@bot.message_handler(commands=['apic'])
def change_api_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        send_with_premium(message.chat.id, """❌ Usage: `/apic [command] [api_url]`""", parse_mode='HTML')
        return
    command = parts[1].strip().lower().replace('/', '')
    api_url = parts[2].strip()
    success, result = set_api_endpoint(command, api_url, user_id)
    send_with_premium(message.chat.id, result)

@bot.message_handler(commands=['lockf'])
def lock_force_join_cmd(message):
    global FORCE_JOIN_ENABLED
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    FORCE_JOIN_ENABLED = False
    send_with_premium(message.chat.id, "🔓 FORCE JOIN DISABLED 🔓")

@bot.message_handler(commands=['unlockf'])
def unlock_force_join_cmd(message):
    global FORCE_JOIN_ENABLED
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    FORCE_JOIN_ENABLED = True
    send_with_premium(message.chat.id, "🔒 FORCE JOIN ENABLED 🔒")

admin_adding_media = {}

@bot.message_handler(commands=['addvideo'])
def add_video_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    admin_adding_media[user_id] = {"type": "video", "files": []}
    send_with_premium(message.chat.id, "🎬 **BULK VIDEO UPLOAD MODE**\n\nSend me videos and send /done when finished.", parse_mode='HTML')

@bot.message_handler(commands=['addphoto'])
def add_photo_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return
    admin_adding_media[user_id] = {"type": "photo", "files": []}
    send_with_premium(message.chat.id, "🖼️ **BULK PHOTO UPLOAD MODE**\n\nSend me photos and send /done when finished.", parse_mode='HTML')

@bot.message_handler(commands=['done'])
def done_adding_media(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    if user_id not in admin_adding_media:
        send_with_premium(message.chat.id, "❌ No active media session!")
        return
    state = admin_adding_media[user_id]
    files = state["files"]
    media_type = state["type"]
    added = 0
    for idx, file_id in enumerate(files, 1):
        if media_type == "video":
            add_video(file_id, f"Video {idx}", user_id)
        else:
            add_photo(file_id, f"Photo {idx}", user_id)
        added += 1
    del admin_adding_media[user_id]
    send_with_premium(message.chat.id, f"✅ Added {added} {media_type}(s) successfully!")

@bot.message_handler(commands=['cancel'])
def cancel_media_add(message):
    user_id = message.from_user.id
    if user_id in admin_adding_media:
        del admin_adding_media[user_id]
        send_with_premium(message.chat.id, "❌ Cancelled.")

@bot.message_handler(content_types=['video', 'photo'])
def handle_bulk_media(message):
    user_id = message.from_user.id
    if not is_admin(user_id) or user_id not in admin_adding_media:
        return
    state = admin_adding_media[user_id]
    if message.video and state["type"] == "video":
        state["files"].append(message.video.file_id)
        send_with_premium(message.chat.id, f"✅ Video #{len(state['files'])} added!")
    elif message.photo and state["type"] == "photo":
        state["files"].append(message.photo[-1].file_id)
        send_with_premium(message.chat.id, f"✅ Photo #{len(state['files'])} added!")

@bot.message_handler(commands=['delvideo'])
def del_video_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    videos = get_all_videos()
    if not videos:
        send_with_premium(message.chat.id, "📋 No videos found!")
        return
    markup = InlineKeyboardMarkup(row_width=1)
    for vid in videos[:20]:
        markup.add(InlineKeyboardButton(f"🗑️ {vid[2][:30]} (ID: {vid[0]})", callback_data=f"delvid_{vid[0]}"))
    send_with_premium(message.chat.id, "🎬 Select video to delete:", reply_markup=markup)

@bot.message_handler(commands=['delphoto'])
def del_photo_cmd(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    photos = get_all_photos()
    if not photos:
        send_with_premium(message.chat.id, "📋 No photos found!")
        return
    markup = InlineKeyboardMarkup(row_width=1)
    for photo in photos[:20]:
        markup.add(InlineKeyboardButton(f"🗑️ {photo[2][:30]} (ID: {photo[0]})", callback_data=f"delphoto_{photo[0]}"))
    send_with_premium(message.chat.id, "🖼️ Select photo to delete:", reply_markup=markup)

@bot.message_handler(commands=['listvideos'])
def list_videos_cmd(message):
    if not is_admin(message.from_user.id):
        return
    videos = get_all_videos()
    send_with_premium(message.chat.id, f"🎬 Total Videos: {len(videos)}")

@bot.message_handler(commands=['listphotos'])
def list_photos_cmd(message):
    if not is_admin(message.from_user.id):
        return
    photos = get_all_photos()
    send_with_premium(message.chat.id, f"🖼️ Total Photos: {len(photos)}")

@bot.message_handler(commands=['setmode'])
def set_mode_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return
    success, result = set_media_mode(parts[1].strip().lower(), message.from_user.id)
    send_with_premium(message.chat.id, result)

@bot.callback_query_handler(func=lambda call: call.data.startswith("vech_page_"))
def vech_page_callback(call):
    try:
        parts = call.data.split("_", 3)
        # format: vech_page_{page_idx}_{user_id}
        if len(parts) < 4:
            bot.answer_callback_query(call.id)
            return
        page_idx = int(parts[2])
        user_id = int(parts[3])

        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Not your session!", True)
            return

        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        state = VECH_PAGINATION.get(user_id)
        if not state:
            bot.answer_callback_query(call.id, "❌ Session expired. Run command again.", True)
            return

        pages = state["pages"]
        if page_idx < 0 or page_idx >= len(pages):
            bot.answer_callback_query(call.id, "❌ Invalid page.", True)
            return

        state["current"] = page_idx
        text = pages[page_idx]

        markup = InlineKeyboardMarkup(row_width=3)
        buttons = []

        if len(pages) > 1:
            if page_idx > 0:
                buttons.append(create_primary_button(
                    "◀ Prev",
                    callback_data=f"vech_page_{page_idx - 1}_{user_id}",
                    icon_custom_emoji_id="5246794802560774143"
                ))

            buttons.append(create_success_button(
                f"{page_idx + 1}/{len(pages)}",
                callback_data=f"vech_noop_{user_id}",
                icon_custom_emoji_id="5422537279400868395"
            ))

            if page_idx < len(pages) - 1:
                buttons.append(create_danger_button(
                    "Next ▶",
                    callback_data=f"vech_page_{page_idx + 1}_{user_id}",
                    icon_custom_emoji_id="6192763368139397161"
                ))

            markup.row(*buttons)

        json_file = state.get("json_file")
        if page_idx == len(pages) - 1 and json_file:
            file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
            JSON_FILES[file_id] = json_file
            markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))

        markup.add(create_default_button("🔙 Back", callback_data="back_to_result", icon_custom_emoji_id="5307517685846785869"))

        try:
            edit_with_premium(chat_id, msg_id, text, reply_markup=markup)
        except Exception as e:
            print(f"Vech pagination edit error: {e}")

        bot.answer_callback_query(call.id, f"📄 Page {page_idx + 1}/{len(pages)}")
    except Exception as e:
        print(f"Vech page callback error: {e}")
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("vech_noop_"))
def vech_noop_callback(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("num_page_"))
def num_page_callback(call):
    if call.data == "num_page_noop":
        bot.answer_callback_query(call.id)
        return

    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id

    state = NUM_PAGINATION.get(user_id)
    if not state:
        bot.answer_callback_query(call.id, "❌ Session expired. Run /num again.", True)
        return

    parts    = call.data.split("_", 3)
    page_num = int(parts[2])
    pages    = state["pages"]

    if page_num < 1 or page_num > len(pages):
        bot.answer_callback_query(call.id, "❌ Invalid page.", True)
        return

    txt, markup = pages[page_num - 1]
    json_file = state.get("json_file")
    if page_num == len(pages) and json_file and markup:
        file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
        JSON_FILES[file_id] = json_file
        markup.add(InlineKeyboardButton("Download JSON", callback_data=f"dljson_{file_id}"))

    try:
        edit_with_premium(chat_id, msg_id, txt, reply_markup=markup)
    except Exception as e:
        print(f"Pagination edit error: {e}")

    bot.answer_callback_query(call.id, f"📄 Page {page_num}/{len(pages)}")

@bot.callback_query_handler(func=lambda call: call.data == "num_dl_json")
def num_dl_json_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    state = NUM_PAGINATION.get(user_id)
    if not state or not state.get("json_file"):
        bot.answer_callback_query(call.id, "❌ Session expired. Run /num again.", True)
        return
    json_file = state["json_file"]
    if os.path.exists(json_file):
        with open(json_file, 'rb') as f:
            bot.send_document(chat_id, f, caption="JSON Export")
        bot.answer_callback_query(call.id, "✅ JSON file sent!")
    else:
        bot.answer_callback_query(call.id, "❌ File not found!", True)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global BOT_LOCKED, FORCE_JOIN_ENABLED
    try:
        if call.data.startswith("dljson_"):
            file_id = call.data.replace("dljson_", "")
            json_file = JSON_FILES.get(file_id)
            if not json_file:
                bot.answer_callback_query(call.id, "❌ File expired or not found!", True)
                return
            if os.path.exists(json_file):
                with open(json_file, 'rb') as f:
                    bot.send_document(call.message.chat.id, f, caption=f"JSON Export")
                bot.answer_callback_query(call.id, "✅ JSON file sent!")
            else:
                bot.answer_callback_query(call.id, "❌ File not found!", True)
            return

        if call.data.startswith("viewall_"):
            username = call.data.replace("viewall_", "")
            posts = POSTS_CACHE.get(username, [])
            if posts:
                bot.answer_callback_query(call.id, f"📸 Sending {len(posts)} posts...")
                send_all_posts(call.message.chat.id, username, posts)
            else:
                bot.answer_callback_query(call.id, "❌ No posts found!", True)
            return

        if call.data == "close_insta" or call.data == "no_posts" or call.data == "close_result":
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.answer_callback_query(call.id)
            return

        if call.data == "back_to_result":
            bot.answer_callback_query(call.id, "✅ Use the command again to see results", True)
            return

        if call.data == "check_join":
            user_id = call.from_user.id
            chat_id = call.message.chat.id

            # ── For join_by_request channels: record that user clicked join ──
            # This is the moment user claims they sent a request.
            # We record it so check_subscription passes for request-mode channels.
            if not PROMOTION_CHANNELS:
                load_channels_from_db()
            for _ch in PROMOTION_CHANNELS:
                if _ch.get('join_by_request') and _ch.get('enabled', True):
                    _uname = _ch.get('username', '')
                    if user_id not in JOIN_REQUESTS_SENT:
                        JOIN_REQUESTS_SENT[user_id] = set()
                    JOIN_REQUESTS_SENT[user_id].add(_uname)

            if check_subscription(user_id):
                bot.answer_callback_query(call.id, "✅ Verification successful!", True)
                try:
                    bot.delete_message(chat_id, call.message.message_id)
                except:
                    pass
            else:
                bot.answer_callback_query(call.id, "❌ Please join all channels first!", True)
            return

        # ── addc inline confirm / skip ──────────────────────────
        # Step 1: "Yes, Add It" → show 2 mode buttons
        if call.data.startswith("addc_confirm_"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "❌ Admin only!", True)
                return
            # format: addc_confirm_<pseudo_user>_<is_private>_<jreq_detected>
            try:
                payload     = call.data[len("addc_confirm_"):]
                parts_cb    = payload.rsplit("_", 2)
                pseudo_user = parts_cb[0]
                is_private  = parts_cb[1] == "1"
                # jreq_detected: Telegram already detected join_by_request on this channel
                jreq_detected = parts_cb[2] == "1"

                # Build mode-select prompt
                txt = (
                    f"⚙️ <b>Select Join Mode</b>\n\n"
                    f"👤 <b>Channel</b>: <code>{pseudo_user}</code>\n\n"
                    f"🌍 <b>Normal Join</b> — user joins directly, bot verifies membership\n\n"
                    f"🔐 <b>Request to Join</b> — user sends join request, bot lets them through "
                    f"as soon as request is sent (no admin approval needed)"
                )
                markup = InlineKeyboardMarkup()
                markup.row(
                    create_success_button(
                        "🌍 Normal Join",
                        callback_data=f"addc_mode_{pseudo_user}_{int(is_private)}_0"
                    ),
                    create_primary_button(
                        "🔐 Request to Join",
                        callback_data=f"addc_mode_{pseudo_user}_{int(is_private)}_1"
                    )
                )
                markup.add(create_danger_button("❌ Cancel", callback_data="addc_skip"))
                bot.answer_callback_query(call.id)
                try:
                    bot.edit_message_text(
                        txt,
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                except Exception:
                    send_with_premium(call.message.chat.id, txt, reply_markup=markup, parse_mode='HTML')
            except Exception as cb_e:
                bot.answer_callback_query(call.id, f"❌ Error: {cb_e}", True)
            return

        # Step 2: mode selected → actually add the channel
        if call.data.startswith("addc_mode_"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "❌ Admin only!", True)
                return
            try:
                payload  = call.data[len("addc_mode_"):]
                parts_cb = payload.rsplit("_", 1)
                token    = parts_cb[0]
                jreq     = parts_cb[1] == "1"   # 0=Normal, 1=Request

                cached = ADDC_LINK_CACHE.pop(token, None)
                if not cached:
                    bot.answer_callback_query(call.id, "❌ Session expired. Run /addc again.", True)
                    return

                chat_id_num = cached["chat_id"]
                url         = cached["link"]          # EXACT link admin typed — never regenerated
                is_private  = cached["is_private"]
                pseudo_user = str(chat_id_num)

                success, result_msg = add_force_join_channel_extended(
                    url, pseudo_user, call.from_user.id, is_private, jreq)

                if success:
                    mode_lbl = "🔐 Request to Join" if jreq else "🌍 Normal Join"
                    note = ""
                    if jreq:
                        note = ("\n\n💡 <b>How it works</b>: User sends join request → "
                                "bot immediately grants access without waiting for admin approval.")
                    bot.answer_callback_query(call.id, "✅ Channel Added!", True)
                    try:
                        bot.edit_message_text(
                            f"✅ <b>Channel Added!</b>\n\n"
                            f"🔗 <b>URL</b>: <code>{url}</code>\n"
                            f"🆔 <b>Chat ID</b>: <code>{chat_id_num}</code>\n"
                            f"⚙️ <b>Mode</b>: {mode_lbl}"
                            f"{note}",
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass
                else:
                    bot.answer_callback_query(call.id, f"❌ {result_msg}", True)
            except Exception as cb_e:
                bot.answer_callback_query(call.id, f"❌ Error: {cb_e}", True)
            return

        if call.data == "addc_skip":
            bot.answer_callback_query(call.id, "Cancelled.", True)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            return
        # ── end addc ────────────────────────────────────────────

        # ── leak pagination ─────────────────────────────────────
        if call.data.startswith("leak_page_"):
            if not is_admin(call.from_user.id):
                # any user can page through their own result stored in NUM_PAGINATION
                pass
            try:
                # format: leak_page_<page_idx>_<user_id>
                parts_lk = call.data[len("leak_page_"):].rsplit("_", 1)
                page_idx = int(parts_lk[0])
                uid_key  = int(parts_lk[1]) if parts_lk[1].isdigit() else call.from_user.id
                state    = NUM_PAGINATION.get(uid_key) or NUM_PAGINATION.get(call.from_user.id)
                if not state:
                    bot.answer_callback_query(call.id, "❌ Session expired, run /leak again", True)
                    return
                pages = state["pages"]
                if page_idx < 0 or page_idx >= len(pages):
                    bot.answer_callback_query(call.id, "❌ No such page", True)
                    return
                txt, _ = pages[page_idx]
                total_pages = len(pages)
                markup = InlineKeyboardMarkup(row_width=3)
                btns = []
                if page_idx > 0:
                    btns.append(create_primary_button(
                        "◀ Prev",
                        callback_data=f"leak_page_{page_idx - 1}_{call.from_user.id}",
                        icon_custom_emoji_id="5246794802560774143"
                    ))
                btns.append(create_success_button(
                    f"{page_idx + 1}/{total_pages}",
                    callback_data="noop",
                    icon_custom_emoji_id="5422537279400868395"
                ))
                if page_idx < total_pages - 1:
                    btns.append(create_danger_button(
                        "Next ▶",
                        callback_data=f"leak_page_{page_idx + 1}_{call.from_user.id}",
                        icon_custom_emoji_id="6192763368139397161"
                    ))
                if btns:
                    markup.row(*btns)
                # JSON on last page
                json_file = state.get("json_file")
                if page_idx == total_pages - 1 and json_file:
                    file_id = hashlib.md5(json_file.encode()).hexdigest()[:16]
                    JSON_FILES[file_id] = json_file
                    markup.add(InlineKeyboardButton("📥 Download JSON", callback_data=f"dljson_{file_id}"))
                bot.answer_callback_query(call.id)
                try:
                    bot.edit_message_text(
                        replace_with_premium_emojis(txt),
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                except Exception:
                    send_with_premium(call.message.chat.id, txt, reply_markup=markup)
            except Exception as lk_e:
                bot.answer_callback_query(call.id, f"❌ {lk_e}", True)
            return
        # ── end leak pagination ─────────────────────────────────

        if call.data == "help":
            class FakeMsg:
                def __init__(self, cid, uid):
                    self.chat = type('obj', (), {'id': cid})
                    self.from_user = type('obj', (), {'id': uid})
                    self.text = "/help"
                    self.date = int(datetime.now().timestamp())
            help_cmd(FakeMsg(call.message.chat.id, call.from_user.id))
            bot.answer_callback_query(call.id)
            return

        if call.data == "profile":
            class FakeMsg:
                def __init__(self, cid, uid):
                    self.chat = type('obj', (), {'id': cid})
                    self.from_user = type('obj', (), {'id': uid})
                    self.text = "/profile"
                    self.date = int(datetime.now().timestamp())
            profile_cmd(FakeMsg(call.message.chat.id, call.from_user.id))
            bot.answer_callback_query(call.id)
            return

        if call.data == "balance":
            class FakeMsg:
                def __init__(self, cid, uid):
                    self.chat = type('obj', (), {'id': cid})
                    self.from_user = type('obj', (), {'id': uid})
                    self.text = "/balance"
                    self.date = int(datetime.now().timestamp())
            balance_cmd(FakeMsg(call.message.chat.id, call.from_user.id))
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"❌ Callback error: {e}")

@bot.message_handler(func=lambda m: True)
def track_all_messages(m):
    if m.chat.type in ['group', 'supergroup']:
        chat_id = m.chat.id
        chat_title = m.chat.title or "Unknown"
        chat_type = m.chat.type
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _col_groups.update_one(
                {"chat_id": chat_id},
                {"$set": {"chat_title": chat_title, "chat_type": chat_type,
                           "last_active": now, "is_active": 1},
                 "$setOnInsert": {"added_date": now}},
                upsert=True
            )
        except Exception as e:
            print(f"track_all_messages error: {e}")



def is_recent_command(message):
    if is_admin(message.from_user.id):
        return True
    msg_time = datetime.fromtimestamp(message.date)
    if msg_time < BOT_START_TIME:
        return False
    return True

def clear_pending_updates():
    global LAST_PROCESSED_UPDATE_ID
    try:
        updates = bot.get_updates(limit=1, timeout=1)
        if updates:
            LAST_PROCESSED_UPDATE_ID = updates[-1].update_id
            bot.get_updates(offset=LAST_PROCESSED_UPDATE_ID + 1, limit=1, timeout=1)
    except Exception as e:
        pass

# ═══════════════════════════════════════════════════════════════
# CHANNEL → BROADCAST FEATURE
# When admin posts anything in BROADCAST_CHANNEL_ID,
# bot forwards that exact message to every user + group.
# ═══════════════════════════════════════════════════════════════

def _do_channel_broadcast(origin_chat_id: int, origin_msg_id: int):
    """Forward a channel post to all users and group chats."""
    all_targets = set()

    # collect users
    try:
        for doc in _col_users.find({}, {"user_id": 1}):
            all_targets.add(doc["user_id"])
    except Exception:
        pass

    # collect groups
    try:
        for gcid in get_all_group_chats():
            all_targets.add(gcid)
    except Exception:
        pass

    success = 0
    fail    = 0
    total   = len(all_targets)
    logger.info(f"[ChannelBroadcast] forwarding msg {origin_msg_id} from {origin_chat_id} → {total} targets")

    for target in all_targets:
        try:
            bot.forward_message(
                chat_id=target,
                from_chat_id=origin_chat_id,
                message_id=origin_msg_id,
                disable_notification=True,
            )
            success += 1
        except Exception as e:
            fail += 1
        time.sleep(0.05)   # ~20 msg/s — stays under Telegram limits

    logger.info(f"[ChannelBroadcast] done — success={success} fail={fail}")

    # notify admin
    try:
        bot.send_message(
            ADMIN_ID,
            f"📢 <b>Channel Broadcast Done</b>\n\n"
            f"✅ Sent: <b>{success}</b>\n"
            f"❌ Failed: <b>{fail}</b>\n"
            f"📊 Total: <b>{total}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


@bot.channel_post_handler(func=lambda m: True)
def on_channel_post(message):
    """Triggered for every post in any channel where the bot is admin."""
    global BROADCAST_CHANNEL_ID
    if BROADCAST_CHANNEL_ID is None:
        return
    if message.chat.id != int(BROADCAST_CHANNEL_ID):
        return

    # run in background so the handler returns immediately
    t = Thread(
        target=_do_channel_broadcast,
        args=(message.chat.id, message.message_id),
        daemon=True,
    )
    t.start()


# ── Admin commands to manage the broadcast channel on the fly ──

@bot.message_handler(commands=['setbc'])
def setbc_cmd(message):
    """
    /setbc <channel_id>
    Set which channel triggers auto-broadcast.
    Use the channel's numeric ID (e.g. -1001234567890).
    The bot must already be an admin in that channel.
    """
    global BROADCAST_CHANNEL_ID
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        send_with_premium(
            message.chat.id,
            "❌ Usage: <code>/setbc -1001234567890</code>\n\n"
            "Send the channel's numeric chat_id.\n"
            "Bot must be admin in that channel.",
            parse_mode="HTML"
        )
        return

    raw = parts[1].strip()
    try:
        cid = int(raw)
    except ValueError:
        send_with_premium(message.chat.id, "❌ That doesn't look like a valid chat_id. Use a number like <code>-1001234567890</code>.", parse_mode="HTML")
        return

    BROADCAST_CHANNEL_ID = cid
    send_with_premium(
        message.chat.id,
        f"✅ <b>Broadcast channel set!</b>\n\n"
        f"📢 Channel ID: <code>{cid}</code>\n\n"
        f"Any post you send to that channel will now auto-forward to all users + groups.",
        parse_mode="HTML"
    )
    logger.info(f"[setbc] admin {user_id} set BROADCAST_CHANNEL_ID={cid}")


@bot.message_handler(commands=['removebc'])
def removebc_cmd(message):
    """Disable the channel broadcast feature."""
    global BROADCAST_CHANNEL_ID
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return

    BROADCAST_CHANNEL_ID = None
    send_with_premium(message.chat.id, "✅ Channel broadcast disabled.")
    logger.info(f"[removebc] admin {user_id} disabled channel broadcast")


# ── /contact — Admin command: export all verified phone numbers as .txt ──────

def _fetch_all_verified_contacts():
    """
    Fetch all contact-verified users from MongoDB.
    Returns list of dicts: [{user_id, phone, username}]
    Sorted by phone_verified_at descending.
    """
    try:
        docs = list(
            _col_users.find(
                {"phone": {"$exists": True, "$nin": [None, ""]}},
                {"user_id": 1, "phone": 1, "phone_verified_at": 1,
                 "username": 1, "first_name": 1}
            ).sort("phone_verified_at", -1)
        )
        contacts = []
        for doc in docs:
            ph = str(doc.get("phone", "")).strip()
            if not ph:
                continue
            uid = str(doc.get("user_id", "")).strip()
            uname = str(doc.get("username", "") or doc.get("first_name", "")).strip() or "N/A"
            contacts.append({
                "user_id":  uid,
                "phone":    ph,
                "username": uname,
                "verified_at": str(doc.get("phone_verified_at", "N/A")),
            })
        return contacts
    except Exception as exc:
        logger.error(f"[_fetch_contacts] MongoDB error: {exc}")
        return []


# Keep old name as alias so nothing else breaks
turso_fetch_all_contacts = _fetch_all_verified_contacts





@bot.message_handler(commands=["contact"])
def contact_export_cmd(message):
    """
    /contact — Admin only.
    Exports all captcha-verified phone numbers from MongoDB as a .txt file.

    Format (tab-separated, one entry per line):
        UserID          Number
        1234567890      +919876543210
    """
    user_id = message.from_user.id
    if not is_admin(user_id):
        send_with_premium(message.chat.id, "❌ Admin only!")
        return

    processing_msg = None
    try:
        processing_msg = send_with_premium(
            message.chat.id,
            "⏳ <b>Contacts fetch ho rahe hain...</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    contacts = _fetch_all_verified_contacts()

    if processing_msg:
        try:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception:
            pass

    if not contacts:
        send_with_premium(
            message.chat.id,
            "❌ <b>No verified contacts found.</b>\n"
            "Abhi tak kisi ne bhi number verify nahi kiya.",
            parse_mode="HTML",
        )
        return

    # ── Build .txt content — clean aligned format ──────────────
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total    = len(contacts)
    bar      = "━" * 42
    header   = (
        f"{bar}\n"
        f"  VERIFIED CONTACTS EXPORT\n"
        f"  Total  : {total}\n"
        f"  Date   : {now_str}\n"
        f"{bar}\n\n"
        f"{'UserID':<20}{'Number'}\n"
        f"{'-'*20}{'-'*20}\n"
    )

    rows = []
    for c in contacts:
        uid    = c.get("user_id",  "").strip() or "N/A"
        number = c.get("phone",    "").strip() or "N/A"
        # ensure number starts with +
        if number and number != "N/A" and not number.startswith("+"):
            number = "+" + number
        rows.append(f"{uid:<20}{number}")

    txt_content = header + "\n".join(rows) + f"\n\n{bar}\n"

    # ── Write temp file and send ────────────────────────────────
    tmp_path = f"/tmp/verified_contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(txt_content)

        caption = (
            f"📋 <b>Verified Contacts</b>\n\n"
            f"✅ Total : <b>{total}</b>\n"
            f"📅 Date  : <code>{now_str}</code>"
        )
        filename = f"contacts_{datetime.now().strftime('%Y%m%d')}.txt"

        with open(tmp_path, "rb") as fh:
            try:
                bot.send_document(
                    message.chat.id,
                    document=fh,
                    caption=caption,
                    parse_mode="HTML",
                    visible_file_name=filename,
                )
            except TypeError:
                fh.seek(0)
                bot.send_document(
                    message.chat.id,
                    document=InputFile(fh, file_name=filename),
                    caption=caption,
                    parse_mode="HTML",
                )

        logger.info(f"[/contact] admin {user_id} exported {total} contacts")

    except Exception as exc:
        send_with_premium(
            message.chat.id,
            f"❌ <b>Export failed:</b>\n<code>{exc}</code>",
            parse_mode="HTML",
        )
        logger.error(f"[/contact] export error: {exc}")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


if __name__ == "__main__":
    print("=" * 60)
    print("✅ OSINT BOT STARTING WITH ALL FIXES...")
    print("✅ Developer: ㅤ⏤͟͟͞͞𝗙ʟɪʀᴛ𝗫ᴏ")
    print("=" * 60)

    # Turso removed — MongoDB only
    print("✅ Contact store: MongoDB only (Turso removed)")

    try:
        bot.remove_webhook()
    except:
        pass

    clear_pending_updates()

    try:
        bot.polling(non_stop=True, skip_pending=True, timeout=15)
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
    except Exception as e:
        print(f"❌ Error: {e}")
        print("🔄 Restarting in 3 seconds...")
        time.sleep(3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
