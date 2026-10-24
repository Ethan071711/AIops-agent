"""Execute 13 new scenarios / 23 real model runs; never target a historical score."""
import asyncio
import json
import os
import platform
import statistics
import sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiops.memory import IncidentMemory
from aiops.model import LocalModel
from aiops.schema import Alert,Settings
from aiops.workflow import Workflow
from examples.observations import Observations


async def evaluate():
    cases=json.loads((ROOT/'eval/cases.json').read_text(encoding='utf-8'))['cases']
    fixture=Observations(cases).start()
    state=ROOT/'.storage/benchmark'
    state.mkdir(parents=True,exist_ok=True)
    settings=Settings(workspace=state,source_repo=ROOT,target_url=str(ROOT),
        kubectl=os.getenv('AIOPS_KUBECTL','kubectl'),kubeconfig=fixture.kubeconfig(state/'kubeconfig.json'),allow_restart=True)
    memory=IncidentMemory(state/'incidents.db')
    model=LocalModel()
    workflow=Workflow(settings,model,memory)
    details=[]
    try:
        for case in cases:
            for repeat in range(case['repeat']):
                alert=Alert(id=f"{case['id']}-{repeat}",summary=case['alert_summary'],
                    metrics_url=f'http://127.0.0.1:{fixture.port}/metrics/'+case['id'],
                    traces_url=f'http://127.0.0.1:{fixture.port}/traces/'+case['id'])
                result=await workflow.run(alert)
                expected=case['expected']
                diagnosis=result['diagnosis'] or {}
                row={'case':case['id'],'repeat':repeat,'expected':expected,
                     'service_correct':diagnosis.get('root_service')==expected['service'],
                     'kind_correct':diagnosis.get('remediation_type')==expected['remediation_type'],
                     'route_correct':result['route']==expected['route'],'result':result}
                details.append(row)
                print(f"[{len(details)}/23] {case['id']}: service={row['service_correct']} route={result['route']} {result['duration_seconds']}s",flush=True)
        times=[r['result']['duration_seconds'] for r in details]
        summary={'measured_at_utc':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
                 'model':model.name,'scenarios':len(cases),'runs':len(details),
                 'service_accuracy':sum(r['service_correct'] for r in details)/len(details),
                 'kind_accuracy':sum(r['kind_correct'] for r in details)/len(details),
                 'route_accuracy':sum(r['route_correct'] for r in details)/len(details),
                 'runtime_mean_seconds':statistics.mean(times),'runtime_p50_seconds':statistics.median(times),
                 'memory_backend':'actual Milvus Lite 3.2.2; BGE vectors; official in-process MCP',
                 'kubectl_backend':'actual kubectl client against an explicitly synthetic API fixture',
                 'kubernetes_patch_requests':sum(r['method']=='PATCH' for r in fixture.requests),
                 'paid_api_calls':0,'historical_results_recovered':False,
                 'limitations':['New synthetic observations, not original 2026 internship cases.',
                    'Model decisions, curl/git/kubectl calls, Milvus operations and test gates execute; the observation/Kubernetes server is a test fixture.',
                    'Repair tests use an isolated fault branch; GitHub PR publishing is validated separately in capstone.',
                    'Repeated cases share incident memory; this is a functional regression, not an independent generalization benchmark.',
                    'Runtime is not a measured production outage recovery time; no human 30–60 minute comparison was conducted.']}
        (ROOT/'results').mkdir(exist_ok=True)
        (ROOT/'results/regression_20261024.json').write_text(json.dumps({'summary':summary,'details':details},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    finally:
        memory.close();fixture.close()


if __name__=='__main__':
    asyncio.run(evaluate())
