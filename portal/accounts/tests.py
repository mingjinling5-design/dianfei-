import json
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase,Client as DjangoClient,override_settings
from django.test import TransactionTestCase
from django.db import connection,transaction
from unittest import skipUnless
from django.utils import timezone
from .models import Account,Room,Reading,Alert
from .state import write_state,read_state,configuration
from .store import TenantStore
from .worker import collect_account
from ysp.tenant_context import tenant_scope,CURRENT


class Client(DjangoClient):
    """Represent a browser reaching the production service through HTTPS nginx."""
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('HTTP_X_FORWARDED_PROTO','https')
        kwargs.setdefault('HTTP_ORIGIN','https://testserver')
        super().__init__(*args,**kwargs)
    def generic(self,*args,**kwargs):
        kwargs['secure']=True
        return super().generic(*args,**kwargs)


@override_settings(ALLOWED_HOSTS=['testserver'])
class IsolationTests(TestCase):
    def setUp(self):
        self.a=Account.objects.create(user=get_user_model().objects.create_user(username='a'),provider_uid='provider-a')
        self.b=Account.objects.create(user=get_user_model().objects.create_user(username='b'),provider_uid='provider-b')
        self.ca=Client();self.cb=Client();self.ca.force_login(self.a.user);self.cb.force_login(self.b.user)
        for account,amount,label in [(self.a,10,'A宿舍'),(self.b,90,'B宿舍')]:
            TenantStore(account).upsert_room({'room_key':'SAME','label':label})
            TenantStore(account).insert_reading('SAME','2026-10-29T12:00:00',None,amount)
            TenantStore(account).set_meta('native_unit','cny')
            write_state(account,{'token':'private-'+label,'ym_id':account.provider_uid,'room_key':'SAME'})
    def test_same_room_key_is_isolated(self):
        ra=self.ca.get('/api/rooms').json()['rooms'];rb=self.cb.get('/api/rooms').json()['rooms']
        self.assertEqual(ra[0]['label'],'A宿舍');self.assertEqual(rb[0]['label'],'B宿舍')
        self.assertEqual(ra[0]['last']['balance_cny'],10);self.assertEqual(rb[0]['last']['balance_cny'],90)
    def test_other_room_returns_404_not_first_room(self):
        TenantStore(self.b).upsert_room({'room_key':'B-ONLY','label':'private'})
        self.assertEqual(self.ca.get('/api/summary?room=B-ONLY').status_code,404)
    def test_summary_and_series_reuse_analytics_without_leak(self):
        self.assertEqual(self.ca.get('/api/summary').json()['balance'],10)
        self.assertEqual(self.cb.get('/api/series').json()['points'][0]['cny'],90)
    def test_csv_only_contains_current_account(self):
        a=self.ca.get('/api/export.csv').content.decode('utf-8-sig')
        b=self.cb.get('/api/export.csv').content.decode('utf-8-sig')
        self.assertIn('10.0',a);self.assertNotIn('90.0',a);self.assertIn('90.0',b)
    def test_alert_ack_requires_owner(self):
        alert=Alert.objects.create(owner=self.b,room_key='SAME',ts='2026',level='warn',message='private')
        r=self.ca.post('/api/alerts/ack',json.dumps({'id':alert.pk}),content_type='application/json')
        self.assertEqual(r.status_code,404);alert.refresh_from_db();self.assertFalse(alert.acked)
    def test_anonymous_apis_cannot_see_rooms(self):
        c=Client()
        for url in ('/api/rooms','/api/summary','/api/export.csv','/api/collect'):
            self.assertEqual(c.get(url).status_code,401)
    def test_private_paths_and_legacy_login_routes_removed(self):
        for url in ('/data/eschool_session.json','/config.json','/capture','/api/auth/import-cookie'):
            self.assertEqual(self.ca.get(url).status_code,404)
    def test_eschool_status_does_not_return_secrets(self):
        data=self.ca.get('/api/eschool/status').content.decode()
        self.assertNotIn('private-A宿舍',data);self.assertNotIn('private-B宿舍',data)
        self.assertNotIn('B宿舍',data)
    def test_new_login_uses_pending_profile_not_active_account(self):
        def handle(action,body,cfg):
            write_state(CURRENT.get()[0],{'pending_phone':'test-only'})
            return {'ok':True,'image':'test'}
        with patch('ysp.eschool_service.handle',side_effect=handle):
            self.ca.post('/api/eschool/captcha',json.dumps({}),content_type='application/json')
        self.a.refresh_from_db();self.assertEqual(read_state(self.a)['ym_id'],'provider-a')
    def test_data_and_settings_storage_are_owner_scoped(self):
        with tenant_scope(self.a,configuration(self.a)):
            from ysp.eschool import EschoolClient
            c=EschoolClient();c.state['token']='new-a';c.save()
        self.a.refresh_from_db();self.b.refresh_from_db()
        self.assertEqual(read_state(self.a)['token'],'new-a')
        self.assertNotEqual(read_state(self.b)['token'],'new-a')
        self.assertNotIn('new-a',self.a.encrypted_state)
    def test_foreign_account_cannot_be_opened_by_path(self):
        with tenant_scope(self.a,configuration(self.a)):
            from ysp import db
            with self.assertRaises(PermissionError):db.connect('orm:'+str(self.b.id))
    def test_collection_failure_does_not_touch_other_user(self):
        before=Reading.objects.filter(owner=self.b).count()
        with patch('ysp.collect.collect_once',return_value={'ok':False,'error':'expired'}):result=collect_account(self.a.pk,True)
        self.assertFalse(result['ok']);self.assertEqual(Reading.objects.filter(owner=self.b).count(),before)
        self.b.refresh_from_db();self.assertEqual(self.b.last_result,{})
    def test_worker_refreshes_due_account_only(self):
        self.b.next_collect=timezone.now()+timedelta(hours=1);self.b.save()
        with patch('ysp.collect.collect_once',return_value={'ok':True,'results':[]}) as mock:
            result=collect_account(self.b.pk)
        self.assertTrue(result['skipped']);mock.assert_not_called()
    def test_first_manual_collection_is_not_blocked_by_pending_schedule(self):
        self.a.next_collect=timezone.now()+timedelta(minutes=10);self.a.save()
        with patch('ysp.collect.collect_once',return_value={'ok':True}) as mock:
            result=collect_account(self.a.pk,True)
        self.assertTrue(result['ok']);mock.assert_called_once()
    def test_repeated_manual_collection_is_rate_limited(self):
        with patch('ysp.collect.collect_once',return_value={'ok':True}) as mock:
            collect_account(self.a.pk,True)
            second=collect_account(self.a.pk,True)
        self.assertFalse(second['ok']);self.assertEqual(mock.call_count,1)
    def test_logout_revokes_browser_session(self):
        self.ca.post('/api/site/logout')
        self.assertEqual(self.ca.get('/api/rooms').status_code,401)
    def test_disconnect_clears_only_current_account_credentials(self):
        self.ca.post('/api/site/disconnect')
        self.a.refresh_from_db();self.b.refresh_from_db()
        self.assertEqual(read_state(self.a),{'need_login':True})
        self.assertEqual(read_state(self.b)['ym_id'],'provider-b')
        self.assertEqual(self.ca.get('/api/rooms').status_code,401)
        self.assertEqual(self.cb.get('/api/summary').json()['balance'],90)
    def test_csrf_is_required_for_mutations(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.a.user)
        self.assertEqual(c.post('/api/site/logout').status_code,403)
        c.get('/api/site/session')
        token=c.cookies['csrftoken'].value
        self.assertEqual(c.post('/api/site/logout',HTTP_X_CSRFTOKEN=token).status_code,200)
    def test_successful_provider_identity_not_body_selects_account(self):
        c=Client()
        c.get('/api/site/session')
        def handle(action,body,cfg):
            write_state(CURRENT.get()[0],{'auth_revision':'new','verified_login':True,'ym_id':'provider-a','token':'own-token'})
            return {'ok':True}
        with patch('ysp.eschool_service.handle',side_effect=handle):
            result=c.post('/api/eschool/login',json.dumps({'code':'123456','uid':'provider-b'}),content_type='application/json')
        self.assertTrue(result.json()['authenticated'])
        self.assertEqual(c.get('/api/rooms').json()['rooms'][0]['label'],'A宿舍')
    def test_failed_otp_cannot_establish_site_session(self):
        c=Client()
        with patch('ysp.eschool_service.handle',return_value={'ok':False,'error':'wrong code'}):
            c.post('/api/eschool/login','{}',content_type='application/json')
        self.assertEqual(c.get('/api/rooms').status_code,401)
    def test_upstream_expiry_keeps_owned_history_available(self):
        write_state(self.a,{'need_login':True})
        result=self.ca.get('/api/auth/status').json()
        self.assertFalse(result['login_required'])
        self.assertTrue(result['logged_in']);self.assertTrue(result['need_relogin'])
        self.assertEqual(self.ca.get('/api/summary').json()['balance'],10)
    def test_all_dashboard_endpoints_remain_usable(self):
        for path in ('daily','hourly','weekday','forecast','anomalies','heatmap','alerts','hourly_series',
                     'quality','distribution','cycles','stale','power','compare','insights'):
            with self.subTest(path=path):
                result=self.ca.get('/api/'+path)
                self.assertEqual(result.status_code,200)
    def test_saving_one_room_does_not_disable_other_owner(self):
        a=TenantStore(self.a);a.upsert_room({'room_key':'NEW','label':'new'})
        a.execute('UPDATE rooms SET enabled=0 WHERE room_key<>?',('NEW',))
        self.assertTrue(Room.objects.get(owner=self.b,room_key='SAME').enabled)
        self.assertFalse(Room.objects.get(owner=self.a,room_key='SAME').enabled)


@skipUnless(connection.vendor=='postgresql','PostgreSQL-only row-lock integration test')
class WorkerLockTests(TransactionTestCase):
    def test_locked_account_is_skipped_by_another_worker(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        account=Account.objects.create(user=get_user_model().objects.create_user(username='locked'))
        def other_worker():
            close_old_connections()
            try:return collect_account(account.pk,True)
            finally:close_old_connections()
        with transaction.atomic():
            Account.objects.select_for_update().get(pk=account.pk)
            with ThreadPoolExecutor(max_workers=1) as pool:result=pool.submit(other_worker).result(timeout=5)
        self.assertFalse(result['ok'])
        self.assertIn('其他请求',result['error'])
