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

# 支持 DATABASE_URL，也支持你手动填 DATABASE_PUBLIC_URL
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

LOCAL_TZ = timezone(timedelta(hours=int(os.getenv("LOCAL_TZ_OFFSET", "8"))))


# =========================
# 快速广告模式状态
# =========================

(
    QA_SPONSOR,
    QA_TEXT,
    QA_BUTTON_TEXT,
    QA_BUTTON_URL,
    QA_IMAGE,
    QA_GROUPS,
    QA_INTERVAL,
    QA_DAILY_LIMIT,
    QA_CONFIRM,
) = range(9)


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
        image TEXT,
        button_text TEXT,
        button_url TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT UNIQUE NOT NULL,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        interval_minutes INTEGER NOT NULL DEFAULT 60,
        daily_limit INTEGER NOT NULL DEFAULT 8,
        quiet_start_hour INTEGER NOT NULL DEFAULT 1,
        quiet_end_hour INTEGER NOT NULL DEFAULT 8,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ad_messages (
        id SERIAL PRIMARY KEY,
        sponsor_id INTEGER NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        ad_id INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        deleted_at TIMESTAMPTZ
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
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


def is_quiet_time(group_row) -> bool:
    current_hour = now_local().hour
    start = int(group_row["quiet_start_hour"])
    end = int(group_row["quiet_end_hour"])

    if start < end:
        return start <= current_hour < end

    return current_hour >= start or current_hour < end


def today_start_local() -> datetime:
    n = now_local()
    return datetime(n.year, n.month, n.day, tzinfo=LOCAL_TZ)


def build_ad_text(sponsor_name: str, ad_text: str) -> str:
    return f"""【广告｜{sponsor_name}】
{ad_text}""".strip()


def build_keyboard(button_text: Optional[str], button_url: Optional[str]):
    if button_text and button_url:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(button_text, url=button_url)]
        ])
    return None


def is_image_http(image: str) -> bool:
    return bool(image and (image.startswith("http://") or image.startswith("https://")))


def image_file_exists(image: str) -> bool:
    return bool(image and os.path.isfile(image))


def normalize_skip(value: str) -> Optional[str]:
    value = value.strip()
    if value in {"-", "无", "不要", "不需要", "跳过", "skip"}:
        return None
    return value


def parse_group_ids(text: str):
    raw = text.replace("，", ",").replace("\n", ",")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except Exception:
            pass
    return ids


# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    text = """广告投放机器人已启动。

最快方式：

/quick_ad

机器人会一步步问你：
广告主、广告文案、按钮、图片、投放群、间隔、每日上限。

常用命令：

/chatid
/add_sponsor 广告主名称
/list_sponsors
/list_ads
/list_groups
/delete_sponsor_ads 广告主名称
/delete_ads_here
/delete_all_ads
/status

群里应急命令：
/delete_ads_here
/pause_here
/resume_here
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
        "第 5 步：请输入图片 URL 或服务器图片路径。\n\n"
        "例如：https://example.com/ad.jpg\n\n"
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
        "第 5 步：请输入图片 URL 或服务器图片路径。\n\n"
        "例如：https://example.com/ad.jpg\n\n"
        "不需要图片就输入：-"
    )
    return QA_IMAGE


async def quick_ad_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image = normalize_skip(update.message.text)
    context.user_data["quick_ad"]["image"] = image

    await update.message.reply_text(
        "第 6 步：请输入要投放的群 ID。\n\n"
        "多个群用逗号隔开。\n\n"
        "例如：\n"
        "-1001234567890,-1009876543210\n\n"
        "不知道群 ID，就把机器人拉进群，在群里发 /chatid。"
    )
    return QA_GROUPS


async def quick_ad_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_ids = parse_group_ids(update.message.text)

    if not group_ids:
        await update.message.reply_text(
            "没有识别到有效群 ID，请重新输入。\n\n"
            "格式示例：-1001234567890,-1009876543210"
        )
        return QA_GROUPS

    context.user_data["quick_ad"]["group_ids"] = group_ids

    await update.message.reply_text(
        f"已识别 {len(group_ids)} 个群。\n\n"
        "第 7 步：请输入每个群多久发一次广告，单位分钟。\n\n"
        f"直接回车不行，建议输入：{DEFAULT_GROUP_INTERVAL_MINUTES}"
    )
    return QA_INTERVAL


