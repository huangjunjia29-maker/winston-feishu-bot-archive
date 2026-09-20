"""Read-only Winston availability check; never print credential values."""
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    args = parser.parse_args()
    root = Path(__WINSTON_PROJECT_ROOT_LITERAL__)
    checks = {
        'project_exists': root.is_dir(),
        'python_exists': (root / '.venv/Scripts/python.exe').is_file(),
        'activation_record_exists': (root / 'ACTIVATED.local').is_file(),
        'auth_module_exists': (root / 'workflow/modules/feishu_auth.py').is_file(),
    }
    try:
        sys.path.insert(0, str(root / 'workflow/modules'))
        import feishu_auth
        credentials = feishu_auth.credentials()
        checks['credentials_present'] = all(bool(value) for value in credentials)
        del credentials
        if args.online:
            checks['token_available'] = bool(feishu_auth.tenant_access_token())
            data = feishu_auth.request('GET', '/open-apis/bot/v3/info/')
            checks['bot_read_ok'] = data.get('code') == 0
    except Exception as exc:
        checks['error_type'] = type(exc).__name__
        print(json.dumps(checks, ensure_ascii=True))
        return 1
    print(json.dumps(checks, ensure_ascii=True))
    return 0 if all(value is True for value in checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
