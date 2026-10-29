"""Direct YUNMA_APP client, completed from the user's eschool.py.

Standard-library transport, manually entered captcha, no request/credential logging.
"""
import base64
import hashlib
import http.cookiejar
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from .config import ROOT
from .adapters.base import SourceError
from .security_token import SecurityTokenCodec
from .app_sign import sign_request
from .tenant_context import CURRENT, ScopedLock

STATE = ROOT / 'data' / 'eschool_session.json'
LOCK = ScopedLock()
APP = 'https://compus.xiaofubao.com'
ELECTRIC = 'https://application.xiaofubao.com'


class SessionExpired(SourceError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EschoolClient:
    def __init__(self, path=STATE):
        self.path = path
        self.tenant=CURRENT.get() if path==STATE else None
        if self.tenant:
            from portal.accounts.state import read_state
            self.state=read_state(self.tenant[0])
        else:self.state = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        self.state.setdefault('device_id', 'ym-' + uuid.uuid4().hex)
        self.state.setdefault('app_version','730')
        self.state.setdefault('app_schema_version','7.3.6')
        self.jar = http.cookiejar.CookieJar()
        for cookie in self.state.get('cookies', []):
            self.jar.set_cookie(http.cookiejar.Cookie(**cookie))
        ca = ROOT / 'integrations/php/certs/cacert.pem'
        ca_path=os.environ.get('YSP_CA_BUNDLE') or (str(ca) if ca.is_file() else None)
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_path)),
                                                 urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def save(self):
        self.state['cookies'] = [dict(version=c.version, name=c.name, value=c.value, port=c.port,
            port_specified=c.port_specified, domain=c.domain, domain_specified=c.domain_specified,
            domain_initial_dot=c.domain_initial_dot, path=c.path, path_specified=c.path_specified,
            secure=c.secure, expires=c.expires, discard=c.discard, comment=c.comment,
            comment_url=c.comment_url, rest=dict(c._rest), rfc2109=c.rfc2109) for c in self.jar]
        if self.tenant:
            from portal.accounts.state import write_state
            write_state(self.tenant[0],self.state)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)

    def request(self, path, data=None, host=APP, form=False, redirect=False):
        if host not in (APP, ELECTRIC, 'https://auth.xiaofubao.com') or not path.startswith('/'):
            raise SourceError('无效的易校园接口地址')
        payload = {'appVersion':self.state['app_version'], 'nt':int(time.time()*1000), 'deviceId':self.state['device_id'],
                   'platform':'YUNMA_APP', 'schoolCode':self.state.get('school_code','')}
        if self.state.get('token'): payload['token'] = self.state['token']
        if self.state.get('ym_id'): payload['ymId'] = self.state['ym_id']
        payload.update(data or {})
        url = host + path
        headers = {'User-Agent':'Mozilla/5.0 (Linux; Android 12; Android for arm64; wv) AppleWebKit/537.36 Version/4.0 Chrome/126.0.6478.186 Mobile Safari/537.36 ZJYXYwebviewbroswer ZJYXYAndroid tourCustomer/yunmaapp.NET/'+self.state['app_schema_version']+'/'+self.state['device_id'],
                   'deviceid':self.state['device_id'], 'platform':'YUNMA_APP', 'Accept':'application/json'}
        if host == APP: headers['sign'] = sign_request(payload)
        if redirect:
            url += '?' + urllib.parse.urlencode(payload)
            raw = None
        else:
            raw = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json; charset=UTF-8'
        req = urllib.request.Request(url, data=raw, headers=headers, method='GET' if redirect else 'POST')
        try:
            response = self.opener.open(req, timeout=25)
        except urllib.error.HTTPError as exc:
            if redirect and exc.code in (301,302,303,307,308):
                return exc.headers.get('Location','')
            raise SourceError(f'易校园接口 HTTP {exc.code}：{path}') from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise self.network_error(exc,host,path) from None
        try:
            with response:
                text = response.read().decode('utf-8-sig','replace')
        except (TimeoutError,OSError,http.client.IncompleteRead) as exc:
            raise self.network_error(exc,host,path) from None
        if redirect: raise SourceError('授权服务未返回预期跳转，请重新登录易校园。')
        if not text.strip(): raise SourceError(f'易校园返回空响应：{path}')
        try: result = json.loads(text)
        except ValueError: raise SourceError(f'易校园返回非JSON内容：{path}') from None
        if not isinstance(result,dict): raise SourceError('易校园响应格式异常')
        if str(result.get('statusCode')) == '204': raise SessionExpired('易校园会话已失效，请重新登录。')
        if result.get('success') is not True:
            message = str(result.get('message') or result.get('msg') or '服务器拒绝请求')[:200]
            raise SourceError(f'{message}（接口 {path}）')
        return result

    def network_error(self, exc, host, path):
        reason = getattr(exc,'reason',exc)
        if isinstance(reason,ssl.SSLCertVerificationError):
            detail = 'TLS证书校验失败（code='+str(reason.verify_code)+'）'
        elif isinstance(reason,ssl.SSLError): detail = 'TLS握手失败'
        elif isinstance(reason,TimeoutError): detail = '网络连接超时'
        elif isinstance(reason,http.client.IncompleteRead): detail = '服务器响应传输中断'
        else: detail = '网络连接失败（'+type(reason).__name__+'）'
        hostname = urllib.parse.urlsplit(host).hostname
        self.state['network_diagnostic'] = {'host':hostname,'path':path,'type':type(reason).__name__}
        self.save()
        return SourceError(detail+'，接口 '+hostname+path+'。请稍后重试，程序未关闭证书校验。')

    def captcha(self, phone, school):
        if not re.fullmatch(r'1\d{10}', phone): raise SourceError('请输入11位手机号。')
        if not re.fullmatch(r'\d{1,20}', school): raise SourceError('请输入学校代码。')
        self.state.update(pending_phone=phone, pending_school_code=school)
        token = self.request('/common/security/token', {'sceneCode':'app_user_login','schoolCode':school,'token':''})
        self.state['security_token'] = token['data']['securityToken']
        image = self.request('/common/security/imageCaptcha', {'securityToken':self.state['security_token'],'schoolCode':school,'token':''})['data']
        image = image.split(',',1)[-1]
        base64.b64decode(image, validate=True)
        self.save()
        return {'image':'data:image/jpeg;base64,'+image, 'level':token['data'].get('level')}

    def send_code(self, captcha):
        if time.time() - self.state.get('sms_sent_at',0) < 60: raise SourceError('短信已发送，请至少等待60秒再重试。')
        security = self.state.get('security_token')
        if not security: raise SourceError('请先获取图片验证码。')
        app_token = SecurityTokenCodec().generate_app_security_token(security, self.state['device_id'],app_schema_version=self.state['app_schema_version'])
        self.request('/compus/user/sendLoginVerificationCode', {'securityToken':security,'appSecurityToken':app_token,
            'mobilePhone':self.state['pending_phone'],'sendCount':0,'schoolCode':self.state['pending_school_code'],'token':'','imageCaptchaValue':captcha})
        self.state['sms_sent_at'] = time.time()
        self.save()

    def login(self, code):
        if not re.fullmatch(r'\d{4,8}',code): raise SourceError('请输入短信验证码。')
        if not self.state.get('pending_phone'): raise SourceError('请先获取验证码。')
        result = self.request('/login/doLoginByVerificationCode', {'mobilePhone':self.state['pending_phone'],'verificationCode':code,
            'token':'','schoolCode':self.state['pending_school_code'],'brand':'Android','clientId':'65l3attk4r095ib','mobileType':'Android for arm64',
            'osType':'Android','osVersion':'12','appAllVersion':self.state['app_schema_version'],'appPlatform':'Android','linkUserId':'','invitationCode':''})
        self.accept_login(result)
        self.save()
        self.authorize()

    def accept_login(self, result):
        # Keep field types only: diagnostics must never store SMS codes or response values.
        def structure(value, depth=0):
            if isinstance(value,dict) and depth < 4:
                return {str(k):structure(v,depth+1) for k,v in list(value.items())[:80]}
            if isinstance(value,list):
                return [structure(value[0],depth+1)] if value and depth < 4 else 'list'
            return type(value).__name__ + (' (empty)' if value in (None,'') else '')
        self.state['login_structure'] = structure(result)
        self.save()
        data = result.get('data')
        if isinstance(data,str):
            try: data = json.loads(data)
            except ValueError: data = None
        if isinstance(data,list) and len(data)==1 and isinstance(data[0],dict): data=data[0]
        if not isinstance(data,dict): data={}
        # Explicit envelope variants only; do not recursively pick an unrelated ID.
        identities = [data[k] for k in ('userInfo','user','loginInfo') if isinstance(data.get(k),dict)] + [data]
        user = next((v for v in identities if any(v.get(k) not in (None,'') for k in ('ymId','id','userId'))),{})
        uid = next((user[k] for k in ('ymId','id','userId') if user.get(k) not in (None,'')),None)
        token = next((v[k] for v in [data]+identities+[result] for k in ('token','accessToken','appToken')
                      if isinstance(v.get(k),str) and v[k].strip()),None)
        key_map = user.get('keyMap') or data.get('keyMap')
        signed_login = (result.get('success') is True and str(result.get('statusCode')) in ('0','200')
            and isinstance(key_map,dict) and any(isinstance(v,str) and v for v in key_map.values())
            and user.get('deviceId') == self.state['device_id'])
        if uid is None or isinstance(uid,(bool,dict,list)) or (not token and not signed_login):
            missing = 'App token 和用户ID' if not token and uid is None else ('App token' if not token else '用户ID')
            self.state['login_diagnostic'] = '接口返回成功，但未找到'+missing
            self.save()
            raise SourceError('接口返回成功，但未找到'+missing+'。已保存不含字段值的响应结构，请勿反复发送短信，等待接口适配。')
        merged = dict(data);merged.update(user)
        user = merged
        for cookie in list(self.jar):
            if cookie.name == 'shiroJID': self.jar.clear(cookie.domain,cookie.path,cookie.name)
        self.state.pop('ym_app_id',None)
        self.state.pop('authorized_at',None)
        self.state.pop('room_key',None)
        self.state.pop('login_diagnostic',None)
        self.state.update(token=token or '',auth_mode='signed' if signed_login else 'token',verified_login=True,auth_revision=uuid.uuid4().hex,
                          ym_id=str(uid),phone=self.state.get('pending_phone',''),
                          school_code=str(user.get('schoolCode') or self.state.get('pending_school_code','')),
                          school_name=user.get('schoolName',''),logged_in_at=datetime.now().isoformat(timespec='seconds'),need_login=False)

    def authorize(self):
        if not self.state.get('token') and not (self.state.get('auth_mode')=='signed' and self.state.get('verified_login') and self.state.get('ym_id')):
            raise SessionExpired('请先通过短信验证码登录易校园。')
        school = self.state['school_code']
        if self.state.get('auth_mode') == 'signed':
            self.authorize_signed(school)
            return
        app_id = self.state.get('ym_app_id')
        if not app_id:
            apps = self.request('/compus/application/getSchoolAppList')
            text = json.dumps(apps,ensure_ascii=False)
            match = re.search(r'ymAppId=([\w-]+)',text)
            if not match:
                apps = self.request('/compus/application/getHomeAppList', {'appVersion':'410','limit':11,'offset':0,'schoolCode':school,'testAccount':1})
                match = re.search(r'ymAppId=([\w-]+)',json.dumps(apps,ensure_ascii=False))
            if not match: raise SourceError('未找到学校电费应用标识，请确认易校园已绑定学校。')
            app_id = match.group(1)
            self.state['ym_app_id'] = app_id
        self.state['ym_auth_token'] = self.request('/compus/user/getAuthToken')['data']
        callback = ELECTRIC+'/#/schoolIntro?v=2&unionid='+urllib.parse.quote(self.state['ym_id'])+'&schoolCode='+urllib.parse.quote(school)
        target = self.request('/authoriz/getCodeV2',{'bindSkip':'1','authType':'2','ymAppId':app_id,'callbackUrl':callback,
            'unionid':self.state['ym_id'],'schoolCode':school},host='https://auth.xiaofubao.com',redirect=True)
        u = urllib.parse.urlsplit(target)
        if u.scheme!='https' or u.hostname!='application.xiaofubao.com': raise SourceError('授权服务未返回电费应用地址，需要重新登录。')
        params = urllib.parse.parse_qs(u.query)
        params.update(urllib.parse.parse_qs(u.fragment.partition('?')[2]))
        code = (params.get('ymCode') or [''])[0]
        if not code: raise SourceError('授权跳转缺少ymCode，可能需要重新短信登录。')
        self.request('/app/login/getUser4Authorize',{'code':code,'authType':'2'},host=ELECTRIC,form=True)
        if not any(c.name=='shiroJID' and not c.is_expired() for c in self.jar): raise SourceError('授权成功响应未下发电费会话。')
        self.state['authorized_at'] = datetime.now().isoformat(timespec='seconds')
        self.state['need_login'] = False
        self.save()

    def authorize_signed(self, school):
        # Only reachable for the UID returned by this client's successful OTP login.
        app_id = '1810181825222034'
        target = self.request('/authoriz/getCodeV2',{'bindSkip':'1','authType':'2','ymAppId':app_id,
            'callbackUrl':ELECTRIC+'/','unionid':self.state['ym_id'],'schoolCode':school,'ymAuthToken':''},
            host='https://auth.xiaofubao.com',redirect=True)
        u = urllib.parse.urlsplit(target)
        if u.scheme!='https' or u.hostname!='application.xiaofubao.com': raise SessionExpired('授权服务要求重新登录。')
        params = urllib.parse.parse_qs(u.query)
        params.update(urllib.parse.parse_qs(u.fragment.partition('?')[2]))
        code = (params.get('ymCode') or [''])[0]
        if not code: raise SourceError('授权跳转未返回ymCode，未建立电费会话。')
        result = self.request('/app/login/getUser4Authorize',{'code':code,'authType':'2'},host=ELECTRIC,form=True)
        identity = result.get('data') or {}
        if str(identity.get('schoolCode','')) != school:
            raise SourceError('授权返回的学校与登录学校不一致，未保存电费会话。')
        if not any(c.name=='shiroJID' and not c.is_expired() for c in self.jar):
            raise SourceError('授权响应未下发电费会话。')
        self.state.update(ym_app_id=app_id,authorized_at=datetime.now().isoformat(timespec='seconds'),need_login=False)
        self.save()

    def electric(self, name, params=None):
        if name not in ('getCoutomConfig','queryBind','queryArea','queryBuilding','queryFloor','queryRoom','queryRoomSurplus'):
            raise SourceError('不支持的查询操作。')
        if self.state.get('need_login'): raise SessionExpired('App登录已失效，请在易校园登录页重新验证。')
        had_cookie = any(c.name=='shiroJID' and not c.is_expired() for c in self.jar)
        try:
            if not had_cookie: self.authorize()
            result = self.request('/app/electric/'+name,params or {},host=ELECTRIC,form=True)
            self.save()
            return result
        except SessionExpired:
            try:
                if not had_cookie: raise SessionExpired('登录失效')
                self.authorize()
                result = self.request('/app/electric/'+name,params or {},host=ELECTRIC,form=True)
                self.save()
                return result
            except SessionExpired:
                self.state['need_login'] = True; self.save()
                raise SessionExpired('App登录已失效，自动重新授权失败，请重新短信登录。') from None

    def status(self):
        phone = self.state.get('phone','')
        authenticated = bool(self.state.get('token')) or (self.state.get('auth_mode')=='signed' and bool(self.state.get('verified_login')) and bool(self.state.get('ym_id')))
        return {'logged_in':authenticated and not self.state.get('need_login'),
            'phone':phone[:3]+'****'+phone[-4:] if phone else '', 'school_code':self.state.get('school_code',''),
            'school_name':self.state.get('school_name',''),'authorized_at':self.state.get('authorized_at'),
            'need_login':bool(self.state.get('need_login')),
            'authorized':any(c.name=='shiroJID' and not c.is_expired() for c in self.jar),
            'room_bound':bool(self.state.get('room_key'))}
