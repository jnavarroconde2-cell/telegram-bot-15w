import os, json, threading, logging, asyncio, base64, urllib.parse, requests
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq

try: import PyPDF2; PDF_AVAILABLE=True
except: PDF_AVAILABLE=False
try: from gtts import gTTS; TTS_AVAILABLE=True
except: TTS_AVAILABLE=False

flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot V13.2 Fix Keiko Vacio - Online"
def run_flask():
    port=int(os.environ.get("PORT",10000))
    flask_app.run(host="0.0.0.0",port=port)
threading.Thread(target=run_flask,daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ["BOT_TOKEN"]
GROQ_API_KEY=os.environ["GROQ_API_KEY"]
groq_client=Groq(api_key=GROQ_API_KEY)

MEMORY_FILE="memorias.json"
user_memories={}
def cargar_memorias():
    global user_memories
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE,'r',encoding='utf-8') as f:
                data=json.load(f)
                user_memories={int(k):v for k,v in data.items()}
            print(f"Memorias: {len(user_memories)}")
        except: user_memories={}
cargar_memorias()
def guardar_memorias():
    try:
        data={str(k):v[-20:] for k,v in user_memories.items()}
        with open(MEMORY_FILE,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception as e: print(f"Error guardado: {e}")
def get_memory(uid):
    if uid not in user_memories: user_memories[uid]=[]
    return user_memories[uid]
async def enviar_largo(update, texto):
    if len(texto)<=4000:
        await update.message.reply_text(texto); return
    for i in range(0,len(texto),4000):
        await update.message.reply_text(texto[i:i+4000])
        await asyncio.sleep(0.2)
async def enviar_audio(update, texto):
    if not TTS_AVAILABLE: return
    try:
        lang='en' if any(w in texto.lower() for w in ["hello","what","you"]) else 'es'
        tts=gTTS(text=texto[:400], lang=lang, tld='com.pe' if lang=='es' else 'com', slow=False)
        buf=BytesIO(); tts.write_to_fp(buf); buf.seek(0)
        await update.message.reply_voice(voice=buf, caption="🎤 Audio bro")
    except Exception as e: print(f"Error audio: {e}")

# --- FIX AUTOCORRECTOR (ESTE ERA EL BUG DE TU CAPTURA) ---
def corregir_y_entender(texto):
    if not texto or len(texto.strip())<2: return texto
    low = texto.lower()
    # Si es Keiko o tiene bandera/fuego, NO corregir para no borrar emojis
    if "keiko" in low or "🇵🇪" in texto or "🔥" in texto or "presidenta" in low:
        return texto
    if len(texto.strip()) <= 5:
        return texto
    try:
        comp=groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role":"system","content":"You are typo corrector. Fix spelling, keep language and emojis. Halo->Hola, vosa->cosa, Haor->Ahora. Only corrected text, never empty."},
                {"role":"user","content":texto}
            ],
            max_tokens=200, temperature=0.2
        )
        res=comp.choices[0].message.content.strip()
        return res if res and len(res)>2 else texto
    except: return texto

def extraer_prompt_imagen(texto):
    t=texto.lower()
    for f in ["creame una imagen de","crea una imagen de","hazme una imagen de","genera una imagen de","creame una imagen","crea una imagen","imagen de","dibuja un","dibuja una","dibuja","crea foto de","create an image of"]:
        if f in t:
            idx=t.find(f)+len(f)
            return texto[idx:].strip().lstrip("de ").strip()
    return texto
def es_pedido_imagen(texto):
    tl=texto.lower().strip()
    if len(tl)<5: return False
    return any(k in tl for k in ["crea una imagen","creame una imagen","hazme una imagen","genera una imagen","imagen de","dibuja un","dibuja una"])

