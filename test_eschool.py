import copy
import http.cookiejar
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ysp.eschool import EschoolClient, SessionExpired
from ysp.security_token import SecurityTokenCodec
from ysp.app_sign import sign_request
from ysp.adapters.base import SourceError, build_adapter
from ysp.adapters.eschool import EschoolAdapter
from ysp.config import DEFAULTS


def cookie():
    return http.cookiejar.Cookie(0,'shiroJID','test-only',None,False,'.xiaofubao.com',True,True,'/',True,True,None,True,None,None,{})


class EschoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=EschoolClient(Path(self.tmp.name)/'session.json')

    def test_published_crypto_vector(self):
        result=SecurityTokenCodec().generate_app_security_token('ce295733862b93cb376efef661c21b4dEW6CpH8wFHp/RvViKZiJ8A==','12345678',timestamp=123)
        self.assertTrue(result.startswith('RxTdUD90Eg91tGZHyhTKwjX9v3fH8WWGgQ3vQ5CuiC'))

    def test_invalid_security_token_fails(self):
        with self.assertRaises(ValueError):
            SecurityTokenCodec().generate_app_security_token('invalid','test')

    def test_session_cookie_survives_restart(self):
        self.client.jar.set_cookie(cookie());self.client.state['token']='test-app-token';self.client.save()
        loaded=EschoolClient(self.client.path)
        self.assertEqual(loaded.state['device_id'],self.client.state['device_id'])
        self.assertEqual(next(iter(loaded.jar)).value,'test-only')
        self.assertNotIn('token',loaded.status())

    def test_expired_cookie_reauthorizes_once(self):
        self.client.jar.set_cookie(cookie());self.client.state['token']='test'
        with patch.object(self.client,'request',side_effect=[SessionExpired('expired'),{'success':True,'data':{'amount':0}}]) as req, patch.object(self.client,'authorize') as auth:
            self.assertEqual(self.client.electric('queryRoomSurplus')['data']['amount'],0)
        self.assertEqual(req.call_count,2);auth.assert_called_once()

    def test_expired_app_token_pauses_future_network_attempts(self):
        self.client.jar.set_cookie(cookie());self.client.state['token']='test'
        with patch.object(self.client,'request',side_effect=SessionExpired('expired')) as req, patch.object(self.client,'authorize',side_effect=SessionExpired('expired')) as auth:
            with self.assertRaises(SessionExpired):self.client.electric('queryArea')
            with self.assertRaises(SessionExpired):self.client.electric('queryArea')
        self.assertEqual(req.call_count,1);auth.assert_called_once()
        self.assertTrue(self.client.state['need_login'])

    def test_sms_cooldown_does_not_send(self):
        import time
        self.client.state['sms_sent_at']=time.time()
        with patch.object(self.client,'request') as req:
            with self.assertRaises(SourceError):self.client.send_code('test')
            req.assert_not_called()

    def test_wrong_schools_never_query(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['source']['type']='eschool'
        adapter=build_adapter(cfg,{'source_type':'nofish'})
        self.assertIsInstance(adapter,EschoolAdapter)
        self.client.state['school_code']='1'
        room=dict(room_key='TEST',school_code='2',area_id='1',building_code='2',floor_code='3',room_code='4')
        with patch('ysp.adapters.eschool.EschoolClient',return_value=self.client), patch.object(self.client,'electric') as req:
            with self.assertRaises(SourceError):adapter.fetch(room)
            req.assert_not_called()

    def test_authorization_missing_code_fails(self):
        self.client.state.update(token='test',ym_id='1',school_code='1',ym_app_id='test')
        with patch.object(self.client,'request',side_effect=[{'data':'auth-token'},'https://application.xiaofubao.com/#/schoolIntro']):
            with self.assertRaises(SourceError):self.client.authorize()

    def test_new_account_clears_old_electric_session(self):
        self.client.jar.set_cookie(cookie())
        self.client.state.update(ym_app_id='old-app',pending_phone='13800000000',pending_school_code='2')
        self.client.accept_login({'data':{'id':'new-user','token':'new-token','schoolCode':'2'}})
        self.assertEqual(len(self.client.jar),0)
        self.assertNotIn('ym_app_id',self.client.state)
        self.assertEqual(self.client.state['phone'],'13800000000')

    def test_nested_login_response(self):
        self.client.accept_login({'success':True,'data':{'token':'secret-example','userInfo':{'id':'42','schoolCode':'10356'}}})
        self.assertEqual(self.client.state['ym_id'],'42')
        self.assertEqual(self.client.state['token'],'secret-example')
        self.assertNotIn('secret-example',json.dumps(self.client.state['login_structure']))

    def test_user_id_alias(self):
        self.client.accept_login({'data':{'userId':'42','accessToken':'secret-example'}})
        self.assertEqual(self.client.state['ym_id'],'42')

    def test_missing_credentials_never_marked_logged_in(self):
        with self.assertRaises(SourceError):self.client.accept_login({'success':True,'data':{'needBind':True}})
        self.assertFalse(self.client.status()['logged_in'])
        self.assertIn('login_structure',self.client.state)

    def test_new_protocol_uses_verified_device_identity(self):
        response={'success':True,'statusCode':0,'data':{'id':'42','deviceId':self.client.state['device_id'],
            'schoolCode':'10356','keyMap':{'730':'encrypted-example'}}}
        self.client.accept_login(response)
        self.assertEqual(self.client.state['auth_mode'],'signed')
        self.assertTrue(self.client.status()['logged_in'])
        self.assertFalse(self.client.state['token'])
        self.assertNotIn('encrypted-example',self.client.path.read_text(encoding='utf-8'))

    def test_wrong_device_cannot_enable_signed_login(self):
        response={'success':True,'statusCode':0,'data':{'id':'42','deviceId':'other-device','keyMap':{'730':'secret'}}}
        with self.assertRaises(SourceError):self.client.accept_login(response)
        self.assertFalse(self.client.status()['logged_in'])

    def test_sign_matches_published_vectors(self):
        vectors=[({'appVersion':'730','sceneCode':'app_user_login','nt':1756225768826,
            'deviceId':'ym-57766d45173c643469eadb10a8766666','platform':'YUNMA_APP','schoolCode':'10337'},
            'i85zWFEF0nb8BR8VxQGw7IqkZvT9C5CUqyt6KteO+4U='),
            ({'appVersion':'730','nt':1756730606020,'ymId':'2408157831570432666',
            'deviceId':'ym-bcbcbb20801444c96978b0c72f114514','platform':'YUNMA_APP'},
            '9buzHakN0foXRu30s7CyB3B8zH9epcBe5vB+zaBC4s4=')]
        for params,expected in vectors:self.assertEqual(sign_request(params),expected)

    def test_signed_authorization_and_school_validation(self):
        self.client.state.update(auth_mode='signed',verified_login=True,ym_id='42',school_code='10356')
        def request(path,*args,**kwargs):
            if path=='/authoriz/getCodeV2':return 'https://application.xiaofubao.com/?ymCode=test-only'
            self.client.jar.set_cookie(cookie())
            return {'success':True,'data':{'schoolCode':'10356'}}
        with patch.object(self.client,'request',side_effect=request):self.client.authorize()
        self.assertTrue(self.client.status()['authorized'])

    def test_signed_authorization_rejects_wrong_school(self):
        self.client.state.update(auth_mode='signed',verified_login=True,ym_id='42',school_code='10356')
        with patch.object(self.client,'request',side_effect=['https://application.xiaofubao.com/?ymCode=test-only',
            {'success':True,'data':{'schoolCode':'other'}}]):
            with self.assertRaises(SourceError):self.client.authorize()

    def test_network_timeout_is_not_reported_as_certificate_error(self):
        error=self.client.network_error(TimeoutError(),'https://compus.xiaofubao.com','/common/security/token')
        self.assertIn('网络连接超时',str(error))
        self.assertNotIn('证书校验失败',str(error))
        self.assertEqual(self.client.state['network_diagnostic']['type'],'TimeoutError')


if __name__=='__main__':unittest.main()
