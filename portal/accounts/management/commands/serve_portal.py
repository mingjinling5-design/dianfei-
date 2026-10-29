from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help='Serve through Waitress for local Windows use; production uses Gunicorn.'
    def add_arguments(self,parser):parser.add_argument('--port',type=int,default=8787)
    def handle(self,*args,**options):
        from portal.wsgi import application
        from waitress import serve
        self.stdout.write('Local multi-user portal: http://127.0.0.1:'+str(options['port']))
        serve(application,host='127.0.0.1',port=options['port'],threads=8,clear_untrusted_proxy_headers=True)
