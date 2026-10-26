"""Private two-user trip ledger. PostgreSQL is authoritative; no ephemeral fallback."""
import copy, datetime as dt, hashlib, hmac, html, json, os, re, secrets, threading, time
from collections import defaultdict, deque
from pathlib import Path
from flask import Flask, request, session, redirect, jsonify, make_response, Response
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg
from psycopg.types.json import Jsonb

app=Flask(__name__)
app.wsgi_app=ProxyFix(app.wsgi_app,x_proto=1,x_host=1)
app.secret_key=os.environ['SECRET_KEY']
app.config.update(MAX_CONTENT_LENGTH=2_000_000,SESSION_COOKIE_HTTPONLY=True,
 SESSION_COOKIE_SECURE=os.environ.get('LOCAL_TEST')!='1',SESSION_COOKIE_SAMESITE='Lax',
 PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))
ROOT=Path(__file__).parent
bundle={'seed':json.loads(os.environ['TRIP_SEED']), 'plan':json.loads(os.environ['TRIP_PLAN']),
 'template':(ROOT/'template.html').read_text(encoding='utf-8'),
 'js':'\n'.join((ROOT/name).read_text(encoding='utf-8') for name in ('client-core.js','client-views.js','client-actions.js'))}
DATABASE_URL=os.environ.get('DATABASE_URL','')
PEOPLE=('Leszek','Karolina')
CURRENCIES=('PLN','EUR','CZK')
CATEGORIES=('nocleg','jedzenie','parking','atrakcje','droga','zakupy','inne','paliwo')
_lock=threading.Lock();_initialized=False
_attempts=defaultdict(deque);_attempt_lock=threading.Lock()

def utc():return dt.datetime.now(dt.timezone.utc).isoformat()
def connect():
 if not DATABASE_URL:raise RuntimeError('DATABASE_URL is not set')
 return psycopg.connect(DATABASE_URL,connect_timeout=8,options='-c statement_timeout=8000',sslmode='require')

def initialize():
 global _initialized
 if _initialized:return
 with _lock:
  if _initialized:return
  seed=copy.deepcopy(bundle['seed'])
  for group in ('expenses','stays','transfers'):
   for item in seed[group]:
    item['_v']=1;item['createdBy']='Import początkowy';item['updatedBy']='Import początkowy'
  with connect() as con:
   con.execute('CREATE TABLE IF NOT EXISTS trip_document (id integer PRIMARY KEY, body jsonb NOT NULL)')
   con.execute('CREATE TABLE IF NOT EXISTS trip_audit (operation text PRIMARY KEY, actor text NOT NULL, at timestamptz NOT NULL DEFAULT now(), changes jsonb NOT NULL)')
   con.execute('INSERT INTO trip_document(id,body) VALUES(1,%s) ON CONFLICT(id) DO NOTHING',(Jsonb(seed),))
  _initialized=True

def load_state():
 initialize()
 with connect() as con:return con.execute('SELECT body FROM trip_document WHERE id=1').fetchone()[0]

def jsjson(value):return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
def check_password(name,password):
 entry=os.environ.get('PASSWORD_'+name.upper(),'')
 try:
  salt,expected=entry.split(':')
  actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
  return hmac.compare_digest(actual,expected)
 except (ValueError,TypeError):return False

def csrf():
 if 'csrf' not in session:session['csrf']=secrets.token_urlsafe(32)
 return session['csrf']

def valid_day(value,nullable=True):
 if value in ('',None):
  if nullable:return
  raise ValueError('Data jest wymagana.')
 if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('Nieprawidłowa data.')
 dt.date.fromisoformat(value)
def text(value,limit=4000,required=False):
 if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):raise ValueError('Nieprawidłowy tekst wpisu.')
def amount(x):
 if type(x) is not int or not 0<x<=100_000_000_000:raise ValueError('Nieprawidłowa kwota.')
