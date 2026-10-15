import os, json, threading, logging, asyncio, base64, urllib.parse, requests, re, random, time, socket, ipaddress, html
import subprocess, tempfile, shutil
from io import BytesIO
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, time as dtime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand, ChatPermissions
from telegram.error import NetworkError, Conflict
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
                          TypeHandler, ApplicationHandlerStop, filters, ContextTypes)
from groq import Groq

logging.basicConfig(level=logging.INFO)
for _n in ("httpx","httpcore","telegram","apscheduler","urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)   # sin esto los logs muestran el token del bot dentro de las URLs

try: from pypdf import PdfReader
except Exception:
    try: from PyPDF2 import PdfReader
    except Exception: PdfReader=None
PDF_AVAILABLE = PdfReader is not None
try: import docx
except Exception: docx=None
try: import openpyxl
except Exception: openpyxl=None
try: import fitz   # pymupdf: para leer PDFs escaneados
except Exception: fitz=None
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
def home(): return "Bot V27 Estable y Seguro - Online"
@flask_app.route('/health')
def health(): return "ok"
def run_flask():
    port=int(os.environ.get("PORT",10000))
    flask_app.run(host="0.0.0.0",port=port)
threading.Thread(target=run_flask,daemon=True).start()

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
TEXTO_MAX=10000            # hasta aquí el archivo se lee entero; si es más largo se lee por partes
TEXTO_LARGO_MAX=60000      # máximo de caracteres que guarda de un archivo/transcripción
TROZO=7000                 # tamaño de cada parte al leer archivos largos
MAX_TROZOS_NOTAS=8         # cuántas partes resume (cuida los límites gratis)
MAX_TG_DESCARGA=20*1024*1024   # Telegram solo deja a los bots bajar archivos de hasta 20 MB
AUDIO_MAX_SEG=1200         # de un audio/video transcribe como máximo 20 minutos
FRAMES_VIDEO=4             # fotogramas que mira de cada video
PDF_PAGINAS_OCR=6          # páginas que lee con visión en un PDF escaneado
MAX_CONCURRENTES=32        # mensajes que atiende a la vez
MAX_PESADOS=3              # tareas pesadas a la vez (imágenes, videos, TikToks, archivos)
INTERVALO_GUARDADO=30      # cada cuántos segundos guarda los cambios
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
NOMBRES_ESTADO=("memorias","perfiles","usuarios","recordatorios","notas","grupos")
_sucio=set()    # (estado, clave) que faltan por guardar (clave=None en la lista de recordatorios)
_kv_ok=set()    # estados que se pudieron leer de Upstash (si no se leyó bien, no se escribe encima de lo guardado)

def _estado(n): return {"memorias":user_memories,"perfiles":perfiles,"usuarios":usuarios,"recordatorios":recordatorios,"notas":notas,"grupos":grupos}[n]
def _k(k):
    try: return int(k)
    except (ValueError,TypeError): return k
def _valor(n,k):
    v=_estado(n).get(k)
    if v is None: return None
    return v[-20:] if n=="memorias" else v
def _serializar_todo(n):
    if n=="recordatorios": return list(recordatorios)
    return {str(k):(v[-20:] if n=="memorias" else v) for k,v in list(_estado(n).items())}

def _kv_h(): return {"Authorization":f"Bearer {UPSTASH_TOKEN}"}
def _kv_cmd(cmd):
    """Un comando de Upstash. Devuelve (ok, resultado). No registra el contenido (privacidad)."""
    try:
        r=requests.post(UPSTASH_URL,headers=_kv_h(),json=cmd,timeout=20)
        if r.status_code!=200: return False,None
        j=r.json()
        if "error" in j: return False,None
        return True,j.get("result")
    except Exception as e:
        logging.warning(f"Upstash falló: {type(e).__name__}"); return False,None
def kv_get(key):
    """('ok',valor) · ('vacio',None) si no existe · ('error',None) si no se pudo leer."""
    ok,res=_kv_cmd(["GET",key])
    if not ok: return "error",None
    if res is None: return "vacio",None
    try: return "ok",json.loads(res)
    except Exception: return "error",None
def kv_hgetall(key):
    ok,res=_kv_cmd(["HGETALL",key])
    if not ok: return "error",{}
    if isinstance(res,dict): return ("ok" if res else "vacio"),res
    res=res or []
    d=dict(zip(res[0::2],res[1::2]))
    return ("ok" if d else "vacio"),d
def _kv_pipeline(cmds):
    """Manda varios comandos juntos. Devuelve una lista de True/False (uno por comando)."""
    try:
        r=requests.post(f"{UPSTASH_URL}/pipeline",headers=_kv_h(),json=cmds,timeout=30)
        if r.status_code==200:
            res=r.json()
            if isinstance(res,list) and len(res)==len(cmds):
                return [isinstance(x,dict) and "error" not in x for x in res]
    except Exception as e:
        logging.warning(f"Upstash (lote) falló: {type(e).__name__}")
    return [_kv_cmd(c)[0] for c in cmds]   # respaldo: uno por uno

def _leer_estado_kv(n):
    """Lee un estado de Upstash. Devuelve (estado, datos): 'ok', 'vacio', 'migrar' (había datos del formato anterior) o 'error'."""
    if n=="recordatorios":
        est,val=kv_get("bot:recordatorios")
        if est=="error": return "error",[]
        return est,(val if isinstance(val,list) else [])
    est,pares=kv_hgetall("bot:h:"+n)
    if est=="error": return "error",{}
    if est=="ok":
        out={}
        for k,s in pares.items():
            try: out[_k(k)]=json.loads(s)
            except Exception: pass
        return "ok",out
    est2,val=kv_get(f"bot:{n}")      # formato anterior: todo en una sola clave
    if est2=="error": return "error",{}
    if est2=="ok" and isinstance(val,dict):
        return "migrar",{_k(k):v for k,v in val.items()}
    return "vacio",{}

def _aplicar_datos(n,datos,migrar=False):
    if n=="recordatorios":
        if not recordatorios:
            recordatorios.extend(datos)            # arranque normal: se conservan los IDs
        else:                                       # recuperación tras una caída de Upstash: se unen sin repetir
            existentes={(r.get("uid"),r.get("ts"),r.get("txt")) for r in recordatorios}
            for r in datos:
                if (r.get("uid"),r.get("ts"),r.get("txt")) not in existentes:
                    r=dict(r); r["id"]=max([x["id"] for x in recordatorios],default=0)+1; recordatorios.append(r)
            _sucio.add(("recordatorios",None))
        return
    est=_estado(n)
    for k,v in datos.items():
        if k not in est:
            est[k]=v
            if migrar: _sucio.add((n,k))

def cargar_estado():
    for n in NOMBRES_ESTADO:
        if USA_KV:
            est,datos=_leer_estado_kv(n)
            if est=="error":
                logging.warning(f"No pude leer '{n}' de Upstash: no escribiré encima hasta lograrlo (reintenta solo).")
                continue
            _kv_ok.add(n)
            _aplicar_datos(n,datos,migrar=(est=="migrar"))
        else:
            try:
                with open(f"{n}.json",'r',encoding='utf-8') as f: data=json.load(f)
                if n=="recordatorios": _aplicar_datos(n,data if isinstance(data,list) else [])
                else: _aplicar_datos(n,{_k(k):v for k,v in data.items()})
            except Exception: pass
    print(f"Estado cargado | usuarios:{len(usuarios)} memorias:{len(user_memories)} KV:{USA_KV} leído:{sorted(_kv_ok) if USA_KV else 'local'}")
cargar_estado()

def marcar(n,k=None): _sucio.add((n,k))
def persistir(n,k=None):
    """Marca algo como 'pendiente de guardar' (se guarda en lote cada pocos segundos)."""
    if n=="recordatorios": _sucio.add((n,None)); return
    if k is None:
        for kk in list(_estado(n)): _sucio.add((n,kk))
    else: _sucio.add((n,k))
def guardar_memorias(k=None): persistir("memorias",k)

def _escribir_kv(items):
    """(en un hilo) items=[(estado,clave,json)] -> devuelve las claves que NO se pudieron guardar."""
    fallidos=[]; lote=[]; peso=0
    def enviar():
        nonlocal lote,peso
        if not lote: return
        res=_kv_pipeline([c for c,_ in lote])
        res=list(res)+[False]*(len(lote)-len(res))
        for (c,clave),ok in zip(lote,res):
            if not ok: fallidos.append(clave)
        lote=[]; peso=0
    for n,k,s in items:
        if n not in _kv_ok: fallidos.append((n,k)); continue
        cmd=["SET","bot:recordatorios",s] if n=="recordatorios" else ["HSET","bot:h:"+n,str(k),s]
        lote.append((cmd,(n,k))); peso+=len(s)
        if peso>500000 or len(lote)>=100: enviar()
    enviar()
    return fallidos
def _borrar_kv(items):
    if USA_KV and items:
        _kv_pipeline([["HDEL","bot:h:"+n,str(k)] for n,k in items])
def _escribir_local(datos):
    for n,s in datos.items():
        try:
            with open(f"{n}.json",'w',encoding='utf-8') as f: f.write(s)
        except Exception as e: print(f"Error guardado local {n}: {e}")

async def flush_async():
    """Guarda los cambios pendientes. Se llama cada pocos segundos y al apagar."""
    if USA_KV:
        for n in NOMBRES_ESTADO:
            if n not in _kv_ok:    # reintenta leer lo que falló al arrancar; recién entonces se permite escribir
                est,datos=await asyncio.to_thread(_leer_estado_kv,n)
                if est!="error":
                    _kv_ok.add(n); _aplicar_datos(n,datos,migrar=(est=="migrar"))
    if not _sucio: return
    lote=list(_sucio); _sucio.clear()
    if USA_KV:
        items=[]
        for n,k in lote:
            try:
                if n=="recordatorios": items.append((n,None,json.dumps(list(recordatorios),ensure_ascii=False)))
                else:
                    v=_valor(n,k)
                    if v is not None: items.append((n,k,json.dumps(v,ensure_ascii=False)))
            except Exception as e: logging.warning(f"No pude preparar {n}: {type(e).__name__}")
        fallidos=await asyncio.to_thread(_escribir_kv,items)
        for f in fallidos: _sucio.add(f)
    else:
        datos={}
        for n in {n for n,_ in lote}:
            try: datos[n]=json.dumps(_serializar_todo(n),ensure_ascii=False)
            except Exception as e: logging.warning(f"No pude preparar {n}: {type(e).__name__}")
        await asyncio.to_thread(_escribir_local,datos)

# ---------- MEMORIA DEL CHAT: SEPARADA ENTRE PRIVADO Y CADA GRUPO ----------
def ckey(update):
    """Clave de memoria: en privado es el id del usuario; en grupos 'grupo:usuario' (así no se mezclan las conversaciones)."""
    u=update.effective_user; c=update.effective_chat
    if c and c.type in ("group","supergroup"): return f"{c.id}:{u.id}"
    return u.id
def get_memory(k):
    if k not in user_memories: user_memories[k]=[]
    return user_memories[k]
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

# ---------- VARIOS MENSAJES A LA VEZ: LÍMITES Y CANDADOS ----------
_sem=None
def sem_pesado():
    global _sem
    if _sem is None: _sem=asyncio.Semaphore(MAX_PESADOS)
    return _sem
_ocupados=set()        # (usuario, tipo de tarea) en curso
@asynccontextmanager
async def pesado(update,tipo,aviso="Estoy terminando tu pedido anterior, espera un momento ⏳"):
    """Para tareas pesadas: máximo MAX_PESADOS a la vez en todo el bot y una de cada tipo por persona."""
    u=update.effective_user
    k=(u.id if u else 0,tipo)
    if k in _ocupados:
        try: await update.effective_message.reply_text(aviso)
        except Exception: pass
        yield False
        return
    _ocupados.add(k)
    try:
        async with sem_pesado():
            yield True
    finally:
        _ocupados.discard(k)
_locks_usuario={}
def lock_de(k):
    l=_locks_usuario.get(k)
    if l is None: l=_locks_usuario[k]=asyncio.Lock()
    return l

# ---------- AVISOS DE ERROR AL ADMIN ----------
_alertas={}
async def alertar_admin(titulo, detalle=""):
    if not ADMIN_ID or APP_BOT is None: return
    clave=titulo+detalle[:60]
    if time.time()-_alertas.get(clave,0)<120: return   # no repite el mismo aviso en 2 minutos
    _alertas[clave]=time.time()
    try: await APP_BOT.send_message(ADMIN_ID,f"⚠️ {titulo}\n{detalle}"[:3500])
    except Exception as e: logging.warning(f"No pude avisar al admin: {type(e).__name__}")

_conflictos=[]
_aviso_error={}
async def error_handler(update, context):
    err=context.error
    logging.error("Excepción no controlada",exc_info=err)
    if isinstance(err,NetworkError): return   # fallos de red pasajeros: solo registro
    if isinstance(err,Conflict):
        # normal unos segundos al desplegar; solo avisa si dura más de 4 minutos
        t=time.time()
        _conflictos.append(t)
        _conflictos[:]=[x for x in _conflictos if t-x<900]
        if t-_conflictos[0]>=240:
            await alertar_admin("Hay DOS copias del bot corriendo",
                                "Revisa que no tengas el bot encendido en otro lugar (PC, celular, otro servicio) o cambia el token con /revoke en BotFather.")
        return
    try:   # en privado, avisa al usuario con un mensaje amable (máximo uno cada 30 s)
        if isinstance(update,Update) and update.effective_message and update.effective_chat and update.effective_chat.type=="private":
            cid=update.effective_chat.id
            if time.time()-_aviso_error.get(cid,0)>30:
                _aviso_error[cid]=time.time()
                await update.effective_message.reply_text("Uy, algo falló de mi lado 😅 inténtalo de nuevo en un momento.")
    except Exception: pass
    await alertar_admin("Error no controlado",f"{type(err).__name__}: {str(err)[:300]}")

# ---------- LLM CON FALLBACK Y ENRUTADOR ----------
_muertos=set()
def llm(messages, max_tokens=800, temperature=0.7, modelos=None, esfuerzo="low"):
    ultimo=None
    for intento in range(2):
        limite=False
        for m in (modelos or MODELOS):
            if m in _muertos: continue
            try:
                kw={"extra_body":{"reasoning_effort":esfuerzo}} if "gpt-oss" in m else {}
                c=groq_client.chat.completions.create(model=m,messages=messages,max_tokens=max_tokens,temperature=temperature,**kw)
                txt=(c.choices[0].message.content or "").strip()
                if txt: return txt
            except Exception as e:
                ultimo=e; s=str(e).lower()
                if "429" in s or "rate" in s: limite=True
                if any(k in s for k in ["decommission","model_not_found","does not exist","no longer supported"]): _muertos.add(m)
                logging.warning(f"Modelo {m} falló: {type(e).__name__}")
        if intento==0 and limite:
            time.sleep(3); continue    # límite gratis: espera un poco y reintenta una vez
        break
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
def construir_system(uid, grupo=False):
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
    if p["hechos"] and not grupo:   # en grupos no se usan los datos personales (los vería todo el grupo)
        s+=" Datos del usuario (úsalos solo si ayudan, sin repetirlos innecesariamente): "+"; ".join(p["hechos"][-12:])+"."
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
        if cambiado: persistir("perfiles",uid)
    except Exception as e: logging.warning(f"aprender_perfil: {type(e).__name__}")

# ---------- VISIÓN Y TRANSCRIPCIÓN ----------
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
            ultimo=e; logging.warning(f"Visión {m} falló: {type(e).__name__}")
    raise ultimo or RuntimeError("Sin respuesta de visión")

def vision_multi(b64s, instruccion, mime="image/jpeg", max_tokens=900):
    """Analiza varias imágenes (máx. 4 por pedido) en una sola llamada."""
    ultimo=None
    contenido=[{"type":"text","text":instruccion}]+[
        {"type":"image_url","image_url":{"url":f"data:{mime};base64,{b}"}} for b in b64s[:4]]
    for m in VISION_MODELOS:
        try:
            c=groq_client.chat.completions.create(model=m,messages=[{"role":"user","content":contenido}],
                                                  max_tokens=max_tokens,temperature=0.2)
            t=(c.choices[0].message.content or "").strip()
            if t: return t
        except Exception as e:
            ultimo=e; logging.warning(f"Visión múltiple {m} falló: {type(e).__name__}")
    raise ultimo or RuntimeError("Sin respuesta de visión")

def transcribir(data, nombre="audio.mp3"):
    ultimo=None
    for m in ("whisper-large-v3","whisper-large-v3-turbo"):
        try:
            r=groq_client.audio.transcriptions.create(file=(nombre,data),model=m,response_format="text")
            t=str(r).strip()
            if t: return t
        except Exception as e:
            ultimo=e; logging.warning(f"Transcripción {m} falló: {type(e).__name__}")
    if ultimo: raise ultimo
    return ""

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

def partir_texto(texto,limite=4000):
    """Corta un texto largo por párrafos o líneas (no a mitad de una frase)."""
    partes=[]
    while len(texto)>limite:
        corte=texto.rfind("\n\n",0,limite)
        if corte<limite*0.5: corte=texto.rfind("\n",0,limite)
        if corte<limite*0.5: corte=texto.rfind(" ",0,limite)
        if corte<=0: corte=limite
        partes.append(texto[:corte].rstrip()); texto=texto[corte:].lstrip()
    if texto.strip(): partes.append(texto)
    return partes

async def enviar_largo(update, texto):
    for p in (partir_texto(texto or "") or ["(sin contenido)"]):
        await update.message.reply_text(p)
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
        logging.warning(f"Error audio: {type(e).__name__}"); return False

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
            except Exception as e: errores.append(f"{modelo}:{type(e).__name__}")
    try:
        url=f"https://image.pollinations.ai/prompt/{enc}?width={w}&height={h}&model=turbo&nologo=true&seed={seed}"
        r=requests.get(url,timeout=60)
        if _es_imagen(r): return r.content
        errores.append(f"anonimo:{r.status_code}")
    except Exception as e: errores.append(f"anonimo:{type(e).__name__}")
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

ultimo_img={}   # uid -> (prompt, caption, ancho, alto, momento)
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
        errores.append(f"Pollinations [{e}]"); logging.warning(f"Pollinations falló: {type(e).__name__}")
    if not contenido:
        await msg.reply_text("El servidor principal no me dejó 😕 probando el plan B gratis (puede tardar 1-2 min) ⏳")
        try: contenido=await asyncio.to_thread(_horde,prompt_en,w,h)
        except Exception as e:
            errores.append(f"Horde [{e}]"); logging.warning(f"Horde falló: {type(e).__name__}")
    if not contenido:
        raise RuntimeError("No pude crear la imagen ahora. "+" | ".join(errores)[:300])
    ultimo_img[uid]=(prompt_en,caption,w,h,time.time())
    bio=BytesIO(contenido); bio.name="imagen.jpg"
    await msg.reply_photo(photo=bio, caption=f"Listo bro 🔥 {caption}"[:900], reply_markup=teclado_img())

async def crear_imagen(update, prompt_original):
    async with pesado(update,"img","Estoy creando tu imagen anterior, espera a que termine 🎨") as ok:
        if not ok: return
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
    async with pesado(update,"img","Estoy creando tu imagen anterior, espera a que termine 🎨") as ok:
        if not ok: return
        if not await consumir(update): return
        prompt_en,caption,w,h,_=data
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
ultima_foto={}   # clave de memoria -> (file_id, momento)
KW_PREGUNTA_FOTO=["la foto","esa foto","en la foto","la imagen","esa imagen","en la imagen","qué ves","que ves","lo que ves",
                  "qué tiene","que tiene","qué significa esto","que significa esto","qué significa eso","que significa eso",
                  "significado de esto","significado de la foto","significado de la imagen"]
def es_pregunta_foto(low, ck):
    f=ultima_foto.get(ck)
    if not f or time.time()-f[1]>VENTANA_FOTO: return False
    if any(v in low for v in VERBOS_CREAR): return False
    return any(k in low for k in KW_PREGUNTA_FOTO)

async def procesar_foto(update, context, file_id, caption, mime="image/jpeg"):
    uid=update.effective_user.id; ck=ckey(update)
    ultima_foto[ck]=(file_id,time.time())
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
    f=await context.bot.get_file(file_id)
    buf=BytesIO(); await f.download_to_memory(buf)
    b64=base64.b64encode(buf.getvalue()).decode("utf-8")
    if quiere_recrear(caption):
        async with pesado(update,"img","Estoy creando tu imagen anterior, espera a que termine 🎨") as ok:
            if not ok: return
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
    mem=get_memory(ck)
    mem.append({"role":"user","content":f"[Te envié una foto] {caption}".strip()})
    mem.append({"role":"assistant","content":respuesta[:1200]})
    user_memories[ck]=mem[-14:]; guardar_memorias(ck)
# ---------- LECTURA DE ARCHIVOS (cortos y largos, PDFs escaneados) ----------
EXT_TEXTO=(".txt",".md",".csv",".tsv",".json",".py",".js",".ts",".html",".css",".log",".xml",".yml",".yaml",".ini",".sql",".java",".c",".cpp",".sh")
EXT_CODIGO=(".py",".js",".ts",".java",".c",".cpp",".sh",".sql",".html",".css")
ultimo_doc={}     # clave de memoria -> (nombre, texto, momento)   (también guarda transcripciones de audios y videos)
resumen_doc={}    # clave de memoria -> notas por partes de un archivo largo
KW_DOC=["el archivo","ese archivo","del archivo","en el archivo","el pdf","ese pdf","del pdf","en el pdf",
        "el documento","ese documento","del documento","el excel","la hoja","el código","el codigo",
        "el audio","ese audio","del audio","en el audio","el video","ese video","del video","en el video",
        "el vídeo","ese vídeo","la transcripción","la transcripcion"]

def extraer_texto_archivo(data, nombre):
    n=nombre.lower()
    if n.endswith(".pdf"):
        if not PdfReader: raise RuntimeError("Falta instalar pypdf o PyPDF2")
        reader=PdfReader(BytesIO(data)); partes=[]
        for p in reader.pages[:60]:
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

def ocr_pdf(data):
    """PDF escaneado: convierte las primeras páginas en imágenes y las lee con visión."""
    if fitz is None: raise RuntimeError("Falta instalar pymupdf (agrégalo a requirements.txt)")
    doc=fitz.open(stream=data,filetype="pdf")
    paginas=[]
    for i,page in enumerate(doc):
        if i>=PDF_PAGINAS_OCR: break
        pix=page.get_pixmap(dpi=110)
        paginas.append(base64.b64encode(pix.tobytes("jpeg")).decode())
    doc.close()
    textos=[]
    for j in range(0,len(paginas),2):
        lote=paginas[j:j+2]
        textos.append(vision_multi(lote,
            f"Transcribe EXACTAMENTE todo el texto de cada imagen (son páginas de un documento, en orden; la primera es la página {j+1}). "
            "Separa cada página con '--- Página N ---'. Mantén las tablas en filas. Devuelve solo el texto, sin comentarios.",max_tokens=1800))
    return "\n".join(textos)

def trozos(texto,n=TROZO):
    partes=[]; actual=""
    for linea in texto.split("\n"):
        if len(actual)+len(linea)+1>n and actual:
            partes.append(actual); actual=""
        while len(linea)>n:
            partes.append(linea[:n]); linea=linea[n:]
        actual+=linea+"\n"
    if actual.strip(): partes.append(actual)
    return partes

def elegir_trozos(texto,pregunta,k=2):
    """Elige las partes del archivo que más se parecen a la pregunta."""
    ps=trozos(texto)
    if len(ps)<=k: return ps
    palabras=re.findall(r"\w{4,}",(pregunta or "").lower())
    puntaje=[(sum(p.lower().count(w) for w in palabras),i) for i,p in enumerate(ps)]
    if not palabras or max(x[0] for x in puntaje)==0: return ps[:k]
    mejores=sorted(sorted(puntaje,reverse=True)[:k],key=lambda x:x[1])
    return [ps[i] for _,i in mejores]

async def construir_notas(ck):
    """Archivo largo: resume cada parte (con pausas para cuidar los límites gratis)."""
    nombre,texto,_=ultimo_doc[ck]
    lista=[]
    for i,p in enumerate(trozos(texto)[:MAX_TROZOS_NOTAS],1):
        try:
            r=await allm([
                {"role":"system","content":"Resume en español, en máximo 120 palabras, los datos, cifras, nombres y ideas clave de este fragmento. Sin introducción."},
                {"role":"user","content":p}],max_tokens=350,temperature=0.2,modelos=MODELOS_LISTOS)
            lista.append(f"[Parte {i}] {r}")
        except Exception as e:
            logging.warning(f"Notas parte {i} falló: {type(e).__name__}")
            lista.append(f"[Parte {i}] (no pude resumir esta parte)")
        await asyncio.sleep(1.5)
    resumen_doc[ck]="\n".join(lista)

def pregunta_defecto(nombre):
    n=(nombre or "").lower()
    if n.endswith(EXT_CODIGO): return "Explica qué hace este código, cómo está organizado y señala errores o mejoras importantes."
    if n.endswith((".csv",".tsv",".xlsx",".xlsm")): return "Resume qué datos contiene (columnas, cantidad aproximada, hallazgos clave)."
    return "Haz un resumen claro y breve con los puntos clave."

def es_pregunta_doc(low, ck):
    d=ultimo_doc.get(ck)
    if not d or time.time()-d[2]>VENTANA_DOC: return False
    if any(v in low for v in VERBOS_CREAR): return False
    return any(k in low for k in KW_DOC)

async def responder_sobre_doc(update, ck, pregunta):
    nombre,texto,_=ultimo_doc[ck]
    # "transcribe el audio": si es corto, se manda el texto tal cual (sin gastar IA)
    if re.search(r"transcri|palabra por palabra|textual",(pregunta or "").lower()) and len(texto)<=12000:
        await enviar_largo(update,f"📝 {texto}"); return
    if len(texto)<=TEXTO_MAX:
        contexto=texto
    else:
        contexto=("NOTAS DE TODO EL ARCHIVO, POR PARTES:\n"+resumen_doc.get(ck,"(sin notas)")+
                  "\n\nFRAGMENTOS MÁS RELEVANTES (texto original):\n"+"\n[...]\n".join(elegir_trozos(texto,pregunta,2)))
    resp=await allm([
        {"role":"system","content":"Eres un asistente experto. Responde usando el contenido del archivo. Si la respuesta no está en el archivo, dilo claramente. Si el contenido viene resumido por partes y no puedes dar un detalle exacto, dilo. Responde en el mismo idioma del usuario."},
        {"role":"user","content":f"ARCHIVO '{nombre}':\n{contexto}\n\nPREGUNTA: {pregunta}"}],max_tokens=1200,temperature=0.3,modelos=MODELOS_LISTOS)
    await enviar_largo(update,f"📄 {resp}")
    mem=get_memory(ck)
    mem.append({"role":"user","content":f"[Archivo {nombre}] {pregunta}"[:300]})
    mem.append({"role":"assistant","content":resp[:1200]})
    user_memories[ck]=mem[-14:]; guardar_memorias(ck)

async def guardar_doc_y_responder(update, ck, nombre, texto, pregunta):
    texto=texto.strip()
    if len(texto)>TEXTO_LARGO_MAX:
        await update.message.reply_text(f"(Contenido muy largo: leí los primeros {TEXTO_LARGO_MAX:,} caracteres)")
    ultimo_doc[ck]=(nombre,texto[:TEXTO_LARGO_MAX],time.time())
    resumen_doc.pop(ck,None)
    if len(ultimo_doc[ck][1])>TEXTO_MAX:
        n=len(trozos(ultimo_doc[ck][1]))
        await update.message.reply_text(f"Contenido largo: lo leo por partes ({min(n,MAX_TROZOS_NOTAS)} de {n}) ⏳")
        await construir_notas(ck)
    await responder_sobre_doc(update,ck,pregunta)

async def procesar_archivo(update, context, doc, pregunta):
    ck=ckey(update)
    nombre=doc.file_name or "archivo"
    if doc.file_size and doc.file_size>MAX_TG_DESCARGA:
        await update.message.reply_text("Telegram solo me deja bajar archivos de hasta 20 MB 😕 mándame uno más liviano."); return
    async with pesado(update,"archivo","Estoy leyendo tu archivo anterior, espera a que termine 📖") as ok:
        if not ok: return
        await update.message.reply_text(f"Leyendo {nombre} 📖...")
        f=await doc.get_file(); buf=BytesIO(); await f.download_to_memory(buf)
        datos=buf.getvalue()
        try:
            texto=await asyncio.to_thread(extraer_texto_archivo,datos,nombre)
        except ValueError:
            await update.message.reply_text("Ese tipo de archivo aún no lo leo 😕\nLeo: PDF (también escaneados), Word (.docx), Excel (.xlsx), TXT, CSV, JSON y código (.py .js .html ...).\nTambién imágenes, fotos, audios y videos."); return
        texto=(texto or "").strip()
        if len(texto)<30 and nombre.lower().endswith(".pdf"):
            await update.message.reply_text(f"Parece un PDF escaneado 🔍 lo leo con visión (primeras {PDF_PAGINAS_OCR} páginas)...")
            try: texto=(await asyncio.to_thread(ocr_pdf,datos) or "").strip()
            except Exception as e:
                await update.message.reply_text(f"No pude leer el PDF escaneado 😕 {str(e)[:150]}"); return
        if not texto:
            await update.message.reply_text("No pude sacar texto de ese archivo 😕 Mándamelo como foto o captura y lo leo con visión 👁️"); return
        await guardar_doc_y_responder(update,ck,nombre,texto,pregunta or pregunta_defecto(nombre))

# ---------- AUDIOS Y VIDEOS (transcribe el audio y mira fotogramas) ----------
def _duracion(exe,ruta):
    r=subprocess.run([exe,"-i",ruta],capture_output=True,timeout=60)
    m=re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)",(r.stderr or b"").decode("utf-8","ignore"))
    return int(m.group(1))*3600+int(m.group(2))*60+float(m.group(3)) if m else 0.0

