import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aiops.agents import DiagnosisAgent,RepairAgent,current_services
from aiops.memory import IncidentMemory,memory_server
from aiops.model import json_value
from aiops.policy import PreToolUse,apply_changes,validate_python_patch
from aiops.schema import Alert,Diagnosis,Patch,Settings
from aiops.github import open_pull_request
from aiops.workflow import Workflow
from examples.observations import Observations

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
    memory.client.release_collection(memory.collection)
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
    evidence={'e1':{'tool':'get_metrics','result':{'stdout':'{"inventory":{"stateful":false}}'}}}
    assert not hook.online({'operation':'restart','deployment':'inventory'},'inventory',evidence)
    settings.allow_restart=True
    assert hook.online({'operation':'restart','deployment':'inventory'},'inventory',evidence)
    assert not hook.online({'operation':'restart','deployment':'inventory'},'orders',evidence)
    assert not hook.online({'operation':'restart','deployment':'payments'},'payments',evidence)
    assert not hook.online({'operation':'flush_redis','deployment':'inventory'},'inventory',evidence)
    path=hook.review('run-test',{'operation':'drop_database'},'high risk')
    assert json.loads(Path(path).read_text())['status']=='pending'


def test_history_or_ambiguous_state_cannot_authorize_a_current_restart(settings):
    settings.allow_restart=True
    hook=PreToolUse(settings)
    action={'operation':'restart','deployment':'worker'}
    history={'h':{'tool':'search_past_incidents','result':{'stdout':'{"worker":{"stateful":false}}'}}}
    assert not hook.online(action,'worker',history)
    for record in [{},{'stateful':True},{'stateful':'false'},{'stateful':0}]:
        evidence={**history,'e1':{'tool':'get_metrics','result':{'stdout':json.dumps({'worker':record})}}}
        assert not hook.online(action,'worker',evidence)


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


def test_diagnosis_must_cite_observed_metrics_and_traces(settings):
    class Model:
        def chat(self,*args):
            return {'content':diagnosis().model_dump_json(),'tool_calls':[]}
    class Tools:
        evidence={'e1':{'tool':'get_metrics','result':{'exit_code':0}}}
    tools=Tools();tools.settings=settings
    alert=Alert(id='test',summary='cache issue',metrics_url='http://localhost',traces_url='http://localhost')
    with pytest.raises(ValueError):
        asyncio.run(DiagnosisAgent(Model()).run(alert,tools))


def test_history_service_names_cannot_replace_current_observations(settings):
    evidence={'e1':{'tool':'get_metrics','result':{'stdout':'{"orders":{"blocked_queries":12}}'}},
              'e2':{'tool':'get_traces','result':{'stdout':'{"spans":[{"service":"orders"}]}'}},
              'e3':{'tool':'search_past_incidents','result':{'stdout':'{"service":"inventory"}'}}}
    assert current_services(evidence)=={'orders'}
    with pytest.raises(PermissionError):
        RepairAgent(None,settings,PreToolUse(settings),lambda *args:None).run('wrong-repo',diagnosis().model_copy(update={'root_service':'payments'}))


def test_json_parser_accepts_fenced_output_but_not_free_text():
    assert json_value('```json\n{"a":1}\n```')=={'a':1}
    with pytest.raises(ValueError):
        json_value('no JSON')


def test_github_owner_check_and_client_lifecycle(settings,monkeypatch):
    import httpx
    import aiops.github as github
    real=httpx.Client
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200,json={'login':settings.owner} if request.url.path=='/user' else {'html_url':'https://github.com/example/test/pull/1'})
    monkeypatch.setenv('GITHUB_TOKEN','TEST-ONLY-credential')
    monkeypatch.setattr(github.httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(handler)))
    assert open_pull_request(settings,'repair/test','test body').endswith('/pull/1')
    assert requests==['/user',f'/repos/{settings.owner}/{settings.repository}/pulls']


