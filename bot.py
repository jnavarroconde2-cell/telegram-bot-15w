import os, json, threading, logging, asyncio, base64, urllib.parse, requests, re, random, time
from io import BytesIO
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq

try: import PyPDF2; PDF_AVAILABLE=True
except: PDF_AVAILABLE=False
try: from gtts import gTTS; TTS_AVAILABLE=True
except: TTS_AVAILABLE=False
try: from ddgs import DDGS
except Exception:
    try: from duckduckgo_search import DDGS
    except Exception: DDGS=None

flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot V15 Imagenes y Vision - Online"
def run_flask():
    port=int(os.environ.get("PORT",10000))
    flask_app.run(host="0.0.0.0",port=port)
threading.Thread(target=run_flask,daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ["BOT_TOKEN"]
GROQ_API_KEY=os.environ["GROQ_API_KEY"]
groq_client=Groq(api_key=GROQ_API_KEY)

# ---------- CONFIG ----------
MODELOS=["openai/gpt-oss-20b","llama-3.1-8b-instant"]
VISION_MODELOS=["meta-llama/llama-4-scout-17b-16e-instruct","meta-llama/llama-4-maverick-17b-128e-instruct"]
USAR_CORRECTOR_IA=False
COOLDOWN=1.5
VENTANA_FOTO=900   # segundos que recuerda tu última foto para preguntas de seguimiento

def ahora():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Lima")).strftime("%A %d/%m/%Y %H:%M")
    except Exception:
        return datetime.now().strftime("%d/%m/%Y %H:%M")

# ---------- LLM CON FALLBACK ----------
def llm(messages, max_tokens=800, temperature=0.7, modelos=None):
    ultimo=None
    for m in (modelos or MODELOS):
        try:
            kw={"extra_body":{"reasoning_effort":"low"}} if "gpt-oss" in m else {}
            c=groq_client.chat.completions.create(model=m,messages=messages,max_tokens=max_tokens,temperature=temperature,**kw)
            txt=(c.choices[0].message.content or "").strip()
            if txt: return txt
        except Exception as e:
            ultimo=e; logging.warning(f"Modelo {m} falló: {e}")
    raise ultimo or RuntimeError("Respuesta vacía")
async def allm(*a,**k): return await asyncio.to_thread(llm,*a,**k)

# ---------- VISIÓN (analizar fotos) ----------
def vision(b64, instruccion, mime="image/jpeg"):
    ultimo=None
    for m in VISION_MODELOS:
        try:
            c=groq_client.chat.completions.create(
                model=m,
                messages=[{"role":"user","content":[
                    {"type":"text","text":instruccion},
                    {"type":"image_url","image_url":{"url":f"data:{mime};base64,{b64}"}}]}],
                max_tokens=900,temperature=0.4)
            t=(c.choices[0].message.content or "").strip()
            if t: return t
        except Exception as e:
            ultimo=e; logging.warning(f"Visión {m} falló: {e}")
    raise ultimo or RuntimeError("Sin respuesta de visión")

def construir_prompt_vision(pregunta):
    p=(pregunta or "").lower()
    base=("Eres un analista visual experto. Responde en el MISMO idioma del usuario (español si no hay texto), claro y directo. "
          "No identifiques a personas reales por su cara; descríbelas solo por lo que se ve. ")
    if any(k in p for k in ["significa","significado","simboliza","quiere decir","mensaje","por qué","por que","chiste","meme","interpreta"]):
        modo=("Explica el SIGNIFICADO: primero 1 línea de qué se ve; luego qué representa, símbolos, contexto cultural, "
              "la intención o el chiste si es un meme, y qué mensaje transmite.")
    elif any(k in p for k in ["lee","texto","dice","transcribe","traduce","ocr","escrito"]):
        modo="Transcribe EXACTAMENTE todo el texto visible y luego tradúcelo o explícalo si hace falta."
    elif any(k in p for k in ["que tiene","qué tiene","que hay","qué hay","que es","qué es","describe","que ves","qué ves","contiene","muestra"]):
        modo="Describe en detalle: objetos, personas, colores, ambiente, texto visible y cualquier detalle relevante."
    else:
        modo="Describe lo importante de la imagen y, si aplica, explica qué significa o qué está pasando."
    return f"{base}{modo}\n\nPregunta del usuario: {pregunta or 'Analiza esta imagen'}"

# ---------- MEMORIA ----------
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
        def _gen():
            tts=gTTS(text=texto[:400], lang=lang, tld='com.pe' if lang=='es' else 'com', slow=False)
            buf=BytesIO(); tts.write_to_fp(buf); buf.seek(0); return buf
        buf=await asyncio.to_thread(_gen)
        await update.message.reply_voice(voice=buf, caption="🎤 Audio bro")
    except Exception as e: print(f"Error audio: {e}")

def msg_error(e):
    s=str(e)
    if "429" in s or "rate" in s.lower():
        return "Estoy al límite gratis por ahora bro 😅 espera unos segundos y reintenta."
    return f"Error bro: {s[:200]}"

# ---------- AUTOCORRECTOR ----------
def corregir_y_entender(texto):
    if not texto or len(texto.strip())<2: return texto
    low = texto.lower()
    if "keiko" in low or "🇵🇪" in texto or "🔥" in texto or "presidenta" in low:
        return texto
    if len(texto.strip()) <= 5 or not USAR_CORRECTOR_IA:
        return texto
    try:
        res=llm([
            {"role":"system","content":"You are typo corrector. Fix spelling, keep language and emojis. Halo->Hola, vosa->cosa, Haor->Ahora. Only corrected text, never empty."},
            {"role":"user","content":texto}],max_tokens=200,temperature=0.2,modelos=["llama-3.1-8b-instant"])
        return res if res and len(res)>2 else texto
    except: return texto

# ---------- IMÁGENES ----------
FRASES_IMG=["creame una imagen de","crea una imagen de","hazme una imagen de","haz una imagen de","genera una imagen de",
            "crea una foto de","genera una foto de","creame una imagen","crea una imagen","imagen de",
            "dibuja una","dibuja un","dibuja","crea foto de","create an image of"]
VERBOS_CREAR=["crea ","creame","dibuja","genera ","hazme","haz una","haz un","create "]
def extraer_prompt_imagen(texto):
    t=texto.lower()
    for f in FRASES_IMG:
        if f in t:
            idx=t.find(f)+len(f)
            resto=texto[idx:].strip()
            resto=re.sub(r"^(de|del|un|una)\s+","",resto,flags=re.I).strip()
            return resto or texto
    return texto
def es_pedido_imagen(texto):
    tl=texto.lower().strip()
    if len(tl)<5: return False
    return any(k in tl for k in ["crea una imagen","creame una imagen","hazme una imagen","haz una imagen","genera una imagen",
                                  "crea una foto de","genera una foto de","imagen de","dibuja un","dibuja una"])
def detectar_formato(texto):
    t=(texto or "").lower()
    if any(k in t for k in ["vertical","historia","story","celular","portrait","retrato","9:16"]): return 768,1344
    if any(k in t for k in ["horizontal","panoramic","panorámic","wallpaper","paisaje","landscape","16:9"]): return 1344,768
    return 1024,1024
def quiere_recrear(texto):
    t=(texto or "").lower()
    return any(k in t for k in ["recrea","recréala","recreala","parecida","similar a","conviértela","convierte esta","transforma","en estilo","estilo anime","versión anime","version anime","caricatura de esta"])

ultimo_img={}   # uid -> (prompt_en, caption, w, h)
def teclado_img():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Otra versión",callback_data="img:otra"),
        InlineKeyboardButton("📱 Vertical",callback_data="img:vert"),
        InlineKeyboardButton("🖥️ Horizontal",callback_data="img:hor")]])

