"""Encrypted rotating OAuth credentials and durable GitHub state checkpoints."""
import datetime as dt
import json
import http.client
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import base64

from cryptography.fernet import Fernet
from api_diagnostics import error_summary


def checkpoint():
    if os.environ.get('BOT_REMOTE_STATE') != '1':
        return
    def git(*args):
        result = subprocess.run(['git', *args], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Remote state checkpoint failed; publishing halted.')
        return result.stdout
    git('add', '.bot-state/state.sqlite3', '.bot-state/x-vault.enc')
    if git('diff', '--cached', '--name-only').strip():
        git('commit', '-m', 'Persist release bot progress')
    # Push even if already committed locally: a previous push may have failed.
    git('push', 'origin', 'HEAD:main')


def access_token(force=False, *, read_only=False):
    path = Path(os.environ['BOT_DATA_DIR']) / 'x-vault.enc'
    cipher = Fernet(os.environ['X_VAULT_KEY'].encode())
    vault = json.loads(cipher.decrypt(path.read_bytes()))
    expires = dt.datetime.fromisoformat(vault['expires_at'].replace('Z', '+00:00'))
    if force or expires <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        if read_only:
            raise RuntimeError('Diagnostic stopped: token expired or near expiry; refresh is disabled.')
        auth = base64.b64encode((vault['client_id'] + ':' + vault['client_secret']).encode()).decode()
        request = urllib.request.Request('https://api.x.com/2/oauth2/token',
            headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded'},
            data=urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': vault['refresh_token']}).encode())
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError('X token refresh rejected: ' + json.dumps(
                error_summary(exc, request.full_url, request.get_method()), sort_keys=True)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise RuntimeError('X token refresh network failure.') from None
        if not result.get('access_token') or not result.get('expires_in'):
            raise RuntimeError('Incomplete X token refresh response.')
        vault['access_token'] = result['access_token']
        vault['refresh_token'] = result.get('refresh_token', vault['refresh_token'])
        vault['expires_at'] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=result['expires_in'])).isoformat()
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(cipher.encrypt(json.dumps(vault).encode()))
        os.replace(temporary, path)
        checkpoint()
    return vault['access_token']
