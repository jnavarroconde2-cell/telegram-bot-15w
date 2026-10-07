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
    return "Bot V5 PRO FIX - Imagenes con logica"

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

# EXTRACTOR NUEVO - YA NO CORTA MAL
def extraer_prompt_imagen(texto):
    t = texto.lower()
    frases = ["creame una imagen de", "crea una imagen de", "hazme una imagen de", "genera una imagen de", "creame una imagen", "crea una imagen", "hazme una imagen", "genera una imagen", "imagen de", "imagen", "crea una foto de", "dibuja"]
    prompt = texto
    for f in frases:
        if f in t:
            idx = t.find(f) + len(f)
            prompt = texto[idx:].strip()
            break
    # Limpieza del "de" "un" del inicio
    low = prompt.lower()
    if low.startswith("de "):
        prompt = prompt[3:].strip()
    if low.startswith("un "):
        prompt = prompt[3:].strip()
    if low.startswith("una "):
        prompt = prompt[4:].strip()
    return prompt if len(prompt) > 1 else texto

def es_pedido_imagen(texto):
    t = texto.lower()
    return any(k in t for k in ["crea una imagen", "creame una imagen", "hazme una imagen", "genera una imagen", "imagen de", "dibuja", "crea una foto"])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola {update.effective_user.first_name} bro! V5 FIX 🔥\nYa no genera bolas con texto.\nPrueba: creame una imagen de goku programador en su pc gamer")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_memories[update.effective_user.id] = []
    await update.message.reply_text("Memoria limpia 🧹")

async def crear_imagen(update: Update, prompt_original: str):
    try:
        prompt_limpio = extraer_prompt_imagen(prompt_original)
        if not prompt_limpio:
            prompt_limpio = prompt_original

        await update.message.reply_text(f"Ya bro, creando PRO: '{prompt_limpio}'...")

        # MEJORADOR DE PROMPT GRATIS - Pasa de español simple a inglés detallado
        try:
            comp = groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": "You are a prompt engineer for image generation. Convert the user's idea (in spanish) to a detailed ENGLISH prompt for turbo model. Make it ultra detailed, 8k, cinematic lighting, highly detailed, sharp focus. Add at the end: 'no text, no words, no letters, no watermark'. Only return the english prompt. Example: 'goku con computadora' -> 'Goku from Dragon Ball Z as a programmer sitting at a modern RGB gaming desk with 3 monitors showing code, wearing black hoodie, drinking coffee, neon lights, ultra detailed, 8k, cinematic lighting, no text, no words'"},
                    {"role": "user", "content": prompt_limpio}
                ],
                max_tokens=150,
                temperature=0.7
            )
            prompt_en = comp.choices[0].message.content.strip().replace('"','').replace("'", "")
        except:
            prompt_en = f"{prompt_limpio}, ultra detailed, 8k, cinematic, no text, no words, no watermark"

        print(f"Original: {prompt_original} | Limpio: {prompt_limpio} | EN: {prompt_en}")

        encoded = urllib.parse.quote(prompt_en)
        seed = int(asyncio.get_event_loop().time())
        # TURBO es el más estable, con prompt mejorado sale con lógica
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&nologo=true&seed={seed}"
        await update.message.reply_photo(photo=url, caption=f"Listo bro 🔥 {prompt_limpio}")
    except Exception as e:
        logging.error(f"Error imagen: {e}")
        await update.message.reply_text(f"Error bro: {e}")

async def imagen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /imagen goku programando")
        return
    await crear_imagen(update, " ".join(context.args))

async def ia_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        texto = update.message.text or ""
        if es_pedido_imagen(texto):
            await crear_imagen(update, texto)
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
                {"role": "system", "content": "Eres una IA normal, mi pata de Lima Perú, hablas como causa, dices 'bro' natural. MODO PROGRAMADOR PRO: Cuando pidan código, eres Senior 10 años, das código limpio, optimizado, comentado, producción."},
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
        await update.message.reply_text(f"Error bro: {e}")

async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        if es_pedido_imagen(caption):
            await crear_imagen(update, caption)
            return
        photo_file = await update.message.photo[-1].get_file()
        buf = BytesIO()
        await photo_file.download_to_memory(buf)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode('utf-8')
        comp = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": [{"type": "text", "text": caption if caption else "Que ves? Describe como peruano bro."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
            max_tokens=600
        )
        await enviar_largo(update, comp.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Error foto bro: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("imagen", imagen_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    print("Bot V5 FIX iniciado 🔥")
    app.run_polling()
