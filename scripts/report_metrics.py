"""Recompute regression labels and real controlled recovery timings from saved evidence."""
import argparse
import hashlib
import json
import sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiops.evaluation import classification_metrics,recovery_metrics,verified_recovery_seconds


def main(regression,recoveries,output):
    labeled=json.loads(regression.read_text('utf-8'))
    recovery=json.loads(recoveries.read_text('utf-8'))
    for trial in recovery['trials']:
        path=ROOT/trial['trace_artifact']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==trial['trace_sha256'],'Trace changed'
        events=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        assert verified_recovery_seconds(events)==trial['alert_to_health_verified_seconds']
    report={'measured_at_utc':datetime.now(timezone.utc).isoformat(),
        'classification':classification_metrics(labeled['details']),
        'controlled_recovery':recovery_metrics(recovery['trials']),
        'sources':{path.relative_to(ROOT).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in [regression,recoveries]},
        'scope':'New synthetic functional regression labels and real single-node controlled K3s trials. They have separate denominators.',
        'limitations':['Exact-label accuracy is not arbitrary root-cause correctness on production incidents.',
            'Successful controlled recovery intervals are not production MTTR; failed observations stay visible.',
            'Small repeated cases share a model, cluster and incident memory; no human 30–60 minute comparison.']}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regression',type=Path,default=ROOT/'results/regression_20261024.json')
    parser.add_argument('--recoveries',type=Path,default=ROOT/'results/recovery_trials_20261025.json')
    parser.add_argument('--output',type=Path,default=ROOT/'results/metrics_20261025.json')
    args=parser.parse_args();main(args.regression,args.recoveries,args.output)
