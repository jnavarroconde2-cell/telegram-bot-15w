import os, json, threading, logging, asyncio, base64, urllib.parse, requests, re, random, time, socket, ipaddress, html
from io import BytesIO
from collections import deque
from datetime import datetime, timedelta, timezone, time as dtime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand
from telegram.error import NetworkError
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
                          TypeHandler, filters, ContextTypes)
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
def home(): return "Bot V21 Estable y Util - Online"
def run_flask():
    port=int(os.environ.get("PORT",10000))
    flask_app.run(host="0.0.0.0",port=port)
threading.Thread(target=run_flask,daemon=True).start()

logging.basicConfig(level=logging.INFO)
TOKEN=os.environ["BOT_TOKEN"]
GROQ_API_KEY=os.environ["GROQ_API_KEY"]
groq_client=Groq(api_key=GROQ_API_KEY)
POLLI_KEY=os.environ.get("POLLINATIONS_KEY","").strip()
HORDE_KEY=os.environ.get("HORDE_KEY","0000000000").strip()
UPSTASH_URL=os.environ.get("UPSTASH_REDIS_REST_URL","").strip().rstrip("/")
UPSTASH_TOKEN=os.environ.get("UPSTASH_REDIS_REST_TOKEN","").strip()
USA_KV=bool(UPSTASH_URL and UPSTASH_TOKEN)
try: ADMIN_ID=int(os.environ.get("ADMIN_ID","0") or 0)
except Exception: ADMIN_ID=0
try: LIMITE_DIARIO=int(os.environ.get("LIMITE_DIARIO","60") or 0)   # usos de IA por usuario al día (0 = sin límite)
except Exception: LIMITE_DIARIO=60

# ---------- CONFIG ----------
MODELOS_RAPIDOS=["openai/gpt-oss-20b","llama-3.1-8b-instant"]
MODELOS_LISTOS=["openai/gpt-oss-120b","llama-3.3-70b-versatile","openai/gpt-oss-20b","llama-3.1-8b-instant"]
MODELOS=MODELOS_RAPIDOS
VISION_MODELOS=["meta-llama/llama-4-scout-17b-16e-instruct","meta-llama/llama-4-maverick-17b-128e-instruct"]
USAR_CORRECTOR_IA=False
USAR_COMPOUND=True
APRENDER_PERFIL=True
RESPONDER_A_REPLIES=True   # en grupos: también responde si le contestan (reply) a un mensaje suyo. False = solo con @mención
BACKUP_AUTOMATICO=True     # manda un backup diario (3 a.m. hora Perú) al admin
MAX_NOTAS=50
COOLDOWN=1.5
VENTANA_FOTO=900
VENTANA_DOC=1800
TEXTO_MAX=10000
INICIO=time.time()
LIMA=timezone(timedelta(hours=-5))
APP_BOT=None

def ahora():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Lima")).strftime("%A %d/%m/%Y %H:%M")
    except Exception:
        return datetime.now(LIMA).strftime("%d/%m/%Y %H:%M")

# ---------- GRUPOS: SOLO RESPONDE SI LO MENCIONAN ----------
def en_grupo(update):
    c=update.effective_chat
    return bool(c and c.type in ("group","supergroup"))

def me_hablan(update, context):
    """En privado siempre True. En grupos: solo si mencionan @bot (o le responden a un mensaje suyo)."""
    if not en_grupo(update): return True
    m=update.message
    if not m: return False
    user=(context.bot.username or "").lower()
    texto=(m.text or m.caption or "").lower()
    if user and re.search(rf"@{re.escape(user)}(?!\w)",texto): return True
    if RESPONDER_A_REPLIES:
        rm=m.reply_to_message
        if rm and rm.from_user and rm.from_user.id==context.bot.id: return True
    return False

def limpiar_mencion(texto, context):
    user=context.bot.username
    if not user or not texto: return texto or ""
    t=re.sub(rf"@{re.escape(user)}(?!\w)","",texto,flags=re.I)
    return re.sub(r"[ \t]{2,}"," ",t).strip()

async def es_admin_grupo(update,context):
    u=update.effective_user
    if not u: return False
    if ADMIN_ID and u.id==ADMIN_ID: return True
    try:
        m=await context.bot.get_chat_member(update.effective_chat.id,u.id)
        return m.status in ("administrator","creator")
    except Exception: return False

# ---------- PERSISTENCIA (Upstash gratis o archivos locales) ----------
user_memories={}; perfiles={}; usuarios={}; recordatorios=[]; notas={}; grupos={}
_sucio=set()
NOMBRES_ESTADO=("memorias","perfiles","usuarios","recordatorios","notas","grupos")
def _estado(n): return {"memorias":user_memories,"perfiles":perfiles,"usuarios":usuarios,"recordatorios":recordatorios,"notas":notas,"grupos":grupos}[n]
def _serializar(n):
    d=_estado(n)
    if n=="memorias": return {str(k):v[-20:] for k,v in list(d.items())}
    if isinstance(d,dict): return {str(k):v for k,v in list(d.items())}
    return list(d)
def _kv_h(): return {"Authorization":f"Bearer {UPSTASH_TOKEN}"}
def kv_get(key):
    if not USA_KV: return None
    try:
        r=requests.get(f"{UPSTASH_URL}/get/{urllib.parse.quote(key,safe='')}",headers=_kv_h(),timeout=15)
        res=r.json().get("result")
        return json.loads(res) if res else None
    except Exception as e:
        logging.warning(f"KV get falló: {e}"); return None
def kv_set(key,val):
    if not USA_KV: return False
    try:
        r=requests.post(UPSTASH_URL,headers=_kv_h(),json=["SET",key,json.dumps(val,ensure_ascii=False)],timeout=20)
        return r.status_code==200
    except Exception as e:
        logging.warning(f"KV set falló: {e}"); return False
def guardar_local(n):
    try:
        with open(f"{n}.json",'w',encoding='utf-8') as f:
            json.dump(_serializar(n),f,ensure_ascii=False,indent=1)
    except Exception as e: print(f"Error guardado local {n}: {e}")
def guardar_estado(n):
    guardar_local(n); kv_set(f"bot:{n}",_serializar(n))
def marcar(n): _sucio.add(n)
def persistir(n): guardar_local(n); marcar(n)
def guardar_memorias(): persistir("memorias")
def flush_kv():
    for n in list(_sucio):
        _sucio.discard(n)
        try: guardar_estado(n)
        except Exception as e: logging.warning(f"Flush {n}: {e}")
def _flusher():
    while True:
        time.sleep(20); flush_kv()
threading.Thread(target=_flusher,daemon=True).start()
def cargar_estado():
    for n in NOMBRES_ESTADO:
        data=kv_get(f"bot:{n}")
        if data is None:
            try:
                with open(f"{n}.json",'r',encoding='utf-8') as f: data=json.load(f)
            except Exception: data=None
        if data is None: continue
        try:
            if n=="recordatorios": recordatorios.clear(); recordatorios.extend(data)
            else: _estado(n).update({int(k):v for k,v in data.items()})
        except Exception as e: print(f"Error cargando {n}: {e}")
    print(f"Estado cargado | usuarios:{len(usuarios)} memorias:{len(user_memories)} KV:{USA_KV}")
cargar_estado()

def get_memory(uid):
    if uid not in user_memories: user_memories[uid]=[]
    return user_memories[uid]
def get_perfil(uid):
    p=perfiles.setdefault(uid,{"modo":"normal","hechos":[]})
    p.setdefault("modo","normal"); p.setdefault("hechos",[])
    return p

# ---------- LÍMITE DIARIO POR USUARIO ----------
uso_diario={}   # uid -> [fecha, usos, ya_avisado]
async def consumir(update):
    """Cuenta un uso de IA. Devuelve False (y avisa una vez) si el usuario ya llegó a su límite de hoy."""
    if LIMITE_DIARIO<=0: return True
    u=update.effective_user
    if not u or (ADMIN_ID and u.id==ADMIN_ID): return True
    hoy=datetime.now(LIMA).strftime("%Y-%m-%d")
    r=uso_diario.get(u.id)
    if not r or r[0]!=hoy:
        r=[hoy,0,False]; uso_diario[u.id]=r
    if r[1]>=LIMITE_DIARIO:
        if not r[2]:
            r[2]=True
            try:
                await update.effective_message.reply_text(
                    f"Llegaste a tu límite de {LIMITE_DIARIO} usos de IA por hoy 😅 Se reinicia a medianoche (hora Perú).\n"
                    "Los comandos simples (/clima, /dolar, /qr, /tt, /s...) siguen funcionando.")
            except Exception: pass
        return False
    r[1]+=1
    return True

