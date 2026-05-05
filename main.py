import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# =========================
# 基础配置
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DEFAULT_GROUP_INTERVAL_MINUTES = int(os.getenv("DEFAULT_GROUP_INTERVAL_MINUTES", "60"))
DEFAULT_GROUP_DAILY_LIMIT = int(os.getenv("DEFAULT_GROUP_DAILY_LIMIT", "8"))

QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "1"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "8"))

AD_LOOP_INTERVAL_SECONDS = int(os.getenv("AD_LOOP_INTERVAL_SECONDS", "60"))
MAX_SENDS_PER_LOOP = int(os.getenv("MAX_SENDS_PER_LOOP", "3"))

LOCAL_TZ_OFFSET = int(os.getenv("LOCAL_TZ_OFFSET", "8"))
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET))

ADMIN_NOTIFY = os.getenv("ADMIN_NOTIFY", "true").lower() == "true"


# =========================
# 快速广告模式状态
# =========================

(
    QA_SPONSOR,
    QA_TEXT,
    QA_BUTTON_TEXT,
    QA_BUTTON_URL,
    QA_IMAGE,
    QA_TARGETS,
    QA_INTERVAL,
    QA_DAILY_LIMIT,
    QA_CONFIRM,
) = range(9)


# =========================
# 设置投放参数状态
# =========================

(
    SET_IMMEDIATE,
    SET_START_AT,
    SET_INTERVAL,
    SET_DAILY_LIMIT,
    SET_QUIET_ENABLED,
    SET_QUIET_RANGE,
    SET_DURATION_DAYS,
    SET_TOTAL_LIMIT,
    SET_CONFIRM,
) = range(100, 109)


# =========================
# 数据库
# =========================

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("缺少 DATABASE_URL 或 DATABASE_PUBLIC_URL")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sponsors (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ads (
        id SERIAL PRIMARY KEY,
        sponsor_id INTEGER NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        text TEXT NOT NULL,
        image_type TEXT,
        image_value TEXT,
        button_text TEXT,
        button_url TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS targets (
        id SERIAL PRIMARY KEY,
        target TEXT UNIQUE NOT NULL,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        interval_minutes INTEGER NOT NULL DEFAULT 60,
        daily_limit INTEGER NOT NULL DEFAULT 8,
        quiet_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        quiet_start_hour INTEGER NOT NULL DEFAULT 1,
        quiet_end_hour INTEGER NOT NULL DEFAULT 8,
        start_at TIMESTAMPTZ,
        end_at TIMESTAMPTZ,
        total_limit INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ad_messages (
        id SERIAL PRIMARY KEY,
        sponsor_id INTEGER NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        ad_id INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
        target TEXT NOT NULL,
        actual_chat_id TEXT,
        message_id BIGINT NOT NULL,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        deleted_at TIMESTAMPTZ
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS delivery_logs (
        id SERIAL PRIMARY KEY,
        target TEXT,
        ad_id INTEGER,
        sponsor_id INTEGER,
        status TEXT NOT NULL,
        reason TEXT,
        detail TEXT,
        message_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # =========================
    # 兼容旧表，缺字段自动补
    # 重点修复：旧版 ad_messages 里残留 chat_id NOT NULL 导致发送成功后写库失败
    # =========================

    cur.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS image_type TEXT;")
    cur.execute("ALTER TABLE ads ADD COLUMN IF NOT EXISTS image_value TEXT;")

    cur.execute("ALTER TABLE targets ADD COLUMN IF NOT EXISTS quiet_enabled BOOLEAN NOT NULL DEFAULT TRUE;")
    cur.execute("ALTER TABLE targets ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE targets ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE targets ADD COLUMN IF NOT EXISTS total_limit INTEGER;")

    cur.execute("ALTER TABLE ad_messages ADD COLUMN IF NOT EXISTS target TEXT;")
    cur.execute("ALTER TABLE ad_messages ADD COLUMN IF NOT EXISTS actual_chat_id TEXT;")

    cur.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='ad_messages'
              AND column_name='chat_id'
        ) THEN
            ALTER TABLE ad_messages ALTER COLUMN chat_id DROP NOT NULL;
        END IF;
    END $$;
    """)

    cur.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='ad_messages'
              AND column_name='chat_id'
        ) THEN
            UPDATE ad_messages
            SET target = COALESCE(target, chat_id::TEXT)
            WHERE target IS NULL;

            UPDATE ad_messages
            SET actual_chat_id = COALESCE(actual_chat_id, chat_id::TEXT)
            WHERE actual_chat_id IS NULL;
        END IF;
    END $$;
    """)

    conn.commit()
    cur.close()
    conn.close()


def fetch_all(query: str, params=()):
    conn = db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_one(query: str, params=()):
    conn = db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def execute(query: str, params=()):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    cur.close()
    conn.close()


def execute_returning(query: str, params=()):
    conn = db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(query, params)
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


# =========================
# 权限
# =========================

def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


async def admin_only(update: Update) -> bool:
    if is_admin(update):
        return True

    if update.message:
        await update.message.reply_text("你没有权限操作这个机器人。")
    return False


# =========================
# 工具函数
# =========================

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_start_local() -> datetime:
    n = now_local()
    return datetime(n.year, n.month, n.day, tzinfo=LOCAL_TZ)


def normalize_skip(value: str) -> Optional[str]:
    value = value.strip()
    if value in {"-", "无", "不要", "不需要", "跳过", "skip"}:
        return None
    return value


def normalize_target(raw: str) -> Optional[str]:
    raw = raw.strip()

    if not raw:
        return None

    if raw.startswith("@"):
        return raw

    if raw.startswith("-") and raw[1:].isdigit():
        return raw

    if raw.isdigit():
        return raw

    return None


def parse_targets(text: str):
    raw = text.replace("，", ",").replace("\n", ",")
    targets = []

    for part in raw.split(","):
        target = normalize_target(part)
        if target and target not in targets:
            targets.append(target)

    return targets


def yes_no(value: str) -> Optional[bool]:
    value = value.strip().lower()
    if value in {"是", "yes", "y", "立即", "现在", "开", "开启"}:
        return True
    if value in {"否", "no", "n", "不", "关闭"}:
        return False
    return None


def parse_local_datetime(text: str) -> Optional[datetime]:
    text = text.strip()

    if text in {"现在", "立即", "now"}:
        return now_local()

    for fmt in ["%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"]:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%m-%d %H:%M":
                current_year = now_local().year
                dt = dt.replace(year=current_year)
            return dt.replace(tzinfo=LOCAL_TZ)
        except Exception:
            pass

    return None


def parse_quiet_range(text: str):
    text = text.strip().replace("：", ":").replace("到", "-").replace("—", "-")
    if "-" not in text:
        return None

    left, right = [x.strip() for x in text.split("-", 1)]

    try:
        start_hour = int(left.split(":")[0])
        end_hour = int(right.split(":")[0])
        if 0 <= start_hour <= 23 and 0 <= end_hour <= 23:
            return start_hour, end_hour
    except Exception:
        pass

    return None


def is_quiet_time(target_row) -> bool:
    if not target_row.get("quiet_enabled", True):
        return False

    current_hour = now_local().hour
    start = int(target_row["quiet_start_hour"])
    end = int(target_row["quiet_end_hour"])

    if start < end:
        return start <= current_hour < end

    return current_hour >= start or current_hour < end


def build_ad_text(sponsor_name: str, ad_text: str) -> str:
    return f"""【广告｜{sponsor_name}】
{ad_text}""".strip()


def build_keyboard(button_text: Optional[str], button_url: Optional[str]):
    if button_text and button_url:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(button_text, url=button_url)]
        ])
    return None


