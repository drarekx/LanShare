#!/usr/bin/env python3
"""LanShare: comparte texto y archivos con la Steam Deck por la red local."""

import json
import mimetypes
import os
import queue
import base64
import secrets
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import filedialog, messagebox, ttk
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urlparse

import roms

PORT = 8765
SHARE_DIR = os.path.expanduser("~/LanShare")
PIN = f"{secrets.randbelow(10**6):06d}"
COOKIE = "lanshare_session"
MAX_FAILS = 5
LOCK_SECONDS = 60

sessions = {}  # id de sesión -> nombre del dispositivo ("iPhone", "Mac"…)
streams = {}  # id de sesión -> conexiones SSE abiertas: así se sabe quién está conectado ahora
file_origin = {}  # nombre de archivo -> quién lo trajo, solo de esta sesión de la app
fails = {}  # ip -> (intentos, bloqueado_hasta)
auth_lock = threading.Lock()
MAX_ITEMS = 50

items = []  # [{"id", "text", "from", "time"}]
items_lock = threading.Lock()
ui_queue = queue.Queue()
changed = threading.Condition()
version = 0


# ROMs pendientes de añadir a Steam y estado de la última operación
steam_lock = threading.Lock()
steam_state = {"pending": [], "status": "idle", "message": "", "recent": []}
# En modo headless, función que recibe cada acceso nuevo en vez de dejarlo pendiente (ver headless())
on_shortcut = None


def steam_snapshot():
    with steam_lock:
        return {"pending": [e["appname"] for e in steam_state["pending"]], "status": steam_state["status"],
                "message": steam_state["message"], "recent": list(steam_state["recent"])}


def apply_steam():
    """Añade a Steam los ROMs pendientes en segundo plano. False si ya hay una operación en marcha."""
    with steam_lock:
        if steam_state["status"] == "working" or not steam_state["pending"]:
            return False
        steam_state["status"], steam_state["message"] = "working", "Añadiendo a Steam…"
        entries = list(steam_state["pending"])
    bump()

    def work():
        def log(m):
            ui_queue.put(("log", m, "steam"))
            with steam_lock:
                steam_state["message"] = m
            bump()
        try:
            n = roms.add_to_steam(entries, log)
            with steam_lock:
                steam_state["pending"] = [e for e in steam_state["pending"] if e not in entries]
                steam_state["status"] = "ok"
                steam_state["message"] = f"✔ {n} juego(s) añadido(s) a Steam" if n else "Ya estaban todos en Steam"
        except Exception as e:  # noqa: BLE001
            with steam_lock:
                steam_state["status"], steam_state["message"] = "error", f"✖ {e}"
            ui_queue.put(("log", f"Steam: {e}", "warn"))
        bump()
        ui_queue.put(("steam", None))
    threading.Thread(target=work, daemon=True).start()
    return True


def device_name(ua):
    """Nombre corto del dispositivo a partir del User-Agent del navegador."""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                      ("Windows", "Windows"), ("CrOS", "Chromebook"), ("Linux", "Linux")):
        if key in ua:
            return name
    return "Navegador"


def connected_devices():
    """Dispositivos con la web abierta ahora mismo (tienen una conexión SSE viva)."""
    with auth_lock:
        return sorted({sessions[sid] for sid, n in streams.items() if n > 0 and sid in sessions})


_ips_cache = {"at": 0, "ips": []}


def link_label(ip):
    """'Cable' o 'Wi-Fi' según la IP de la Deck por la que ha entrado el navegador."""
    for addr, label in _ips_cache["ips"]:
        if addr == ip:
            return label
    if time.time() - _ips_cache["at"] > 30:  # IP nueva (p. ej. se ha enchufado el cable): se vuelve a mirar
        _ips_cache.update(at=time.time(), ips=local_ips())
        return link_label(ip)
    return ""


def bump():
    global version
    with changed:
        version += 1
        changed.notify_all()


def local_ips():
    """[(ip, etiqueta)] con el cable primero: es más rápido y no gasta el Wi-Fi de la Deck."""
    ips = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            iface, ip = parts[1], parts[3].split("/")[0]
            if ip.startswith("127."):
                continue
            if iface.startswith(("en", "eth")):
                ips.append((0, ip, "Cable"))
            elif iface.startswith(("wl", "wlan")):
                ips.append((1, ip, "Wi-Fi"))
            else:
                ips.append((2, ip, iface))
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ips.append((0, s.getsockname()[0], "Red"))
            s.close()
        except Exception:
            ips.append((0, "127.0.0.1", "Local"))
    return [(ip, label) for _, ip, label in sorted(ips)]


def add_item(text, source):
    with items_lock:
        item = {"id": int(time.time() * 1000), "text": text, "from": source,
                "time": time.strftime("%H:%M:%S")}
        items.insert(0, item)
        del items[MAX_ITEMS:]
    bump()
    return item


