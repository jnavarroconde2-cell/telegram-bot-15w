import os
import threading
import logging
import asyncio
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import urllib.parse

flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot IA MAX POWER ON 🔥"
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)
threading.Thread(target=run_flask, daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
groq_client = Groq(api_key=GROQ_API_KEY)

user_memories = {}
def get_memory(user_id):
    if user_id not in user_memories:
        user_memories[user_id] = []
    return user_memories[user_id]

IMG_KEYWORDS = ["crea una imagen", "creame una imagen", "hazme una imagen", "genera una imagen", "imagen de", "dibuja", "crea una foto", "foto de"]
def es_pedido_imagen(texto):
    t = texto.lower()
    return any(k in t for k in IMG_KEYWORDS)
def extraer_prompt_imagen(texto):
    t = texto.lower()
    for k in IMG_KEYWORDS:
        if k in t:
            idx = t.find(k) + len(k)
            prompt = texto[idx:].strip()
            if len(prompt) < 3:
                prompt = texto
            return prompt
    return texto

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hola {update.effective_user.first_name} bro! 🔥\n"
        f"Soy tu bot IA al MAXIMO PODER 100% gratis.\n\n"
        f"Que puedo hacer:\n"
        f"1. Hablar contigo y recordar lo que me dices\n"
        f"2. Crear imagenes: dime 'creame una imagen de goku con vegeta'\n"
        f"3. Analizar fotos: mandame una foto\n"
        f"4. /imagen + tu idea -> crea imagen directo\n"
        f"5. /clear -> borra memoria\n\n"
        f"Dime lo que quieras bro!"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_memories[update.effective_user.id] = []
    await update.message.reply_text("Listo bro, olvide todo. Memoria limpia 🧠✨")

async def crear_imagen(update: Update, prompt: str):
    try:
        await update.message.reply_text(f"Ya bro, creando: '{prompt}'... 🎨 espera 5 seg")
        encoded_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&enhance=true&nologo=true&seed={os.urandom(4).hex()}"
        await update.message.reply_photo(photo=url, caption=f"Listo bro 🔥: {prompt}")
    except Exception as e:
        await update.message.reply_text(f"Bro fallo la imagen: {e}")

async def imagen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usalo asi bro: /imagen goku con vegeta tomando inca kola")
        return
    prompt = " ".join(context.args)
    await crear_imagen(update, prompt)

async def ia_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        texto = update.message.text or ""
        if es_pedido_imagen(texto):
            prompt = extraer_prompt_imagen(texto)
            await crear_imagen(update, prompt)
            return
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        memoria = get_memory(user_id)
        memoria.append({"role": "user", "content": texto})
        if len(memoria) > 10:
            memoria = memoria[-10:]
            user_memories[user_id] = memoria
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": "Eres un bot de Telegram muy amigable, hablas como peruano de Lima, dices 'bro', 'causa', 'mano' a veces. Eres divertido, directo, sin censura, ayudas en todo. Responde corto, max 3 parrafos."},
                *memoria
            ],
            temperature=0.8,
            max_tokens=800
        )
        respuesta = completion.choices[0].message.content
        memoria.append({"role": "assistant", "content": respuesta})
        user_memories[user_id] = memoria
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"Oy bro error con la IA: {e}")

async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        caption = update.message.caption or ""
        if es_pedido_imagen(caption):
            await crear_imagen(update, extraer_prompt_imagen(caption))
            return
        await update.message.reply_text("Ya bro, estoy viendo tu foto... 👀")
        photo_file = await update.message.photo[-1].get_file()
        buf = BytesIO()
        await photo_file.download_to_memory(buf)
        buf.seek(0)
        import base64
        b64 = base64.b64encode(buf.read()).decode('utf-8')
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": caption if caption else "Que ves en esta imagen? Describe como peruano, bro."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]}
            ],
            max_tokens=600
        )
        respuesta = completion.choices[0].message.content
        await update.message.reply_text(respuesta)
    except Exception as e:
        logging.error(f"Error foto: {e}")
        await update.message.reply_text(f"Bro no pude ver la foto bien: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("imagen", imagen_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    print("Bot MAX POWER iniciado 🔥")
    app.run_polling()