def validate_record(kind,r):
 text(r.get('id'),150,True)
 if not re.fullmatch(r'[A-Za-z0-9_-]+',r['id']):raise ValueError('Nieprawidłowy identyfikator.')
 if kind=='expenses':
  amount(r.get('amountMinor'));text(r.get('title'),300,True)
  if r.get('payer') not in PEOPLE or r.get('currency') not in CURRENCIES or r.get('category') not in CATEGORIES:raise ValueError('Nieprawidłowy płatnik, waluta lub kategoria.')
  if r.get('status') not in ('paid','confirmed') or r.get('method') not in ('unknown','cash','card','bank') or r.get('split') not in ('unset','half','karolina','leszek','exclude'):raise ValueError('Nieprawidłowe ustawienia kosztu.')
  valid_day(r.get('date'));valid_day(r.get('paymentDate'));text(r.get('notes',''),4000);text(r.get('location',''),250)
  for k in ('liters','odometer'):
   v=r.get(k)
   if v is not None and (type(v) not in (int,float) or not 0<=v<=100_000_000):raise ValueError('Nieprawidłowe dane paliwa.')
 elif kind=='transfers':
  amount(r.get('amountMinor'));valid_day(r.get('date'),False);text(r.get('note',''),1000)
  if r.get('currency') not in CURRENCIES or r.get('from') not in PEOPLE or r.get('to') not in PEOPLE or r['from']==r['to']:raise ValueError('Nieprawidłowy zwrot.')
 elif kind=='stays':
  text(r.get('name'),250,True);text(r.get('city'),150,True);text(r.get('note',''),4000);text(r.get('address',''),500)
  valid_day(r.get('start'),False);valid_day(r.get('end'),False)
  if r['end']<=r['start'] or r.get('status') not in ('proposed','selected','paid','stayed'):raise ValueError('Nieprawidłowy pobyt.')
  if r.get('url') and not re.match(r'^https?://',r['url']):raise ValueError('Nieprawidłowy adres strony.')
 if len(json.dumps(r))>20000:raise ValueError('Wpis jest za długi.')

@app.before_request
def guard():
 if request.path in ('/healthz','/robots.txt'):return
 if request.method in ('POST','PUT','PATCH','DELETE'):
  token=request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
  if not token or not str(token).isascii() or not hmac.compare_digest(str(token),str(session.get('csrf',''))):return jsonify(error='Sesja wygasła. Odśwież stronę i zaloguj się ponownie.'),403
  origin=request.headers.get('Origin')
  if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):return jsonify(error='Nieprawidłowe źródło żądania.'),403
 if request.path!='/login' and session.get('user') not in PEOPLE:
  if request.path.startswith('/api/'):return jsonify(error='Zaloguj się ponownie.'),401
  return redirect('/login')

@app.after_request
def secure(response):
 response.headers['Cache-Control']='private, no-store'
 response.headers['X-Content-Type-Options']='nosniff'
 response.headers['X-Frame-Options']='DENY'
 response.headers['Referrer-Policy']='no-referrer'
 response.headers['X-Robots-Tag']='noindex, nofollow, noarchive'
 response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
 if os.environ.get('LOCAL_TEST')!='1':response.headers['Strict-Transport-Security']='max-age=31536000'
 return response