def _audio_liviano(exe,ruta):
    """Audio mono 16 kHz de bajo peso para Whisper (máx. AUDIO_MAX_SEG segundos). Devuelve bytes o None si no hay audio."""
    salida=ruta+".mp3"
    r=subprocess.run([exe,"-y","-i",ruta,"-vn","-ac","1","-ar","16000","-b:a","48k","-t",str(AUDIO_MAX_SEG),salida],
                     capture_output=True,timeout=240)
    if r.returncode==0 and os.path.exists(salida) and os.path.getsize(salida)>2000:
        with open(salida,"rb") as f: return f.read()
    return None

def _fotogramas(exe,ruta,dur,n=FRAMES_VIDEO):
    tiempos=[dur*(i+1)/(n+1) for i in range(n)] if dur>0 else [0.5]
    frames=[]
    for i,t in enumerate(tiempos):
        out=f"{ruta}.f{i}.jpg"
        r=subprocess.run([exe,"-y","-ss",f"{t:.2f}","-i",ruta,"-frames:v","1","-vf","scale=640:-2","-q:v","5",out],
                         capture_output=True,timeout=60)
        if r.returncode==0 and os.path.exists(out) and os.path.getsize(out)>500:
            with open(out,"rb") as f: frames.append(base64.b64encode(f.read()).decode())
    return frames

