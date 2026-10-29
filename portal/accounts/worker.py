from datetime import timedelta,datetime
import random
from django.db import transaction, connection
from django.utils import timezone
from .models import Account
from .state import read_state, configuration
from ysp.tenant_context import tenant_scope


def collect_account(account_id,force=False):
    with transaction.atomic():
        query=Account.objects.select_for_update(skip_locked=True) if connection.vendor=='postgresql' else Account.objects.select_for_update()
        account=query.filter(pk=account_id,user__isnull=False).first()
        if account is None:return {'ok':False,'error':'该账号正在处理其他请求，请稍后重试。'}
        now=timezone.now()
        if not force and account.next_collect>now:return {'ok':False,'skipped':True}
        state=read_state(account)
        attempted=account.last_result.get('_attempted_at')
        if force and attempted and account.last_result.get('_auth_revision')==state.get('auth_revision'):
            try:
                if (now-datetime.fromisoformat(attempted)).total_seconds()<30:
                    return {'ok':False,'error':'刚刚已采集，请稍后刷新。'}
            except (ValueError,TypeError):pass
        if not state.get('room_key') or state.get('need_login'):
            account.next_collect=now+timedelta(minutes=10)
            account.save(update_fields=['next_collect'])
            return {'ok':False,'error':'请先登录并保存宿舍。','need_login':bool(state.get('need_login'))}
        cfg=configuration(account)
        with tenant_scope(account,cfg):
            from ysp.collect import collect_once
            from ysp.adapters.base import SourceError
            try:result=collect_once(cfg,verbose=False,run_alerts=False)
            except SourceError as exc:result={'ok':False,'error':str(exc)}
        account.last_result={**result,'_attempted_at':now.isoformat(),'_auth_revision':state.get('auth_revision')}
        account.next_collect=now+timedelta(minutes=10,seconds=random.randint(0,20))
        account.save(update_fields=['last_result','next_collect'])
        return result
