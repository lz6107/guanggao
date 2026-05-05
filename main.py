import os
import asyncio
from datetime import datetime, time as dt_time, timezone, timedelta
from typing import Optional, List

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
)


# =========================
# 基础配置
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# 管理员 Telegram ID，多个用英文逗号隔开
# 例如 ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DEFAULT_GROUP_INTERVAL_MINUTES = int(os.getenv("DEFAULT_GROUP_INTERVAL_MINUTES", "60"))
DEFAULT_GROUP_DAILY_LIMIT = int(os.getenv("DEFAULT_GROUP_DAILY_LIMIT", "8"))

# 静默时间：凌晨1点到早上8点
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "1"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "8"))

# 广告循环检查间隔，单位秒
AD_LOOP_INTERVAL_SECONDS = int(os.getenv("AD_LOOP_INTERVAL_SECONDS", "60"))

# 每次循环最多发几个群，防止瞬间刷屏
MAX_SENDS_PER_LOOP = int(os.getenv("MAX_SENDS_PER_LOOP", "3"))

# 时区，默认中国时间 UTC+8
LOCAL_TZ = timezone(timedelta(hours=int(os.getenv("LOCAL_TZ_OFFSET", "8"))))


# =========================
# 数据库
# =========================

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("缺少 DATABASE_URL，请在 Railway 里添加 PostgreSQL 并设置 DATABASE_URL")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


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
    if not user:
        return False
    return user.id in ADMIN_IDS


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

    # 跨天情况，比如 23点到8点
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
    if not image:
        return False
    return image.startswith("http://") or image.startswith("https://")


def image_file_exists(image: str) -> bool:
    if not image:
        return False
    return os.path.isfile(image)


# =========================
# /start 和帮助
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    text = """广告投放机器人已启动。

常用命令：

广告主管理：
/add_sponsor 广告主名称
/list_sponsors
/pause_sponsor 广告主名称
/resume_sponsor 广告主名称
/delete_sponsor_ads 广告主名称

广告素材：
/add_ad 广告主名称 | 广告标题 | 广告正文 | 按钮文字 | 按钮链接 | 图片路径或图片URL
/list_ads
/pause_ad 广告ID
/resume_ad 广告ID

投放群：
/chatid
/add_group 群ID | 群名称 | 间隔分钟 | 每日上限
/list_groups
/pause_group 群ID
/resume_group 群ID

清理广告：
/delete_ads_here
/delete_all_ads

状态：
/status

说明：
1. 图片可以填 URL，也可以填服务器里的本地路径。
2. 不需要图片时，图片字段填 -。
3. 不需要按钮时，按钮文字和按钮链接都填 -。
"""
    await update.message.reply_text(text)


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"当前 chat_id：{chat.id}\n标题：{chat.title or chat.full_name or ''}"
    )


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
        ON CONFLICT(name) DO UPDATE SET name = EXCLUDED.name
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
            "无按钮或无图片可以填 -\n\n"
            "例：\n"
            "/add_ad ABC交易所 | 主流币信号频道 | 实时观察BTC/ETH/SOL | 进入频道 | https://t.me/xxx | https://xxx.com/ad.jpg"
        )
        return

    sponsor_name, title, text, button_text, button_url, image = parts[:6]

    sponsor = fetch_one("SELECT id FROM sponsors WHERE name=%s;", (sponsor_name,))
    if not sponsor:
        await update.message.reply_text(f"广告主不存在：{sponsor_name}，请先 /add_sponsor")
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
            "用法：/add_group 群ID | 群名称 | 间隔分钟 | 每日上限\n\n"
            "例：/add_group -1001234567890 | 币圈交流群A | 60 | 8"
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
# 状态统计
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
# 自动投放逻辑
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

    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except Exception:
            return True

    now_utc = datetime.now(timezone.utc)
    diff = now_utc - last
    return diff.total_seconds() >= int(group_row["interval_minutes"]) * 60


def pick_next_ad_for_group(chat_id: int):
    # 随机选一条启用广告，且广告主也启用
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
        raise RuntimeError("缺少 DATABASE_URL")
    if not ADMIN_IDS:
        raise RuntimeError("缺少 ADMIN_IDS，请填你的 Telegram 用户 ID")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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

    print("广告投放机器人启动成功")
    app.run_polling()


if __name__ == "__main__":
    main()