def analizar_media_local(data,nombre,es_video):
    exe=_ffmpeg_exe()
    if not exe: raise RuntimeError("falta ffmpeg (agrega imageio-ffmpeg a requirements.txt)")
    d=tempfile.mkdtemp()
    try:
        ext=os.path.splitext(nombre)[1].lower() or (".mp4" if es_video else ".mp3")
        ruta=os.path.join(d,"in"+ext)
        with open(ruta,"wb") as f: f.write(data)
        dur=_duracion(exe,ruta)
        audio=_audio_liviano(exe,ruta)
        frames=_fotogramas(exe,ruta,dur) if es_video else []
        return audio,frames,dur
    finally:
        shutil.rmtree(d,ignore_errors=True)

def pregunta_media(es_video):
    if es_video: return "Resume de qué trata este video: qué se ve y, si hay voz, qué dice. Da los puntos clave."
    return "Resume lo que dice este audio, con los puntos clave."

async def procesar_media(update,context,file_obj,nombre,es_video,pregunta,tam=0):
    m=update.message; ck=ckey(update)
    if tam and tam>MAX_TG_DESCARGA:
        await m.reply_text("Telegram solo me deja bajar archivos de hasta 20 MB 😕 mándame uno más liviano o recortado."); return
    async with pesado(update,"media","Estoy analizando tu archivo anterior, espera a que termine ⏳") as ok:
        if not ok: return
        await m.reply_text("Analizando el video 🎬 (audio e imágenes)..." if es_video else "Escuchando el audio 🎧...")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
        f=await file_obj.get_file(); buf=BytesIO(); await f.download_to_memory(buf)
        try:
            audio,frames,dur=await asyncio.to_thread(analizar_media_local,buf.getvalue(),nombre,es_video)
        except Exception as e:
            await m.reply_text(f"No pude procesar el archivo 😕 {str(e)[:200]}"); return
        transcripcion=""; descripcion=""
        if audio:
            try: transcripcion=await asyncio.to_thread(transcribir,audio)
            except Exception as e: logging.warning(f"Transcripción falló: {type(e).__name__}")
        if frames:
            try:
                descripcion=await asyncio.to_thread(vision_multi,frames,
                    "Estas imágenes son fotogramas de un mismo video, en orden. Describe en español, en 2-3 frases por imagen, qué se ve "
                    "(personas, objetos, lugar, texto en pantalla, acciones). No identifiques a personas por su cara.")
            except Exception as e: logging.warning(f"Visión del video falló: {type(e).__name__}")
        if not transcripcion and not descripcion:
            await m.reply_text("No pude sacar nada útil 😕 (no encontré voz ni imágenes que analizar, o los servicios gratis están saturados)."); return
        partes=[]
        if descripcion: partes.append("DESCRIPCIÓN VISUAL (fotogramas del video):\n"+descripcion)
        if transcripcion: partes.append("TRANSCRIPCIÓN DEL AUDIO:\n"+transcripcion)
        elif es_video: partes.append("(El video no tiene voz que transcribir.)")
        if dur>AUDIO_MAX_SEG:
            await m.reply_text(f"(Dura {int(dur//60)} min: transcribí solo los primeros {AUDIO_MAX_SEG//60} minutos)")
        etiqueta=("video " if es_video else "audio ")+nombre
        await guardar_doc_y_responder(update,ck,etiqueta,"\n\n".join(partes),pregunta or pregunta_media(es_video))

async def media_handler(update,context):
    if not update.message: return
    if not me_hablan(update,context): return   # en grupos: solo con @mención en el pie o respuesta al bot
    m=update.message
    caption=limpiar_mencion(m.caption or "",context)
    if m.audio: obj,nombre,video,tam=m.audio,(m.audio.file_name or "audio.mp3"),False,m.audio.file_size
    elif m.video: obj,nombre,video,tam=m.video,(m.video.file_name or "video.mp4"),True,m.video.file_size
    elif m.video_note: obj,nombre,video,tam=m.video_note,"video_redondo.mp4",True,m.video_note.file_size
    else: return
    if not await consumir(update): return
    try: await procesar_media(update,context,obj,nombre,video,caption,tam)
    except Exception as e: await m.reply_text(f"Error con el archivo: {str(e)[:200]}")

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
        except Exception as e: logging.warning(f"Búsqueda {fn.__name__} falló: {type(e).__name__}")
    return ""

CLAVES_BUSQUEDA=["busca","que paso","qué pasó","que hay","noticias","quien es","quién es","keiko","presidenta","peru","perú",
                 "hoy","actual","último","ultimo","precio","cotización","clima","resultado","cuanto cuesta","cuánto cuesta"]
CLAVES_CLEAR=["borra la memoria","limpia la memoria","olvida todo","borra todo","reinicia la memoria"]

# ---------- STICKERS (/s) ----------
def imagen_a_sticker(data):
    if Image is None: raise RuntimeError("Falta instalar Pillow")
    im=Image.open(BytesIO(data)).convert("RGBA")
    esc_=512/max(im.size)
    im=im.resize((max(1,round(im.width*esc_)),max(1,round(im.height*esc_))),Image.LANCZOS)
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
UA_MOVIL="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
UA_PC="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KB_TT=InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Solo audio",callback_data="tt:mp3")]])

# Dominios de TikTok: tiktok.com (con vm., vt., m., www...), tiktokv.com y tt.site (links de TikTok Lite)
TT_URL_RE=re.compile(r"(?<![\w.@-])(?:https?://)?(?:[\w-]+\.)*(?:tiktok\.com|tiktokv\.com|tt\.site)/[^\s]+",re.I)
TT_RE=TT_URL_RE
URL_ANY=re.compile(r"(?:https?://|www\.)\S+",re.I)
TT_PROMO=re.compile(r"^/(tiktoklite|lite|download|about|legal|business|creators|company|explore|login|signup|privacy|terms|community-guidelines)(/|$)",re.I)

def _puntaje_tt(u):
    """Qué tan probable es que el link sea un video descargable (-1 = no sirve: publicidad, perfil, página suelta)."""
    try: p=urllib.parse.urlparse(u)
    except Exception: return -1
    host=(p.hostname or "").lower(); path=p.path or ""
    if path in ("","/"): return -1
    if TT_PROMO.match(path): return -1                  # publicidad de TikTok Lite, descargas, páginas de la empresa...
    if re.match(r"^/@[^/]+/?$",path): return -1         # perfil, no es un video
    if "/video/" in path or "/photo/" in path: return 3
    if host.startswith(("vm.","vt.")) or host.endswith("tt.site") or path.startswith(("/t/","/v/","/embed")): return 2
    return 1

def links_tt(texto):
    """Todos los links de TikTok del texto, del más probable al menos probable (sin publicidad)."""
    cands=[]
    for m in TT_URL_RE.finditer(texto or ""):
        u=m.group(0).rstrip(").,;!?\"'»>]")
        if not u.lower().startswith("http"): u="https://"+u
        s=_puntaje_tt(u)
        if s>0 and u not in [c for c,_ in cands]: cands.append((u,s))
    cands.sort(key=lambda x:-x[1])
    return [u for u,_ in cands]

def sacar_link_tt(texto):
    l=links_tt(texto)
    return l[0] if l else None

def solo_link_tt(texto):
    """True si el mensaje es básicamente un link de TikTok (con o sin el texto que añade la app al compartir)."""
    resto=TT_URL_RE.sub("",URL_ANY.sub("",texto or "")).strip()
    return len(resto)<=20 or (len(resto)<=300 and bool(re.search(r"tiktok|compart|shared|mira |check out|watch|publicaci",resto,re.I)))

def _canonico_tt(u):
    m=re.search(r"https?://[^\s?#\"'<>]*tiktok\.com/[^\s?#\"'<>]*/(?:video|photo)/\d+",u or "")
    if m: return m.group(0)
    m=re.search(r"tiktok\.com/(?:v|embed/v2|embed)/(\d+)",u or "")
    if m: return f"https://www.tiktok.com/@_/video/{m.group(1)}"
    return None

def normalizar_link_tt(link):
    """Convierte links cortos (vm., vt., tiktok.com/t/, tt.site/t/...) al link completo del video."""
    c=_canonico_tt(link)
    if c: return c
    try:
        r=requests.get(link,headers={"User-Agent":UA_MOVIL},allow_redirects=True,timeout=15)
        for u in [r.url]+[h.headers.get("Location","") for h in r.history]:
            c=_canonico_tt(u)
            if c: return c
        m=re.search(r"https://www\.tiktok\.com/@[\w.\-]+/(?:video|photo)/\d+",r.text or "")
        if m: return m.group(0)
        logging.info("El link de TikTok no se pudo expandir")
    except Exception as e:
        logging.warning(f"No pude expandir el link: {type(e).__name__}")
    return link

# Cada video enviado guarda sus datos (link, audio, y el video en memoria unos minutos)
# para que el botón "Solo audio" saque el audio de ESE video aunque TikWM falle.
tt_cache={}   # (chat_id, message_id) -> datos
tt_ult={}     # user_id -> datos del último TikTok pedido
def limpiar_tt():
    t=time.time()
    for dic in (tt_cache,tt_ult):
        for k,v in list(dic.items()):
            if t-v["ts"]>1200: dic.pop(k,None)       # a los 20 minutos se olvida
    vivos={id(v):v for v in list(tt_cache.values())+list(tt_ult.values()) if v.get("video")}
    for v in sorted(vivos.values(),key=lambda x:x["ts"],reverse=True)[3:]:
        v["video"]=None                              # solo los 3 más recientes conservan el video (ahorra memoria)

def guardar_video_tt(d,buf):
    try:
        data=buf.getvalue()
        if len(data)<=40*1024*1024: d["video"]=data
    except Exception: pass

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
    import yt_dlp
    d=tempfile.mkdtemp()
    opts={"outtmpl":os.path.join(d,"%(id)s.%(ext)s"),"format":"best[ext=mp4]/best","quiet":True,
          "noplaylist":True,"max_filesize":49*1024*1024,"retries":2,"http_headers":{"User-Agent":UA_PC}}
    with yt_dlp.YoutubeDL(opts) as y:
        info=y.extract_info(url,download=True)
        ruta=y.prepare_filename(info)
    with open(ruta,"rb") as f: data=f.read()
    shutil.rmtree(d,ignore_errors=True)
    return BytesIO(data),(info.get("title") or "")[:200]

# --- sacar el audio de un video con ffmpeg (viene incluido con imageio-ffmpeg) ---
def _ffmpeg_exe():
    p=shutil.which("ffmpeg")
    if p: return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

def _extraer_audio(video_bytes):
    """Devuelve (BytesIO con el audio, extensión). Primero copia el audio sin recomprimir (m4a); si no se puede, lo pasa a mp3."""
    exe=_ffmpeg_exe()
    if not exe: raise RuntimeError("falta ffmpeg (agrega imageio-ffmpeg a requirements.txt)")
    d=tempfile.mkdtemp()
    entrada=os.path.join(d,"in.mp4")
    try:
        with open(entrada,"wb") as f: f.write(video_bytes)
        for ext,args in (("m4a",["-c:a","copy"]),("mp3",["-c:a","libmp3lame","-b:a","192k"])):
            salida=os.path.join(d,f"out.{ext}")
            try:
                r=subprocess.run([exe,"-y","-i",entrada,"-vn",*args,salida],capture_output=True,timeout=120)
            except Exception: continue
            if r.returncode==0 and os.path.exists(salida) and os.path.getsize(salida)>1000:
                with open(salida,"rb") as f: data=f.read()
                return BytesIO(data),ext
        raise RuntimeError("no pude sacar el audio del video (¿no tiene sonido?)")
    finally:
        shutil.rmtree(d,ignore_errors=True)

async def _enviar_video(m,video,titulo,d):
    sent=await m.reply_video(video=video,caption=f"✅ Sin marca de agua 🔥\n{titulo}"[:1000],reply_markup=KB_TT,
                             supports_streaming=True,write_timeout=120,read_timeout=120)
    tt_cache[(sent.chat_id,sent.message_id)]=d   # el botón "Solo audio" de ESTE video usa estos datos
    return sent

