import json
import sqlite3
import uuid
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.conf import settings
from portal.accounts.models import Account, Room, Reading, Event, Alert, Metadata
from portal.accounts.state import write_state


class Command(BaseCommand):
    help='Migrate the original local owner and history without issuing a browser login.'
    def handle(self,*args,**options):
        statefile=settings.BASE_DIR/'data/eschool_session.json'
        database=settings.BASE_DIR/'data/power.db'
        if not statefile.exists() or not database.exists():raise CommandError('No local data to import')
        state=json.loads(statefile.read_text(encoding='utf-8'))
        if not state.get('ym_id') or not (state.get('verified_login') or state.get('token')):
            raise CommandError('Local owner has not completed upstream login')
        with transaction.atomic():
            account,_=Account.objects.get_or_create(provider_uid=str(state['ym_id']))
            if Metadata.objects.filter(owner=account,key='local_import_complete').exists():
                self.stdout.write('Local migration already complete');return
            if account.user_id is None:
                account.user=get_user_model().objects.create_user(username=uuid.uuid4().hex,password=None)
                account.save(update_fields=['user'])
            if not account.encrypted_state:write_state(account,state)
            source=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
            source.row_factory=sqlite3.Row
            rooms=[dict(r) for r in source.execute('SELECT * FROM rooms')]
            for row in rooms:Room.objects.get_or_create(owner=account,room_key=row['room_key'],defaults={'enabled':bool(row['enabled']),'data':row})
            records=[dict(r) for r in source.execute('SELECT * FROM readings')]
            for row in records:
                raw=row.get('raw')
                try:raw=json.loads(raw) if raw else None
                except ValueError:raw=None
                Reading.objects.get_or_create(owner=account,room_key=row['room_key'],ts=row['ts'],defaults={
                    'balance_kwh':row['balance_kwh'],'balance_cny':row['balance_cny'],'source':row.get('source',''),'raw':raw})
            for row in source.execute('SELECT * FROM events'):
                Event.objects.get_or_create(owner=account,room_key=row['room_key'],ts=row['ts'],kind=row['kind'],defaults={'value':row['value'],'note':row['note']})
            for row in source.execute('SELECT * FROM alerts'):
                Alert.objects.get_or_create(owner=account,room_key=row['room_key'],ts=row['ts'],level=row['level'],message=row['message'],defaults={'acked':bool(row['acked'])})
            for row in source.execute('SELECT * FROM meta'):
                if row['k'] not in ('native_unit','last_collect','fail_streak','model_state'):continue
                try:value=json.loads(row['v'])
                except ValueError:continue
                Metadata.objects.get_or_create(owner=account,key=row['k'],defaults={'value':value})
            source.close()
            if not account.config:
                account.config={'rooms':[r for r in rooms if r['enabled']]};account.save(update_fields=['config'])
            Metadata.objects.create(owner=account,key='local_import_complete',value=True)
        self.stdout.write(f'Migrated local owner: {len(rooms)} rooms, {len(records)} readings. Sign in through OTP to access.')
