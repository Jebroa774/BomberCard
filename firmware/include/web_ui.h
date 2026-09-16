#pragma once

#include <Arduino.h>

namespace pocketlab {

inline constexpr char WEB_UI[] PROGMEM = R"HTML(<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#101827">
  <title>BomberCard</title>
  <style>
    :root{color-scheme:dark;--bg:#0a0f1b;--panel:#111a2a;--line:#25334a;--text:#ecf3ff;--muted:#91a2bd;--accent:#41d3a2;--warn:#ffca68;--bad:#ff7185}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#172b43 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
    header,main{width:min(1120px,calc(100% - 28px));margin:auto}header{display:flex;align-items:center;justify-content:space-between;padding:24px 0 12px}h1{font-size:clamp(23px,5vw,38px);margin:0;letter-spacing:-.04em}h2{font-size:17px;margin:0 0 16px}.subtitle,.muted{color:var(--muted)}
    .pill{border:1px solid var(--line);border-radius:99px;padding:7px 11px;background:#0b1422}.pill.ok{color:var(--accent);border-color:#266f5e}.pill.bad{color:var(--bad);border-color:#753242}
    main{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;padding:14px 0 42px}.card{grid-column:span 4;background:linear-gradient(145deg,#131e30,#0f1725);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 18px 48px #0004}.wide{grid-column:span 8}.full{grid-column:1/-1}
    .metric{font-size:32px;font-weight:720;letter-spacing:-.04em}.row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 0}.stack{display:grid;gap:8px}.tag{font:12px ui-monospace,monospace;padding:4px 7px;border-radius:6px;background:#19263a;color:#b9cae3}.safe{padding:10px;border-radius:10px;background:#14281f;color:#9de7cb;border:1px solid #285542}
    button,.button,input{border:1px solid var(--line);border-radius:10px;background:#18253a;color:var(--text);padding:9px 12px;font:inherit}button,.button{cursor:pointer;text-decoration:none}button:hover,.button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:not-allowed}.primary{background:#1e765d;border-color:#349779}.danger{color:#ffb4bf}input[type=file]{width:100%}
    table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;border-bottom:1px solid var(--line);padding:9px 6px;overflow-wrap:anywhere}th{color:var(--muted);font-weight:600}.actions{display:flex;gap:7px;flex-wrap:wrap}.notice{min-height:22px;color:var(--warn)}
    @media(max-width:850px){.card,.wide{grid-column:span 6}}@media(max-width:580px){header{align-items:flex-start;gap:12px}.card,.wide{grid-column:1/-1}.metric{font-size:27px}}
  </style>
</head>
<body>
  <header><div><h1>BomberCard</h1><div class="subtitle">Lokales Hardware-Dashboard</div></div><div id="connection" class="pill">Verbinde …</div></header>
  <main>
    <section class="card"><h2>Gerät</h2><div class="metric" id="uptime">–</div><div class="muted">Laufzeit</div><div class="row"><span>Firmware</span><span class="tag" id="version">–</span></div><div class="row"><span>Freier Heap</span><span id="heap">–</span></div></section>
    <section class="card wide"><h2>125-kHz LF-RFID</h2><div class="stack"><div class="row"><span>Versorgung</span><span id="lfPower">aus</span></div><div class="row"><span>HTRC110-Verbindung</span><span id="lfTransport">–</span></div><div class="row"><span>Antenne</span><span id="lfAntenna">–</span></div><div class="row"><span>Phase / Abtastwert</span><span class="tag" id="lfPhase">–</span></div></div><div class="actions"><button class="primary" id="lfOn">Einschalten</button><button id="lfTest">Selbsttest</button><button id="lfOff">Ausschalten</button></div><div class="muted">Der Selbsttest prüft Interface, Phasenmessung und Antennenfehler. Tag-Protokolle werden danach einzeln ergänzt.</div><div id="lfNotice" class="notice"></div></section>
    <section class="card wide"><h2>Hardware</h2><div class="row"><span>microSD</span><span id="sd">–</span></div><div class="row"><span>13,56-MHz NFC</span><span id="nfc">–</span></div><div class="row"><span>Sub-GHz</span><span id="subghz">–</span></div><div class="row"><span>I/O-Expander</span><span id="ioexp">–</span></div><div class="row"><span>Platinentemperatur</span><span id="boardTemp">–</span></div><div class="safe">Sub-GHz-TX und freie GPIO-Ausgänge bleiben in diesem Build gesperrt.</div></section>
    <section class="card"><h2>Sicherheit & Kopplung</h2><div class="row"><span>ATECC608C</span><span id="atecc">–</span></div><div class="row"><span>PAIR-Taste</span><span id="pairButton">–</span></div><div class="row"><span>Kopplungsfenster</span><span id="pairWindow">geschlossen</span></div><button class="primary" id="pairArm">PAIR halten + 60 s öffnen</button><div class="muted">Das Secure Element ist im Entwicklungsstand absichtlich noch nicht irreversibel provisioniert.</div><div id="pairNotice" class="notice"></div></section>
    <section class="card full"><h2>IR-Fernbedienung (NEC)</h2><div class="actions"><label>Adresse <input id="irAddress" type="number" min="0" max="255" value="0"></label><label>Befehl <input id="irCommand" type="number" min="0" max="255" value="0"></label><label>Wiederholungen <input id="irRepeats" type="number" min="0" max="2" value="0"></label><button class="primary" id="irSend">Senden</button></div><div class="muted">Gepulste 38-kHz-Ausgabe über alle drei IR-LEDs; maximal zwei Wiederholungen.</div><div id="irNotice" class="notice"></div></section>
    <section class="card full"><h2>Dateien auf microSD</h2><div class="actions"><button data-path="/" class="browse">Root</button><button data-path="/uploads" class="browse">Uploads</button><button id="remount">SD neu einbinden</button></div><div class="row"><span id="filePath" class="tag">/</span><span id="storageUsage" class="muted">–</span></div><table><thead><tr><th>Datei</th><th>Größe</th><th>Aktion</th></tr></thead><tbody id="files"><tr><td colspan="3" class="muted">Wird geladen …</td></tr></tbody></table><form id="uploadForm" class="row"><input id="uploadFile" type="file" required><button type="submit">Hochladen</button></form><div id="fileNotice" class="notice"></div></section>
  </main>
<script>
const $=id=>document.getElementById(id);let token='',currentPath='/',ws,retry,wsPort=81;
const text=(id,value)=>$(id).textContent=value;
const humanBytes=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KiB':(n/1048576).toFixed(1)+' MiB';
const duration=ms=>{const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return `${h}h ${m}m ${s%60}s`};
async function request(path,options={}){options.headers={...(options.headers||{}),'X-BomberCard-Token':token};const r=await fetch(path,options);const data=await r.json().catch(()=>({ok:false,error:'invalid_response'}));if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}
function yesNo(v){return v?'bereit':'nicht erkannt'}
function render(s){
  $('connection').className='pill ok';text('connection','Live');text('uptime',duration(s.uptimeMs));text('version',s.firmware.version);text('heap',humanBytes(s.freeHeap));
  const h=s.hardware,lf=h.lfRfid||{},sec=h.security||{},bt=h.boardTemperature||{},st=s.storage;text('lfPower',lf.power?'ein':'aus');text('lfTransport',lf.transport?'bereit':'nicht bereit');text('lfAntenna',lf.power?(lf.antennaFail?'Fehler / Spule prüfen':'OK'):'aus');text('lfPhase',lf.phase===255?'–':`${lf.phase} / ${lf.samplingTime}`);text('sd',st.mounted?'eingebunden':'nicht verfügbar');text('nfc',yesNo(h.nfc));text('subghz',yesNo(h.subGhz));text('ioexp',yesNo(h.ioExpander));text('boardTemp',bt.valid?`${Number(bt.celsius).toFixed(1)} °C${bt.overTemperature?' / Abschaltung':''}`:(bt.overTemperature?'Sensorfehler / Abschaltung':'nicht verfügbar'));text('atecc',yesNo(sec.atecc608c));text('pairButton',sec.pairButton?'gedrückt':'frei');text('pairWindow',sec.pairingWindow?'offen':'geschlossen');text('storageUsage',st.mounted?`${humanBytes(st.usedBytes)} / ${humanBytes(st.totalBytes)}`:'keine Karte');$('lfTest').disabled=!lf.power;
}
async function refresh(){try{render(await request('/api/status'))}catch(e){$('connection').className='pill bad';text('connection','Offline')}}
function connect(){ws=new WebSocket(`ws://${location.hostname}:${wsPort}/`);ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.type==='status')render(m)}catch{}};ws.onclose=()=>{clearTimeout(retry);retry=setTimeout(connect,1800);$('connection').className='pill bad';text('connection','Verbindung weg')};ws.onerror=()=>ws.close()}
async function mutate(path,notice){try{const r=await request(path,{method:'POST'});text(notice,r.message||'OK');await refresh();return r}catch(e){text(notice,e.message)}}
async function loadFiles(path=currentPath){currentPath=path;try{const d=await request('/api/files?path='+encodeURIComponent(path));text('filePath',d.path);const body=$('files');body.replaceChildren();if(!d.entries.length){const tr=body.insertRow();const td=tr.insertCell();td.colSpan=3;td.className='muted';td.textContent='Ordner ist leer.'}for(const f of d.entries){const tr=body.insertRow(),name=tr.insertCell(),size=tr.insertCell(),actions=tr.insertCell();name.textContent=f.name;size.textContent=f.directory?'Ordner':humanBytes(f.size);const wrap=document.createElement('div');wrap.className='actions';if(f.directory){const b=document.createElement('button');b.textContent='Öffnen';b.onclick=()=>loadFiles(f.name);wrap.append(b)}else{const a=document.createElement('a');a.className='button';a.textContent='Download';a.href='/api/file?path='+encodeURIComponent(f.name);wrap.append(a)}const del=document.createElement('button');del.className='danger';del.textContent='Löschen';del.onclick=async()=>{if(!confirm(`${f.name} löschen?`))return;try{await request('/api/file?path='+encodeURIComponent(f.name),{method:'DELETE'});loadFiles()}catch(e){text('fileNotice',e.message)}};wrap.append(del);actions.append(wrap)}}catch(e){text('fileNotice',e.message)}}
async function boot(){const c=await fetch('/api/config').then(r=>r.json());token=c.token;wsPort=c.websocketPort||81;connect();await refresh();await loadFiles('/')}
$('lfOn').onclick=()=>mutate('/api/lf/power?enabled=1','lfNotice');$('lfOff').onclick=()=>mutate('/api/lf/power?enabled=0','lfNotice');$('lfTest').onclick=()=>mutate('/api/lf/diagnose','lfNotice');$('pairArm').onclick=()=>mutate('/api/security/pairing-window','pairNotice');$('remount').onclick=async()=>{await mutate('/api/sd/remount','fileNotice');loadFiles(currentPath)};document.querySelectorAll('.browse').forEach(b=>b.onclick=()=>loadFiles(b.dataset.path));
$('irSend').onclick=()=>mutate('/api/ir/tx?address='+encodeURIComponent($('irAddress').value)+'&command='+encodeURIComponent($('irCommand').value)+'&repeats='+encodeURIComponent($('irRepeats').value),'irNotice');
$('uploadForm').onsubmit=async e=>{e.preventDefault();const file=$('uploadFile').files[0];if(!file)return;const data=new FormData();data.append('file',file);try{await request('/api/upload?path='+encodeURIComponent('/uploads/'+file.name),{method:'POST',body:data});text('fileNotice','Upload abgeschlossen.');loadFiles('/uploads')}catch(err){text('fileNotice',err.message)}};
boot().catch(e=>{text('connection',e.message);$('connection').className='pill bad'});setInterval(refresh,5000);
</script>
</body></html>)HTML";

}  // namespace pocketlab
