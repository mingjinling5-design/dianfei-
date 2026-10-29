from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from portal.accounts.models import Account
from portal.accounts.state import read_state


class Command(BaseCommand):
    help = 'Grant the portal admin permission to an existing account by its verified phone number.'

    def add_arguments(self, parser):
        parser.add_argument('--phone', required=True)

    def handle(self, *args, **options):
        phone=''.join(c for c in options['phone'] if c.isdigit())
        matches=[]
        for account in Account.objects.filter(user__isnull=False).select_related('user'):
            saved=''.join(c for c in str(read_state(account).get('phone','')) if c.isdigit())
            if saved and saved==phone:matches.append(account)
        if len(matches)!=1:raise CommandError('Phone must match exactly one existing logged-in portal account.')
        user=matches[0].user
        user.is_staff=True
        user.save(update_fields=['is_staff'])
        self.stdout.write(self.style.SUCCESS('Admin access granted. Open /admin/users after signing in.'))
