import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from portal.accounts.models import Account
from portal.accounts.worker import collect_account


class Command(BaseCommand):
    help='Collect each account independently; PostgreSQL row locks prevent duplicate workers.'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        while True:
            close_old_connections()
            ids=list(Account.objects.filter(user__isnull=False,next_collect__lte=timezone.now()).values_list('pk',flat=True))
            for pk in ids:
                try:collect_account(pk)
                except Exception as exc:
                    self.stderr.write('Collection failed: '+type(exc).__name__)
                    Account.objects.filter(pk=pk).update(next_collect=timezone.now()+timedelta(minutes=10))
            Account.objects.filter(user__isnull=True,provider_uid__isnull=True,created_at__lt=timezone.now()-timedelta(days=1)).delete()
            if options['once']:break
            time.sleep(15)
