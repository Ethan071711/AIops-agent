import pytest

from aiops.evaluation import classification_metrics,recovery_metrics,verified_recovery_seconds


def test_recompute_labels_including_failed_diagnosis_without_dropping_denominator():
    expected={'service':'catalog','remediation_type':'code_fix','route':'code_fix_verified'}
    rows=[{'case':'one','expected':expected,'result':{'diagnosis':{'root_service':'catalog','remediation_type':'code_fix'},'route':'code_fix_verified'}},
          {'case':'two','expected':expected,'result':{'diagnosis':None,'route':'needs_review'}}]
    summary=classification_metrics(rows)
    assert summary['runs']==2 and summary['scenarios']==2
    assert summary['service_label']['accuracy']==.5
    assert summary['final_route']['total']==2


def test_recovery_uses_health_confirmation_not_restart_acceptance_or_workflow_end():
    events=[{'stage':'alert','seconds':0},
        {'stage':'low_risk_action','seconds':2,'data':{'exit_code':0}},
        {'stage':'recovery_verified','seconds':8,'data':{'rollout_ready':True,'health_status':200}},
        {'stage':'memory_saved','seconds':11}]
    assert verified_recovery_seconds(events)==8
    assert verified_recovery_seconds(events[:2]) is None
    events[2]['data']['health_status']=503
    with pytest.raises(ValueError):verified_recovery_seconds(events)


def test_timeout_is_visible_and_never_averaged_as_zero_or_success():
    def trial(id,expected,seconds,route,health,progress):
        return {'id':id,'expect_recovered':expected,'alert_to_health_verified_seconds':seconds,
            'observed_seconds':32,'pod_replaced':True,'worker_progress_restored':progress,
            'health_status':health,'result':{'route':route}}
    rows=[trial('one',True,8,'auto_remediated',200,True),
          trial('two',True,12,'auto_remediated',200,True),
          trial('persistent',False,None,'needs_review',503,False)]
    summary=recovery_metrics(rows)
    assert summary['successful_mean_seconds']==10 and summary['verified_recoveries']==2
    assert summary['unrecovered_observations'][0]['trial']=='persistent'
    assert summary['unexpected_outcome_trials']==[]
    rows[0]['worker_progress_restored']=False
    with pytest.raises(ValueError):recovery_metrics(rows)