def buscar_en_internet(query):
    q_lower=query.lower()
    if "keiko" in q_lower:
        query = query + " Keiko Fujimori presidenta Peru 2026"
    try:
        url=f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        r=requests.get(url,timeout=10).json()
        res=r.get("AbstractText","")
        if not res:
            for tp in r.get("RelatedTopics",[])[:3]:
                if isinstance(tp,dict) and "Text" in tp:
                    res+=tp["Text"]+"\n"
        return res[:1500] if res else f"Info buscada sobre: {query}"
    except Exception as e: return f"Buscando {query}"

async def crear_imagen(update, prompt_original):
    try:
        prompt_limpio=extraer_prompt_imagen(prompt_original)
        await update.message.reply_text(f"Ya bro, creando: '{prompt_limpio}'... 🎨")
        try:
            comp=groq_client.chat.completions.create(
                model="openai/gpt-oss-20b"
                messages=[{"role":"system","content":"Convert to detailed ENGLISH photorealistic prompt, no text. Only english."},{"role":"user","content":prompt_limpio}],
                max_tokens=150, temperature=0.7
            )
            prompt_en=comp.choices[0].message.content.strip()
        except: prompt_en=f"{prompt_limpio} photorealistic 8k no text"
        encoded=urllib.parse.quote(prompt_en)
        seed=int(asyncio.get_event_loop().time())
        url=f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&nologo=true&seed={seed}"
        r=requests.get(url,timeout=45); r.raise_for_status()
        bio=BytesIO(r.content); bio.name="imagen.jpg"
        await update.message.reply_photo(photo=bio, caption=f"Listo bro 🔥 {prompt_limpio}")
    except Exception as e: await update.message.reply_text(f"Error imagen: {e}")

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Qué onda {update.effective_user.first_name} bro! 🚀 V13.2\nFix mensaje vacío Keiko 🔥🇵🇪\nUsa /clear si venías de V12")
async def clear_cmd(update,context):
    user_memories[update.effective_user.id]=[]; guardar_memorias()
    await update.message.reply_text("Memoria limpia 🧹 ahora prueba: Que hay de bueno con keiko la presidenta de peru")
async def imagen_cmd(update,context):
    if not context.args: await update.message.reply_text("Usa: /imagen un gato"); return
    await crear_imagen(update," ".join(context.args))
async def buscar_cmd(update,context):
    if not context.args: await update.message.reply_text("Usa: /buscar keiko fujimori"); return
    q=" ".join(context.args); res=buscar_en_internet(q); await update.message.reply_text(f"Encontré:\n{res}")

async def ia_reply(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    texto_original=update.message.text or ""
    if not texto_original.strip(): return
    quiere_audio=any(x in texto_original.lower() for x in ["en audio","nota de voz","audio","con voz","hablame"])
    texto_corregido=corregir_y_entender(texto_original)
    print(f"TXT: '{texto_original}' -> '{texto_corregido}'")
    if es_pedido_imagen(texto_corregido):
        await crear_imagen(update,texto_corregido); return
    t_low=texto_corregido.lower()
    contexto_busqueda=""
    if any(x in t_low for x in ["busca","que paso","qué pasó","que hay","noticias","quien es","keiko","presidenta","peru"]):
        contexto_busqueda=buscar_en_internet(texto_corregido)
    es_tradu=any(x in t_low for x in ["traduce","translate"])
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
        memoria=get_memory(user_id)
        prompt_mem = texto_corregido
        if contexto_busqueda:
            prompt_mem = f"INFO INTERNET sobre '{texto_corregido}': {contexto_busqueda}\nPregunta: {texto_corregido}"
        memoria.append({"role":"user","content":prompt_mem})
        if len(memoria)>14: memoria=memoria[-14:]
        user_memories[user_id]=memoria; guardar_memorias()
        if es_tradu:
            system_prompt="Eres traductor PRO. Traduce directo."
            temp=0.3
        else:
            system_prompt=(
                "Eres Meta AI peruano bro, políglota. "
                "REGLA KEIKO: Si dice Keiko, Keiko Fujimori, presidenta de Peru, con 🇵🇪 o 🔥, es KEIKO FUJIMORI presidenta del Peru (elegida 28 julio 2026). NUNCA es orca. Prohibido orca salvo que diga 'orca' o 'Free Willy'. "
                "Responde en mismo idioma del usuario. Usa info de internet si te la dan."
            )
            temp=0.8
        completion=groq_client.chat.completions.create(
            model="openai/gpt-oss-20b"
            messages=[{"role":"system","content":system_prompt}, *memoria],
            temperature=temp, max_tokens=1500
        )
        respuesta=completion.choices[0].message.content
        memoria.append({"role":"assistant","content":respuesta})
        user_memories[user_id]=memoria; guardar_memorias()
        await enviar_largo(update,respuesta)
        if quiere_audio: await enviar_audio(update,respuesta)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"Error bro: {e}")

