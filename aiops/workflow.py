import json
import threading
import time
import uuid

from mcp.shared.memory import create_connected_server_and_client_session

from .agents import DiagnosisAgent,RepairAgent
from .memory import memory_server
from .policy import PreToolUse
from .tools import DiagnosticTools,command


class Workflow:
    def __init__(self,settings,model,memory):
        self.settings,self.model,self.memory=settings,model,memory

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
                        argv=[self.settings.kubectl,'--context',self.settings.kube_context,'--namespace',
                              self.settings.kube_namespace,'rollout','restart','deployment/'+action['deployment']]
                        if self.settings.kubeconfig:
                            argv[1:1]=['--kubeconfig',str(self.settings.kubeconfig)]
                        response=command(argv,timeout=15)
                        emit('low_risk_action',response)
                        result['route']='auto_remediated' if response['exit_code']==0 else 'needs_review'
                        if result['route']=='needs_review':
                            result['review']=hook.review(run_id,action,'Allowed restart did not complete')
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
