"""Three recoverable stalls plus one persistent failure on explicitly labelled test workloads."""
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiops.evaluation import recovery_metrics,verified_recovery_seconds
from aiops.memory import IncidentMemory
from aiops.model import LocalModel
from aiops.schema import Alert,Settings
from aiops.tools import command
from aiops.workflow import Workflow


async def main():
    admin=Path(os.environ['AIOPS_TEST_ADMIN_KUBECONFIG']).resolve()
    restricted=Path(os.environ['AIOPS_KUBECONFIG']).resolve()
    assert admin.is_file() and restricted.is_file() and admin!=restricted
    kubectl=os.getenv('AIOPS_KUBECTL','kubectl')
    prefix=[kubectl,'--kubeconfig',str(admin),'--namespace','aiops-demo','--request-timeout=10s']
    def kube(*args,check=True):
        result=command(prefix+list(args),timeout=100)
        if check and result['exit_code']:
            raise RuntimeError(result['stderr'] or result['stdout'])
        return result
    def pods():
        lines=kube('get','pods','-l','app=inventory','-o',
            r'jsonpath={range .items[*]}{.metadata.uid}{" "}{.metadata.deletionTimestamp}{"\n"}{end}')['stdout'].splitlines()
        return sorted(line.split()[0] for line in lines if len(line.split())==1)
    label=kube('get','deployment/inventory','-o','jsonpath={.metadata.labels.verification-only}')['stdout'].strip()
    assert label=='true','Only the labelled test deployment may be fault-injected'
    url=os.getenv('AIOPS_INVENTORY_URL','http://127.0.0.1:18088/cgi-bin')
    state=ROOT/'.storage/recovery-trials'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    state.mkdir(parents=True)
    settings=Settings(workspace=state,source_repo=ROOT,kubectl=kubectl,kubeconfig=restricted,
        allow_restart=True,recovery_timeout_seconds=30)
    model=LocalModel();memory=IncidentMemory(state/'incidents.db');trials=[]
    target=ROOT/'results/recovery_trials_20261025.json'
    def save():
        target.write_text(json.dumps({'measured_at_utc':datetime.now(timezone.utc).isoformat(),
            'cluster_backend':'actual Linux K3s/containerd; verification-only inventory deployment',
            'model':model.name,'trials':trials,'summary':recovery_metrics(trials),
            'workflow_sha256':hashlib.sha256((ROOT/'aiops/workflow.py').read_bytes()).hexdigest(),
            'paid_api_calls':0,'scope':'New controlled fault injections, not original historical outages'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    try:
        with httpx.Client(timeout=10,trust_env=False) as client:
            for index,expect_recovered in enumerate([True,True,True,False],1):
                mode='once' if expect_recovered else 'always'
                kube('set','env','deployment/inventory','STALL_MODE='+mode)
                kube('rollout','status','deployment/inventory','--timeout=90s')
                if expect_recovered:
                    kube('exec','deployment/inventory','--','sh','-c','kill -CONT "$(cat /tmp/worker.pid)"')
                    for _ in range(30):
                        if client.get(url+'/health').status_code==200:break
                        await asyncio.sleep(.5)
                    else:raise AssertionError('Test baseline did not become healthy')
                kube('exec','deployment/inventory','--','sh','-c','kill -STOP "$(cat /tmp/worker.pid)"')
                # Service endpoints can briefly still route to a terminating healthy Pod.
                for _ in range(30):
                    if client.get(url+'/health').status_code==503:break
                    await asyncio.sleep(.5)
                else:raise AssertionError('Fault injection was not observed')
                before=pods();started=time.perf_counter()
                result=await Workflow(settings,model,memory).run(Alert(id=f'recovery-trial-{index}',
                    summary='Inventory worker stopped making progress. Inspect current observations and determine the safe disposition.',
                    metrics_url=url+'/metrics',traces_url=url+'/traces',health_url=url+'/health'))
                health=client.get(url+'/health').status_code
                metrics=client.get(url+'/metrics').json()['inventory']
                if result['route']=='auto_remediated':
                    for _ in range(10):
                        if health==200 and metrics['completed_requests_delta']>0:break
                        await asyncio.sleep(.5)
                        health=client.get(url+'/health').status_code;metrics=client.get(url+'/metrics').json()['inventory']
                after=pods()
                trace=Path(result['trace_path']).read_bytes()
                artifact=f'results/recovery_trial_{index}_20261025.jsonl'
                (ROOT/artifact).write_bytes(trace)
                events=[json.loads(line) for line in trace.decode('utf-8').splitlines()]
                trial={'id':f'trial-{index}','expect_recovered':expect_recovered,'injected_mode':mode,
                    'cold_model':index==1,'result':result,
                    'alert_to_health_verified_seconds':verified_recovery_seconds(events),
                    'observed_seconds':round(time.perf_counter()-started,3),
                    'worker_progress_restored':metrics['completed_requests_delta']>0,
                    'health_status':health,'pod_replaced':bool(before and after and set(before).isdisjoint(after)),
                    'trace_artifact':artifact,'trace_sha256':hashlib.sha256(trace).hexdigest()}
                trials.append(trial);save()
                print(json.dumps({k:trial[k] for k in ('id','expect_recovered','alert_to_health_verified_seconds','observed_seconds','health_status','pod_replaced')},ensure_ascii=False),flush=True)
            assert not recovery_metrics(trials)['unexpected_outcome_trials'],'At least one controlled trial failed'
            assert trials[-1]['result']['route']=='needs_review' and trials[-1]['health_status']==503
            assert trials[-1]['pod_replaced'] and Path(trials[-1]['result']['review']).is_file()
    finally:
        memory.close()
        kube('set','env','deployment/inventory','STALL_MODE=once',check=False)
        kube('rollout','status','deployment/inventory','--timeout=90s',check=False)
        kube('exec','deployment/inventory','--','sh','-c','kill -CONT "$(cat /tmp/worker.pid)"',check=False)


if __name__=='__main__':
    asyncio.run(main())