async def descargar_tiktok(update,context,link):
    m=update.message; uid=update.effective_user.id
    async with pesado(update,"tt","Estoy bajando tu TikTok anterior, espera a que termine ⏬") as ok:
        if not ok: return
        aviso=await m.reply_text("Bajando tu TikTok sin marca de agua ⏬...")
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="upload_video")
            limpiar_tt()
            errores=[]; info=None; candidatos=[]; muy_grande=False; titulo=""
            canon=await asyncio.to_thread(normalizar_link_tt,link)
            logging.info(f"TikTok: {urllib.parse.urlparse(link).netloc} -> {urllib.parse.urlparse(canon).netloc}")
            d={"link":canon,"audio":None,"titulo":"","video":None,"ts":time.time()}
            tt_ult[uid]=d
            for u in dict.fromkeys([canon,link]):
                try:
                    info=await asyncio.to_thread(_tikwm,u); break
                except Exception as e: errores.append(f"TikWM: {e}")
            if info:
                titulo=info["titulo"]; d["titulo"]=titulo; d["audio"]=info.get("audio")
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
                    guardar_video_tt(d,buf)
                    await _enviar_video(m,buf,titulo,d); return
                if muy_grande:
                    await m.reply_text(f"El video pesa más de 50 MB y Telegram no me deja subirlo 😕\nDescárgalo aquí (sin marca):\n{candidatos[-1]}"); return
                for u in candidatos:
                    try: await _enviar_video(m,u,titulo,d); return
                    except Exception as e: errores.append(f"Telegram: {str(e)[:70]}")
            try:
                buf,titulo=await asyncio.to_thread(_ytdlp,canon)
                d["titulo"]=titulo; guardar_video_tt(d,buf)
                await _enviar_video(m,buf,titulo,d); return
            except Exception as e: errores.append(f"yt-dlp: {str(e)[:90]}")
            detalle=" | ".join(errores)[:400]
            logging.warning("TikTok falló (ver detalle técnico en el chat del usuario)")
            low=detalle.lower()
            if "unsupported url" in low:
                pista="Ese link no parece un video de TikTok 🤔 (¿es de una tienda de apps, de un perfil o de publicidad?). Copia el link del video con «Compartir → Copiar enlace»."
            elif any(k in low for k in ["private","privado","unavailable","not available","removed","deleted","url parsing"]):
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
    """Saca el audio de un TikTok con 4 intentos, del más seguro al último recurso."""
    errores=[]; titulo=d.get("titulo","")
    async def enviar(buf,ext):
        buf.name=f"tiktok.{ext}"
        await msg.reply_audio(audio=buf,title=(titulo or "Audio TikTok")[:60],performer="TikTok",
                              write_timeout=120,read_timeout=120)
    # 1) audio directo (si TikWM ya me dio el link)
    if d.get("audio"):
        try:
            buf=await asyncio.to_thread(_bajar,d["audio"])
            if buf: await enviar(buf,"mp3"); return
        except Exception as e: errores.append(f"audio directo: {str(e)[:60]}")
    # 2) sacar el audio del video que ya bajé (no depende de internet)
    if d.get("video"):
        try:
            buf,ext=await asyncio.to_thread(_extraer_audio,d["video"])
            await enviar(buf,ext); return
        except Exception as e: errores.append(f"ffmpeg: {str(e)[:80]}")
    # 3) pedirle de nuevo los datos a TikWM
    try:
        info=await asyncio.to_thread(_tikwm,d["link"])
        d["audio"]=info.get("audio") or d.get("audio"); titulo=titulo or info.get("titulo","")
        if info.get("audio"):
            buf=await asyncio.to_thread(_bajar,info["audio"])
            if buf: await enviar(buf,"mp3"); return
        for u in (info.get("video"),info.get("normal")):   # sin link de audio: bajo su video y le saco el audio
            if not u: continue
            vb=await asyncio.to_thread(_bajar,u)
            if vb:
                buf,ext=await asyncio.to_thread(_extraer_audio,vb.getvalue())
                await enviar(buf,ext); return
    except Exception as e: errores.append(f"TikWM: {str(e)[:80]}")
    # 4) último recurso: yt-dlp baja el video y le saco el audio
    try:
        vb,t2=await asyncio.to_thread(_ytdlp,d["link"])
        titulo=titulo or t2
        buf,ext=await asyncio.to_thread(_extraer_audio,vb.getvalue())
        await enviar(buf,ext); return
    except Exception as e: errores.append(f"yt-dlp: {str(e)[:80]}")
    detalle=" | ".join(errores)[:300]
    pista="\nAgrega imageio-ffmpeg a requirements.txt y vuelve a desplegar." if "falta ffmpeg" in detalle else ""
    logging.warning("Audio de TikTok falló")
    await msg.reply_text(f"No pude sacar el audio 😕{pista}\n\nDetalle técnico: {detalle}")

async def tt_callback(update,context):
    q=update.callback_query
    await q.answer("Sacando el audio 🎵")
    limpiar_tt()
    d=tt_cache.get((q.message.chat_id,q.message.message_id)) or tt_ult.get(q.from_user.id)
    if not d:
        await q.message.reply_text("Ya no tengo ese TikTok (pasaron más de 20 minutos), mándame el link otra vez."); return
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
        await update.message.reply_text("Usa: /tt link_de_tiktok\n(o responde a un mensaje que tenga el link)\nFunciona con links normales, cortos (vm., vt.) y de TikTok Lite (tt.site)."); return
    await descargar_tiktok(update,context,link)

async def mp3_cmd(update,context):
    link=_link_de(update,context)
    if not link:
        await update.message.reply_text("Usa: /mp3 link_de_tiktok\n(o responde a un mensaje que tenga el link)"); return
    await update.message.reply_text("Sacando el audio 🎵...")
    canon=await asyncio.to_thread(normalizar_link_tt,link)
    await enviar_audio_tiktok(update.message,{"link":canon,"audio":None,"titulo":"","video":None,"ts":time.time()})
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
def limpiar_url(u):
    return (u or "").rstrip(").,;!?\"'»>]")

def url_segura(u):
    p=urllib.parse.urlparse(u)
    if p.scheme not in ("http","https") or not p.hostname: return False
    try:
        for _,_,_,_,sa in socket.getaddrinfo(p.hostname,None):
            ip=ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: return False
    except Exception: return False
    return True

def _get_seguro(url,max_saltos=5,max_bytes=2500000):
    """Descarga una página validando CADA redirección (no solo el link inicial) y limitando el tamaño."""
    headers={"User-Agent":UA_PC,"Accept-Language":"es,en;q=0.8"}
    actual=url
    for _ in range(max_saltos+1):
        if not url_segura(actual): raise ValueError("Ese link no está permitido 🔒")
        r=requests.get(actual,headers=headers,timeout=20,allow_redirects=False,stream=True)
        if r.status_code in (301,302,303,307,308):
            loc=r.headers.get("Location"); r.close()
            if not loc: raise RuntimeError("redirección inválida")
            actual=urllib.parse.urljoin(actual,loc); continue
        r.raise_for_status()
        ctype=r.headers.get("content-type","").lower()
        if "pdf" in ctype:
            r.close(); raise ValueError("Ese link es un PDF: descárgalo y mándamelo como archivo 📄")
        data=b""
        for chunk in r.iter_content(65536):
            data+=chunk
            if len(data)>max_bytes: break
        r.close()
        mc=re.search(r"charset=([\w-]+)",ctype)
        return data.decode(mc.group(1) if mc else "utf-8",errors="replace")
    raise RuntimeError("demasiadas redirecciones")

def leer_pagina(url):
    if not url_segura(url): raise ValueError("Ese link no está permitido 🔒")
    texto=""; titulo=""
    try:
        raw=_get_seguro(url)
        mt=re.search(r"(?is)<title[^>]*>(.*?)</title>",raw)
        titulo=html.unescape(mt.group(1)).strip() if mt else ""
        raw=re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|form|iframe)[^>]*>.*?</\1>"," ",raw)
        raw=re.sub(r"(?s)<[^>]+>"," ",raw)
        texto=re.sub(r"\s+"," ",html.unescape(raw)).strip()
    except ValueError: raise
    except Exception as e: logging.warning(f"Lectura directa falló: {type(e).__name__}")
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
    url=limpiar_url(mu.group(0))
    if sacar_link_tt(url):
        await m.reply_text("Eso es un link de TikTok 🎬 Para bajar el video usa /tt (o /mp3 para el audio)."); return
    if not await consumir(update): return
    await resumir_url(update,url)

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
        p["modo"]=context.args[0].lower(); persistir("perfiles",uid)
        await update.message.reply_text(f"Modo cambiado a {p['modo']} ✅"); return
    await update.message.reply_text(f"Modo actual: {p['modo']}\nElige cómo quieres que te responda:",reply_markup=teclado_modos(p["modo"]))
async def modo_callback(update,context):
    q=update.callback_query; modo=q.data.split(":")[1]
    if modo not in MODOS: await q.answer(); return
    uid=q.from_user.id
    p=get_perfil(uid); p["modo"]=modo; persistir("perfiles",uid)
    await q.answer(f"Modo {modo} ✅")
    try: await q.edit_message_text(f"Modo actual: {modo}\nElige cómo quieres que te responda:",reply_markup=teclado_modos(modo))
    except Exception: pass
async def perfil_cmd(update,context):
    uid=update.effective_user.id; p=get_perfil(uid)
    if context.args:
        hecho=" ".join(context.args)[:200]
        if hecho.lower() not in [h.lower() for h in p["hechos"]]:
            p["hechos"].append(hecho); p["hechos"]=p["hechos"][-20:]; persistir("perfiles",uid)
        await update.message.reply_text("Guardado ✅"); return
    if en_grupo(update):
        await update.message.reply_text("Por privacidad, mira tu perfil en mi chat privado 🔒"); return
    hechos="\n".join(f"• {h}" for h in p["hechos"]) or "(vacío todavía)"
    await update.message.reply_text(f"👤 Tu perfil\nModo: {p['modo']}\n{hechos}\n\nAgrega con /perfil me gusta el fútbol · borra con /olvida\nTambién aprendo cuando me cuentas cosas tuyas ('me llamo...', 'vivo en...').")
async def olvida_cmd(update,context):
    uid=update.effective_user.id
    p=get_perfil(uid); p["hechos"]=[]; persistir("perfiles",uid)
    await update.message.reply_text("Listo, olvidé tus datos de perfil 🧹 (tu chat se borra con /clear)")