# ---------- AVISOS DE ERROR AL ADMIN ----------
_alertas={}
async def alertar_admin(titulo, detalle=""):
    if not ADMIN_ID or APP_BOT is None: return
    clave=titulo+detalle[:60]
    if time.time()-_alertas.get(clave,0)<120: return   # no repite el mismo aviso en 2 minutos
    _alertas[clave]=time.time()
    try: await APP_BOT.send_message(ADMIN_ID,f"⚠️ {titulo}\n{detalle}"[:3500])
    except Exception as e: logging.warning(f"No pude avisar al admin: {e}")

async def error_handler(update, context):
    err=context.error
    logging.error("Excepción no controlada",exc_info=err)
    if isinstance(err,NetworkError): return   # fallos de red pasajeros: solo registro
    await alertar_admin("Error no controlado",f"{type(err).__name__}: {str(err)[:300]}")

# ---------- LLM CON FALLBACK Y ENRUTADOR ----------
_muertos=set()
def llm(messages, max_tokens=800, temperature=0.7, modelos=None, esfuerzo="low"):
    ultimo=None
    for m in (modelos or MODELOS):
        if m in _muertos: continue
        try:
            kw={"extra_body":{"reasoning_effort":esfuerzo}} if "gpt-oss" in m else {}
            c=groq_client.chat.completions.create(model=m,messages=messages,max_tokens=max_tokens,temperature=temperature,**kw)
            txt=(c.choices[0].message.content or "").strip()
            if txt: return txt
        except Exception as e:
            ultimo=e; s=str(e).lower()
            if any(k in s for k in ["decommission","model_not_found","does not exist","no longer supported"]): _muertos.add(m)
            logging.warning(f"Modelo {m} falló: {e}")
    raise ultimo or RuntimeError("Respuesta vacía")
async def allm(*a,**k): return await asyncio.to_thread(llm,*a,**k)

KW_COMPLEJO=["explica","por qué","por que","cómo funciona","como funciona","analiza","compara","diferencia","resuelve","calcula",
             "demuestra","código","codigo","programa","script","función","funcion","error","bug","estrategia","ensayo","paso a paso",
             "ventajas","desventajas","optimiza","algoritmo","matemática","matematica","ecuación","ecuacion","redacta","resumen largo"]
def es_complejo(t):
    tl=(t or "").lower()
    return len(tl)>220 or "```" in tl or any(k in tl for k in KW_COMPLEJO)

MODOS={
 "normal":"",
 "serio":"Responde de forma formal, precisa y sin bromas ni jerga.",
 "profesor":"Actúa como profesor paciente: explica paso a paso con ejemplos simples y termina con una pregunta corta para comprobar que entendió.",
 "programador":"Actúa como ingeniero de software senior: respuestas técnicas, código limpio y comentado en bloques, menciona errores comunes y buenas prácticas.",
 "bromista":"Responde con humor peruano ligero y divertido, sin dejar de ser útil y correcto.",
}
def construir_system(uid):
    p=get_perfil(uid)
    s=("Eres Meta AI peruano bro, políglota. "
       "REGLA KEIKO: Si dice Keiko, Keiko Fujimori, presidenta de Peru, con 🇵🇪 o 🔥, es KEIKO FUJIMORI presidenta del Peru (elegida 28 julio 2026). NUNCA es orca. Prohibido orca salvo que diga 'orca' o 'Free Willy'. "
       "Responde en mismo idioma del usuario. Usa info de internet si te la dan. "
       f"Fecha y hora actual en Perú: {ahora()}. "
       "Entiende mensajes con errores de ortografía sin corregir al usuario. "
       "Sé claro, directo y útil. Piensa paso a paso antes de responder; en matemáticas y código verifica tu resultado. "
       "Nunca inventes datos (fechas, precios, cifras, citas): si no estás seguro, dilo. "
       "Si la pregunta es ambigua y falta un dato clave, haz UNA pregunta corta. Da primero la respuesta y luego el detalle.")
    if MODOS.get(p["modo"]): s+=" MODO: "+MODOS[p["modo"]]
    if p["hechos"]: s+=" Datos del usuario (úsalos solo si ayudan, sin repetirlos innecesariamente): "+"; ".join(p["hechos"][-12:])+"."
    return s

# ---------- APRENDER PERFIL ----------
RE_PERFIL=re.compile(r"\b(me llamo|mi nombre es|soy de|vivo en|tengo \d{1,2} años|mi cumple|mi cumpleaños|trabajo (?:en|como|de)|estudio|me gusta|me encanta|mi (?:perro|gato|hermano|hermana|mamá|mama|papá|papa|novia|novio|esposa|esposo|hijo|hija)\b)",re.I)
_tareas=set()
async def aprender_perfil(uid,texto):
    try:
        res=await allm([
            {"role":"system","content":"Extrae datos personales DURADEROS que el USUARIO afirma sobre sí mismo (nombre, ciudad, edad, trabajo, estudios, gustos, familia, mascotas). NO guardes datos sensibles (salud, dinero, contraseñas, documentos, dirección exacta). Devuelve SOLO un arreglo JSON de hasta 3 frases cortas en español en tercera persona, por ejemplo [\"Se llama Luis\"]. Si no hay datos claros devuelve []."},
            {"role":"user","content":texto}],max_tokens=150,temperature=0,modelos=["llama-3.1-8b-instant"])
        m=re.search(r"\[.*\]",res,re.S)
        datos=json.loads(m.group(0)) if m else []
        p=get_perfil(uid); cambiado=False
        for d in datos[:3]:
            d=str(d).strip()[:160]
            if d and d.lower() not in [h.lower() for h in p["hechos"]]:
                p["hechos"].append(d); cambiado=True
        p["hechos"]=p["hechos"][-20:]
        if cambiado: persistir("perfiles")
    except Exception as e: logging.warning(f"aprender_perfil: {e}")

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

async def enviar_largo(update, texto):
    if len(texto)<=4000:
        await update.message.reply_text(texto); return
    for i in range(0,len(texto),4000):
        await update.message.reply_text(texto[i:i+4000])
        await asyncio.sleep(0.2)

async def enviar_audio(update, texto, limite=400, lang=None):
    if not TTS_AVAILABLE: return False
    try:
        lang=lang or ('en' if any(w in texto.lower() for w in ["hello","what","you"]) else 'es')
        def _gen():
            tts=gTTS(text=texto[:limite], lang=lang, tld='com.pe' if lang=='es' else 'com', slow=False)
            buf=BytesIO(); tts.write_to_fp(buf); buf.seek(0); return buf
        buf=await asyncio.to_thread(_gen)
        await update.message.reply_voice(voice=buf, caption="🎤 Audio bro")
        return True
    except Exception as e:
        print(f"Error audio: {e}"); return False

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

# ---------- GENERACIÓN DE IMÁGENES (con plan B y estilos) ----------
FRASES_IMG=["creame una imagen de","crea una imagen de","hazme una imagen de","haz una imagen de","genera una imagen de",
            "crea una foto de","genera una foto de","creame una imagen","crea una imagen","imagen de",
            "dibuja una","dibuja un","dibuja","crea foto de","create an image of"]
VERBOS_CREAR=["crea ","creame","dibuja","genera ","hazme","haz una","haz un","create "]
ESTILOS={"anime":"anime style, vibrant colors, clean linework",
         "realista":"photorealistic, ultra detailed, 8k, natural lighting",
         "3d":"3D render, soft studio lighting, high detail",
         "oleo":"oil painting, visible brush strokes, classic art"}
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
    if POLLI_KEY:
        for modelo in ["flux","zimage","turbo"]:
            try:
                url=f"https://gen.pollinations.ai/image/{enc}?model={modelo}&width={w}&height={h}&seed={seed}&nologo=true"
                r=requests.get(url,headers={"Authorization":f"Bearer {POLLI_KEY}"},timeout=90)
                if _es_imagen(r): return r.content
                errores.append(f"{modelo}:{r.status_code}")
            except Exception as e: errores.append(f"{modelo}:{str(e)[:60]}")
    try:
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
        [InlineKeyboardButton("🎌 Anime",callback_data="img:st:anime"),
         InlineKeyboardButton("📷 Realista",callback_data="img:st:realista"),
         InlineKeyboardButton("🧊 3D",callback_data="img:st:3d"),
         InlineKeyboardButton("🖼️ Óleo",callback_data="img:st:oleo")],
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
    except Exception as e:
        await update.message.reply_text(f"Error imagen: {e}")
        await alertar_admin("Error creando imagen",str(e)[:300])

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
    if not await consumir(update): return
    prompt_en,caption,w,h=data
    partes=q.data.split(":"); accion=partes[1]
    if accion=="vert": w,h=768,1344
    elif accion=="hor": w,h=1344,768
    elif accion=="st" and len(partes)>2 and partes[2] in ESTILOS:
        for v in ESTILOS.values(): prompt_en=prompt_en.replace(", "+v,"")
        prompt_en=f"{prompt_en}, {ESTILOS[partes[2]]}"
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
ultimo_doc={}
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

