import os
import threading
from flask import Flask
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- Truco para Render (Web Service gratis) ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot ON - funcionando"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_flask, daemon=True).start()
# --- Fin truco ---

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("No pusiste el BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola {update.effective_user.first_name} bro! 👋 Bot online!")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - Iniciar\n/help - Ayuda")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Me dijiste: {update.message.text}")

if __name__ == "__main__":
    print("Iniciando bot...")
    tg_app = ApplicationBuilder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    tg_app.run_polling()
