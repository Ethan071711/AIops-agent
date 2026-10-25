"""Recomputable label and recovery metrics with explicit denominators."""
import math
import statistics


def classification_metrics(details):
    if not details:
        raise ValueError('No regression runs')
    counts={'service':0,'kind':0,'route':0}
    known=known_correct=0
    for row in details:
        expected=row['expected'];result=row['result'];diagnosis=result.get('diagnosis') or {}
        counts['service']+=diagnosis.get('root_service')==expected['service']
        counts['kind']+=diagnosis.get('remediation_type')==expected['remediation_type']
        counts['route']+=result.get('route')==expected['route']
        if expected['service']!='unknown':
            known+=1;known_correct+=diagnosis.get('root_service')==expected['service']
    return {'scenarios':len({row['case'] for row in details}),'runs':len(details),
        'service_label':{'correct':counts['service'],'total':len(details),'accuracy':counts['service']/len(details)},
        'disposition_label':{'correct':counts['kind'],'total':len(details),'accuracy':counts['kind']/len(details)},
        'final_route':{'correct':counts['route'],'total':len(details),'accuracy':counts['route']/len(details)},
        'known_service_localization':{'correct':known_correct,'total':known,'accuracy':known_correct/known if known else None},
        'definitions':'Exact expected service/type/route labels. Service labels include unknown; no semantic grading of root-cause prose.'}


def verified_recovery_seconds(events):
    alerts=[event for event in events if event['stage']=='alert']
    if len(alerts)!=1:
        raise ValueError('Expected one alert start in a run trace')
    for event in events:
        if event['stage']=='recovery_verified':
            if event['data'].get('rollout_ready') is not True or event['data'].get('health_status')!=200:
                raise ValueError('A success event must include readiness and HTTP 200')
            elapsed=event['seconds']-alerts[0]['seconds']
            if not math.isfinite(elapsed) or elapsed<0:
                raise ValueError('Invalid recovery interval')
            return round(elapsed,3)
    return None


def recovery_metrics(trials):
    successful=[];timeouts=[];expected_recoverable=0;unexpected=[]
    for trial in trials:
        expected_recoverable+=trial['expect_recovered']
        elapsed=trial['alert_to_health_verified_seconds']
        if trial['result']['route']=='auto_remediated':
            if elapsed is None or not math.isfinite(elapsed) or elapsed<0:
                raise ValueError('Recovered trial needs a valid observed interval')
            if not (trial['pod_replaced'] and trial['worker_progress_restored'] and trial['health_status']==200):
                raise ValueError('Recovered trial lacks independent workload confirmation')
            successful.append(elapsed)
        else:
            if elapsed is not None:
                raise ValueError('Unrecovered trial cannot have a successful recovery time')
            timeouts.append({'trial':trial['id'],'observation_seconds':trial['observed_seconds'],
                             'route':trial['result']['route'],'health_status':trial['health_status']})
        actual=trial['result']['route']=='auto_remediated'
        if actual!=trial['expect_recovered']:
            unexpected.append(trial['id'])
    return {'trials':len(trials),'expected_recoverable_trials':expected_recoverable,
        'verified_recoveries':len(successful),'unrecovered_observations':timeouts,
        'unexpected_outcome_trials':unexpected,'successful_intervals_seconds':successful,
        'successful_mean_seconds':statistics.mean(successful) if successful else None,
        'successful_median_seconds':statistics.median(successful) if successful else None,
        'successful_min_seconds':min(successful) if successful else None,
        'successful_max_seconds':max(successful) if successful else None,
        'definitions':'Alert trace start to readiness + HTTP 200 success event; worker progress and Pod replacement confirmed independently. Unrecovered observations are reported separately, never as zero or successful recovery times.'}
