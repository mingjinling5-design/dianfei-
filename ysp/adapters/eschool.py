import math
from .base import BaseAdapter, Reading, SourceError
from ..eschool import EschoolClient, LOCK


class EschoolAdapter(BaseAdapter):
    name = 'eschool'

    def check(self):
        with LOCK:
            client = EschoolClient()
            if not client.status()['logged_in']: raise SourceError('请打开 /eschool 完成易校园短信登录。')
            client.electric('getCoutomConfig', {'customType':1})
            return '易校园登录与电费接口可用。'

    def fetch(self, room):
        with LOCK:
            client = EschoolClient()
            if client.state.get('room_key') != room.get('room_key'):
                raise SourceError('请先在易校园登录页为当前账号选择并保存宿舍。')
            fields = dict(zip(('areaId','buildingCode','floorCode','roomCode'),
                (str(room.get(k) or '') for k in ('area_id','building_code','floor_code','room_code'))))
            if not all(fields.values()): raise SourceError('请先在易校园登录页选择完整宿舍。')
            if room.get('school_code') and str(room['school_code']) != client.state.get('school_code'):
                raise SourceError('当前账号学校与宿舍不匹配，请重新选择宿舍。')
            data = client.electric('queryRoomSurplus', fields)['data']
            try: amount = float(data['amount'])
            except (KeyError,TypeError,ValueError): raise SourceError('易校园余额格式异常。') from None
            if not math.isfinite(amount): raise SourceError('易校园余额不是有效数值。')
            return Reading(room['room_key'],None,amount,source=self.name,unit='cny',
                           raw={'amount':amount,'roomName':data.get('displayRoomName','')})
