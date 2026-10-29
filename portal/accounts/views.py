import hashlib
import hmac
import json
import mimetypes
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.contrib import auth
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect,FileResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import ensure_csrf_cookie
from .models import Account, Room, RateLimit
from .state import configuration, read_state, write_state
from .store import TenantStore
from ysp.tenant_context import CURRENT, tenant_scope


def response(data,status=200):
    out=JsonResponse(data,status=status,json_dumps_params={'ensure_ascii':False})
    out['Cache-Control']='no-store'
    return out


def client_ip(request):
    # The production web container is private; nginx overwrites X-Real-IP.
    value=request.META.get('HTTP_X_REAL_IP') if settings.PRODUCTION else None
    return value or request.META.get('REMOTE_ADDR','')


def account_for(request,pending=False):
    if request.user.is_authenticated and not pending:
        return Account.objects.get(user=request.user)
    pending=request.session.get('pending_account')
    account=Account.objects.filter(pk=pending,user__isnull=True,provider_uid__isnull=True).first() if pending else None
    if not account:
        account=Account.objects.create()
        request.session['pending_account']=str(account.pk)
    return account


@ensure_csrf_cookie
def site_session(request):
    if request.method!='GET':return response({'error':'method not allowed'},405)
    if not throttle(request,'site-session',client_ip(request),240,3600):
        return response({'error':'请求过于频繁，请稍后重试'},429)
    account_for(request)
    return response({'authenticated':request.user.is_authenticated,'is_admin':request.user.is_authenticated and request.user.is_staff,'csrf':get_token(request)})