async def enviar_imagen(msg, uid, prompt_en, caption, w=1024, h=1024):
    encoded=urllib.parse.quote(prompt_en)
    contenido=None; ultimo=None
    for modelo in ["flux","turbo"]:   # flux = mejor calidad; turbo = respaldo rápido
        try:
            seed=random.randint(1,999999)
            url=f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&model={modelo}&nologo=true&seed={seed}"
            r=await asyncio.to_thread(requests.get,url,timeout=60); r.raise_for_status()
            contenido=r.content; break
        except Exception as e: ultimo=e
    if not contenido: raise ultimo or RuntimeError("sin imagen")
    ultimo_img[uid]=(prompt_en,caption,w,h)
    bio=BytesIO(contenido); bio.name="imagen.jpg"
    await msg.reply_photo(photo=bio, caption=f"Listo bro 🔥 {caption}"[:900], reply_markup=teclado_img())

async def crear_imagen(update, prompt_original):
    try:
        uid=update.effective_user.id
        w,h=detectar_formato(prompt_original)
        prompt_limpio=extraer_prompt_imagen(prompt_original)
        await update.message.reply_text(f"Ya bro, creando: '{prompt_limpio}'... 🎨")
        try:
            prompt_en=await allm([
                {"role":"system","content":"Convert to ONE detailed ENGLISH image-generation prompt. Add lighting, composition, camera/lens or art-medium details and 'ultra detailed, high quality'. Respect the style the user asks for (anime, cartoon, painting, 3D...); if none, photorealistic. No text in the image. Output only the prompt."},
                {"role":"user","content":prompt_limpio}],max_tokens=200,temperature=0.7,modelos=["llama-3.1-8b-instant","openai/gpt-oss-20b"])
        except: prompt_en=f"{prompt_limpio} photorealistic 8k no text"
        await enviar_imagen(update.message,uid,prompt_en,prompt_limpio,w,h)
    except Exception as e: await update.message.reply_text(f"Error imagen: {e}")

