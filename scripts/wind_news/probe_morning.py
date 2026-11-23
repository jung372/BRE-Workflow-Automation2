"""Read only sanitized news status from inside the matching container."""
import json
import sys
from urllib.request import Request, urlopen

from news.profiles import profile_for


def main():
    profile = profile_for(sys.argv[1])
    port = 8090 if profile.industry == 'wind' else 8091
    header = {'Authorization': 'Bearer ' + profile.env('API_TOKEN')}

    def get(path):
        request = Request(f'http://127.0.0.1:{port}' + path, headers=header)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    assert get('/health/live')['industry'] == profile.industry
    status = get('/v1/briefings/readiness')
    delivery = get('/v1/deliveries/status')
    for key in ('teams_configured', 'delivery_status', 'delivery_claimed_at'):
        status[key] = delivery[key]
    print(json.dumps(status))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('MORNING_STATUS_UNAVAILABLE')
