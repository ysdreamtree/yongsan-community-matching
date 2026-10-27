import contextlib,http.client,json,os,secrets,socket,subprocess,sys,tempfile,time,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
tmp=tempfile.TemporaryDirectory();os.environ['MATCH_DB_PATH']=str(Path(tmp.name)/'db.sqlite3');os.environ['MATCH_REGISTRATION_ENABLED']='1';notice_path=Path(tmp.name)/'notice.txt';notice_path.write_text('테스트용 개인정보 안내입니다. 실제 개인정보를 넣지 않습니다. 수집 목적과 보유 기간, 권리 사항을 포함해야 합니다.',encoding='utf-8');os.environ['MATCH_PRIVACY_NOTICE_FILE']=str(notice_path);os.environ['MATCH_PRIVACY_VERSION']='test-v1'
s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close();origin=f'http://127.0.0.1:{port}';os.environ['MATCH_PORT']=str(port);os.environ['MATCH_ORIGIN']=origin
sys.path.insert(0,str(root));import server
server.init()
with server.db() as c:
 for uid,role in [('ADM-TEST','admin'),('EMP-ONE','staff'),('EMP-TWO','staff')]:c.execute('INSERT INTO users(id,role,alias,dept,credential,created) VALUES(?,?,?,?,?,?)',(uid,role,uid,'test',server.hashkey('long-test-password-123'),server.now()))