async def img_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    uid=q.from_user.id
    data=ultimo_img.get(uid)
    if not data:
        await q.message.reply_text("Ya no tengo esa imagen guardada, pídeme otra 🎨"); return
    prompt_en,caption,w,h=data
    accion=q.data.split(":")[1]
    if accion=="vert": w,h=768,1344
    elif accion=="hor": w,h=1344,768
    try:
        await q.message.reply_text("Va otra 🎨...")
        await enviar_imagen(q.message,uid,prompt_en,caption,w,h)
    except Exception as e: await q.message.reply_text(f"Error imagen: {e}")

# ---------- ANÁLISIS DE FOTOS ----------
ultima_foto={}   # uid -> (file_id, timestamp)
KW_PREGUNTA_FOTO=["la foto","esa foto","en la foto","la imagen","esa imagen","en la imagen","qué ves","que ves","lo que ves",
                  "qué tiene","que tiene","qué significa esto","que significa esto","qué significa eso","que significa eso",
                  "significado de esto","significado de la foto","significado de la imagen"]
def es_pregunta_foto(low, uid):
    f=ultima_foto.get(uid)
    if not f or time.time()-f[1]>VENTANA_FOTO: return False
    if any(v in low for v in VERBOS_CREAR): return False
    return any(k in low for k in KW_PREGUNTA_FOTO)