# ---------- /pensar (razonamiento profundo) ----------
async def pensar_cmd(update,context):
    m=update.message; uid=update.effective_user.id
    q=" ".join(context.args).strip() if context.args else ((m.reply_to_message.text or "") if m.reply_to_message else "")
    if not q: await m.reply_text("Usa: /pensar tu pregunta difícil (matemática, lógica, código, decisiones...)"); return
    if not await consumir(update): return
    await m.reply_text("Pensando a fondo 🧠...")
    try:
        resp=await allm([{"role":"system","content":construir_system(uid,en_grupo(update))+" Razona con mucho cuidado, verifica cada cálculo y da al final una respuesta clara."},
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
    persistir("notas",uid)
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
        notas[uid]=[]; persistir("notas",uid)
        await m.reply_text("Borré todas tus notas 🧹"); return
    if not ident.isdigit():
        await m.reply_text("Usa: /borrarnota ID  (o /borrarnota todas). Mira los IDs con /notas"); return
    lst=notas.get(uid,[])
    n=next((x for x in lst if x["id"]==int(ident)),None)
    if not n:
        await m.reply_text("No encontré esa nota 🤔"); return
    lst.remove(n); persistir("notas",uid)
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

# ---------- GRUPOS: RESUMEN DE LA CHARLA ----------
msgs_grupo={}   # chat_id -> últimos mensajes (solo en memoria RAM, nunca se guardan en disco ni en Upstash)
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
    except Exception as e: logging.warning(f"Recordatorio no enviado: {type(e).__name__}")
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
# ======================================================================
# GRUPOS: BIENVENIDA, VERIFICACIÓN Y SEGURIDAD
# ======================================================================
MSG_BIENVENIDA_MAX=800
REGLAS_MAX=1500
DEF_GRUPO={"bienvenida":True,"verificar":False,"verificar_min":3,"antispam":False,"antispam_min":10,
           "raid":True,"raid_n":6,"borrar_seg":0,"borrar_prev":False,"borrar_union":False,"despedida":False}
recien={}          # (chat, usuario) -> momento en que entró (solo memoria; sirve para el anti-spam)
joins={}           # chat -> momentos de ingresos recientes (detector de raid)
raid_estado={}     # chat -> usuarios acumulados durante un raid
ult_saludo={}      # chat -> id del último saludo enviado por el bot
expulsados={}      # (chat, usuario) -> momento (para no despedir a quienes expulsó la verificación)
_aviso_perm={}     # (chat, permiso) -> último aviso de falta de permisos
_aviso_spam={}     # (chat, usuario) -> último aviso de anti-spam
PERMISOS_BASE=ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,
                              can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,
                              can_send_other_messages=True,can_add_web_page_previews=True,can_invite_users=True)

def gv(cid,clave):
    v=grupos.get(cid,{}).get(clave)
    return DEF_GRUPO.get(clave) if v is None else v
def cfg_g(cid): return grupos.setdefault(cid,{})
def esc(t): return html.escape(str(t or ""),quote=False)
def nombre_corto(u): return (u.first_name or u.username or "Alguien")[:40]
def parse_onoff(s):
    s=(s or "").lower().strip()
    if s in ("on","si","sí","activar","activado","1","true"): return True
    if s in ("off","no","desactivar","desactivado","0","false"): return False
    return None
def fmt_seg(s):
    if not s: return "No"
    return f"{s} s" if s<60 else f"{s//60} min"

async def permisos_bot(context,cid):
    try:
        mb=await context.bot.get_chat_member(cid,context.bot.id)
        if mb.status=="creator": return {"admin":True,"restringir":True,"borrar":True}
        if mb.status=="administrator":
            return {"admin":True,"restringir":bool(getattr(mb,"can_restrict_members",False)),
                    "borrar":bool(getattr(mb,"can_delete_messages",False))}
    except Exception: pass
    return {"admin":False,"restringir":False,"borrar":False}

async def nota_permiso(context,cid,clave,texto):
    p=await permisos_bot(context,cid)
    return "" if p[clave] else f"\n\n⚠️ Ojo: {texto}"

async def _borrar_job(context):
    d=context.job.data
    try: await context.bot.delete_message(d["chat"],d["mid"])
    except Exception: pass
def programar_borrado(context,cid,mid,seg):
    jq=context.job_queue
    if jq and seg>0: jq.run_once(_borrar_job,when=seg,data={"chat":cid,"mid":mid})
async def borrar_msg(bot,cid,mid):
    if not mid: return
    try: await bot.delete_message(cid,mid)
    except Exception: pass

async def avisar_permiso(context,cid,que):
    k=(cid,que)
    if time.time()-_aviso_perm.get(k,0)<3600: return
    _aviso_perm[k]=time.time()
    try:
        m=await context.bot.send_message(cid,f"⚠️ Para esta función necesito ser administrador con permiso de {que}. Pídele a un admin que me lo active.")
        programar_borrado(context,cid,m.message_id,30)
    except Exception: pass

# ---------- Textos y botones del saludo ----------
def media_de(msg):
    if not msg: return None
    if msg.animation: return ("animation",msg.animation.file_id)   # GIFs
    if msg.video: return ("video",msg.video.file_id)
    if msg.photo: return ("photo",msg.photo[-1].file_id)
    return None

def texto_bienvenida(chat_id, titulo, humanos, bot_user, extra="", pie=""):
    """Arma el saludo: usa el mensaje del grupo, si no el general, si no el de siempre."""
    plantilla=(grupos.get(chat_id,{}).get("texto") or grupos.get(0,{}).get("texto") or "")
    nombres=", ".join(x.mention_html() for x in humanos)+(f" {extra}" if extra else "")
    if not plantilla:
        base=f"🎉 ¡Bienvenido/a {nombres}!\nPara hablar conmigo mencióname con @{bot_user}. Mira mis comandos con /ayuda 😎"
    else:
        t=esc(plantilla)   # el texto del admin nunca rompe el formato
        base=(t.replace("{nombre}",nombres)
               .replace("{grupo}",esc(titulo or "el grupo"))
               .replace("{bot}",f"@{bot_user}"))
    return base+pie

def pie_verificacion(cid):
    return f"\n\n🔒 Toca el botón ✅ en los próximos {gv(cid,'verificar_min')} min para poder escribir; si no, serás expulsado (podrás volver a entrar)."

def teclado_bienvenida(cid,con_verif):
    cfg=grupos.get(cid,{}); reglas=cfg.get("reglas")
    filas=[]; fila=[]
    if con_verif: fila.append(InlineKeyboardButton("✅ Acepto las reglas" if reglas else "✅ Soy humano",callback_data="bv:ok"))
    elif reglas: fila.append(InlineKeyboardButton("✅ Acepto las reglas",callback_data="bv:ok"))
    if reglas: fila.append(InlineKeyboardButton("📜 Reglas",callback_data="bv:reglas"))
    if fila: filas.append(fila)
    botones=[InlineKeyboardButton(lab,url=url) for lab,url in (cfg.get("botones") or [])]
    for i in range(0,len(botones),2): filas.append(botones[i:i+2])
    return InlineKeyboardMarkup(filas) if filas else None

async def enviar_bienvenida(bot,chat_id,texto,markup=None,thread=None):
    """Envía el saludo (con foto/video/GIF si hay uno configurado). Devuelve el mensaje enviado."""
    base={"chat_id":chat_id}
    if thread: base["message_thread_id"]=thread
    md=grupos.get(chat_id,{}).get("media") or grupos.get(0,{}).get("media")   # primero la del grupo, si no la general
    if md and len(texto)<=1000:
        tipo,fid=md
        kw=dict(base,caption=texto,parse_mode="HTML",reply_markup=markup)
        try:
            if tipo=="photo": return await bot.send_photo(photo=fid,**kw)
            if tipo=="video": return await bot.send_video(video=fid,**kw)
            return await bot.send_animation(animation=fid,**kw)
        except Exception as e: logging.warning(f"Bienvenida con foto/video falló: {type(e).__name__}")
    try: return await bot.send_message(text=texto,parse_mode="HTML",reply_markup=markup,**base)
    except Exception as e: logging.warning(f"Bienvenida con formato falló: {type(e).__name__}")
    return await bot.send_message(text=re.sub(r"<[^>]+>","",html.unescape(texto)),**base)

async def saludar(context,cid,titulo,humanos,con_verif,extra="",thread=None,seguimiento=True):
    """Manda el saludo (y aplica 'borrar anterior' y 'borrar después de X segundos')."""
    bot_user=context.bot.username
    if gv(cid,"bienvenida"):
        texto=texto_bienvenida(cid,titulo,humanos,bot_user,extra,pie_verificacion(cid) if con_verif else "")
    elif con_verif:
        nombres=", ".join(x.mention_html() for x in humanos)+(f" {extra}" if extra else "")
        texto=f"🔒 {nombres}"+pie_verificacion(cid)
    else:
        return None
    if seguimiento and gv(cid,"borrar_prev") and not grupos.get(cid,{}).get("pend") and ult_saludo.get(cid):
        await borrar_msg(context.bot,cid,ult_saludo.pop(cid,None))
    try: sent=await enviar_bienvenida(context.bot,cid,texto,teclado_bienvenida(cid,con_verif),thread)
    except Exception as e:
        logging.warning(f"No pude enviar el saludo: {type(e).__name__}"); return None
    if seguimiento and sent:
        ult_saludo[cid]=sent.message_id
        seg=gv(cid,"borrar_seg")
        if seg>0:
            if gv(cid,"verificar"): seg=max(seg,gv(cid,"verificar_min")*60+10)   # no borra el botón antes de que se acabe el tiempo
            programar_borrado(context,cid,sent.message_id,seg)
    return sent

# ---------- Verificación anti-bots ----------
async def levantar_restriccion(bot,cid,uid):
    perms=PERMISOS_BASE
    try:
        chat=await bot.get_chat(cid)
        if chat.permissions: perms=chat.permissions
    except Exception: pass
    await bot.restrict_chat_member(cid,uid,permissions=perms)

async def _verif_timeout(context):
    d=context.job.data; cid,uid=d["chat"],d["uid"]
    pend=grupos.get(cid,{}).get("pend",{})
    info=pend.pop(str(uid),None)
    if not info: return
    persistir("grupos",cid)
    nombre=info.get("n","Un usuario")
    expulsados[(cid,uid)]=time.time()
    ok=True
    try:
        await context.bot.ban_chat_member(cid,uid)
        await context.bot.unban_chat_member(cid,uid,only_if_banned=True)   # lo expulsa pero puede volver a entrar
    except Exception as e:
        ok=False; logging.warning(f"No pude expulsar a un usuario: {type(e).__name__}")
        try: await levantar_restriccion(context.bot,cid,uid)
        except Exception: pass
        await avisar_permiso(context,cid,"expulsar miembros")
    if cid in raid_estado: return
    try:
        txt=(f"🚫 {esc(nombre)} no se verificó a tiempo y fue expulsado (puede volver a entrar)." if ok
             else f"⚠️ {esc(nombre)} no se verificó, pero no pude expulsarlo.")
        m=await context.bot.send_message(cid,txt,parse_mode="HTML")
        programar_borrado(context,cid,m.message_id,20)
    except Exception: pass

def programar_verif(jq,cid,uid,seg):
    if jq: jq.run_once(_verif_timeout,when=max(5,seg),data={"chat":cid,"uid":uid},name=f"ver{cid}_{uid}")

async def silenciar_nuevos(context,cid,humanos):
    """Silencia a los nuevos hasta que se verifiquen. Devuelve los que quedaron pendientes."""
    minutos=gv(cid,"verificar_min")
    pendientes=[]
    pend=cfg_g(cid).setdefault("pend",{})
    for u in humanos:
        try:
            mb=await context.bot.get_chat_member(cid,u.id)
            if mb.status in ("administrator","creator","restricted"): continue   # admins y silenciados por un admin no se tocan
            await context.bot.restrict_chat_member(cid,u.id,permissions=ChatPermissions(can_send_messages=False))
        except Exception as e:
            logging.warning(f"No pude silenciar a un usuario: {type(e).__name__}")
            await avisar_permiso(context,cid,"restringir miembros")
            continue
        pend[str(u.id)]={"t":time.time()+minutos*60,"n":nombre_corto(u)}
        programar_verif(context.job_queue,cid,u.id,minutos*60)
        pendientes.append(u)
    if pendientes: persistir("grupos",cid)
    return pendientes

# ---------- Raid: un solo saludo para todos ----------
async def _raid_fin(context):
    cid=context.job.data
    est=raid_estado.pop(cid,None)
    if not est or not est["users"]: return
    vistos={}
    for u in est["users"]: vistos[u.id]=u
    lista=list(vistos.values())
    extra=f"y {len(lista)-5} más" if len(lista)>5 else ""
    con_verif=bool(gv(cid,"verificar") and grupos.get(cid,{}).get("pend"))
    await saludar(context,cid,est.get("titulo"),lista[:5],con_verif,extra=extra,thread=est.get("thread"))

# ---------- Entra un miembro ----------
async def bienvenida_handler(update,context):
    m=update.message
    if not m or not m.new_chat_members: return
    cid=update.effective_chat.id; titulo=update.effective_chat.title
    bot_user=context.bot.username
    if any(x.id==context.bot.id for x in m.new_chat_members):
        await m.reply_text(f"👋 ¡Hola grupo! Soy una IA gratis. Para hablar conmigo mencióname con @{bot_user} y tu pregunta. Mira mis comandos con /ayuda 😎\n"
                           "Admins: configuren la bienvenida y la seguridad con /panel (para verificación y anti-spam hazme administrador).")
        return
    if gv(cid,"borrar_union"):   # borra el aviso "X se unió al grupo"
        try: await m.delete()
        except Exception: await avisar_permiso(context,cid,"borrar mensajes")
    humanos=[x for x in m.new_chat_members if not x.is_bot][:20]
    if not humanos: return
    ahora_t=time.time()
    for u in humanos: recien[(cid,u.id)]=ahora_t
    if len(recien)>3000:
        for k,v in list(recien.items()):
            if ahora_t-v>3600: recien.pop(k,None)
    thread=m.message_thread_id if getattr(m,"is_topic_message",False) else None
    pendientes=await silenciar_nuevos(context,cid,humanos) if gv(cid,"verificar") else []   # verificación
    # detector de raid
    dq=joins.setdefault(cid,deque(maxlen=60))
    for _ in humanos: dq.append(ahora_t)
    recientes=sum(1 for t in dq if ahora_t-t<=60)
    if gv(cid,"raid") and context.job_queue and (cid in raid_estado or recientes>=gv(cid,"raid_n")):
        est=raid_estado.get(cid)
        if not est:
            est={"users":[],"thread":thread,"titulo":titulo}
            raid_estado[cid]=est
            await alertar_admin("🚨 Posible raid",f"{titulo or cid}: {recientes} ingresos en menos de 1 minuto. Los saludaré a todos juntos.")
        est["users"].extend(humanos)
        for j in context.job_queue.get_jobs_by_name(f"raid{cid}"): j.schedule_removal()
        context.job_queue.run_once(_raid_fin,when=45,data=cid,name=f"raid{cid}")
        return
    await saludar(context,cid,titulo,humanos[:5],bool(pendientes),thread=thread)

# ---------- Sale un miembro (despedida) ----------
def texto_despedida(cid,titulo,u,bot_user):
    plantilla=grupos.get(cid,{}).get("texto_despedida") or "👋 {nombre} salió del grupo. ¡Hasta pronto!"
    t=esc(plantilla)
    return (t.replace("{nombre}",f"<b>{esc(nombre_corto(u))}</b>")
             .replace("{grupo}",esc(titulo or "el grupo"))
             .replace("{bot}",f"@{bot_user}"))

async def despedida_handler(update,context):
    m=update.message
    if not m or not m.left_chat_member: return
    u=m.left_chat_member; cid=update.effective_chat.id
    if u.id==context.bot.id or u.is_bot: return
    recien.pop((cid,u.id),None)
    pend=grupos.get(cid,{}).get("pend",{})
    if str(u.id) in pend:   # se fue antes de verificarse
        pend.pop(str(u.id),None); persistir("grupos",cid)
        if context.job_queue:
            for j in context.job_queue.get_jobs_by_name(f"ver{cid}_{u.id}"): j.schedule_removal()
    if not gv(cid,"despedida"): return
    if time.time()-expulsados.get((cid,u.id),0)<90: return   # lo expulsó la verificación: no se despide
    try:
        await context.bot.send_message(cid,texto_despedida(cid,update.effective_chat.title,u,context.bot.username),parse_mode="HTML")
    except Exception as e: logging.warning(f"Despedida falló: {type(e).__name__}")

# ---------- Anti-spam para recién llegados (también revisa mensajes editados) ----------
RE_LINK_SPAM=re.compile(r"(https?://|www\.|t\.me/|wa\.me/|bit\.ly/|\b[\w-]+\.(?:com|net|org|io|xyz|top|link|shop|site|club|live|cc)\b)",re.I)
async def antispam_handler(update,context):
    m=update.effective_message; u=update.effective_user
    if not m or not u or u.is_bot or not en_grupo(update): return
    cid=update.effective_chat.id
    if not gv(cid,"antispam"): return
    t0=recien.get((cid,u.id))
    if not t0: return
    if time.time()-t0>gv(cid,"antispam_min")*60:
        recien.pop((cid,u.id),None); return
    texto=m.text or m.caption or ""
    if texto.startswith("/"): return   # los comandos no se tocan
    ents=list(m.entities or [])+list(m.caption_entities or [])
    link=any(e.type in ("url","text_link") for e in ents) or bool(RE_LINK_SPAM.search(texto))
    reenv=bool(getattr(m,"forward_origin",None) or getattr(m,"forward_date",None))
    if not (link or reenv): return
    try:
        mb=await context.bot.get_chat_member(cid,u.id)
        if mb.status in ("administrator","creator"):
            recien.pop((cid,u.id),None); return
    except Exception: pass
    try: await m.delete()
    except Exception:
        await avisar_permiso(context,cid,"borrar mensajes"); return
    if time.time()-_aviso_spam.get((cid,u.id),0)>60:
        _aviso_spam[(cid,u.id)]=time.time()
        try:
            w=await context.bot.send_message(cid,f"🚫 {esc(nombre_corto(u))}, por seguridad no puedes enviar links ni reenvíos en tus primeros {gv(cid,'antispam_min')} minutos.",parse_mode="HTML")
            programar_borrado(context,cid,w.message_id,15)
        except Exception: pass
    raise ApplicationHandlerStop

# ---------- Botones del saludo (✅ aceptar/verificar y 📜 reglas) ----------
async def bv_callback(update,context):
    q=update.callback_query
    if not q.message: await q.answer(); return
    cid=q.message.chat_id; uid=q.from_user.id
    if q.data=="bv:reglas":
        r=grupos.get(cid,{}).get("reglas")
        if not r:
            await q.answer("Este grupo aún no tiene reglas.",show_alert=True); return
        if len(r)<=190:
            await q.answer(r,show_alert=True)
        else:
            await q.answer("Te las muestro abajo 👇")
            try:
                m=await context.bot.send_message(cid,"📜 REGLAS DEL GRUPO:\n\n"+r[:3500])
                programar_borrado(context,cid,m.message_id,180)
            except Exception: pass
        return
    if q.data=="bv:ok":
        pend=grupos.get(cid,{}).get("pend",{})
        if str(uid) in pend:
            try: await levantar_restriccion(context.bot,cid,uid)
            except Exception as e:
                logging.warning(f"No pude quitar el silencio a un usuario: {type(e).__name__}")
                await q.answer("No pude quitarte el silencio, avisa a un admin.",show_alert=True); return
            pend.pop(str(uid),None); persistir("grupos",cid)
            if context.job_queue:
                for j in context.job_queue.get_jobs_by_name(f"ver{cid}_{uid}"): j.schedule_removal()
            await q.answer("✅ ¡Verificado! Ya puedes escribir.",show_alert=True)
        else:
            await q.answer("¡Gracias! 🙌")
        return
    await q.answer()

# ---------- Estado de la configuración ----------
async def texto_estado(cid,titulo,context):
    cfg=grupos.get(cid,{}); gen=grupos.get(0,{})
    def oo(k): return "✅" if gv(cid,k) else "❌"
    if cfg.get("texto"): msg="personalizado del grupo"
    elif gen.get("texto"): msg="general del bot"
    else: msg="el de siempre"
    md=cfg.get("media") or gen.get("media")
    if md:
        tipo={"photo":"foto","video":"video","animation":"GIF"}.get(md[0],"archivo")
        media=f"{tipo} ({'del grupo' if cfg.get('media') else 'general'})"
    else: media="ninguna"
    reglas=cfg.get("reglas")
    p=await permisos_bot(context,cid)
    def pe(k): return "✅" if p[k] else "❌"
    return (f"📊 Estado de la bienvenida — {titulo or 'este grupo'}\n\n"
            f"🎉 Saludo: {'✅ activado' if gv(cid,'bienvenida') else '❌ desactivado'}\n"
            f"📝 Mensaje: {msg}\n"
            f"🖼️ Foto/video: {media}\n"
            f"📜 Reglas: {('✅ ('+str(len(reglas))+' caracteres)') if reglas else '❌ sin reglas'}\n"
            f"🔘 Botones de enlace: {len(cfg.get('botones') or [])}\n"
            f"🔒 Verificación: {oo('verificar')} ({gv(cid,'verificar_min')} min) · pendientes ahora: {len(cfg.get('pend') or {})}\n"
            f"🛡️ Anti-spam a recién llegados: {oo('antispam')} ({gv(cid,'antispam_min')} min)\n"
            f"🚨 Aviso de raid: {oo('raid')} (≥{gv(cid,'raid_n')} ingresos en 1 min)\n"
            f"🧹 Borrar saludo: {fmt_seg(gv(cid,'borrar_seg'))}\n"
            f"♻️ Borrar saludo anterior: {oo('borrar_prev')}\n"
            f"🗑️ Borrar aviso 'se unió': {oo('borrar_union')}\n"
            f"👋 Despedida: {oo('despedida')}\n\n"
            f"🤖 Permisos del bot: administrador {pe('admin')} · restringir {pe('restringir')} · borrar mensajes {pe('borrar')}")

# ---------- /bienvenida ----------
async def _solo_admin_grupo(update,context):
    m=update.message
    if not en_grupo(update):
        await m.reply_text("Este comando es para grupos 👥"); return False
    if not await es_admin_grupo(update,context):
        await m.reply_text("Solo los administradores del grupo pueden cambiar esto 🔒"); return False
    return True

async def bienvenida_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id; titulo=update.effective_chat.title
    cfg=cfg_g(cid)
    args=[a.lower() for a in (context.args or [])]
    arg=args[0] if args else ""; val=args[1] if len(args)>1 else ""
    if arg in ("probar","test","prueba"):
        texto=texto_bienvenida(cid,titulo,[update.effective_user],context.bot.username)
        await enviar_bienvenida(context.bot,cid,texto,teclado_bienvenida(cid,False)); return
    if arg=="estado":
        await m.reply_text(await texto_estado(cid,titulo,context)); return
    o=parse_onoff(arg)
    if o is not None:
        cfg["bienvenida"]=o; persistir("grupos",cid)
        await m.reply_text("Bienvenida activada ✅" if o else "Bienvenida desactivada ❌"); return
    if arg=="borrar":
        if not val:
            await m.reply_text(f"Borrado del saludo: {fmt_seg(gv(cid,'borrar_seg'))}\nUsa /bienvenida borrar 60 (segundos) · /bienvenida borrar 5m · /bienvenida borrar off"); return
        if parse_onoff(val) is False: seg=0
        elif val.isdigit(): seg=int(val)
        else: seg=parsear_duracion(val)
        if seg is None or seg>3600:
            await m.reply_text("Pon un tiempo válido (máximo 1 hora): 60, 30s, 5m u off."); return
        cfg["borrar_seg"]=seg; persistir("grupos",cid)
        nota=await nota_permiso(context,cid,"borrar","necesito ser administrador con permiso de borrar mensajes para esto.") if seg else ""
        await m.reply_text(("Listo: borraré el saludo a los "+fmt_seg(seg)+" ✅" if seg else "Listo: ya no borraré el saludo ❌")+nota); return
    mapa={"anterior":"borrar_prev","union":"borrar_union","raid":"raid"}
    if arg in mapa:
        o=parse_onoff(val)
        if o is None:
            await m.reply_text(f"Usa /bienvenida {arg} on  o  /bienvenida {arg} off"); return
        cfg[mapa[arg]]=o; persistir("grupos",cid)
        nota=await nota_permiso(context,cid,"borrar","necesito ser administrador con permiso de borrar mensajes para esto.") if (o and arg!="raid") else ""
        await m.reply_text(f"Listo: {arg} {'activado ✅' if o else 'desactivado ❌'}"+nota); return
    await m.reply_text(f"Bienvenida: {'activada ✅' if gv(cid,'bienvenida') else 'desactivada ❌'}\n\n"
                       "Opciones:\n"
                       "• /bienvenida on | off | probar | estado\n"
                       "• /bienvenida borrar 60 → borra el saludo a los 60 s (off lo desactiva)\n"
                       "• /bienvenida anterior on|off → borra el saludo anterior\n"
                       "• /bienvenida union on|off → borra el aviso 'X se unió'\n"
                       "• /bienvenida raid on|off → aviso de ingresos masivos\n\n"
                       "Personaliza: /mensajebienvenida · /fotobienvenida · /reglas · /botonbienvenida\n"
                       "Seguridad: /verificacion · /antispam · /despedida\n"
                       "Todo con botones: /panel")

async def fotobienvenida_cmd(update,context):
    m=update.message
    arg=(context.args[0].lower() if context.args else "")
    if en_grupo(update):
        if not await es_admin_grupo(update,context):
            await m.reply_text("Solo los administradores del grupo pueden cambiar esto 🔒"); return
        clave=update.effective_chat.id
    else:
        if not es_admin(update):
            await m.reply_text("Solo el admin del bot 🔒 (en un grupo, los administradores pueden usarlo allí)"); return
        clave=0   # bienvenida general para todos los grupos
    cfg=grupos.setdefault(clave,{})
    if arg in ("borrar","quitar","off"):
        cfg.pop("media",None); persistir("grupos",clave)
        await m.reply_text("Listo, quité la foto/video de bienvenida ✅"); return
    md=media_de(m.reply_to_message)
    if not md:
        await m.reply_text("Usa: responde a una foto, video o GIF con /fotobienvenida\nPara quitarla: /fotobienvenida borrar"); return
    cfg["media"]=list(md); persistir("grupos",clave)
    await m.reply_text("✅ Listo, ahora saludaré con eso a los nuevos miembros"+(" de este grupo." if clave else " de todos los grupos (salvo los que tengan una propia)."))

AYUDA_MSGBIENVENIDA=("Usa: /mensajebienvenida ¡Hola {nombre}! Bienvenido a {grupo} 🎉 Lee las reglas fijadas.\n\n"
                     "Variables que puedes usar:\n"
                     "• {nombre} → menciona al nuevo miembro\n"
                     "• {grupo} → nombre del grupo\n"
                     "• {bot} → el @ del bot\n\n"
                     "También puedes responder a un texto con /mensajebienvenida para usarlo.\n"
                     "Ver cómo queda: /mensajebienvenida probar\n"
                     "Volver al mensaje normal: /mensajebienvenida borrar")
async def mensajebienvenida_cmd(update,context):
    m=update.message
    if en_grupo(update):
        if not await es_admin_grupo(update,context):
            await m.reply_text("Solo los administradores del grupo pueden cambiar esto 🔒"); return
        clave=update.effective_chat.id; titulo=update.effective_chat.title
    else:
        if not es_admin(update):
            await m.reply_text("Solo el admin del bot 🔒 (en un grupo, los administradores pueden usarlo allí)"); return
        clave=0; titulo="tu grupo"
    cfg=grupos.setdefault(clave,{})
    partes=(m.text or "").split(None,1)               # así se respetan los saltos de línea
    arg=partes[1].strip() if len(partes)>1 else ""
    if arg.lower() in ("borrar","quitar","off","reset"):
        cfg.pop("texto",None); persistir("grupos",clave)
        await m.reply_text("Listo, volví al mensaje de bienvenida normal ✅"); return
    if arg.lower() in ("probar","test","prueba"):
        texto=texto_bienvenida(clave,titulo,[update.effective_user],context.bot.username)
        await enviar_bienvenida(context.bot,update.effective_chat.id,texto,teclado_bienvenida(clave,False)); return
    texto=arg or ((m.reply_to_message.text or m.reply_to_message.caption or "").strip() if m.reply_to_message else "")
    if not texto:
        actual=cfg.get("texto")
        await m.reply_text(AYUDA_MSGBIENVENIDA+(f"\n\nMensaje actual:\n{actual}" if actual else "")); return
    if len(texto)>MSG_BIENVENIDA_MAX:
        await m.reply_text(f"Muy largo 😅 el máximo es {MSG_BIENVENIDA_MAX} caracteres (el tuyo tiene {len(texto)})."); return
    cfg["texto"]=texto; persistir("grupos",clave)
    await m.reply_text("✅ Mensaje de bienvenida guardado"+(" para este grupo." if clave else " para todos los grupos (salvo los que tengan uno propio).")+"\nPruébalo con /mensajebienvenida probar")

# ---------- /reglas ----------
async def reglas_cmd(update,context):
    m=update.message
    if not en_grupo(update):
        await m.reply_text("Las reglas son por grupo 👥 Úsalo dentro de un grupo."); return
    cid=update.effective_chat.id
    partes=(m.text or "").split(None,1)
    arg=partes[1].strip() if len(partes)>1 else ""
    if not arg:
        r=grupos.get(cid,{}).get("reglas")
        await m.reply_text(("📜 Reglas del grupo:\n\n"+r) if r else "Este grupo aún no tiene reglas 📜 (los admins pueden crearlas con /reglas y el texto)."); return
    if not await es_admin_grupo(update,context):
        await m.reply_text("Solo los administradores del grupo pueden cambiar las reglas 🔒"); return
    cfg=cfg_g(cid)
    if arg.lower() in ("borrar","quitar","off"):
        cfg.pop("reglas",None); persistir("grupos",cid)
        await m.reply_text("Listo, borré las reglas ✅"); return
    if arg.lower()=="guardar":
        arg=((m.reply_to_message.text or m.reply_to_message.caption or "").strip() if m.reply_to_message else "")
        if not arg:
            await m.reply_text("Responde al mensaje que tiene las reglas con /reglas guardar"); return
    if len(arg)>REGLAS_MAX:
        await m.reply_text(f"Muy largas 😅 el máximo es {REGLAS_MAX} caracteres (las tuyas tienen {len(arg)})."); return
    cfg["reglas"]=arg; persistir("grupos",cid)
    await m.reply_text("✅ Reglas guardadas. Ahora el saludo trae los botones 📜 Reglas y ✅ Acepto las reglas.\nPruébalo con /bienvenida probar")

# ---------- /botonbienvenida ----------
AYUDA_BOTONES=("Usa: /botonbienvenida Mi canal | https://t.me/micanal\n"
               "• Hasta 4 botones de enlace en el saludo.\n"
               "• Ver los botones: /botonbienvenida\n"
               "• Quitar uno: /botonbienvenida borrar 2\n"
               "• Quitar todos: /botonbienvenida borrar")
async def botonbienvenida_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id; cfg=cfg_g(cid)
    lista=cfg.setdefault("botones",[])
    partes=(m.text or "").split(None,1)
    arg=partes[1].strip() if len(partes)>1 else ""
    if not arg:
        act="\n".join(f"{i+1}. {lab} → {url}" for i,(lab,url) in enumerate(lista)) or "(no hay botones todavía)"
        await m.reply_text("🔘 Botones del saludo:\n"+act+"\n\n"+AYUDA_BOTONES); return
    low=arg.lower()
    if low.startswith("borrar"):
        resto=low[6:].strip()
        if resto.isdigit():
            i=int(resto)-1
            if not (0<=i<len(lista)):
                await m.reply_text("Ese número de botón no existe 🤔"); return
            lista.pop(i)
        else:
            lista.clear()
        persistir("grupos",cid)
        await m.reply_text("Listo, botón(es) borrado(s) ✅"); return
    if "|" not in arg:
        await m.reply_text(AYUDA_BOTONES); return
    etiqueta,url=[x.strip() for x in arg.split("|",1)]
    if url.lower().startswith(("t.me/","wa.me/")): url="https://"+url
    if not re.match(r"^https?://\S+$",url,re.I):
        await m.reply_text("El enlace debe empezar con https:// (o t.me/...) 🔗"); return
    if not etiqueta or len(etiqueta)>30:
        await m.reply_text("El texto del botón debe tener entre 1 y 30 caracteres."); return
    if len(lista)>=4:
        await m.reply_text("Ya hay 4 botones. Quita uno con /botonbienvenida borrar NÚMERO"); return
    lista.append([etiqueta,url]); persistir("grupos",cid)
    await m.reply_text(f"✅ Botón agregado ({len(lista)}/4). Pruébalo con /bienvenida probar")

# ---------- /verificacion y /antispam ----------
async def verificacion_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id; cfg=cfg_g(cid)
    on=None; minutos=None
    for a in (context.args or []):
        o=parse_onoff(a)
        if o is not None: on=o
        elif a.isdigit(): minutos=max(1,min(60,int(a)))
    if on is None and minutos is None:
        await m.reply_text(f"🔒 Verificación: {'activada ✅' if gv(cid,'verificar') else 'desactivada ❌'} ({gv(cid,'verificar_min')} min)\n\n"
                           "Los nuevos quedan silenciados hasta tocar el botón ✅; si no lo hacen a tiempo, se les expulsa (pueden volver a entrar).\n"
                           "Usa: /verificacion on 3  ·  /verificacion off  ·  /verificacion 5 (cambia los minutos)\n"
                           "Necesito ser administrador con permisos de restringir y expulsar miembros."); return
    if minutos: cfg["verificar_min"]=minutos
    if on is not None:
        if on:
            p=await permisos_bot(context,cid)
            if not p["restringir"]:
                await m.reply_text("Para activarla necesito ser administrador con permiso de restringir miembros (y expulsar usuarios). Dame esos permisos y vuelve a intentarlo."); return
        cfg["verificar"]=on
    persistir("grupos",cid)
    await m.reply_text(f"🔒 Verificación {'activada ✅' if gv(cid,'verificar') else 'desactivada ❌'} · {gv(cid,'verificar_min')} min")

async def antispam_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id; cfg=cfg_g(cid)
    on=None; minutos=None
    for a in (context.args or []):
        o=parse_onoff(a)
        if o is not None: on=o
        elif a.isdigit(): minutos=max(1,min(120,int(a)))
    if on is None and minutos is None:
        await m.reply_text(f"🛡️ Anti-spam a recién llegados: {'activado ✅' if gv(cid,'antispam') else 'desactivado ❌'} ({gv(cid,'antispam_min')} min)\n\n"
                           "Borra los links y reenvíos de quien acaba de entrar, durante sus primeros minutos (los comandos no se tocan).\n"
                           "Usa: /antispam on 10  ·  /antispam off  ·  /antispam 15 (cambia los minutos)\n"
                           "Necesito permiso de borrar mensajes."); return
    if minutos: cfg["antispam_min"]=minutos
    if on is not None:
        if on:
            p=await permisos_bot(context,cid)
            if not p["borrar"]:
                await m.reply_text("Para activarlo necesito ser administrador con permiso de borrar mensajes. Dame ese permiso y vuelve a intentarlo."); return
        cfg["antispam"]=on
    persistir("grupos",cid)
    await m.reply_text(f"🛡️ Anti-spam {'activado ✅' if gv(cid,'antispam') else 'desactivado ❌'} · {gv(cid,'antispam_min')} min")

# ---------- /despedida ----------
AYUDA_DESPEDIDA=("Usa:\n/despedida on · /despedida off\n/despedida texto ¡Chau {nombre}! Te extrañaremos en {grupo} 👋\n/despedida probar · /despedida borrar (vuelve al mensaje normal)\n\n"
                 "Variables: {nombre} · {grupo} · {bot}")
async def despedida_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id; cfg=cfg_g(cid)
    partes=(m.text or "").split(None,1)
    arg=partes[1].strip() if len(partes)>1 else ""
    low=arg.lower()
    if not arg:
        actual=cfg.get("texto_despedida")
        await m.reply_text(f"👋 Despedida: {'activada ✅' if gv(cid,'despedida') else 'desactivada ❌'}\n"
                           f"Mensaje: {actual or '(el de siempre)'}\n\n"+AYUDA_DESPEDIDA); return
    o=parse_onoff(low)
    if o is not None:
        cfg["despedida"]=o; persistir("grupos",cid)
        await m.reply_text("Despedida activada ✅" if o else "Despedida desactivada ❌"); return
    if low in ("probar","test","prueba"):
        await m.reply_text(texto_despedida(cid,update.effective_chat.title,update.effective_user,context.bot.username),parse_mode="HTML"); return
    if low in ("borrar","reset","quitar"):
        cfg.pop("texto_despedida",None); persistir("grupos",cid)
        await m.reply_text("Listo, volví al mensaje de despedida normal ✅"); return
    if low.startswith("texto "): arg=arg[6:].strip()
    if len(arg)>MSG_BIENVENIDA_MAX:
        await m.reply_text(f"Muy largo 😅 el máximo es {MSG_BIENVENIDA_MAX} caracteres."); return
    cfg["texto_despedida"]=arg; cfg["despedida"]=True; persistir("grupos",cid)
    await m.reply_text("✅ Mensaje de despedida guardado y activado. Pruébalo con /despedida probar")

# ---------- /panel (menú con botones) ----------
PANEL_CICLO_VMIN=[2,3,5,10,15]
PANEL_CICLO_BSEG=[0,30,60,300,600]
PANEL_MAPA={"bien":"bienvenida","verif":"verificar","spam":"antispam","raid":"raid","desp":"despedida","bprev":"borrar_prev","bunion":"borrar_union"}
PANEL_NECESITA={"verif":("restringir","restringir miembros y expulsar usuarios"),
                "spam":("borrar","borrar mensajes"),"bunion":("borrar","borrar mensajes"),"bprev":("borrar","borrar mensajes")}

def teclado_panel(cid):
    def oo(k): return "✅" if gv(cid,k) else "❌"
    filas=[
        [InlineKeyboardButton(f"🎉 Saludo {oo('bienvenida')}",callback_data="pn:bien"),
         InlineKeyboardButton(f"🔒 Verificación {oo('verificar')}",callback_data="pn:verif")],
        [InlineKeyboardButton(f"⏱️ Tiempo: {gv(cid,'verificar_min')} min",callback_data="pn:vmin"),
         InlineKeyboardButton(f"🛡️ Anti-spam {oo('antispam')}",callback_data="pn:spam")],
        [InlineKeyboardButton(f"🚨 Aviso raid {oo('raid')}",callback_data="pn:raid"),
         InlineKeyboardButton(f"👋 Despedida {oo('despedida')}",callback_data="pn:desp")],
        [InlineKeyboardButton(f"🧹 Borrar saludo: {fmt_seg(gv(cid,'borrar_seg'))}",callback_data="pn:bseg"),
         InlineKeyboardButton(f"♻️ Borrar anterior {oo('borrar_prev')}",callback_data="pn:bprev")],
        [InlineKeyboardButton(f"🗑️ Borrar 'se unió' {oo('borrar_union')}",callback_data="pn:bunion"),
         InlineKeyboardButton("📊 Estado",callback_data="pn:estado")],
        [InlineKeyboardButton("👀 Probar saludo",callback_data="pn:probar"),
         InlineKeyboardButton("✖️ Cerrar",callback_data="pn:cerrar")]]
    return InlineKeyboardMarkup(filas)

async def panel_cmd(update,context):
    m=update.message
    if not await _solo_admin_grupo(update,context): return
    cid=update.effective_chat.id
    await m.reply_text(f"⚙️ Panel de bienvenida y seguridad — {update.effective_chat.title}\nToca un botón para cambiar cada opción 👇\n"
                       "Textos y archivos: /mensajebienvenida · /fotobienvenida · /reglas · /botonbienvenida · /despedida",
                       reply_markup=teclado_panel(cid))

async def panel_callback(update,context):
    q=update.callback_query
    if not q.message or q.message.chat.type not in ("group","supergroup"):
        await q.answer(); return
    if not await es_admin_grupo(update,context):
        await q.answer("Solo los administradores del grupo pueden usar el panel 🔒",show_alert=True); return
    cid=q.message.chat_id; titulo=q.message.chat.title
    accion=q.data.split(":",1)[1]; cfg=cfg_g(cid)
    if accion=="cerrar":
        await q.answer()
        try: await q.message.delete()
        except Exception: pass
        return
    if accion=="estado":
        await q.answer()
        await context.bot.send_message(cid,await texto_estado(cid,titulo,context)); return
    if accion=="probar":
        await q.answer()
        texto=texto_bienvenida(cid,titulo,[q.from_user],context.bot.username)
        await enviar_bienvenida(context.bot,cid,texto,teclado_bienvenida(cid,False)); return
    aviso="Listo ✅"
    if accion in PANEL_MAPA:
        clave=PANEL_MAPA[accion]; nuevo=not gv(cid,clave)
        if nuevo and accion in PANEL_NECESITA:
            perm,desc=PANEL_NECESITA[accion]
            p=await permisos_bot(context,cid)
            if not p[perm]:
                await q.answer(f"Primero hazme administrador con permiso de {desc}.",show_alert=True); return
        cfg[clave]=nuevo
    elif accion=="vmin":
        act=gv(cid,"verificar_min")
        cfg["verificar_min"]=PANEL_CICLO_VMIN[(PANEL_CICLO_VMIN.index(act)+1)%len(PANEL_CICLO_VMIN)] if act in PANEL_CICLO_VMIN else 3
    elif accion=="bseg":
        act=gv(cid,"borrar_seg")
        nuevo=PANEL_CICLO_BSEG[(PANEL_CICLO_BSEG.index(act)+1)%len(PANEL_CICLO_BSEG)] if act in PANEL_CICLO_BSEG else 0
        cfg["borrar_seg"]=nuevo
        if nuevo:
            p=await permisos_bot(context,cid)
            if not p["borrar"]: aviso="Listo, pero necesito permiso de borrar mensajes para que funcione ⚠️"
    else:
        await q.answer(); return
    persistir("grupos",cid)
    await q.answer(aviso)
    try: await q.edit_message_reply_markup(reply_markup=teclado_panel(cid))
    except Exception: pass
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
    n_pend=sum(len(c.get("pend") or {}) for c in grupos.values())
    if USA_KV:
        faltan=[n for n in NOMBRES_ESTADO if n not in _kv_ok]
        mem=("Upstash ✅" if not faltan else f"Upstash ⚠️ no pude leer: {', '.join(faltan)} (reintento solo; mientras tanto no escribo encima)")
    else:
        mem="archivo local ⚠️ (se borra al reiniciar)"
    await update.message.reply_text(
        f"📊 Estadísticas\nUsuarios: {len(usuarios)}\nMensajes: {msgs}\nEn línea hace: {up//3600}h {(up%3600)//60}m\n"
        f"Usos de IA hoy: {usos_hoy} (límite por usuario: {LIMITE_DIARIO if LIMITE_DIARIO>0 else 'sin límite'})\n"
        f"Búsquedas web hoy: {_compound['n']}\nRecordatorios activos: {len(recordatorios)}\nNotas guardadas: {n_notas}\n"
        f"Grupos con configuración: {len([k for k in grupos if k!=0])} · activos desde el reinicio: {len(msgs_grupo)}\n"
        f"Verificaciones pendientes: {n_pend}\n"
        f"Tareas pesadas en curso: {len(_ocupados)} (máx. {MAX_PESADOS} a la vez)\n"
        f"ffmpeg (audios, videos y TikTok): {'✅' if _ffmpeg_exe() else '❌ falta imageio-ffmpeg'}\n"
        f"PDFs escaneados (pymupdf): {'✅' if fitz else '❌ falta pymupdf'}\n"
        f"Modelos descartados: {', '.join(_muertos) or 'ninguno'}\n"
        f"Memoria permanente: {mem} · cambios por guardar: {len(_sucio)}")
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
    """Backup para el admin: NO incluye las conversaciones con la IA (privacidad)."""
    data={"fecha":ahora()}
    for n in NOMBRES_ESTADO:
        if n!="memorias": data[n]=_serializar_todo(n)
    buf=BytesIO(json.dumps(data,ensure_ascii=False,indent=1).encode("utf-8"))
    buf.name=f"backup_{datetime.now(LIMA).strftime('%Y%m%d_%H%M')}.json"
    return buf
async def backup_cmd(update,context):
    if not es_admin(update):
        await update.message.reply_text("Solo el admin 🔒"); return
    if update.effective_chat.type!="private":
        await update.message.reply_text("Pídeme el backup en mi chat privado 🔒 (contiene datos de usuarios)"); return
    try:
        await update.message.reply_document(document=_armar_backup(),caption=f"💾 Backup · {len(usuarios)} usuarios (sin conversaciones)")
    except Exception as e: await update.message.reply_text(f"No pude armar el backup 😕 {str(e)[:150]}")
async def backup_auto(context):
    if not ADMIN_ID: return
    try: await context.bot.send_document(ADMIN_ID,document=_armar_backup(),caption="💾 Backup automático diario (sin conversaciones)")
    except Exception as e: logging.warning(f"Backup automático falló: {type(e).__name__}")

_marca_usr={}
async def registrar_usuario(update,context):
    try:
        u=update.effective_user
        if not u or u.is_bot: return
        m=update.message
        # guarda en memoria RAM los últimos mensajes del grupo (para /resumen)
        if en_grupo(update) and m and m.text and not m.text.startswith("/"):
            guardar_msg_grupo(update)
        # en grupos solo cuenta a quien le habla al bot (mención, reply o comando)
        if en_grupo(update) and not me_hablan(update,context):
            if not (m and (m.text or "").startswith("/")): return
        nuevo=u.id not in usuarios
        d=usuarios.setdefault(u.id,{"nombre":u.first_name or "","msgs":0,"primera":int(time.time())})
        d["nombre"]=u.first_name or d.get("nombre","")
        if m: d["msgs"]=d.get("msgs",0)+1
        if nuevo or time.time()-_marca_usr.get(u.id,0)>300:   # no se guarda en cada mensaje (ahorra comandos de Upstash)
            _marca_usr[u.id]=time.time(); marcar("usuarios",u.id)
    except Exception: pass

# ---------- PRIVACIDAD: /privacidad Y /borrardatos ----------
async def privacidad_cmd(update,context):
    await update.message.reply_text(
        "🔒 PRIVACIDAD\n\n"
        "Lo que guardo de ti:\n"
        "• Tus últimos 20 mensajes con la IA (separados: el chat privado y cada grupo van aparte)\n"
        "• Tu perfil (datos que me cuentas), tu modo, tus notas y tus recordatorios\n"
        "• Tu nombre y cuántos mensajes me escribes\n\n"
        "Lo que NO se guarda:\n"
        "• Fotos, audios, videos y archivos: se procesan y solo quedan unos minutos en memoria\n"
        "• La charla de los grupos para /resumen: solo está en la memoria de mi servidor (los últimos 120 mensajes) y se pierde al reiniciar\n"
        "• Los registros del servidor no incluyen lo que escribes\n\n"
        "Ojo: para responderte envío tus mensajes a Groq (el servicio de IA gratis que uso).\n"
        "El backup que recibe el admin NO incluye tus conversaciones.\n\n"
        "Para borrar todo lo tuyo: /borrardatos (en mi chat privado)")

async def borrardatos_cmd(update,context):
    if en_grupo(update):
        await update.message.reply_text("Por privacidad, usa /borrardatos en mi chat privado 🔒"); return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Sí, borrar mis datos",callback_data="bd:si"),
                              InlineKeyboardButton("Cancelar",callback_data="bd:no")]])
    await update.message.reply_text("Esto borra TODO lo que guardé de ti: tus chats con la IA (también los de grupos), tu perfil, tus notas, tu modo y tus recordatorios.\n¿Seguro?",reply_markup=kb)

