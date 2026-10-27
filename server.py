"""지역상생 협력 매칭 플랫폼. Local development server; not a production web server."""
import base64, contextlib, csv, datetime, hashlib, hmac, io, json, os, re, secrets, sqlite3, sys, time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('MATCH_DB_PATH', ROOT / 'data' / 'platform.sqlite3'))
HOST = os.environ.get('MATCH_HOST', '127.0.0.1')
PORT = int(os.environ.get('MATCH_PORT', '8765'))
ALLOWED_ORIGIN = os.environ.get('MATCH_ORIGIN', f'http://127.0.0.1:{PORT}')
SECURE_COOKIES = os.environ.get('MATCH_SECURE_COOKIES') == '1'
FIELDS = {'교육·역량 강화','독서·문화예술','공간·자원 공유','지역상생 공동사업','기타'}
STATUS = {'접수','내용 확인','부서 검토','협의 중','실행 예정','실행 완료','보완 요청','보류','연계 어려움','신청 철회'}
SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('applicant','staff','admin')), org TEXT NOT NULL DEFAULT '', alias TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', dept TEXT NOT NULL DEFAULT '', credential TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, privacy_version TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(ip TEXT PRIMARY KEY, count INTEGER NOT NULL, until INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL, title TEXT NOT NULL, field TEXT NOT NULL, program TEXT NOT NULL, timing TEXT NOT NULL, scale TEXT NOT NULL, details TEXT NOT NULL, resources TEXT NOT NULL, desired TEXT NOT NULL, recommendation TEXT NOT NULL, website TEXT NOT NULL, want_public INTEGER NOT NULL, name_public INTEGER NOT NULL, summary TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '접수', next_action TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '', reply TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS interests(proposal_id TEXT NOT NULL REFERENCES proposals(id), staff_id TEXT NOT NULL REFERENCES users(id), program TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(proposal_id,staff_id));
CREATE TABLE IF NOT EXISTS assignments(proposal_id TEXT NOT NULL REFERENCES proposals(id), staff_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(proposal_id,staff_id));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT,proposal_id TEXT NOT NULL REFERENCES proposals(id),author TEXT NOT NULL REFERENCES users(id),body TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created TEXT NOT NULL);
'''
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
def hashkey(key, salt=None):
    salt = salt or secrets.token_bytes(16)
    dig = hashlib.scrypt(key.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
    return base64.urlsafe_b64encode(salt+dig).decode()
def verify(key, stored):
    try:
        raw=base64.urlsafe_b64decode(stored); return hmac.compare_digest(hashkey(key,raw[:16]),stored)
    except (ValueError,TypeError): return False
def thash(token): return hashlib.sha256(token.encode()).hexdigest()
def ident(prefix): return prefix+secrets.token_hex(8).upper()
def privacy_notice():
    path=os.environ.get('MATCH_PRIVACY_NOTICE_FILE','');version=os.environ.get('MATCH_PRIVACY_VERSION','').strip()
    if os.environ.get('MATCH_REGISTRATION_ENABLED')!='1' or not path or not version:return None
    try: text=Path(path).read_text(encoding='utf-8').strip()
    except OSError:return None
    if not 50<=len(text)<=8000 or len(version)>80:return None
    return {'version':version,'text':text}
@contextlib.contextmanager
def db():
    DB.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=15000')
    try: yield c; c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
def init():
    with db() as c: c.executescript(SCHEMA)
    try: os.chmod(DB,0o600);os.chmod(DB.parent,0o700)
    except OSError: pass
def audit(c,actor,action,target): c.execute('INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)',(actor,action,target,now()))
def clean(value,maxlen,required=False):
    if not isinstance(value,str): raise Error(400,'입력값을 확인해 주세요.')
    v=value.strip()
    if len(v)>maxlen or (required and not v): raise Error(400,'필수 항목 또는 입력 길이를 확인해 주세요.')
    return v
class Error(Exception):
    def __init__(self,status,msg): self.status=status;self.msg=msg

def public_row(c,r):
    count=c.execute('SELECT COUNT(*) FROM interests i JOIN users u ON u.id=i.staff_id WHERE i.proposal_id=? AND u.active=1',(r['id'],)).fetchone()[0]
    name=c.execute('SELECT org FROM users WHERE id=?',(r['owner'],)).fetchone()[0] if r['name_public'] else '지역 기업·기관·소상공인'
    return dict(id=r['id'],title=r['title'],field=r['field'],program=r['program'],timing=r['timing'],scale=r['scale'],summary=r['summary'],status=r['status'],name=name,count=count,created=r['created'])
def private_row(r): return {k:r[k] for k in ('id','owner','kind','title','field','program','timing','scale','details','resources','desired','recommendation','website','want_public','name_public','summary','approved','status','next_action','due','reply','created','updated')}

def proposal_input(d):
    title=clean(d.get('title',''),100,True);field=clean(d.get('field',''),50,True)
    if field not in FIELDS: raise Error(400,'협력 분야를 선택해 주세요.')
    kind=clean(d.get('kind',''),20,True)
    if kind not in ('협력 제안','관심 등록','대상 추천'):raise Error(400,'신청 유형을 확인해 주세요.')
    public=d.get('want_public') is True;namepublic=d.get('name_public') is True
    summary=clean(d.get('summary',''),600,public)
    details=clean(d.get('details',''),2000,True)
    recommendation=clean(d.get('recommendation',''),100,kind=='대상 추천')
    website=clean(d.get('website',''),250,kind=='대상 추천')
    if website and not re.match(r'^https?://[^\s]+$',website):raise Error(400,'홈페이지 주소를 확인해 주세요.')
    return (kind,title,field,clean(d.get('program',''),100,True),clean(d.get('timing',''),80),clean(d.get('scale',''),100),details,clean(d.get('resources',''),300),clean(d.get('desired',''),600),recommendation,website,int(public),int(namepublic),summary)

class Handler(BaseHTTPRequestHandler):
    server_version='MatchingPlatform/0.1'
    def log_message(self,fmt,*args): pass
    def respond(self,status,obj,headers=None):
        if status<400 and getattr(self,'_db',None) is not None:self._db.commit()
        data=json.dumps(obj,ensure_ascii=False).encode('utf-8');self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers();self.wfile.write(data)
    def content(self):
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<0 or n>16384: raise ValueError()
            v=json.loads(self.rfile.read(n))
            if not isinstance(v,dict): raise ValueError()
            return v
        except (ValueError,json.JSONDecodeError): raise Error(400,'요청 내용을 확인해 주세요.')
    def cookie(self):
        jar=cookies.SimpleCookie()
        try:jar.load(self.headers.get('Cookie',''))
        except cookies.CookieError:return ''
        return jar['match_session'].value if 'match_session' in jar else ''
    def session(self,c):
        token=self.cookie()
        if not token:return None
        return c.execute('SELECT u.id,u.role,u.org,u.alias,u.dept,u.active,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',(thash(token),int(time.time()))).fetchone()
    def require(self,c,*roles):
        u=self.session(c)
        if not u:raise Error(401,'로그인이 필요합니다.')
        if u['role'] not in roles:raise Error(403,'접근 권한이 없습니다.')
        return u
    def origin(self):
        if self.headers.get('Origin')!=ALLOWED_ORIGIN:raise Error(403,'허용되지 않은 요청입니다.')
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise Error(415,'요청 형식이 올바르지 않습니다.')
    def csrf(self,c):
        u=self.session(c)
        if not u or not hmac.compare_digest(u['csrf'],self.headers.get('X-CSRF-Token','')):raise Error(403,'요청을 확인할 수 없습니다. 다시 로그인해 주세요.')
        return u
    def session_cookie(self,token,delete=False):
        return 'match_session='+token+'; Path=/; HttpOnly; SameSite=Strict; '+('Max-Age=0' if delete else 'Max-Age=43200')+('; Secure' if SECURE_COOKIES else '')
    def create_session(self,c,uid):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(thash(token),uid,csrf,int(time.time())+43200));return token,csrf
    def do_GET(self):
        try:self.get()
        except Error as e:self.respond(e.status,{'error':e.msg})
        except Exception:self.respond(500,{'error':'처리 중 오류가 발생했습니다.'})
    def get(self):
        path=urlsplit(self.path).path
        if path=='/':
            data=(ROOT/'static'/'index.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");self.end_headers();self.wfile.write(data);return
        with db() as c:
            self._db=c
            u=self.session(c)
            if path=='/api/session':return self.respond(200,{'user':dict(u) if u else None})
            if path=='/api/notice':return self.respond(200,{'notice':privacy_notice()})
            if path=='/api/public':
                rows=c.execute("SELECT * FROM proposals WHERE want_public=1 AND approved=1 AND status!='신청 철회' ORDER BY created DESC").fetchall();return self.respond(200,{'items':[public_row(c,r) for r in rows]})
            if path=='/api/mine':
                u=self.require(c,'applicant');rows=c.execute('SELECT * FROM proposals WHERE owner=? ORDER BY created DESC',(u['id'],)).fetchall();return self.respond(200,{'items':[private_row(r) for r in rows]})
            if path=='/api/staff':
                u=self.require(c,'staff');rows=c.execute('SELECT * FROM proposals WHERE id IN (SELECT proposal_id FROM interests WHERE staff_id=?) OR id IN (SELECT proposal_id FROM assignments WHERE staff_id=?)',(u['id'],u['id'])).fetchall();out=[]
                for r in rows:
                    if c.execute('SELECT 1 FROM assignments WHERE proposal_id=? AND staff_id=?',(r['id'],u['id'])).fetchone():
                        v=private_row(r);owner=c.execute('SELECT org,alias,contact FROM users WHERE id=?',(r['owner'],)).fetchone();v['applicant']=dict(owner);v['assigned']=True
                    else:
                        # Previously interested proposals which were later unpublished are not exposed.
                        if not(r['want_public'] and r['approved'] and r['status']!='신청 철회'):continue
                        v=public_row(c,r);v['assigned']=False
                    out.append(v)
                return self.respond(200,{'items':out})
            if path=='/api/admin/export':
                self.require(c,'admin');params=parse_qs(urlsplit(self.path).query);month=params.get('month',[''])[0]
                if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):raise Error(400,'기준 월을 확인해 주세요.')
                output=io.StringIO(newline='');writer=csv.writer(output)
                def safe(v):
                    val=str(v or '')
                    return "'"+val if val.startswith(('=','+','-','@','\t','\r')) else val
                writer.writerow(['접수번호','공개 표시명','제안 제목','공개 요약','분야','관련 사업','상태','연결 의사 수','접수일'])
                for r in c.execute("SELECT * FROM proposals WHERE want_public=1 AND approved=1 AND status!='신청 철회' ORDER BY created DESC").fetchall():
                    local=datetime.datetime.fromisoformat(r['created']).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
                    if local.strftime('%Y-%m')!=month:continue
                    v=public_row(c,r);writer.writerow([safe(x) for x in (v['id'],v['name'],v['title'],v['summary'],v['field'],v['program'],v['status'],v['count'],local.strftime('%Y-%m-%d'))])
                body=('\ufeff'+output.getvalue()).encode('utf-8');self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8');self.send_header('Content-Disposition','attachment; filename="matching-report.csv"');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body);return
            if path=='/api/admin':
                self.require(c,'admin');rows=c.execute('SELECT p.*,u.org,u.alias,u.contact FROM proposals p JOIN users u ON p.owner=u.id ORDER BY p.created DESC').fetchall();items=[]
                for r in rows:
                    v=private_row(r);v.update(org=r['org'],alias=r['alias'],contact=r['contact']);v['interests']=[dict(x) for x in c.execute('SELECT i.staff_id,i.program,i.message,u.alias,u.dept FROM interests i JOIN users u ON u.id=i.staff_id WHERE i.proposal_id=? AND u.active=1',(r['id'],)).fetchall()];v['assigned']=[x[0] for x in c.execute('SELECT staff_id FROM assignments WHERE proposal_id=?',(r['id'],))];v['notes']=[dict(x) for x in c.execute('SELECT n.body,n.created,u.alias FROM notes n JOIN users u ON u.id=n.author WHERE proposal_id=? ORDER BY n.id DESC',(r['id'],))];items.append(v)
                staff=[dict(x) for x in c.execute("SELECT id,alias,dept,active FROM users WHERE role='staff' ORDER BY alias")];return self.respond(200,{'items':items,'staff':staff})
            raise Error(404,'페이지를 찾을 수 없습니다.')
    def do_POST(self):
        try:self.origin();self.post()
        except Error as e:self.respond(e.status,{'error':e.msg})
        except (sqlite3.IntegrityError,ValueError):self.respond(400,{'error':'요청 내용을 확인해 주세요.'})
        except Exception:self.respond(500,{'error':'처리 중 오류가 발생했습니다.'})
    def post(self):
        path=urlsplit(self.path).path;d=self.content()
        with db() as c:
            self._db=c
            if path=='/api/register':
                org=clean(d.get('org',''),80,True);alias=clean(d.get('alias',''),30,True);contact=clean(d.get('contact',''),120,True)
                # This switch remains off until the institution approves and installs its privacy notice.
                notice=privacy_notice()
                if not notice: raise Error(503,'신청 접수 준비 중입니다. 개인정보 안내 및 운영 설정을 확인해 주세요.')
                if d.get('privacy_ack') is not True or d.get('privacy_version')!=notice['version']:raise Error(400,'최신 개인정보 안내 확인이 필요합니다.')
                uid=ident('YS-');key=secrets.token_urlsafe(24);c.execute('INSERT INTO users(id,role,org,alias,contact,credential,created,privacy_version) VALUES(?,?,?,?,?,?,?,?)',(uid,'applicant',org,alias,contact,hashkey(key),now(),notice['version']));token,csrf=self.create_session(c,uid);audit(c,uid,'register',uid);return self.respond(201,{'id':uid,'key':key,'csrf':csrf}, {'Set-Cookie':self.session_cookie(token)})
            if path=='/api/login':
                uid=clean(d.get('id',''),40,True).upper();key=clean(d.get('key',''),256,True);ip=self.client_address[0];t=int(time.time());row=c.execute('SELECT count,until FROM attempts WHERE ip=?',(ip,)).fetchone()
                if row and row['count']>=10 and row['until']>t:raise Error(429,'로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.')
                u=c.execute('SELECT id,credential,active FROM users WHERE id=?',(uid,)).fetchone();valid=verify(key,u['credential']) if u else verify(key,hashkey('dummy-key'))
                if not u or not valid or not u['active']:
                    if not row or row['until']<=t:c.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(ip,1,t+900))
                    else:c.execute('UPDATE attempts SET count=count+1 WHERE ip=?',(ip,))
                    # Persist failure counter before returning.
                    c.commit();raise Error(401,'ID 또는 인증키를 확인해 주세요.')
                c.execute('DELETE FROM attempts WHERE ip=?',(ip,));token,csrf=self.create_session(c,uid);return self.respond(200,{'csrf':csrf},{'Set-Cookie':self.session_cookie(token)})
            u=self.csrf(c)
            if path=='/api/logout':c.execute('DELETE FROM sessions WHERE token_hash=?',(thash(self.cookie()),));return self.respond(200,{'ok':True},{'Set-Cookie':self.session_cookie('',True)})
            if path=='/api/proposal':
                u=self.require(c,'applicant');data=proposal_input(d);pid=ident('P-');t=now();c.execute('INSERT INTO proposals(id,owner,kind,title,field,program,timing,scale,details,resources,desired,recommendation,website,want_public,name_public,summary,created,updated) VALUES('+','.join('?'*18)+')',(pid,u['id'],*data,t,t));audit(c,u['id'],'proposal_created',pid);return self.respond(201,{'id':pid})
            if path=='/api/proposal/update':
                u=self.require(c,'applicant');pid=clean(d.get('id',''),40,True);row=c.execute('SELECT * FROM proposals WHERE id=? AND owner=?',(pid,u['id'])).fetchone()
                if not row:raise Error(404,'신청을 찾을 수 없습니다.')
                if row['status']=='신청 철회':raise Error(400,'철회된 신청은 수정할 수 없습니다.')
                data=proposal_input(d);c.execute("UPDATE proposals SET kind=?,title=?,field=?,program=?,timing=?,scale=?,details=?,resources=?,desired=?,recommendation=?,website=?,want_public=?,name_public=?,summary=?,approved=0,status='내용 확인',updated=? WHERE id=?",(*data,now(),pid));audit(c,u['id'],'proposal_updated_reapproval',pid);return self.respond(200,{'ok':True})
            if path=='/api/proposal/withdraw':
                u=self.require(c,'applicant');pid=clean(d.get('id',''),40,True);row=c.execute('SELECT * FROM proposals WHERE id=? AND owner=?',(pid,u['id'])).fetchone()
                if not row:raise Error(404,'신청을 찾을 수 없습니다.')
                all_=d.get('withdraw_all') is True;c.execute('UPDATE proposals SET want_public=0,approved=0,status=?,updated=? WHERE id=?',('신청 철회' if all_ else row['status'],now(),pid));audit(c,u['id'],'withdraw_all' if all_ else 'withdraw_public',pid);return self.respond(200,{'ok':True})
            if path in ('/api/interest','/api/interest/withdraw'):
                u=self.require(c,'staff');pid=clean(d.get('id',''),40,True);r=c.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
                if path=='/api/interest/withdraw':c.execute('DELETE FROM interests WHERE proposal_id=? AND staff_id=?',(pid,u['id']));audit(c,u['id'],'interest_withdrawn',pid);return self.respond(200,{'ok':True})
                if not r or not r['want_public'] or not r['approved'] or r['status']=='신청 철회':raise Error(404,'공개 중인 제안이 아닙니다.')
                c.execute('INSERT INTO interests VALUES(?,?,?,?,?) ON CONFLICT(proposal_id,staff_id) DO UPDATE SET program=excluded.program,message=excluded.message',(pid,u['id'],clean(d.get('program',''),100,True),clean(d.get('message',''),1000,True),now()));audit(c,u['id'],'interest_upserted',pid);return self.respond(200,{'ok':True})
            if path=='/api/note':
                u=self.require(c,'staff');pid=clean(d.get('id',''),40,True)
                if not c.execute('SELECT 1 FROM assignments WHERE proposal_id=? AND staff_id=?',(pid,u['id'])).fetchone():raise Error(403,'배정된 담당자만 기록할 수 있습니다.')
                c.execute('INSERT INTO notes(proposal_id,author,body,created) VALUES(?,?,?,?)',(pid,u['id'],clean(d.get('body',''),1500,True),now()));audit(c,u['id'],'note_created',pid);return self.respond(200,{'ok':True})
            if path.startswith('/api/admin/'):
                u=self.require(c,'admin');pid=clean(d.get('id',''),40,True)
                if path=='/api/admin/staff':
                    active=d.get('active') is True;staff=c.execute("SELECT id FROM users WHERE id=? AND role='staff'",(pid,)).fetchone()
                    if not staff:raise Error(404,'직원을 찾을 수 없습니다.')
                    c.execute('UPDATE users SET active=? WHERE id=?',(int(active),pid))
                    if not active:c.execute('DELETE FROM sessions WHERE user_id=?',(pid,));c.execute('DELETE FROM assignments WHERE staff_id=?',(pid,));c.execute('DELETE FROM interests WHERE staff_id=?',(pid,))
                    audit(c,u['id'],'staff_activated' if active else 'staff_deactivated',pid);return self.respond(200,{'ok':True})
                r=c.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
                if not r:raise Error(404,'신청을 찾을 수 없습니다.')
                if path=='/api/admin/approval':
                    ok=d.get('approved') is True
                    if ok and (not r['want_public'] or r['status']=='신청 철회'):raise Error(400,'공개 요청 중인 제안만 승인할 수 있습니다.')
                    c.execute('UPDATE proposals SET approved=?,updated=? WHERE id=?',(int(ok),now(),pid));audit(c,u['id'],'public_approved' if ok else 'public_revoked',pid);return self.respond(200,{'ok':True})
                if path=='/api/admin/assign':
                    sid=clean(d.get('staff_id',''),40,True)
                    if not c.execute("SELECT 1 FROM interests i JOIN users u ON u.id=i.staff_id WHERE i.proposal_id=? AND i.staff_id=? AND u.active=1 AND u.role='staff'",(pid,sid)).fetchone():raise Error(400,'연결 의사를 등록한 승인 직원만 배정할 수 있습니다.')
                    if d.get('assigned') is True:c.execute('INSERT OR IGNORE INTO assignments VALUES(?,?)',(pid,sid))
                    else:c.execute('DELETE FROM assignments WHERE proposal_id=? AND staff_id=?',(pid,sid))
                    audit(c,u['id'],'assignment_changed',pid);return self.respond(200,{'ok':True})
                if path=='/api/admin/update':
                    status=clean(d.get('status',''),30,True)
                    if status not in STATUS:raise Error(400,'처리 상태를 확인해 주세요.')
                    reply=clean(d.get('reply',''),1000)
                    if status in ('보완 요청','보류','연계 어려움') and not reply:raise Error(400,'신청자 안내에 사유를 입력해 주세요.')
                    due=clean(d.get('due',''),10)
                    if due and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',due):raise Error(400,'회신기한을 확인해 주세요.')
                    c.execute('UPDATE proposals SET status=?,next_action=?,due=?,reply=?,approved=CASE WHEN ?=? THEN 0 ELSE approved END,updated=? WHERE id=?',(status,clean(d.get('next_action',''),200),due,reply,status,'신청 철회',now(),pid));note=clean(d.get('note',''),1500)
                    if note:c.execute('INSERT INTO notes(proposal_id,author,body,created) VALUES(?,?,?,?)',(pid,u['id'],note,now()))
                    audit(c,u['id'],'status_updated',pid);return self.respond(200,{'ok':True})
            raise Error(404,'요청 경로를 찾을 수 없습니다.')

def main():
    init()
    if len(sys.argv)>1 and sys.argv[1]=='provision':
        import getpass
        if len(sys.argv)!=4 or sys.argv[2] not in ('staff','admin'):sys.exit('사용법: python server.py provision staff|admin 별칭')
        role,alias=sys.argv[2:];dept=input('부서명: ').strip();secret=getpass.getpass('인증키 (최소 16자, 화면에 표시되지 않음): ')
        if len(secret)<16:sys.exit('인증키는 16자 이상이어야 합니다.')
        uid=ident('EMP-' if role=='staff' else 'ADM-')
        with db() as c:c.execute('INSERT INTO users(id,role,alias,dept,credential,created) VALUES(?,?,?,?,?,?)',(uid,role,alias,dept,hashkey(secret),now()))
        print('발급 ID:',uid);return
    if HOST not in ('127.0.0.1','localhost','::1'):sys.exit('개발 서버는 로컬에서만 실행할 수 있습니다. 실제 공개 운영은 검토된 배포 환경을 사용하세요.')
    print(f'로컬 서버: http://{HOST}:{PORT}  | 접수 허용: {os.environ.get("MATCH_REGISTRATION_ENABLED")=="1"}',flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
if __name__=='__main__':main()