@app.route('/login',methods=['GET','POST'])
def login():
 error=''
 if request.method=='POST':
  name=request.form.get('user','');password=request.form.get('password','')
  ip=request.remote_addr or 'unknown'
  with _attempt_lock:
   q=_attempts[ip];t=time.monotonic()
   while q and t-q[0]>600:q.popleft()
   limited=len(q)>=10
   if not limited:q.append(t)
   if len(_attempts)>1000:
    for k in list(_attempts):
     if not _attempts[k] or t-_attempts[k][-1]>600:_attempts.pop(k,None)
  if limited:error='Zbyt wiele prób. Odczekaj 10 minut.'
  elif name in PEOPLE and len(password)<=200 and check_password(name,password):
   session.clear();session['user']=name;session['csrf']=secrets.token_urlsafe(32);session.permanent=True
   return redirect('/')
  else:error='Nieprawidłowy użytkownik lub hasło.'
 return '''<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#274b43"><title>Wspólna podróż · logowanie</title><style>*{box-sizing:border-box}body{margin:0;background:#f6f4ec;color:#253d37;font:16px/1.6 system-ui;min-height:100svh;display:grid;place-items:center;padding:22px}.box{max-width:470px;width:100%;padding:34px;border:1px solid #dddcd2;border-radius:22px;background:#fffefa}h1{font:40px/1.1 Georgia;margin:10px 0 20px}label{display:block;margin:15px 0 5px}input,select,button{font:inherit;width:100%;padding:13px;border-radius:10px;border:1px solid #ccc;background:white}button{margin-top:22px;background:#274b43;color:white;cursor:pointer}.small{font-size:13px;color:#68736d}.error{color:#923b35}</style><main class="box"><p class="small">PRYWATNY PLANER · DWA KONTA</p><h1>Wasza wspólna<br>podróż.</h1><p>Ten sam plan i te same wydatki na obu telefonach.</p><form method="post"><input type="hidden" name="csrf" value="'''+html.escape(csrf())+'''"><label for="user">Kto się loguje?</label><select id="user" name="user" autocomplete="username"><option>Leszek</option><option>Karolina</option></select><label for="password">Hasło</label><input type="password" id="password" name="password" required autocomplete="current-password"><p class="error">'''+html.escape(error)+'''</p><button>Zaloguj się</button></form><p class="small">Darmowa baza: do 25.11.2026. Kopię danych pobierz do 24.11.2026. Po przerwie serwer może potrzebować chwili na uruchomienie.</p></main></html>'''

@app.post('/api/logout')
def logout():session.clear();return jsonify(ok=True)
@app.get('/healthz')
def health():return jsonify(server='ok',databaseConfigured=bool(DATABASE_URL))
@app.get('/robots.txt')
def robots():return Response('User-agent: *\nDisallow: /\n',mimetype='text/plain')

@app.get('/')
def index():
 ready=False;state=copy.deepcopy(bundle['seed'])
 if DATABASE_URL:
  try:state=load_state();ready=True
  except psycopg.Error:app.logger.error('PostgreSQL unavailable; credentials not logged')
 setup=''
 if not ready:
  setup='<div class="notice warn"><strong>Wspólny zapis nie jest jeszcze aktywny.</strong><p>Administrator musi połączyć aplikację z bazą: w Render otwórz bazę → Connect → Internal Database URL, a następnie usługę aplikacji → Environment → dodaj DATABASE_URL i wybierz Save, rebuild, and deploy. Nie wklejaj adresu bazy do publicznego repozytorium. Potem odśwież stronę. Widoczny plan jest na razie tylko do odczytu.</p></div>'
 page=bundle['template'].replace('__STATE__',jsjson(state)).replace('__PLAN__',jsjson(bundle['plan'])).replace('__JS__',bundle['js']).replace('__USER__',html.escape(session['user'])).replace('__SETUP__',setup).replace('__CLOUD__',jsjson({'csrf':csrf(),'user':session['user'],'ready':ready}))
 return Response(page,mimetype='text/html')

@app.get('/api/state')
def api_state():
 state=load_state()
 if request.args.get('revision')==str(state['revision']):return jsonify(unchanged=True,revision=state['revision'])
 return jsonify(state=state)