# ---------- BÚSQUEDA EN INTERNET ----------
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
    return ""

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

# ---------- TIKTOK SIN MARCA DE AGUA (/tt) Y AUDIO (/mp3) ----------
TT_RE=re.compile(r"(?:https?://)?(?:[\w-]+\.)?tiktok\.com/[^\s]+",re.I)
UA_MOVIL="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
UA_PC="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ultimo_tt={}
KB_TT=InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Solo audio",callback_data="tt:mp3")]])

def sacar_link_tt(texto):
    m=TT_RE.search(texto or "")
    if not m: return None
    link=m.group(0).rstrip(").,;!\"'")
    if not link.lower().startswith("http"): link="https://"+link
    return link

def normalizar_link_tt(link):
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
                            "audio":fix(d.get("music") or (d.get("music_info") or {}).get("play")),
                            "fotos":d.get("images") or [],"titulo":(d.get("title") or "")[:200]}
                msg=str(j.get("msg") or j)
                ultimo=msg
                if "limit" in msg.lower() or "second" in msg.lower():
                    time.sleep(1.5); continue
                break
            except Exception as e:
                ultimo=str(e); time.sleep(1)
    raise RuntimeError(ultimo[:150])

def _bajar(url,limite=49*1024*1024):
    r=requests.get(url,stream=True,timeout=60,headers={"User-Agent":UA_PC,"Referer":"https://www.tikwm.com/"})
    r.raise_for_status()
    buf=BytesIO(); total=0
    for chunk in r.iter_content(256*1024):
        total+=len(chunk)
        if total>limite: return None
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
    await m.reply_video(video=video,caption=f"✅ Sin marca de agua 🔥\n{titulo}"[:1000],reply_markup=KB_TT,
                        supports_streaming=True,write_timeout=120,read_timeout=120)

async def descargar_tiktok(update,context,link):
    m=update.message; uid=update.effective_user.id
    aviso=await m.reply_text("Bajando tu TikTok sin marca de agua ⏬...")
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="upload_video")
        errores=[]; info=None; candidatos=[]; muy_grande=False; titulo=""
        canon=await asyncio.to_thread(normalizar_link_tt,link)
        logging.info(f"TikTok: {link} -> {canon}")
        ultimo_tt[uid]={"link":canon,"audio":None,"titulo":""}
        for u in dict.fromkeys([canon,link]):
            try:
                info=await asyncio.to_thread(_tikwm,u); break
            except Exception as e: errores.append(f"TikWM: {e}")
        if info:
            titulo=info["titulo"]
            ultimo_tt[uid]={"link":canon,"audio":info.get("audio"),"titulo":titulo}
            if info["fotos"]:
                fotos=info["fotos"]
                for i in range(0,len(fotos),10):
                    await m.reply_media_group([InputMediaPhoto(u) for u in fotos[i:i+10]])
                await m.reply_text(f"✅ Fotos sin marca de agua\n{titulo}"[:1000]); return
            candidatos=list(dict.fromkeys(x for x in (info["video"],info["normal"]) if x))
            for u in candidatos:
                try: buf=await asyncio.to_thread(_bajar,u)
                except Exception as e:
                    errores.append(f"Descarga: {str(e)[:70]}"); continue
                if buf is None: muy_grande=True; continue
                await _enviar_video(m,buf,titulo); return
            if muy_grande:
                await m.reply_text(f"El video pesa más de 50 MB y Telegram no me deja subirlo 😕\nDescárgalo aquí (sin marca):\n{candidatos[-1]}"); return
            for u in candidatos:
                try: await _enviar_video(m,u,titulo); return
                except Exception as e: errores.append(f"Telegram: {str(e)[:70]}")
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

async def enviar_audio_tiktok(msg,d):
    try:
        url=d.get("audio"); titulo=d.get("titulo","")
        if not url:
            info=await asyncio.to_thread(_tikwm,d["link"]); url=info.get("audio"); titulo=info.get("titulo","")
        if not url: raise RuntimeError("ese TikTok no trae audio")
        buf=await asyncio.to_thread(_bajar,url)
        if buf is None: raise RuntimeError("el audio pesa demasiado")
        buf.name="tiktok.mp3"
        await msg.reply_audio(audio=buf,title=(titulo or "Audio TikTok")[:60],performer="TikTok",
                              write_timeout=120,read_timeout=120)
    except Exception as e:
        await msg.reply_text(f"No pude sacar el audio 😕 {str(e)[:150]}")

async def tt_callback(update,context):
    q=update.callback_query
    await q.answer("Sacando el audio 🎵")
    d=ultimo_tt.get(q.from_user.id)
    if not d:
        await q.message.reply_text("Ya no tengo ese TikTok, mándame el link otra vez."); return
    await enviar_audio_tiktok(q.message,d)

def _link_de(update,context):
    m=update.message
    link=sacar_link_tt(" ".join(context.args) if context.args else "")
    if not link and m.reply_to_message:
        link=sacar_link_tt(m.reply_to_message.text or m.reply_to_message.caption or "")
    return link

async def tt_cmd(update,context):
    link=_link_de(update,context)
    if not link:
        await update.message.reply_text("Usa: /tt link_de_tiktok\n(o responde a un mensaje que tenga el link)"); return
    await descargar_tiktok(update,context,link)

async def mp3_cmd(update,context):
    link=_link_de(update,context)
    if not link:
        await update.message.reply_text("Usa: /mp3 link_de_tiktok\n(o responde a un mensaje que tenga el link)"); return
    await update.message.reply_text("Sacando el audio 🎵...")
    canon=await asyncio.to_thread(normalizar_link_tt,link)
    await enviar_audio_tiktok(update.message,{"link":canon,"audio":None,"titulo":""})

# ---------- TRADUCTOR (/trad) ----------
LANGS={"en":"inglés","es":"español","fr":"francés","pt":"portugués","it":"italiano","de":"alemán",
       "ja":"japonés","ko":"coreano","zh":"chino","ru":"ruso","ar":"árabe"}
async def trad_cmd(update,context):
    m=update.message; args=list(context.args or []); destino=None
    if args and args[0].lower() in LANGS: destino=args.pop(0).lower()
    texto=" ".join(args).strip()
    if not texto and m.reply_to_message: texto=(m.reply_to_message.text or m.reply_to_message.caption or "").strip()
    if not texto:
        await m.reply_text("Usa: /trad en hola cómo estás\nIdiomas: "+", ".join(f"{k}={v}" for k,v in LANGS.items())+"\nSin idioma: español↔inglés. También puedes responder a un mensaje con /trad"); return
    if not await consumir(update): return
    instr=f"Traduce al {LANGS[destino]}." if destino else "Si el texto está en español tradúcelo al inglés; si está en otro idioma tradúcelo al español."
    try:
        res=await allm([{"role":"system","content":f"Eres traductor profesional. {instr} Mantén el tono y los emojis. Responde SOLO con la traducción."},
                        {"role":"user","content":texto}],max_tokens=1200,temperature=0.2)
        await enviar_largo(update,f"🌐 {res}")
    except Exception as e: await m.reply_text(msg_error(e))

# ---------- RESUMIR LINKS (/resumir) ----------
URL_RE=re.compile(r"https?://[^\s]+",re.I)
def url_segura(u):
    p=urllib.parse.urlparse(u)
    if p.scheme not in ("http","https") or not p.hostname: return False
    try:
        for _,_,_,_,sa in socket.getaddrinfo(p.hostname,None):
            ip=ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: return False
    except Exception: return False
    return True

