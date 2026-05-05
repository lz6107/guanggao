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
    QA_TARGETS,
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
        target TEXT NOT NULL,
        actual_chat_id TEXT,
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


def today_start_local() -> datetime:
    n = now_local()
    return datetime(n.year, n.month, n.day, tzinfo=LOCAL_TZ)


def is_quiet_time(target_row) -> bool:
    current_hour = now_local().hour
    start = int(target_row["quiet_start_hour"])
    end = int(target_row["quiet_end_hour"])

    if start < end:
        return start <= current_hour < end

    return current_hour >= start or current_hour < end


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

    # 数字群ID，例如 -1001234567890
    if raw.startswith("-") and raw[1:].isdigit():
        return raw

    # 纯数字也允许
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


# =========================
# /start /chatid
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    text = """广告投放机器人已启动。

最快使用方式：

/quick_ad

快速广告模式支持：
1. 直接发图片给机器人
2. 投放目标支持 @频道名 / @群名 / -100群ID
3. 多个目标用逗号分隔
4. 自动保存广告主、广告素材、投放目标
5. 支持按广告主一键删除所有广告

常用命令：
/quick_ad
/chatid
/list_sponsors
/list_ads
/list_targets
/delete_sponsor_ads 广告主名称
/delete_ads_here
/delete_all_ads
/status

群里应急命令：
/chatid
/delete_ads_here
/pause_here
/resume_here

取消快速广告：
/cancel
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
        "支持：\n"
        "@频道用户名\n"
        "@公开群用户名\n"
        "-100数字群ID\n\n"
        "多个目标用逗号隔开。\n\n"
        "例如：\n"
        "@a123,-1001234567890,@b456"
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
        "支持：\n"
        "@频道用户名\n"
        "@公开群用户名\n"
        "-100数字群ID\n\n"
        "多个目标用逗号隔开。\n\n"
        "例如：\n"
        "@a123,-1001234567890,@b456"
    )
    return QA_TARGETS


async def quick_ad_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    targets = parse_targets(update.message.text)

    if not targets:
        await update.message.reply_text(
            "没有识别到有效投放目标，请重新输入。\n\n"
            "支持示例：\n"
            "@a123\n"
            "@a123,-1001234567890,@b456"
        )
        return QA_TARGETS

    context.user_data["quick_ad"]["targets"] = targets

    await update.message.reply_text(
        f"已识别 {len(targets)} 个投放目标。\n\n"
        "第 7 步：请输入每个目标多久发一次广告，单位分钟。\n\n"
        f"建议输入：{DEFAULT_GROUP_INTERVAL_MINUTES}"
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
        "第 8 步：请输入每个目标每天最多发几条广告。\n\n"
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

    image_info = "无"
    if data.get("image_type") == "telegram_file_id":
        image_info = "已上传 Telegram 图片"
    elif data.get("image_type") == "url":
        image_info = data.get("image_value")
    elif data.get("image_type") == "local":
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

每个目标间隔：{data['interval']} 分钟
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
                quiet_start_hour, quiet_end_hour
            )
            VALUES (%s, %s, 'active', %s, %s, %s, %s)
            ON CONFLICT(target) DO UPDATE SET
                status='active',
                interval_minutes=EXCLUDED.interval_minutes,
                daily_limit=EXCLUDED.daily_limit,
                quiet_start_hour=EXCLUDED.quiet_start_hour,
                quiet_end_hour=EXCLUDED.quiet_end_hour;
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
        f"机器人会按设置自动投放。"
    )

    context.user_data.pop("quick_ad", None)
    return ConversationHandler.END


async def quick_ad_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("quick_ad", None)
    await update.message.reply_text("已取消快速广告创建。")
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
        SELECT target, title, status, interval_minutes, daily_limit
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
            f"{r['target']}｜{r['status']}｜间隔{r['interval_minutes']}分钟｜每日{r['daily_limit']}条"
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


async def pause_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    target = str(update.effective_chat.id)
    execute("UPDATE targets SET status='paused' WHERE target=%s;", (target,))
    await update.message.reply_text("已暂停当前群投放。")


async def resume_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    target = str(update.effective_chat.id)
    execute("UPDATE targets SET status='active' WHERE target=%s;", (target,))
    await update.message.reply_text("已恢复当前群投放。")


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
        ORDER BY ad_messages.id DESC;
    """, (sponsor_name,))

    if not rows:
        await update.message.reply_text(f"没有找到该广告主未删除的广告记录：{sponsor_name}")
        return

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

    await update.message.reply_text(
        f"广告主【{sponsor_name}】清理完成。\n成功：{ok_count}\n失败：{fail_count}"
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
        ORDER BY id DESC;
    """, (actual_chat_id,))

    if not rows:
        await update.message.reply_text("当前群没有未删除的广告记录。")
        return

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
        ORDER BY id DESC;
    """)

    if not rows:
        await update.message.reply_text("没有未删除的广告记录。")
        return

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

    targets = fetch_one("SELECT COUNT(*) AS c FROM targets;")["c"]
    active_targets = fetch_one("SELECT COUNT(*) AS c FROM targets WHERE status='active';")["c"]

    messages = fetch_one("SELECT COUNT(*) AS c FROM ad_messages;")["c"]
    undeleted = fetch_one("SELECT COUNT(*) AS c FROM ad_messages WHERE deleted=FALSE;")["c"]

    text = f"""机器人状态：

广告主：{sponsors} 个，启用 {active_sponsors} 个
广告素材：{ads} 条，启用 {active_ads} 条
投放目标：{targets} 个，启用 {active_targets} 个
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

def target_daily_count(target: str) -> int:
    start = today_start_local()
    row = fetch_one("""
        SELECT COUNT(*) AS c
        FROM ad_messages
        WHERE target=%s AND sent_at >= %s;
    """, (target, start))
    return row["c"]


def target_last_sent_at(target: str):
    row = fetch_one("""
        SELECT sent_at
        FROM ad_messages
        WHERE target=%s
        ORDER BY sent_at DESC
        LIMIT 1;
    """, (target,))
    return row["sent_at"] if row else None


def target_interval_ok(target_row) -> bool:
    last = target_last_sent_at(target_row["target"])

    if not last:
        return True

    now_utc = datetime.now(timezone.utc)
    diff = now_utc - last
    return diff.total_seconds() >= int(target_row["interval_minutes"]) * 60


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


async def send_ad_to_target(bot, target_row) -> bool:
    ad = pick_next_ad_for_target(target_row["target"])

    if not ad:
        print("没有可投放广告")
        return False

    text = build_ad_text(ad["sponsor_name"], ad["text"])
    keyboard = build_keyboard(ad["button_text"], ad["button_url"])
    target = target_row["target"]

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

        actual_chat_id = str(msg.chat.id)

        execute(
            """
            INSERT INTO ad_messages(
                sponsor_id, ad_id, target, actual_chat_id, message_id
            )
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                ad["sponsor_id"],
                ad["ad_id"],
                target,
                actual_chat_id,
                msg.message_id,
            )
        )

        print(f"已投放广告：target={target} actual_chat_id={actual_chat_id} ad={ad['ad_id']}")
        return True

    except Exception as e:
        print(f"投放失败 target={target}: {e}")
        return False


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

                if is_quiet_time(t):
                    print(f"静默时间，跳过：{t['target']}")
                    continue

                if target_daily_count(t["target"]) >= int(t["daily_limit"]):
                    print(f"达到每日上限，跳过：{t['target']}")
                    continue

                if not target_interval_ok(t):
                    print(f"间隔未到，跳过：{t['target']}")
                    continue

                ok = await send_ad_to_target(app.bot, t)

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
            QA_SPONSOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_sponsor)
            ],
            QA_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_text)
            ],
            QA_BUTTON_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_button_text)
            ],
            QA_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_button_url)
            ],
            QA_IMAGE: [
                MessageHandler(filters.PHOTO, quick_ad_image_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_image_text),
            ],
            QA_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_targets)
            ],
            QA_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_interval)
            ],
            QA_DAILY_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_daily_limit)
            ],
            QA_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_ad_confirm)
            ],
        },
        fallbacks=[CommandHandler("cancel", quick_ad_cancel)],
    )

    app.add_handler(quick_ad_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))

    app.add_handler(CommandHandler("list_sponsors", list_sponsors))
    app.add_handler(CommandHandler("list_ads", list_ads))
    app.add_handler(CommandHandler("list_targets", list_targets))

    app.add_handler(CommandHandler("pause_sponsor", pause_sponsor))
    app.add_handler(CommandHandler("resume_sponsor", resume_sponsor))

    app.add_handler(CommandHandler("pause_here", pause_here))
    app.add_handler(CommandHandler("resume_here", resume_here))

    app.add_handler(CommandHandler("delete_sponsor_ads", delete_sponsor_ads))
    app.add_handler(CommandHandler("delete_ads_here", delete_ads_here))
    app.add_handler(CommandHandler("delete_all_ads", delete_all_ads))

    app.add_handler(CommandHandler("status", status))

    print("广告投放机器人启动成功，支持图片直传和 @用户名投放")
    app.run_polling()


if __name__ == "__main__":
    main()
