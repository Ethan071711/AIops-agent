from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Alert(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1,max_length=100)
    summary: str = Field(min_length=2,max_length=2000)
    metrics_url: str
    traces_url: str


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    root_service: str = Field(min_length=1,max_length=100)
    cause: str = Field(min_length=2,max_length=1500)
    remediation_type: Literal['code_fix','online_op','info_only']
    confidence: float = Field(ge=0,le=1,allow_inf_nan=False)
    evidence_ids: list[str] = Field(min_length=1,max_length=12)
    action: dict | None = None


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    old: str = Field(min_length=1,max_length=30000)
    new: str = Field(max_length=30000)


class Patch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    explanation: str = Field(min_length=2,max_length=2000)
    changes: list[Change] = Field(min_length=1,max_length=3)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: Path
    source_repo: Path
    target_url: str = 'https://github.com/Ethan071711/AIops-agent.git'
    target_branch: str = 'demo/fault-injected'
    owner: str = 'Ethan071711'
    repository: str = 'AIops-agent'
    allowed_files: list[str] = ['examples/repair_service.py']
    repair_services: list[str] = ['catalog']
    kube_context: str = 'aiops-demo'
    kube_namespace: str = 'aiops-demo'
    restartable_deployments: list[str] = ['inventory','worker']
    kubectl: str = 'kubectl'
    kubeconfig: Path | None = None
    allow_restart: bool = False
    publish_pr: bool = False
    max_tool_rounds: int = Field(default=5,ge=1,le=8)