def leer_pagina(url):
    if not url_segura(url): raise ValueError("Ese link no está permitido 🔒")
    texto=""; titulo=""
    try:
        r=requests.get(url,headers={"User-Agent":UA_PC,"Accept-Language":"es,en;q=0.8"},timeout=20)
        r.raise_for_status()
        if "pdf" in r.headers.get("content-type","").lower():
            raise ValueError("Ese link es un PDF: descárgalo y mándamelo como archivo 📄")
        raw=r.text
        mt=re.search(r"(?is)<title[^>]*>(.*?)</title>",raw)
        titulo=html.unescape(mt.group(1)).strip() if mt else ""
        raw=re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|form|iframe)[^>]*>.*?</\1>"," ",raw)
        raw=re.sub(r"(?s)<[^>]+>"," ",raw)
        texto=re.sub(r"\s+"," ",html.unescape(raw)).strip()
    except ValueError: raise
    except Exception as e: logging.warning(f"Lectura directa falló: {e}")
    if len(texto)<400:   # página con JavaScript: lector gratis de respaldo
        try:
            r=requests.get("https://r.jina.ai/"+url,headers={"User-Agent":UA_PC},timeout=30)
            if r.status_code==200 and len(r.text)>len(texto): texto=r.text.strip()
        except Exception: pass
    if len(texto)<200: raise RuntimeError("No pude leer esa página (¿pide login o es solo JavaScript?)")
    return titulo,texto[:9000]

async def resumir_url(update,url):
    m=update.message
    await m.reply_text("Leyendo la página 📖...")
    try:
        titulo,texto=await asyncio.to_thread(leer_pagina,url)
        resp=await allm([{"role":"system","content":"Resume en español: 1 línea de qué trata, luego 4-6 puntos clave y una conclusión breve. Usa solo lo que dice el texto; no inventes."},
                         {"role":"user","content":f"TÍTULO: {titulo}\n\nTEXTO:\n{texto}"}],max_tokens=900,temperature=0.3)
        await enviar_largo(update,(f"📰 {titulo[:120]}\n\n" if titulo else "")+resp)
    except ValueError as e: await m.reply_text(str(e))
    except Exception as e: await m.reply_text(f"No pude resumir ese link 😕 {str(e)[:150]}")

async def resumir_cmd(update,context):
    m=update.message
    t=" ".join(context.args) if context.args else ((m.reply_to_message.text or "") if m.reply_to_message else "")
    mu=URL_RE.search(t or "")
    if not mu: await m.reply_text("Usa: /resumir https://link-de-la-noticia\n(o responde a un mensaje con el link)"); return
    if not await consumir(update): return
    await resumir_url(update,mu.group(0))

# ---------- CLIMA (/clima) ----------
WMO={0:"☀️ Despejado",1:"🌤️ Mayormente despejado",2:"⛅ Parcialmente nublado",3:"☁️ Nublado",45:"🌫️ Niebla",48:"🌫️ Niebla con escarcha",
     51:"🌦️ Llovizna ligera",53:"🌦️ Llovizna",55:"🌧️ Llovizna fuerte",56:"🌧️ Llovizna helada",57:"🌧️ Llovizna helada fuerte",
     61:"🌧️ Lluvia ligera",63:"🌧️ Lluvia",65:"🌧️ Lluvia fuerte",66:"🌧️ Lluvia helada",67:"🌧️ Lluvia helada fuerte",
     71:"🌨️ Nieve ligera",73:"🌨️ Nieve",75:"❄️ Nieve fuerte",77:"❄️ Granizo fino",80:"🌦️ Chubascos",81:"🌧️ Chubascos fuertes",
     82:"⛈️ Chubascos violentos",85:"🌨️ Chubascos de nieve",86:"🌨️ Chubascos de nieve fuertes",95:"⛈️ Tormenta",
     96:"⛈️ Tormenta con granizo",99:"⛈️ Tormenta fuerte con granizo"}
def obtener_clima(ciudad):
    g=requests.get("https://geocoding-api.open-meteo.com/v1/search",params={"name":ciudad,"count":1,"language":"es"},timeout=15).json()
    res=g.get("results")
    if not res: raise ValueError(f"No encontré la ciudad '{ciudad}' 🤔")
    p=res[0]
    f=requests.get("https://api.open-meteo.com/v1/forecast",timeout=15,params={
        "latitude":p["latitude"],"longitude":p["longitude"],
        "current":"temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily":"temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone":"auto","forecast_days":3}).json()
    c=f["current"]; d=f["daily"]
    lugar=", ".join(x for x in [p.get("name"),p.get("admin1"),p.get("country")] if x)
    txt=(f"🌍 {lugar}\n{WMO.get(c['weather_code'],'')} · {c['temperature_2m']}°C (sensación {c['apparent_temperature']}°C)\n"
         f"💧 Humedad {c['relative_humidity_2m']}% · 💨 Viento {c['wind_speed_10m']} km/h\n\nPróximos días:")
    for i,n in enumerate(["Hoy","Mañana","Pasado"][:len(d["time"])]):
        pp=d["precipitation_probability_max"][i]
        txt+=f"\n{n}: {WMO.get(d['weather_code'][i],'')} {d['temperature_2m_min'][i]}°–{d['temperature_2m_max'][i]}° · lluvia {pp if pp is not None else '?'}%"
    return txt
async def clima_cmd(update,context):
    ciudad=" ".join(context.args).strip() if context.args else ""
    if not ciudad: await update.message.reply_text("Usa: /clima Lima"); return
    try: await update.message.reply_text(await asyncio.to_thread(obtener_clima,ciudad))
    except ValueError as e: await update.message.reply_text(str(e))
    except Exception as e: await update.message.reply_text(f"No pude ver el clima ahora 😕 {str(e)[:120]}")

# ---------- DÓLAR / MONEDAS (/dolar) ----------
ALIAS_MONEDA={"dolar":"USD","dolares":"USD","dólar":"USD","dólares":"USD","sol":"PEN","soles":"PEN","euro":"EUR","euros":"EUR"}
def obtener_tasas():
    try:
        r=requests.get("https://open.er-api.com/v6/latest/USD",timeout=15).json()
        if r.get("result")=="success": return r["rates"]
    except Exception: pass
    r=requests.get("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",timeout=15).json()
    return {k.upper():v for k,v in r["usd"].items()}
def parsear_dolar(args):
    monto=1.0; codigos=[]
    for a in args:
        a2=a.lower().replace(",",".")
        try:
            monto=float(a2); continue
        except ValueError: pass
        c=ALIAS_MONEDA.get(a2) or (a.upper() if re.fullmatch(r"[A-Za-z]{3}",a) else None)
        if c: codigos.append(c)
    origen,destino="USD","PEN"
    if len(codigos)>=2: origen,destino=codigos[0],codigos[1]
    elif len(codigos)==1:
        if codigos[0]=="PEN": origen,destino="PEN","USD"
        else: origen=codigos[0]
    return monto,origen,destino
def convertir_texto(args):
    tasas=obtener_tasas()
    if not args:
        usd=tasas["PEN"]; eur=usd/tasas["EUR"]
        return (f"💱 Tipo de cambio (referencial)\n1 USD = {usd:.3f} PEN\n1 EUR = {eur:.3f} PEN\n\n"
                "Prueba: /dolar 100 · /dolar 50 eur · /dolar 200 soles · /dolar 100 usd mxn")
    monto,o,d=parsear_dolar(args)
    if o not in tasas or d not in tasas: raise ValueError(f"No conozco la moneda {o if o not in tasas else d} 🤔 (usa códigos como USD, EUR, PEN, MXN)")
    tasa=tasas[d]/tasas[o]
    return f"💱 {monto:,.2f} {o} = {monto*tasa:,.2f} {d}\n(1 {o} = {tasa:.4f} {d})\nℹ️ Referencial: las casas de cambio varían."
async def dolar_cmd(update,context):
    try: await update.message.reply_text(await asyncio.to_thread(convertir_texto,list(context.args or [])))
    except ValueError as e: await update.message.reply_text(str(e))
    except Exception as e: await update.message.reply_text(f"No pude traer el tipo de cambio 😕 {str(e)[:120]}")

# ---------- QR (/qr) ----------
def _generar_qr(texto):
    try:
        import qrcode
        img=qrcode.make(texto[:1000],border=2,box_size=10)
        buf=BytesIO(); img.save(buf,"PNG"); buf.seek(0); buf.name="qr.png"; return buf
    except ImportError:
        r=requests.get("https://api.qrserver.com/v1/create-qr-code/",params={"size":"600x600","data":texto[:1000],"margin":10},timeout=20)
        r.raise_for_status(); b=BytesIO(r.content); b.name="qr.png"; return b