def safe_name(name):
    name = os.path.basename(name.replace("\\", "/")).strip() or "archivo"
    name = name.lstrip(".") or "archivo"
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.lexists(os.path.join(SHARE_DIR, candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate


def list_files():
    files = []
    for name in sorted(os.listdir(SHARE_DIR), key=str.lower):
        path = os.path.join(SHARE_DIR, name)
        if os.path.isfile(path) and not name.endswith(".part"):
            st = os.stat(path)
            files.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files


PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0e1319">
<title>LanShare · Steam Deck</title>
<style>
:root{--bg:#0e1319;--card:#161d26;--field:#1e2732;--line:#2b3644;--sep:#222c38;--fg:#e6edf3;--mut:#8b98a5;
--acc:#1a9fff;--on-acc:#06121f;--link:#5cb8ff;--ok:#3ecf8e;--warn:#f2b84b;--bad:#ff6b6b}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-tap-highlight-color:transparent}
.wrap{max-width:760px;margin:auto;padding:16px;display:flex;flex-direction:column;gap:14px}
.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.meta{color:var(--mut);font-size:13px}
a{color:var(--link);text-decoration:none}
button{font:inherit;cursor:pointer;border:0;border-radius:10px;min-height:44px;padding:0 16px;color:var(--fg);background:var(--field)}
button:hover{background:var(--line)}
button.pri{background:var(--acc);color:var(--on-acc);font-weight:700}
button.pri:hover{background:#3aaeff}
button:disabled{opacity:.45;cursor:default}
button:focus-visible,a:focus-visible,textarea:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
/* cabecera */
.top{display:flex;align-items:center;gap:12px;padding-top:2px}
.logo{width:36px;height:36px;border-radius:10px;background:var(--acc);display:grid;place-items:center;flex:none}
.grow{flex:1;min-width:0}
.name{font-weight:700;font-size:17px}
.conn{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none}
.conn.off .dot{background:var(--bad)}.conn.off{color:var(--bad)}
/* pestañas */
.tabs{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1fr) minmax(0,1.3fr);gap:4px;padding:4px;background:var(--card);border-radius:12px;position:sticky;top:8px;z-index:2;box-shadow:0 0 0 8px var(--bg)}
.tabs button{background:transparent;color:var(--mut);font-weight:600;font-size:14px;border-radius:9px;display:flex;align-items:center;justify-content:center;gap:5px;padding:0 4px;white-space:nowrap}
.tabs button[aria-selected=true]{background:var(--line);color:var(--fg)}
.badge{font-size:11px;font-weight:700;border-radius:999px;padding:1px 7px;background:var(--field);color:var(--mut)}
.badge.warn{background:var(--warn);color:#1a1406}
.badge:empty{display:none}
[role=tabpanel]{display:flex;flex-direction:column;gap:14px}
[role=tabpanel][hidden]{display:none}
/* tarjetas y listas */
.card{background:var(--card);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px}
.lbl{font-size:13px;font-weight:600;color:var(--mut);letter-spacing:.04em;text-transform:uppercase;margin:0}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:0 2px}
textarea{width:100%;min-height:96px;background:var(--field);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:12px;font:inherit;resize:vertical}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.list{list-style:none;margin:0;padding:0;background:var(--card);border-radius:14px}
.list>li{display:flex;align-items:center;gap:12px;padding:10px 14px;border-top:1px solid var(--sep)}
.list>li:first-child{border-top:0}
.list .empty{color:var(--mut);padding:16px 14px}
.ft{width:40px;height:40px;border-radius:9px;font-size:11px;font-weight:700;display:grid;place-items:center;flex:none;background:#26303c;color:#b7c2cd}
.ft.img{background:#1d3550;color:#8fd0ff}.ft.vid{background:#2a2340;color:#b9a3ff}.ft.doc{background:#3a2f16;color:var(--warn)}
.ft.zip{background:#163a2c;color:var(--ok)}.ft.ask{background:#3a2f16;color:var(--warn);font-size:18px}
.info{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.info .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--fg)}
.act{min-height:44px;min-width:44px;display:grid;place-items:center;padding:0 6px;font-size:14px;font-weight:600}
.txt pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit}
.txt{align-items:flex-start!important}
/* subir */
.drop{border:1.5px dashed #3a4757;border-radius:14px;background:#121921;padding:22px 16px;display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;width:100%;min-height:0}
.drop:hover,.drop.over{border-color:var(--acc);background:#10263b}
.drop b{font-weight:600}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--acc);border-radius:3px;transition:width .2s}
.up{display:flex;flex-direction:column;gap:6px}
.up .row{justify-content:space-between;flex-wrap:nowrap}
.up .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ROMs */
select{background:var(--field);color:var(--fg);border:1px solid var(--line);border-radius:10px;min-height:44px;padding:0 10px;font:inherit;max-width:100%}
.rom.need{background:#1f1b12;box-shadow:inset 0 0 0 1px #5a4719}
.rom .wide{flex-basis:100%;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.list>li.rom{flex-wrap:wrap}
.okt{font-size:12px;font-weight:600;color:var(--ok)}
.x{background:none;color:var(--mut);min-width:44px;padding:0}
.chk{display:flex;gap:12px;align-items:flex-start;flex-direction:row}
.chk input{width:22px;height:22px;margin:0;accent-color:var(--acc);flex:none}
#romgo{width:100%;min-height:52px;font-size:16px}
#steam{background:#0f2236}
#rominfo:empty{display:none}
#steam:empty{display:none}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--line);color:var(--fg);padding:10px 16px;border-radius:10px;display:none;box-shadow:0 6px 24px #0008;max-width:calc(100% - 32px)}
</style></head><body><div class="wrap">
<header class="top">
<div class="logo" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#06121f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg></div>
<div class="grow"><div class="name">LanShare</div><div class="conn" id="conn"><span class="dot"></span><span id="connText">Conectando…</span></div></div>
<form method="post" action="/logout"><button>Salir</button></form>
</header>
<nav class="tabs" role="tablist" aria-label="Secciones">
<button role="tab" id="t-text" aria-controls="p-text" data-tab="text">Texto <span class="badge" id="b-text"></span></button>
<button role="tab" id="t-files" aria-controls="p-files" data-tab="files">Archivos <span class="badge" id="b-files"></span></button>
<button role="tab" id="t-roms" aria-controls="p-roms" data-tab="roms">Juegos retro <span class="badge warn" id="b-roms"></span></button>
</nav>

<section role="tabpanel" id="p-text" aria-labelledby="t-text">
<div class="card">
<label class="lbl" for="txt">Enviar texto a la Deck</label>
<textarea id="txt" placeholder="Escribe o pega aquí…"></textarea>
<div class="row"><button class="pri" onclick="sendText()">Enviar</button><span class="meta">Se copia en el portapapeles de la Deck · Ctrl+Intro</span></div>
</div>
<div class="head"><h2 class="lbl">Historial</h2><span class="meta" id="c-text"></span></div>
<ul class="list" id="items"><li class="empty">Nada todavía</li></ul>
</section>

<section role="tabpanel" id="p-files" aria-labelledby="t-files" hidden>
<button class="drop" id="drop" type="button">
<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#1a9fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5M4 20h16"/></svg>
<b>Elegir archivos para la Deck</b><span class="meta">También puedes arrastrarlos o pegarlos</span>
</button>
<input id="file" type="file" multiple hidden>
<div class="card" id="prog" hidden>
<div class="row meta" style="justify-content:space-between"><span id="upsum"></span><span id="upeta"></span></div>
<div id="uplist" style="display:flex;flex-direction:column;gap:12px"></div>
</div>
<div class="head"><h2 class="lbl">En la Deck · ~/LanShare</h2><span class="meta" id="c-files"></span></div>
<ul class="list" id="files"><li class="empty">Aún no hay archivos</li></ul>
</section>

<section role="tabpanel" id="p-roms" aria-labelledby="t-roms" hidden>
<div id="romoff" class="card meta" hidden>EmuDeck no está configurado en la Deck.</div>
<div id="romon" style="display:flex;flex-direction:column;gap:14px">
<button class="drop" id="romdrop" type="button">
<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#1a9fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="7" width="20" height="10" rx="3"/><path d="M6 12h3M7.5 10.5v3M16 11h.01M18 13h.01"/></svg>
<b>Elegir ROMs</b><span class="meta">La consola se detecta por la extensión</span>
</button>
<input id="romfile" type="file" multiple hidden>
<ul class="list" id="romlist" hidden></ul>
<label class="card chk"><input type="checkbox" id="tosteam" checked>
<span class="info"><span style="font-weight:600">Añadir a Steam al terminar</span><span class="meta">Steam se cierra y se vuelve a abrir solo. No lo hagas con un juego abierto.</span></span></label>
<button class="pri" id="romgo" onclick="installRoms()" disabled>Instalar</button>
<div class="meta" id="rominfo" style="text-align:center"></div>
<div class="card" id="steam"></div>
</div>
</section>
</div><div id="toast" role="status"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',1800)}
function fmt(n){const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return (i?n.toFixed(1).replace('.',','):n)+' '+u[i]}
function eta(s){if(!isFinite(s))return '';return s<60?Math.max(1,Math.round(s))+' s':Math.floor(s/60)+' min '+Math.round(s%60)+' s'}
function ago(t){const s=Date.now()/1000-t;if(s<60)return 'ahora';if(s<3600)return 'hace '+Math.floor(s/60)+' min';
  if(s<86400)return 'hace '+Math.floor(s/3600)+' h';if(s<172800)return 'ayer';return new Date(t*1000).toLocaleDateString('es')}
async function copy(text){
  try{await navigator.clipboard.writeText(text);}
  catch(e){const a=document.createElement('textarea');a.value=text;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();}
  toast('Copiado');
}
// ---------- pestañas (se recuerda la última en este navegador)
const TABS=['text','files','roms'];
function show(tab){
  if(!TABS.includes(tab))tab='text';
  for(const t of TABS){$('t-'+t).setAttribute('aria-selected',t==tab);$('p-'+t).hidden=t!=tab}
  try{localStorage.setItem('lanshare.tab',tab)}catch(e){}
}
document.querySelectorAll('[role=tab]').forEach(b=>b.onclick=()=>show(b.dataset.tab));
let start=location.hash.slice(1);if(!TABS.includes(start))try{start=localStorage.getItem('lanshare.tab')||'text'}catch(e){}
show(start);
// ---------- estado
function online(ok,via){
  $('conn').classList.toggle('off',!ok);
  $('connText').textContent=ok?'Conectado a la Steam Deck'+(via?' · '+via.toLowerCase():''):'Sin conexión con la Deck';
}
const FT={img:['png','jpg','jpeg','gif','webp','bmp','heic','avif','svg'],vid:['mp4','mkv','webm','mov','avi','mp3','flac','ogg','wav','m4a','opus'],
  doc:['pdf','txt','md','doc','docx','odt','rtf','csv','json','log'],zip:['zip','7z','rar','tar','gz','xz','zst','bz2']};
const VIEW=['png','jpg','jpeg','gif','webp','bmp','avif','svg','mp4','webm','mov','mp3','flac','ogg','wav','m4a','opus','pdf','txt','md','csv','json','log'];
const extOf=n=>(n.includes('.')?n.split('.').pop():'').toLowerCase();
function ftClass(e){for(const k in FT)if(FT[k].includes(e))return k;return ''}
let ITEMS=[];
async function refresh(){
  let d;
  try{const r=await fetch('/api/state');if(r.status==401)return location.reload();if(!r.ok)throw 0;d=await r.json()}
  catch(e){online(false);return}
  online(true,d.via);
  ITEMS=d.items;renderSteam(d.steam);
  $('c-text').textContent=d.items.length?d.items.length+' textos':'';
  $('items').innerHTML=d.items.length?d.items.map((it,i)=>`<li class="txt"><div class="info"><pre>${esc(it.text)}</pre>
    <span class="meta">${esc(it.from)} · ${esc(it.time.slice(0,5))}</span></div><button onclick="copy(ITEMS[${i}].text)">Copiar</button></li>`).join('')
    :'<li class="empty">Nada todavía</li>';
  $('b-files').textContent=d.files.length||'';
  $('c-files').textContent=d.files.length==1?'1 archivo':d.files.length?d.files.length+' archivos':'';
  $('files').innerHTML=d.files.length?d.files.map(f=>{const u='/files/'+encodeURIComponent(f.name),e=extOf(f.name),v=VIEW.includes(e);
    return `<li><span class="ft ${ftClass(e)}" aria-hidden="true">${esc((e||'—').slice(0,4).toUpperCase())}</span>
    <a class="info" href="${u}" title="Descargar ${esc(f.name)}"><span class="n">${esc(f.name)}</span>
    <span class="meta">${fmt(f.size)} · ${ago(f.mtime)}${f.from?' · desde '+esc(f.from):''}</span></a>
    ${v?`<a class="act" href="${u}?inline=1" target="_blank">Ver</a>`:`<a class="act" href="${u}">Bajar</a>`}</li>`}).join('')
    :'<li class="empty">Aún no hay archivos</li>';
}
async function sendText(){
  const v=$('txt').value;if(!v.trim())return;
  const r=await fetch('/api/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:v})});
  if(r.ok){$('txt').value='';toast('Enviado a la Deck');refresh();}else toast('Error al enviar');
}
$('txt').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))sendText()});
// ---------- subidas: 2 a la vez, con resumen de velocidad y tiempo restante
const UPS=[];let upTotal=0,upDone=0;
function renderUps(){
  const act=UPS.filter(u=>u.started);
  $('prog').hidden=!UPS.length;
  if(!UPS.length){upTotal=upDone=0;return}
  $('upsum').textContent=`Subiendo ${Math.min(upDone+1,upTotal)} de ${upTotal}`;
  const left=UPS.reduce((a,u)=>a+u.size-u.loaded,0),rate=act.reduce((a,u)=>a+u.rate,0);
  $('upeta').textContent=rate?'Quedan '+eta(left/rate):'';
  $('uplist').innerHTML=act.map(u=>`<div class="up"><div class="row"><span class="n">${esc(u.name)}</span>
    <span class="meta mono" style="font-size:12px;flex:none">${u.rate?fmt(u.rate)+'/s · ':''}${Math.round(u.loaded/u.size*100||0)} %</span></div>
    <div class="bar"><i style="width:${u.loaded/u.size*100||0}%"></i></div></div>`).join('');
}
function upload(file){
  return new Promise(res=>{
    const u={name:file.name,size:file.size||1,loaded:0,rate:0,started:true};UPS.push(u);renderUps();
    const x=new XMLHttpRequest();x.open('PUT','/api/upload?name='+encodeURIComponent(file.name));
    const t0=performance.now();let last={t:t0,b:0};
    x.upload.onprogress=e=>{if(!e.lengthComputable)return;u.loaded=e.loaded;
      const now=performance.now(),dt=(now-last.t)/1000;
      if(dt>=0.5){const r=(e.loaded-last.b)/dt;u.rate=u.rate?u.rate*0.6+r*0.4:r;last={t:now,b:e.loaded}}renderUps()};
    const end=msg=>{UPS.splice(UPS.indexOf(u),1);upDone++;renderUps();toast(msg);refresh();res()};
    x.onload=()=>{if(x.status==401)return location.reload();
      end(x.status==200?`Subido: ${file.name} (${fmt(file.size/((performance.now()-t0)/1000))}/s)`:'Error subiendo '+file.name)};
    x.onerror=()=>end('Error subiendo '+file.name);
    x.send(file);
  });
}
const PARALLEL=2;
async function uploadAll(fs){if(!fs.length)return;show('files');upTotal+=fs.length;const q=[...fs];
  await Promise.all(Array.from({length:Math.min(PARALLEL,q.length)},async()=>{while(q.length)await upload(q.shift())}))}
function dropZone(el,input,onFiles){
  el.onclick=()=>input.click();
  input.onchange=e=>{onFiles([...e.target.files]);e.target.value=''};
  el.ondragover=e=>{e.preventDefault();el.classList.add('over')};
  el.ondragleave=()=>el.classList.remove('over');
  el.ondrop=e=>{e.preventDefault();el.classList.remove('over');onFiles([...e.dataTransfer.files])};
}
dropZone($('drop'),$('file'),uploadAll);
document.addEventListener('paste',e=>{if(e.target.id!=='txt'&&e.clipboardData.files.length)uploadAll([...e.clipboardData.files])});
// ---------- ROMs
let SYS=[],ROMQ=[];
const TRACK=['bin','img','sub','ccd','raw'];
const SHORT={nes:'NES',snes:'SNES',n64:'N64',gb:'GB',gbc:'GBC',gba:'GBA',nds:'DS',n3ds:'3DS',gamecube:'GC',wii:'WII','wiiu/roms':'WIIU',
  switch:'NSW',psx:'PS1',ps2:'PS2',psp:'PSP',genesis:'MD',mastersystem:'SMS',gamegear:'GG',segacd:'MCD',dreamcast:'DC',saturn:'SAT',fbneo:'ARC',mame:'ARC'};
const stemOf=n=>n.replace(/\.[^.]+$/,'');
function detect(n){const e=extOf(n);const m=SYS.filter(s=>s.exts.includes(e));return m.length==1?m[0].folder:''}
async function loadSystems(){
  const r=await fetch('/api/systems');if(!r.ok)return;const d=await r.json();SYS=d.systems;
  if(!d.available){$('romon').hidden=true;$('romoff').hidden=false}
}
function renderRoms(){
  $('romlist').hidden=!ROMQ.length;
  $('romlist').innerHTML=ROMQ.map((f,i)=>{const s=SYS.find(s=>s.folder==f.sys);
    return `<li class="rom ${f.sys?'':'need'}">
    <span class="ft ${f.sys?'img':'ask'}" aria-hidden="true">${f.sys?esc(SHORT[f.sys]||f.sys.slice(0,4).toUpperCase()):'?'}</span>
    <span class="info"><span class="n">${esc(stemOf(f.file.name))}</span><span class="meta">${esc(f.file.name)} · ${fmt(f.file.size)}</span></span>
    ${f.pct!=null?`<span class="meta mono">${Math.round(f.pct)} %</span>`:f.sys?`<span class="okt">${esc(s?s.label:f.sys)}</span>`:''}
    <button class="x" aria-label="Quitar ${esc(f.file.name)}" onclick="ROMQ.splice(${i},1);renderRoms()">✕</button>
    ${f.pct!=null?`<div class="wide"><div class="bar" style="flex:1"><i style="width:${f.pct}%"></i></div></div>`:''}
    ${!f.sys||f.pick?`<div class="wide"><label class="meta" for="sys${i}" style="color:var(--warn)">${f.sys?'Consola':'¿De qué consola es?'}</label>
    <select id="sys${i}" onchange="ROMQ[${i}].sys=this.value;ROMQ[${i}].pick=true;renderRoms()"><option value="">Elige…</option>
    ${sysFor(f.file.name).map(s=>`<option value="${s.folder}" ${s.folder==f.sys?'selected':''}>${esc(s.label)}</option>`).join('')}</select></div>`:''}
    </li>`}).join('');
  const ready=ROMQ.length&&ROMQ.every(f=>f.sys),size=ROMQ.reduce((a,f)=>a+f.file.size,0);
  $('romgo').disabled=!ready;
  $('romgo').textContent=ROMQ.length?`Instalar ${ROMQ.length} ${ROMQ.length==1?'archivo':'archivos'} · ${fmt(size)}`:'Instalar';
  $('rominfo').textContent=ROMQ.length&&!ready?'Elige la consola de los marcados en amarillo':'';
}
// en el desplegable, solo las consolas que aceptan esa extensión; si ninguna, todas
function sysFor(n){const m=SYS.filter(s=>s.exts.includes(extOf(n)));return m.length?m:SYS}
function addRoms(files){for(const f of files){const sys=detect(f.name);ROMQ.push({file:f,sys,pick:!sys})}renderRoms()}
function putRom(f,steam){
  return new Promise(res=>{
    const x=new XMLHttpRequest();
    x.open('PUT','/api/rom?'+new URLSearchParams({system:f.sys,name:f.file.name,steam:steam?1:0}));
    x.upload.onprogress=e=>{if(e.lengthComputable){f.pct=e.loaded/e.total*100;renderRoms()}};
    x.onload=()=>{if(x.status==401)return location.reload();let d={};try{d=JSON.parse(x.responseText)}catch(e){}
      res(x.status==200?d:{ok:false,error:x.responseText||'Error'})};
    x.onerror=()=>res({ok:false,error:'Conexión cortada'});
    x.send(f.file);
  });
}
async function installRoms(){
  const steam=$('tosteam').checked;$('romgo').disabled=true;
  const batch=[...ROMQ];const stems=new Set(batch.filter(f=>['cue','m3u','gdi'].includes(extOf(f.file.name))).map(f=>stemOf(f.file.name)));
  let queued=0,errors=[];
  // las pistas (.bin) de un .cue van primero y sin acceso propio
  batch.sort((a,b)=>TRACK.includes(extOf(b.file.name))-TRACK.includes(extOf(a.file.name)));
  for(const f of batch){
    const skip=TRACK.includes(extOf(f.file.name))&&stems.has(stemOf(f.file.name));
    f.pct=0;renderRoms();
    const d=await putRom(f,steam&&!skip);
    if(d.ok){ROMQ.splice(ROMQ.indexOf(f),1);if(d.shortcut)queued++;else if(steam&&!skip&&d.reason)errors.push(`${f.file.name}: ${d.reason}`)}
    else{errors.push(`${f.file.name}: ${d.error}`);f.pct=null}
    renderRoms();
  }
  if(errors.length)alert('Avisos:\n\n'+errors.join('\n'));
  toast(`Instalados ${batch.length-errors.length} archivo(s)`);
  if(steam&&queued)await fetch('/api/steam/apply',{method:'POST'});
  refresh();
}
dropZone($('romdrop'),$('romfile'),addRoms);
function applySteam(){fetch('/api/steam/apply',{method:'POST'}).then(refresh)}
function renderSteam(s){
  const lines=[];
  $('b-roms').textContent=s.pending.length||'';
  if(s.pending.length)lines.push(`<div style="font-weight:600">${s.pending.length==1?'1 juego pendiente':s.pending.length+' juegos pendientes'} de añadir a Steam</div>
    <div class="meta">${s.pending.map(esc).join(' · ')}</div>
    ${s.status!='working'?'<button class="pri" onclick="applySteam()">Añadir a Steam ahora</button>':''}`);
  if(s.message)lines.push(`<div class="meta" style="color:${s.status=='error'?'var(--bad)':s.status=='ok'?'var(--ok)':'var(--mut)'}">${esc(s.message)}</div>`);
  if(s.recent.length)lines.push('<div class="meta">Últimos instalados: '+s.recent.map(esc).join(' · ')+'</div>');
  $('steam').innerHTML=lines.join('');
}
loadSystems();
// Actualizaciones al instante por SSE; sondeo lento solo como respaldo
refresh();
const es=new EventSource('/api/events');
es.onmessage=()=>refresh();
es.onopen=()=>refresh();
es.onerror=()=>online(false);
setInterval(refresh,15000);
</script></body></html>"""


LOGIN = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0e1319">
<title>LanShare · Acceso</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1319;color:#e6edf3;font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{background:#161d26;border-radius:16px;padding:28px 24px;width:100%;max-width:360px;display:flex;flex-direction:column;gap:16px;text-align:center;align-items:center}
.logo{width:48px;height:48px;border-radius:13px;background:#1a9fff;display:grid;place-items:center}
h1{font-size:20px;margin:0}p{margin:0;color:#8b98a5}
label{position:absolute;width:1px;height:1px;overflow:hidden}
input{background:#1e2732;color:#e6edf3;border:1px solid #2b3644;border-radius:12px;padding:12px;font:30px ui-monospace,"SF Mono",Menlo,Consolas,monospace;text-align:center;letter-spacing:.3em;width:100%}
input:focus{outline:2px solid #1a9fff;outline-offset:2px}
button{background:#1a9fff;color:#06121f;font-weight:700;border:0;border-radius:12px;min-height:48px;width:100%;font:inherit;font-weight:700;font-size:16px;cursor:pointer}
.err{color:#ff6b6b}
</style></head><body>
<form method="post" action="/login">
<div class="logo" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#06121f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg></div>
<h1>LanShare</h1>
<p>Escribe el código de 6 cifras que aparece en la Steam Deck</p>
<label for="code">Código de acceso</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required autofocus placeholder="······">
%ERROR%
<button>Entrar</button>
</form></body></html>"""



class Handler(BaseHTTPRequestHandler):
    server_version = "LanShare/1.1"
    protocol_version = "HTTP/1.1"  # keep-alive: menos conexiones nuevas

    def log_message(self, fmt, *args):
        pass

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else ""

    def _authorized(self, _qs=None):
        sid = self._session()
        with auth_lock:
            return bool(sid) and sid in sessions

    def _device(self):
        with auth_lock:
            return sessions.get(self._session()) or self._client()

    def _login_page(self, error="", code=200):
        err = f'<p class="err">{error}</p>' if error else ""
        self._send(code, LOGIN.replace("%ERROR%", err).encode(), "text/html; charset=utf-8")

    def _redirect(self, where, cookie=None):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _login(self):
        ip = self._client()
        now = time.time()
        with auth_lock:
            count, until = fails.get(ip, (0, 0))
            if until > now:
                return self._login_page(f"Demasiados intentos. Espera {int(until - now) + 1} s.", 429)
        length = min(int(self.headers.get("Content-Length", 0)), 1024)
        code = parse_qs(self.rfile.read(length).decode(errors="ignore")).get("code", [""])[0].strip()
        if secrets.compare_digest(code.encode(), PIN.encode()):
            sid = secrets.token_urlsafe(24)
            with auth_lock:
                sessions[sid] = device_name(self.headers.get("User-Agent", ""))
                fails.pop(ip, None)
            ui_queue.put(("log", f"{sessions[sid]} ({ip}) ha entrado", "login"))
            return self._redirect("/", f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
        with auth_lock:
            count += 1
            fails[ip] = (0, now + LOCK_SECONDS) if count >= MAX_FAILS else (count, 0)
        ui_queue.put(("log", f"{ip} introdujo un código incorrecto", "warn"))
        msg = (f"Demasiados intentos. Espera {LOCK_SECONDS} s." if count >= MAX_FAILS
               else f"Código incorrecto ({MAX_FAILS - count} intentos restantes)")
        self._login_page(msg, 401)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.command in ("POST", "PUT") and code >= 400:
            # puede quedar cuerpo sin leer: no reutilizar la conexión
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _client(self):
        return self.client_address[0]

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if not self._authorized():
            if url.path == "/":
                return self._login_page()
            return self._send(401, b"No autorizado")
        if url.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            with items_lock:
                data = {"items": list(items), "steam": steam_snapshot()}
            files = list_files()
            for f in files:
                f["from"] = file_origin.get(f["name"], "")
            data.update(files=files, device=self._device(), via=link_label(self.connection.getsockname()[0]),
                        devices=connected_devices())
            return self._json(data)
        if url.path == "/api/systems":
            return self._json(roms.systems_info())
        if url.path == "/api/events":
            return self._events()
        if url.path.startswith("/files/"):
            return self._download(unquote(url.path[len("/files/"):]), "inline" in qs)
        self._send(404, b"No encontrado")

    def _events(self):
        """Server-Sent Events: avisa al navegador en cuanto cambia algo."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen, last_dir, idle = version, os.stat(SHARE_DIR).st_mtime, 0
        sid = self._session()
        with auth_lock:
            streams[sid] = streams.get(sid, 0) + 1
        ui_queue.put(("devices", None))
        try:
            while True:
                with changed:
                    changed.wait_for(lambda: version != seen, timeout=2)
                    current = version
                dir_m = os.stat(SHARE_DIR).st_mtime
                if current != seen or dir_m != last_dir:
                    seen, last_dir, idle = current, dir_m, 0
                    self.wfile.write(f"data: {current}\n\n".encode())
                else:
                    idle += 2
                    if idle >= 20:  # mantiene viva la conexión
                        idle = 0
                        self.wfile.write(b": ping\n\n")
        except OSError:
            pass
        finally:
            with auth_lock:
                streams[sid] -= 1
                if not streams[sid]:
                    del streams[sid]
            ui_queue.put(("devices", None))

    def _download(self, name, inline):
        name = os.path.basename(name)
        path = os.path.join(SHARE_DIR, name)
        if not name or not os.path.isfile(path):
            return self._send(404, b"No encontrado")
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng:
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                else:
                    start = max(size - int(b), 0)
                code = 206
            except ValueError:
                pass
            if start > end or start >= size:
                return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        ctype = (mimetypes.guess_type(name)[0] or "application/octet-stream") if inline else "application/octet-stream"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                # sendfile: el kernel copia del disco a la red sin pasar por Python
                self.connection.sendfile(f, start, length)
        except OSError:
            self.close_connection = True
            return
        if start == 0 and not inline:
            ui_queue.put(("log", f"{self._device()} descargó {name}", "download"))

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/login":
            return self._login()
        if url.path == "/logout":
            with auth_lock:
                sessions.pop(self._session(), None)
            return self._redirect("/", f"{COOKIE}=; Path=/; Max-Age=0")
        if not self._authorized():
            return self._send(401, b"No autorizado")
        if url.path == "/api/text":
            length = int(self.headers.get("Content-Length", 0))
            if length > 5 * 1024 * 1024:
                return self._send(413, b"Demasiado grande")
            try:
                text = json.loads(self.rfile.read(length))["text"]
            except Exception:
                return self._send(400, b"JSON invalido")
            item = add_item(str(text), self._device())
            ui_queue.put(("text", item))
            return self._json({"ok": True})
        if url.path == "/api/steam/apply":
            started = apply_steam()
            return self._json({"ok": True, "started": started})
        self._send(404, b"No encontrado")

    def do_PUT(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if not self._authorized():
            return self._send(401, b"No autorizado")
        if url.path == "/api/rom":
            return self._put_rom(qs)
        if url.path != "/api/upload":
            return self._send(404, b"No encontrado")
        name = safe_name(qs.get("name", ["archivo"])[0])
        path = os.path.join(SHARE_DIR, name)
        error = self._receive(path)
        if error:
            return self._send(500, error.encode())
        file_origin[name] = self._device()
        bump()
        ui_queue.put(("file", name, self._device()))
        self._json({"ok": True, "name": name})

    def _put_rom(self, qs):
        system = qs.get("system", [""])[0]
        try:
            folder = roms.rom_folder(system)
        except ValueError as e:
            return self._send(400, str(e).encode())
        path = roms.rom_path(folder, qs.get("name", ["rom"])[0])
        error = self._receive(path)
        if error:
            return self._send(500, error.encode())
        label = roms.SYSTEM_BY_FOLDER[system][1]
        ui_queue.put(("log", f"{os.path.basename(path)} → {label}", "rom", f"ROM de {self._device()}"))
        result = {"ok": True, "path": path, "shortcut": False, "reason": ""}
        if qs.get("steam", ["0"])[0] == "1" and roms.needs_shortcut(path):
            entry = roms.build_shortcut(system, path)
            if entry:
                with steam_lock:
                    if on_shortcut:  # modo headless: el plugin de Decky lo añade en caliente
                        steam_state["status"] = "working"
                        steam_state["message"] = f"Añadiendo «{entry['appname']}» a Steam…"
                    else:
                        steam_state["pending"].append(entry)
                    steam_state["recent"] = ([entry["appname"]] + steam_state["recent"])[:8]
                if on_shortcut:
                    on_shortcut(entry)
                result["shortcut"] = True
            else:
                result["reason"] = f"no encuentro un emulador configurado para {label}"
        bump()
        ui_queue.put(("steam", None))
        self._json(result)

    def _receive(self, path):
        """Guarda el cuerpo de la petición en `path` (vía .part). Devuelve un mensaje de error o None."""
        length = int(self.headers.get("Content-Length", 0))
        tmp = path + ".part"
        buf = bytearray(4 * 1024 * 1024)
        view = memoryview(buf)
        try:
            with open(tmp, "wb", buffering=0) as f:
                remaining = length
                while remaining > 0:
                    n = self.rfile.readinto(view[:min(len(buf), remaining)])
                    if not n:
                        raise ConnectionError("conexion cortada")
                    f.write(view[:n])
                    remaining -= n
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            if os.path.exists(tmp):
                os.remove(tmp)
            return str(e) or "error"
        return None


BG, SURFACE, SURFACE2, BORDER = "#0e1319", "#161d26", "#1e2732", "#2b3644"
FG, MUTED, ACCENT, ACCENT_DARK, OK, WARN = "#e6edf3", "#8b98a5", "#1a9fff", "#1d4f7c", "#3ecf8e", "#f2b84b"
ON_ACCENT, BAD, HOVER = "#06121f", "#ff6b6b", "#1b2430"  # texto oscuro sobre el azul: el blanco no llega a 3:1
MONO = "Hack"

# Actividad: tipo -> (símbolo, fondo de la insignia, color del símbolo)
KINDS = {
    "text_in": ("↓", "#1d3550", "#8fd0ff"), "text_out": ("↑", "#1d3550", "#8fd0ff"),
    "file_in": ("↓", "#163a2c", OK), "file_out": ("↑", "#163a2c", OK),
    "download": ("↓", "#26303c", "#b7c2cd"), "login": ("●", "#26303c", "#b7c2cd"),
    "rom": ("▶", "#3a2f16", WARN), "steam": ("✔", "#3a2f16", WARN), "warn": ("!", "#3a1c1f", BAD),
}
MAX_ACTIVITY = 60


def setup_style(root):
    """Tema oscuro común a las apps de la Deck (igual que DeckSpace)."""
    import tkinter.font as tkfont
    global MONO
    root.configure(background=BG)
    for name in ("TkDefaultFont", "TkTextFont"):
        tkfont.nametofont(name).configure(size=11)
    base = tkfont.nametofont("TkDefaultFont").actual("family")
    MONO = next((f for f in ("Hack", "DejaVu Sans Mono") if f in tkfont.families()), "monospace")
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                 troughcolor=SURFACE2, fieldbackground=SURFACE2, insertcolor=FG, focuscolor=ACCENT)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=SURFACE)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Card.TLabel", background=SURFACE, foreground=FG)
    st.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED)
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Title.TLabel", font=(base, 18, "bold"))
    st.configure("H2.TLabel", background=SURFACE, foreground=MUTED, font=(base, 10, "bold"))
    st.configure("Pin.TLabel", background=SURFACE, foreground=ACCENT, font=(MONO, 30, "bold"))
    st.configure("TButton", background=SURFACE2, foreground=FG, borderwidth=0, padding=(12, 8), relief="flat",
                 width=-4)  # clam fuerza 11 caracteres de ancho mínimo; así cada botón mide lo que su texto
    st.map("TButton", background=[("disabled", SURFACE), ("pressed", BORDER), ("active", BORDER)],
           foreground=[("disabled", MUTED)])
    st.configure("Small.TButton", background=BORDER, padding=(10, 4))
    st.map("Small.TButton", background=[("pressed", "#3a4757"), ("active", "#3a4757")])
    st.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, font=(base, 11, "bold"))
    st.map("Accent.TButton", background=[("disabled", ACCENT_DARK), ("pressed", "#1580d0"), ("active", "#3aaeff")],
           foreground=[("disabled", "#9fb4c8")])
    st.configure("TEntry", fieldbackground=SURFACE2, foreground=FG, padding=6)
    st.configure("TCheckbutton", background=SURFACE, foreground="#b7c2cd", indicatorbackground=SURFACE2)
    st.map("TCheckbutton", background=[("active", SURFACE)], indicatorbackground=[("selected", ACCENT)])
    st.configure("Vertical.TScrollbar", background=BORDER, troughcolor=SURFACE, bordercolor=SURFACE,
                 arrowcolor=MUTED, lightcolor=BORDER, darkcolor=BORDER, gripcount=0, arrowsize=12)
    st.map("Vertical.TScrollbar", background=[("active", "#3a4757")])
    return base


class App:
    def __init__(self, root):
        self.root = root
        self.font = setup_style(root)
        root.title("LanShare")
        # Pensada para la pantalla de la Deck (1280x800); en un monitor grande no pasa de ese tamaño
        w = min(1240, root.winfo_screenwidth() - 40)
        h = min(800, root.winfo_screenheight() - 80)
        root.geometry(f"{w}x{h}")
        root.minsize(min(1000, w), min(620, h))
        f = self.font

        # --- cabecera: logo, estado y dispositivos conectados
        head = tk.Frame(root, background=BG, padx=24, pady=14)
        head.pack(fill="x")
        tk.Label(head, text="↗", background=ACCENT, foreground=ON_ACCENT, font=(f, 15, "bold"),
                 width=2).pack(side="left", ipady=2)
        ttk.Label(head, text="LanShare", style="Title.TLabel").pack(side="left", padx=(12, 14))
        tk.Label(head, text="●  Servidor activo", background="#11261d", foreground=OK, font=(f, 10, "bold"),
                 padx=12, pady=4).pack(side="left")
        self.devices = ttk.Label(head, style="Muted.TLabel")
        self.devices.pack(side="right")
        tk.Frame(root, background=SURFACE2, height=1).pack(fill="x")

        # --- pie: datos fijos y mensajes pasajeros
        foot = tk.Frame(root, background=BG, padx=24, pady=8)
        foot.pack(fill="x", side="bottom")
        tk.Frame(root, background=SURFACE2, height=1).pack(fill="x", side="bottom")
        ttk.Label(foot, text=f"Carpeta: {SHARE_DIR.replace(os.path.expanduser('~'), '~')}     Puerto {PORT}"
                             "     Cerrar la ventana apaga el servidor", style="Muted.TLabel",
                  font=(f, 9)).pack(side="left")
        self.status = ttk.Label(foot, foreground=OK, font=(f, 9, "bold"))
        self.status.pack(side="right")

        body = ttk.Frame(root, padding=(24, 18, 24, 16))
        body.pack(fill="both", expand=True)

        # --- columna izquierda: conectar un dispositivo
        aside = ttk.Frame(body, style="Card.TFrame", padding=20)
        aside.pack(side="left", fill="y")
        ttk.Label(aside, text="CONECTAR UN DISPOSITIVO", style="H2.TLabel").pack(anchor="w")
        top = ttk.Frame(aside, style="Card.TFrame")
        top.pack(fill="x", pady=(14, 14))
        qr_box = tk.Frame(top, background="#ffffff", padx=6, pady=6)
        qr_box.pack(side="left")
        self.qr_label = tk.Label(qr_box, background="#ffffff", borderwidth=0)
        self.qr_label.pack()
        pin = ttk.Frame(top, style="Card.TFrame")
        pin.pack(side="left", padx=(18, 0))
        ttk.Label(pin, text="Código de acceso", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Label(pin, text=f"{PIN[:3]} {PIN[3:]}", style="Pin.TLabel").pack(anchor="w")
        ttk.Label(pin, text="Nuevo en cada arranque", style="CardMuted.TLabel", font=(f, 9)).pack(anchor="w")

        self.urls = []
        for ip, label in local_ips():
            u = f"http://{ip}:{PORT}/"
            self.urls.append(u)
            row = tk.Frame(aside, background=SURFACE2, padx=10, pady=5)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text=label.upper(), width=6, anchor="w", background=SURFACE2,
                     foreground=OK if label == "Cable" else MUTED, font=(f, 9, "bold")).pack(side="left")
            tk.Label(row, text=f"{ip}:{PORT}", background=SURFACE2, foreground=FG,
                     font=(MONO, 10)).pack(side="left", padx=(4, 10))
            ttk.Button(row, text="Copiar", style="Small.TButton",
                       command=lambda u=u: self.copy(u, "URL copiada")).pack(side="right")
            ttk.Button(row, text="QR", style="Small.TButton",
                       command=lambda u=u: self.show_qr(u)).pack(side="right", padx=(0, 6))
        self.qr_caption = ttk.Label(aside, style="CardMuted.TLabel", font=(f, 9))
        self.qr_caption.pack(anchor="w", pady=(2, 0))

        # juegos pendientes de Steam (abajo del todo; solo visible si hay algo)
        self.steam_card = tk.Frame(aside, background="#0f2236", padx=14, pady=12)
        self.steam_title = tk.Label(self.steam_card, background="#0f2236", foreground=FG, font=(f, 11, "bold"),
                                    anchor="w", justify="left", wraplength=320)
        self.steam_title.pack(fill="x")
        self.steam_text = tk.Label(self.steam_card, background="#0f2236", foreground=MUTED, font=(f, 10),
                                   anchor="w", justify="left", wraplength=320)
        self.steam_text.pack(fill="x", pady=(4, 10))
        self.steam_btn = ttk.Button(self.steam_card, text="Añadir a Steam ahora", style="Accent.TButton",
                                    command=apply_steam)
        self.steam_btn.pack(fill="x")

        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True, padx=(20, 0))

        # --- enviar texto / archivos
        send = ttk.Frame(main, style="Card.TFrame", padding=18)
        send.pack(fill="x")
        hrow = ttk.Frame(send, style="Card.TFrame")
        hrow.pack(fill="x")
        ttk.Label(hrow, text="ENVIAR A LOS DISPOSITIVOS  ·  Ctrl+Intro", style="H2.TLabel").pack(side="left")
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(hrow, text="Copiar lo que llegue", variable=self.auto).pack(side="right")
        self.entry = tk.Text(send, height=3, wrap="word", background=SURFACE2, foreground=FG, insertbackground=FG,
                             relief="flat", highlightthickness=1, highlightbackground=BORDER,
                             highlightcolor=ACCENT, font=(f, 11), padx=10, pady=8)
        self.entry.pack(fill="x", pady=(10, 0))
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        btns = ttk.Frame(send, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Enviar texto", style="Accent.TButton", command=self.send).pack(side="left")
        ttk.Button(btns, text="Portapapeles", command=self.send_clipboard).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Compartir archivo…", command=self.share_file).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Abrir carpeta", command=lambda: self.open(SHARE_DIR)).pack(side="left", padx=(6, 0))

        # --- actividad: filas con insignia, título, detalle y hora
        act = ttk.Frame(main, style="Card.TFrame", padding=(18, 16, 8, 12))
        act.pack(fill="both", expand=True, pady=(16, 0))
        arow = ttk.Frame(act, style="Card.TFrame")
        arow.pack(fill="x", padx=(0, 10))
        ttk.Label(arow, text="ACTIVIDAD", style="H2.TLabel").pack(side="left")
        ttk.Label(arow, text="Toca un texto para copiarlo o un archivo para abrirlo", style="CardMuted.TLabel",
                  font=(f, 9)).pack(side="right")
        box = ttk.Frame(act, style="Card.TFrame")
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(box, background=SURFACE, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview)

        def set_bar(first, last):  # la barra solo aparece cuando la lista no cabe
            bar.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                bar.pack_forget()
            elif not bar.winfo_ismapped():
                bar.pack(side="right", fill="y", before=self.canvas)
        self.canvas.configure(yscrollcommand=set_bar)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows = tk.Frame(self.canvas, background=SURFACE)
        win = self.canvas.create_window(0, 0, window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width - 8))
        for seq, step in (("<Button-4>", -2), ("<Button-5>", 2)):
            self.canvas.bind_all(seq, lambda e, s=step: self.canvas.yview_scroll(s, "units"))
        self.empty = tk.Label(self.rows, text="Aún no hay actividad. Lo que llegue o envíes aparecerá aquí.",
                              background=SURFACE, foreground=MUTED, font=(f, 10), pady=16)
        self.empty.pack(anchor="w")
        self.activity = []

        self.show_qr(self.urls[0])
        self.update_devices()
        self.update_steam()
        root.after(200, self.poll)

    # ---------- piezas de la interfaz
    def update_devices(self):
        devs = connected_devices()
        if not devs:
            text = "Ningún dispositivo conectado"
        else:
            text = f"{len(devs)} {'dispositivo conectado' if len(devs) == 1 else 'dispositivos conectados'}: " \
                   + ", ".join(devs)
        self.devices.configure(text=text)

    def update_steam(self):
        snap = steam_snapshot()
        n = len(snap["pending"])
        if n or snap["message"]:
            self.steam_title.configure(text=(f"{n} {'juego pendiente' if n == 1 else 'juegos pendientes'} "
                                             "de añadir a Steam") if n else "Steam")
            detail = " · ".join(snap["pending"])
            if snap["message"]:
                detail = (detail + "\n" if detail else "") + snap["message"]
            self.steam_text.configure(text=detail, foreground={"error": BAD, "ok": OK}.get(snap["status"], MUTED))
            if n and snap["status"] != "working":
                self.steam_btn.pack(fill="x")
            else:
                self.steam_btn.pack_forget()
            if not self.steam_card.winfo_ismapped():
                self.steam_card.pack(side="bottom", fill="x")
        elif self.steam_card.winfo_ismapped():
            self.steam_card.pack_forget()

    def add_activity(self, kind, title, detail="", on_click=None):
        """Añade una fila arriba de la lista. on_click: acción al tocarla (copiar un texto, abrir un archivo)."""
        if self.empty.winfo_ismapped():
            self.empty.pack_forget()
        f = self.font
        glyph, badge_bg, badge_fg = KINDS.get(kind, ("•", "#26303c", "#b7c2cd"))
        row = tk.Frame(self.rows, background=SURFACE, padx=10, pady=7, cursor="hand2" if on_click else "")
        badge = tk.Label(row, text=glyph, background=badge_bg, foreground=badge_fg, font=(f, 12, "bold"),
                         width=2, height=1)
        badge.pack(side="left", ipady=3, ipadx=2)
        when = tk.Label(row, text=time.strftime("%H:%M"), background=SURFACE, foreground=MUTED, font=(MONO, 9))
        when.pack(side="right")
        txt = tk.Frame(row, background=SURFACE)
        txt.pack(side="left", fill="x", expand=True, padx=(12, 10))
        one_line = title.replace("\n", " ")
        t1 = tk.Label(txt, text=one_line[:110] + ("…" if len(one_line) > 110 else ""), background=SURFACE,
                      foreground=FG, font=(f, 11), anchor="w")
        t1.pack(fill="x")
        widgets = [row, txt, t1, when]
        if detail:
            t2 = tk.Label(txt, text=detail, background=SURFACE, foreground=MUTED, font=(f, 9), anchor="w")
            t2.pack(fill="x")
            widgets.append(t2)

        def paint(color):
            for w in widgets:
                w.configure(background=color)
        for w in widgets + [badge]:
            w.bind("<Enter>", lambda e: paint(HOVER))
            w.bind("<Leave>", lambda e: paint(SURFACE))
            if on_click:
                w.bind("<Button-1>", lambda e: on_click())
        if self.activity:  # lo más nuevo, arriba
            row.pack(fill="x", before=self.activity[0])
        else:
            row.pack(fill="x")
        self.activity.insert(0, row)
        for old in self.activity[MAX_ACTIVITY:]:
            old.destroy()
        del self.activity[MAX_ACTIVITY:]
        self.canvas.yview_moveto(0)

    def flash(self, msg, color=OK):
        self.status.configure(text=msg, foreground=color)
        if getattr(self, "_flash", None):
            self.root.after_cancel(self._flash)
        self._flash = self.root.after(4000, lambda: self.status.configure(text=""))

    def show_qr(self, url):
        try:
            png = subprocess.run(["qrencode", "-t", "PNG", "-s", "4", "-m", "1", "-o", "-", url],
                                 capture_output=True, check=True).stdout
            self.qr_img = tk.PhotoImage(data=base64.b64encode(png))
            self.qr_label.configure(image=self.qr_img)
            self.qr_caption.configure(text=f"El QR abre {url}")
        except (OSError, subprocess.CalledProcessError, tk.TclError):
            self.qr_caption.configure(text="(no se pudo generar el QR: falta qrencode)")

    # ---------- acciones
    def copy(self, text, msg="Copiado"):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.flash(msg)

    def open(self, target):
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_file(self, name):
        path = os.path.join(SHARE_DIR, name)
        if os.path.exists(path):
            self.open(path)
        else:
            self.flash(f"{name} ya no está en la carpeta", WARN)

    def _send_text(self, text):
        if text.strip():
            add_item(text, "Deck")
            self.add_activity("text_out", text, "Enviado desde la Deck",
                              lambda t=text: self.copy(t, "Texto copiado al portapapeles"))
            self.flash("Texto enviado")

    def send(self):
        self._send_text(self.entry.get("1.0", "end-1c"))
        self.entry.delete("1.0", "end")

    def send_clipboard(self):
        try:
            self._send_text(self.root.clipboard_get())
        except tk.TclError:
            messagebox.showinfo("LanShare", "El portapapeles está vacío o no contiene texto.")

    def share_file(self):
        paths = filedialog.askopenfilenames(title="Compartir archivos")
        for p in paths:
            name = safe_name(os.path.basename(p))
            dest = os.path.join(SHARE_DIR, name)
            try:
                os.link(p, dest)  # sin duplicar espacio si es el mismo disco
            except OSError:
                shutil.copy2(p, dest)
            file_origin[name] = "la Deck"
            self.add_activity("file_out", name, "Compartido desde la Deck", lambda n=name: self.open_file(n))
        if paths:
            bump()

    def poll(self):
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg[0] == "text":
                    item = msg[1]
                    detail = f"Texto de {item['from']}"
                    if self.auto.get():
                        self.copy(item["text"], f"Texto de {item['from']} copiado al portapapeles")
                        detail += " · copiado al portapapeles"
                    self.add_activity("text_in", item["text"], detail,
                                      lambda t=item["text"]: self.copy(t, "Texto copiado al portapapeles"))
                elif msg[0] == "file":
                    name, who = msg[1], msg[2]
                    try:
                        size = f"  ·  {fmt_size(os.path.getsize(os.path.join(SHARE_DIR, name)))}"
                    except OSError:
                        size = ""
                    self.add_activity("file_in", name, f"Archivo de {who}{size}", lambda n=name: self.open_file(n))
                    self.flash(f"Recibido: {name}")
                elif msg[0] == "log":
                    kind = msg[2] if len(msg) > 2 else ""
                    self.add_activity(kind, msg[1], msg[3] if len(msg) > 3 else "")
                elif msg[0] == "steam":
                    self.update_steam()
                elif msg[0] == "devices":
                    self.update_devices()
        except queue.Empty:
            pass
        self.root.after(200, self.poll)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def headless():
    """Servidor sin ventana para el plugin de Decky (modo juego).

    Protocolo por líneas JSON. Salida (stdout): {"type": "ready"|"text"|"file"|"log"|"shortcut"|"error", ...}.
    Entrada (stdin): {"cmd": "shortcut_result", "appname", "ok", "error"} o {"cmd": "quit"}.
    Los ROMs con "Añadir a Steam" no esperan a reiniciar Steam: se envían como "shortcut" y el plugin
    los añade con SteamClient.Apps.AddShortcut.
    """
    import signal
    import sys
    global on_shortcut
    out_lock = threading.Lock()

    def emit(kind, **data):
        with out_lock:
            sys.stdout.write(json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    os.makedirs(SHARE_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        emit("error", message=f"No se pudo abrir el puerto {PORT}: {e}. ¿Está abierta la app de escritorio?")
        sys.exit(1)
    server.daemon_threads = True
    on_shortcut = lambda entry: emit("shortcut", entry=entry)  # noqa: E731

    def forward():
        while True:
            msg = ui_queue.get()
            if msg[0] == "text":
                emit("text", item=msg[1])
            elif msg[0] == "file":
                emit("file", name=msg[1], client=msg[2])
            elif msg[0] == "log":
                emit("log", message=msg[1])

    def commands():
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if cmd.get("cmd") == "quit":
                break
            if cmd.get("cmd") == "shortcut_result":
                name = cmd.get("appname", "?")
                with steam_lock:
                    if cmd.get("ok"):
                        steam_state["status"], steam_state["message"] = "ok", f"✔ «{name}» añadido a Steam"
                    else:
                        steam_state["status"] = "error"
                        steam_state["message"] = f"✖ «{name}»: {cmd.get('error') or 'no se pudo añadir'}"
                bump()
        server.shutdown()  # stdin cerrado o "quit": el plugin se ha ido

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    threading.Thread(target=forward, daemon=True).start()
    threading.Thread(target=commands, daemon=True).start()
    emit("ready", pin=PIN, port=PORT, share_dir=SHARE_DIR,
         urls=[{"url": f"http://{ip}:{PORT}/", "label": label} for ip, label in local_ips()])
    server.serve_forever()


def main():
    os.makedirs(SHARE_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("LanShare", f"No se pudo abrir el puerto {PORT}:\n{e}\n\n¿Ya está abierta la app?")
        return
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    finally:
        server.shutdown()


if __name__ == "__main__":
    import sys
    headless() if "--headless" in sys.argv[1:] else main()