def image_type_from_text(value: Optional[str]):
    if not value:
        return None, None

    if value.startswith("http://") or value.startswith("https://"):
        return "url", value

    if os.path.isfile(value):
        return "local", value

    return None, None


def format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "未设置"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def target_sent_total(target: str) -> int:
    row = fetch_one("""
        SELECT COUNT(*) AS c
        FROM ad_messages
        WHERE target=%s
          AND deleted=FALSE;
    """, (target,))
    return int(row["c"] or 0)


def target_sent_today(target: str) -> int:
    start = today_start_local()
    row = fetch_one("""
        SELECT COUNT(*) AS c
        FROM ad_messages
        WHERE target=%s
          AND deleted=FALSE
          AND sent_at >= %s;
    """, (target, start))
    return int(row["c"] or 0)


def target_last_sent_at(target: str):
    row = fetch_one("""
        SELECT sent_at
        FROM ad_messages
        WHERE target=%s
          AND deleted=FALSE
        ORDER BY sent_at DESC
        LIMIT 1;
    """, (target,))
    return row["sent_at"] if row else None


def remaining_count(target_row) -> str:
    total_limit = target_row.get("total_limit")
    if not total_limit:
        return "不限"

    sent = target_sent_total(target_row["target"])
    remain = max(int(total_limit) - sent, 0)
    return str(remain)


def log_delivery(target, ad_id, sponsor_id, status, reason, detail=None, message_id=None):
    execute(
        """
        INSERT INTO delivery_logs(target, ad_id, sponsor_id, status, reason, detail, message_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """,
        (target, ad_id, sponsor_id, status, reason, detail, message_id)
    )


def get_meta(key: str, default=None):
    row = fetch_one("SELECT value FROM meta WHERE key=%s;", (key,))
    if not row:
        return default
    return row["value"]


def set_meta(key: str, value: str):
    execute(
        """
        INSERT INTO meta(key, value)
        VALUES (%s, %s)
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;
        """,
        (key, value)
    )


def should_log_skip(target: str, reason: str, cooldown_seconds: int = 600) -> bool:
    key = f"skip_log:{target}:{reason}"
    last_raw = get_meta(key, "0")

    try:
        last = float(last_raw)
    except Exception:
        last = 0

    now_ts = datetime.now(timezone.utc).timestamp()

    if now_ts - last >= cooldown_seconds:
        set_meta(key, str(now_ts))
        return True

    return False


