"""Run during build. Uses a transactional memory adapter, never the live database."""
import copy, hashlib, os, re, unittest
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

if __name__=='__main__':unittest.main()
