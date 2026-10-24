import json
import os
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def command(argv,cwd=None,timeout=20):
    # No shell parsing: model text cannot turn an argument into a second command.
    result=subprocess.run([str(a) for a in argv],cwd=cwd,capture_output=True,text=True,
                          encoding='utf-8',errors='replace',timeout=timeout,
                          env={**os.environ,'GCM_INTERACTIVE':'never','GIT_TERMINAL_PROMPT':'0'})
    return {'argv':[str(a) for a in argv],'exit_code':result.returncode,
            'stdout':result.stdout[-10000:],'stderr':result.stderr[-2000:]}


def tool_spec(name,description,properties=None,required=None):
    return {'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}}}


SPECS=[
    tool_spec('get_metrics','Read the current alert metrics with curl.'),
    tool_spec('get_traces','Read the current alert traces with curl.'),
    tool_spec('git_log','Read recent commits for the monitored source.'),
    tool_spec('git_show','Read a particular deployment commit.',{'commit':{'type':'string'}},['commit']),
    tool_spec('kubectl_read','Read Kubernetes observations. Only get/describe/top/logs.',
              {'operation':{'type':'string','enum':['get','describe','top','logs']},'resource':{'type':'string'}},['operation']),
    tool_spec('search_past_incidents','Search similar historical incidents through in-process MCP/Milvus.',
              {'query':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':5}},['query'])]


class DiagnosticTools:
    def __init__(self,settings,alert,hook,mcp,emit):
        self.settings,self.alert,self.hook,self.mcp,self.emit=settings,alert,hook,mcp,emit
        self.evidence={}

    async def call(self,name,args):
        self.hook.check('diagnose',name,args)
        if name in {'get_metrics','get_traces'}:
            if args:
                raise ValueError('Observation URLs cannot be overridden by the model')
            url=self.alert.metrics_url if name=='get_metrics' else self.alert.traces_url
            parts=urlsplit(url)
            if parts.scheme not in {'http','https'} or parts.username or parts.password or parts.hostname not in {'127.0.0.1','localhost','::1'}:
                raise PermissionError('This demo only fetches explicitly configured loopback observations')
            result=command([shutil.which('curl') or 'curl','--fail','--silent','--show-error','--max-time','5',url])
        elif name=='git_log':
            if args:
                raise ValueError('git_log takes no arguments')
            result=command(['git','log','-5','--oneline'],self.settings.source_repo)
        elif name=='git_show':
            commit=args.get('commit','')
            if set(args)!={'commit'} or not re.fullmatch('[0-9a-fA-F]{7,40}',commit):
                raise PermissionError('Only a commit hash may be inspected')
            result=command(['git','show','--no-ext-diff','--stat',commit],self.settings.source_repo)
        elif name=='kubectl_read':
            if set(args)-{'operation','resource'}:
                raise ValueError('Unexpected kubectl arguments')
            argv=[self.settings.kubectl,'--context',self.settings.kube_context,'--namespace',
                  self.settings.kube_namespace,args['operation'],args.get('resource','pods')]
            if self.settings.kubeconfig:
                argv[1:1]=['--kubeconfig',str(self.settings.kubeconfig)]
            result=command(argv,timeout=10)
        elif name=='search_past_incidents':
            response=await self.mcp.call_tool(name,args)
            if response.isError:
                raise RuntimeError('Incident memory tool failed')
            result={'transport':'official MCP in-memory session','content':[b.text for b in response.content if hasattr(b,'text')]}
        else:
            raise PermissionError('Unknown tool')
        key='e'+str(len(self.evidence)+1)
        self.evidence[key]={'tool':name,'result':result}
        self.emit('observation',{'id':key,**self.evidence[key]})
        return {'evidence_id':key,**result}
