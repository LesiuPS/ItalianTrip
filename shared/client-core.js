'use strict';
const STORE='jesienna-wyprawa-2026-actuals-v1';
const SEED=JSON.parse(document.getElementById('trip-state').textContent);
const PLAN=JSON.parse(document.getElementById('trip-plan').textContent);
const CAT={nocleg:'Noclegi',jedzenie:'Jedzenie i napoje',parking:'Parkingi',atrakcje:'Atrakcje',droga:'Opłaty drogowe',zakupy:'Zakupy i pamiątki',inne:'Inne',paliwo:'Paliwo'};
const METHODS={unknown:'Sposób niepodany',cash:'Gotówka',card:'Karta',bank:'Przelew'};
const SPLITS={unset:'Podział do ustalenia',half:'50/50',karolina:'Koszt Karoliny',leszek:'Koszt Leszka',exclude:'Bez rozliczania'};
const CUR=['PLN','EUR','CZK'];
const $=s=>document.getElementById(s);
const clone=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=(n,c)=>new Intl.NumberFormat('pl-PL',{style:'currency',currency:c,minimumFractionDigits:2,maximumFractionDigits:2}).format(n/100);
const num=x=>new Intl.NumberFormat('pl-PL',{maximumFractionDigits:2}).format(x);
const datefmt=x=>x?new Intl.DateTimeFormat('pl-PL',{day:'2-digit',month:'2-digit'}).format(new Date(x+'T12:00:00')):'Data do uzupełnienia';
const longdate=x=>new Intl.DateTimeFormat('pl-PL',{weekday:'long',day:'numeric',month:'long'}).format(new Date(x+'T12:00:00'));
const now=()=>new Date().toISOString();
const formDate=()=>{const d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');};
const uid=()=>globalThis.crypto?.randomUUID?.()||('id-'+Date.now()+'-'+Math.random().toString(36).slice(2));
const alive=xs=>xs.filter(x=>!x.deletedAt);
const activeExpenses=()=>alive(state.expenses);
const paid=(xs=activeExpenses())=>xs.filter(e=>e.status==='paid');
const totals=xs=>xs.reduce((a,e)=>(a[e.currency]=(a[e.currency]||0)+e.amountMinor,a),{});
const totalText=(x,empty='Brak wpisów')=>{const t=Array.isArray(x)?totals(x):x;const parts=CUR.filter(c=>t[c]).map(c=>money(t[c],c));return parts.length?parts.join(' + '):empty;};
const navurl=q=>'https://www.google.com/maps/dir/?api=1&destination='+encodeURIComponent(q)+'&travelmode=driving';
const safeurl=u=>/^https?:\/\//i.test(u||'')?u:'#';
function isDate(d){return d===null||d===''||(typeof d==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(d)&&!Number.isNaN(Date.parse(d))&&new Date(d+'T12:00:00Z').toISOString().slice(0,10)===d);}
function validate(s){
 if(!s||s.schemaVersion!==1||s.tripId!==SEED.tripId||!Array.isArray(s.expenses)||!Array.isArray(s.stays)||!Array.isArray(s.transfers)||s.expenses.length>20000)throw Error('Nieprawidłowy format kopii tej podróży.');
 const ids=new Set();
 s.expenses.forEach(e=>{
  if(!e||typeof e.id!=='string'||!e.id||ids.has(e.id)||typeof e.title!=='string'||e.title.length>300||!Number.isSafeInteger(e.amountMinor)||e.amountMinor<=0||e.amountMinor>1e12||!CUR.includes(e.currency)||!['Leszek','Karolina'].includes(e.payer)||!CAT[e.category]||!['paid','confirmed'].includes(e.status)||!Object.hasOwn(SPLITS,e.split)||!Object.hasOwn(METHODS,e.method)||!isDate(e.date)||!isDate(e.paymentDate))throw Error('Kopia zawiera nieprawidłowy lub zduplikowany wydatek.');
  ids.add(e.id);
 });
 const tids=new Set();s.transfers.forEach(t=>{if(!t||typeof t.id!=='string'||tids.has(t.id)||!Number.isSafeInteger(t.amountMinor)||t.amountMinor<=0||!CUR.includes(t.currency)||!['Leszek','Karolina'].includes(t.from)||!['Leszek','Karolina'].includes(t.to)||t.from===t.to||!isDate(t.date))throw Error('Nieprawidłowy zwrot w kopii.');tids.add(t.id);});
 const sids=new Set();s.stays.forEach(t=>{if(!t||typeof t.id!=='string'||sids.has(t.id)||typeof t.name!=='string'||!isDate(t.start)||!isDate(t.end))throw Error('Nieprawidłowe dane noclegu.');sids.add(t.id);});
 s.settings={halfUnassigned:!!s.settings?.halfUnassigned,includeFuel:!!s.settings?.includeFuel};
 if(!s.notes||typeof s.notes!=='object'||Array.isArray(s.notes))s.notes={};
 return s;
}
function stamp(x,fallback){return Date.parse(x.updatedAt||fallback||'')||0;}
function merge(a,b){
 validate(a);validate(b);
 const newer=stamp(a)>stamp(b)?a:b;
 const out=clone(newer);
 ['expenses','transfers','stays'].forEach(k=>{const m=new Map();for(const s of [a,b])for(const x of s[k]){const old=m.get(x.id);if(!old||stamp(x,s.updatedAt)>=old.t)m.set(x.id,{v:clone(x),t:stamp(x,s.updatedAt)});}out[k]=[...m.values()].map(x=>x.v);});
 const older=newer===a?b:a;out.notes={...older.notes,...newer.notes};return out;
}
let state=clone(SEED),storageOK=true,toastTimer,activeTab='wydatki',dayIdx=1,editingStayId=null;
function toast(s){$('toast').textContent=s;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},4800);}
const CLOUD=JSON.parse(document.getElementById('cloud-config').textContent);
let cloudBase=clone(state), saving=false, pending=null, lastConnection=true, pollBusy=false;
const DRAFT_KEY='trip-pending-'+CLOUD.user;
function same(a,b){return JSON.stringify(a)===JSON.stringify(b);}
function changesSince(base,next){
 const changes=[];
 for(const collection of ['expenses','stays','transfers']){
  const old=new Map(base[collection].map(x=>[x.id,x]));
  for(const value of next[collection]){const before=old.get(value.id);if(!before||!same(before,value))changes.push({collection,id:value.id,version:before?(before._v||1):0,value:clone(value)});}
 }
 for(const collection of ['notes','settings'])for(const [id,value] of Object.entries(next[collection]))if(!same(base[collection][id],value))changes.push({collection,id,before:base[collection][id]??null,value});
 return changes;
}
function saveDraft(){try{if(pending)sessionStorage.setItem(DRAFT_KEY,JSON.stringify({pending,state}));else sessionStorage.removeItem(DRAFT_KEY);}catch(e){}}
function setCloudStatus(text,bad=false){$('saveStatus').textContent=text;$('saveStatus').style.color=bad?'#923b35':'';}
function pendingBanner(error=''){
 $('cloudPending').hidden=!pending;
 if(pending)$('cloudPendingText').textContent=error||'Zapis trwa. Poczekaj na potwierdzenie wspólnej bazy.';
 $('retryCloud').disabled=saving;$('loadCloud').disabled=saving;
}
async function api(path,options={}){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
 try{
  const r=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,signal:controller.signal,headers:{'Content-Type':'application/json','X-CSRF-Token':CLOUD.csrf,...options.headers}});
  let data;try{data=await r.json();}catch(e){throw Error('Serwer się uruchamia lub połączenie zostało przerwane. Spróbuj ponownie.');}
  if(!r.ok){const e=Error(data.error||'Nie udało się zapisać.');e.status=r.status;throw e;}
  return data;
 }finally{clearTimeout(timer);}
}
async function persist(message){
 if(!CLOUD.ready){state=clone(cloudBase);render();toast('Wspólna baza nie jest jeszcze połączona. Wpis nie został zapisany.');return false;}
 if(saving||pending){toast('Najpierw zakończ poprzedni zapis.');return false;}
 const changes=changesSince(cloudBase,state);if(!changes.length)return true;
 pending={operation:uid(),changes,message:message||'Zmiany zapisane we wspólnej bazie.'};saveDraft();return sendPending();
}
async function sendPending(){
 if(!pending||saving)return false;
 saving=true;setCloudStatus('Zapisywanie we wspólnej bazie…');pendingBanner();
 try{
  const result=await api('/api/changes',{method:'POST',body:JSON.stringify(pending)});
  const msg=pending.message;state=validate(result.state);cloudBase=clone(state);pending=null;saveDraft();
  render();setCloudStatus('Zapisano wspólnie · '+new Date().toLocaleTimeString('pl-PL',{hour:'2-digit',minute:'2-digit'}));toast(msg);lastConnection=true;return true;
 }catch(e){
  setCloudStatus('Nie potwierdzono zapisu',true);
  pendingBanner(e.status===409?e.message+' Pobierz roboczy JSON, a następnie wczytaj wspólną wersję i popraw ten wpis ponownie.':(e.name==='AbortError'?'Przekroczono czas połączenia. Ponów zapis; nie zdubluje to kosztu.':e.message));
  lastConnection=false;return false;
 }finally{saving=false;$('cloudPending').hidden=!pending;$('retryCloud').disabled=false;$('loadCloud').disabled=false;}
}
async function refreshCloud(force=false){
 if(!CLOUD.ready||saving||pending||pollBusy||document.hidden)return;
 if(!force&&(document.querySelector('dialog[open]')||document.activeElement?.matches('input,textarea,select')))return;
 pollBusy=true;
 try{
  const r=await api('/api/state'+(force?'':'?revision='+encodeURIComponent(cloudBase.revision)));
  if(r.state){state=validate(r.state);cloudBase=clone(state);render();}
  setCloudStatus('Wspólny zapis · online');lastConnection=true;
 }catch(e){setCloudStatus(e.status===401?'Sesja wygasła — zaloguj ponownie':'Brak połączenia · ostatni odczyt',true);lastConnection=false;}
 finally{pollBusy=false;}
}
function mutationBlocked(ev){
 const el=ev.target.closest('button,input,select,form');
 const isMutation=ev.type==='submit'||el?.matches('#addExpense,#addFuel,#addTransfer,#halfUnassigned,#includeFuel,#importJson,button[data-edit],button[data-stay],button[data-addstay],button[data-laundry],button[data-delete-transfer],button[data-restore],#saveDayNote');
 if(isMutation&&(!CLOUD.ready||saving||pending)){
  ev.preventDefault();ev.stopImmediatePropagation();toast(!CLOUD.ready?'Najpierw trzeba połączyć bazę danych.': 'Najpierw zakończ poprzedni zapis — użyj paska poniżej.');
 }
}
for(const kind of ['click','submit','change'])document.addEventListener(kind,mutationBlocked,true);
window.addEventListener('beforeunload',ev=>{if(pending){ev.preventDefault();ev.returnValue='';}});
