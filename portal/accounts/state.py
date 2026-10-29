import json
import os
from cryptography.fernet import Fernet
from django.conf import settings


def cipher():
    supplied=os.environ.get('YSP_CREDENTIAL_KEY')
    path=settings.DATA/'credential.key'
    if not supplied:
        if settings.PRODUCTION:raise RuntimeError('Production requires YSP_CREDENTIAL_KEY')
        if not path.exists():
            try:
                with path.open('xb') as f:f.write(Fernet.generate_key())
            except FileExistsError:pass
        supplied=path.read_bytes().strip()
    return Fernet(supplied)


def read_state(account):
    return json.loads(cipher().decrypt(account.encrypted_state.encode())) if account.encrypted_state else {}


def write_state(account,state):
    account.encrypted_state=cipher().encrypt(json.dumps(state,ensure_ascii=False).encode()).decode()
    account.save(update_fields=['encrypted_state'])


def configuration(account):
    import copy
    from ysp.config import DEFAULTS
    cfg=copy.deepcopy(DEFAULTS)
    cfg['source'].update(type='eschool',strict_tls=True)
    cfg['rooms']=account.config.get('rooms',[])
    cfg['app']['weather']['enabled']=False
    cfg['app']['daytype']['enabled']=False
    cfg['alerts']['enabled']=False
    cfg['collect'].update(interval_minutes=10,minute_offset=0,jitter_seconds=0,retry=1)
    cfg['_tenant_id']=str(account.id)
    cfg['_config_path']='account settings'
    return cfg
