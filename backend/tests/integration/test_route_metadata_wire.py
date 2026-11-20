"""Real uvicorn and socket upstream: provider fallback metadata and success body."""
import os, sys, socket, subprocess, tempfile, threading, time, json
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import httpx

def test_route_metadata_over_real_sockets():
    root=Path(__file__).resolve().parents[3]; modes=['first','second','all']
    class Handler(BaseHTTPRequestHandler):
     mode='first'; first=None
     def log_message(self,*args): pass
     def send(self,code,obj):
      b=json.dumps(obj).encode(); self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
     def do_GET(self):
      p=self.path.split('/')[1]; ids={'groq':'llama-3.1-8b-instant','deepseek':'deepseek-chat','gemini':'gemini-2.0-flash'}; model=ids[p]
      self.send(200, {'models':[{'name':'models/'+model,'supportedGenerationMethods':['generateContent']}]} if p=='gemini' else {'data':[{'id':model}]})
     def do_POST(self):
      p=self.path.split('/')[1]; self.rfile.read(int(self.headers.get('content-length',0)))
      if self.first is None: type(self).first=p
      if self.mode=='all' or (self.mode=='second' and p==self.first): return self.send(500,{'error':{'message':'stub failure'}})
      self.send(200, {'candidates':[{'content':{'parts':[{'text':'stub ok'}]}}],'usageMetadata':{'totalTokenCount':4}} if p=='gemini' else {'choices':[{'message':{'content':'stub ok'}}],'usage':{'total_tokens':4}})
    up=ThreadingHTTPServer(('127.0.0.1',0),Handler); threading.Thread(target=up.serve_forever,daemon=True).start(); port=up.server_port
    for mode in modes:
     Handler.mode=mode; Handler.first=None
     with tempfile.TemporaryDirectory() as d:
      with socket.socket() as sock: sock.bind(('127.0.0.1',0)); p=sock.getsockname()[1]
      pre=('from dataclasses import replace;import app.providers.model_registry as m;'+''.join(f"m.PROVIDER_SPECS['{x}']=replace(m.PROVIDER_SPECS['{x}'],base_url='http://127.0.0.1:{port}/{x}');" for x in ['groq','gemini','deepseek'])+f"import uvicorn;uvicorn.run('app.main:app',host='127.0.0.1',port={p},log_level='critical')")
      env=dict(os.environ, DATABASE_URL=f'sqlite+aiosqlite:///{d}/test.db',ROUTTENER_DATA_DIR=d,REDIS_URL='',REQUIRE_API_KEY='false',DEMO_MODE_ENABLED='false',ALLOWED_HOSTS='*',PYTHONPATH=str(root/'backend'),GROQ_API_KEY='fake-groq-key',GEMINI_API_KEY='fake-gemini-key',DEEPSEEK_API_KEY='fake-deepseek-key')
      with open(d+'/server.log','w+') as out:
       proc=subprocess.Popen([sys.executable,'-c',pre],cwd=root/'backend',env=env,stdout=out,stderr=subprocess.STDOUT)
       try:
        for _ in range(300):
         try:
          if httpx.get(f'http://127.0.0.1:{p}/health',timeout=1).status_code==200: break
         except Exception: time.sleep(.1)
        r=httpx.post(f'http://127.0.0.1:{p}/api/v1/route',json={'prompt':'Write code','task_type':'code','max_fallbacks':3},timeout=60)
    
        body=r.json(); assert r.status_code == 200
        assert body['success'] == (mode != 'all')
        if mode == 'first': assert body['error'] is None
        if mode == 'second': assert body['error'] is not None  # existing public behaviour; owner decision pending
        print('CASE',mode,'HTTP',r.status_code,'RAW_RESPONSE',json.dumps(body,sort_keys=True),flush=True)
       finally:
        proc.terminate();proc.wait(timeout=20);out.seek(0)
        for l in out:
         try: event=json.loads(l)
         except ValueError: continue
         if event.get('event') in ('execution_succeeded','all_providers_failed','route_endpoint_handled'):
          print('RAW_LOG',mode,json.dumps(event,sort_keys=True),flush=True)
          if event.get('event') == 'execution_succeeded': assert event['fallbacks_used'] == (0 if mode == 'first' else 1), event
    up.shutdown()
