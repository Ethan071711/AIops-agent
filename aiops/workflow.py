import asyncio
import json
import subprocess
import threading
import time
import uuid

from mcp.shared.memory import create_connected_server_and_client_session

from .agents import DiagnosisAgent,RepairAgent
from .memory import memory_server
from .policy import PreToolUse
from .tools import DiagnosticTools,command,fetch_observation,validate_observation_url


class Workflow:
    def __init__(self,settings,model,memory):
        self.settings,self.model,self.memory=settings,model,memory

    async def verify_recovery(self,alert,service,hook,emit):
        start=time.monotonic()
        deadline=start+self.settings.recovery_timeout_seconds
        prefix=[self.settings.kubectl,'--context',self.settings.kube_context,
                '--namespace',self.settings.kube_namespace,'--request-timeout=5s']
        if self.settings.kubeconfig:
            prefix[1:1]=['--kubeconfig',str(self.settings.kubeconfig)]
        fields=['metadata.generation','status.observedGeneration','spec.replicas','status.replicas',
                'status.updatedReplicas','status.readyReplicas','status.availableReplicas']
        expression='jsonpath='+'{"|"}'.join('{.'+field+'}' for field in fields)
        checks=0
        while time.monotonic()<deadline:
            checks+=1
            observed={'attempt':checks,'rollout_ready':False,'health_status':None}
            try:
                hook.check('diagnose','kubectl_read',{'operation':'get','resource':'deployment/'+service})
                response=command(prefix+['get','deployment/'+service,'-o',expression],
                                 timeout=max(.1,min(10,deadline-time.monotonic())))
                if response['exit_code']==0:
                    values=[int(v or 0) for v in response['stdout'].strip().split('|')]
                    generation,seen,desired,total,updated,ready,available=values
                    observed['deployment']=dict(zip(fields,values))
                    observed['rollout_ready']=(generation>0 and seen>=generation and desired>0
                                                and total==updated==ready==available==desired)
                if observed['rollout_ready'] and time.monotonic()<deadline:
                    hook.check('diagnose','get_health',{})
                    health=fetch_observation(alert.health_url,timeout=max(.1,min(6,deadline-time.monotonic())),include_status=True)
                    observed['health_status']=health['http_status']
                    if health['exit_code']==0 and health['http_status']==200 and time.monotonic()<deadline:
                        emit('recovery_verified',observed)
                        return {'recovered':True,'checks':checks,'last_check':observed,
                                'elapsed_seconds':round(time.monotonic()-start,3)}
            except (subprocess.TimeoutExpired,ValueError) as error:
                observed['error']=type(error).__name__
            emit('recovery_check',observed)
            await asyncio.sleep(min(1,max(0,deadline-time.monotonic())))
        return {'recovered':False,'checks':checks,'last_check':observed,
                'elapsed_seconds':round(time.monotonic()-start,3),
                'reason':'Deployment readiness and HTTP 200 health were not both confirmed before the deadline'}

    async def run(self,alert):
        start=time.perf_counter()
        run_id=uuid.uuid4().hex
        directory=self.settings.workspace/'runs'/run_id
        directory.mkdir(parents=True)
        lock=threading.Lock()

        def emit(stage,data):
            event={'stage':stage,'seconds':round(time.perf_counter()-start,3),'data':data}
            with lock,(directory/'trace.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(event,ensure_ascii=False)+'\n')

        hook=PreToolUse(self.settings,emit)
        result={'run_id':run_id,'alert_id':alert.id,'diagnosis':None,'route':'needs_review','fix':None,
                'errors':[],'paid_api_calls':0}
        diagnosis=None
        emit('alert',alert.model_dump())
        try:
            async with create_connected_server_and_client_session(memory_server(self.memory)) as session:
                tools=DiagnosticTools(self.settings,alert,hook,session,emit)
                diagnosis=await DiagnosisAgent(self.model).run(alert,tools,self.settings.max_tool_rounds)
                result['diagnosis']=diagnosis.model_dump()
                emit('diagnosis',result['diagnosis'])
                if diagnosis.confidence<0.6 or diagnosis.root_service=='unknown':
                    result['review']=hook.review(run_id,diagnosis.action,'Insufficient diagnostic evidence')
                elif diagnosis.remediation_type=='code_fix':
                    fix=RepairAgent(self.model,self.settings,hook,emit).run(run_id,diagnosis)
                    result['fix']=fix
                    result['route']='code_fix_pr' if fix['status']=='pr_created' else 'code_fix_verified' if fix['status']=='verified_local' else 'needs_review'
                    if result['route']=='needs_review':
                        result['review']=hook.review(run_id,diagnosis.model_dump(),'Repair did not pass the build/test gate')
                elif diagnosis.remediation_type=='online_op':
                    action=diagnosis.action or {}
                    if hook.online(action,diagnosis.root_service,tools.evidence):
                        if not alert.health_url:
                            raise ValueError('Automatic restart requires an operator-configured health_url')
                        validate_observation_url(alert.health_url)
                        argv=[self.settings.kubectl,'--context',self.settings.kube_context,'--namespace',
                              self.settings.kube_namespace,'rollout','restart','deployment/'+action['deployment']]
                        if self.settings.kubeconfig:
                            argv[1:1]=['--kubeconfig',str(self.settings.kubeconfig)]
                        response=command(argv,timeout=15)
                        emit('low_risk_action',response)
                        if response['exit_code']==0:
                            result['recovery']=await self.verify_recovery(alert,diagnosis.root_service,hook,emit)
                            if result['recovery']['recovered']:
                                result['route']='auto_remediated'
                        if result['route']=='needs_review':
                            result['review']=hook.review(run_id,action,'Restart failed or service recovery was not confirmed')
                    else:
                        result['review']=hook.review(run_id,action,'High-risk or non-allowlisted online action was blocked')
                else:
                    result['route']='info_only'
        except Exception as error:
            while isinstance(error,BaseExceptionGroup) and len(error.exceptions)==1:
                error=error.exceptions[0]
            result['errors'].append({'kind':type(error).__name__,'detail':str(error)[:500]})
            result['review']=hook.review(run_id,{'alert':alert.id},'Agent failure: '+type(error).__name__)
        if diagnosis is not None:
            try:
                self.memory.add(run_id,alert,diagnosis,result['route'])
                emit('memory_saved',{'database':'Milvus','incident_id':run_id})
            except Exception as error:
                result['errors'].append({'kind':type(error).__name__,'detail':'Incident memory write failed'})
                result['review']=hook.review(run_id,{'incident_id':run_id},'Milvus write failed; diagnosis trace retained locally')
        result['duration_seconds']=round(time.perf_counter()-start,3)
        result['trace_path']=str(directory/'trace.jsonl')
        (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
