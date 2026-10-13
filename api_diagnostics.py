"""Bounded, allowlisted HTTP error summaries; never emit server free text."""
import json
import http.client
import re
import urllib.parse

MAX_ERROR_BYTES = 8192
PARAMETERS = frozenset({'max_results', 'start_time', 'end_time', 'since_id',
                        'until_id', 'tweet.fields', 'pagination_token', 'expansions'})
TITLES = frozenset({'Invalid Request', 'Unauthorized', 'Forbidden', 'Not Found',
                    'Too Many Requests', 'Internal Server Error', 'Service Unavailable'})
OAUTH_ERRORS = frozenset({'invalid_request', 'invalid_client', 'invalid_grant',
                          'unauthorized_client', 'unsupported_grant_type', 'invalid_scope'})


def safe_endpoint(url):
    path = urllib.parse.urlsplit(url).path
    if path in {'/2/users/me', '/2/tweets', '/2/oauth2/token'}:
        return path
    if re.fullmatch(r'/2/users/[^/]+/tweets', path):
        return '/2/users/:id/tweets'
    return '/[redacted]'


def error_summary(exc, url, method):
    # All emitted strings are constants or bounded numeric metadata. Query
    # values, IDs, headers, request bodies and arbitrary server text are omitted.
    result = {'method': method if method in {'GET', 'POST'} else 'OTHER',
              'endpoint': safe_endpoint(url), 'status': int(exc.code)}
    try:
        raw = exc.read(MAX_ERROR_BYTES + 1)
        if len(raw) > MAX_ERROR_BYTES:
            result['body'] = 'oversized-omitted'
            return result
        body = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
        result['body'] = 'unavailable-or-non-json'
        return result
    if not isinstance(body, dict):
        result['body'] = 'non-object-omitted'
        return result
    title = body.get('title')
    if isinstance(title, str) and title in TITLES:
        result['title'] = title
    oauth = body.get('error')
    if isinstance(oauth, str) and oauth in OAUTH_ERRORS:
        result['oauth_error'] = oauth
    errors = body.get('errors')
    if isinstance(errors, list):
        result['error_count'] = min(len(errors), 999)
        parameters, codes = set(), set()
        for error in errors[:20]:
            if not isinstance(error, dict):
                continue
            code = error.get('code')
            if type(code) is int and 0 <= code <= 9999:
                codes.add(code)
            parameter = error.get('parameter')
            if isinstance(parameter, str) and parameter in PARAMETERS:
                parameters.add(parameter)
            params = error.get('parameters')
            if isinstance(params, dict):
                parameters.update(PARAMETERS.intersection(params))
        if parameters:
            result['parameters'] = sorted(parameters)
        if codes:
            result['codes'] = sorted(codes)
    return result
