"""Solar-only operator API client. Preview and diagnose never configure or send."""
import argparse
from datetime import date
import json
import os
import sys
from urllib.parse import urlsplit
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['preview', 'diagnose', 'send', 'test'], default='preview')
    parser.add_argument('--date')
    args = parser.parse_args()
    try:
        if args.date and date.fromisoformat(args.date).isoformat() != args.date:
            raise ValueError('invalid_date')
        base = os.environ.get('SOLAR_NEWS_SERVICE_URL', 'http://127.0.0.1:8091').rstrip('/')
        parsed = urlsplit(base)
        # The token may travel over HTTP only on local loopback, never a public host.
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path
                or parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('invalid_service_url')
        token = os.environ.get('SOLAR_NEWS_API_TOKEN', '')
        if len(token) < 32:
            raise ValueError('missing_api_token')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *unused):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        def request(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, headers={
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with opener.open(req, timeout=45) as response:
                return json.loads(response.read(1000000))
        health = request('/health/ready')
        if health.get('industry') != 'solar':
            raise ValueError('wrong_industry_service')
        suffix = '?issue_date=' + args.date if args.date else ''
        status = request('/v1/deliveries/status' + suffix)
        if args.mode == 'diagnose':
            result = status
        else:
            body = {'issue_date': args.date or status['issue_date']}
            if args.mode == 'test':
                body['message_type'] = 'connection_test'
            result = request('/v1/deliveries/' + ('preview' if args.mode == 'preview' else 'send'), body)
            if args.mode == 'preview':
                result = {'status': 'PREVIEWED', 'teams_configured': status['teams_configured'],
                          'card_format': status['card_format'], 'issue_date': status['issue_date']}
        # Only fixed operational fields, never arbitrary upstream errors/card bodies.
        allowed = {'status', 'teams_configured', 'card_format', 'issue_date', 'revision',
                   'publication_state', 'delivery_status', 'briefing_stopped', 'error_code', 'claimed'}
        safe = {key: value for key, value in result.items() if key in allowed}
        print(json.dumps(safe, ensure_ascii=True))
        return 1 if result.get('status') in {'UNKNOWN', 'FAILED', 'SKIPPED'} else 0
    except Exception:
        print('SOLAR_OPERATOR_REQUEST_FAILED', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
