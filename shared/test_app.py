"""Run during build. Uses a transactional memory adapter, never the live database."""
import copy, hashlib, os, re, unittest
from unittest.mock import patch
os.environ['LOCAL_TEST']='1'
from shared import app as m

class Result:
    def __init__(self, value=None): self.value=value
    def fetchone(self): return self.value

class Memory:
    def __init__(self): self.doc=None; self.audit={}
    def __call__(self): return self
    def __enter__(self): self.saved=copy.deepcopy((self.doc,self.audit)); return self
    def __exit__(self,kind,*args):
        if kind: self.doc,self.audit=self.saved
    def execute(self,sql,params=()):
        if sql.startswith('CREATE'): return Result()
        if sql.startswith('INSERT INTO trip_document'):
            if self.doc is None:self.doc=copy.deepcopy(params[0].obj)
            return Result()
        if sql.startswith('SELECT body'):return Result((copy.deepcopy(self.doc),))
        if sql.startswith('SELECT 1'):return Result((1,) if params[0] in self.audit else None)
        if sql.startswith('UPDATE trip_document'):self.doc=copy.deepcopy(params[0].obj);return Result()
        if sql.startswith('INSERT INTO trip_audit'):self.audit[params[0]]=params[1];return Result()
        raise AssertionError('Unexpected test SQL')