def record_ad_message(sponsor_id, ad_id, target, actual_chat_id, message_id):
    conn = db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='ad_messages';
    """)
    columns = {r["column_name"] for r in cur.fetchall()}

    if "chat_id" in columns:
        cur.execute(
            """
            INSERT INTO ad_messages(
                sponsor_id, ad_id, chat_id, target, actual_chat_id, message_id
            )
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (
                sponsor_id,
                ad_id,
                actual_chat_id,
                target,
                actual_chat_id,
                message_id,
            )
        )
    else:
        cur.execute(
            """
            INSERT INTO ad_messages(
                sponsor_id, ad_id, target, actual_chat_id, message_id
            )
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                sponsor_id,
                ad_id,
                target,
                actual_chat_id,
                message_id,
            )
        )

    conn.commit()
    cur.close()
    conn.close()


async def notify_admins(bot, text: str):
    if not ADMIN_NOTIFY:
        return

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"通知管理员失败 {admin_id}: {e}")


def current_chat_possible_targets(update: Update):
    chat = update.effective_chat
    targets = [str(chat.id)]

    username = getattr(chat, "username", None)
    if username:
        targets.append(f"@{username}")

    return targets


# =========================
# /start /chatid
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    text = """广告投放机器人已启动。

常用命令：

/quick_ad - 快速创建广告，支持直接发图片、填写广告主、文案、按钮、投放目标
/shezhi 目标 - 设置某个频道或群的投放参数，例如 /shezhi @a123
/status - 查看整体状态
/status 目标 - 查看某个频道或群的投放状态
/logs - 查看最近投放日志
/logs 目标 - 查看某个频道或群的投放日志
/list_sponsors - 查看广告主列表
/list_ads - 查看广告素材列表
/list_targets - 查看投放目标列表
/delete_sponsor_ads 广告主名称 - 删除某个广告主所有已投放广告
/delete_sponsor 广告主名称 - 彻底删除某个广告主、广告素材、投放记录和日志
/delete_ads_here - 删除当前群里机器人发过的广告
/delete_all_ads - 删除所有未删除的广告
/pause_sponsor 广告主名称 - 暂停某个广告主
/resume_sponsor 广告主名称 - 恢复某个广告主
/pause_target 目标 - 暂停某个频道或群投放
/resume_target 目标 - 恢复某个频道或群投放
/pause_here - 暂停当前群投放
/resume_here - 恢复当前群投放
/chatid - 查看当前群或频道的 chat_id
/cancel - 取消当前填写流程

快速开始：
1. 私聊机器人发送 /quick_ad
2. 按提示填写广告
3. 再发送 /shezhi @频道名 设置投放时间、频率、期限
"""
    await update.message.reply_text(text)


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"当前 chat_id：{chat.id}\n标题：{chat.title or chat.full_name or ''}"
    )


# =========================
# 快速广告模式
# =========================

async def quick_ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return ConversationHandler.END

    context.user_data["quick_ad"] = {}

    await update.message.reply_text(
        "开始快速创建广告。\n\n"
        "第 1 步：请输入广告主名称。\n\n"
        "例如：ABC交易所"
    )
    return QA_SPONSOR


async def quick_ad_sponsor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sponsor = update.message.text.strip()

    if not sponsor:
        await update.message.reply_text("广告主名称不能为空，请重新输入。")
        return QA_SPONSOR

    context.user_data["quick_ad"]["sponsor"] = sponsor

    await update.message.reply_text(
        "第 2 步：请输入广告正文。\n\n"
        "例如：\n"
        "主流币实时观察，自动追踪 BTC / ETH / SOL / XRP / BNB 的短线信号、支撑压力和异动提醒。"
    )
    return QA_TEXT


async def quick_ad_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("广告正文不能为空，请重新输入。")
        return QA_TEXT

    context.user_data["quick_ad"]["text"] = text

    await update.message.reply_text(
        "第 3 步：请输入按钮文字。\n\n"
        "例如：进入频道\n\n"
        "不需要按钮就输入：-"
    )
    return QA_BUTTON_TEXT


async def quick_ad_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    button_text = normalize_skip(update.message.text)
    context.user_data["quick_ad"]["button_text"] = button_text

    if button_text:
        await update.message.reply_text(
            "第 4 步：请输入按钮链接。\n\n"
            "例如：https://t.me/your_channel"
        )
        return QA_BUTTON_URL

    context.user_data["quick_ad"]["button_url"] = None

    await update.message.reply_text(
        "第 5 步：请发送广告图片。\n\n"
        "你可以直接发图片给我。\n"
        "也可以发送图片链接。\n"
        "不需要图片就输入：-"
    )
    return QA_IMAGE


async def quick_ad_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    button_url = normalize_skip(update.message.text)

    if not button_url:
        await update.message.reply_text("你前面填写了按钮文字，所以这里必须填写按钮链接。")
        return QA_BUTTON_URL

    if not (button_url.startswith("http://") or button_url.startswith("https://")):
        await update.message.reply_text("按钮链接必须以 http:// 或 https:// 开头，请重新输入。")
        return QA_BUTTON_URL

    context.user_data["quick_ad"]["button_url"] = button_url

    await update.message.reply_text(
        "第 5 步：请发送广告图片。\n\n"
        "你可以直接发图片给我。\n"
        "也可以发送图片链接。\n"
        "不需要图片就输入：-"
    )
    return QA_IMAGE


async def quick_ad_image_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file_id = photo.file_id

    context.user_data["quick_ad"]["image_type"] = "telegram_file_id"
    context.user_data["quick_ad"]["image_value"] = file_id

    await update.message.reply_text(
        "图片已保存。\n\n"
        "第 6 步：请输入投放目标。\n\n"
        "支持：@频道用户名、@公开群用户名、-100数字群ID\n"
        "多个目标用逗号隔开。\n\n"
        "例如：@a123,-1001234567890,@b456"
    )
    return QA_TARGETS


async def quick_ad_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = normalize_skip(update.message.text)

    if not value:
        context.user_data["quick_ad"]["image_type"] = None
        context.user_data["quick_ad"]["image_value"] = None
    else:
        image_type, image_value = image_type_from_text(value)

        if not image_type:
            await update.message.reply_text(
                "图片格式不对。\n\n"
                "你可以：\n"
                "1. 直接发送图片\n"
                "2. 发送 http:// 或 https:// 图片链接\n"
                "3. 不需要图片输入 -"
            )
            return QA_IMAGE

        context.user_data["quick_ad"]["image_type"] = image_type
        context.user_data["quick_ad"]["image_value"] = image_value

    await update.message.reply_text(
        "第 6 步：请输入投放目标。\n\n"
        "支持：@频道用户名、@公开群用户名、-100数字群ID\n"
        "多个目标用逗号隔开。\n\n"
        "例如：@a123,-1001234567890,@b456"
    )
    return QA_TARGETS


async def quick_ad_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    targets = parse_targets(update.message.text)

    if not targets:
        await update.message.reply_text(
            "没有识别到有效投放目标，请重新输入。\n\n"
            "示例：@a123,-1001234567890,@b456"
        )
        return QA_TARGETS

    context.user_data["quick_ad"]["targets"] = targets

    await update.message.reply_text(
        f"已识别 {len(targets)} 个投放目标。\n\n"
        "第 7 步：请输入默认投放间隔，单位分钟。\n\n"
        f"建议输入：{DEFAULT_GROUP_INTERVAL_MINUTES}\n\n"
        "这个值后面可以用 /shezhi 目标 重新修改。"
    )
    return QA_INTERVAL


async def quick_ad_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：60")
        return QA_INTERVAL

    interval = int(raw)

    if interval < 5:
        await update.message.reply_text("间隔太短，建议至少 5 分钟，请重新输入。")
        return QA_INTERVAL

    context.user_data["quick_ad"]["interval"] = interval

    await update.message.reply_text(
        "第 8 步：请输入默认每日最多发几条广告。\n\n"
        f"建议输入：{DEFAULT_GROUP_DAILY_LIMIT}\n\n"
        "这个值后面可以用 /shezhi 目标 重新修改。"
    )
    return QA_DAILY_LIMIT


async def quick_ad_daily_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：8")
        return QA_DAILY_LIMIT

    daily_limit = int(raw)

    if daily_limit < 1:
        await update.message.reply_text("每日上限至少为 1，请重新输入。")
        return QA_DAILY_LIMIT

    context.user_data["quick_ad"]["daily_limit"] = daily_limit
    data = context.user_data["quick_ad"]

    button_info = "无"
    if data.get("button_text") and data.get("button_url"):
        button_info = f"{data['button_text']} → {data['button_url']}"

    image_info = "无"
    if data.get("image_type") == "telegram_file_id":
        image_info = "已上传 Telegram 图片"
    elif data.get("image_type"):
        image_info = data.get("image_value")

    preview = f"""请确认广告配置：

广告主：{data['sponsor']}

广告正文：
{data['text']}

按钮：{button_info}
图片：{image_info}

投放目标数量：{len(data['targets'])}
投放目标：
{", ".join(data['targets'])}

默认间隔：{data['interval']} 分钟
默认每日上限：{data['daily_limit']} 条

确认创建请输入：确认
取消请输入：取消
"""
    await update.message.reply_text(preview)
    return QA_CONFIRM


async def quick_ad_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip()

    if answer not in {"确认", "yes", "YES", "Yes", "y", "Y"}:
        await update.message.reply_text("已取消快速广告创建。")
        context.user_data.pop("quick_ad", None)
        return ConversationHandler.END

    data = context.user_data["quick_ad"]

    sponsor_row = execute_returning(
        """
        INSERT INTO sponsors(name, status)
        VALUES (%s, 'active')
        ON CONFLICT(name) DO UPDATE SET status='active'
        RETURNING id, name;
        """,
        (data["sponsor"],)
    )

    title = f"{data['sponsor']} 快速广告"

    ad_row = execute_returning(
        """
        INSERT INTO ads(
            sponsor_id, title, text, image_type, image_value,
            button_text, button_url, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
        RETURNING id;
        """,
        (
            sponsor_row["id"],
            title,
            data["text"],
            data.get("image_type"),
            data.get("image_value"),
            data.get("button_text"),
            data.get("button_url"),
        )
    )

    for target in data["targets"]:
        execute(
            """
            INSERT INTO targets(
                target, title, status, interval_minutes, daily_limit,
                quiet_enabled, quiet_start_hour, quiet_end_hour, start_at, end_at, total_limit
            )
            VALUES (%s, %s, 'paused', %s, %s, TRUE, %s, %s, NULL, NULL, NULL)
            ON CONFLICT(target) DO UPDATE SET
                status='paused',
                interval_minutes=EXCLUDED.interval_minutes,
                daily_limit=EXCLUDED.daily_limit,
                start_at=NULL,
                end_at=NULL,
                total_limit=NULL;
            """,
            (
                target,
                f"快速投放目标 {target}",
                data["interval"],
                data["daily_limit"],
                QUIET_START_HOUR,
                QUIET_END_HOUR,
            )
        )

    await update.message.reply_text(
        f"快速广告创建完成。\n\n"
        f"广告主：{sponsor_row['name']}\n"
        f"广告ID：{ad_row['id']}\n"
        f"投放目标：{len(data['targets'])} 个\n\n"
        f"为避免未设置前乱发，目标已默认暂停。\n"
        f"下一步请设置并启动投放：\n"
        f"/shezhi {data['targets'][0]}"
    )

    context.user_data.pop("quick_ad", None)
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = None

    if "shezhi" in context.user_data:
        target = context.user_data["shezhi"].get("target")

    context.user_data.pop("quick_ad", None)
    context.user_data.pop("shezhi", None)

    if target:
        await update.message.reply_text(
            f"已取消当前设置流程。\n\n"
            f"目标 {target} 已保持暂停状态，避免按旧规则继续投放。\n"
            f"需要恢复请重新使用：/shezhi {target}"
        )
    else:
        await update.message.reply_text("已取消当前流程。")

    return ConversationHandler.END


# =========================
# /shezhi 设置投放参数
# =========================

async def shezhi_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return ConversationHandler.END

    if not context.args:
        await update.message.reply_text("用法：/shezhi 目标\n例如：/shezhi @a123")
        return ConversationHandler.END

    target = normalize_target(context.args[0])

    if not target:
        await update.message.reply_text("目标格式不正确，支持 @频道名、@群名、-100群ID。")
        return ConversationHandler.END

    row = fetch_one("SELECT * FROM targets WHERE target=%s;", (target,))
    if not row:
        await update.message.reply_text(
            f"没有找到目标：{target}\n"
            f"请先用 /quick_ad 创建广告并添加这个目标。"
        )
        return ConversationHandler.END

    # 关键修复：
    # 进入 /shezhi 设置流程时，先暂停该目标，防止后台循环按旧规则继续投放。
    execute(
        "UPDATE targets SET status='paused' WHERE target=%s;",
        (target,)
    )

    context.user_data["shezhi"] = {
        "target": target,
    }

    await update.message.reply_text(
        f"开始设置投放目标：{target}\n\n"
        f"设置期间该目标已临时暂停，确认保存后才会恢复投放。\n\n"
        f"第 1 步：是否立即投放一次？\n"
        f"回复：是 / 否"
    )
    return SET_IMMEDIATE


async def shezhi_immediate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = yes_no(update.message.text)

    if result is None:
        await update.message.reply_text("请回复：是 或 否")
        return SET_IMMEDIATE

    context.user_data["shezhi"]["immediate"] = result

    if result:
        context.user_data["shezhi"]["start_at"] = now_local()
        await update.message.reply_text(
            "已选择立即投放。\n\n"
            "第 2 步：请输入投放间隔，单位分钟。\n例如：60"
        )
        return SET_INTERVAL

    await update.message.reply_text(
        "第 2 步：请输入开始投放时间。\n\n"
        "格式：2026-05-06 20:00\n"
        "也可以输入：现在"
    )
    return SET_START_AT


async def shezhi_start_at(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = parse_local_datetime(update.message.text)

    if not dt:
        await update.message.reply_text(
            "时间格式不正确。\n\n"
            "请按这个格式输入：2026-05-06 20:00\n"
            "或输入：现在"
        )
        return SET_START_AT

    context.user_data["shezhi"]["start_at"] = dt

    await update.message.reply_text(
        "第 3 步：请输入投放间隔，单位分钟。\n例如：60"
    )
    return SET_INTERVAL


async def shezhi_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：60")
        return SET_INTERVAL

    interval = int(raw)

    if interval < 5:
        await update.message.reply_text("间隔太短，建议至少 5 分钟，请重新输入。")
        return SET_INTERVAL

    context.user_data["shezhi"]["interval"] = interval

    await update.message.reply_text(
        "第 4 步：请输入每日最多投放几条。\n例如：8"
    )
    return SET_DAILY_LIMIT


async def shezhi_daily_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：8")
        return SET_DAILY_LIMIT

    daily_limit = int(raw)

    if daily_limit < 1:
        await update.message.reply_text("每日上限至少为 1，请重新输入。")
        return SET_DAILY_LIMIT

    context.user_data["shezhi"]["daily_limit"] = daily_limit

    await update.message.reply_text(
        "第 5 步：是否启用静默时间？\n"
        "回复：是 / 否"
    )
    return SET_QUIET_ENABLED


async def shezhi_quiet_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = yes_no(update.message.text)

    if result is None:
        await update.message.reply_text("请回复：是 或 否")
        return SET_QUIET_ENABLED

    context.user_data["shezhi"]["quiet_enabled"] = result

    if result:
        await update.message.reply_text(
            "第 6 步：请输入静默时间段。\n\n"
            "格式：01:00-08:00\n"
            "意思是这个时间段不发广告。"
        )
        return SET_QUIET_RANGE

    context.user_data["shezhi"]["quiet_start_hour"] = QUIET_START_HOUR
    context.user_data["shezhi"]["quiet_end_hour"] = QUIET_END_HOUR

    await update.message.reply_text(
        "第 6 步：请输入投放期限，单位天。\n\n"
        "比如：3、7、15\n"
        "如果想长期投放，输入：0"
    )
    return SET_DURATION_DAYS


async def shezhi_quiet_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = parse_quiet_range(update.message.text)

    if not result:
        await update.message.reply_text(
            "静默时间格式不正确。\n\n"
            "请按这个格式输入：01:00-08:00"
        )
        return SET_QUIET_RANGE

    start_hour, end_hour = result

    context.user_data["shezhi"]["quiet_start_hour"] = start_hour
    context.user_data["shezhi"]["quiet_end_hour"] = end_hour

    await update.message.reply_text(
        "第 7 步：请输入投放期限，单位天。\n\n"
        "比如：3、7、15\n"
        "如果想长期投放，输入：0"
    )
    return SET_DURATION_DAYS


async def shezhi_duration_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：7。长期投放请输入 0。")
        return SET_DURATION_DAYS

    days = int(raw)
    context.user_data["shezhi"]["duration_days"] = days

    start_at = context.user_data["shezhi"]["start_at"]

    if days == 0:
        context.user_data["shezhi"]["end_at"] = None
    else:
        context.user_data["shezhi"]["end_at"] = start_at + timedelta(days=days)

    await update.message.reply_text(
        "第 8 步：请输入总投放上限。\n\n"
        "比如：30 表示最多投放 30 条。\n"
        "如果不限制总投放量，输入：0"
    )
    return SET_TOTAL_LIMIT


async def shezhi_total_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：30。不限制请输入 0。")
        return SET_TOTAL_LIMIT

    total_limit = int(raw)
    context.user_data["shezhi"]["total_limit"] = None if total_limit == 0 else total_limit

    data = context.user_data["shezhi"]

    quiet_text = "关闭"
    if data["quiet_enabled"]:
        quiet_text = f"启用，{data['quiet_start_hour']}:00-{data['quiet_end_hour']}:00"

    duration_text = "长期"
    if data["duration_days"] != 0:
        duration_text = f"{data['duration_days']} 天"

    total_text = "不限" if data["total_limit"] is None else f"{data['total_limit']} 条"

    preview = f"""请确认投放设置：

目标：{data['target']}
是否立即投放：{"是" if data["immediate"] else "否"}
开始时间：{format_dt(data["start_at"])}
投放间隔：{data["interval"]} 分钟
每日上限：{data["daily_limit"]} 条
静默时间：{quiet_text}
投放期限：{duration_text}
结束时间：{format_dt(data["end_at"])}
总投放上限：{total_text}

确认保存请输入：确认
取消请输入：取消
"""
    await update.message.reply_text(preview)
    return SET_CONFIRM


async def shezhi_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip()

    if answer not in {"确认", "yes", "YES", "Yes", "y", "Y"}:
        await update.message.reply_text("已取消设置。")
        context.user_data.pop("shezhi", None)
        return ConversationHandler.END

    data = context.user_data["shezhi"]

    execute(
        """
        UPDATE targets
        SET
            status='active',
            interval_minutes=%s,
            daily_limit=%s,
            quiet_enabled=%s,
            quiet_start_hour=%s,
            quiet_end_hour=%s,
            start_at=%s,
            end_at=%s,
            total_limit=%s
        WHERE target=%s;
        """,
        (
            data["interval"],
            data["daily_limit"],
            data["quiet_enabled"],
            data.get("quiet_start_hour", QUIET_START_HOUR),
            data.get("quiet_end_hour", QUIET_END_HOUR),
            data["start_at"],
            data["end_at"],
            data["total_limit"],
            data["target"],
        )
    )

    await update.message.reply_text(
        f"投放设置已保存：{data['target']}\n\n"
        f"可以用下面命令查看状态：\n"
        f"/status {data['target']}"
    )

    if data["immediate"]:
        target_row = fetch_one("SELECT * FROM targets WHERE target=%s;", (data["target"],))
        ok, note = await send_ad_to_target(context.bot, target_row, force=True)
        await update.message.reply_text(f"立即投放结果：{note}")

    context.user_data.pop("shezhi", None)
    return ConversationHandler.END


# =========================
# 列表和管理
# =========================

async def list_sponsors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("SELECT id, name, status FROM sponsors ORDER BY id DESC LIMIT 50;")

    if not rows:
        await update.message.reply_text("暂无广告主。")
        return

    text = "广告主列表：\n" + "\n".join(
        f"{r['id']}. {r['name']}｜{r['status']}" for r in rows
    )
    await update.message.reply_text(text)


async def list_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("""
        SELECT ads.id, ads.title, ads.status, sponsors.name AS sponsor_name
        FROM ads
        JOIN sponsors ON sponsors.id = ads.sponsor_id
        ORDER BY ads.id DESC
        LIMIT 50;
    """)

    if not rows:
        await update.message.reply_text("暂无广告素材。")
        return

    text = "广告素材列表：\n" + "\n".join(
        f"{r['id']}. [{r['sponsor_name']}] {r['title']}｜{r['status']}" for r in rows
    )
    await update.message.reply_text(text)


async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("""
        SELECT target, title, status, interval_minutes, daily_limit, start_at, end_at, total_limit
        FROM targets
        ORDER BY id DESC
        LIMIT 50;
    """)

    if not rows:
        await update.message.reply_text("暂无投放目标。")
        return

    lines = ["投放目标列表："]
    for r in rows:
        lines.append(
            f"{r['target']}｜{r['status']}｜间隔{r['interval_minutes']}分钟｜每日{r['daily_limit']}条｜开始{format_dt(r['start_at'])}｜结束{format_dt(r['end_at'])}｜总上限{r['total_limit'] or '不限'}"
        )

    await update.message.reply_text("\n".join(lines))


async def pause_sponsor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.message.reply_text("用法：/pause_sponsor 广告主名称")
        return

    execute("UPDATE sponsors SET status='paused' WHERE name=%s;", (name,))
    await update.message.reply_text(f"已暂停广告主：{name}")


async def resume_sponsor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    name = " ".join(context.args).strip()

    if not name:
        await update.message.reply_text("用法：/resume_sponsor 广告主名称")
        return

    execute("UPDATE sponsors SET status='active' WHERE name=%s;", (name,))
    await update.message.reply_text(f"已恢复广告主：{name}")


async def pause_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("用法：/pause_target 目标\n例如：/pause_target @a123")
        return

    target = normalize_target(context.args[0])
    if not target:
        await update.message.reply_text("目标格式不正确。")
        return

    execute("UPDATE targets SET status='paused' WHERE target=%s;", (target,))
    await update.message.reply_text(f"已暂停目标投放：{target}")


async def resume_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("用法：/resume_target 目标\n例如：/resume_target @a123")
        return

    target = normalize_target(context.args[0])
    if not target:
        await update.message.reply_text("目标格式不正确。")
        return

    execute("UPDATE targets SET status='active' WHERE target=%s;", (target,))
    await update.message.reply_text(f"已恢复目标投放：{target}")


async def pause_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    possible = current_chat_possible_targets(update)

    execute(
        "UPDATE targets SET status='paused' WHERE target = ANY(%s);",
        (possible,)
    )

    await update.message.reply_text("已暂停当前群/频道对应目标的投放。")


async def resume_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    possible = current_chat_possible_targets(update)

    execute(
        "UPDATE targets SET status='active' WHERE target = ANY(%s);",
        (possible,)
    )

    await update.message.reply_text("已恢复当前群/频道对应目标的投放。")


# =========================
# 删除广告
# =========================

async def delete_message_safe(bot, actual_chat_id: str, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id=actual_chat_id, message_id=message_id)
        return True
    except Exception as e:
        print(f"删除失败 chat={actual_chat_id} msg={message_id}: {e}")
        return False


async def delete_sponsor_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    sponsor_name = " ".join(context.args).strip()

    if not sponsor_name:
        await update.message.reply_text("用法：/delete_sponsor_ads 广告主名称")
        return

    rows = fetch_all("""
        SELECT ad_messages.id, ad_messages.actual_chat_id, ad_messages.message_id
        FROM ad_messages
        JOIN sponsors ON sponsors.id = ad_messages.sponsor_id
        WHERE sponsors.name=%s
          AND ad_messages.deleted=FALSE
          AND ad_messages.actual_chat_id IS NOT NULL
          AND ad_messages.message_id IS NOT NULL
        ORDER BY ad_messages.id DESC;
    """, (sponsor_name,))

    if not rows:
        await update.message.reply_text(
            f"没有找到该广告主未删除的广告记录：{sponsor_name}\n\n"
            f"说明：如果是修复前已经发出的广告，因为当时数据库记录失败，机器人无法自动删除那批旧广告。"
        )
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["actual_chat_id"], r["message_id"])

        if ok:
            ok_count += 1
            execute("UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;", (r["id"],))
        else:
            fail_count += 1

        await asyncio.sleep(0.15)

    await update.message.reply_text(
        f"广告主【{sponsor_name}】广告消息清理完成。\n成功：{ok_count}\n失败：{fail_count}"
    )


async def delete_sponsor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    sponsor_name = " ".join(context.args).strip()

    if not sponsor_name:
        await update.message.reply_text(
            "用法：/delete_sponsor 广告主名称\n\n"
            "说明：这个命令会彻底删除该广告主、该广告主的所有广告素材、投放记录和日志，并尝试删除已经发出去的广告消息。"
        )
        return

    sponsor = fetch_one(
        "SELECT id, name FROM sponsors WHERE name=%s;",
        (sponsor_name,)
    )

    if not sponsor:
        await update.message.reply_text(f"没有找到广告主：{sponsor_name}")
        return

    sponsor_id = sponsor["id"]

    rows = fetch_all("""
        SELECT id, actual_chat_id, message_id
        FROM ad_messages
        WHERE sponsor_id=%s
          AND deleted=FALSE
          AND actual_chat_id IS NOT NULL
          AND message_id IS NOT NULL
        ORDER BY id DESC;
    """, (sponsor_id,))

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(
            context.bot,
            r["actual_chat_id"],
            r["message_id"]
        )

        if ok:
            ok_count += 1
            execute(
                "UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;",
                (r["id"],)
            )
        else:
            fail_count += 1

        await asyncio.sleep(0.15)

    execute(
        "DELETE FROM delivery_logs WHERE sponsor_id=%s;",
        (sponsor_id,)
    )

    execute(
        "DELETE FROM ad_messages WHERE sponsor_id=%s;",
        (sponsor_id,)
    )

    execute(
        "DELETE FROM ads WHERE sponsor_id=%s;",
        (sponsor_id,)
    )

    execute(
        "DELETE FROM sponsors WHERE id=%s;",
        (sponsor_id,)
    )

    await update.message.reply_text(
        f"广告主【{sponsor_name}】已彻底删除。\n\n"
        f"已尝试删除线上广告：{len(rows)} 条\n"
        f"删除成功：{ok_count}\n"
        f"删除失败：{fail_count}\n\n"
        f"该广告主、广告素材、投放记录、投放日志已从数据库删除。"
    )


async def delete_ads_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    actual_chat_id = str(update.effective_chat.id)

    rows = fetch_all("""
        SELECT id, actual_chat_id, message_id
        FROM ad_messages
        WHERE actual_chat_id=%s
          AND deleted=FALSE
          AND message_id IS NOT NULL
        ORDER BY id DESC;
    """, (actual_chat_id,))

    if not rows:
        await update.message.reply_text("当前群没有未删除的广告记录。")
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["actual_chat_id"], r["message_id"])

        if ok:
            ok_count += 1
            execute("UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;", (r["id"],))
        else:
            fail_count += 1

        await asyncio.sleep(0.15)

    await update.message.reply_text(
        f"当前群广告清理完成。\n成功：{ok_count}\n失败：{fail_count}"
    )


async def delete_all_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("""
        SELECT id, actual_chat_id, message_id
        FROM ad_messages
        WHERE deleted=FALSE
          AND actual_chat_id IS NOT NULL
          AND message_id IS NOT NULL
        ORDER BY id DESC;
    """)

    if not rows:
        await update.message.reply_text("没有未删除的广告记录。")
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["actual_chat_id"], r["message_id"])

        if ok:
            ok_count += 1
            execute("UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;", (r["id"],))
        else:
            fail_count += 1

        await asyncio.sleep(0.15)

    await update.message.reply_text(
        f"全部广告清理完成。\n成功：{ok_count}\n失败：{fail_count}"
    )


# =========================
# 状态与日志
# =========================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if context.args:
        target = normalize_target(context.args[0])
        row = fetch_one("SELECT * FROM targets WHERE target=%s;", (target,))

        if not row:
            await update.message.reply_text(f"没有找到目标：{target}")
            return

        today = target_sent_today(target)
        total = target_sent_total(target)
        remain = remaining_count(row)

        last = target_last_sent_at(target)
        last_text = format_dt(last) if last else "暂无"

        text = f"""目标投放状态：

目标：{row['target']}
状态：{row['status']}
开始时间：{format_dt(row['start_at'])}
结束时间：{format_dt(row['end_at'])}
投放间隔：{row['interval_minutes']} 分钟
每日上限：{row['daily_limit']} 条
今日已发：{today} / {row['daily_limit']}
总计已发：{total}
总上限：{row['total_limit'] or '不限'}
剩余：{remain}
静默时间：{"启用" if row['quiet_enabled'] else "关闭"}，{row['quiet_start_hour']}:00-{row['quiet_end_hour']}:00
最后发送：{last_text}
"""
        await update.message.reply_text(text)
        return

    sponsors = fetch_one("SELECT COUNT(*) AS c FROM sponsors;")["c"]
    active_sponsors = fetch_one("SELECT COUNT(*) AS c FROM sponsors WHERE status='active';")["c"]

    ads = fetch_one("SELECT COUNT(*) AS c FROM ads;")["c"]
    active_ads = fetch_one("SELECT COUNT(*) AS c FROM ads WHERE status='active';")["c"]

    targets = fetch_one("SELECT COUNT(*) AS c FROM targets;")["c"]
    active_targets = fetch_one("SELECT COUNT(*) AS c FROM targets WHERE status='active';")["c"]

    messages = fetch_one("SELECT COUNT(*) AS c FROM ad_messages;")["c"]
    undeleted = fetch_one("SELECT COUNT(*) AS c FROM ad_messages WHERE deleted=FALSE;")["c"]

    logs_count = fetch_one("SELECT COUNT(*) AS c FROM delivery_logs;")["c"]

    text = f"""机器人整体状态：

广告主：{sponsors} 个，启用 {active_sponsors} 个
广告素材：{ads} 条，启用 {active_ads} 条
投放目标：{targets} 个，启用 {active_targets} 个
已投放记录：{messages} 条
未删除广告：{undeleted} 条
投放日志：{logs_count} 条

默认间隔：{DEFAULT_GROUP_INTERVAL_MINUTES} 分钟
默认每日上限：{DEFAULT_GROUP_DAILY_LIMIT} 条
默认静默时间：{QUIET_START_HOUR}:00 - {QUIET_END_HOUR}:00
"""
    await update.message.reply_text(text)


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if context.args:
        target = normalize_target(context.args[0])
        rows = fetch_all("""
            SELECT *
            FROM delivery_logs
            WHERE target=%s
            ORDER BY id DESC
            LIMIT 20;
        """, (target,))
    else:
        rows = fetch_all("""
            SELECT *
            FROM delivery_logs
            ORDER BY id DESC
            LIMIT 20;
        """)

    if not rows:
        await update.message.reply_text("暂无投放日志。")
        return

    lines = ["最近投放日志："]

    for r in rows:
        icon = "✅" if r["status"] == "success" else "❌" if r["status"] == "failed" else "⏸"
        lines.append(
            f"{icon} {format_dt(r['created_at'])}\n"
            f"目标：{r['target']}\n"
            f"状态：{r['status']}\n"
            f"原因：{r['reason'] or '-'}\n"
            f"详情：{r['detail'] or '-'}\n"
        )

    await update.message.reply_text("\n".join(lines))


# =========================
# 自动投放
# =========================

def target_interval_ok(target_row) -> bool:
    last = target_last_sent_at(target_row["target"])

    if not last:
        return True

    diff = now_utc() - last
    return diff.total_seconds() >= int(target_row["interval_minutes"]) * 60


def target_time_window_ok(target_row):
    current = now_utc()

    start_at = target_row.get("start_at")
    end_at = target_row.get("end_at")

    if start_at and current < start_at:
        return False, "未到开始时间"

    if end_at and current > end_at:
        return False, "投放期限已结束"

    return True, "时间窗口正常"


def target_total_limit_ok(target_row):
    total_limit = target_row.get("total_limit")

    if not total_limit:
        return True, "不限总量"

    sent = target_sent_total(target_row["target"])

    if sent >= int(total_limit):
        return False, "达到总投放上限"

    return True, "总量未满"


def pick_next_ad_for_target(target: str):
    return fetch_one("""
        SELECT
            ads.id AS ad_id,
            ads.title,
            ads.text,
            ads.image_type,
            ads.image_value,
            ads.button_text,
            ads.button_url,
            sponsors.id AS sponsor_id,
            sponsors.name AS sponsor_name
        FROM ads
        JOIN sponsors ON sponsors.id = ads.sponsor_id
        WHERE ads.status='active'
          AND sponsors.status='active'
        ORDER BY RANDOM()
        LIMIT 1;
    """)


async def send_ad_to_target(bot, target_row, force=False):
    target = target_row["target"]

    if not force:
        ok, reason = target_time_window_ok(target_row)
        if not ok:
            if should_log_skip(target, reason):
                log_delivery(target, None, None, "skipped", reason, None)
            return False, reason

        if is_quiet_time(target_row):
            reason = "处于静默时间"
            if should_log_skip(target, reason):
                log_delivery(target, None, None, "skipped", reason, None)
            return False, reason

        if target_sent_today(target) >= int(target_row["daily_limit"]):
            reason = "达到每日上限"
            if should_log_skip(target, reason):
                log_delivery(target, None, None, "skipped", reason, None)
            return False, reason

        ok, reason = target_total_limit_ok(target_row)
        if not ok:
            if should_log_skip(target, reason):
                log_delivery(target, None, None, "skipped", reason, None)
            return False, reason

        if not target_interval_ok(target_row):
            reason = "未到投放间隔"
            if should_log_skip(target, reason):
                log_delivery(target, None, None, "skipped", reason, None)
            return False, reason

    ad = pick_next_ad_for_target(target)

    if not ad:
        reason = "没有可投放广告"

        if should_log_skip(target, reason):
            log_delivery(
                target,
                None,
                None,
                "skipped",
                reason,
                "当前没有启用中的广告主或广告素材"
            )

        return False, reason

    text = build_ad_text(ad["sponsor_name"], ad["text"])
    keyboard = build_keyboard(ad["button_text"], ad["button_url"])

    msg = None

    try:
        image_type = ad.get("image_type")
        image_value = ad.get("image_value")

        if image_type == "telegram_file_id" and image_value:
            msg = await bot.send_photo(
                chat_id=target,
                photo=image_value,
                caption=text,
                reply_markup=keyboard,
            )

        elif image_type == "url" and image_value:
            msg = await bot.send_photo(
                chat_id=target,
                photo=image_value,
                caption=text,
                reply_markup=keyboard,
            )

        elif image_type == "local" and image_value and os.path.isfile(image_value):
            with open(image_value, "rb") as f:
                msg = await bot.send_photo(
                    chat_id=target,
                    photo=f,
                    caption=text,
                    reply_markup=keyboard,
                )

        else:
            msg = await bot.send_message(
                chat_id=target,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

    except Exception as e:
        err = str(e)

        log_delivery(
            target,
            ad["ad_id"],
            ad["sponsor_id"],
            "failed",
            "Telegram发送失败",
            err
        )

        notify_text = f"""❌ 广告发送失败

目标：{target}
广告主：{ad['sponsor_name']}
广告ID：{ad['ad_id']}
原因：{err}

常见原因：
1. 机器人不是频道/群管理员
2. 没有发消息权限
3. @用户名写错
4. 私密群不能用 @用户名，需要用 -100 群ID
"""
        await notify_admins(bot, notify_text)

        return False, err

    actual_chat_id = str(msg.chat.id)
    message_id = msg.message_id

    try:
        record_ad_message(
            sponsor_id=ad["sponsor_id"],
            ad_id=ad["ad_id"],
            target=target,
            actual_chat_id=actual_chat_id,
            message_id=message_id,
        )

    except Exception as e:
        err = str(e)

        log_delivery(
            target,
            ad["ad_id"],
            ad["sponsor_id"],
            "failed",
            "发送成功但记录失败",
            err,
            message_id
        )

        notify_text = f"""⚠️ 广告已发送，但数据库记录失败

目标：{target}
实际chat_id：{actual_chat_id}
广告主：{ad['sponsor_name']}
广告ID：{ad['ad_id']}
message_id：{message_id}

错误：
{err}

这类广告可能无法被 /delete_sponsor_ads 自动删除。
"""
        await notify_admins(bot, notify_text)

        return False, "发送成功但记录失败"

    log_delivery(
        target,
        ad["ad_id"],
        ad["sponsor_id"],
        "success",
        "发送成功",
        f"actual_chat_id={actual_chat_id}, message_id={message_id}",
        message_id
    )

    sent_total = target_sent_total(target)
    sent_today = target_sent_today(target)
    remain = remaining_count(target_row)

    notify_text = f"""✅ 广告发送成功

目标：{target}
实际chat_id：{actual_chat_id}
广告主：{ad['sponsor_name']}
广告ID：{ad['ad_id']}
今日进度：{sent_today} / {target_row['daily_limit']}
总计已发：{sent_total}
剩余：{remain}
message_id：{message_id}
"""
    await notify_admins(bot, notify_text)

    return True, "发送成功"


async def ad_loop(app: Application):
    await asyncio.sleep(5)

    while True:
        try:
            rows = fetch_all("""
                SELECT *
                FROM targets
                WHERE status='active'
                ORDER BY id ASC;
            """)

            sends = 0

            for t in rows:
                if sends >= MAX_SENDS_PER_LOOP:
                    break

                ok, note = await send_ad_to_target(app.bot, t, force=False)

                if ok:
                    sends += 1
                    await asyncio.sleep(1.5)
                else:
                    print(f"跳过 {t['target']}：{note}")

        except Exception as e:
            print("广告循环异常:", e)

        await asyncio.sleep(AD_LOOP_INTERVAL_SECONDS)


async def post_init(app: Application):
    asyncio.create_task(ad_loop(app))


# =========================
# 启动
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("缺少 BOT_TOKEN")

    if not DATABASE_URL:
        raise RuntimeError("缺少 DATABASE_URL 或 DATABASE_PUBLIC_URL")

    if not ADMIN_IDS:
        raise RuntimeError("缺少 ADMIN_IDS，请填你的 Telegram 用户 ID")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    quick_ad_conv = ConversationHandler(
        entry_points=[CommandHandler("quick_ad", quick_ad_start)],
        states={
            QA_SPONSOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_sponsor)],
            QA_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_text)],
            QA_BUTTON_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_button_text)],
            QA_BUTTON_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_button_url)],
            QA_IMAGE: [
                MessageHandler(filters.PHOTO, quick_ad_image_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_image_text),
            ],
            QA_TARGETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_targets)],
            QA_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_interval)],
            QA_DAILY_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_daily_limit)],
            QA_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    shezhi_conv = ConversationHandler(
        entry_points=[CommandHandler("shezhi", shezhi_start)],
        states={
            SET_IMMEDIATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_immediate)],
            SET_START_AT: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_start_at)],
            SET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_interval)],
            SET_DAILY_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_daily_limit)],
            SET_QUIET_ENABLED: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_quiet_enabled)],
            SET_QUIET_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_quiet_range)],
            SET_DURATION_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_duration_days)],
            SET_TOTAL_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_total_limit)],
            SET_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, shezhi_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
    )

    app.add_handler(quick_ad_conv)
    app.add_handler(shezhi_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))

    app.add_handler(CommandHandler("list_sponsors", list_sponsors))
    app.add_handler(CommandHandler("list_ads", list_ads))
    app.add_handler(CommandHandler("list_targets", list_targets))

    app.add_handler(CommandHandler("pause_sponsor", pause_sponsor))
    app.add_handler(CommandHandler("resume_sponsor", resume_sponsor))
    app.add_handler(CommandHandler("pause_target", pause_target))
    app.add_handler(CommandHandler("resume_target", resume_target))
    app.add_handler(CommandHandler("pause_here", pause_here))
    app.add_handler(CommandHandler("resume_here", resume_here))

    app.add_handler(CommandHandler("delete_sponsor_ads", delete_sponsor_ads))
    app.add_handler(CommandHandler("delete_sponsor", delete_sponsor))
    app.add_handler(CommandHandler("delete_ads_here", delete_ads_here))
    app.add_handler(CommandHandler("delete_all_ads", delete_all_ads))

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))

    print("广告投放机器人启动成功：/shezhi 设置期间自动暂停目标，避免按旧规则乱发")
    app.run_polling()


if __name__ == "__main__":
    main()