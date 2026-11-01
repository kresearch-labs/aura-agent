"""teams_chat_page.py — the people chat's one page.

No framework, no build step. Everything a person wrote is put on the page with
textContent / createElement — never as HTML — so a message can't run anything;
links are http(s) only; files come from the server's own route.
"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>team chat</title>
<style>
:root{--bg:#0f1318;--panel:#151a21;--line:#252c35;--text:#e4e7eb;--dim:#8b95a1;--faint:#5c6672;
 --accent:#6ea8fe;--me:#1d2735;--warn:#f0b35a;--ok:#5fcf8e;--hl:#23324a}
@media (prefers-color-scheme: light){:root{--bg:#f7f8fa;--panel:#fff;--line:#e3e6ea;--text:#1d2329;
 --dim:#5b6570;--faint:#97a0aa;--accent:#2563eb;--me:#eef3fd;--hl:#e7efff}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex}
[hidden]{display:none!important}
#login{margin:auto;width:min(22rem,92vw);display:flex;flex-direction:column;gap:.55rem}
#login h1{font-size:1.2rem;margin:.2rem 0}
input,textarea{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:7px;
 padding:.55rem .7rem;font:inherit}
button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:.5rem .9rem;cursor:pointer;font:inherit}
button.ghost{background:transparent;color:var(--dim);padding:.2rem .45rem}
#err,.err{color:#f08080;min-height:1.2em;font-size:.9em}
#app{display:flex;flex:1;min-width:0}
#side{width:250px;flex:none;background:var(--panel);border-right:1px solid var(--line);display:flex;
 flex-direction:column;overflow:auto}
#side h3{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin:.9rem .9rem .3rem}
.item{display:flex;align-items:center;gap:.45rem;padding:.3rem .9rem;cursor:pointer;color:var(--dim)}
.item:hover{background:var(--hl)} .item.on{background:var(--hl);color:var(--text);font-weight:600}
.item .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{background:var(--accent);color:#fff;border-radius:9px;font-size:.72rem;padding:0 .4rem}
.badge.at{background:var(--warn);color:#000}
.dot{width:.55rem;height:.55rem;border-radius:50%;background:var(--faint);flex:none}
.dot.on{background:var(--ok)}
#search{margin:.7rem .9rem 0}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#top{padding:.6rem 1rem;border-bottom:1px solid var(--line);display:flex;gap:.6rem;align-items:center}
#top b{font-size:1.02rem} #top .sp{flex:1}
#log{flex:1;overflow-y:auto;padding:.6rem 1rem}
.m{padding:.3rem .5rem;border-radius:7px;position:relative}
.m:hover{background:var(--hl)} .m.mine{background:var(--me)}
.who{font-weight:600;margin-right:.45rem} .ts{color:var(--faint);font-size:.75rem}
.body{white-space:pre-wrap;word-break:break-word}
.body code{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:0 .25rem;font-size:.9em}
.body pre{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:.5rem;overflow:auto;margin:.3rem 0}
a{color:var(--accent)}
.mention{color:var(--warn);font-weight:600} .mention.me{background:var(--warn);color:#000;border-radius:4px;padding:0 .2rem}
.reply{border-left:3px solid var(--line);padding-left:.5rem;color:var(--dim);font-size:.85em;margin-bottom:.15rem}
.acts{position:absolute;right:.4rem;top:.15rem;display:none;gap:.1rem;background:var(--panel);
 border:1px solid var(--line);border-radius:6px}
.m:hover .acts{display:flex}
.reacts{display:flex;gap:.3rem;margin-top:.2rem;flex-wrap:wrap}
.chip{border:1px solid var(--line);border-radius:10px;padding:0 .4rem;font-size:.85em;cursor:pointer}
.chip.me{border-color:var(--accent)}
.file img{max-width:min(420px,100%);max-height:320px;border-radius:6px;display:block;margin-top:.25rem}
.seen,.typing{color:var(--faint);font-size:.78rem;padding:.1rem .5rem}
#comp{border-top:1px solid var(--line);padding:.6rem 1rem}
#replyto{color:var(--dim);font-size:.85em;margin-bottom:.3rem}
#row{display:flex;gap:.45rem;align-items:flex-end}
#box{flex:1;resize:none;max-height:10rem}
.picker{position:absolute;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.25rem;display:flex;gap:.2rem;z-index:5}
.picker span{cursor:pointer;padding:.15rem .25rem;font-size:1.1em}
@media (max-width:700px){#side{width:180px}}
</style></head><body>
<form id="login" hidden>
 <h1>team chat</h1>
 <div style="color:var(--dim)">Ask your Aura: <i>"open the team chat"</i> — it signs you in.
  On a Vikunja hub you can also sign in here.</div>
 <input id="u" autocomplete="username" placeholder="Vikunja username">
 <input id="p" type="password" autocomplete="current-password" placeholder="password">
 <input id="c" inputmode="numeric" autocomplete="one-time-code" placeholder="2-factor code" hidden>
 <button>sign in</button><span id="err"></span>
</form>
<div id="app" hidden>
 <nav id="side">
  <input id="search" placeholder="search messages">
  <h3>rooms <button class="ghost" id="newroom" title="new room">+</button></h3><div id="rooms"></div>
  <h3>direct</h3><div id="dms"></div>
  <h3>people</h3><div id="people"></div>
 </nav>
 <main id="main">
  <div id="top"><b id="title"></b><span class="sp"></span>
   <button class="ghost" id="notif" title="notifications">🔔</button>
   <span id="me" style="color:var(--dim)"></span><button class="ghost" id="out">sign out</button></div>
  <div id="log"></div><div class="typing" id="typing"></div>
  <div id="comp"><div id="replyto" hidden></div>
   <div id="row"><button class="ghost" id="attach" title="attach a file">📎</button>
    <input type="file" id="file" hidden>
    <textarea id="box" rows="1" maxlength="4000" placeholder="message — Enter sends, Shift+Enter new line, **bold** *italic* `code`, @name, #task"></textarea>
    <button id="send">send</button></div><div class="err" id="cerr"></div></div>
 </main>
</div>
<script>
const $=id=>document.getElementById(id);
let tok=null,me=null,room=null,rooms=[],emoji=[],taskUrl="",replyTo=null,seen={},msgs={},es=null,lastId=0;
const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
async function api(path,body){
 const r=await fetch(path,{method:body?"POST":"GET",headers:{"Authorization":"Bearer "+tok,
   ...(body&&!(body instanceof Blob)?{"Content-Type":"application/json"}:{})},
   body:body?(body instanceof Blob?body:JSON.stringify(body)):undefined});
 if(r.status==401){out();throw new Error("signed out")}
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||("error "+r.status));return j}
// ---- sign in: by your Aura (#t=..&u=..), or Vikunja's password
function start(t,u){tok=t;me=u;try{sessionStorage.setItem("chat",JSON.stringify({tok,me}))}catch(_){}
 $("login").hidden=true;$("app").hidden=false;$("me").textContent=me;boot()}
$("login").onsubmit=async e=>{e.preventDefault();$("err").textContent="";
 const r=await fetch("login",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({u:$("u").value,p:$("p").value,c:$("c").value})});
 const j=await r.json().catch(()=>({}));
 if(r.status==412){$("c").hidden=false;$("c").focus();$("err").textContent="enter your 2-factor code";return}
 if(!r.ok){$("err").textContent=j.error||"could not sign in";return}
 $("p").value="";start(j.token,j.user)};
function out(){tok=null;try{sessionStorage.removeItem("chat")}catch(_){};if(es)es.close();location.hash="";location.reload()}
$("out").onclick=out;
// ---- the page
async function boot(){
 const m=await api("api/me");emoji=m.emoji;taskUrl=m.task_url||"";lastId=m.last_event||0;
 await loadRooms();const first=rooms.find(r=>r.name=="general")||rooms[0];if(first)openRoom(first.id);
 loadPeople();setInterval(loadPeople,30000);listen()}
async function loadRooms(){rooms=(await api("api/rooms")).rooms;drawRooms()}
function drawRooms(){for(const [box,kind] of [["rooms","room"],["dms","dm"]]){const b=$(box);b.textContent="";
 for(const r of rooms.filter(x=>x.kind==kind)){const it=el("div","item"+(r.id==room?" on":""));
  it.append(el("span","n",(kind=="room"?"# ":"")+r.name));
  if(r.mentions)it.append(el("span","badge at","@"+r.mentions));else if(r.unread&&r.id!=room)it.append(el("span","badge",r.unread));
  it.onclick=()=>openRoom(r.id);b.append(it)}}
 const total=rooms.reduce((a,r)=>a+(r.id==room?0:r.unread),0);document.title=(total?"("+total+") ":"")+"team chat"}
async function loadPeople(){const ps=(await api("api/people")).people;const b=$("people");b.textContent="";
 for(const p of ps){const it=el("div","item");it.append(el("span","dot"+(p.online?" on":"")),el("span","n",p.name+(p.name==me?" (you)":"")));
  if(p.name!=me)it.onclick=async()=>{const r=await api("api/dm",{user:p.name});await loadRooms();openRoom(r.id)};b.append(it)}}
async function openRoom(id){room=id;replyTo=null;$("replyto").hidden=true;msgs={};
 const r=rooms.find(x=>x.id==id);$("title").textContent=r?(r.kind=="room"?"# ":"")+r.name:"";
 const h=await api("api/history?room="+id);seen=h.seen||{};$("log").textContent="";
 for(const m of h.messages)put(m);drawSeen();bottom();markRead();if(r){r.unread=0;r.mentions=0}drawRooms();$("box").focus()}
function bottom(){$("log").scrollTop=$("log").scrollHeight}
function nearBottom(){const l=$("log");return l.scrollTop+l.clientHeight>=l.scrollHeight-60}
// ---- formatting (all as text nodes)
function fmt(text,into){
 const parts=text.split(/(```[\s\S]*?```)/g);
 for(const part of parts){if(part.startsWith("```")&&part.endsWith("```")&&part.length>5){
   into.append(el("pre",null,part.slice(3,-3).replace(/^\w*\n/,"")));continue}
  const re=/(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`|https?:\/\/[^\s<>()]+|@[\w.-]+|#\d+)/g;let i=0,m;
  while((m=re.exec(part))){if(m.index>i)into.append(document.createTextNode(part.slice(i,m.index)));const s=m[0];
   if(s.startsWith("**"))into.append(el("b",null,s.slice(2,-2)));
   else if(s.startsWith("`"))into.append(el("code",null,s.slice(1,-1)));
   else if(s.startsWith("*"))into.append(el("i",null,s.slice(1,-1)));
   else if(s.startsWith("http")){const a=el("a",null,s);a.href=s;a.target="_blank";a.rel="noopener noreferrer";into.append(a)}
   else if(s.startsWith("@"))into.append(el("span","mention"+(s.slice(1).toLowerCase()==(me||"").toLowerCase()?" me":""),s));
   else if(s.startsWith("#")&&taskUrl){const a=el("a",null,s);a.href=taskUrl.replace("{id}",s.slice(1));a.target="_blank";a.rel="noopener noreferrer";into.append(a)}
   else into.append(document.createTextNode(s));i=m.index+s.length}
  if(i<part.length)into.append(document.createTextNode(part.slice(i)))}}
function when(ts){const d=new Date(ts*1000);return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})+
 (new Date().toDateString()==d.toDateString()?"":" · "+d.toLocaleDateString())}
function put(m){msgs[m.id]=m;let d=document.getElementById("m"+m.id);const fresh=!d;
 if(!d){d=el("div","m");d.id="m"+m.id}else d.textContent="";
 d.className="m"+(m.user==me?" mine":"");
 if(m.reply){const q=el("div","reply");q.append(el("b",null,m.reply.user+" "));q.append(document.createTextNode((m.reply.body||"(deleted)").replace(/[*`]/g,"")));d.append(q)}
 d.append(el("span","who",m.user==me?"you":m.user),el("span","ts",when(m.ts)+(m.edited&&!m.deleted?" · edited":"")));
 const b=el("div","body");if(m.deleted)b.append(el("i",null,"(deleted)"));else fmt(m.body||"",b);d.append(b);
 if(m.file&&!m.deleted){const f=el("div","file");
  if(m.file.type.startsWith("image/")){const img=el("img");img.alt=m.file.name;img.src="files/"+m.file.id+"?t="+tok;f.append(img)}
  else{const a=el("a",null,"📄 "+m.file.name+" ("+Math.ceil(m.file.size/1024)+" KB)");a.href="files/"+m.file.id+"?t="+tok;a.download=m.file.name;f.append(a)}
  d.append(f)}
 const rs=el("div","reacts");for(const [e,us] of Object.entries(m.reactions||{})){const c=el("span","chip"+(us.includes(me)?" me":""),e+" "+us.length);
  c.title=us.join(", ");c.onclick=()=>api("api/react",{id:m.id,emoji:e});rs.append(c)}d.append(rs);
 if(!m.deleted){const a=el("div","acts");
  const btn=(t,f,tt)=>{const x=el("button","ghost",t);x.title=tt;x.onclick=f;a.append(x)};
  btn("↩",()=>{replyTo=m.id;$("replyto").hidden=false;$("replyto").textContent="replying to "+m.user+": "+(m.body||"").slice(0,80)+"  (Esc cancels)";$("box").focus()},"reply");
  btn("☺",ev=>pick(ev,m.id),"react");
  if(m.user==me){btn("✎",()=>editMsg(m),"edit");btn("🗑",()=>{if(confirm("Delete this message?"))api("api/delete",{id:m.id})},"delete")}
  d.append(a)}
 if(fresh){const keep=nearBottom();$("log").append(d);if(keep)bottom()}}
function pick(ev,id){document.querySelectorAll(".picker").forEach(p=>p.remove());const p=el("div","picker");
 for(const e of emoji){const s=el("span",null,e);s.onclick=()=>{api("api/react",{id,emoji:e});p.remove()};p.append(s)}
 const r=ev.target.getBoundingClientRect();p.style.left=(r.left-150)+"px";p.style.top=(r.bottom+4)+"px";document.body.append(p);
 setTimeout(()=>document.addEventListener("click",()=>p.remove(),{once:true}),0)}
function editMsg(m){const t=prompt("Edit your message:",m.body);if(t!=null&&t.trim()&&t!=m.body)api("api/edit",{id:m.id,body:t})}
function drawSeen(){document.querySelectorAll(".seen").forEach(x=>x.remove());const ids=Object.keys(msgs).map(Number);
 if(!ids.length)return;const last=Math.max(...ids);const author=msgs[last]&&msgs[last].user;
 const who=Object.entries(seen).filter(([u,l])=>u!=me&&u!=author&&l>=last).map(([u])=>u);
 if(who.length){const s=el("div","seen","seen by "+who.join(", "));document.getElementById("m"+last).after(s)}}
let readT=null;function markRead(){clearTimeout(readT);readT=setTimeout(()=>{const ids=Object.keys(msgs).map(Number);
 if(ids.length&&room&&!document.hidden)api("api/read",{room,last:Math.max(...ids)})},400)}
// ---- live (server-sent events; a reconnect gets what it missed)
function listen(){if(es)es.close();es=new EventSource("api/events?t="+tok+"&after="+lastId);
 const on=(k,f)=>es.addEventListener(k,ev=>{lastId=Math.max(lastId,+ev.lastEventId||0);f(JSON.parse(ev.data))});
 on("msg",e=>{const m=e.d;if(e.room==room){put(m);drawSeen();markRead()}else{const r=rooms.find(x=>x.id==e.room);
   if(r&&m.user!=me){r.unread++;if((m.body||"").toLowerCase().includes("@"+me.toLowerCase()))r.mentions++;drawRooms()}else loadRooms()}
  if(m.user!=me)notify(m,e.room)});
 on("edit",e=>{if(e.room==room)put(e.d)});
 on("delete",e=>{if(e.room==room&&msgs[e.d.id])put({...msgs[e.d.id],deleted:1,body:""})});
 on("react",e=>{if(e.room==room&&msgs[e.d.id])put({...msgs[e.d.id],reactions:e.d.reactions})});
 on("seen",e=>{if(e.room==room){seen[e.d.user]=e.d.last;drawSeen()}});
 on("room",()=>loadRooms());
 es.addEventListener("beat",ev=>{const b=JSON.parse(ev.data);const t=b.typing.filter(x=>x.room==room).map(x=>x.user);
  $("typing").textContent=t.length?t.join(", ")+(t.length>1?" are":" is")+" typing…":""});
 es.onerror=()=>{}}
function notify(m,rid){const r=rooms.find(x=>x.id==rid);const mine=(m.body||"").toLowerCase().includes("@"+me.toLowerCase());
 if(!document.hidden&&rid==room)return;if(!(mine||(r&&r.kind=="dm")))return;
 if(window.Notification&&Notification.permission=="granted")new Notification(m.user+(r&&r.kind=="room"?" in #"+r.name:""),{body:(m.body||"").slice(0,140)})}
$("notif").onclick=()=>{if(window.Notification)Notification.requestPermission()};
document.addEventListener("visibilitychange",()=>{if(!document.hidden)markRead()});
// ---- writing
let typeT=0;$("box").addEventListener("input",()=>{const b=$("box");b.style.height="auto";b.style.height=b.scrollHeight+"px";
 if(Date.now()-typeT>3000&&room){typeT=Date.now();api("api/typing",{room}).catch(()=>{})}});
$("box").addEventListener("keydown",e=>{if(e.key=="Enter"&&!e.shiftKey){e.preventDefault();send()}
 if(e.key=="Escape"){replyTo=null;$("replyto").hidden=true}});
$("send").onclick=()=>send();
async function send(file){const b=$("box").value.trim();if((!b&&!file)||!room)return;$("cerr").textContent="";
 try{await api("api/post",{room,body:b,reply_to:replyTo,file:file||null});$("box").value="";$("box").style.height="auto";
  replyTo=null;$("replyto").hidden=true}catch(e){$("cerr").textContent=e.message}}
$("attach").onclick=()=>$("file").click();
$("file").onchange=async()=>{const f=$("file").files[0];if(!f)return;$("cerr").textContent="";
 try{const r=await fetch("api/upload",{method:"POST",headers:{"Authorization":"Bearer "+tok,"X-Name":encodeURIComponent(f.name)},body:f});
  const j=await r.json();if(!r.ok)throw new Error(j.error||"upload failed");await send(j.id)}catch(e){$("cerr").textContent=e.message}
 $("file").value=""};
$("newroom").onclick=async()=>{const n=prompt("New room name:");if(n){const r=await api("api/room",{name:n});await loadRooms();openRoom(r.id)}};
let sT=null;$("search").oninput=()=>{clearTimeout(sT);sT=setTimeout(async()=>{const q=$("search").value.trim();
 if(!q){openRoom(room);return}const res=(await api("api/search?q="+encodeURIComponent(q))).messages;
 $("title").textContent="search: "+q;$("log").textContent="";msgs={};
 if(!res.length)$("log").append(el("div","seen","nothing found"));
 for(const m of res){const r=rooms.find(x=>x.id==m.room);const d=el("div","m");d.append(el("span","who",(r?(r.kind=="room"?"#":"")+r.name:"")+" · "+m.user),el("span","ts",when(m.ts)));
  const b=el("div","body");fmt(m.body,b);d.append(b);d.style.cursor="pointer";d.onclick=()=>{$("search").value="";openRoom(m.room)};$("log").append(d)}},300)};
// ---- start: a sign-in your Aura opened (#t=…&u=…), a saved one, or the form
(function(){const h=new URLSearchParams(location.hash.slice(1));
 if(h.get("t")&&h.get("u")){history.replaceState(null,"",location.pathname);start(h.get("t"),h.get("u"));return}
 try{const s=JSON.parse(sessionStorage.getItem("chat")||"null");if(s){start(s.tok,s.me);return}}catch(_){}
 $("login").hidden=false})();
</script></body></html>"""
