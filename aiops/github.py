"""Own-repository PR creation. Credentials stay in memory and out of remotes/logs."""
import os
import subprocess

import httpx


def client_for(owner):
    token=os.getenv('GITHUB_TOKEN')
    if not token:
        env={**os.environ,'GCM_INTERACTIVE':'never','GIT_TERMINAL_PROMPT':'0'}
        result=subprocess.run(['git','credential','fill'],input=f'protocol=https\nhost=github.com\nusername={owner}\n\n',
                              env=env,text=True,capture_output=True,timeout=20,check=True)
        values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        token=values.get('password')
    if not token:
        raise PermissionError('No GitHub credential available')
    client=httpx.Client(base_url='https://api.github.com',timeout=30,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
    profile=client.get('/user')
    profile.raise_for_status()
    if profile.json()['login'].lower()!=owner.lower():
        client.close()
        raise PermissionError('PR publishing is restricted to the authenticated owner repository')
    return client


def open_pull_request(settings,branch,body):
    with client_for(settings.owner) as client:
        response=client.post(f'/repos/{settings.owner}/{settings.repository}/pulls',json={
            'title':'fix: bound the sample service request history', 'head':branch,
            'base':settings.target_branch,'body':body})
        response.raise_for_status()
        return response.json()['html_url']
