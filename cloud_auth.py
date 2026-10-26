"""Retired OAuth 2.0 vault.

Posting uses OAuth 1.0a user credentials from GitHub Actions secrets
(X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET).
The encrypted refresh-token vault is not read or updated.
"""


def access_token(force=False):
    raise RuntimeError(
        'The encrypted OAuth 2.0 vault is retired. Posting uses OAuth 1.0a secrets '
        'X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, and X_ACCESS_TOKEN_SECRET.'
    )


def checkpoint():
    raise RuntimeError('State checkpoints are written by bot.py to .bot-state/posted.json.')
