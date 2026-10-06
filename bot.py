import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Logging para ver que pasa en Render
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("No pusiste el BOT_TOKEN en Render!")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hola {update.effective_user.first_name} bro! 👋\n"
        "Soy tu bot y ya estoy online en Render.\n"
        "Escribe /help para ver comandos."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Iniciar\n"
        "/help - Ayuda\n"
        "Mándame cualquier texto y te respondo."
    )

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Me dijiste: {update.message.text}")

if __name__ == "__main__":
    print("Iniciando bot...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling()
