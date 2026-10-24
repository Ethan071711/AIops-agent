"""Explicit synthetic observation/Kubernetes API fixture for regression tests."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Observations:
    def __init__(self,cases,port=0,live_metrics=None):
        self.cases={c['id']:c for c in cases}
        self.live_metrics=live_metrics
        self.requests=[]
        outer=self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def send(self,value,status=200):
                raw=json.dumps(value).encode()
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

            def do_GET(self):
                path=urlsplit(self.path).path
                outer.requests.append({'method':'GET','path':path})
                parts=path.strip('/').split('/')
                if len(parts)==2 and parts[0] in {'metrics','traces'} and parts[1] in outer.cases:
                    if outer.live_metrics and parts[0]=='metrics':
                        return self.send(outer.live_metrics())
                    return self.send(outer.cases[parts[1]][parts[0]])
                if path=='/version':
                    return self.send({'major':'1','minor':'35','gitVersion':'v1.35.0','platform':'fixture'})
                if path=='/api':
                    return self.send({'kind':'APIVersions','versions':['v1'],'serverAddressByClientCIDRs':[]})
                if path=='/apis':
                    return self.send({'kind':'APIGroupList','groups':[{'name':'apps','versions':[{'groupVersion':'apps/v1','version':'v1'}],
                        'preferredVersion':{'groupVersion':'apps/v1','version':'v1'}}]})
                if path=='/api/v1':
                    return self.send({'kind':'APIResourceList','groupVersion':'v1','resources':[
                        {'name':'pods','singularName':'pod','namespaced':True,'kind':'Pod','verbs':['get','list']}]})
                if path=='/apis/apps/v1':
                    return self.send({'kind':'APIResourceList','groupVersion':'apps/v1','resources':[
                        {'name':'deployments','singularName':'deployment','namespaced':True,'kind':'Deployment','verbs':['get','list','patch']}]})
                if '/deployments/' in path:
                    return self.send(outer.deployment(path.rsplit('/',1)[-1]))
                if path=='/api/v1/namespaces/aiops-demo/pods':
                    return self.send({'apiVersion':'v1','kind':'PodList','metadata':{'resourceVersion':'1'},'items':[
                        {'apiVersion':'v1','kind':'Pod','metadata':{'name':'inventory-fixture','namespace':'aiops-demo'},
                         'spec':{'containers':[{'name':'app','image':'fixture'}]},'status':{'phase':'Running'}}]})
                self.send({'kind':'Status','status':'Failure','reason':'NotFound','code':404},404)

            def do_PATCH(self):
                path=urlsplit(self.path).path
                payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
                outer.requests.append({'method':'PATCH','path':path,'body':payload})
                name=path.rsplit('/',1)[-1]
                if name not in {'inventory','worker'} or not path.startswith('/apis/apps/v1/namespaces/aiops-demo/deployments/'):
                    return self.send({'kind':'Status','status':'Failure','reason':'Forbidden','code':403},403)
                value=outer.deployment(name)
                value['spec']['template']['metadata'].update(payload.get('spec',{}).get('template',{}).get('metadata',{}))
                self.send(value)

        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    @staticmethod
    def deployment(name):
        return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':'aiops-demo','resourceVersion':'1','generation':1},
                'spec':{'replicas':1,'selector':{'matchLabels':{'app':name}},
                        'template':{'metadata':{'labels':{'app':name}},'spec':{'containers':[{'name':'app','image':'fixture'}]}}}}

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def kubeconfig(self,path):
        path.write_text(json.dumps({'apiVersion':'v1','kind':'Config',
            'clusters':[{'name':'fixture','cluster':{'server':f'http://127.0.0.1:{self.port}'}}],
            'contexts':[{'name':'aiops-demo','context':{'cluster':'fixture','user':'fixture','namespace':'aiops-demo'}}],
            'current-context':'aiops-demo','users':[{'name':'fixture','user':{}}]}),encoding='utf-8')
        return path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    rows=json.loads((root/'eval/cases.json').read_text(encoding='utf-8'))['cases']
    fixture=Observations(rows,args.port).start()
    print(f'Synthetic observations at http://127.0.0.1:{fixture.port}; this is not a Kubernetes cluster.')
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fixture.close()
