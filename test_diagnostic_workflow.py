"""Static fail-closed guard for the separate manual-only diagnostic workflow."""
import json
from pathlib import Path
import unittest


class DiagnosticWorkflowTests(unittest.TestCase):
    def test_workflow_has_only_manual_read_only_diagnostic_job(self):
        workflow = json.loads((Path(__file__).parent / '.github/workflows/diagnose-reconciliation.yml').read_text())
        self.assertEqual(set(workflow), {'name', 'on', 'permissions', 'concurrency', 'jobs'})
        self.assertEqual(workflow['on'], {'workflow_dispatch': {}})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['concurrency'], {'group': 't3codes-publisher', 'cancel-in-progress': False})
        self.assertEqual(set(workflow['jobs']), {'diagnose'})
        job = workflow['jobs']['diagnose']
        self.assertEqual(set(job), {'runs-on', 'timeout-minutes', 'steps'})
        self.assertEqual(job['runs-on'], 'ubuntu-latest')
        self.assertEqual(job['timeout-minutes'], 10)
        self.assertEqual(job['steps'], [
            {'uses': 'actions/checkout@v4', 'with': {'persist-credentials': False}},
            {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}},
            {'name': 'Install existing dependencies', 'run': 'python -m pip install -r requirements.txt'},
            {'name': 'Run mocked tests', 'run': 'python -m unittest -v'},
            {'name': 'Inspect uncertain delivery without publishing',
             'env': {'X_VAULT_KEY': '${{ secrets.X_VAULT_KEY }}',
                     'BOT_DATA_DIR': '${{ github.workspace }}/.bot-state', 'BOT_REMOTE_STATE': '0'},
             'run': '''snapshot="$(mktemp)"
trap 'rm -f "$snapshot"' EXIT
git show HEAD:.bot-state/state.sqlite3 > "$snapshot"
python bot.py diagnose-reconciliation --state "$snapshot"'''},
        ])


if __name__ == '__main__':
    unittest.main()
