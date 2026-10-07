import os
import threading
from flask import Flask
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq

# --- Truco Render ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot IA ON"
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)
threading.Thread(target=run_flask, daemon=True).start()
# --- Fin truco ---

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola {update.effective_user.first_name} bro! 👋 Soy tu bot con IA, háblame de lo que quieras.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Solo háblame normal y te respondo con IA 🤖")

async def ia_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pregunta = update.message.text
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Eres un bot de Telegram amigable, hablas como peruano, dices 'bro' a veces, eres divertido y ayudas en todo."},
                {"role": "user", "content": pregunta}
            ]
        )
        respuesta = completion.choices[0].message.content
        await update.message.reply_text(respuesta)
    except Exception as e:
        await update.message.reply_text(f"Uy bro error con la IA: {e}")

if __name__ == "__main__":
    print("Bot IA iniciando...")
    tg_app = ApplicationBuilder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    tg_app.run_polling()
