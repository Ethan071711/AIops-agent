"""PreToolUse enforcement runs before commands, file changes and PR writes."""
import ast
import json
import re
import time
import uuid
from pathlib import Path


DIAGNOSTIC_TOOLS={'get_metrics','get_traces','git_log','git_show','kubectl_read','search_past_incidents'}
REPAIR_TOOLS={'clone_repo','read_repo','apply_patch','build_and_test','open_pr'}
BLACKLIST={'shell','exec','delete_database','flush_redis','delete_namespace','force_push','merge_pr'}


class PreToolUse:
    def __init__(self,settings,emit=lambda stage,data:None):
        self.settings,self.emit=settings,emit

    def check(self,stage,name,arguments):
        if name in BLACKLIST:
            self.emit('blocked',{'tool':name,'reason':'blacklist'})
            raise PermissionError('Tool is blacklisted')
        allowed=DIAGNOSTIC_TOOLS if stage=='diagnose' else REPAIR_TOOLS
        if name not in allowed:
            self.emit('blocked',{'tool':name,'reason':'not_in_stage_whitelist'})
            raise PermissionError('Tool is not permitted in this agent stage')
        if name=='kubectl_read':
            if arguments.get('operation') not in {'get','describe','top','logs'}:
                raise PermissionError('Only read-only kubectl operations are allowed')
            resource=arguments.get('resource','pods')
            if not isinstance(resource,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./=-]{0,120}',resource):
                raise PermissionError('Invalid Kubernetes resource')
        self.emit('pre_tool_use',{'stage':stage,'tool':name,'decision':'allow'})

    def review(self,run_id,action,reason):
        directory=self.settings.workspace/'reviews'
        directory.mkdir(parents=True,exist_ok=True)
        request={'id':uuid.uuid4().hex,'run_id':run_id,'action':action,'reason':reason,
                 'status':'pending','created_at':time.time()}
        path=directory/(request['id']+'.json')
        path.write_text(json.dumps(request,ensure_ascii=False,indent=2),encoding='utf-8')
        self.emit('human_review',{'review_id':request['id'],'reason':reason})
        return str(path)

    def online(self,action,service,evidence):
        # A historical or model-supplied stateless claim cannot authorize a restart.
        stateful=[]
        for observation in evidence.values():
            result=observation['result']
            if observation['tool']!='get_metrics' or result.get('exit_code',0)!=0:
                continue
            try:
                payload=json.loads(result.get('stdout',''))
            except json.JSONDecodeError:
                continue
            record=payload.get(service) if isinstance(payload,dict) else None
            if isinstance(record,dict) and 'stateful' in record:
                stateful.append(record['stateful'])
        stateless_now=bool(stateful) and all(flag is False for flag in stateful)
        allowed=(self.settings.allow_restart and stateless_now
                 and set(action)=={'operation','deployment'} and action['operation']=='restart'
                 and action['deployment']==service
                 and action['deployment'] in self.settings.restartable_deployments)
        self.emit('pre_tool_use',{'stage':'online_op','tool':action.get('operation'),
                                 'current_stateless':stateless_now,'decision':'allow' if allowed else 'human_review'})
        return allowed


def validate_python_patch(text):
    tree=ast.parse(text)
    safe_imports={'collections','math','decimal'}
    unsafe_calls={'eval','exec','compile','open','__import__','getattr','setattr','delattr','globals','locals','vars','breakpoint'}
    for node in ast.walk(tree):
        if isinstance(node,ast.Import) and any(n.name.split('.')[0] not in safe_imports for n in node.names):
            raise PermissionError('Patch introduces an unapproved import')
        if isinstance(node,ast.ImportFrom) and (node.module or '').split('.')[0] not in safe_imports:
            raise PermissionError('Patch introduces an unapproved import')
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in unsafe_calls:
            raise PermissionError('Patch introduces an unsafe operation')
        if isinstance(node,ast.Attribute) and node.attr.startswith('__'):
            raise PermissionError('Patch introduces reflective access')
        if isinstance(node,ast.Name) and node.id.startswith('__'):
            raise PermissionError('Patch introduces reflective globals')


def apply_changes(directory,patch,allowed_files):
    directory=Path(directory).resolve()
    edits={}
    for change in patch.changes:
        target=(directory/change.path).resolve()
        if change.path not in allowed_files or not target.is_relative_to(directory) or (directory/change.path).is_symlink():
            raise PermissionError('Patch path is outside the approved repair scope')
        content=edits.get(target,target.read_text(encoding='utf-8'))
        if content.count(change.old)!=1:
            raise ValueError('Patch must match exactly one existing span')
        content=content.replace(change.old,change.new,1)
        validate_python_patch(content)
        edits[target]=content
    for target,content in edits.items():
        target.write_text(content,encoding='utf-8',newline='\n')