proc=subprocess.Popen([sys.executable,str(root/'server.py')],env=os.environ,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for _ in range(100):
 try:
  c=http.client.HTTPConnection('127.0.0.1',port,timeout=1);c.request('GET','/api/session');c.getresponse().read();c.close();break
 except OSError:time.sleep(.03)
class Client:
 def __init__(self):self.cookie='';self.csrf=''
 def req(self,path,obj=None,origin_override=None,include_csrf=True):
  c=http.client.HTTPConnection('127.0.0.1',port,timeout=3);h={}
  if self.cookie:h['Cookie']=self.cookie
  if obj is not None:
   h.update({'Content-Type':'application/json','Origin':origin if origin_override is None else origin_override})
   if include_csrf:h['X-CSRF-Token']=self.csrf
  c.request('POST' if obj is not None else 'GET',path,body=None if obj is None else json.dumps(obj).encode(),headers=h);r=c.getresponse();raw=r.read();cookie=r.getheader('Set-Cookie');status=r.status;c.close()
  if cookie:self.cookie=cookie.split(';')[0]
  try:data=json.loads(raw)
  except json.JSONDecodeError:data=raw.decode()
  return status,data
 def login(self,id,key):
  status,d=self.req('/api/login',{'id':id,'key':key});assert status==200,(status,d);self.csrf=d['csrf']
 def register(self,org):
  status,d=self.req('/api/register',{'org':org,'alias':'예시','contact':'private@example.invalid','privacy_ack':True,'privacy_version':'test-v1'});assert status==201,(status,d);self.csrf=d['csrf'];return d
class Tests(unittest.TestCase):
 def test_complete_workflow_and_authorization(self):
  client=Client();a=client.register('가상기업');outsider=Client();outsider.register('타인')
  admin=Client();admin.login('ADM-TEST','long-test-password-123');staff=Client();staff.login('EMP-ONE','long-test-password-123');other=Client();other.login('EMP-TWO','long-test-password-123')
  p={'kind':'협력 제안','title':'함께하는 책','field':'독서·문화예술','program':'독서경영','timing':'','scale':'','details':'PRIVATE DETAIL','resources':'','desired':'','recommendation':'','website':'','want_public':True,'name_public':False,'summary':'공개 요약'}
  status,created=client.req('/api/proposal',p);self.assertEqual(status,201);pid=created['id']
  self.assertEqual(outsider.req('/api/proposal/update',{**p,'id':pid})[0],404)
  self.assertEqual(staff.req('/api/interest',{'id':pid,'program':'독서','message':'관심'})[0],404)
  self.assertEqual(admin.req('/api/admin/approval',{'id':pid,'approved':True})[0],200)
  export_status,export=admin.req('/api/admin/export?month='+__import__('datetime').datetime.now(__import__('datetime').timezone(__import__('datetime').timedelta(hours=9))).strftime('%Y-%m'));self.assertEqual(export_status,200);self.assertNotIn('private@example',export);self.assertEqual(staff.req('/api/admin/export?month=2026-10')[0],403)
  pub=Client().req('/api/public')[1];self.assertEqual(len(pub['items']),1);self.assertNotIn('PRIVATE DETAIL',json.dumps(pub));self.assertNotIn('private@example',json.dumps(pub));self.assertNotIn('가상기업',json.dumps(pub))
  for _ in range(2):self.assertEqual(staff.req('/api/interest',{'id':pid,'program':'독서','message':'총괄 메시지'})[0],200)
  pub=Client().req('/api/public')[1];self.assertEqual(pub['items'][0]['count'],1);self.assertNotIn('총괄 메시지',json.dumps(pub,ensure_ascii=False))
  self.assertEqual(other.req('/api/note',{'id':pid,'body':'작성'})[0],403)
  self.assertEqual(staff.req('/api/admin/assign',{'id':pid,'staff_id':'EMP-ONE','assigned':True})[0],403)
  self.assertEqual(admin.req('/api/admin/assign',{'id':pid,'staff_id':'EMP-ONE','assigned':True})[0],200)
  row=staff.req('/api/staff')[1]['items'][0];self.assertEqual(row['applicant']['contact'],'private@example.invalid');self.assertEqual(row['details'],'PRIVATE DETAIL')
  self.assertEqual(other.req('/api/staff')[1]['items'],[]);self.assertEqual(staff.req('/api/note',{'id':pid,'body':'진행 확인'})[0],200)
  self.assertEqual(client.req('/api/proposal/update',{**p,'id':pid,'title':'변경'})[0],200);self.assertEqual(Client().req('/api/public')[1]['items'],[])
  self.assertEqual(admin.req('/api/admin/approval',{'id':pid,'approved':True})[0],200)
  self.assertEqual(client.req('/api/proposal/withdraw',{'id':pid,'withdraw_all':False})[0],200);self.assertEqual(Client().req('/api/public')[1]['items'],[])
  self.assertEqual(admin.req('/api/admin/approval',{'id':pid,'approved':True})[0],400)
  client2=Client();client2.login(a['id'],a['key']);self.assertEqual(client2.req('/api/mine')[1]['items'][0]['id'],pid)
  self.assertEqual(client2.req('/api/logout',{})[0],200);self.assertEqual(client2.req('/api/mine')[0],401)
 def test_csrf_and_role(self):
  c=Client();c.login('EMP-ONE','long-test-password-123')
  self.assertEqual(c.req('/api/interest',{},origin_override='http://attacker.invalid')[0],403)
  self.assertEqual(c.req('/api/interest',{},include_csrf=False)[0],403)
  self.assertEqual(c.req('/api/admin',None)[0],403)
 def test_privacy_gate_defaults_closed(self):
  previous=os.environ.pop('MATCH_REGISTRATION_ENABLED',None)
  try:self.assertIsNone(server.privacy_notice())
  finally:
   if previous is not None:os.environ['MATCH_REGISTRATION_ENABLED']=previous
 def test_z_database_survives_restart(self):
  global proc
  c=Client();a=c.register('재시작 테스트')
  proc.terminate();proc.wait(timeout=5)
  proc=subprocess.Popen([sys.executable,str(root/'server.py')],env=os.environ,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for _ in range(100):
   try:
    x=http.client.HTTPConnection('127.0.0.1',port,timeout=1);x.request('GET','/api/session');x.getresponse().read();x.close();break
   except OSError:time.sleep(.03)
  new=Client();new.login(a['id'],a['key']);self.assertEqual(new.req('/api/session')[1]['user']['org'],'재시작 테스트')
 def test_validation(self):
  c=Client();c.register('예시');p={'kind':'대상 추천','title':'추천','field':'기타','program':'사업 미지정','details':'필요','want_public':False,'name_public':False,'summary':'','timing':'','scale':'','resources':'','desired':'','recommendation':'대상','website':'javascript:alert(1)'}
  self.assertEqual(c.req('/api/proposal',p)[0],400)
if __name__=='__main__':
 try:unittest.main(verbosity=2)
 finally:proc.terminate();proc.wait(timeout=5);tmp.cleanup()
