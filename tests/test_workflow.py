import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aiops.agents import DiagnosisAgent,RepairAgent
from aiops.memory import IncidentMemory,memory_server
from aiops.model import json_value
from aiops.policy import PreToolUse,apply_changes,validate_python_patch
from aiops.schema import Alert,Diagnosis,Patch,Settings

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=tmp_path/'state',source_repo=ROOT)


def diagnosis():
    return Diagnosis(root_service='catalog',cause='Request cache grows without bound',remediation_type='code_fix',
                     confidence=.9,evidence_ids=['e1','e2'])


class TinyEmbedder:
    def encode(self,text):
        return [1.,0.,0.,0.]


def test_milvus_persists_and_official_inprocess_mcp_queries_real_records(tmp_path):
    path=tmp_path/'incidents.db'
    memory=IncidentMemory(path,TinyEmbedder(),4)
    alert=Alert(id='test',summary='cache growing',metrics_url='http://localhost/metrics',traces_url='http://localhost/traces')
    memory.add('test-record',alert,diagnosis(),'code_fix_verified')
    memory.close()
    reopened=IncidentMemory(path,TinyEmbedder(),4)
    async def check():
        async with create_connected_server_and_client_session(memory_server(reopened)) as client:
            tools=await client.list_tools()
            assert [t.name for t in tools.tools]==['search_past_incidents']
            result=await client.call_tool('search_past_incidents',{'query':'cache growing'})
            assert not result.isError
            content=json.loads(next(b.text for b in result.content if hasattr(b,'text')))
            assert content['incidents'][0]['id']=='test-record'
            bad=await client.call_tool('search_past_incidents',{'query':'cache','limit':999})
            assert bad.isError
    try:
        asyncio.run(check())
    finally:
        reopened.close()


@pytest.mark.parametrize('name',['shell','delete_database','apply_patch','open_pr'])
def test_diagnosis_whitelist_and_blacklist(settings,name):
    with pytest.raises(PermissionError):
        PreToolUse(settings).check('diagnose',name,{})


@pytest.mark.parametrize('args',[{'operation':'delete','resource':'pods'},{'operation':'get','resource':'pods; rm -rf /'},
                                 {'operation':'get','resource':'--all-namespaces'}])
def test_kubectl_mutations_and_injection_are_blocked(settings,args):
    with pytest.raises(PermissionError):
        PreToolUse(settings).check('diagnose','kubectl_read',args)


def test_low_risk_scope_and_high_risk_review(settings):
    hook=PreToolUse(settings)
    assert not hook.online({'operation':'restart','deployment':'inventory'})
    settings.allow_restart=True
    assert hook.online({'operation':'restart','deployment':'inventory'})
    assert not hook.online({'operation':'restart','deployment':'payments'})
    assert not hook.online({'operation':'flush_redis','deployment':'inventory'})
    path=hook.review('run-test',{'operation':'drop_database'},'high risk')
    assert json.loads(Path(path).read_text())['status']=='pending'


@pytest.mark.parametrize('source',['import os\nos.remove("x")','__builtins__["eval"]("1")',
                                  'getattr(1,"__class__")','x=().__class__'])
def test_generated_code_cannot_introduce_io_or_reflection(source):
    with pytest.raises(PermissionError):
        validate_python_patch(source)


def test_patch_is_exact_bounded_and_cannot_modify_tests(tmp_path):
    path=tmp_path/'service.py';path.write_text('VALUE=1\n')
    patch=Patch(explanation='fix value',changes=[{'path':'service.py','old':'VALUE=1','new':'VALUE=2'}])
    apply_changes(tmp_path,patch,['service.py'])
    assert path.read_text()=='VALUE=2\n'
    with pytest.raises(ValueError):
        apply_changes(tmp_path,patch,['service.py'])
    patch.changes[0].path='../test.py'
    with pytest.raises(PermissionError):
        apply_changes(tmp_path,patch,['service.py'])


def test_diagnosis_must_cite_observed_metrics_and_traces():
    class Model:
        def chat(self,*args):
            return {'content':diagnosis().model_dump_json(),'tool_calls':[]}
    class Tools:
        evidence={'e1':{'tool':'get_metrics','result':{'exit_code':0}}}
    alert=Alert(id='test',summary='cache issue',metrics_url='http://localhost',traces_url='http://localhost')
    with pytest.raises(ValueError):
        asyncio.run(DiagnosisAgent(Model()).run(alert,Tools()))


def test_json_parser_accepts_fenced_output_but_not_free_text():
    assert json_value('```json\n{"a":1}\n```')=={'a':1}
    with pytest.raises(ValueError):
        json_value('no JSON')


def fixture_repo(tmp_path):
    repo=tmp_path/'target';(repo/'examples/tests').mkdir(parents=True)
    (repo/'examples/service.py').write_text('def value():\n    return 1\n')
    (repo/'examples/tests/test_service.py').write_text('from examples.service import value\ndef test_value():\n    assert value()==2\n')
    (repo/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    for args in [['init','-b','fault'],['add','.'],['-c','user.name=test','-c','user.email=test@localhost','commit','-m','fixture: failing value']]:
        subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    return repo


@pytest.mark.parametrize('replacement,expected',[('return 2','verified_local'),('return 3','tests_failed')])
def test_real_clone_build_and_red_green_test_gate(settings,tmp_path,replacement,expected):
    repo=fixture_repo(tmp_path)
    settings.target_url=str(repo);settings.target_branch='fault';settings.allowed_files=['examples/service.py']
    class Model:
        def patch(self,prompt):
            return {'explanation':'correct return value','changes':[{'path':'examples/service.py','old':'return 1','new':replacement}]}
    result=RepairAgent(Model(),settings,PreToolUse(settings),lambda stage,data:None).run('local-repair',diagnosis())
    assert result['status']==expected and result['pr_url'] is None
    assert not result['baseline']['passed']
    assert result['verification']['passed']==(expected=='verified_local')
    assert (repo/'examples/service.py').read_text().endswith('return 1\n')
