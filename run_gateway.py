#!/usr/bin/env python3
import os
import uvicorn

if __name__ == '__main__':
    host=os.getenv('VIDIGEN_HOST','127.0.0.1')
    port=int(os.getenv('VIDIGEN_PORT','8787'))
    uvicorn.run('gateway.server:app',host=host,port=port,reload=False)