async def procesar_foto(update, context, file_id, caption, mime="image/jpeg"):
    uid=update.effective_user.id
    ultima_foto[uid]=(file_id,time.time())
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
    f=await context.bot.get_file(file_id)
    buf=BytesIO(); await f.download_to_memory(buf)
    b64=base64.b64encode(buf.getvalue()).decode("utf-8")
    # recrear a partir de la foto
    if quiere_recrear(caption):
        desc=await asyncio.to_thread(vision,b64,
            "Describe this image in precise detail in English (subject, pose, colors, setting, lighting, style) as a prompt for an AI image generator. Do not identify real people. Output only the description.",mime)
        await update.message.reply_text("Ya bro, recreando tu imagen... 🎨")
        w,h=detectar_formato(caption)
        prompt_en=await allm([
            {"role":"system","content":"Merge the image description with the requested changes/style into ONE detailed English image-generation prompt. No text in the image. Output only the prompt."},
            {"role":"user","content":f"Description: {desc}\nRequested changes/style: {caption}"}],
            max_tokens=250,temperature=0.7,modelos=["llama-3.1-8b-instant","openai/gpt-oss-20b"])
        await enviar_imagen(update.message,uid,prompt_en,"Recreación de tu foto",w,h)
        return
    respuesta=await asyncio.to_thread(vision,b64,construir_prompt_vision(caption),mime)
    await enviar_largo(update,respuesta)
    mem=get_memory(uid)
    mem.append({"role":"user","content":f"[Te envié una foto] {caption}".strip()})
    mem.append({"role":"assistant","content":respuesta[:1200]})
    user_memories[uid]=mem[-14:]; guardar_memorias()

# ---------- BÚSQUEDA ----------
_cache_busq={}
def buscar_en_internet(query):
    q_lower=query.lower()
    if "keiko" in q_lower:
        query = query + " Keiko Fujimori presidenta Peru 2026"
    hit=_cache_busq.get(query)
    if hit and time.time()-hit[0]<600: return hit[1]
    res=""
    if DDGS:
        try:
            items=DDGS().text(query,max_results=4,region="pe-es")
            res="\n".join(f"- {i.get('title','')}: {i.get('body','')}" for i in items)
        except Exception as e: logging.warning(f"ddgs falló: {e}")
    if not res:
        try:
            url=f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
            r=requests.get(url,timeout=10).json()
            res=r.get("AbstractText","")
            if not res:
                for tp in r.get("RelatedTopics",[])[:3]:
                    if isinstance(tp,dict) and "Text" in tp:
                        res+=tp["Text"]+"\n"
        except Exception: pass
    res=res[:1800] if res else f"Info buscada sobre: {query}"
    _cache_busq[query]=(time.time(),res)
    return res

CLAVES_BUSQUEDA=["busca","que paso","qué pasó","que hay","noticias","quien es","quién es","keiko","presidenta","peru","perú",
                 "hoy","actual","último","ultimo","precio","cotización","clima","resultado","cuanto cuesta","cuánto cuesta"]
CLAVES_CLEAR=["borra la memoria","limpia la memoria","olvida todo","borra todo","reinicia la memoria"]

# ---------- COMANDOS ----------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Qué onda {update.effective_user.first_name} bro! 🚀 V15\nCreo imágenes y analizo tus fotos 🔥🇵🇪\nUsa /ayuda para ver todo")
async def ayuda_cmd(update,context):
    await update.message.reply_text(
        "Comandos (también puedes pedírmelos hablando normal):\n"
        "/imagen un gato → o dime 'dibuja un gato'\n"
        "  • agrega 'vertical' u 'horizontal' para el tamaño\n"
        "  • usa los botones 🔄 para otra versión\n"
        "/buscar tema → o dime 'busca tema'\n"
        "/clear → o dime 'borra la memoria'\n\n"
        "Fotos 🖼️: mándame una foto (con o sin pregunta):\n"
        "  • 'qué tiene' → la describo\n"
        "  • 'qué significa' → te doy el significado\n"
        "  • 'qué dice' → leo y traduzco el texto\n"
        "  • 'recrea esto en estilo anime' → creo una nueva\n"
        "  • también puedes responder (reply) a una foto con tu pregunta\n\n"
        "Voz 🎧, PDFs 📄 y di 'en audio' para respuesta con voz 🎤")