def admin_users(request):
    if not request.user.is_authenticated:return HttpResponseRedirect('/eschool')
    if not request.user.is_staff:return response({'error':'需要管理员权限'},403)
    users=get_user_model().objects.filter(account__isnull=False).select_related('account').order_by('date_joined')
    lines=[]
    for user in users:
        account=user.account
        room=Room.objects.filter(owner=account,enabled=True).first()
        label=(room.data.get('label','') if room else '')
        phone=read_state(account).get('phone','')
        status=account.last_result.get('ok')
        state='正常' if status else ('等待首次采集' if not account.last_result else '采集异常')
        lines.append('<tr><td>'+escape(account.created_at.strftime('%Y-%m-%d %H:%M'))+'</td><td>'+escape(phone or '未记录')+'</td><td>'+escape(label or '未识别')+'</td><td>'+escape(state)+'</td></tr>')
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>用户管理</title><style>body{font:16px system-ui,"Microsoft YaHei",sans-serif;background:#f5f6f8;color:#20242b;margin:0}main{max-width:900px;margin:40px auto;padding:24px;background:white;border-radius:12px}a{color:#2563eb}table{border-collapse:collapse;width:100%;margin-top:20px}th,td{text-align:left;border-bottom:1px solid #e5e7eb;padding:12px}small{color:#596475}</style><main><a href="/">← 返回仪表盘</a><h1>用户管理</h1><small>手机号仅对管理员显示；此页面不要转发给无关人员。</small><table><thead><tr><th>加入时间</th><th>手机号</th><th>宿舍</th><th>状态</th></tr></thead><tbody>'''+''.join(lines)+'''</tbody></table></main></html>'''
    return HttpResponse(html)


def throttle(request,kind,identity,limit,seconds):
    material=kind+':'+str(identity)
    key=hmac.new(settings.SECRET_KEY.encode(),material.encode(),hashlib.sha256).hexdigest()
    now=timezone.now()
    with transaction.atomic():
        RateLimit.objects.get_or_create(key=key)
        row=RateLimit.objects.select_for_update().get(pk=key)
        if row.started+timedelta(seconds=seconds)<=now:row.started=now;row.count=0
        if row.count>=limit:return False
        row.count+=1;row.save()
    return True


def eschool(request,action):
    if request.method not in ('GET','POST') or (request.method=='GET' and action!='status'):
        return response({'error':'method not allowed'},405)
    public={'status','captcha','send-code','login'}
    if not request.user.is_authenticated and action not in public:return response({'error':'请先登录'},401)
    try:body=json.loads(request.body or b'{}') if request.method=='POST' else {}
    except ValueError:return response({'error':'无效JSON'},400)
    if not isinstance(body,dict):return response({'error':'无效请求'},400)
    if action=='status' and not request.user.is_authenticated:
        return response({'ok':True,'logged_in':False,'authorized':False,'source':'eschool','rooms':[],
            'phone':'','school_code':'','school_name':'','need_login':False,'room_bound':False,'interval_minutes':10})
    if action in {'send-code','login'} and not request.session.get('pending_account'):
        return response({'error':'请先获取图片验证码。'},400)
    ip=client_ip(request)
    if action=='captcha' and not throttle(request,'captcha',ip,30,300):return response({'error':'请求过于频繁，请稍后重试'},429)
    if action=='login' and not throttle(request,'login',ip,20,900):return response({'error':'登录尝试过多，请稍后重试'},429)
    account=account_for(request,pending=action in {'captcha','send-code','login'})
    if action=='send-code':
        state=read_state(account)
        phone=state.get('pending_phone')
        if not phone:return response({'error':'请先获取图片验证码'},400)
        if not throttle(request,'sms-minute',phone,1,60) or not throttle(request,'sms-hour',phone,5,3600):
            return response({'error':'短信发送过于频繁，请稍后重试'},429)
    from ysp.eschool_service import handle
    from ysp.adapters.base import SourceError
    with transaction.atomic():
        account=Account.objects.select_for_update().get(pk=account.pk)
        before=read_state(account).get('auth_revision')
        cfg=configuration(account)
        with tenant_scope(account,cfg):
            try:result=handle(action,body,cfg)
            except SourceError as exc:result={'ok':False,'error':str(exc)}
            except (ValueError,TypeError,KeyError):result={'ok':False,'error':'接口响应格式异常，请重试或联系维护者。'}
        result.pop('login_structure',None)
        state=read_state(account)
        verified=(action=='login' and state.get('auth_revision') and state['auth_revision']!=before
            and state.get('verified_login') and state.get('ym_id'))
        if verified:
            # The provider UID comes exclusively from the successful upstream OTP response.
            target,_=Account.objects.get_or_create(provider_uid=str(state['ym_id']))
            target=Account.objects.select_for_update().get(pk=target.pk)
            if target.user_id is None:
                user=get_user_model().objects.create_user(username=uuid.uuid4().hex,password=None)
                target.user=user;target.save(update_fields=['user'])
            write_state(target,state)
            auth.login(request,target.user,backend='django.contrib.auth.backends.ModelBackend')
            request.session.pop('pending_account',None)
            if account.pk!=target.pk and account.user_id is None:account.delete()
            result['authenticated']=True
    return response(result)


def logout(request):
    if request.method!='POST':return response({'error':'method not allowed'},405)
    auth.logout(request)
    return response({'ok':True})


def disconnect(request):
    if request.method!='POST':return response({'error':'method not allowed'},405)
    if not request.user.is_authenticated:return response({'error':'请先登录'},401)
    with transaction.atomic():
        account=Account.objects.select_for_update().get(user=request.user)
        write_state(account,{'need_login':True})
    auth.logout(request)
    return response({'ok':True})


class ConfigProxy:
    def value(self):
        tenant=CURRENT.get()
        if not tenant:raise PermissionError('Missing account scope')
        return tenant[1]
    def __getitem__(self,k):return self.value()[k]
    def get(self,k,default=None):return self.value().get(k,default)


_analytics=None
def analytics_module():
    global _analytics
    if _analytics is None:
        from ysp import server
        server.CFG=ConfigProxy()
        server.conn=lambda:TenantStore(CURRENT.get()[0]) if CURRENT.get() else (_ for _ in ()).throw(PermissionError('Missing account scope'))
        from ysp import analyze
        from ysp.config import DEFAULTS
        analyze.set_vacancy_config(DEFAULTS)
        _analytics=server
    return _analytics


def api(request,path):
    if path=='health':return response({'ok':True,'service':'electricity-portal','source':'eschool'})
    if not request.user.is_authenticated:return response({'error':'请先登录'},401)
    allowed={'rooms','summary','series','daily','hourly','weekday','forecast','anomalies','heatmap',
        'alerts','hourly_series','quality','distribution','cycles','stale','power','compare','insights','export.csv','auth/status'}
    if request.method=='GET' and path in allowed:
        account=account_for(request)
        cfg=configuration(account)
        if request.GET.get('room') and not Room.objects.filter(owner=account,room_key=request.GET['room']).exists():
            return response({'error':'未找到宿舍'},404)
        with tenant_scope(account,cfg):
            if path=='auth/status':
                from ysp.eschool import EschoolClient
                st=EschoolClient().status()
                return response({'source_type':'eschool','login_required':False,'logged_in':True,
                    'app_logged_in':st['logged_in'],'need_relogin':st['need_login'],'need_location':not st['room_bound']})
            module=analytics_module()
            result=module.ROUTES_GET['/api/'+path](dict(request.GET.lists()))
            if path=='quality':
                from ysp.eschool import EschoolClient
                st=EschoolClient().status()
                result['session'].update(logged_in=st['logged_in'],need_relogin=st['need_login'])
            if isinstance(result,dict) and '__csv__' in result:
                out=HttpResponse(result['__csv__'].encode('utf-8-sig'),content_type='text/csv; charset=utf-8')
                out['Content-Disposition']='attachment; filename="readings.csv"';out['Cache-Control']='no-store';return out
            return response(result)
    if request.method=='POST' and path=='collect':
        from .worker import collect_account
        return response(collect_account(account_for(request).pk,force=True))
    if request.method=='POST' and path=='alerts/ack':
        try:aid=int(json.loads(request.body).get('id',0))
        except (ValueError,TypeError):return response({'error':'无效请求'},400)
        from .models import Alert
        updated=Alert.objects.filter(owner=account_for(request),pk=aid).update(acked=True)
        return response({'ok':True}) if updated else response({'error':'未找到记录'},404)
    return response({'error':'not found'},404)


def page(request,path=''):
    if path in ('','index.html') and not request.user.is_authenticated:return HttpResponseRedirect('/eschool')
    if path=='login':path='eschool'
    root=settings.BASE_DIR/'web'
    allowed={'','index.html','eschool','eschool.html','site.js','manifest.webmanifest'}
    if path not in allowed and not (path.startswith('vendor/') or path.startswith('icons/')):return HttpResponse(status=404)
    target=(root/(path or 'index.html')).resolve()
    if path=='eschool':target=root/'eschool.html'
    if not target.is_relative_to(root.resolve()) or not target.is_file():return HttpResponse(status=404)
    content=target.read_bytes()
    mime=mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
    out=HttpResponse(content,content_type=mime)
    out['Cache-Control']='no-store'
    return out


def source(request):
    path=settings.BASE_DIR/'release/source.zip'
    if not path.is_file():return HttpResponse(status=404)
    return FileResponse(path.open('rb'),as_attachment=True,filename='electricity-portal-source.zip')
