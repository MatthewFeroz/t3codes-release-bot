"""One-time Windows -> private GitHub Actions vault migration; never logs secrets."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parent
source = Path(os.environ['LOCALAPPDATA']) / 'T3CodesReleaseBot'
target = root / '.bot-state'
target.mkdir(exist_ok=True)
vault_path = target / 'x-vault.enc'
if vault_path.exists():
    raise SystemExit('Cloud vault already exists; refusing to replace rotating credentials.')
command = "$p=Join-Path $env:LOCALAPPDATA 'T3CodesReleaseBot\\credentials.dpapi'; [Net.NetworkCredential]::new('',(ConvertTo-SecureString (Get-Content -LiteralPath $p -Raw))).Password"
result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, text=True, check=True)
vault = json.loads(result.stdout)
key = Fernet.generate_key()
result = subprocess.run(['gh', 'secret', 'set', 'X_VAULT_KEY', '--repo', 'MatthewFeroz/t3codes-release-bot'],
                        input=key, capture_output=True)
if result.returncode:
    raise SystemExit('Could not save the GitHub encryption secret.')
vault_path.write_bytes(Fernet(key).encrypt(json.dumps(vault).encode()))
with sqlite3.connect(source / 'state.sqlite3') as original, sqlite3.connect(target / 'state.sqlite3') as copy:
    original.backup(copy)
print('Encrypted credentials and deduplication state migrated; key saved as GitHub Actions secret.')