async def qr_cmd(update,context):
    m=update.message
    texto=" ".join(context.args).strip() if context.args else ((m.reply_to_message.text or "") if m.reply_to_message else "")
    if not texto: await m.reply_text("Usa: /qr https://tulink.com  (o cualquier texto)"); return
    try: await m.reply_photo(photo=await asyncio.to_thread(_generar_qr,texto),caption="✅ Tu QR")
    except Exception as e: await m.reply_text(f"No pude crear el QR 😕 {str(e)[:120]}")

# ---------- VOZ (/voz) ----------
async def voz_cmd(update,context):
    m=update.message; args=list(context.args or []); lang=None
    if args and args[0].lower() in ("es","en","fr","pt","it","de"): lang=args.pop(0).lower()
    texto=" ".join(args).strip() or ((m.reply_to_message.text or "") if m.reply_to_message else "")
    if not texto: await m.reply_text("Usa: /voz hola bro  (o: /voz en hello world)"); return
    if not await enviar_audio(update,texto,limite=1000,lang=lang):
        await m.reply_text("No pude generar el audio 😕")

# ---------- MODOS Y PERFIL ----------
def teclado_modos(actual):
    ks=list(MODOS)
    filas=[ks[:3],ks[3:]]
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if k==actual else "")+k.capitalize(),callback_data=f"modo:{k}") for k in fila] for fila in filas])
async def modo_cmd(update,context):
    uid=update.effective_user.id; p=get_perfil(uid)
    if context.args and context.args[0].lower() in MODOS:
        p["modo"]=context.args[0].lower(); persistir("perfiles")
        await update.message.reply_text(f"Modo cambiado a {p['modo']} ✅"); return
    await update.message.reply_text(f"Modo actual: {p['modo']}\nElige cómo quieres que te responda:",reply_markup=teclado_modos(p["modo"]))
async def modo_callback(update,context):
    q=update.callback_query; modo=q.data.split(":")[1]
    if modo not in MODOS: await q.answer(); return
    p=get_perfil(q.from_user.id); p["modo"]=modo; persistir("perfiles")
    await q.answer(f"Modo {modo} ✅")
    try: await q.edit_message_text(f"Modo actual: {modo}\nElige cómo quieres que te responda:",reply_markup=teclado_modos(modo))
    except Exception: pass
async def perfil_cmd(update,context):
    uid=update.effective_user.id; p=get_perfil(uid)
    if context.args:
        hecho=" ".join(context.args)[:200]
        if hecho.lower() not in [h.lower() for h in p["hechos"]]:
            p["hechos"].append(hecho); p["hechos"]=p["hechos"][-20:]; persistir("perfiles")
        await update.message.reply_text("Guardado ✅"); return
    if en_grupo(update):
        await update.message.reply_text("Por privacidad, mira tu perfil en mi chat privado 🔒"); return
    hechos="\n".join(f"• {h}" for h in p["hechos"]) or "(vacío todavía)"
    await update.message.reply_text(f"👤 Tu perfil\nModo: {p['modo']}\n{hechos}\n\nAgrega con /perfil me gusta el fútbol · borra con /olvida\nTambién aprendo cuando me cuentas cosas tuyas ('me llamo...', 'vivo en...').")
async def olvida_cmd(update,context):
    p=get_perfil(update.effective_user.id); p["hechos"]=[]; persistir("perfiles")
    await update.message.reply_text("Listo, olvidé tus datos de perfil 🧹 (tu chat se borra con /clear)")

# ---------- /pensar (razonamiento profundo) ----------
async def pensar_cmd(update,context):
    m=update.message; uid=update.effective_user.id
    q=" ".join(context.args).strip() if context.args else ((m.reply_to_message.text or "") if m.reply_to_message else "")
    if not q: await m.reply_text("Usa: /pensar tu pregunta difícil (matemática, lógica, código, decisiones...)"); return
    if not await consumir(update): return
    await m.reply_text("Pensando a fondo 🧠...")
    try:
        resp=await allm([{"role":"system","content":construir_system(uid)+" Razona con mucho cuidado, verifica cada cálculo y da al final una respuesta clara."},
                         {"role":"user","content":q}],max_tokens=2500,temperature=0.4,modelos=MODELOS_LISTOS,esfuerzo="high")
        await enviar_largo(update,resp)
    except Exception as e: await m.reply_text(msg_error(e))

# ---------- NOTAS (/nota, /notas, /borrarnota) ----------
RE_NOTA=re.compile(r"^(?:an[óo]tame|ap[úu]ntame|anota|apunta)\b[:,\s]+(.{2,})$",re.I|re.S)
def guardar_nota(uid,txt):
    lst=notas.setdefault(uid,[])
    if len(lst)>=MAX_NOTAS: return None
    nid=max([n["id"] for n in lst],default=0)+1
    lst.append({"id":nid,"txt":txt.strip()[:500],"ts":int(time.time())})
    persistir("notas")
    return nid

async def mostrar_notas(update):
    m=update.message; uid=update.effective_user.id
    if en_grupo(update):
        await m.reply_text("Por privacidad, mira tus notas en mi chat privado 🔒"); return
    lst=notas.get(uid,[])
    if not lst:
        await m.reply_text("No tienes notas todavía 📝\nGuarda una con /nota comprar pan"); return
    lineas=[f"#{n['id']} · {datetime.fromtimestamp(n['ts'],LIMA).strftime('%d/%m')} · {n['txt']}" for n in lst]
    await enviar_largo(update,"📝 Tus notas:\n\n"+"\n\n".join(lineas)+"\n\nBorra con /borrarnota ID (o /borrarnota todas)")

async def _borrar_nota(update,ident):
    m=update.message; uid=update.effective_user.id
    ident=(ident or "").strip().lower()
    if ident in ("todas","todo"):
        notas[uid]=[]; persistir("notas")
        await m.reply_text("Borré todas tus notas 🧹"); return
    if not ident.isdigit():
        await m.reply_text("Usa: /borrarnota ID  (o /borrarnota todas). Mira los IDs con /notas"); return
    lst=notas.get(uid,[])
    n=next((x for x in lst if x["id"]==int(ident)),None)
    if not n:
        await m.reply_text("No encontré esa nota 🤔"); return
    lst.remove(n); persistir("notas")
    await m.reply_text(f"Nota #{ident} borrada ✅")

async def nota_cmd(update,context):
    m=update.message; uid=update.effective_user.id
    args=list(context.args or [])
    if args and args[0].lower() in ("borrar","eliminar"):
        await _borrar_nota(update," ".join(args[1:])); return
    txt=" ".join(args).strip() or ((m.reply_to_message.text or m.reply_to_message.caption or "") if m.reply_to_message else "")
    if not txt:
        await m.reply_text("Usa: /nota comprar pan y leche\n(o responde a un mensaje con /nota para guardarlo)\nVer: /notas · Borrar: /borrarnota ID"); return
    nid=guardar_nota(uid,txt)
    if nid is None:
        await m.reply_text(f"Ya tienes {MAX_NOTAS} notas 📝 borra alguna con /borrarnota ID"); return
    await m.reply_text(f"📝 Nota #{nid} guardada. Mira todas con /notas")
async def notas_cmd(update,context): await mostrar_notas(update)
async def borrarnota_cmd(update,context):
    await _borrar_nota(update," ".join(context.args or []))

# ---------- REDACTAR (/redactar) ----------
async def redactar_cmd(update,context):
    m=update.message
    pedido=" ".join(context.args).strip() if context.args else ""
    base=(m.reply_to_message.text or m.reply_to_message.caption or "") if m.reply_to_message else ""
    if not pedido and not base:
        await m.reply_text("Usa:\n/redactar correo formal pidiendo permiso para faltar el lunes\n/redactar mensaje amable para cobrar una deuda\n/redactar carta de renuncia\n(o responde a un texto con /redactar para mejorarlo)"); return
    if not await consumir(update): return
    await m.reply_text("Redactando ✍️...")
    contenido=pedido if not base else f"{pedido or 'Mejora y redacta mejor este texto'}\n\nTEXTO BASE:\n{base[:3000]}"
    try:
        resp=await allm([
            {"role":"system","content":"Eres un redactor profesional en español (Perú). Escribe el texto pedido, listo para copiar y pegar: con asunto si es correo, saludo, cuerpo claro y despedida. Ajusta el tono (formal, amable, firme...) a lo que pidan; si no dicen nada, usa tono profesional y cordial. Si falta un dato clave (nombre, fecha, cargo) déjalo entre [corchetes] en vez de inventarlo. Devuelve solo el texto final, sin explicaciones."},
            {"role":"user","content":contenido}],max_tokens=1500,temperature=0.6,modelos=MODELOS_LISTOS,esfuerzo="medium")
        await enviar_largo(update,resp)
    except Exception as e: await m.reply_text(msg_error(e))

