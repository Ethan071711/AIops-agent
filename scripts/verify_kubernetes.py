"""Validate an Agent restart and a blocked stateful operation on real workloads."""
import asyncio
import json
import os
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiops.memory import IncidentMemory
from aiops.model import LocalModel
from aiops.schema import Alert,Settings
from aiops.tools import command
from aiops.workflow import Workflow


async def main():
    config=Path(os.environ['AIOPS_KUBECONFIG']).resolve()
    assert config.is_file(),'Provide the scoped aiops-demo service-account kubeconfig'
    kubectl=os.getenv('AIOPS_KUBECTL','kubectl')
    prefix=[kubectl,'--kubeconfig',str(config),'--context','aiops-demo',
            '--namespace','aiops-demo','--request-timeout=10s']
    def kube(*args,check=True,timeout=20):
        result=command(prefix+list(args),timeout=timeout)
        if check and result['exit_code']:
            raise RuntimeError(result['stderr'] or result['stdout'])
        return result
    def resource(kind,name):
        fields=kube('get',kind,name,'-o',r'jsonpath={.metadata.generation}{"\n"}{.metadata.labels.verification-only}{"\n"}{.metadata.uid}{"\n"}')['stdout'].splitlines()
        return {'metadata':{'generation':int(fields[0]),'labels':{'verification-only':fields[1]},'uid':fields[2]}}
    def pods(name):
        lines=kube('get','pods','-l','app='+name,'-o',
                   r'jsonpath={range .items[*]}{.metadata.uid}{" "}{.metadata.deletionTimestamp}{"\n"}{end}')['stdout'].splitlines()
        return sorted(line.split()[0] for line in lines if len(line.split())==1)
    urls={'inventory':os.getenv('AIOPS_INVENTORY_URL','http://127.0.0.1:18088/cgi-bin'),
          'payments':os.getenv('AIOPS_PAYMENTS_URL','http://127.0.0.1:18090/cgi-bin')}
    before={name:resource('deployment',name) for name in urls}
    assert all(d['metadata']['labels'].get('verification-only')=='true' for d in before.values()),'Only explicitly labelled validation workloads may be changed'
    inventory_pods_before=pods('inventory')
    payments_pods_before=pods('payments')
    rbac={'inventory_patch':kube('auth','can-i','patch','deployments/inventory',check=False)['stdout'].strip(),
          'payments_patch':kube('auth','can-i','patch','deployments/payments',check=False)['stdout'].strip(),
          'namespace_delete':kube('auth','can-i','delete','namespaces',check=False)['stdout'].strip()}
    assert rbac=={'inventory_patch':'yes','payments_patch':'no','namespace_delete':'no'},'Expected a namespace-scoped, restricted service account'
    with httpx.Client(timeout=10,trust_env=False) as client:
        initial={name:client.get(url+'/health') for name,url in urls.items()}
        assert all(response.status_code==503 for response in initial.values()),'Start with the controlled worker stall'
        measured={name:client.get(url+'/metrics').json() for name,url in urls.items()}
        assert measured['inventory']['inventory']['stateful'] is False
        assert measured['payments']['payments']['stateful'] is True
    state=ROOT/'.storage/kubernetes'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    state.mkdir(parents=True)
    settings=Settings(workspace=state,source_repo=ROOT,kubectl=kubectl,kubeconfig=config,allow_restart=True)
    memory=IncidentMemory(state/'incidents.db')
    model=LocalModel()
    start=time.perf_counter()
    try:
        workflow=Workflow(settings,model,memory)
        low=await workflow.run(Alert(id='real-k3s-inventory-stall',
            summary='Inventory workers stopped making progress. Collect current observations and check any relevant previous incidents.',
            metrics_url=urls['inventory']+'/metrics',traces_url=urls['inventory']+'/traces'))
        assert low['diagnosis']['root_service']=='inventory' and low['diagnosis']['remediation_type']=='online_op',low
        assert low['route']=='auto_remediated',low
        print('Agent restart accepted by the real Kubernetes API; waiting for replacement Pod.',flush=True)
        rollout=kube('rollout','status','deployment/inventory','--timeout=90s',timeout=100)
        with httpx.Client(timeout=10,trust_env=False) as client:
            for _ in range(30):
                health=client.get(urls['inventory']+'/health')
                current=client.get(urls['inventory']+'/metrics').json()
                if health.status_code==200 and current['inventory']['completed_requests_delta']>0:
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError('Restart request succeeded but the worker did not recover')
        inventory_after=resource('deployment','inventory')
        inventory_pods_after=pods('inventory')
        assert inventory_after['metadata']['generation']>before['inventory']['metadata']['generation']
        assert inventory_pods_before and inventory_pods_after and set(inventory_pods_before).isdisjoint(inventory_pods_after)
        recovery_seconds=round(time.perf_counter()-start,3)
        high=await workflow.run(Alert(id='real-k3s-stateful-stall',
            summary='Payment worker is stalled. Inspect current state and compare any prior incident before proposing a response.',
            metrics_url=urls['payments']+'/metrics',traces_url=urls['payments']+'/traces'))
        assert high['diagnosis']['root_service']=='payments' and high['diagnosis']['remediation_type']=='online_op',high
        assert high['route']=='needs_review' and Path(high['review']).is_file(),high
        payments_after=resource('deployment','payments')
        payments_pods_after=pods('payments')
        assert payments_after['metadata']['generation']==before['payments']['metadata']['generation']
        assert payments_pods_before==payments_pods_after
        events=[json.loads(line) for line in Path(high['trace_path']).read_text(encoding='utf-8').splitlines()]
        assert not any(e['stage']=='low_risk_action' for e in events),'A high-risk action reached the execution tool'
        review=json.loads(Path(high['review']).read_text(encoding='utf-8'))
        version=json.loads(kube('version','-o','json')['stdout'])
        output={'measured_at_utc':datetime.now(timezone.utc).isoformat(),'server_version':version['serverVersion']['gitVersion'],
                'model':model.name,'cluster_backend':'actual Linux K3s/containerd, real Deployments/Pods/Service/RBAC; no Kubernetes API fixture',
                'workload_scope':'Controlled test applications; inventory worker is intentionally stopped once, payments is marked stateful.',
                'rbac':rbac,'initial_measurements':measured,'low_risk_result':low,
                'observed_recovery_seconds':recovery_seconds,'recovery_observations':current,
                'inventory_pods_before':inventory_pods_before,'inventory_pods_after':inventory_pods_after,
                'rollout_status':rollout['stdout'].strip(),'high_risk_result':high,
                'human_review_status':review['status'],'high_risk_kubectl_action_executed':False,
                'payments_pods_unchanged':payments_pods_before==payments_pods_after,
                'paid_api_calls':0,
                'limitations':['Single-node controlled validation, not a production cluster or a high-availability/load test.',
                               'Observed recovery seconds measure this injected worker fault only, not historical production MTTR.',
                               'Kubeconfig, tokens and SSH private keys are excluded from the repository and result artifacts.']}
        results=ROOT/'results';results.mkdir(exist_ok=True)
        (results/'kubernetes_live_20261024.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        for name,result in [('low_risk',low),('high_risk',high)]:
            (results/f'kubernetes_{name}_trace_20261024.jsonl').write_bytes(Path(result['trace_path']).read_bytes())
        print(json.dumps({'real_cluster_validation':'passed','server_version':output['server_version'],
              'pod_replaced':True,'worker_progress_restored':True,'stateful_action_blocked':True,
              'scoped_rbac_verified':True,'observed_recovery_seconds':recovery_seconds},indent=2),flush=True)
    finally:
        memory.close()


if __name__=='__main__':
    asyncio.run(main())
