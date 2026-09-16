"""Dependency-light production smoke checks for the gateway."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN','test-token')
os.environ.setdefault('WORKFLOW_FILE','/tmp/vidigen-test-missing-workflow.json')
from httpx import ASGITransport, AsyncClient
from gateway.server import app
import asyncio

async def main():
    t=ASGITransport(app=app)
    async with AsyncClient(transport=t,base_url='http://test') as c:
        checks=[]
        r=await c.get('/health'); checks.append(('auth required',r.status_code==401))
        r=await c.get('/health',headers={'Authorization':'Bearer test-token'}); checks.append(('health',r.status_code==200 and r.json().get('ok') is True))
        r=await c.post('/api/analyze',headers={'Authorization':'Bearer test-token'},json={'prompt':'cinematic product ad'}); checks.append(('analyze',r.status_code==200 and bool(r.json().get('type'))))
        r=await c.post('/api/generate',headers={'Authorization':'Bearer test-token'},json={'prompt':'x','ratio':'bad'}); checks.append(('validation',r.status_code==422))
        failed=[name for name,ok in checks if not ok]
        for name,ok in checks: print(('PASS' if ok else 'FAIL'),name)
        if failed: print('Failed:',', '.join(failed)); return 1
        print(f'{len(checks)}/{len(checks)} smoke checks passed'); return 0

if __name__=='__main__': sys.exit(asyncio.run(main()))