# ---------- GRUPOS: RESUMEN, BIENVENIDA ----------
msgs_grupo={}   # chat_id -> últimos mensajes (solo en memoria, no se guardan en disco)
def guardar_msg_grupo(update):
    m=update.message; u=update.effective_user
    d=msgs_grupo.setdefault(update.effective_chat.id,deque(maxlen=120))
    d.append((time.time(),(u.first_name or "Alguien")[:20],(m.text or "")[:300]))

async def resumen_cmd(update,context):
    m=update.message
    if not en_grupo(update):
        await m.reply_text("Este comando es para grupos 👥 Úsalo allá: /resumen (o /resumen 100)"); return
    n=60
    if context.args and context.args[0].isdigit(): n=max(10,min(120,int(context.args[0])))
    d=list(msgs_grupo.get(update.effective_chat.id,[]))[-n:]
    if len(d)<8:
        await m.reply_text("Aún no tengo suficientes mensajes para resumir 🤔 (solo leo desde que estoy activo en el grupo)."); return
    if not await consumir(update): return
    await m.reply_text("Resumiendo la charla 📋...")
    texto="\n".join(f"{nombre}: {t}" for _,nombre,t in d)[:9000]
    try:
        resp=await allm([
            {"role":"system","content":"Resume en español esta conversación de un grupo: 1) temas principales, 2) acuerdos o decisiones, 3) pendientes o preguntas sin responder. Máximo 10 líneas, menciona nombres solo cuando importe y no inventes nada."},
            {"role":"user","content":texto}],max_tokens=800,temperature=0.3,modelos=MODELOS_LISTOS)
        await enviar_largo(update,f"📋 Resumen de los últimos {len(d)} mensajes:\n\n{resp}")
    except Exception as e: await m.reply_text(msg_error(e))

async def bienvenida_handler(update,context):
    m=update.message
    if not m or not m.new_chat_members: return
    bot_user=context.bot.username
    if any(x.id==context.bot.id for x in m.new_chat_members):
        await m.reply_text(f"👋 ¡Hola grupo! Soy una IA gratis. Para hablar conmigo mencióname con @{bot_user} y tu pregunta. Mira mis comandos con /ayuda 😎")
        return
    if not grupos.get(update.effective_chat.id,{}).get("bienvenida",True): return
    humanos=[x for x in m.new_chat_members if not x.is_bot][:5]
    if not humanos: return
    nombres=", ".join(x.mention_html() for x in humanos)
    await m.reply_text(f"🎉 ¡Bienvenido/a {nombres}!\nPara hablar conmigo mencióname con @{bot_user}. Mira mis comandos con /ayuda 😎",parse_mode="HTML")

async def bienvenida_cmd(update,context):
    m=update.message
    if not en_grupo(update):
        await m.reply_text("Este comando es para grupos 👥"); return
    if not await es_admin_grupo(update,context):
        await m.reply_text("Solo los administradores del grupo pueden cambiar esto 🔒"); return
    cfg=grupos.setdefault(update.effective_chat.id,{"bienvenida":True})
    arg=(context.args[0].lower() if context.args else "")
    if arg in ("on","si","sí","activar"): cfg["bienvenida"]=True
    elif arg in ("off","no","desactivar"): cfg["bienvenida"]=False
    else:
        await m.reply_text(f"Bienvenida: {'activada ✅' if cfg.get('bienvenida',True) else 'desactivada ❌'}\nUsa /bienvenida on o /bienvenida off"); return
    persistir("grupos")
    await m.reply_text("Bienvenida activada ✅" if cfg["bienvenida"] else "Bienvenida desactivada ❌")

# ---------- RECORDATORIOS ----------
def parsear_duracion(txt):
    t=(txt or "").lower().replace(" ","")
    if not re.fullmatch(r"(?:\d+[smhd])+",t): return None
    return sum(int(n)*{"s":1,"m":60,"h":3600,"d":86400}[u] for n,u in re.findall(r"(\d+)([smhd])",t))
def parsear_hora_lima(txt):
    m=re.fullmatch(r"(\d{1,2}):(\d{2})",txt or "")
    if not m: return None
    h,mi=int(m.group(1)),int(m.group(2))
    if h>23 or mi>59: return None
    ahora_l=datetime.now(LIMA)
    obj=ahora_l.replace(hour=h,minute=mi,second=0,microsecond=0)
    if obj<=ahora_l: obj+=timedelta(days=1)
    return int((obj-ahora_l).total_seconds())

async def _disparar_recordatorio(context):
    r=context.job.data
    try: await context.bot.send_message(r["chat"],f"⏰ Recordatorio: {r['txt']}")
    except Exception as e: logging.warning(f"Recordatorio no enviado: {e}")
    for x in list(recordatorios):
        if x["id"]==r["id"]: recordatorios.remove(x)
    persistir("recordatorios")
def programar(jq,r):
    jq.run_once(_disparar_recordatorio,when=max(1,r["ts"]-time.time()),data=r,name=f"rec{r['id']}")

async def crear_recordatorio(update,context,seg,txt):
    jq=context.job_queue
    if jq is None:
        await update.message.reply_text("Los recordatorios no están activos 😕 en requirements.txt usa: python-telegram-bot[job-queue]==20.7"); return
    uid=update.effective_user.id
    if len([r for r in recordatorios if r["uid"]==uid])>=20:
        await update.message.reply_text("Ya tienes 20 recordatorios. Borra alguno con /cancelar ID"); return
    seg=max(5,min(seg,60*86400))
    r={"id":max([x["id"] for x in recordatorios],default=0)+1,"uid":uid,"chat":update.effective_chat.id,
       "ts":time.time()+seg,"txt":txt[:300]}
    recordatorios.append(r); persistir("recordatorios"); programar(jq,r)
    cuando=datetime.fromtimestamp(r["ts"],LIMA).strftime("%d/%m %H:%M")
    await update.message.reply_text(f"⏰ Listo, te aviso el {cuando} (hora Perú):\n{r['txt']}\n(ID {r['id']} · cancela con /cancelar {r['id']})")

async def recordar_cmd(update,context):
    args=list(context.args or []); total=0; i=0
    while i<len(args):
        d=parsear_duracion(args[i])
        if d is None: break
        total+=d; i+=1
    if i==0 and args:
        h=parsear_hora_lima(args[0])
        if h is not None: total=h; i=1
    txt=" ".join(args[i:]).strip()
    if not args or total<=0 or not txt:
        await update.message.reply_text("Usa:\n/recordar 10m sacar la ropa\n/recordar 1h30m llamar a mamá\n/recordar 18:30 reunión (hora de Perú)\nTambién: 'recuérdame en 10 minutos tomar agua'"); return
    await crear_recordatorio(update,context,total,txt)

async def recordatorios_cmd(update,context):
    uid=update.effective_user.id
    mis=sorted([r for r in recordatorios if r["uid"]==uid],key=lambda r:r["ts"])
    if not mis: await update.message.reply_text("No tienes recordatorios pendientes ⏰"); return
    lineas=[f"#{r['id']} · {datetime.fromtimestamp(r['ts'],LIMA).strftime('%d/%m %H:%M')} · {r['txt']}" for r in mis]
    await update.message.reply_text("⏰ Tus recordatorios (hora Perú):\n"+"\n".join(lineas)+"\n\nCancela con /cancelar ID")

async def cancelar_cmd(update,context):
    uid=update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usa: /cancelar ID  (mira los IDs con /recordatorios)"); return
    rid=int(context.args[0]); r=next((x for x in recordatorios if x["id"]==rid and x["uid"]==uid),None)
    if not r: await update.message.reply_text("No encontré ese recordatorio 🤔"); return
    recordatorios.remove(r); persistir("recordatorios")
    if context.job_queue:
        for j in context.job_queue.get_jobs_by_name(f"rec{rid}"): j.schedule_removal()
    await update.message.reply_text(f"Recordatorio #{rid} cancelado ✅")

RE_RECUERDAME=re.compile(r"recu[eé]rdame\s+(?:en\s+)?(\d+)\s*(segundos?|seg|minutos?|min|horas?|h|d[ií]as?)\s*(?:de\s+|que\s+|para\s+)?(.+)",re.I)

