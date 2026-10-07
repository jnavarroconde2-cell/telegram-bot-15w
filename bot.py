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

flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot IA V4 PRO - Programador + Imagenes PRO"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

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

async def enviar_largo(update, texto):
    if len(texto) <= 4000:
        await update.message.reply_text(texto)
        return
    for i in range(0, len(texto), 4000):
        await update.message.reply_text(texto[i:i+4000])
        await asyncio.sleep(0.3)

IMG_KEYWORDS = ["crea una imagen", "creame una imagen", "hazme una imagen", "genera una imagen", "imagen de", "dibuja", "crea una foto"]
def es_pedido_imagen(texto):
    t = texto.lower()
    return any(k in t for k in IMG_KEYWORDS)

def extraer_prompt_imagen(texto):
    t = texto.lower()
    for k in IMG_KEYWORDS:
        if k in t and len(k) > 5:
            idx = t.find(k) + len(k)
            p = texto[idx:].strip()
            if len(p) > 2:
                return p
    return texto

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola {update.effective_user.first_name} bro! 🔥\nBot V4 PRO:\n1. Hablo normal y recuerdo\n2. Creo imágenes PRO con lógica\n3. Analizo fotos\n4. Te doy código PRO senior\nUsa: creame una imagen de...\n/clear limpia memoria")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_memories[update.effective_user.id] = []
    await update.message.reply_text("Memoria limpia bro 🧹")

# --- IMAGENES PRO CON MEJORADOR GRATIS ---
async def crear_imagen(update: Update, prompt: str):
    try:
        await update.message.reply_text(f"Ya bro, mejorando tu idea: '{prompt}'...")

        # 1. Mejoramos el prompt con Groq gratis (100 tokens nada mas)
        try:
            comp = groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": "Eres experto en prompts para FLUX. Convierte la idea del usuario a un prompt en INGLES, ultra detallado, cinematografico, 8k, ultra realistic, highly detailed, sharp focus. Ejemplo: 'goku programador' -> 'Goku from Dragon Ball Z as a professional programmer sitting at modern RGB gaming setup with 3 monitors showing code, wearing black hoodie, drinking coffee, neon lights, ultra detailed, 8k, cinematic lighting'. Solo devuelve el prompt en ingles, nada mas."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=150,
                temperature=0.8
            )
            prompt_en = comp.choices[0].message.content.strip()
        except:
            prompt_en = prompt

        print(f"Original: {prompt} | Mejorado: {prompt_en}")
        encoded = urllib.parse.quote(prompt_en)
        seed = int(asyncio.get_event_loop().time())
        # flux + enhance=true = mucha mejor logica que turbo
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&enhance=true&nologo=true&seed={seed}&referrer=bot"
        await update.message.reply_photo(photo=url, caption=f"Listo bro 🔥\n{prompt}")
    except Exception as e:
        logging.error(f"Error imagen: {e}")
        try:
            # Reintento rapido si flux falla
            encoded = urllib.parse.quote(prompt)
            url2 = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&nologo=true"
            await update.message.reply_photo(photo=url2, caption=f"Listo bro (reintento) 👉 {prompt}")
        except Exception as e2:
            await update.message.reply_text(f"Bro falló imagen: {e2}")

async def imagen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /imagen goku programando en su pc")
        return
    prompt = " ".join(context.args)
    await crear_imagen(update, prompt)

async def ia_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        texto = update.message.text or ""
        if es_pedido_imagen(texto):
            await crear_imagen(update, extraer_prompt_imagen(texto))
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
                {"role": "system", "content": "Eres una IA normal, mi pata de Lima Perú, hablas como causa, dices 'bro' natural. Eres útil y directo. HABILIDADES: 1) Conversas normal 2) Creas imágenes PRO 3) Tienes memoria. MODO PROGRAMADOR PRO: Cuando pidan código, eres Senior 10 años, das código limpio, optimizado, comentado, producción. Si es largo dividelo con titulos. Web=responsive moderno. Python=con try/except. Explica en 2 líneas antes."},
                *memoria
            ],
            temperature=0.8,
            max_tokens=2000
        )
        respuesta = completion.choices[0].message.content
        memoria.append({"role": "assistant", "content": respuesta})
        user_memories[user_id] = memoria
        await enviar_largo(update, respuesta)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"Error bro: {e}")

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
            messages=[{"role": "user", "content": [{"type": "text", "text": caption if caption else "Que ves? Describe como peruano bro."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
            max_tokens=600
        )
        await enviar_largo(update, completion.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Error foto bro: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("imagen", imagen_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    print("Bot V4 PRO iniciado 🔥")
    app.run_polling()