def test_completed_diagnosis_is_saved_even_when_repair_fails(settings,tmp_path,monkeypatch):
    rows=[{'id':'case','metrics':{'catalog':{'cache_entries':1000}},'traces':{'spans':[{'service':'catalog','cache_growth':True}]}}]
    fixture=Observations(rows).start()
    memory=IncidentMemory(tmp_path/'failed-repair.db',TinyEmbedder(),4)
    class Model:
        calls=0
        def chat(self,*args):
            self.calls+=1
            if self.calls==1:
                return {'content':'','tool_calls':[{'name':'get_metrics','arguments':{}},{'name':'get_traces','arguments':{}}]}
            if self.calls>2:
                return {'content':json.dumps({'remediation_type':'code_fix','action':None,'reason':'Per-request retention defect'}),'tool_calls':[]}
            return {'content':diagnosis().model_dump_json(),'tool_calls':[]}
    def fail(*args):
        raise ValueError('injected repair failure')
    monkeypatch.setattr(RepairAgent,'run',fail)
    alert=Alert(id='repair-fails',summary='cache growing',metrics_url=f'http://127.0.0.1:{fixture.port}/metrics/case',
                traces_url=f'http://127.0.0.1:{fixture.port}/traces/case')
    try:
        result=asyncio.run(Workflow(settings,Model(),memory).run(alert))
        assert result['route']=='needs_review' and result['errors'][0]['kind']=='ValueError'
        assert memory.search('cache growing')[0]['id']==result['run_id']
    finally:
        memory.close();fixture.close()


def test_missing_current_evidence_produces_a_persistable_unknown_diagnosis(settings):
    class Model:
        def chat(self,*args):
            return {'content':'Observations unavailable.','tool_calls':[]}
    class Tools:
        evidence={'e1':{'tool':'get_metrics','result':{'stdout':'{"error":"unavailable"}'}},
                  'e2':{'tool':'get_traces','result':{'stdout':'{"spans":[]}'}}}
    tools=Tools();tools.settings=settings
    alert=Alert(id='test',summary='No current observations',metrics_url='http://localhost',traces_url='http://localhost')
    result=asyncio.run(DiagnosisAgent(Model()).run(alert,tools))
    assert result.root_service=='unknown' and result.confidence==0
    assert result.remediation_type=='info_only' and result.evidence_ids==['e1','e2']


def test_history_cannot_supply_the_current_remediation_classification(settings):
    class Model:
        def chat(self,messages):
            context=json.dumps(messages)
            assert 'CURRENT_LOCK' in context and 'HISTORY_ONLY_POISON' not in context
            assert 'source change' in context
            return {'content':json.dumps({'remediation_type':'online_op',
                'action':{'operation':'review_session_locks'},'reason':'Current local sessions are blocked'})}
    class Tools:
        evidence={'e1':{'tool':'get_metrics','result':{'stdout':'{"orders":{"blocked_sessions":3}}'}},
                  'e2':{'tool':'get_traces','result':{'stdout':'{"service":"orders","error":"CURRENT_LOCK"}'}},
                  'e3':{'tool':'search_past_incidents','result':{'stdout':'HISTORY_ONLY_POISON'}}}
        emit=staticmethod(lambda *args:None)
    tools=Tools();tools.settings=settings
    initial=diagnosis().model_copy(update={'root_service':'orders'})
    result=DiagnosisAgent(Model()).classify(initial,tools)
    assert result.root_service=='orders' and result.remediation_type=='online_op'
    assert result.evidence_ids==initial.evidence_ids


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


def test_repair_retries_a_rejected_span_without_relaxing_the_guard(settings,tmp_path):
    repo=fixture_repo(tmp_path)
    settings.target_url=str(repo);settings.target_branch='fault';settings.allowed_files=['examples/service.py']
    class Model:
        calls=0
        def patch(self,prompt):
            self.calls+=1
            return {'explanation':'correct return value','changes':[{'path':'examples/service.py',
                    'old':'return 99' if self.calls==1 else 'return 1','new':'return 2'}]}
    model=Model();events=[]
    result=RepairAgent(model,settings,PreToolUse(settings),lambda s,d:events.append(s)).run('retry-repair',diagnosis())
    assert model.calls==2 and result['status']=='verified_local' and 'patch_rejected' in events
