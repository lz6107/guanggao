import os
import sqlite3
import time
from datetime import datetime, timedelta
from telegram import Bot, InputMediaPhoto
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ==== 环境变量 ====
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram Bot Token
DB_PATH = os.getenv("DB_PATH", "ads.db")  # SQLite 文件

bot = Bot(token=BOT_TOKEN)

# ==== 数据库初始化 ====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS ad_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sponsor TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        message TEXT,
        image_file TEXT,
        start_time TEXT,
        interval_min INTEGER,
        duration_days INTEGER,
        sent_count INTEGER DEFAULT 0,
        total_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending'
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ==== 添加广告 ====
def add_ad(sponsor, chat_id, message, image_file, start_time, interval_min, duration_days, total_count):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    INSERT INTO ad_messages
    (sponsor, chat_id, message, image_file, start_time, interval_min, duration_days, total_count)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (sponsor, chat_id, message, image_file, start_time, interval_min, duration_days, total_count))
    conn.commit()
    conn.close()

# ==== 删除广告 ====
def delete_sponsor_ads(update, context):
    if len(context.args) < 1:
        update.message.reply_text("用法: /delete_sponsor_ads 广告主名称")
        return
    sponsor = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM ad_messages WHERE sponsor=?", (sponsor,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    update.message.reply_text(f"已删除 {deleted} 条该广告主的广告记录")

# ==== 投放任务 ====
def send_ads_loop():
    while True:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.utcnow().isoformat()
        c.execute("SELECT * FROM ad_messages WHERE status='pending'")
        ads = c.fetchall()
        for ad in ads:
            ad_id, sponsor, chat_id, message, image_file, start_time, interval_min, duration_days, sent_count, total_count, status = ad
            start_dt = datetime.fromisoformat(start_time) if start_time else datetime.utcnow()
            end_dt = start_dt + timedelta(days=duration_days)
            if datetime.utcnow() >= start_dt and datetime.utcnow() <= end_dt:
                try:
                    if image_file:
                        bot.send_photo(chat_id=chat_id, photo=open(image_file, "rb"), caption=message)
                    else:
                        bot.send_message(chat_id=chat_id, text=message)
                    sent_count += 1
                    print(f"发送成功: 广告ID {ad_id} 已发送 {sent_count}/{total_count}")
                    if sent_count >= total_count:
                        status = 'done'
                    c.execute("""
                    UPDATE ad_messages SET sent_count=?, status=? WHERE id=?
                    """, (sent_count, status, ad_id))
                except Exception as e:
                    print(f"发送失败 广告ID {ad_id} 原因: {e}")
        conn.commit()
        conn.close()
        time.sleep(30)  # 每30秒检查一次

# ==== Telegram 命令 ====
def start(update, context: CallbackContext):
    update.message.reply_text("广告发布机器人已启动！\n/shezhi 设置广告\n/delete_sponsor_ads 删除广告")

def shezhi(update, context: CallbackContext):
    # 示例：交互式步骤可用 MessageHandler 或 ConversationHandler 实现
    update.message.reply_text("此功能可通过交互式对话创建广告，包含图片上传、投放间隔、投放期限等参数（中文提示）")

# ==== 启动机器人 ====
def main():
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("shezhi", shezhi))
    dp.add_handler(CommandHandler("delete_sponsor_ads", delete_sponsor_ads))
    
    # 投放线程
    import threading
    t = threading.Thread(target=send_ads_loop, daemon=True)
    t.start()

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()