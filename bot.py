import os
import json
import threading
import logging
import asyncio
import base64
import urllib.parse
import requests
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq

# Intenta importar PDF, si no está no crashea
try:
    import PyPDF2
    PDF_AVAILABLE = True
except:
    PDF_AVAILABLE = False

# --- FLASK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot V12 ULTRA Online"
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
threading.Thread(target=run_flask, daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
groq_client = Groq(api_key=GROQ_API_KEY)

# --- MEMORIA PERMANENTE ---
MEMORY_FILE = "memorias.json"
user_memories = {}

def cargar_memorias():
    global user_memories
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                user_memories = json.load(f)
                # convertir keys str a int
                user_memories = {int(k): v for k, v in user_memories.items()}
            print(f"Memorias cargadas: {len(user_memories)} usuarios")
        except Exception as e:
            print(f"Error cargando memorias: {e}")
            user_memories = {}

def guardar_memorias():
    try:
        # convertir keys int a str para json
        data = {str(k): v[-20:] for k, v in user_memories.items()} # guardamos solo últimos 20
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error guardando: {e}")

cargar_memorias()

def get_memory(uid):
    if uid not in user_memories:
        user_memories[uid] = []
    return user_memories[uid]

async def enviar_largo(update, texto):
    if len(texto) <= 4000:
        await update.message.reply_text(texto)
        return
    for i in range(0, len(texto), 4000):
        await update.message.reply_text(texto[i:i+4000])
        await asyncio.sleep(0.2)

# --- 1. AUTOCORRECTOR POLIGLOTA ---
def corregir_y_entender(texto_usuario):
    if not texto_usuario or len(texto_usuario.strip()) < 2:
        return texto_usuario
    try:
        comp = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": "You are multilingual typo corrector. Fix spelling but KEEP language. 'Halo'->'Hola', 'Haor que puedes acer haora'->'Ahora que puedes hacer ahora', 'ingies'->'inglés', 'wat can u doo'->'what can you do', 'buska'->'busca'. Only corrected text."},
                {"role": "user", "content": texto_usuario}
            ],
            max_tokens=200, temperature=0.2
        )
        return comp.choices[0].message.content.strip()
    except:
        return texto_usuario

def extraer_prompt_imagen(texto):
    t = texto.lower()
    frases = ["creame una imagen de", "crea una imagen de", "hazme una imagen de", "genera una imagen de", "creame una imagen", "crea una imagen", "imagen de", "dibuja un", "dibuja una", "dibuja", "crea foto de", "create an image of"]
    prompt = texto
    for f in frases:
        if f in t:
            idx = t.find(f) + len(f)
            prompt = texto[idx:].strip()
            break
    if prompt.lower().startswith("de "): prompt = prompt[3:].strip()
    return prompt

def es_pedido_imagen(texto):
    tl = texto.lower().strip()
    if len(tl) < 5: return False
    return any(k in tl for k in ["crea una imagen", "creame una imagen", "hazme una imagen", "genera una imagen", "imagen de", "dibuja un", "dibuja una"])

# --- 2. BUSQUEDA REAL EN INTERNET ---
def buscar_en_internet(query):
    try:
        # DuckDuckGo instant
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        r = requests.get(url, timeout=10).json()
        resultado = ""
        if r.get("AbstractText"):
            resultado += r["AbstractText"] + "\n"
        if r.get("RelatedTopics"):
            for topic in r["RelatedTopics"][:2]:
                if isinstance(topic, dict) and "Text" in topic:
                    resultado += topic["Text"] + "\n"
        if not resultado:
            # fallback: buscar en wikipedia
            resultado = f"No encontré resumen directo, pero busqué: {query}"
        return resultado.strip()[:1500]
    except Exception as e:
        return f"Error buscando: {e}"

# --- 3. CREAR IMAGEN (FIX) ---
async def crear_imagen(update: Update, prompt_original: str):
    try:
        prompt_limpio = extraer_prompt_imagen(prompt_original)
        await update.message.reply_text(f"Ya bro, creando PRO: '{prompt_limpio}'... 🎨")
        try:
            comp = groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": "Convert to detailed ENGLISH prompt photorealistic. Add: indoor, photorealistic, no text, no words, no signs, 8k. Only english."},
                    {"role": "user", "content": prompt_limpio}
                ],
                max_tokens=150, temperature=0.7
            )
            prompt_en = comp.choices[0].message.content.strip()
        except:
            prompt_en = f"{prompt_limpio} photorealistic, indoor, no text, 8k"
        encoded = urllib.parse.quote(prompt_en)
        seed = int(asyncio.get_event_loop().time())
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&enhance=false&nologo=true&seed={seed}"
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        bio = BytesIO(r.content)
        bio.name = "imagen.jpg"
        await update.message.reply_photo(photo=bio, caption=f"Listo bro 🔥 {prompt_limpio}")
    except Exception as e:
        await update.message.reply_text(f"Error imagen bro: {e}")

# --- COMANDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"¡Qué onda {update.effective_user.first_name} bro! 🚀 V12 ULTRA\n"
        "✅ Entiendo mal escrito: Halo->Hola\n"
        "✅ Cualquier idioma + traductor\n"
        "✅ Imágenes PRO\n"
        "✅ 🎤 Audios: mándame nota de voz\n"
        "✅ 🔍 Busco en internet: 'busca quién ganó ayer'\n"
        "✅ 📄 Leo PDFs: mándame un PDF\n"
        "✅ 💾 Memoria permanente\n"
        "✅ 👁️ Veo fotos\n\n"
        "Dime bro, ¿qué hacemos?"
    )

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_memories[update.effective_user.id] = []
    guardar_memorias()
    await update.message.reply_text("Memoria limpia bro 🧹")