async def borrar_datos_usuario(uid,context):
    claves=[k for k in list(user_memories) if k==uid or (isinstance(k,str) and k.endswith(f":{uid}"))]
    for k in claves:
        for d in (user_memories,resumen_doc,ultimo_doc,ultima_foto): d.pop(k,None)
    for d in (perfiles,notas,usuarios,ultimo_img,tt_ult): d.pop(uid,None)
    quitados=[r for r in recordatorios if r["uid"]==uid]
    for r in quitados:
        recordatorios.remove(r)
        if context.job_queue:
            for j in context.job_queue.get_jobs_by_name(f"rec{r['id']}"): j.schedule_removal()
    for n,k in list(_sucio):
        if (n=="memorias" and k in claves) or (n in ("perfiles","notas","usuarios") and k==uid): _sucio.discard((n,k))
    if USA_KV:
        items=[("memorias",k) for k in claves]+[("perfiles",uid),("notas",uid),("usuarios",uid)]
        await asyncio.to_thread(_borrar_kv,items)
    else:
        for n in ("memorias","perfiles","notas","usuarios"): persistir(n)
    if quitados: persistir("recordatorios")
    return f"{len(claves)} conversaciones, perfil, notas y {len(quitados)} recordatorios"

async def borrardatos_callback(update,context):
    q=update.callback_query; uid=q.from_user.id
    if q.data=="bd:no":
        await q.answer("Cancelado")
        try: await q.edit_message_text("Cancelado ✅ no borré nada.")
        except Exception: pass
        return
    await q.answer("Borrando…")
    try:
        res=await borrar_datos_usuario(uid,context)
        txt=f"Listo 🧹 borré tus datos ({res})."
    except Exception as e:
        txt=f"No pude borrar todo 😕 {str(e)[:120]}"
    try: await q.edit_message_text(txt)
    except Exception: pass

