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
import base64

# --- SERVIDOR PARA RENDER ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot IA MAX POWER ON - Programmer Pro"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# --- CONFIG ---
logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
groq_client = Groq(api_key=GROQ_API_KEY)

# --- MEMORIA ---
user_memories = {}
def get_memory(user_id):
    if user_id not in user_memories:
        user_memories[user_id] = []
    return user_memories[user_id]

# --- FUNCION PARA MENSAJES LARGOS (PARCHE) ---
async def enviar_largo(update, texto):
    if len(texto) <= 4000:
        await update.message.reply_text(texto, parse_mode='Markdown')
        return
    # Si es muy largo lo partimos en bloques
    for i in range(0, len(texto), 4000):
        parte = texto[i:i+4000]
        await update.message.reply_text(parte, parse_mode='Markdown')
        await asyncio.sleep(0.3)

# --- DETECTOR DE IMAGENES ---
IMG_KEYWORDS = ["crea una imagen", "creame una imagen", "hazme una imagen", "genera una imagen", "imagen de", "dibuja", "crea una foto", "goku", "naruto"]
def es_pedido_imagen(texto):
    t = texto.lower()
    return any(k in t for k in IMG_KEYWORDS)

def extraer_prompt_imagen(texto):
    t = texto.lower()
    for k in IMG_KEYWORDS:
        if k in t and len(k) > 5:
            idx = t.find(k) + len(k)
            prompt = texto[idx:].strip()
            if len(prompt) > 3:
                return prompt
    return texto

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola {update.effective_user.first_name} bro! 🔥\nSoy tu bot IA normal + Programador Pro.\n1. Hablar normal\n2. Crear imágenes: 'creame una imagen de goku programador'\n3. Analizar fotos\n4. Programar: pídeme código PRO\n/clear -> borra memoria")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_memories[update.effective_user.id] = []
    await update.message.reply_text("Listo bro, memoria limpia 🧹")

async def crear_imagen(update: Update, prompt: str):
    try:
        await update.message.reply_text(f"Ya bro, creando: '{prompt}'... espera 5 seg")
        # Turbo es 3 veces más rápido que flux, evita el Timed out
        encoded_prompt = urllib.parse.quote(prompt + " high quality, 4k, detailed")
        seed = int(asyncio.get_event_loop().time())
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=turbo&nologo=true&seed={seed}"
        await update.message.reply_photo(photo=url, caption=f"Listo bro 👉 {prompt}")
    except Exception as e:
        logging.error(f"Error imagen 1: {e}")
        try:
            await asyncio.sleep(1)
            encoded_prompt = urllib.parse.quote(prompt)
            url2 = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true"
            await update.message.reply_photo(photo=url2, caption=f"Listo bro (2do intento) 👉 {prompt}")
        except Exception as e2:
            await update.message.reply_text(f"Bro falló la imagen: {e2}. Reintenta en 10 seg mano")

async def imagen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /imagen goku programando")
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
        if len(memoria) > 12:
            memoria = memoria[-12:]
        user_memories[user_id] = memoria

        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": "Eres una IA normal, mi pata de Lima, Perú. Hablas normal, dices 'bro', 'mano' de forma natural. Eres útil, directo. HABILIDADES: 1) Conversas normal 2) Creas imágenes si te piden 3) Tienes memoria. MODO PROGRAMADOR PRO: Cuando te pidan código, eres Programador Senior 10 años experiencia. Das código limpio, optimizado, comentado y listo para producción. Si es código largo, dividelo por partes con titulos claros. Explicas en 2 líneas antes del código. Web=responsive moderno. Python=con manejo de errores. Siempre código completo."},
                *memoria
            ],
            temperature=0.8,
            max_tokens=2000
        )
        respuesta = completion.choices[0].message.content
        memoria.append({"role": "assistant", "content": respuesta})
        user_memories[user_id] = memoria

        # AQUI ESTA EL PARCHE PARA QUE NO DE MESSAGE TOO LONG
        await enviar_largo(update, respuesta)

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"Oy bro error: {e}")

async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        caption = update.message.caption or ""
        if es_pedido_imagen(caption):
            await crear_imagen(update, extraer_prompt_imagen(caption))
            return
        photo_file = await update.message.photo[-1].get_file()
        buf = BytesIO()
        await photo_file.download_to_memory(buf)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode('utf-8')
        completion = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": caption if caption else "Que ves en esta imagen? Describe como peruano, bro."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]}
            ],
            max_tokens=600
        )
        respuesta = completion.choices[0].message.content
        await enviar_largo(update, respuesta)
    except Exception as e:
        logging.error(f"Error foto: {e}")
        await update.message.reply_text(f"Bro no pude ver la foto: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("imagen", imagen_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    print("Bot MAX POWER Programmer iniciado 🔥")
    app.run_polling()