class TestSharedPlanner(unittest.TestCase):
    def setUp(self):
        m._initialized=False;self.db=Memory();m.connect=self.db
        m._attempts.clear()
        m.app.config.update(TESTING=True,SESSION_COOKIE_SECURE=False)
        salt='ab'*16
        for name in m.PEOPLE:os.environ['PASSWORD_'+name.upper()]=salt+':'+hashlib.pbkdf2_hmac('sha256',b'test-password-only',bytes.fromhex(salt),600000).hex()
        self.l=m.app.test_client();self.k=m.app.test_client()
        self.csrf={}
        for c,name in [(self.l,'Leszek'),(self.k,'Karolina')]:
            r=c.get('/login');token=re.search(r'name="csrf" value="([^"]+)"',r.text).group(1)
            self.assertEqual(c.post('/login',data={'user':name,'password':'test-password-only','csrf':token}).status_code,302)
            with c.session_transaction() as session:self.csrf[id(c)]=session['csrf']
        self.l.get('/api/state')
    def post(self,c,operation,changes):
        return c.post('/api/changes',json={'operation':operation,'changes':changes},headers={'X-CSRF-Token':self.csrf[id(c)]})
    def addition(self,ident='test-record'):
        x=copy.deepcopy(self.db.doc['expenses'][0]);x['id']=ident;x['amountMinor']=500;x['createdBy']='FAKE';x['updatedBy']='FAKE';return {'collection':'expenses','id':ident,'version':0,'value':x}
    def test_auth_and_csrf(self):
        client=m.app.test_client();self.assertEqual(client.get('/api/state').status_code,401)
        self.assertEqual(self.l.post('/api/changes',json={}).status_code,403)
        self.assertFalse(m.check_password('Leszek','wrong'))
        self.assertEqual(self.l.get('/').status_code,200)
    def test_two_people_and_retry(self):
        a=self.addition();r=self.post(self.l,'operation-one',[a]);self.assertEqual(r.status_code,200)
        doc=self.k.get('/api/state').json['state'];x=next(x for x in doc['expenses'] if x['id']==a['id']);self.assertEqual(x['createdBy'],'Leszek')
        rev=doc['revision'];self.assertEqual(self.post(self.l,'operation-one',[a]).json['state']['revision'],rev)
        b=self.addition('test-karolina');self.assertEqual(self.post(self.k,'operation-two',[b]).status_code,200)
        doc=self.l.get('/api/state').json['state'];self.assertEqual(next(x for x in doc['expenses'] if x['id']==b['id'])['createdBy'],'Karolina')
    def test_conflict_and_validation(self):
        old=copy.deepcopy(self.db.doc['expenses'][0]);old['amountMinor']=900
        change={'collection':'expenses','id':old['id'],'version':1,'value':old}
        self.assertEqual(self.post(self.l,'operation-first',[change]).status_code,200)
        self.assertEqual(self.post(self.k,'operation-stale',[change]).status_code,409)
        bad=self.addition('test-invalid');bad['value']['amountMinor']=-1
        self.assertEqual(self.post(self.l,'operation-invalid',[bad]).status_code,400)
    def test_batch_is_atomic(self):
        before=copy.deepcopy(self.db.doc)
        bad=self.addition('test-invalid');bad['value']['currency']='BAD'
        self.assertEqual(self.post(self.l,'operation-batch',[self.addition(),bad]).status_code,400)
        self.assertEqual(self.db.doc,before)
    def test_notes_and_backup(self):
        change={'collection':'notes','id':'2026-10-28','before':None,'value':'Shared note'}
        self.assertEqual(self.post(self.k,'operation-note',[change]).status_code,200)
        self.assertEqual(self.l.get('/api/state').json['state']['notes']['2026-10-28'],'Shared note')
        r=self.l.get('/api/offline');self.assertEqual(r.status_code,200)
        self.assertIn('attachment',r.headers['Content-Disposition']);self.assertNotIn('cloud-config',r.text)
        self.assertNotIn(self.csrf[id(self.l)],r.text)
    def test_form_keeps_origin_without_leaking_external_referrer(self):
        response=m.app.test_client().get('/login')
        self.assertEqual(response.headers['Referrer-Policy'],'same-origin')
        self.assertIn("form-action 'self'",response.headers['Content-Security-Policy'])
        self.assertIn('name="csrf"',response.text)
    def test_both_logins_behind_internal_http_proxy(self):
        public='https://planner.example.test'
        # Browser sees HTTPS, while the trusted proxy forwards an internal host.
        internal='http://private-service:10000'
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':public,'RENDER':'true'}):
            for name in m.PEOPLE:
                with self.subTest(user=name):
                    c=m.app.test_client()
                    response=c.get('/login',base_url=internal)
                    token=re.search(r'name="csrf" value="([^"]+)"',response.text).group(1)
                    response=c.post('/login',base_url=internal,data={'user':name,'password':'test-password-only','csrf':token},headers={'Origin':public,'X-Forwarded-Proto':'http','X-Forwarded-Host':'private-service:10000'})
                    self.assertEqual(response.status_code,302,response.text)
                    self.assertEqual(response.headers['Location'],'/')
                    page=c.get('/',base_url=internal)
                    self.assertEqual(page.status_code,200)
                    self.assertIn(name,page.text)
    def test_same_origin_api_write_and_sync(self):
        public='https://planner.example.test'
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':public}):
            response=self.l.post('/api/changes',json={'operation':'origin-save-one','changes':[self.addition('origin-test-record')]},headers={'Origin':public,'X-CSRF-Token':self.csrf[id(self.l)]})
            self.assertEqual(response.status_code,200,response.text)
            state=self.k.get('/api/state').json['state']
            self.assertEqual(next(x for x in state['expenses'] if x['id']=='origin-test-record')['createdBy'],'Leszek')
    def test_foreign_null_and_spoofed_origins_still_rejected(self):
        public='https://planner.example.test'
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':public}):
            for origin in ('null','https://attacker.example','http://planner.example.test','https://planner.example.test.attacker.example','https://planner.example.test@attacker.example','https://planner.example.test:444','https://planner.example.test/path','https://planner.example.test, https://attacker.example',''):
                with self.subTest(origin=origin):
                    response=self.l.post('/api/changes',json={},headers={'Origin':origin,'X-CSRF-Token':self.csrf[id(self.l)],'X-Forwarded-Host':'attacker.example','X-Forwarded-Proto':'https'})
                    self.assertEqual(response.status_code,403)
    def test_same_origin_still_requires_csrf(self):
        public='https://planner.example.test'
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':public}):
            for token in ('','bad-token'):
                response=self.l.post('/api/changes',json={},headers={'Origin':public,'X-CSRF-Token':token})
                self.assertEqual(response.status_code,403)
    def test_origin_normalization_and_render_setting(self):
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':'','RENDER_EXTERNAL_URL':'https://Planner.Example.test/','RENDER':'true'}):
            with m.app.test_request_context('/',base_url='http://internal-host:10000'):
                self.assertTrue(m.allowed_origin('https://planner.example.test'))
                self.assertTrue(m.allowed_origin('https://planner.example.test:443'))
                self.assertFalse(m.allowed_origin('http://internal-host:10000'))
                self.assertFalse(m.allowed_origin('null'))
        with patch.dict(os.environ,{'PUBLIC_ORIGIN':'','RENDER_EXTERNAL_URL':'','RENDER':'true'}):
            with m.app.test_request_context('/'):
                self.assertFalse(m.allowed_origin('http://localhost'))

if __name__=='__main__':unittest.main()