# ---------- COMANDOS BÁSICOS ----------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Qué onda {update.effective_user.first_name} bro! 🚀 V27\nSoy tu IA gratis: imágenes, fotos, archivos, audios, videos, TikToks, stickers, notas, clima, dólar, recordatorios y más 🔥🇵🇪\nUsa /ayuda para ver todo")
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
        "🎬 /tt link → TikTok sin marca (links normales, cortos y de TikTok Lite; o pega el link / el texto de compartir solo; con botón 🎵 Solo audio)\n"
        "🎵 /mp3 link → audio de un TikTok\n"
        "🎭 /s → responde a una foto con /s y la vuelvo sticker\n"
        "🎚️ /modo → normal, serio, profesor, programador, bromista\n"
        "👤 /perfil · /olvida → lo que sé de ti\n"
        "🧹 /clear → borra la memoria del chat\n"
        "🔒 /privacidad → qué guardo · /borrardatos → borra todo lo tuyo\n"
        "🆔 /id\n\n"
        "🖼️ Fotos: mándame una (qué tiene, qué significa, qué dice, recrea esto en estilo anime)\n"
        "📄 Archivos: PDF (también escaneados), Word, Excel, TXT, CSV, código (pregunta en el pie o después)\n"
        "🎧 Audios y 🎬 videos: mándamelos y los transcribo y analizo (hasta 20 MB); luego pregúntame ('qué dice el audio', 'transcribe el audio')\n"
        "🎤 Si me hablas con voz, te respondo con voz\n\n"
        "👥 EN GRUPOS: los comandos funcionan normal; para hablar con la IA mencióname con @ y tu pregunta (o respóndele a un mensaje mío).\n"
        "   /resumen → resume la charla del grupo · /reglas → ver las reglas\n"
        "   ⚙️ Admins del grupo: /panel (menú con botones) · /bienvenida · /mensajebienvenida · /fotobienvenida · /reglas · /botonbienvenida · /verificacion · /antispam · /despedida")