async def imagen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /imagen un gato gamer")
        return
    await crear_imagen(update, " ".join(context.args))

async def buscar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usa: /buscar quien ganó la champions")
        return
    query = " ".join(context.args)
    await update.message.reply_text(f"Buscando bro: {query}... 🔍")
    res = buscar_en_internet(query)
    await update.message.reply_text(f"Encontré bro:\n{res}")

# --- IA PRINCIPAL ---
async def ia_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    texto_original = update.message.text or ""
    texto_corregido = corregir_y_entender(texto_original)

    if es_pedido_imagen(texto_corregido):
        await crear_imagen(update, texto_corregido)
        return

    t_low = texto_corregido.lower()
    # Detectar búsqueda
    if any(x in t_low for x in ["busca", "busca en internet", "search", "quien gano", "que paso", "noticias de"]):
        await update.message.reply_text(f"Buscando eso bro: {texto_corregido} 🔍")
        info = buscar_en_internet(texto_corregido)
        texto_corregido = f"Con esta info de internet: {info}\n\nPregunta del usuario: {texto_corregido}. Responde como pata peruano usando la info."

    es_tradu = any(x in t_low for x in ["traduce", "traduccion", "translate", "traduceme"])

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        memoria = get_memory(user_id)
        memoria.append({"role": "user", "content": texto_corregido})
        if len(memoria) > 14: memoria = memoria[-14:]
        user_memories[user_id] = memoria
        guardar_memorias()

        if es_tradu:
            system_prompt = "Eres traductor PRO políglota. Traduce rápido y da ejemplo."
            temp = 0.3
        else:
            system_prompt = (
                "Eres Meta AI con flow peruano bro, políglota. Respondes en mismo idioma del usuario. "
                "Entiendes errores tipo Halo=Hola, ingies=inglés. Eres IA 100% funcional: chat, imágenes, voz, buscar internet, leer PDFs. "
                "Si te dan info de internet, úsala. Sé natural, no robótico."
            )
            temp = 0.85

        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "system", "content": system_prompt}, *memoria],
            temperature=temp, max_tokens=2000
        )
        respuesta = completion.choices[0].message.content
        memoria.append({"role": "assistant", "content": respuesta})
        user_memories[user_id] = memoria
        guardar_memorias()
        await enviar_largo(update, respuesta)
    except Exception as e:
        await update.message.reply_text(f"Ups bro error: {e}")

# --- AUDIO (VOZ) ---
async def voz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("Escuchando tu audio bro 🎧...")
        voice_file = await update.message.voice.get_file()
        buf = BytesIO()
        await voice_file.download_to_memory(buf)
        buf.seek(0)
        # Transcribir con Groq Whisper
        transcription = groq_client.audio.transcriptions.create(
            file=("audio.ogg", buf.read()),
            model="whisper-large-v3",
            language="es",
            response_format="text"
        )
        texto_transcrito = str(transcription).strip()
        await update.message.reply_text(f"Te entendí bro: '{texto_transcrito}'")
        # Ahora lo procesamos como texto normal
        update.message.text = texto_transcrito
        await ia_reply(update, context)
    except Exception as e:
        logging.error(f"Error voz: {e}")
        await update.message.reply_text(f"Error con tu audio bro: {e} - intenta con audio más corto")

# --- PDFs ---
async def documento_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc.file_name.lower().endswith(".pdf"):
            await update.message.reply_text("Solo leo PDFs por ahora bro 📄")
            return
        if not PDF_AVAILABLE:
            await update.message.reply_text("Falta librería PyPDF2 bro, añádela a requirements.txt")
            return
        await update.message.reply_text(f"Leyendo PDF bro: {doc.file_name} 📖...")
        file = await doc.get_file()
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        reader = PyPDF2.PdfReader(buf)
        texto_pdf = ""
        for page in reader.pages[:5]: # solo 5 primeras páginas
            texto_pdf += page.extract_text() + "\n"
        texto_pdf = texto_pdf[:4000] # limitar
        if not texto_pdf.strip():
            await update.message.reply_text("No pude leer texto de ese PDF bro")
            return
        # Resumir
        comp = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": "Eres resumidor PRO. Resume PDF en español peruano, con puntos clave, breve y útil."},
                {"role": "user", "content": f"Resume este PDF:\n{texto_pdf}"}
            ],
            max_tokens=1000, temperature=0.5
        )
        resumen = comp.choices[0].message.content
        await enviar_largo(update, f"📄 Resumen de {doc.file_name}:\n\n{resumen}")
    except Exception as e:
        await update.message.reply_text(f"Error leyendo PDF bro: {e}")

# --- FOTOS ---
async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = update.message.caption or ""
        if es_pedido_imagen(caption):
            await crear_imagen(update, caption); return
        photo_file = await update.message.photo[-1].get_file()
        buf = BytesIO(); await photo_file.download_to_memory(buf); buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode('utf-8')
        comp = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": [{"type": "text", "text": caption or "Describe esta imagen como mi pata peruano."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
            max_tokens=700
        )
        await enviar_largo(update, comp.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Error foto bro: {e}")

# --- MAIN ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("imagen", imagen_cmd))
    app.add_handler(CommandHandler("buscar", buscar_cmd))
    app.add_handler(MessageHandler(filters.VOICE, voz_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, documento_handler))
    app.add_handler(MessageHandler(filters.PHOTO, foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ia_reply))
    print("Bot V12 ULTRA iniciado 🔥")
    app.run_polling()
