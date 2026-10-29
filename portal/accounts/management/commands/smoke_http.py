"""Temporary two-account test through a running local HTTP server; no upstream SMS."""
import http.cookiejar
import json
import urllib.request
import uuid
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.contrib.sessions.models import Session
from django.test import Client
from portal.accounts.models import Account
from portal.accounts.store import TenantStore


class Command(BaseCommand):
    def add_arguments(self,parser):parser.add_argument('--url',default='http://127.0.0.1:8787')
    def handle(self,*args,**options):
        users=[];sessions=[]
        try:
            for amount in (13.25,86.75):
                user=get_user_model().objects.create_user(username='smoke-'+uuid.uuid4().hex,password=None)
                users.append(user.pk)
                account=Account.objects.create(user=user)
                store=TenantStore(account)
                store.upsert_room({'room_key':'SAME','label':'smoke-room'})
                store.set_meta('native_unit','cny')
                store.insert_reading('SAME','2026-10-29T12:00:00',None,amount)
                client=Client();client.force_login(user)
                token=client.cookies[settings.SESSION_COOKIE_NAME].value
                sessions.append(token)
                request=urllib.request.Request(options['url']+'/api/summary',headers={'Cookie':settings.SESSION_COOKIE_NAME+'='+token})
                with urllib.request.urlopen(request,timeout=15) as response:result=json.load(response)
                if result.get('balance')!=amount:raise CommandError('HTTP account isolation failed')
            self.stdout.write('PASS: two independent HTTP sessions see only their own balances.')
        finally:
            Session.objects.filter(session_key__in=sessions).delete()
            get_user_model().objects.filter(pk__in=users).delete()