async def clear_cmd(update,context):
    ck=ckey(update)
    user_memories[ck]=[]; guardar_memorias(ck)
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
    if not update.message: return   # ignora mensajes editados y otros tipos de actualización
    user_id=update.effective_user.id
    ck=ckey(update)
    grupo=en_grupo(update)
    # en grupos solo responde si lo mencionan (o le responden a un mensaje suyo)
    if not me_hablan(update,context): return
    texto_original=texto_override or update.message.text or ""
    if texto_override is None: texto_original=limpiar_mencion(texto_original,context)
    if not texto_original.strip():
        if texto_override is None and grupo and (update.message.text or "").strip():
            await update.message.reply_text("Dime bro, ¿en qué te ayudo? 😎")
        return
    if texto_override is None:
        if time.time()-_ultimo_msg.get(user_id,0)<COOLDOWN: return
        _ultimo_msg[user_id]=time.time()
    low_o=texto_original.lower()
    # si me hablas con voz, respondo con voz
    quiere_audio=any(x in low_o for x in ["en audio","nota de voz","audio","con voz","hablame"]) or texto_override is not None

    # link de TikTok pegado solo (con o sin el texto de compartir), o "descarga + link"
    link_tt=sacar_link_tt(texto_original)
    if link_tt and (solo_link_tt(texto_original) or any(k in low_o for k in ["descarga","baja","bájame","bajame","sin marca"])):
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
        await resumir_url(update,limpiar_url(url_gen.group(0))); return

    rm=update.message.reply_to_message
    if rm and rm.photo and rm.from_user and not rm.from_user.is_bot and not any(v in low_o for v in VERBOS_CREAR):
        try: await procesar_foto(update,context,rm.photo[-1].file_id,texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return
    if es_pregunta_foto(low_o,ck):
        try: await procesar_foto(update,context,ultima_foto[ck][0],texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return
    if es_pregunta_doc(low_o,ck):
        try: await responder_sobre_doc(update,ck,texto_original)
        except Exception as e: await update.message.reply_text(msg_error(e))
        return

    texto_corregido=await asyncio.to_thread(corregir_y_entender,texto_original)
    if es_pedido_imagen(texto_corregido):
        await crear_imagen(update,texto_corregido); return
    t_low=texto_corregido.lower()
    busco=any(x in t_low for x in CLAVES_BUSQUEDA)
    contexto_busqueda=""
    if busco:
        contexto_busqueda=await asyncio.to_thread(buscar_en_internet,texto_corregido)
    es_tradu=any(x in t_low for x in ["traduce","translate"])
    complejo=es_complejo(texto_corregido) and not es_tradu
    async with lock_de(ck):   # un mensaje a la vez por persona: así la memoria del chat no se mezcla
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
            memoria=get_memory(ck)
            memoria.append({"role":"user","content":texto_corregido})
            if len(memoria)>14: memoria=memoria[-14:]
            user_memories[ck]=memoria
            if es_tradu:
                system_prompt="Eres traductor PRO. Traduce directo."
                temp=0.3
            else:
                system_prompt=construir_system(user_id,grupo)
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
            user_memories[ck]=memoria; guardar_memorias(ck)
            await enviar_largo(update,respuesta)
            if quiere_audio: await enviar_audio(update,respuesta)
            if APRENDER_PERFIL and not es_tradu and RE_PERFIL.search(texto_corregido):
                t=asyncio.create_task(aprender_perfil(user_id,texto_corregido)); _tareas.add(t); t.add_done_callback(_tareas.discard)
        except Exception as e:
            logging.error(f"Error en el chat IA: {type(e).__name__}")
            await update.message.reply_text(msg_error(e))
            if "429" not in str(e) and "rate" not in str(e).lower():
                await alertar_admin("Error en el chat IA",f"{type(e).__name__}: {str(e)[:300]}")

async def voz_handler(update,context):
    if not update.message: return
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
    if not update.message: return
    if not me_hablan(update,context): return   # en grupos: solo con @mención en el pie o respuesta al bot
    try:
        doc=update.message.document
        nombre=(doc.file_name or "").lower()
        mime=(doc.mime_type or "")
        caption=limpiar_mencion(update.message.caption or "",context)
        if mime.startswith("image/") or nombre.endswith((".jpg",".jpeg",".png",".webp")):
            if any(k in caption.lower() for k in ["sticker","estiker"]):
                await hacer_sticker(update.message,context,doc.file_id); return
            if not await consumir(update): return
            if es_pedido_imagen(caption): await crear_imagen(update,caption); return
            await procesar_foto(update,context,doc.file_id,caption,doc.mime_type or "image/jpeg"); return
        # audios y videos enviados como archivo
        es_vid=mime.startswith("video/") or nombre.endswith((".mp4",".mov",".mkv",".webm",".avi"))
        if es_vid or mime.startswith("audio/") or nombre.endswith((".mp3",".m4a",".wav",".ogg",".opus",".aac",".flac")):
            if not await consumir(update): return
            await procesar_media(update,context,doc,doc.file_name or "archivo",es_vid,caption,doc.file_size); return
        if not await consumir(update): return
        await procesar_archivo(update,context,doc,caption)
    except Exception as e: await update.message.reply_text(f"Error archivo: {str(e)[:250]}")

async def foto_handler(update,context):
    if not update.message: return
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

# ---------- MANTENIMIENTO: GUARDADO EN LOTE Y LIMPIEZA DE MEMORIA ----------
def limpiar_memoria():
    """Borra de la RAM lo que ya caducó (archivos, fotos, búsquedas, avisos...)."""
    t=time.time(); hoy=datetime.now(LIMA).strftime("%Y-%m-%d")
    for k,v in list(ultimo_doc.items()):
        if t-v[2]>VENTANA_DOC*2: ultimo_doc.pop(k,None); resumen_doc.pop(k,None)
    for k,v in list(ultima_foto.items()):
        if t-v[1]>VENTANA_FOTO*2: ultima_foto.pop(k,None)
    for k,v in list(ultimo_img.items()):
        if t-v[4]>3600: ultimo_img.pop(k,None)
    for k,v in list(_cache_busq.items()):
        if t-v[0]>600: _cache_busq.pop(k,None)
    limpiar_tt()
    for k,v in list(recien.items()):
        if t-v>3600: recien.pop(k,None)
    for cid,dq in list(joins.items()):
        while dq and t-dq[0]>120: dq.popleft()
        if not dq: joins.pop(cid,None)
    for dic,ttl in ((expulsados,600),(_aviso_perm,3600),(_aviso_spam,3600),(_alertas,3600),(_aviso_error,3600),(_ultimo_msg,600),(_marca_usr,3600),(ult_saludo_ts,86400)):
        for k,v in list(dic.items()):
            if t-v>ttl: dic.pop(k,None)
    for k,v in list(uso_diario.items()):
        if v[0]!=hoy: uso_diario.pop(k,None)
    for cid,d in list(msgs_grupo.items()):
        if not d or t-d[-1][0]>86400: msgs_grupo.pop(cid,None)
    if len(_locks_usuario)>500:
        for k,l in list(_locks_usuario.items()):
            if not l.locked(): _locks_usuario.pop(k,None)

ult_saludo_ts={}   # (reservado: marcas de tiempo auxiliares; se limpia solo)
_tarea_mant=None
async def _bucle_mantenimiento():
    ciclos=0
    while True:
        await asyncio.sleep(INTERVALO_GUARDADO)
        try: await flush_async()
        except Exception as e: logging.warning(f"Guardado falló: {type(e).__name__}")
        ciclos+=1
        if ciclos%max(1,600//INTERVALO_GUARDADO)==0:   # cada ~10 minutos
            try: limpiar_memoria()
            except Exception as e: logging.warning(f"Limpieza falló: {type(e).__name__}")

# ---------- ARRANQUE ----------
async def post_init(app):
    global APP_BOT,_tarea_mant
    APP_BOT=app.bot
    _tarea_mant=asyncio.create_task(_bucle_mantenimiento())
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
            BotCommand("privacidad","Qué guarda el bot de ti"),BotCommand("borrardatos","Borrar todos mis datos"),
            BotCommand("reglas","Ver las reglas del grupo"),
            BotCommand("panel","Panel de bienvenida y seguridad (admins)"),
            BotCommand("bienvenida","Bienvenida del grupo (admins)"),
            BotCommand("mensajebienvenida","Personalizar el mensaje de bienvenida"),
            BotCommand("fotobienvenida","Foto o video de bienvenida"),
            BotCommand("botonbienvenida","Botones de enlace del saludo"),
            BotCommand("verificacion","Verificación anti-bots (admins)"),
            BotCommand("antispam","Anti-spam a recién llegados (admins)"),
            BotCommand("despedida","Mensaje de despedida (admins)"),
            BotCommand("clear","Borrar memoria del chat")])
    except Exception as e: logging.warning(f"set_my_commands: {type(e).__name__}")
    if app.job_queue:
        for r in list(recordatorios):
            if r["ts"]<time.time()-86400: recordatorios.remove(r); continue
            programar(app.job_queue,r)
        # retoma las verificaciones que quedaron pendientes si el bot se reinició
        for cid_,cfg_ in list(grupos.items()):
            for uid_s,info in list((cfg_.get("pend") or {}).items()):
                try: programar_verif(app.job_queue,cid_,int(uid_s),info.get("t",0)-time.time())
                except Exception: pass
        if BACKUP_AUTOMATICO and ADMIN_ID:
            app.job_queue.run_daily(backup_auto,time=dtime(3,0,tzinfo=LIMA),name="backup_diario")
async def post_shutdown(app):
    try: await flush_async()   # guarda lo pendiente antes de apagar
    except Exception as e: logging.warning(f"Guardado final falló: {type(e).__name__}")

if __name__=="__main__":
    app=(ApplicationBuilder().token(TOKEN).concurrent_updates(MAX_CONCURRENTES)
         .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_error_handler(error_handler)
    MSG=filters.UpdateType.MESSAGE   # solo mensajes nuevos (los editados se ignoran, salvo en el anti-spam)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS,antispam_handler),group=-2)
    app.add_handler(TypeHandler(Update,registrar_usuario),group=-1)
    for nombre,fn in [("start",start),("ayuda",ayuda_cmd),("clear",clear_cmd),("imagen",imagen_cmd),("buscar",buscar_cmd),
                      ("tt",tt_cmd),("mp3",mp3_cmd),("s",s_cmd),("trad",trad_cmd),("resumir",resumir_cmd),
                      ("clima",clima_cmd),("dolar",dolar_cmd),("qr",qr_cmd),("voz",voz_cmd),("modo",modo_cmd),
                      ("perfil",perfil_cmd),("olvida",olvida_cmd),("pensar",pensar_cmd),("recordar",recordar_cmd),
                      ("recordatorios",recordatorios_cmd),("cancelar",cancelar_cmd),("id",id_cmd),
                      ("stats",stats_cmd),("broadcast",broadcast_cmd),("backup",backup_cmd),
                      ("nota",nota_cmd),("notas",notas_cmd),("borrarnota",borrarnota_cmd),("redactar",redactar_cmd),
                      ("resumen",resumen_cmd),("bienvenida",bienvenida_cmd),("fotobienvenida",fotobienvenida_cmd),
                      ("mensajebienvenida",mensajebienvenida_cmd),("reglas",reglas_cmd),
                      ("botonbienvenida",botonbienvenida_cmd),("verificacion",verificacion_cmd),
                      ("antispam",antispam_cmd),("despedida",despedida_cmd),("panel",panel_cmd),
                      ("privacidad",privacidad_cmd),("borrardatos",borrardatos_cmd)]:
        app.add_handler(CommandHandler(nombre,fn,filters=MSG))
    app.add_handler(CallbackQueryHandler(img_callback,pattern="^img:"))
    app.add_handler(CallbackQueryHandler(tt_callback,pattern="^tt:"))
    app.add_handler(CallbackQueryHandler(modo_callback,pattern="^modo:"))
    app.add_handler(CallbackQueryHandler(bv_callback,pattern="^bv:"))
    app.add_handler(CallbackQueryHandler(panel_callback,pattern="^pn:"))
    app.add_handler(CallbackQueryHandler(borrardatos_callback,pattern="^bd:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS,bienvenida_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER,despedida_handler))
    # /s en el pie de una foto: debe ir ANTES de los handlers de foto y documento
    app.add_handler(MessageHandler(MSG & (filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(r"(?i)^/s(@\w+)?(\s|$)"),s_cmd))
    app.add_handler(MessageHandler(MSG & filters.VOICE,voz_handler))
    app.add_handler(MessageHandler(MSG & (filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE),media_handler))
    app.add_handler(MessageHandler(MSG & filters.Document.ALL,documento_handler))
    app.add_handler(MessageHandler(MSG & filters.PHOTO,foto_handler))
    app.add_handler(MessageHandler(MSG & filters.TEXT & ~filters.COMMAND,ia_reply))
    print("Bot V27 iniciado 🔥")
    app.run_polling()
# === FIN DEL ARCHIVO ===
