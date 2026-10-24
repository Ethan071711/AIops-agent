import argparse
import asyncio
import json
import os
from pathlib import Path

from aiops.memory import IncidentMemory
from aiops.model import LocalModel
from aiops.schema import Alert,Settings
from aiops.workflow import Workflow

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description='Diagnose an alert, risk-gate actions and test repairs before PR.')
    parser.add_argument('--alert',required=True,type=Path)
    parser.add_argument('--source-repo',type=Path,default=ROOT)
    parser.add_argument('--publish-pr',action='store_true',help='Push a tested repair and open a PR in the configured owner repository')
    args=parser.parse_args()
    settings=Settings(workspace=ROOT/'.storage',source_repo=args.source_repo.resolve(),publish_pr=args.publish_pr,
        kubectl=os.getenv('AIOPS_KUBECTL','kubectl'))
    alert=Alert.model_validate_json(args.alert.read_text(encoding='utf-8'))
    memory=IncidentMemory(os.getenv('MILVUS_URI',str(ROOT/'.storage/incidents.db')))
    try:
        result=asyncio.run(Workflow(settings,LocalModel(),memory).run(alert))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        memory.close()


if __name__=='__main__':
    main()
