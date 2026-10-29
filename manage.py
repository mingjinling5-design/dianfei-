import os
import sys
from pathlib import Path

deps = Path(__file__).parent/'data/server-deps'
if deps.is_dir(): sys.path.insert(0,str(deps))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','portal.settings')
if __name__ == '__main__':
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
