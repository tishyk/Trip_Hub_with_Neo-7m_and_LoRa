#include "web_server.h"
#include "config.h"
#include "chat_log.h"
#include "lora_radio.h"
#include "net.h"
#include "trip_storage.h"
#include "presence.h"
#include <Arduino.h>
#include <WebServer.h>
#include <math.h>

namespace {
WebServer server(80);

const char* COOKIE_HEADER_KEYS[] = {"Cookie"};

const char LOGIN_TOP[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoRa Chat - sign in</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:360px;margin:3rem auto;padding:1rem;color:#222}
h1{font-size:1.2rem;margin:0 0 1rem}
form{display:flex;flex-direction:column;gap:.6rem}
label{font-size:.9rem;color:#444}
input{padding:.6rem;border:1px solid #ccc;border-radius:6px;font-size:1rem}
button{padding:.6rem;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:1rem;cursor:pointer}
button:active{background:#1d4ed8}
.err{margin-top:.8rem;padding:.6rem;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;border-radius:6px}
small{color:#888;display:block;margin-top:1rem;line-height:1.4}
</style></head>
<body>
<h1>LoRa Chat - sign in</h1>
<form method="POST" action="/auth">
<label for="id">Device ID</label>
<input id="id" name="id" autocomplete="off" autocapitalize="off" spellcheck="false" autofocus required>
<button type="submit">Enter</button>
</form>
)HTML";

const char LOGIN_BOTTOM[] PROGMEM = R"HTML(<small>You must know the device ID of the radio you want to use.</small>
</body></html>)HTML";

const char CHAT_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoRa Chat</title>
<style>
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font-family:system-ui,-apple-system,sans-serif;color:#222;background:#f7f7f8;display:flex;flex-direction:column;max-width:520px;margin:0 auto;padding:.5rem}
.bar{display:flex;justify-content:space-between;align-items:center;padding:.4rem .2rem .6rem}
.bar h1{font-size:1.05rem;margin:0}
.bar a{color:#2563eb;text-decoration:none;font-size:.85rem}
#list{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:.4rem;padding:.2rem;background:#fff;border:1px solid #e5e7eb;border-radius:8px}
.empty{color:#888;text-align:center;padding:2rem 1rem;font-size:.9rem}
.msg{padding:.5rem 1.6rem .5rem .7rem;border-radius:10px;max-width:80%;word-wrap:break-word;position:relative}
.msg .meta{font-size:.72rem;color:#666;margin-top:.2rem}
.menu-btn{position:absolute;top:.1rem;right:.25rem;color:#666;cursor:pointer;font-size:1.1rem;background:none;border:0;padding:.1rem .35rem;line-height:1;border-radius:4px}
.menu-btn:hover{background:rgba(0,0,0,.06);color:#222}
.popover{position:absolute;top:1.65rem;right:.25rem;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.12);z-index:10;display:none;flex-direction:column;min-width:8rem;overflow:hidden}
.popover.open{display:flex}
.popover button{background:#fff;border:0;padding:.55rem .85rem;text-align:left;cursor:pointer;font-size:.9rem;color:#222;font-family:inherit}
.popover button:hover{background:#f3f4f6}
.popover button.danger{color:#b91c1c}
.popover button.danger:hover{background:#fef2f2}
.rx{background:#dbeafe;align-self:flex-start;border:1px solid #bfdbfe}
.tx{background:#dcfce7;align-self:flex-end;border:1px solid #bbf7d0}
.tx.unsent{background:#fde047;border:1px solid #ca8a04;color:#422006}
.toast{position:fixed;bottom:5rem;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:.45rem .9rem;border-radius:6px;font-size:.85rem;opacity:0;transition:opacity .2s;pointer-events:none;z-index:100}
.toast.show{opacity:1}
.input-row{display:flex;gap:.4rem;align-items:center;padding:.6rem .2rem .2rem}
#input{flex:1;padding:.55rem;border:1px solid #ccc;border-radius:8px;font-size:1rem;font-family:inherit}
#send{padding:.55rem 1rem;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:1rem;cursor:pointer}
#send:disabled{background:#9ca3af;cursor:not-allowed}
.counter{font-size:.7rem;color:#888;text-align:right;padding:0 .4rem}
.counter.over{color:#b91c1c}
.banner{margin:0 0 .4rem;padding:.4rem .6rem;background:#fef9c3;border:1px solid #fde68a;border-radius:6px;font-size:.85rem;color:#78350f}
.devs{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;padding:.1rem .2rem .5rem;font-size:.75rem;color:#888}
.devchip{padding:.1rem .5rem;border-radius:10px;border:1px solid #d1d5db}
.devchip.on{background:#dcfce7;border-color:#86efac;color:#166534}
.devchip.off{background:#f3f4f6;color:#9ca3af}
.sig{margin-left:.35rem;font-weight:600;opacity:.85}
</style></head>
<body>
<div class="bar"><h1>LoRa Chat - )HTML";

// Title splits here so we can inject the runtime g_deviceId between
// the two PROGMEM halves. The post-title chunk picks up exactly where
// the pre-title chunk left off.
const char CHAT_HTML_POST[] PROGMEM = R"HTML(</h1><a href="/logout">Sign out</a></div>
<div id="devs" class="devs"></div>
<div id="banner" class="banner" hidden></div>
<div id="list"><div class="empty">No messages yet.</div></div>
<div class="input-row">
<input id="input" placeholder="Type a message..." autocomplete="off">
<button id="send" type="button">Send</button>
</div>
<div class="counter"><span id="count">0</span>/220 B</div>
<script>
let lastId = 0;
const list = document.getElementById('list');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const countEl = document.getElementById('count');
const counter = countEl.parentElement;
const banner = document.getElementById('banner');

function fmtTime(ts){
  if(!ts) return '';
  const d = new Date(ts*1000);
  return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function escHtml(s){
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function renderMsg(m){
  const div = document.createElement('div');
  let cls = 'msg ' + (m.dir === 'rx' ? 'rx' : 'tx');
  if (m.dir === 'tx' && m.sent === false) cls += ' unsent';
  div.className = cls;
  div.dataset.id = m.id;
  div.dataset.text = m.text;
  let meta = fmtTime(m.ts);
  if (m.dir === 'rx') {
    if (typeof m.rssi === 'number') meta += ' &middot; ' + m.rssi + ' dBm';
    if (typeof m.snr === 'number') meta += ' SNR ' + m.snr.toFixed(1);
  } else {
    meta += ' &middot; ' + (m.sent ? 'sent' : 'failed');
  }
  div.innerHTML =
    '<button class="menu-btn" type="button" title="options">&#x22EF;</button>' +
    '<div class="popover">' +
      '<button type="button" class="act-reply">Reply</button>' +
      '<button type="button" class="act-copy">Copy</button>' +
      '<button type="button" class="act-delete danger">Delete</button>' +
    '</div>' +
    (m.dir === 'rx' ? '&larr; ' : '&rarr; ') + escHtml(m.text) +
    '<div class="meta">' + meta + '</div>';
  return div;
}
function closeMenus(){
  document.querySelectorAll('.popover.open').forEach(p => p.classList.remove('open'));
}
function showToast(msg){
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1300);
}
// LoRa packets cap at ~232 B plaintext after AES+PKCS7. Wire format is
// 'CHAT:<sender>:<body>'. Reserve ~12 B for 'CHAT:' + headroom, plus 16 B
// for a worst-case 15-char sender + ':' separator, leaving 204 B for body.
const MAX_BYTES = 204;
function utf8Bytes(s){ return new TextEncoder().encode(s).length; }
function updateCounter(){
  const n = utf8Bytes(input.value);
  countEl.textContent = n;
  counter.classList.toggle('over', n > MAX_BYTES);
}
function replyTo(text){
  const tail = text.length <= 10 ? text : text.slice(-10);
  input.value = '> ' + tail + '... ';
  updateCounter();
  input.focus();
  const end = input.value.length;
  input.setSelectionRange(end, end);
}
async function copyText(text){
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch(e) {}
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { ok = document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
  }
  showToast(ok ? 'Copied' : 'Copy blocked - long-press text instead');
}
async function poll(){
  try {
    const r = await fetch('/api/messages?since=' + lastId);
    if (!r.ok) {
      if (r.status === 401) location.reload();
      return;
    }
    const msgs = await r.json();
    if (msgs.length === 0) return;
    const empty = list.querySelector('.empty');
    if (empty) empty.remove();
    const nearBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 60;
    for (const m of msgs) {
      list.appendChild(renderMsg(m));
      if (m.id > lastId) lastId = m.id;
    }
    if (nearBottom) list.scrollTop = list.scrollHeight;
    banner.hidden = true;
  } catch (e) {
    banner.textContent = 'Lost connection - retrying...';
    banner.hidden = false;
  }
}
async function send(){
  const text = input.value.trim();
  if (!text) return;
  const nbytes = utf8Bytes(text);
  if (nbytes > MAX_BYTES) {
    banner.textContent = 'Too long: ' + nbytes + ' bytes (max ' + MAX_BYTES + ')';
    banner.hidden = false;
    return;
  }
  sendBtn.disabled = true;
  try {
    const r = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type':'application/x-www-form-urlencoded'},
      body: 'text=' + encodeURIComponent(text)
    });
    if (r.ok) {
      input.value = '';
      countEl.textContent = '0';
      counter.classList.remove('over');
      poll();
    } else if (r.status === 401) {
      location.reload();
    } else {
      banner.textContent = 'Send failed (' + r.status + ')';
      banner.hidden = false;
    }
  } catch (e) {
    banner.textContent = 'Send failed - check connection';
    banner.hidden = false;
  } finally {
    sendBtn.disabled = false;
  }
}
async function delMsg(id){
  try {
    await fetch('/api/delete', {
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'id=' + encodeURIComponent(id)
    });
    const el = list.querySelector('.msg[data-id="'+id+'"]');
    if (el) el.remove();
    if (list.children.length === 0) {
      const e = document.createElement('div'); e.className='empty'; e.textContent='No messages yet.';
      list.appendChild(e);
    }
  } catch (e) {}
}
list.addEventListener('click', e => {
  if (e.target.matches('.menu-btn')) {
    e.stopPropagation();
    const pop = e.target.nextElementSibling;
    const wasOpen = pop.classList.contains('open');
    closeMenus();
    if (!wasOpen) pop.classList.add('open');
    return;
  }
  const msgDiv = e.target.closest('.msg');
  if (!msgDiv) return;
  const text = msgDiv.dataset.text || '';
  const id = msgDiv.dataset.id;
  if (e.target.matches('.act-reply')) {
    replyTo(text);
    closeMenus();
  } else if (e.target.matches('.act-copy')) {
    copyText(text);
    closeMenus();
  } else if (e.target.matches('.act-delete')) {
    delMsg(id);
    closeMenus();
  }
});
document.addEventListener('click', e => {
  if (!e.target.closest('.popover') && !e.target.matches('.menu-btn')) closeMenus();
});
input.addEventListener('input', updateCounter);
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
sendBtn.addEventListener('click', send);

// ----- Device liveness strip -----
// Map LoRa RSSI (dBm) to a 1..10 signal score: -120 dBm ~ 1, -50 dBm ~ 10.
function sigScore(rssi){
  if (rssi === undefined || rssi === null || rssi <= -32000) return null;
  const q = Math.round((rssi + 120) / 70 * 9) + 1;
  return Math.max(1, Math.min(10, q));
}
async function pollDevices(){
  try {
    const r = await fetch('/api/devices');
    if (!r.ok) return;
    const devs = await r.json();
    const host = document.getElementById('devs');
    if (!host) return;
    host.innerHTML = '';
    const lbl = document.createElement('span');
    lbl.textContent = 'Devices:';
    host.appendChild(lbl);
    if (!devs.length) { host.appendChild(document.createTextNode(' —')); return; }
    for (const d of devs) {
      const el = document.createElement('span');
      el.className = 'devchip ' + (d.online ? 'on' : 'off');
      let label = d.name + (d.self ? ' (this)' : '');
      const score = d.self ? null : sigScore(d.rssi);
      if (score !== null) {
        label += ' ';
        const sig = document.createElement('span');
        sig.className = 'sig';
        sig.textContent = score + '/10';
        el.textContent = label;
        el.appendChild(sig);
      } else {
        el.textContent = label;
      }
      const age = d.age < 60 ? d.age + 's' : Math.floor(d.age/60) + 'm';
      el.title = (d.online ? 'online' : 'offline') + ' · last seen ' + age + ' ago' +
                 (score !== null ? ' · RSSI ' + d.rssi + ' dBm' : '');
      host.appendChild(el);
    }
  } catch (e) {}
}

poll();
setInterval(poll, 3000);
pollDevices();
setInterval(pollDevices, 15000);
</script>
</body></html>)HTML";

bool readAuthCookie(String& out) {
  String cookie = server.header("Cookie");
  if (cookie.length() == 0) return false;
  const String key = "lora_auth=";
  int pos = cookie.indexOf(key);
  if (pos < 0) return false;
  pos += key.length();
  int end = cookie.indexOf(';', pos);
  out = (end < 0) ? cookie.substring(pos) : cookie.substring(pos, end);
  out.trim();
  return out.length() > 0;
}

bool isAuthed() {
  String token;
  if (!readAuthCookie(token)) return false;
  return token == g_deviceId;
}

bool requireAuthApi() {
  if (isAuthed()) {
    Net.noteActivity();
    return true;
  }
  server.send(401, "application/json", "{\"error\":\"unauth\"}");
  return false;
}

void serveLogin(bool showError) {
  server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  server.send(200, "text/html", "");
  server.sendContent_P(LOGIN_TOP);
  if (showError) {
    server.sendContent(F("<div class='err'>Wrong device ID</div>"));
  }
  server.sendContent_P(LOGIN_BOTTOM);
}

String escapeJson(const char* s) {
  String out;
  for (const char* p = s; *p; p++) {
    char c = *p;
    if (c == '"')      out += "\\\"";
    else if (c == '\\') out += "\\\\";
    else if (c == '\n') out += "\\n";
    else if (c == '\r') out += "\\r";
    else if (c == '\t') out += "\\t";
    else if ((unsigned char)c < 0x20) {
      char buf[8];
      snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)c);
      out += buf;
    } else {
      out += c;
    }
  }
  return out;
}

String msgToJson(const ChatMessage& m) {
  String s = "{\"id\":" + String(m.id);
  s += ",\"dir\":\"";
  s += m.incoming ? "rx" : "tx";
  s += "\",\"text\":\"";
  s += escapeJson(m.text);
  s += "\",\"ts\":" + String(m.timestamp);
  if (m.incoming) {
    if (m.rssi != INT16_MIN) s += ",\"rssi\":" + String(m.rssi);
    if (!isnan(m.snr))       s += ",\"snr\":" + String(m.snr, 1);
  } else {
    s += ",\"sent\":";
    s += m.sent ? "true" : "false";
  }
  s += "}";
  return s;
}

void handleRoot() {
  Net.noteActivity();
  if (isAuthed()) {
    // Streamed in three parts so we can inject the runtime device id
    // between the two static HTML halves without rebuilding the whole
    // page in RAM.
    server.setContentLength(CONTENT_LENGTH_UNKNOWN);
    server.send(200, "text/html", "");
    server.sendContent_P(CHAT_HTML);
    server.sendContent(g_deviceId);
    server.sendContent_P(CHAT_HTML_POST);
  } else {
    serveLogin(false);
  }
}

void handleAuth() {
  Net.noteActivity();
  String id = server.arg("id");
  id.trim();
  if (id == g_deviceId) {
    server.sendHeader("Set-Cookie",
                      String("lora_auth=") + g_deviceId +
                      "; Path=/; Max-Age=604800");
    server.sendHeader("Location", "/", true);
    server.send(302, "text/plain", "");
  } else {
    serveLogin(true);
  }
}

void handleLogout() {
  Net.noteActivity();
  server.sendHeader("Set-Cookie", "lora_auth=; Path=/; Max-Age=0");
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

// Static heap-side scratch — avoids putting 110KB on the 8KB task stack
// and avoids ever holding two copies of the buffer at once.
static ChatMessage g_apiBuf[CHAT_MAX];

void streamMessagesJson(uint32_t since) {
  size_t n = Chat.getSince(since, g_apiBuf, CHAT_MAX);
  server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  server.send(200, "application/json", "[");
  for (size_t i = 0; i < n; i++) {
    if (i > 0) server.sendContent(",");
    server.sendContent(msgToJson(g_apiBuf[i]));
  }
  server.sendContent("]");
}

void handleApiMessages() {
  if (!requireAuthApi()) return;
  uint32_t since = 0;
  if (server.hasArg("since")) since = (uint32_t)server.arg("since").toInt();
  streamMessagesJson(since);
}

void handleApiSend() {
  if (!requireAuthApi()) return;
  String text = server.arg("text");
  text.trim();
  if (text.length() == 0) {
    server.send(400, "application/json", "{\"error\":\"empty\"}");
    return;
  }
  // Byte-based limit. Wire format: CHAT:<sender>:<body>. Reserve up to
  // 16 B for sender+':' on top of CHAT: + AES headroom → 204 B body.
  if (text.length() > 204) {
    server.send(413, "application/json", "{\"error\":\"too long\"}");
    return;
  }

  // Chat log stores just the user text (no prefix); the wire format adds
  // 'CHAT:<sender>:' so receivers can both attribute and distinguish
  // chat from protocol traffic.
  uint32_t id = Chat.addTx(text.c_str(), false);
  String wire = String("CHAT:") + g_deviceId + ":" + text;
  bool ok = Radio.sendEncrypted(wire.c_str());
  Chat.markSent(id, ok);
  Chat.flushIfNeeded();

  // Return the just-added message so the client can render it
  // without an extra poll.
  streamMessagesJson(id - 1);
}

void handleApiDelete() {
  if (!requireAuthApi()) return;
  if (!server.hasArg("id")) {
    server.send(400, "application/json", "{\"error\":\"id required\"}");
    return;
  }
  uint32_t id = (uint32_t)server.arg("id").toInt();
  bool ok = Chat.remove(id);
  Chat.flushIfNeeded();
  server.send(ok ? 200 : 404, "application/json",
              ok ? "{\"ok\":true}" : "{\"error\":\"not found\"}");
}

// Read-only trip inspection.
// GET /api/trips       -> [{"id","npts","sync"}, ...]
// GET /api/trip?id=T.. -> {"id","npts","meta":<raw .json>,"fixes":[[ts,lat,lon,alt,spd],..]}
void handleApiTrips() {
  if (!requireAuthApi()) return;
  char ids[trip_storage::MAX_UNSENT][trip_storage::TRIP_ID_MAX];
  size_t n = trip_storage::getUnsentTrips(ids, trip_storage::MAX_UNSENT);
  server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  server.send(200, "application/json", "[");
  for (size_t i = 0; i < n; i++) {
    if (i > 0) server.sendContent(",");
    char row[64];
    snprintf(row, sizeof(row), "{\"id\":\"%s\",\"npts\":%u,\"sync\":%u}",
             ids[i], (unsigned)trip_storage::tripNpts(ids[i]),
             (unsigned)trip_storage::syncStatus(ids[i]));
    server.sendContent(row);
  }
  server.sendContent("]");
}

void handleApiTrip() {
  if (!requireAuthApi()) return;
  if (!server.hasArg("id")) {
    server.send(400, "application/json", "{\"error\":\"id required\"}");
    return;
  }
  String idArg = server.arg("id");
  const char* id = idArg.c_str();
  size_t npts = trip_storage::tripNpts(id);
  char meta[300];
  size_t mlen = trip_storage::readMetaJson(id, meta, sizeof(meta));
  if (mlen == 0 && npts == 0) {
    server.send(404, "application/json", "{\"error\":\"not found\"}");
    return;
  }
  server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  server.send(200, "application/json", "{\"id\":\"");
  server.sendContent(id);
  server.sendContent("\",\"npts\":");
  server.sendContent(String((unsigned)npts));
  server.sendContent(",\"meta\":");
  server.sendContent(mlen > 0 ? meta : "null");
  server.sendContent(",\"fixes\":[");
  sync_codec::Fix buf[16];
  size_t from = 0;
  bool   firstFix = true;
  while (true) {
    size_t got = trip_storage::readFixesRange(id, from, 16, buf);
    if (got == 0) break;
    for (size_t i = 0; i < got; i++) {
      char row[96];
      snprintf(row, sizeof(row), "%s[%ld,%.6f,%.6f,%d,%.2f]",
               firstFix ? "" : ",",
               (long)buf[i].ts, buf[i].lat, buf[i].lon,
               (int)lroundf(buf[i].alt), buf[i].spd);
      server.sendContent(row);
      firstFix = false;
    }
    from += got;
    if (got < 16) break;
  }
  server.sendContent("]}");
}

// GET /api/devices -> liveness roster [{name,hwid,age,online,self}], self first.
void handleApiDevices() {
  if (!requireAuthApi()) return;
  char buf[768];
  Presence.toJson(buf, sizeof(buf), millis());
  server.send(200, "application/json", buf);
}

void handleNotFound() {
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}
}

void WebUi::begin() {
  server.collectHeaders(COOKIE_HEADER_KEYS,
                        sizeof(COOKIE_HEADER_KEYS) / sizeof(COOKIE_HEADER_KEYS[0]));
  server.on("/",              HTTP_GET,  handleRoot);
  server.on("/auth",          HTTP_POST, handleAuth);
  server.on("/logout",        HTTP_GET,  handleLogout);
  server.on("/api/messages",  HTTP_GET,  handleApiMessages);
  server.on("/api/send",      HTTP_POST, handleApiSend);
  server.on("/api/delete",    HTTP_POST, handleApiDelete);
  server.on("/api/trips",     HTTP_GET,  handleApiTrips);
  server.on("/api/trip",      HTTP_GET,  handleApiTrip);
  server.on("/api/devices",   HTTP_GET,  handleApiDevices);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.println("HTTP server listening on :80 (auth gate active, chat live)");
}

void WebUi::loop() {
  server.handleClient();
}

WebUi Web;