async def clear_cmd(update,context):
    user_memories[update.effective_user.id]=[]; guardar_memorias()
    await update.message.reply_text("Memoria limpia 🧹 ahora prueba: Que hay de bueno con keiko la presidenta de peru")
async def imagen_cmd(update,context):
    if not context.args: await update.message.reply_text("Usa: /imagen un gato"); return
    await crear_imagen(update," ".join(context.args))
async def buscar_cmd(update,context):
    if not context.args: await update.message.reply_text("Usa: /buscar keiko fujimori"); return
    q=" ".join(context.args)
    res=await asyncio.to_thread(buscar_en_internet,q)
    await update.message.reply_text(f"Encontré:\n{res}")

# ---------- CHAT PRINCIPAL ----------
_ultimo_msg={}
async def ia_reply(update:Update, context:ContextTypes.DEFAULT_TYPE, texto_override=None):
    user_id=update.effective_user.id
    texto_original=texto_override or update.message.text or ""
    if not texto_original.strip(): return
    if texto_override is None:
        if time.time()-_ultimo_msg.get(user_id,0)<COOLDOWN: return
        _ultimo_msg[user_id]=time.time()
    low_o=texto_original.lower()
    quiere_audio=any(x in low_o for x in ["en audio","nota de voz","audio","con voz","hablame"])

    if any(x in low_o for x in CLAVES_CLEAR):
        await clear_cmd(update,context); return

    # responder (reply) a una foto de otra persona/usuario con una pregunta
    rm=update.message.reply_to_message
    if rm and rm.photo and rm.from_user and not rm.from_user.is_bot and not any(v in low_o for v in VERBOS_CREAR):
        try:
            await procesar_foto(update,context,rm.photo[-1].file_id,texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return
    # pregunta de seguimiento sobre tu última foto
    if es_pregunta_foto(low_o,user_id):
        try:
            await procesar_foto(update,context,ultima_foto[user_id][0],texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return

    texto_corregido=await asyncio.to_thread(corregir_y_entender,texto_original)
    print(f"TXT: '{texto_original}' -> '{texto_corregido}'")
    if es_pedido_imagen(texto_corregido):
        await crear_imagen(update,texto_corregido); return
    t_low=texto_corregido.lower()
    contexto_busqueda=""
    if any(x in t_low for x in CLAVES_BUSQUEDA):
        contexto_busqueda=await asyncio.to_thread(buscar_en_internet,texto_corregido)
    es_tradu=any(x in t_low for x in ["traduce","translate"])
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
        memoria=get_memory(user_id)
        memoria.append({"role":"user","content":texto_corregido})
        if len(memoria)>14: memoria=memoria[-14:]
        user_memories[user_id]=memoria
        if es_tradu:
            system_prompt="Eres traductor PRO. Traduce directo."
            temp=0.3
        else:
            system_prompt=(
                "Eres Meta AI peruano bro, políglota. "
                "REGLA KEIKO: Si dice Keiko, Keiko Fujimori, presidenta de Peru, con 🇵🇪 o 🔥, es KEIKO FUJIMORI presidenta del Peru (elegida 28 julio 2026). NUNCA es orca. Prohibido orca salvo que diga 'orca' o 'Free Willy'. "
                "Responde en mismo idioma del usuario. Usa info de internet si te la dan. "
                f"Fecha y hora actual en Perú: {ahora()}. "
                "Entiende mensajes con errores de ortografía sin corregir al usuario. "
                "Sé claro, directo y útil; si no sabes algo dilo en vez de inventar."
            )
            temp=0.8
        msgs=list(memoria)
        if contexto_busqueda:
            msgs[-1]={"role":"user","content":f"INFO INTERNET sobre '{texto_corregido}':\n{contexto_busqueda}\n\nPregunta: {texto_corregido}"}
        respuesta=await allm([{"role":"system","content":system_prompt},*msgs],max_tokens=1500,temperature=temp)
        memoria.append({"role":"assistant","content":respuesta[:1500]})
        user_memories[user_id]=memoria; guardar_memorias()
        await enviar_largo(update,respuesta)
        if quiere_audio: await enviar_audio(update,respuesta)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(msg_error(e))

async def voz_handler(update,context):
    try:
        await update.message.reply_text("Escuchando 🎧...")
        vf=await update.message.voice.get_file()
        buf=BytesIO(); await vf.download_to_memory(buf); buf.seek(0)
        data=buf.read()
        transcription=await asyncio.to_thread(lambda: groq_client.audio.transcriptions.create(
            file=("audio.ogg",data),model="whisper-large-v3",response_format="text"))
        txt=str(transcription).strip()
        if not txt: await update.message.reply_text("Audio vacío"); return
        await update.message.reply_text(f"Entendí: '{txt}'")
        await ia_reply(update,context,texto_override=txt)
    except Exception as e: await update.message.reply_text(f"Error voz: {e}")

async def documento_handler(update,context):
    try:
        doc=update.message.document
        nombre=(doc.file_name or "").lower()
        # imágenes enviadas como archivo -> se analizan como foto
        if (doc.mime_type or "").startswith("image/") or nombre.endswith((".jpg",".jpeg",".png",".webp")):
            caption=update.message.caption or ""
            if es_pedido_imagen(caption): await crear_imagen(update,caption); return
            await procesar_foto(update,context,doc.file_id,caption,doc.mime_type or "image/jpeg"); return
        if not nombre.endswith(".pdf"): await update.message.reply_text("Solo PDFs o imágenes"); return
        if not PDF_AVAILABLE: await update.message.reply_text("Falta PyPDF2"); return
        await update.message.reply_text(f"Leyendo {doc.file_name} 📖...")
        f=await doc.get_file(); buf=BytesIO(); await f.download_to_memory(buf); buf.seek(0)
        reader=PyPDF2.PdfReader(buf); texto=""
        for p in reader.pages[:5]:
            try: texto+=(p.extract_text() or "")+"\n"
            except: pass
        texto=texto[:4000]
        if not texto.strip(): await update.message.reply_text("No pude sacar texto del PDF (¿es escaneado?)"); return
        resumen=await allm([{"role":"system","content":"Resume PDF breve"},{"role":"user","content":texto}],max_tokens=1000)
        await enviar_largo(update,f"📄 Resumen:\n{resumen}")
        mem=get_memory(update.effective_user.id)
        mem.append({"role":"user","content":f"[Te envié el PDF '{doc.file_name}']"})
        mem.append({"role":"assistant","content":f"Resumen del PDF '{doc.file_name}': {resumen[:1200]}"})
        user_memories[update.effective_user.id]=mem[-14:]; guardar_memorias()
    except Exception as e: await update.message.reply_text(f"Error PDF: {e}")

async def foto_handler(update,context):
    try:
        caption=update.message.caption or ""
        if es_pedido_imagen(caption): await crear_imagen(update,caption); return
        await procesar_foto(update,context,update.message.photo[-1].file_id,caption)
    except Exception as e:
        try:
            txt=await allm([{"role":"user","content":"No pude ver foto, di que intente de nuevo"}],max_tokens=100)
            await update.message.reply_text(txt)
        except: await update.message.reply_text(f"Error foto: {e}")

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("ayuda",ayuda_cmd))
    app.add_handler(CommandHandler("clear",clear_cmd))
    app.add_handler(CommandHandler("imagen",imagen_cmd))
    app.add_handler(CommandHandler("buscar",buscar_cmd))
    app.add_handler(CallbackQueryHandler(img_callback,pattern="^img:"))
    app.add_handler(MessageHandler(filters.VOICE,voz_handler))
    app.add_handler(MessageHandler(filters.Document.ALL,documento_handler))
    app.add_handler(MessageHandler(filters.PHOTO,foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ia_reply))
    print("Bot V15 Imagenes y Vision iniciado 🔥")
    app.run_polling()