@app.post('/api/changes')
def changes():
 initialize();payload=request.get_json(silent=True)
 if not isinstance(payload,dict):return jsonify(error='Nieprawidłowy format.'),400
 operation=payload.get('operation','');items=payload.get('changes')
 if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation) or not isinstance(items,list) or not 1<=len(items)<=500:return jsonify(error='Nieprawidłowe zmiany.'),400
 actor=session['user'];stamp=utc()
 with connect() as con:
  document=con.execute('SELECT body FROM trip_document WHERE id=1 FOR UPDATE').fetchone()[0]
  if con.execute('SELECT 1 FROM trip_audit WHERE operation=%s',(operation,)).fetchone():return jsonify(state=document,duplicate=True)
  document=copy.deepcopy(document);touched=set()
  for change in items:
   if not isinstance(change,dict):raise ValueError('Nieprawidłowa zmiana.')
   kind=change.get('collection');ident=change.get('id');value=change.get('value')
   if kind not in ('expenses','stays','transfers','notes','settings') or not isinstance(ident,str) or (kind,ident) in touched:raise ValueError('Nieprawidłowa lub powtórzona pozycja.')
   touched.add((kind,ident))
   if kind in ('notes','settings'):
    if kind=='notes':valid_day(ident,False);text(value,4000)
    elif ident not in ('halfUnassigned','includeFuel') or type(value) is not bool:raise ValueError('Nieprawidłowe ustawienie.')
    if document[kind].get(ident)!=change.get('before'):return jsonify(error='Inna osoba zmieniła tę samą pozycję. Wczytaj wspólną wersję i ponów zmianę.'),409
    document[kind][ident]=value;continue
   if not isinstance(value,dict) or value.get('id')!=ident:raise ValueError('Nieprawidłowy wpis.')
   prev=next((x for x in document[kind] if x['id']==ident),None)
   if (prev.get('_v',1) if prev else 0)!=change.get('version'):return jsonify(error='Inna osoba zmieniła ten sam wpis. Nie nadpisano jej zmian.'),409
   if not prev and len(document[kind])>=20000:raise ValueError('Limit liczby wpisów.')
   value=copy.deepcopy(value);validate_record(kind,value)
   value['_v']=(prev.get('_v',1) if prev else 0)+1
   value['createdAt']=prev.get('createdAt',stamp) if prev else stamp
   value['createdBy']=prev.get('createdBy','Import początkowy') if prev else actor
   value['updatedAt']=stamp;value['updatedBy']=actor;value['deletedAt']=stamp if value.get('deletedAt') else None
   if prev:document[kind][document[kind].index(prev)]=value
   else:document[kind].append(value)
  document['revision']=int(document['revision'])+1;document['updatedAt']=stamp
  if len(json.dumps(document))>1_500_000:raise ValueError('Limit rozmiaru danych — pobierz kopię.')
  con.execute('UPDATE trip_document SET body=%s WHERE id=1',(Jsonb(document),))
  con.execute('INSERT INTO trip_audit(operation,actor,changes) VALUES(%s,%s,%s)',(operation,actor,Jsonb(items)))
 return jsonify(state=document)

@app.get('/api/offline')
def offline():
 state=load_state();e=lambda x:html.escape(str(x))
 rows=''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [x.get('date') or '',x['title'],f"{x['amountMinor']/100:.2f}",x['currency'],x['payer'],x.get('createdBy',''),x['status']])+'</tr>' for x in state['expenses'] if not x.get('deletedAt'))
 days=''.join('<h2>'+e(d['date']+' · '+d['title'])+'</h2>'+''.join('<p><b>'+e(v['time'])+'</b> '+e(v['text'])+'</p>' for v in d['items'])+'<p>'+e(state['notes'].get(d['date'],''))+'</p>' for d in bundle['plan']['plan'])
 raw=e(json.dumps({'state':state,'plan':bundle['plan']},ensure_ascii=False,indent=2))
 page='<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kopia podróży</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:30px auto;padding:20px;color:#253d37}table{width:100%;border-collapse:collapse}td,th{padding:8px;border:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Kopia podróży</h1><p>Archiwum offline. Nowe wpisy dodawaj na stronie online.</p><table><tr><th>Data</th><th>Opis</th><th>Kwota</th><th>Waluta</th><th>Płatnik</th><th>Autor</th><th>Status</th></tr>'+rows+'</table>'+days+'<details><summary>Pełne dane archiwum</summary><pre>'+raw+'</pre></details></html>'
 response=make_response(page);response.headers['Content-Type']='text/html; charset=utf-8';response.headers['Content-Disposition']='attachment; filename="podroz-kopia-offline.html"';return response

@app.errorhandler(ValueError)
def bad_input(error):return jsonify(error=str(error)),400
@app.errorhandler(psycopg.Error)
def database_error(error):
 app.logger.error('Database request failed; details intentionally withheld')
 return jsonify(error='Baza chwilowo niedostępna. Nie potwierdzono zapisu. Spróbuj ponownie.'),503
@app.errorhandler(RuntimeError)
def not_configured(error):return jsonify(error='Wspólna baza nie jest jeszcze połączona. Administrator musi ustawić DATABASE_URL.'),503

if __name__=='__main__':app.run(host='127.0.0.1',port=5000)