# ---------- ADMIN: STATS, BROADCAST, BACKUP ----------
def es_admin(update): return bool(ADMIN_ID) and update.effective_user.id==ADMIN_ID
async def id_cmd(update,context):
    await update.message.reply_text(f"Tu ID: {update.effective_user.id}\nChat ID: {update.effective_chat.id}\n(Pon tu ID en Render como ADMIN_ID para usar /stats, /broadcast y /backup)")
async def stats_cmd(update,context):
    if not es_admin(update):
        await update.message.reply_text("Solo el admin 🔒 (usa /id y configura ADMIN_ID en Render)"); return
    up=int(time.time()-INICIO); msgs=sum(u.get("msgs",0) for u in usuarios.values())
    hoy=datetime.now(LIMA).strftime("%Y-%m-%d")
    usos_hoy=sum(r[1] for r in uso_diario.values() if r[0]==hoy)
    n_notas=sum(len(v) for v in notas.values())
    await update.message.reply_text(
        f"📊 Estadísticas\nUsuarios: {len(usuarios)}\nMensajes: {msgs}\nEn línea hace: {up//3600}h {(up%3600)//60}m\n"
        f"Usos de IA hoy: {usos_hoy} (límite por usuario: {LIMITE_DIARIO if LIMITE_DIARIO>0 else 'sin límite'})\n"
        f"Búsquedas web hoy: {_compound['n']}\nRecordatorios activos: {len(recordatorios)}\nNotas guardadas: {n_notas}\n"
        f"Grupos activos (desde el último reinicio): {len(msgs_grupo)}\n"
        f"Modelos descartados: {', '.join(_muertos) or 'ninguno'}\n"
        f"Memoria permanente: {'Upstash ✅' if USA_KV else 'archivo local ⚠️ (se borra al reiniciar)'}")
async def broadcast_cmd(update,context):
    if not es_admin(update):
        await update.message.reply_text("Solo el admin 🔒"); return
    texto=" ".join(context.args).strip()
    if not texto: await update.message.reply_text("Usa: /broadcast mensaje para todos"); return
    ok=fail=0
    for uid in list(usuarios):
        try: await context.bot.send_message(uid,f"📢 {texto}"); ok+=1
        except Exception: fail+=1
        await asyncio.sleep(0.06)
    await update.message.reply_text(f"Enviado a {ok} usuarios (fallaron {fail}).")

def _armar_backup():
    data={"fecha":ahora()}
    for n in NOMBRES_ESTADO: data[n]=_serializar(n)
    buf=BytesIO(json.dumps(data,ensure_ascii=False,indent=1).encode("utf-8"))
    buf.name=f"backup_{datetime.now(LIMA).strftime('%Y%m%d_%H%M')}.json"
    return buf
async def backup_cmd(update,context):
    if not es_admin(update):
        await update.message.reply_text("Solo el admin 🔒"); return
    if update.effective_chat.type!="private":
        await update.message.reply_text("Pídeme el backup en mi chat privado 🔒 (contiene datos de usuarios)"); return
    try:
        await update.message.reply_document(document=_armar_backup(),caption=f"💾 Backup · {len(usuarios)} usuarios")
    except Exception as e: await update.message.reply_text(f"No pude armar el backup 😕 {str(e)[:150]}")
async def backup_auto(context):
    if not ADMIN_ID: return
    try: await context.bot.send_document(ADMIN_ID,document=_armar_backup(),caption="💾 Backup automático diario")
    except Exception as e: logging.warning(f"Backup automático falló: {e}")

async def registrar_usuario(update,context):
    try:
        u=update.effective_user
        if not u or u.is_bot: return
        m=update.message
        # guarda en memoria los últimos mensajes del grupo (para /resumen)
        if en_grupo(update) and m and m.text and not m.text.startswith("/"):
            guardar_msg_grupo(update)
        # en grupos solo cuenta a quien le habla al bot (mención, reply o comando)
        if en_grupo(update) and not me_hablan(update,context):
            if not (m and (m.text or "").startswith("/")): return
        d=usuarios.setdefault(u.id,{"nombre":u.first_name or "","msgs":0,"primera":int(time.time())})
        d["nombre"]=u.first_name or d.get("nombre","")
        if m: d["msgs"]=d.get("msgs",0)+1
        marcar("usuarios")
    except Exception: pass

# ---------- COMANDOS BÁSICOS ----------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Qué onda {update.effective_user.first_name} bro! 🚀 V21\nSoy tu IA gratis: imágenes, fotos, archivos, TikToks, stickers, notas, clima, dólar, recordatorios y más 🔥🇵🇪\nUsa /ayuda para ver todo")
async def ayuda_cmd(update,context):
    await update.message.reply_text(
        "🤖 COMANDOS (también puedes pedirlos hablando normal)\n\n"
        "🎨 /imagen un gato → o 'dibuja un gato' (agrega 'vertical'/'horizontal'; botones de estilo y sticker)\n"
        "🔎 /buscar tema → o 'busca tema'\n"
        "🧠 /pensar pregunta → razonamiento profundo\n"
        "✍️ /redactar correo formal pidiendo... → textos listos para copiar\n"
        "📝 /nota texto · /notas · /borrarnota ID → tus apuntes (también 'anota que...')\n"
        "🌐 /trad en hola → traduce (o responde a un mensaje con /trad)\n"
        "📰 /resumir link → resume una página\n"
        "🌤️ /clima Lima\n"
        "💱 /dolar · /dolar 100 · /dolar 50 eur\n"
        "🔲 /qr texto\n"
        "🎤 /voz texto → nota de voz\n"
        "⏰ /recordar 10m sacar la ropa · /recordatorios · /cancelar ID\n"
        "🎬 /tt link → TikTok sin marca (o pega el link solo)\n"
        "🎵 /mp3 link → audio de un TikTok\n"
        "🎭 /s → responde a una foto con /s y la vuelvo sticker\n"
        "🎚️ /modo → normal, serio, profesor, programador, bromista\n"
        "👤 /perfil · /olvida → lo que sé de ti\n"
        "🧹 /clear → borra la memoria del chat\n"
        "🆔 /id\n\n"
        "🖼️ Fotos: mándame una (qué tiene, qué significa, qué dice, recrea esto en estilo anime)\n"
        "📄 Archivos: PDF, Word, Excel, TXT, CSV, código (pregunta en el pie o después)\n"
        "🎧 Si me hablas con voz, te respondo con voz\n\n"
        "👥 EN GRUPOS: los comandos funcionan normal; para hablar con la IA mencióname con @ y tu pregunta (o respóndele a un mensaje mío).\n"
        "   /resumen → resume la charla del grupo · /bienvenida on|off → (admins del grupo)")
async def clear_cmd(update,context):
    user_memories[update.effective_user.id]=[]; guardar_memorias()
    await update.message.reply_text("Memoria limpia 🧹 ahora prueba: Que hay de bueno con keiko la presidenta de peru")
