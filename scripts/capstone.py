"""Actual bounded-history fault, real model repair, real tests and an owner-repository PR."""
import asyncio
import importlib.util
import json
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


def load_service(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main():
    source=ROOT/'.storage/fault-branch'
    service=load_service(source/'examples/repair_service.py','faulted_workload')
    for i in range(1000):
        service.remember_request('request-'+str(i))
    before=len(service.REQUESTS)
    assert before>64,'Capstone must begin with a reproduced unbounded history fault'
    cases=json.loads((ROOT/'eval/cases.json').read_text(encoding='utf-8'))['cases']
    fixture=Observations(cases,live_metrics=lambda:{'catalog':{'cache_entries':len(service.REQUESTS),
        'history_expected_limit':64,'measurement':'actual running Python sample workload'}}).start()
    state=ROOT/'.storage/capstone';state.mkdir(parents=True,exist_ok=True)
    settings=Settings(workspace=state,source_repo=source,publish_pr=True)
    memory=IncidentMemory(state/'incidents.db')
    try:
        alert=Alert(id='capstone-history-growth',summary='Catalog retains request history after processing; memory grows after deployment.',
            metrics_url=f'http://127.0.0.1:{fixture.port}/metrics/memory',
            traces_url=f'http://127.0.0.1:{fixture.port}/traces/memory')
        result=await Workflow(settings,LocalModel(),memory).run(alert)
        assert result['route']=='code_fix_pr',result
        assert not result['fix']['baseline']['passed'] and result['fix']['verification']['passed']
        assert result['fix']['pr_url'].startswith('https://github.com/Ethan071711/AIops-agent/pull/')
        patched=load_service(state/'clones'/result['run_id']/'examples/repair_service.py','repaired_workload')
        for i in range(1000):
            patched.remember_request('request-'+str(i))
        after=len(patched.REQUESTS)
        assert after<=64 and patched.REQUESTS[-1]=='request-999'
        result.update(measured_at_utc=datetime.now(timezone.utc).isoformat(),
                      observed_before_entries=before,observed_after_entries=after,
                      samples_processed=1000,original_source_modified=False,
                      provenance='New actual sample-workload run; not historic internship metrics',
                      trace_artifact='capstone_trace_20261024.jsonl')
        output=ROOT/'results';output.mkdir(exist_ok=True)
        (output/'capstone_20261024.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/'capstone_trace_20261024.jsonl').write_bytes(Path(result['trace_path']).read_bytes())
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        memory.close();fixture.close()


if __name__=='__main__':
    asyncio.run(main())