async def quick_ad_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("请输入数字，例如：60")
        return QA_INTERVAL

    interval = int(raw)
    if interval < 5:
        await update.message.reply_text("间隔太短，建议至少 5 分钟。请重新输入。")
        return QA_INTERVAL

    context.user_data["quick_ad"]["interval"] = interval

    await update.message.reply_text(
        "第 8 步：请输入每个群每天最多发几条广告。\n\n"
        f"建议输入：{DEFAULT_GROUP_DAILY_LIMIT}"
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

    image_info = data.get("image") or "无"

    preview = f"""请确认广告配置：

广告主：{data['sponsor']}

广告正文：
{data['text']}

按钮：{button_info}
图片：{image_info}

投放群数量：{len(data['group_ids'])}
投放群ID：{", ".join(str(x) for x in data['group_ids'])}

每群间隔：{data['interval']} 分钟
每日上限：{data['daily_limit']} 条

确认创建并开始投放请输入：确认
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
        INSERT INTO ads(sponsor_id, title, text, image, button_text, button_url, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'active')
        RETURNING id;
        """,
        (
            sponsor_row["id"],
            title,
            data["text"],
            data.get("image"),
            data.get("button_text"),
            data.get("button_url"),
        )
    )

    for gid in data["group_ids"]:
        execute(
            """
            INSERT INTO groups(chat_id, title, status, interval_minutes, daily_limit, quiet_start_hour, quiet_end_hour)
            VALUES (%s, %s, 'active', %s, %s, %s, %s)
            ON CONFLICT(chat_id) DO UPDATE SET
                status='active',
                interval_minutes=EXCLUDED.interval_minutes,
                daily_limit=EXCLUDED.daily_limit,
                quiet_start_hour=EXCLUDED.quiet_start_hour,
                quiet_end_hour=EXCLUDED.quiet_end_hour;
            """,
            (
                gid,
                f"快速投放群 {gid}",
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
        f"投放群：{len(data['group_ids'])} 个\n\n"
        f"机器人会按设置自动投放。"
    )

    context.user_data.pop("quick_ad", None)
    return ConversationHandler.END


async def quick_ad_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("quick_ad", None)
    await update.message.reply_text("已取消快速广告创建。")
    return ConversationHandler.END


# =========================
# 广告主管理
# =========================

async def add_sponsor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("用法：/add_sponsor 广告主名称")
        return

    row = execute_returning(
        """
        INSERT INTO sponsors(name)
        VALUES (%s)
        ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name
        RETURNING id, name, status;
        """,
        (name,)
    )

    await update.message.reply_text(f"已添加广告主：{row['name']}，ID：{row['id']}")


async def list_sponsors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("SELECT id, name, status FROM sponsors ORDER BY id DESC LIMIT 50;")

    if not rows:
        await update.message.reply_text("暂无广告主。")
        return

    lines = ["广告主列表："]
    for r in rows:
        lines.append(f"{r['id']}. {r['name']}｜{r['status']}")

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


# =========================
# 广告素材管理
# =========================

async def add_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    raw = update.message.text.replace("/add_ad", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) < 6:
        await update.message.reply_text(
            "用法：\n"
            "/add_ad 广告主名称 | 广告标题 | 广告正文 | 按钮文字 | 按钮链接 | 图片路径或图片URL\n\n"
            "无按钮或无图片可以填 -"
        )
        return

    sponsor_name, title, text, button_text, button_url, image = parts[:6]

    sponsor = fetch_one("SELECT id FROM sponsors WHERE name=%s;", (sponsor_name,))
    if not sponsor:
        await update.message.reply_text(f"广告主不存在：{sponsor_name}")
        return

    button_text = None if button_text == "-" else button_text
    button_url = None if button_url == "-" else button_url
    image = None if image == "-" else image

    row = execute_returning(
        """
        INSERT INTO ads(sponsor_id, title, text, button_text, button_url, image)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (sponsor["id"], title, text, button_text, button_url, image)
    )

    await update.message.reply_text(f"已添加广告素材，广告ID：{row['id']}")


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

    lines = ["广告素材列表："]
    for r in rows:
        lines.append(f"{r['id']}. [{r['sponsor_name']}] {r['title']}｜{r['status']}")

    await update.message.reply_text("\n".join(lines))


async def pause_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("用法：/pause_ad 广告ID")
        return

    ad_id = int(context.args[0])
    execute("UPDATE ads SET status='paused' WHERE id=%s;", (ad_id,))
    await update.message.reply_text(f"已暂停广告：{ad_id}")


async def resume_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("用法：/resume_ad 广告ID")
        return

    ad_id = int(context.args[0])
    execute("UPDATE ads SET status='active' WHERE id=%s;", (ad_id,))
    await update.message.reply_text(f"已恢复广告：{ad_id}")


# =========================
# 群管理
# =========================

async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    raw = update.message.text.replace("/add_group", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) < 1 or not parts[0]:
        await update.message.reply_text(
            "用法：/add_group 群ID | 群名称 | 间隔分钟 | 每日上限"
        )
        return

    chat_id = int(parts[0])
    title = parts[1] if len(parts) >= 2 and parts[1] else str(chat_id)
    interval = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else DEFAULT_GROUP_INTERVAL_MINUTES
    daily_limit = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else DEFAULT_GROUP_DAILY_LIMIT

    execute(
        """
        INSERT INTO groups(chat_id, title, interval_minutes, daily_limit, quiet_start_hour, quiet_end_hour)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=EXCLUDED.title,
            status='active',
            interval_minutes=EXCLUDED.interval_minutes,
            daily_limit=EXCLUDED.daily_limit;
        """,
        (chat_id, title, interval, daily_limit, QUIET_START_HOUR, QUIET_END_HOUR)
    )

    await update.message.reply_text(f"已添加/更新投放群：{title}｜{chat_id}")


async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = fetch_all("""
        SELECT chat_id, title, status, interval_minutes, daily_limit
        FROM groups
        ORDER BY id DESC
        LIMIT 50;
    """)

    if not rows:
        await update.message.reply_text("暂无投放群。")
        return

    lines = ["投放群列表："]
    for r in rows:
        lines.append(
            f"{r['title']}｜{r['chat_id']}｜{r['status']}｜间隔{r['interval_minutes']}分钟｜每日{r['daily_limit']}条"
        )

    await update.message.reply_text("\n".join(lines))


async def pause_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("用法：/pause_group 群ID")
        return

    chat_id = int(context.args[0])
    execute("UPDATE groups SET status='paused' WHERE chat_id=%s;", (chat_id,))
    await update.message.reply_text(f"已暂停群投放：{chat_id}")


async def resume_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("用法：/resume_group 群ID")
        return

    chat_id = int(context.args[0])
    execute("UPDATE groups SET status='active' WHERE chat_id=%s;", (chat_id,))
    await update.message.reply_text(f"已恢复群投放：{chat_id}")


async def pause_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    chat_id = update.effective_chat.id
    execute("UPDATE groups SET status='paused' WHERE chat_id=%s;", (chat_id,))
    await update.message.reply_text("已暂停当前群投放。")


async def resume_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    chat_id = update.effective_chat.id
    execute("UPDATE groups SET status='active' WHERE chat_id=%s;", (chat_id,))
    await update.message.reply_text("已恢复当前群投放。")


# =========================
# 删除广告
# =========================

async def delete_message_safe(bot, chat_id, message_id) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print(f"删除失败 chat={chat_id} msg={message_id}: {e}")
        return False


async def delete_sponsor_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    sponsor_name = " ".join(context.args).strip()
    if not sponsor_name:
        await update.message.reply_text("用法：/delete_sponsor_ads 广告主名称")
        return

    rows = fetch_all("""
        SELECT ad_messages.id, ad_messages.chat_id, ad_messages.message_id
        FROM ad_messages
        JOIN sponsors ON sponsors.id = ad_messages.sponsor_id
        WHERE sponsors.name=%s AND ad_messages.deleted=FALSE
        ORDER BY ad_messages.id DESC;
    """, (sponsor_name,))

    if not rows:
        await update.message.reply_text(f"没有找到该广告主未删除的广告记录：{sponsor_name}")
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["chat_id"], r["message_id"])
        if ok:
            ok_count += 1
            execute(
                "UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;",
                (r["id"],)
            )
        else:
            fail_count += 1
        await asyncio.sleep(0.15)

    await update.message.reply_text(
        f"广告主【{sponsor_name}】清理完成。\n成功：{ok_count}\n失败：{fail_count}"
    )


async def delete_ads_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    chat_id = update.effective_chat.id

    rows = fetch_all("""
        SELECT id, chat_id, message_id
        FROM ad_messages
        WHERE chat_id=%s AND deleted=FALSE
        ORDER BY id DESC;
    """, (chat_id,))

    if not rows:
        await update.message.reply_text("当前群没有未删除的广告记录。")
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["chat_id"], r["message_id"])
        if ok:
            ok_count += 1
            execute(
                "UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;",
                (r["id"],)
            )
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
        SELECT id, chat_id, message_id
        FROM ad_messages
        WHERE deleted=FALSE
        ORDER BY id DESC;
    """)

    if not rows:
        await update.message.reply_text("没有未删除的广告记录。")
        return

    ok_count = 0
    fail_count = 0

    for r in rows:
        ok = await delete_message_safe(context.bot, r["chat_id"], r["message_id"])
        if ok:
            ok_count += 1
            execute(
                "UPDATE ad_messages SET deleted=TRUE, deleted_at=NOW() WHERE id=%s;",
                (r["id"],)
            )
        else:
            fail_count += 1
        await asyncio.sleep(0.15)

    await update.message.reply_text(
        f"全部广告清理完成。\n成功：{ok_count}\n失败：{fail_count}"
    )


# =========================
# 状态
# =========================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    sponsors = fetch_one("SELECT COUNT(*) AS c FROM sponsors;")["c"]
    active_sponsors = fetch_one("SELECT COUNT(*) AS c FROM sponsors WHERE status='active';")["c"]

    ads = fetch_one("SELECT COUNT(*) AS c FROM ads;")["c"]
    active_ads = fetch_one("SELECT COUNT(*) AS c FROM ads WHERE status='active';")["c"]

    groups = fetch_one("SELECT COUNT(*) AS c FROM groups;")["c"]
    active_groups = fetch_one("SELECT COUNT(*) AS c FROM groups WHERE status='active';")["c"]

    messages = fetch_one("SELECT COUNT(*) AS c FROM ad_messages;")["c"]
    undeleted = fetch_one("SELECT COUNT(*) AS c FROM ad_messages WHERE deleted=FALSE;")["c"]

    text = f"""机器人状态：

广告主：{sponsors} 个，启用 {active_sponsors} 个
广告素材：{ads} 条，启用 {active_ads} 条
投放群：{groups} 个，启用 {active_groups} 个
已投放记录：{messages} 条
未删除广告：{undeleted} 条

默认间隔：{DEFAULT_GROUP_INTERVAL_MINUTES} 分钟
默认每日上限：{DEFAULT_GROUP_DAILY_LIMIT} 条
静默时间：{QUIET_START_HOUR}:00 - {QUIET_END_HOUR}:00
"""
    await update.message.reply_text(text)


# =========================
# 自动投放
# =========================

def group_daily_count(chat_id: int) -> int:
    start = today_start_local()
    row = fetch_one("""
        SELECT COUNT(*) AS c
        FROM ad_messages
        WHERE chat_id=%s AND sent_at >= %s;
    """, (chat_id, start))
    return row["c"]


def group_last_sent_at(chat_id: int):
    row = fetch_one("""
        SELECT sent_at
        FROM ad_messages
        WHERE chat_id=%s
        ORDER BY sent_at DESC
        LIMIT 1;
    """, (chat_id,))
    return row["sent_at"] if row else None


def group_interval_ok(group_row) -> bool:
    last = group_last_sent_at(group_row["chat_id"])
    if not last:
        return True

    now_utc = datetime.now(timezone.utc)
    diff = now_utc - last
    return diff.total_seconds() >= int(group_row["interval_minutes"]) * 60


def pick_next_ad_for_group(chat_id: int):
    return fetch_one("""
        SELECT
            ads.id AS ad_id,
            ads.title,
            ads.text,
            ads.image,
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


async def send_ad_to_group(bot, group_row) -> bool:
    ad = pick_next_ad_for_group(group_row["chat_id"])
    if not ad:
        print("没有可投放广告")
        return False

    text = build_ad_text(ad["sponsor_name"], ad["text"])
    keyboard = build_keyboard(ad["button_text"], ad["button_url"])

    try:
        if ad["image"]:
            if is_image_http(ad["image"]):
                msg = await bot.send_photo(
                    chat_id=group_row["chat_id"],
                    photo=ad["image"],
                    caption=text,
                    reply_markup=keyboard,
                )
            elif image_file_exists(ad["image"]):
                with open(ad["image"], "rb") as f:
                    msg = await bot.send_photo(
                        chat_id=group_row["chat_id"],
                        photo=f,
                        caption=text,
                        reply_markup=keyboard,
                    )
            else:
                print(f"图片不存在，改发文字：{ad['image']}")
                msg = await bot.send_message(
                    chat_id=group_row["chat_id"],
                    text=text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
        else:
            msg = await bot.send_message(
                chat_id=group_row["chat_id"],
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        execute(
            """
            INSERT INTO ad_messages(sponsor_id, ad_id, chat_id, message_id)
            VALUES (%s, %s, %s, %s);
            """,
            (ad["sponsor_id"], ad["ad_id"], group_row["chat_id"], msg.message_id)
        )

        print(f"已投放广告：group={group_row['chat_id']} ad={ad['ad_id']}")
        return True

    except Exception as e:
        print(f"投放失败 group={group_row['chat_id']}: {e}")
        return False


async def ad_loop(app: Application):
    await asyncio.sleep(5)

    while True:
        try:
            groups = fetch_all("""
                SELECT *
                FROM groups
                WHERE status='active'
                ORDER BY id ASC;
            """)

            sends = 0

            for g in groups:
                if sends >= MAX_SENDS_PER_LOOP:
                    break

                if is_quiet_time(g):
                    print(f"静默时间，跳过群：{g['chat_id']}")
                    continue

                if group_daily_count(g["chat_id"]) >= int(g["daily_limit"]):
                    print(f"达到每日上限，跳过群：{g['chat_id']}")
                    continue

                if not group_interval_ok(g):
                    print(f"间隔未到，跳过群：{g['chat_id']}")
                    continue

                ok = await send_ad_to_group(app.bot, g)
                if ok:
                    sends += 1
                    await asyncio.sleep(1.5)

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
            QA_IMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_image)],
            QA_GROUPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_groups)],
            QA_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_interval)],
            QA_DAILY_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_daily_limit)],
            QA_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_confirm)],
        },
        fallbacks=[CommandHandler("cancel", quick_ad_cancel)],
    )

    app.add_handler(quick_ad_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))

    app.add_handler(CommandHandler("add_sponsor", add_sponsor))
    app.add_handler(CommandHandler("list_sponsors", list_sponsors))
    app.add_handler(CommandHandler("pause_sponsor", pause_sponsor))
    app.add_handler(CommandHandler("resume_sponsor", resume_sponsor))

    app.add_handler(CommandHandler("add_ad", add_ad))
    app.add_handler(CommandHandler("list_ads", list_ads))
    app.add_handler(CommandHandler("pause_ad", pause_ad))
    app.add_handler(CommandHandler("resume_ad", resume_ad))

    app.add_handler(CommandHandler("add_group", add_group))
    app.add_handler(CommandHandler("list_groups", list_groups))
    app.add_handler(CommandHandler("pause_group", pause_group))
    app.add_handler(CommandHandler("resume_group", resume_group))
    app.add_handler(CommandHandler("pause_here", pause_here))
    app.add_handler(CommandHandler("resume_here", resume_here))

    app.add_handler(CommandHandler("delete_sponsor_ads", delete_sponsor_ads))
    app.add_handler(CommandHandler("delete_ads_here", delete_ads_here))
    app.add_handler(CommandHandler("delete_all_ads", delete_all_ads))

    app.add_handler(CommandHandler("status", status))

    print("广告投放机器人启动成功，已启用快速广告模式 /quick_ad")
    app.run_polling()


if __name__ == "__main__":
    main()
