import hashlib
import json
from . import db
from .config import ROOT, db_path
from .eschool import EschoolClient, LOCK
from .adapters.base import SourceError


def persist_config(cfg):
    from .tenant_context import CURRENT
    tenant=CURRENT.get()
    if tenant:
        account=tenant[0]
        account.config={'rooms':cfg.get('rooms',[])}
        account.save(update_fields=['config'])
        return
    path = ROOT / 'config.json'
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({k:v for k,v in cfg.items() if not k.startswith('_')},ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)


def resolve_room(client, cfg):
    """Restore an explicit bound/saved room; never select the first arbitrary room."""
    keys=('areaId','buildingCode','floorCode','roomCode')
    saved=[dict(zip(keys,(str(r.get(k) or '') for k in ('area_id','building_code','floor_code','room_code')))) for r in cfg.get('rooms',[])]
    bound=client.electric('queryBind',{'bindType':1}).get('rows',[])
    candidates=[r for r in bound if all(str(r.get(k) or '') for k in keys)]
    candidates=list({tuple(str(r.get(k) or '') for k in keys):r for r in candidates}.values())
    matched=next((r for r in candidates if {k:str(r.get(k) or '') for k in keys} in saved),None)
    selected = candidates[0] if len(candidates)==1 else matched
    target={k:str(selected.get(k) or '') for k in keys} if selected else next((r for r in saved if len(saved)==1),None)
    if target is None and not candidates and len(saved)==1 and all(saved[0].values()):target=saved[0]
    if target is None:return {'restored':False,'message':'没有唯一可确认的宿舍，请首次选择并保存；下次登录会自动恢复。'}
    names={k:str((selected or {}).get(k) or target[keys[i]]) for i,k in enumerate(('areaName','buildingName','floorName','roomName'))}
    return {'restored':True,'selection':{**target,**names},'message':'已从易校园接口识别宿舍。'}


def handle(action, body, cfg):
    with LOCK:
        client = EschoolClient()
        if action == 'status':
            return {'ok':True, **client.status(), 'source':cfg['source']['type'],
                    'rooms':cfg.get('rooms',[]),'interval_minutes':cfg['collect']['interval_minutes'],
                    'login_diagnostic':client.state.get('login_diagnostic'),
                    'login_structure':client.state.get('login_structure')}
        if action == 'captcha':
            result = client.captcha(str(body.get('phone','')).strip(),str(body.get('school_code','')).strip())
            return {'ok':True,**result}
        if action == 'send-code':
            captcha = str(body.get('captcha','')).strip()
            if not captcha: raise SourceError('请填写图片验证码。')
            client.send_code(captcha)
            return {'ok':True,'message':'短信验证码已发送，请在下方填写。'}
        if action == 'login':
            client.login(str(body.get('code','')).strip())
            cfg['source'].update(type='eschool',strict_tls=True)
            persist_config(cfg)
            return {'ok':True,**client.status(),'message':'易校园登录及电费授权成功，请选择宿舍。'}
        if action == 'authorize':
            client.authorize()
            return {'ok':True,'message':'电费会话已重新授权。'}
        if action == 'auto-room':
            restored=resolve_room(client,cfg)
            if restored['restored']:
                saved=handle('room',restored['selection'],cfg)
                restored['room']=saved['room']
            return {'ok':True,**restored}
        if action == 'locations':
            level = body.get('level')
            paths = {'area':'queryArea','building':'queryBuilding','floor':'queryFloor','room':'queryRoom'}
            if level not in paths: raise SourceError('无效的宿舍层级。')
            required = {'area':(), 'building':('areaId',), 'floor':('areaId','buildingCode'), 'room':('areaId','buildingCode','floorCode')}[level]
            params = {k:str(body.get(k,'')) for k in required}
            if not all(params.values()): raise SourceError('请先选择上一级位置。')
            if level == 'area': params['type'] = 1
            result = client.electric(paths[level],params)
            return {'ok':True,'rows':result.get('rows',[])}
        if action == 'room':
            fields = {k:str(body.get(k,'')).strip() for k in ('areaId','buildingCode','floorCode','roomCode')}
            if not all(fields.values()): raise SourceError('请选择完整宿舍。')
            options = client.electric('queryRoom',{k:v for k,v in fields.items() if k!='roomCode'}).get('rows',[])
            selected = next((r for r in options if str(r.get('roomCode')) == fields['roomCode']),None)
            if not selected: raise SourceError('未找到所选宿舍，请刷新后重选。')
            # Keep the existing history key when selecting the same physical room.
            key = 'DT-' + hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()[:16]
            connection = db.connect(db_path(cfg))
            previous = next((r for r in db.list_rooms(connection) if r['room_key']==key),{})
            label = ' '.join(str(body.get(k,'')) for k in ('areaName','buildingName','floorName'))+' '+str(selected.get('roomName') or fields['roomCode'])
            room = dict(room_key=key,label=label.strip(),school_code=client.state['school_code'],
                area_id=fields['areaId'],building_code=fields['buildingCode'],floor_code=fields['floorCode'],room_code=fields['roomCode'],
                unit_price=float(previous.get('unit_price') or 0.538),warn_kwh=20,critical_kwh=5)
            db.upsert_room(connection,room)
            connection.execute('UPDATE rooms SET enabled=0 WHERE room_key<>?',(key,)); connection.commit()
            db.set_meta(connection,'native_unit','cny');connection.close()
            cfg['rooms']=[room];cfg['source'].update(type='eschool',strict_tls=True)
            cfg['collect'].update(interval_minutes=10,minute_offset=0,jitter_seconds=0,retry=1)
            persist_config(cfg)
            client.state['room_key']=key
            client.save()
            return {'ok':True,'room':room,'message':'宿舍已保存，点击立即采集查看余额。'}
        raise SourceError('未知操作。')