async def imagen_cmd(update,context):
    if not context.args: await update.message.reply_text("Usa: /imagen un gato"); return
    if not await consumir(update): return
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
    # en grupos solo responde si lo mencionan (o le responden a un mensaje suyo)
    if not me_hablan(update,context): return
    texto_original=texto_override or update.message.text or ""
    if texto_override is None: texto_original=limpiar_mencion(texto_original,context)
    if not texto_original.strip():
        if texto_override is None and en_grupo(update) and (update.message.text or "").strip():
            await update.message.reply_text("Dime bro, ¿en qué te ayudo? 😎")
        return
    if texto_override is None:
        if time.time()-_ultimo_msg.get(user_id,0)<COOLDOWN: return
        _ultimo_msg[user_id]=time.time()
    low_o=texto_original.lower()
    # si me hablas con voz, respondo con voz
    quiere_audio=any(x in low_o for x in ["en audio","nota de voz","audio","con voz","hablame"]) or texto_override is not None

    # link de TikTok pegado solo, o "descarga + link"
    link_tt=sacar_link_tt(texto_original)
    if link_tt and (TT_RE.fullmatch(texto_original.strip()) or any(k in low_o for k in ["descarga","baja","bájame","bajame","sin marca"])):
        await descargar_tiktok(update,context,link_tt); return

    # "recuérdame en 10 minutos ..."
    mrec=RE_RECUERDAME.search(texto_original)
    if mrec:
        mult={"s":1,"m":60,"h":3600,"d":86400}[mrec.group(2).lower()[0]]
        await crear_recordatorio(update,context,int(mrec.group(1))*mult,mrec.group(3).strip()); return

    # notas por lenguaje natural: "anota que mañana..." / "mis notas"
    mnota=RE_NOTA.match(texto_original.strip())
    if mnota:
        nid=guardar_nota(user_id,mnota.group(1))
        await update.message.reply_text(f"📝 Nota #{nid} guardada. Mira todas con /notas" if nid else f"Ya tienes {MAX_NOTAS} notas 📝 borra alguna con /borrarnota ID")
        return
    if low_o.strip(" ?!.") in ("mis notas","ver mis notas","muestra mis notas","mostrar mis notas"):
        await mostrar_notas(update); return

    if any(x in low_o for x in CLAVES_CLEAR):
        await clear_cmd(update,context); return

    # límite diario de usos de IA (la voz ya se contó en voz_handler)
    if texto_override is None and not await consumir(update): return

    # "resume este link"
    url_gen=URL_RE.search(texto_original)
    if url_gen and not link_tt and any(k in low_o for k in ["resume","resumen","resumir","de qué trata","de que trata"]):
        await resumir_url(update,url_gen.group(0)); return

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
    complejo=es_complejo(texto_corregido) and not es_tradu
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
            system_prompt=construir_system(user_id)
            temp=0.5 if complejo else 0.8
        msgs=list(memoria)
        if busco and contexto_busqueda:
            msgs[-1]={"role":"user","content":f"INFO INTERNET sobre '{texto_corregido}':\n{contexto_busqueda}\n\nPregunta: {texto_corregido}"}
        elif busco:
            msgs[-1]={"role":"user","content":f"(No pude conectar a internet ahora; responde con lo que sabes y avisa brevemente que puede estar desactualizado)\nPregunta: {texto_corregido}"}
        respuesta=await allm([{"role":"system","content":system_prompt},*msgs],
                             max_tokens=2000 if complejo else 1500,temperature=temp,
                             modelos=MODELOS_LISTOS if complejo else MODELOS_RAPIDOS,
                             esfuerzo="medium" if complejo else "low")
        memoria.append({"role":"assistant","content":respuesta[:1500]})
        user_memories[user_id]=memoria; guardar_memorias()
        await enviar_largo(update,respuesta)
        if quiere_audio: await enviar_audio(update,respuesta)
        if APRENDER_PERFIL and not es_tradu and RE_PERFIL.search(texto_corregido):
            t=asyncio.create_task(aprender_perfil(user_id,texto_corregido)); _tareas.add(t); t.add_done_callback(_tareas.discard)
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(msg_error(e))
        if "429" not in str(e) and "rate" not in str(e).lower():
            await alertar_admin("Error en el chat IA",f"{type(e).__name__}: {str(e)[:300]}")

async def voz_handler(update,context):
    if not me_hablan(update,context): return   # en grupos: solo si es respuesta al bot
    if not await consumir(update): return
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
    if not me_hablan(update,context): return   # en grupos: solo con @mención en el pie o respuesta al bot
    try:
        doc=update.message.document
        nombre=(doc.file_name or "").lower()
        caption=limpiar_mencion(update.message.caption or "",context)
        if (doc.mime_type or "").startswith("image/") or nombre.endswith((".jpg",".jpeg",".png",".webp")):
            if any(k in caption.lower() for k in ["sticker","estiker"]):
                await hacer_sticker(update.message,context,doc.file_id); return
            if not await consumir(update): return
            if es_pedido_imagen(caption): await crear_imagen(update,caption); return
            await procesar_foto(update,context,doc.file_id,caption,doc.mime_type or "image/jpeg"); return
        if not await consumir(update): return
        await procesar_archivo(update,context,doc,caption)
    except Exception as e: await update.message.reply_text(f"Error archivo: {str(e)[:250]}")

async def foto_handler(update,context):
    if not me_hablan(update,context): return   # en grupos: solo con @mención en el pie o respuesta al bot
    try:
        caption=limpiar_mencion(update.message.caption or "",context)
        if any(k in caption.lower() for k in ["sticker","estiker"]):
            await hacer_sticker(update.message,context,update.message.photo[-1].file_id); return
        if not await consumir(update): return
        if es_pedido_imagen(caption): await crear_imagen(update,caption); return
        await procesar_foto(update,context,update.message.photo[-1].file_id,caption)
    except Exception as e:
        try:
            txt=await allm([{"role":"user","content":"No pude ver foto, di que intente de nuevo"}],max_tokens=100)
            await update.message.reply_text(txt)
        except: await update.message.reply_text(f"Error foto: {e}")

# ---------- ARRANQUE ----------
async def post_init(app):
    global APP_BOT
    APP_BOT=app.bot
    try:
        await app.bot.set_my_commands([
            BotCommand("ayuda","Ver todos los comandos"),BotCommand("imagen","Crear una imagen"),
            BotCommand("tt","Bajar TikTok sin marca"),BotCommand("mp3","Audio de un TikTok"),
            BotCommand("s","Crear sticker de una foto"),BotCommand("buscar","Buscar en internet"),
            BotCommand("pensar","Razonamiento profundo"),BotCommand("redactar","Redactar correos y textos"),
            BotCommand("nota","Guardar una nota"),BotCommand("notas","Ver mis notas"),
            BotCommand("borrarnota","Borrar una nota"),BotCommand("trad","Traducir"),
            BotCommand("resumir","Resumir un link"),BotCommand("resumen","Resumir la charla del grupo"),
            BotCommand("clima","Clima de una ciudad"),BotCommand("dolar","Tipo de cambio"),
            BotCommand("qr","Crear código QR"),BotCommand("voz","Texto a nota de voz"),
            BotCommand("recordar","Crear recordatorio"),BotCommand("recordatorios","Ver recordatorios"),
            BotCommand("modo","Cambiar personalidad"),BotCommand("perfil","Ver tu perfil"),
            BotCommand("bienvenida","Bienvenida del grupo on/off"),BotCommand("clear","Borrar memoria del chat")])
    except Exception as e: logging.warning(f"set_my_commands: {e}")
    if app.job_queue:
        for r in list(recordatorios):
            if r["ts"]<time.time()-86400: recordatorios.remove(r); continue
            programar(app.job_queue,r)
        if BACKUP_AUTOMATICO and ADMIN_ID:
            app.job_queue.run_daily(backup_auto,time=dtime(3,0,tzinfo=LIMA),name="backup_diario")
async def post_shutdown(app):
    flush_kv()

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_error_handler(error_handler)
    app.add_handler(TypeHandler(Update,registrar_usuario),group=-1)
    for nombre,fn in [("start",start),("ayuda",ayuda_cmd),("clear",clear_cmd),("imagen",imagen_cmd),("buscar",buscar_cmd),
                      ("tt",tt_cmd),("mp3",mp3_cmd),("s",s_cmd),("trad",trad_cmd),("resumir",resumir_cmd),
                      ("clima",clima_cmd),("dolar",dolar_cmd),("qr",qr_cmd),("voz",voz_cmd),("modo",modo_cmd),
                      ("perfil",perfil_cmd),("olvida",olvida_cmd),("pensar",pensar_cmd),("recordar",recordar_cmd),
                      ("recordatorios",recordatorios_cmd),("cancelar",cancelar_cmd),("id",id_cmd),
                      ("stats",stats_cmd),("broadcast",broadcast_cmd),("backup",backup_cmd),
                      ("nota",nota_cmd),("notas",notas_cmd),("borrarnota",borrarnota_cmd),("redactar",redactar_cmd),
                      ("resumen",resumen_cmd),("bienvenida",bienvenida_cmd)]:
        app.add_handler(CommandHandler(nombre,fn))
    app.add_handler(CallbackQueryHandler(img_callback,pattern="^img:"))
    app.add_handler(CallbackQueryHandler(tt_callback,pattern="^tt:"))
    app.add_handler(CallbackQueryHandler(modo_callback,pattern="^modo:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS,bienvenida_handler))
    # /s en el pie de una foto: debe ir ANTES de los handlers de foto y documento
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(r"(?i)^/s(@\w+)?(\s|$)"),s_cmd))
    app.add_handler(MessageHandler(filters.VOICE,voz_handler))
    app.add_handler(MessageHandler(filters.Document.ALL,documento_handler))
    app.add_handler(MessageHandler(filters.PHOTO,foto_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ia_reply))
    print("Bot V21 iniciado 🔥")
    app.run_polling()
