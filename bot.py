import os, json, threading, logging, asyncio, base64, urllib.parse, requests, re, random, time
from io import BytesIO
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq

try: from pypdf import PdfReader
except Exception:
    try: from PyPDF2 import PdfReader
    except Exception: PdfReader=None
PDF_AVAILABLE = PdfReader is not None
try: import docx
except Exception: docx=None
try: import openpyxl
except Exception: openpyxl=None
try: from PIL import Image
except Exception: Image=None
try: from gtts import gTTS; TTS_AVAILABLE=True
except: TTS_AVAILABLE=False
try: from ddgs import DDGS
except Exception:
    try: from duckduckgo_search import DDGS
    except Exception: DDGS=None

flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Bot V18 Completo - Online"
def run_flask():
    port=int(os.environ.get("PORT",10000))
    flask_app.run(host="0.0.0.0",port=port)
threading.Thread(target=run_flask,daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ["BOT_TOKEN"]
GROQ_API_KEY=os.environ["GROQ_API_KEY"]
groq_client=Groq(api_key=GROQ_API_KEY)
POLLI_KEY=os.environ.get("POLLINATIONS_KEY","").strip()    # opcional (gratis en enter.pollinations.ai)
HORDE_KEY=os.environ.get("HORDE_KEY","0000000000").strip() # AI Horde anónimo por defecto

# ---------- CONFIG ----------
MODELOS=["openai/gpt-oss-20b","llama-3.1-8b-instant"]
VISION_MODELOS=["meta-llama/llama-4-scout-17b-16e-instruct","meta-llama/llama-4-maverick-17b-128e-instruct"]
USAR_CORRECTOR_IA=False
USAR_COMPOUND=True        # búsqueda web integrada de Groq (gratis, ~250/día)
COOLDOWN=1.5
VENTANA_FOTO=900
VENTANA_DOC=1800
TEXTO_MAX=10000

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

# ---------- VISIÓN ----------
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
    return f"Error bro: {s[:250]}"

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

# ---------- GENERACIÓN DE IMÁGENES (con plan B) ----------
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

def _es_imagen(r):
    return r.status_code==200 and r.headers.get("content-type","").lower().startswith("image")

def _pollinations(prompt_en,w,h):
    enc=urllib.parse.quote(prompt_en[:900])
    seed=random.randint(1,999999)
    errores=[]
    if POLLI_KEY:   # con clave gratis: modelos buenos
        for modelo in ["flux","zimage","turbo"]:
            try:
                url=f"https://gen.pollinations.ai/image/{enc}?model={modelo}&width={w}&height={h}&seed={seed}&nologo=true"
                r=requests.get(url,headers={"Authorization":f"Bearer {POLLI_KEY}"},timeout=90)
                if _es_imagen(r): return r.content
                errores.append(f"{modelo}:{r.status_code}")
            except Exception as e: errores.append(f"{modelo}:{str(e)[:60]}")
    try:   # modo anónimo (puede estar limitado o dar 402)
        url=f"https://image.pollinations.ai/prompt/{enc}?width={w}&height={h}&model=turbo&nologo=true&seed={seed}"
        r=requests.get(url,timeout=60)
        if _es_imagen(r): return r.content
        errores.append(f"anonimo:{r.status_code}")
    except Exception as e: errores.append(f"anonimo:{str(e)[:60]}")
    raise RuntimeError(", ".join(errores))

def _horde(prompt_en,w,h,max_espera=170):
    H={"apikey":HORDE_KEY,"Client-Agent":"telegrambot:1.0:anon","Content-Type":"application/json"}
    ww=max(512,min(768,(w//64)*64)) if w<=h else 896
    hh=max(512,min(768,(h//64)*64)) if h<=w else 896
    if w==h: ww=hh=768
    body={"prompt":prompt_en[:800]+" ### blurry, low quality, deformed, text, watermark",
          "params":{"width":ww,"height":hh,"steps":25,"n":1,"sampler_name":"k_euler_a","cfg_scale":7},
          "nsfw":False,"censor_nsfw":True,"r2":True}
    r=requests.post("https://aihorde.net/api/v2/generate/async",json=body,headers=H,timeout=30)
    r.raise_for_status()
    rid=r.json()["id"]
    t0=time.time()
    while True:
        if time.time()-t0>max_espera:
            try: requests.delete(f"https://aihorde.net/api/v2/generate/status/{rid}",headers=H,timeout=10)
            except Exception: pass
            raise RuntimeError("Horde tardó demasiado")
        time.sleep(4)
        c=requests.get(f"https://aihorde.net/api/v2/generate/check/{rid}",headers=H,timeout=20).json()
        if c.get("faulted"): raise RuntimeError("Horde falló")
        if c.get("done"): break
    s=requests.get(f"https://aihorde.net/api/v2/generate/status/{rid}",headers=H,timeout=30).json()
    img=s["generations"][0]["img"]
    if img.startswith("http"):
        return requests.get(img,timeout=60).content
    return base64.b64decode(img)

ultimo_img={}
def teclado_img():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Otra versión",callback_data="img:otra"),
         InlineKeyboardButton("📱 Vertical",callback_data="img:vert"),
         InlineKeyboardButton("🖥️ Horizontal",callback_data="img:hor")],
        [InlineKeyboardButton("🎭 Hacer sticker",callback_data="img:sticker")]])

async def enviar_imagen(msg, uid, prompt_en, caption, w=1024, h=1024):
    contenido=None; errores=[]
    try: contenido=await asyncio.to_thread(_pollinations,prompt_en,w,h)
    except Exception as e:
        errores.append(f"Pollinations [{e}]"); logging.warning(f"Pollinations falló: {e}")
    if not contenido:
        await msg.reply_text("El servidor principal no me dejó 😕 probando el plan B gratis (puede tardar 1-2 min) ⏳")
        try: contenido=await asyncio.to_thread(_horde,prompt_en,w,h)
        except Exception as e:
            errores.append(f"Horde [{e}]"); logging.warning(f"Horde falló: {e}")
    if not contenido:
        raise RuntimeError("No pude crear la imagen ahora. "+" | ".join(errores)[:300])
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
    if q.data=="img:sticker":
        try:
            fid=file_id_imagen(q.message)
            if fid: await hacer_sticker(q.message,context,fid)
        except Exception as e: await q.message.reply_text(f"Error sticker: {str(e)[:200]}")
        return
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
ultima_foto={}
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

# ---------- LECTURA DE ARCHIVOS ----------
EXT_TEXTO=(".txt",".md",".csv",".tsv",".json",".py",".js",".ts",".html",".css",".log",".xml",".yml",".yaml",".ini",".sql",".java",".c",".cpp",".sh")
EXT_CODIGO=(".py",".js",".ts",".java",".c",".cpp",".sh",".sql",".html",".css")
ultimo_doc={}   # uid -> (nombre, texto, timestamp)
KW_DOC=["el archivo","ese archivo","del archivo","en el archivo","el pdf","ese pdf","del pdf","en el pdf",
        "el documento","ese documento","del documento","el excel","la hoja","el código","el codigo"]

def extraer_texto_archivo(data, nombre):
    n=nombre.lower()
    if n.endswith(".pdf"):
        if not PdfReader: raise RuntimeError("Falta instalar pypdf o PyPDF2")
        reader=PdfReader(BytesIO(data)); partes=[]
        for p in reader.pages[:20]:
            try: partes.append(p.extract_text() or "")
            except Exception: pass
        return "\n".join(partes)
    if n.endswith(".docx"):
        if not docx: raise RuntimeError("Falta instalar python-docx")
        d=docx.Document(BytesIO(data))
        partes=[p.text for p in d.paragraphs]
        for t in d.tables:
            for fila in t.rows: partes.append(" | ".join(c.text for c in fila.cells))
        return "\n".join(partes)
    if n.endswith((".xlsx",".xlsm")):
        if not openpyxl: raise RuntimeError("Falta instalar openpyxl")
        wb=openpyxl.load_workbook(BytesIO(data),read_only=True,data_only=True)
        partes=[]
        for ws in wb.worksheets[:5]:
            partes.append(f"## Hoja: {ws.title}")
            for i,fila in enumerate(ws.iter_rows(values_only=True)):
                if i>=200: break
                partes.append(" | ".join("" if c is None else str(c) for c in fila))
        return "\n".join(partes)
    if n.endswith(EXT_TEXTO):
        for enc in ("utf-8","latin-1"):
            try: return data.decode(enc)
            except Exception: continue
    raise ValueError("tipo no soportado")

def pregunta_defecto(nombre):
    n=(nombre or "").lower()
    if n.endswith(EXT_CODIGO): return "Explica qué hace este código, cómo está organizado y señala errores o mejoras importantes."
    if n.endswith((".csv",".tsv",".xlsx",".xlsm")): return "Resume qué datos contiene (columnas, cantidad aproximada, hallazgos clave)."
    return "Haz un resumen claro y breve con los puntos clave."

def es_pregunta_doc(low, uid):
    d=ultimo_doc.get(uid)
    if not d or time.time()-d[2]>VENTANA_DOC: return False
    if any(v in low for v in VERBOS_CREAR): return False
    return any(k in low for k in KW_DOC)

async def responder_sobre_doc(update, uid, pregunta):
    nombre,texto,_=ultimo_doc[uid]
    resp=await allm([
        {"role":"system","content":"Eres un asistente experto. Responde usando el contenido del archivo. Si la respuesta no está en el archivo, dilo claramente. Responde en el mismo idioma del usuario."},
        {"role":"user","content":f"ARCHIVO '{nombre}':\n{texto}\n\nPREGUNTA: {pregunta}"}],max_tokens=1200,temperature=0.3)
    await enviar_largo(update,f"📄 {resp}")
    mem=get_memory(uid)
    mem.append({"role":"user","content":f"[Archivo {nombre}] {pregunta}"[:300]})
    mem.append({"role":"assistant","content":resp[:1200]})
    user_memories[uid]=mem[-14:]; guardar_memorias()

async def procesar_archivo(update, context, doc, pregunta):
    uid=update.effective_user.id
    nombre=doc.file_name or "archivo"
    if doc.file_size and doc.file_size>20*1024*1024:
        await update.message.reply_text("Telegram solo me deja bajar archivos de hasta 20 MB 😕 mándame uno más liviano."); return
    await update.message.reply_text(f"Leyendo {nombre} 📖...")
    f=await doc.get_file(); buf=BytesIO(); await f.download_to_memory(buf)
    try:
        texto=await asyncio.to_thread(extraer_texto_archivo,buf.getvalue(),nombre)
    except ValueError:
        await update.message.reply_text("Ese tipo de archivo aún no lo leo 😕\nLeo: PDF, Word (.docx), Excel (.xlsx), TXT, CSV, JSON y código (.py .js .html ...).\nTambién imágenes y fotos."); return
    texto=(texto or "").strip()
    if not texto:
        await update.message.reply_text("No pude sacar texto de ese archivo (¿es un PDF escaneado?). Mándamelo como foto o captura y lo leo con visión 👁️"); return
    cortado=len(texto)>TEXTO_MAX
    ultimo_doc[uid]=(nombre,texto[:TEXTO_MAX],time.time())
    if cortado: await update.message.reply_text(f"(Archivo largo: leí los primeros {TEXTO_MAX} caracteres)")
    await responder_sobre_doc(update,uid,pregunta or pregunta_defecto(nombre))

# ---------- BÚSQUEDA EN INTERNET (varios respaldos) ----------
_cache_busq={}
_compound={"dia":"","n":0}
def compound_disponible():
    hoy=time.strftime("%Y-%m-%d")
    if _compound["dia"]!=hoy: _compound.update(dia=hoy,n=0)
    return USAR_COMPOUND and _compound["n"]<240

def _buscar_compound(query):
    _compound["n"]+=1
    c=groq_client.chat.completions.create(
        model="groq/compound-mini",
        messages=[{"role":"system","content":"Search the web and answer with up-to-date facts in Spanish, max 8 lines, include dates. No intro."},
                  {"role":"user","content":query}],
        max_tokens=700)
    return (c.choices[0].message.content or "").strip()

def _buscar_ddgs(query):
    items=DDGS(timeout=10).text(query,max_results=4,region="pe-es")
    return "\n".join(f"- {i.get('title','')}: {i.get('body','')}" for i in items)

def _buscar_wikipedia(query):
    h={"User-Agent":"TelegramBot/1.0 (educational)"}
    for lang in ("es","en"):
        api=f"https://{lang}.wikipedia.org/w/api.php"
        r=requests.get(api,params={"action":"query","list":"search","srsearch":query,"format":"json","srlimit":2},headers=h,timeout=10).json()
        hits=r.get("query",{}).get("search",[])
        if not hits: continue
        title=hits[0]["title"]
        e=requests.get(api,params={"action":"query","prop":"extracts","exintro":1,"explaintext":1,"titles":title,"format":"json"},headers=h,timeout=10).json()
        page=next(iter(e.get("query",{}).get("pages",{}).values()),{})
        ext=page.get("extract","")
        if ext: return f"{title}: {ext[:1200]}"
    return ""

def _buscar_ddg_basico(query):
    url=f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
    r=requests.get(url,timeout=10).json()
    res=r.get("AbstractText","")
    if not res:
        for tp in r.get("RelatedTopics",[])[:3]:
            if isinstance(tp,dict) and "Text" in tp: res+=tp["Text"]+"\n"
    return res

def buscar_en_internet(query):
    q_lower=query.lower()
    if "keiko" in q_lower:
        query = query + " Keiko Fujimori presidenta Peru 2026"
    hit=_cache_busq.get(query)
    if hit and time.time()-hit[0]<600: return hit[1]
    fuentes=[]
    if compound_disponible(): fuentes.append(_buscar_compound)
    if DDGS: fuentes.append(_buscar_ddgs)
    fuentes+= [_buscar_wikipedia,_buscar_ddg_basico]
    for fn in fuentes:
        try:
            res=(fn(query) or "").strip()
            if res:
                res=res[:1800]
                _cache_busq[query]=(time.time(),res)
                return res
        except Exception as e: logging.warning(f"Búsqueda {fn.__name__} falló: {e}")
    return ""   # vacío = no se pudo conectar

CLAVES_BUSQUEDA=["busca","que paso","qué pasó","que hay","noticias","quien es","quién es","keiko","presidenta","peru","perú",
                 "hoy","actual","último","ultimo","precio","cotización","clima","resultado","cuanto cuesta","cuánto cuesta"]
CLAVES_CLEAR=["borra la memoria","limpia la memoria","olvida todo","borra todo","reinicia la memoria"]

# ---------- STICKERS (/s) ----------
def imagen_a_sticker(data):
    if Image is None: raise RuntimeError("Falta instalar Pillow")
    im=Image.open(BytesIO(data)).convert("RGBA")
    esc=512/max(im.size)
    im=im.resize((max(1,round(im.width*esc)),max(1,round(im.height*esc))),Image.LANCZOS)
    for q in (95,85,75,60,45):
        buf=BytesIO(); im.save(buf,"WEBP",quality=q,method=6)
        if buf.tell()<=500*1024: break
    buf.seek(0); buf.name="sticker.webp"
    return buf

def file_id_imagen(m):
    if not m: return None
    if m.photo: return m.photo[-1].file_id
    d=m.document
    if d and (d.mime_type or "").startswith("image/"): return d.file_id
    st=m.sticker
    if st and not st.is_animated and not st.is_video: return st.file_id
    return None

async def hacer_sticker(msg, context, file_id):
    f=await context.bot.get_file(file_id)
    buf=BytesIO(); await f.download_to_memory(buf)
    st=await asyncio.to_thread(imagen_a_sticker,buf.getvalue())
    await msg.reply_sticker(sticker=st)

async def s_cmd(update,context):
    m=update.message
    fid=file_id_imagen(m) or file_id_imagen(m.reply_to_message)
    if not fid:
        await m.reply_text("Responde a una foto con /s, o mándame la foto con /s en el pie 🖼️"); return
    try: await hacer_sticker(m,context,fid)
    except Exception as e: await m.reply_text(f"Error sticker: {str(e)[:200]}")

# ---------- TIKTOK SIN MARCA DE AGUA (/tt) ----------
TT_RE=re.compile(r"(?:https?://)?(?:[\w-]+\.)?tiktok\.com/[^\s]+",re.I)
UA_MOVIL="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
UA_PC="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def sacar_link_tt(texto):
    m=TT_RE.search(texto or "")
    if not m: return None
    link=m.group(0).rstrip(").,;!\"'")
    if not link.lower().startswith("http"): link="https://"+link
    return link

def normalizar_link_tt(link):
    """Convierte links cortos (vm./vt./tiktok.com/t/) al link completo del video."""
    try:
        if re.search(r"/(video|photo)/\d+",link):
            return link.split("?")[0]
        r=requests.get(link,headers={"User-Agent":UA_MOVIL},allow_redirects=True,timeout=15)
        urls=[r.url]+[h.headers.get("Location","") for h in r.history]
        for u in urls:
            m=re.search(r"https?://[^\s?]*tiktok\.com/[^\s?]*/(?:video|photo)/\d+",u)
            if m: return m.group(0)
        m=re.search(r"https://www\.tiktok\.com/@[\w.\-]+/(?:video|photo)/\d+",r.text or "")
        if m: return m.group(0)
    except Exception as e:
        logging.warning(f"No pude expandir el link: {e}")
    return link

def _tikwm(url):
    hdr={"User-Agent":UA_PC,"Origin":"https://www.tikwm.com","Referer":"https://www.tikwm.com/",
         "Accept":"application/json, text/javascript, */*; q=0.01","X-Requested-With":"XMLHttpRequest"}
    def fix(u): return ("https://www.tikwm.com"+u) if u and u.startswith("/") else u
    ultimo="sin respuesta"
    for host in ("https://www.tikwm.com","https://tikwm.com"):
        for _ in range(3):
            try:
                r=requests.post(f"{host}/api/",data={"url":url,"count":12,"cursor":0,"web":1,"hd":1},headers=hdr,timeout=30)
                try: j=r.json()
                except Exception: raise RuntimeError(f"respuesta no válida (HTTP {r.status_code})")
                d=j.get("data")
                if j.get("code")==0 and d:
                    return {"video":fix(d.get("hdplay") or d.get("play")),"normal":fix(d.get("play")),
                            "fotos":d.get("images") or [],"titulo":(d.get("title") or "")[:200]}
                msg=str(j.get("msg") or j)
                ultimo=msg
                if "limit" in msg.lower() or "second" in msg.lower():
                    time.sleep(1.5); continue    # límite de velocidad: reintenta
                break                            # error del link: no sirve reintentar aquí
            except Exception as e:
                ultimo=str(e); time.sleep(1)
    raise RuntimeError(ultimo[:150])

def _bajar(url,limite=49*1024*1024):
    r=requests.get(url,stream=True,timeout=60,headers={"User-Agent":UA_PC,"Referer":"https://www.tikwm.com/"})
    r.raise_for_status()
    buf=BytesIO(); total=0
    for chunk in r.iter_content(256*1024):
        total+=len(chunk)
        if total>limite: return None   # más de 50 MB
        buf.write(chunk)
    buf.seek(0); return buf

def _ytdlp(url):
    import yt_dlp, tempfile
    d=tempfile.mkdtemp()
    opts={"outtmpl":os.path.join(d,"%(id)s.%(ext)s"),"format":"best[ext=mp4]/best","quiet":True,
          "noplaylist":True,"max_filesize":49*1024*1024,"retries":2,"http_headers":{"User-Agent":UA_PC}}
    with yt_dlp.YoutubeDL(opts) as y:
        info=y.extract_info(url,download=True)
        ruta=y.prepare_filename(info)
    with open(ruta,"rb") as f: data=f.read()
    try: os.remove(ruta); os.rmdir(d)
    except Exception: pass
    return BytesIO(data),(info.get("title") or "")[:200]

async def _enviar_video(m,video,titulo):
    await m.reply_video(video=video,caption=f"✅ Sin marca de agua 🔥\n{titulo}"[:1000],
                        supports_streaming=True,write_timeout=120,read_timeout=120)

async def descargar_tiktok(update,context,link):
    m=update.message
    aviso=await m.reply_text("Bajando tu TikTok sin marca de agua ⏬...")
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="upload_video")
        errores=[]; info=None; candidatos=[]; muy_grande=False; titulo=""
        canon=await asyncio.to_thread(normalizar_link_tt,link)
        logging.info(f"TikTok: {link} -> {canon}")
        # 1) TikWM con el link completo y con el original
        for u in dict.fromkeys([canon,link]):
            try:
                info=await asyncio.to_thread(_tikwm,u); break
            except Exception as e: errores.append(f"TikWM: {e}")
        if info:
            titulo=info["titulo"]
            if info["fotos"]:   # presentación de fotos
                fotos=info["fotos"]
                for i in range(0,len(fotos),10):
                    await m.reply_media_group([InputMediaPhoto(u) for u in fotos[i:i+10]])
                await m.reply_text(f"✅ Fotos sin marca de agua\n{titulo}"[:1000]); return
            candidatos=list(dict.fromkeys(x for x in (info["video"],info["normal"]) if x))
            # 2) bajar el video y subirlo
            for u in candidatos:
                try: buf=await asyncio.to_thread(_bajar,u)
                except Exception as e:
                    errores.append(f"Descarga: {str(e)[:70]}"); continue
                if buf is None: muy_grande=True; continue
                await _enviar_video(m,buf,titulo); return
            # 3) pesa más de 50 MB: link directo
            if muy_grande:
                await m.reply_text(f"El video pesa más de 50 MB y Telegram no me deja subirlo 😕\nDescárgalo aquí (sin marca):\n{candidatos[-1]}"); return
            # 4) mi servidor no pudo bajarlo: que Telegram lo baje por su cuenta
            for u in candidatos:
                try: await _enviar_video(m,u,titulo); return
                except Exception as e: errores.append(f"Telegram: {str(e)[:70]}")
        # 5) último recurso: yt-dlp
        try:
            buf,titulo=await asyncio.to_thread(_ytdlp,canon)
            await _enviar_video(m,buf,titulo); return
        except Exception as e: errores.append(f"yt-dlp: {str(e)[:90]}")
        detalle=" | ".join(errores)[:400]
        logging.warning(f"TikTok falló: {detalle}")
        low=detalle.lower()
        if any(k in low for k in ["private","privado","unavailable","not available","removed","deleted","url parsing"]):
            pista="Puede que el video sea privado o borrado, o que el link no sea válido."
        else:
            pista="TikTok bloquea muchos servidores gratis 😕 intenta de nuevo en unos segundos o con otro video."
        await m.reply_text(f"No pude bajar ese TikTok.\n{pista}\n\nDetalle técnico: {detalle}")
    except Exception as e:
        await m.reply_text(f"Error con el TikTok: {str(e)[:200]}")
    finally:
        try: await aviso.delete()
        except Exception: pass

async def tt_cmd(update,context):
    m=update.message
    link=sacar_link_tt(" ".join(context.args) if context.args else "")
    if not link and m.reply_to_message:
        link=sacar_link_tt(m.reply_to_message.text or m.reply_to_message.caption or "")
    if not link:
        await m.reply_text("Usa: /tt link_de_tiktok\n(o responde a un mensaje que tenga el link)"); return
    await descargar_tiktok(update,context,link)

# ---------- COMANDOS ----------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Qué onda {update.effective_user.first_name} bro! 🚀 V18\nCreo imágenes, analizo fotos, leo archivos, bajo TikToks y hago stickers 🔥🇵🇪\nUsa /ayuda para ver todo")
async def ayuda_cmd(update,context):
    await update.message.reply_text(
        "Comandos (también puedes pedírmelos hablando normal):\n"
        "/imagen un gato → o dime 'dibuja un gato'\n"
        "  • agrega 'vertical' u 'horizontal' para el tamaño\n"
        "  • botones 🔄 para otra versión y 🎭 para sticker\n"
        "/buscar tema → o dime 'busca tema'\n"
        "/tt link → baja un TikTok sin marca de agua (o pega el link solo)\n"
        "/s → responde a una foto con /s y te la vuelvo sticker 🎭\n"
        "/clear → o dime 'borra la memoria'\n\n"
        "Fotos 🖼️: mándame una foto (con o sin pregunta):\n"
        "  • 'qué tiene' → la describo\n"
        "  • 'qué significa' → te doy el significado\n"
        "  • 'qué dice' → leo y traduzco el texto\n"
        "  • 'recrea esto en estilo anime' → creo una nueva\n"
        "  • también puedes responder (reply) a una foto con tu pregunta\n\n"
        "Archivos 📄: PDF, Word, Excel, TXT, CSV, JSON y código.\n"
        "  • escribe tu pregunta en el pie del archivo, o pregunta después ('qué dice el archivo sobre...')\n\n"
        "Voz 🎧 y di 'en audio' para respuesta con voz 🎤")
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
    await update.message.reply_text(f"Encontré:\n{res}" if res else "No pude conectar a internet ahora bro 😕 intenta de nuevo en un rato.")

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

    # link de TikTok pegado solo, o "descarga + link"
    link_tt=sacar_link_tt(texto_original)
    if link_tt and (TT_RE.fullmatch(texto_original.strip()) or any(k in low_o for k in ["descarga","baja","bájame","bajame","sin marca"])):
        await descargar_tiktok(update,context,link_tt); return

    if any(x in low_o for x in CLAVES_CLEAR):
        await clear_cmd(update,context); return

    rm=update.message.reply_to_message
    if rm and rm.photo and rm.from_user and not rm.from_user.is_bot and not any(v in low_o for v in VERBOS_CREAR):
        try: await procesar_foto(update,context,rm.photo[-1].file_id,texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return
    if es_pregunta_foto(low_o,user_id):
        try: await procesar_foto(update,context,ultima_foto[user_id][0],texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return
    if es_pregunta_doc(low_o,user_id):
        try: await responder_sobre_doc(update,user_id,texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return

    texto_corregido=await asyncio.to_thread(corregir_y_entender,texto_original)
    print(f"TXT: '{texto_original}' -> '{texto_corregido}'")
    if es_pedido_imagen(texto_corregido):
        await crear_imagen(update,texto_corregido); return
    t_low=texto_corregido.lower()
    busco=any(x in t_low for x in CLAVES_BUSQUEDA)
    contexto_busqueda=""
    if busco:
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
        if busco and contexto_busqueda:
            msgs[-1]={"role":"user","content":f"INFO INTERNET sobre '{texto_corregido}':\n{contexto_busqueda}\n\nPregunta: {texto_corregido}"}
        elif busco:
            msgs[-1]={"role":"user","content":f"(No pude conectar a internet ahora; responde con lo que sabes y avisa brevemente que puede estar desactualizado)\nPregunta: {texto_corregido}"}
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
        caption=update.message.caption or ""
        if (doc.mime_type or "").startswith("image/") or nombre.endswith((".jpg",".jpeg",".png",".webp")):
            if any(k in caption.lower() for k in ["sticker","estiker"]):
                await hacer_sticker(update.message,context,doc.file_id); return
            if es_pedido_imagen(caption): await crear_imagen(update,caption); return
            await procesar_foto(update,context,doc.file_id,caption,doc.mime_type or "image/jpeg"); return
        await procesar_archivo(update,context,doc,caption)
    except Exception as e: await update.message.reply_text(f"Error archivo: {str(e)[:250]}")

async def foto_handler(update,context):
    try:
        caption=update.message.caption or ""
        if any(k in caption.lower() for k in ["sticker","estiker"]):
            await hacer_sticker(update.message,context,update.message.photo[-1].file_id); return
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
    app.add_handler(CommandHandler("tt",tt_cmd))
    app.add_handler(CommandHandler("s",s_cmd))
    app.add_handler(CallbackQueryHandler(img_callback,pattern="^img:"))
    # /s en el pie de una foto: debe ir ANTES de los handlers de foto y documento
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(r"(?i)^/s(@\w+)?(\s|$)"),s_cmd))
    app.add_handler(MessageHandler(filters.VOICE,voz_handler))
    app.add_handler(MessageHandler(filters.Document.ALL,documento_handler))
    app.add_handler(MessageHandler(filters.PHOTO,foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ia_reply))
    print("Bot V18 iniciado 🔥")
    app.run_polling()
