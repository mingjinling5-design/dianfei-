"""Build a corresponding-source archive using an allowlist, never data or secrets."""
import zipfile
from pathlib import Path

root=Path(__file__).resolve().parent.parent
out=root/'release';out.mkdir(exist_ok=True)
with zipfile.ZipFile(out/'source.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for folder in ('portal','ysp','web','licenses','deploy'):
        for path in (root/folder).rglob('*'):
            rel=path.relative_to(root)
            if not path.is_file() or '__pycache__' in rel.parts or path.suffix=='.pyc':continue
            if folder=='deploy' and any(part in rel.parts for part in ('letsencrypt','backups','acme')):continue
            archive.write(path,str(rel).replace('\\','/'))
    for name in ('manage.py','requirements-server.txt','requirements.txt','THIRD_PARTY_NOTICES.md','Dockerfile','.dockerignore','compose.yaml','.env.example'):
        if (root/name).is_file():archive.write(root/name,name)
print('Corresponding source archive created; private data excluded.')
