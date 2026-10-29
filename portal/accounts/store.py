"""Owner-scoped ORM equivalent of the original analytics database interface."""
from datetime import datetime, timedelta
from django.db.models import Min, Max
from .models import Room, Reading, Event, Alert, Metadata


class TenantStore:
    tenant_store=True
    def __init__(self,owner):self.owner=owner
    def close(self):pass
    def commit(self):pass
    def execute(self,sql,args=()):
        if ' '.join(sql.split()).lower()=='update rooms set enabled=0 where room_key<>?':
            Room.objects.filter(owner=self.owner).exclude(room_key=args[0]).update(enabled=False)
            return self
        raise NotImplementedError('Raw SQL is unavailable in owner-scoped storage')
    def upsert_room(self,room):
        data={'unit_price':0.538,'warn_kwh':20,'critical_kwh':5,'school_code':'','school_name':'',**room}
        Room.objects.update_or_create(owner=self.owner,room_key=room['room_key'],defaults={'data':data,'enabled':bool(room.get('enabled',True))})
    def list_rooms(self,enabled_only=False):
        query=Room.objects.filter(owner=self.owner).order_by('room_key')
        if enabled_only:query=query.filter(enabled=True)
        return [{**r.data,'room_key':r.room_key,'enabled':r.enabled} for r in query]
    def get_room(self,room_key):
        return next((r for r in self.list_rooms() if r['room_key']==room_key),None)
    def insert_reading(self,room_key,ts,balance_kwh,balance_cny=None,source='',raw=None):
        if not Room.objects.filter(owner=self.owner,room_key=room_key).exists():return False
        _,created=Reading.objects.get_or_create(owner=self.owner,room_key=room_key,ts=ts,
            defaults={'balance_kwh':balance_kwh,'balance_cny':balance_cny,'source':source,'raw':raw})
        return created
    def fetch_readings(self,room_key,since=None,until=None):
        query=Reading.objects.filter(owner=self.owner,room_key=room_key)
        if since:query=query.filter(ts__gte=since)
        if until:query=query.filter(ts__lte=until)
        return list(query.order_by('ts').values('ts','balance_kwh','balance_cny'))
    def last_reading(self,room_key):
        return Reading.objects.filter(owner=self.owner,room_key=room_key).order_by('-ts').values('ts','balance_kwh','balance_cny').first()
    def count_readings(self,room_key=None):
        query=Reading.objects.filter(owner=self.owner)
        return (query.filter(room_key=room_key) if room_key else query).count()
    def coverage(self,room_key):
        query=Reading.objects.filter(owner=self.owner,room_key=room_key)
        return {**query.aggregate(first=Min('ts'),last=Max('ts')),'count':query.count()}
    def add_event(self,room_key,ts,kind,value=0.0,note=''):
        Event.objects.get_or_create(owner=self.owner,room_key=room_key,ts=ts,kind=kind,defaults={'value':value,'note':note})
    def fetch_events(self,room_key,since=None):
        query=Event.objects.filter(owner=self.owner,room_key=room_key)
        if since:query=query.filter(ts__gte=since)
        return list(query.order_by('ts').values('ts','kind','value','note'))
    def add_alert(self,room_key,level,message):
        return Alert.objects.create(owner=self.owner,room_key=room_key,level=level,message=message,ts=datetime.now().isoformat(timespec='seconds')).pk
    def list_alerts(self,room_key=None,limit=100):
        query=Alert.objects.filter(owner=self.owner)
        if room_key:query=query.filter(room_key=room_key)
        return list(query.order_by('-ts').values('id','room_key','ts','level','message','acked')[:limit])
    def recent_alert(self,room_key,level,within_hours):
        since=(datetime.now()-timedelta(hours=within_hours)).isoformat(timespec='seconds')
        return Alert.objects.filter(owner=self.owner,room_key=room_key,level=level,ts__gte=since).order_by('-ts').values().first()
    def ack_alert(self,alert_id):Alert.objects.filter(owner=self.owner,pk=alert_id).update(acked=True)
    def set_meta(self,k,v):Metadata.objects.update_or_create(owner=self.owner,key=k,defaults={'value':v})
    def get_meta(self,k,default=None):
        row=Metadata.objects.filter(owner=self.owner,key=k).first()
        return row.value if row else default
    def bulk_insert(self,rows):return sum(self.insert_reading(*row) for row in rows)
