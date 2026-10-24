"""Measure additional synthetic response-classification checks used in development."""
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiops.agents import DiagnosisAgent
from aiops.model import LocalModel
from aiops.schema import Diagnosis,Settings


def main():
    rows=[json.loads(line) for line in (ROOT/'eval/classification_checks.jsonl').read_text(encoding='utf-8').splitlines()]
    agent=DiagnosisAgent(LocalModel())
    settings=Settings(workspace=ROOT/'.storage',source_repo=ROOT)
    results=[]
    for case in rows:
        evidence={'e1':{'tool':'get_metrics','result':{'exit_code':0,'stdout':json.dumps(case['metrics'])}},
                  'e2':{'tool':'get_traces','result':{'exit_code':0,'stdout':json.dumps(case['traces'])}}}
        events=[]
        tools=SimpleNamespace(evidence=evidence,settings=settings,emit=lambda stage,data:events.append({'stage':stage,'data':data}))
        initial=Diagnosis(root_service=case['service'],cause='Classify current observations; this is not an end-to-end root-cause test.',
                          remediation_type='info_only',confidence=.8,evidence_ids=['e1','e2'])
        result=agent.classify(initial,tools)
        correct=result.remediation_type==case['expected']
        results.append({'id':case['id'],'expected':case['expected'],'predicted':result.remediation_type,'correct':correct,'events':events})
        print(f"[{len(results)}/{len(rows)}] {case['id']}: {result.remediation_type}; correct={correct}",flush=True)
    output={'measured_at_utc':datetime.now(timezone.utc).isoformat(),'model':agent.model.name,'cases':len(results),
            'correct':sum(r['correct'] for r in results),'accuracy':sum(r['correct'] for r in results)/len(results),
            'scope':'Additional synthetic classification checks used during development; expected labels are not passed to the model. Service identity is supplied. Not an untouched holdout, end-to-end or production benchmark.',
            'used_for_prompt_feedback':True,'expected_labels_passed_to_model':False,'results':results}
    (ROOT/'results/classification_checks_20261024.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in output.items() if k!='results'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