async def voz_handler(update,context):
    try:
        await update.message.reply_text("Escuchando 🎧...")
        vf=await update.message.voice.get_file()
        buf=BytesIO(); await vf.download_to_memory(buf); buf.seek(0)
        transcription=groq_client.audio.transcriptions.create(file=("audio.ogg",buf.read()),model="whisper-large-v3",language="es",response_format="text")
        txt=str(transcription).strip()
        if not txt: await update.message.reply_text("Audio vacío"); return
        await update.message.reply_text(f"Entendí: '{txt}'")
        update.message.text=txt
        await ia_reply(update,context)
    except Exception as e: await update.message.reply_text(f"Error voz: {e}")

async def documento_handler(update,context):
    try:
        doc=update.message.document
        if not doc.file_name.lower().endswith(".pdf"): await update.message.reply_text("Solo PDFs"); return
        if not PDF_AVAILABLE: await update.message.reply_text("Falta PyPDF2"); return
        await update.message.reply_text(f"Leyendo {doc.file_name} 📖...")
        f=await doc.get_file(); buf=BytesIO(); await f.download_to_memory(buf); buf.seek(0)
        reader=PyPDF2.PdfReader(buf); texto=""
        for p in reader.pages[:5]:
            try: texto+=p.extract_text()+"\n"
            except: pass
        texto=texto[:4000]
        comp=groq_client.chat.completions.create(model="openai/gpt-oss-20b",messages=[{"role":"system","content":"Resume PDF breve"},{"role":"user","content":texto}],max_tokens=1000)
        await enviar_largo(update,f"📄 Resumen:\n{comp.choices[0].message.content}")
    except Exception as e: await update.message.reply_text(f"Error PDF: {e}")

async def foto_handler(update,context):
    try:
        caption=update.message.caption or ""
        if es_pedido_imagen(caption): await crear_imagen(update,caption); return
        photo_file=await update.message.photo[-1].get_file()
        buf=BytesIO(); await photo_file.download_to_memory(buf); buf.seek(0)
        b64=base64.b64encode(buf.read()).decode('utf-8')
        comp=groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role":"user","content":[{"type":"text","text":caption or "Describe imagen"},{"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}]}],
            max_tokens=700
        )
        await enviar_largo(update,comp.choices[0].message.content)
    except Exception as e:
        try:
            comp=groq_client.chat.completions.create(model="openai/gpt-oss-20b",messages=[{"role":"user","content":"No pude ver foto, di que intente de nuevo"}],max_tokens=100)
            await update.message.reply_text(comp.choices[0].message.content)
        except: await update.message.reply_text(f"Error foto: {e}")

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("clear",clear_cmd))
    app.add_handler(CommandHandler("imagen",imagen_cmd))
    app.add_handler(CommandHandler("buscar",buscar_cmd))
    app.add_handler(MessageHandler(filters.VOICE,voz_handler))
    app.add_handler(MessageHandler(filters.Document.ALL,documento_handler))
    app.add_handler(MessageHandler(filters.PHOTO,foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ia_reply))
    print("Bot V13.2 FIX VACIO iniciado 🔥")
    app.run_polling()
