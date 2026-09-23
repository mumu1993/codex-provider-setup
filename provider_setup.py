#!/usr/bin/env python3
"""Configure a local CLIProxyAPI gateway without replacing personal Codex auth."""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

VERSION = '1.0.0'
PROXY_VERSION = '7.3.15'
LABEL = 'dev.codex-provider-setup'
PROVIDER = 'provider_kit'
CARD_IDS = {'personal': 'provider-kit-personal', 'gateway': 'provider-kit-gateway'}
EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}
MODEL_KEYS = {'model', 'model_provider', 'model_reasoning_effort', 'review_model',
              'model_context_window', 'model_auto_compact_token_limit',
              'model_auto_compact_token_limit_scope', 'model_max_output_tokens',
              'model_catalog_json', 'service_tier', 'openai_base_url'}


def private_write(path: Path, content: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.provider-kit-', delete=False) as f:
        temp = Path(f.name)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_manifest(data):
    m = copy.deepcopy(data)
    if not isinstance(m, dict) or not isinstance(m.get('models'), list) or not m['models']:
        raise ValueError('Manifest must contain a non-empty models array.')
    m.setdefault('name', 'Team Gateway')
    m.setdefault('port', 8317)
    if not isinstance(m['name'], str) or not 1 <= len(m['name']) <= 80:
        raise ValueError('name must be 1–80 characters.')
    if type(m['port']) is not int or not 1024 <= m['port'] <= 65535:
        raise ValueError('port must be an integer between 1024 and 65535.')
    seen = set()
    for model in m['models']:
        if not isinstance(model, dict):
            raise ValueError('Every model must be an object.')
        ident = model.get('id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}', ident):
            raise ValueError('Model IDs must be exact, non-empty identifiers (maximum 128 characters).')
        if ident in seen:
            raise ValueError(f'Duplicate model ID: {ident}')
        seen.add(ident)
        if model.get('protocol') not in ('responses', 'chat'):
            raise ValueError(f'{ident}: protocol must be responses or chat.')
        base = model.get('base_url', '')
        if not isinstance(base, str) or re.search(r'\{(?!model\})', base):
            raise ValueError(f'{ident}: only the {{model}} URL placeholder is supported.')
        parsed = urlparse(base.replace('{model}', 'example-model'))
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or (parsed.scheme == 'http' and not local):
            raise ValueError(f'{ident}: use HTTPS, or HTTP on loopback for local tests.')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f'{ident}: base_url cannot contain credentials, queries or fragments.')
        model['base_url'] = base.rstrip('/')
        levels = model.setdefault('reasoning_efforts', [])
        if not isinstance(levels, list) or any(not isinstance(x, str) or x not in EFFORTS for x in levels) or len(set(levels)) != len(levels):
            raise ValueError(f'{ident}: unsupported or duplicate reasoning effort.')
        if 'context_window' in model and (type(model['context_window']) is not int or not 4096 <= model['context_window'] <= 10000000):
            raise ValueError(f'{ident}: invalid context_window.')
        header = model.get('api_key_header')
        if header is not None and (not isinstance(header, str) or not re.fullmatch(r'[A-Za-z0-9-]+', header)):
            raise ValueError(f'{ident}: invalid API key header.')
        headers = model.get('headers', {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9-]+', k) or not isinstance(v, str) or '\n' in v or '\r' in v for k, v in headers.items()):
            raise ValueError(f'{ident}: invalid headers.')
    m.setdefault('default_model', m['models'][0]['id'])
    if m['default_model'] not in seen:
        raise ValueError('default_model must be listed in models.')
    return m


def proxy_config(manifest, upstream_key, local_key):
    result = {'host': '127.0.0.1', 'port': manifest['port'], 'api-keys': [local_key],
              'debug': False, 'logging-to-file': False, 'request-retry': 1,
              'disable-image-generation': 'passthrough', 'codex-api-key': [],
              'openai-compatibility': []}
    for index, model in enumerate(manifest['models']):
        base = model['base_url'].replace('{model}', quote(model['id'], safe=''))
        headers = dict(model.get('headers', {}))
        if model.get('api_key_header'):
            headers[model['api_key_header']] = upstream_key
        item = {'name': model['id'], 'alias': model['id'],
                'display-name': model.get('display_name', model['id']), 'is-compat': True,
                'thinking': {'levels': model['reasoning_efforts']}}
        if model.get('context_window'):
            item['max-context-length'] = model['context_window']
        if model['protocol'] == 'responses':
            result['codex-api-key'].append({'api-key': upstream_key, 'base-url': base, 'headers': headers, 'models': [item]})
        else:
            result['openai-compatibility'].append({'name': f'provider-kit-chat-{index}', 'base-url': base,
                'api-key-entries': [{'api-key': upstream_key}], 'headers': headers, 'models': [item]})
    return result


def render_codex_config(original, mode, manifest, local_key):
    parsed = tomllib.loads(original)
    lines = original.splitlines(keepends=True)
    root_end = next((i for i, l in enumerate(lines) if re.match(r'^\s*\[', l)), len(lines))
    root = []
    for line in lines[:root_end]:
        match = re.match(r'''^\s*["']?([A-Za-z_][A-Za-z_0-9]*)["']?\s*=''', line)
        if not match or match.group(1) not in MODEL_KEYS:
            root.append(line)
    tables = []
    in_owned = False
    for line in lines[root_end:]:
        if re.match(r'^\s*\[', line):
            in_owned = bool(re.match(r'^\s*\[model_providers\.(?:provider_kit|"provider_kit"|\'provider_kit\')(?:\]|\.)', line))
        if not in_owned:
            tables.append(line)
    personal_model = manifest.get('personal_model')
    if personal_model is None and parsed.get('model_provider', 'openai') == 'openai':
        personal_model = parsed.get('model')
    settings = {'model': manifest['default_model'] if mode == 'gateway' else personal_model,
                'model_provider': PROVIDER if mode == 'gateway' else 'openai',
                'service_tier': 'default' if mode == 'gateway' else parsed.get('service_tier', 'default')}
    if settings['model'] is None:
        del settings['model']
    levels = next(m['reasoning_efforts'] for m in manifest['models'] if m['id'] == manifest['default_model'])
    effort = manifest.get('personal_reasoning_effort', parsed.get('model_reasoning_effort', 'medium')) if mode == 'personal' else parsed.get('model_reasoning_effort', 'medium')
    if mode == 'gateway':
        if levels:
            settings['model_reasoning_effort'] = effort if effort in levels else ('medium' if 'medium' in levels else levels[0])
    elif effort in EFFORTS or effort == 'ultra':
        settings['model_reasoning_effort'] = effort
    owned = {'name': manifest['name'], 'base_url': f"http://127.0.0.1:{manifest['port']}/v1",
             'wire_api': 'responses', 'requires_openai_auth': False,
             'supports_websockets': False, 'experimental_bearer_token': local_key,
             'stream_idle_timeout_ms': 600000}
    text = ''.join(f'{k} = {json.dumps(v, ensure_ascii=False)}\n' for k, v in settings.items())
    text += ''.join(root) + '\n' + ''.join(tables).rstrip() + f'\n\n[model_providers.{PROVIDER}]\n'
    text += ''.join(f'{k} = {json.dumps(v, ensure_ascii=False)}\n' for k, v in owned.items())
    expected = {k: v for k, v in parsed.items() if k not in MODEL_KEYS}
    expected = copy.deepcopy(expected)
    expected.setdefault('model_providers', {})[PROVIDER] = owned
    expected.update(settings)
    if tomllib.loads(text) != expected:
        raise ValueError('Cannot safely preserve this TOML layout. No files were changed.')
    return text


def personal_preferences(manifest, baseline):
    result = copy.deepcopy(manifest)
    saved = tomllib.loads(baseline)
    if saved.get('model_provider', 'openai') == 'openai':
        if 'personal_model' not in result and saved.get('model'):
            result['personal_model'] = saved['model']
        if saved.get('model_reasoning_effort'):
            result['personal_reasoning_effort'] = saved['model_reasoning_effort']
    return result


def cc_schema(database, codex_text=None):
    if not database.exists():
        raise ValueError('Open CC Switch once, then quit it before using --with-cc-switch.')
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        cols = {r[1] for r in db.execute('PRAGMA table_info(providers)')}
        required = {'id', 'app_type', 'name', 'settings_config', 'meta', 'is_current', 'category'}
        if not required <= cols:
            raise ValueError('Unsupported CC Switch database schema; no database changes made.')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'proxy_config' not in tables:
            raise ValueError('Unknown CC Switch routing schema; ownership cannot be verified.')
        pc = {r[1] for r in db.execute('PRAGMA table_info(proxy_config)')}
        if not {'app_type', 'enabled', 'live_takeover_active'} <= pc:
            raise ValueError('Unknown CC Switch routing schema; ownership cannot be verified.')
        row = db.execute("SELECT enabled,live_takeover_active FROM proxy_config WHERE app_type='codex'").fetchone()
        if row and any(row):
            raise ValueError('Turn off Codex local routing in CC Switch, then quit CC Switch and retry.')
        if codex_text and 'listen_port' in pc:
            config = tomllib.loads(codex_text)
            provider = config.get('model_provider', 'openai')
            url = urlparse(config.get('model_providers', {}).get(provider, {}).get('base_url', ''))
            port = db.execute("SELECT listen_port FROM proxy_config WHERE app_type='codex'").fetchone()
            if port and url.hostname in ('127.0.0.1', 'localhost', '::1') and url.port == port[0]:
                raise ValueError('Codex still points to CC Switch local routing. Disable and restore that route first.')
        if 'proxy_live_backup' in tables and db.execute("SELECT 1 FROM proxy_live_backup WHERE app_type='codex'").fetchone():
            raise ValueError('CC Switch still owns a Codex route backup. Disable its Codex local routing first.')
        return cols
    finally:
        db.close()


def integrate_cc_switch(database, settings_file, personal, gateway, mode):
    cols = cc_schema(database)
    prefs = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    rows = [('personal', 'Personal ChatGPT', personal, 'official'), ('gateway', 'Team Gateway · CLIProxyAPI', gateway, 'third_party')]
    with closing(sqlite3.connect(database)) as db:
        with db:
            for name, label, text, category in rows:
                values = {'id': CARD_IDS[name], 'app_type': 'codex', 'name': label,
                          'settings_config': json.dumps({'auth': {}, 'config': text}),
                          'meta': json.dumps({'commonConfigEnabled': False, 'endpointAutoSelect': False}),
                          'category': category, 'is_current': int(name == mode)}
                if 'sort_index' in cols:
                    values['sort_index'] = 0 if name == 'personal' else 1
                names = list(values)
                assignment = ','.join(f'{k}=excluded.{k}' for k in names if k not in ('id', 'app_type'))
                db.execute(f"INSERT INTO providers ({','.join(names)}) VALUES ({','.join('?' for _ in names)}) ON CONFLICT(id,app_type) DO UPDATE SET {assignment}", list(values.values()))
            db.execute("UPDATE providers SET is_current=(id=?) WHERE app_type='codex'", (CARD_IDS[mode],))
    prefs['currentProviderCodex'] = CARD_IDS[mode]
    prefs['preserveCodexOfficialAuthOnSwitch'] = True
    private_write(settings_file, json.dumps(prefs, ensure_ascii=False, indent=2))


def response_succeeded(body):
    try:
        data = json.loads(body)
        return data.get('status') == 'completed' and not data.get('error')
    except (ValueError, UnicodeDecodeError):
        pass
    completed = False
    for line in body.decode(errors='replace').splitlines():
        if not line.startswith('data: ') or line[6:] == '[DONE]':
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            continue
        if data.get('type') in ('error', 'response.failed', 'response.incomplete'):
            return False
        if data.get('type') == 'response.completed':
            completed = data.get('response', {}).get('status') == 'completed'
    return completed


def request(url, token=None, payload=None, timeout=30):
    headers = {'User-Agent': f'codex-provider-setup/{VERSION}'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = Request(url, headers=headers, data=None if payload is None else json.dumps(payload).encode())
    try:
        ca_file = '/etc/ssl/cert.pem' if platform.system() == 'Darwin' and not os.environ.get('SSL_CERT_FILE') and Path('/etc/ssl/cert.pem').exists() else None
        with urlopen(req, timeout=timeout, context=ssl.create_default_context(cafile=ca_file)) as result:
            return result.read()
    except HTTPError as exc:
        # Do not echo arbitrary upstream responses, URLs or headers containing secrets.
        raise RuntimeError(f'HTTP {exc.code}; check access, model ID and endpoint configuration.') from None
    except URLError:
        raise RuntimeError('Connection failed; check the proxy service, network and endpoint.') from None


def download_proxy(destination, version=PROXY_VERSION):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid proxy version.')
    os_name = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'amd64', 'AMD64': 'amd64'}.get(platform.machine())
    if not os_name or not arch:
        raise ValueError('Automatic proxy download supports macOS/Linux arm64 and x86_64.')
    filename = f'CLIProxyAPI_{version}_{os_name}_{arch}.tar.gz'
    base = f'https://github.com/router-for-me/CLIProxyAPI/releases/download/v{version}'
    checksums = request(base + '/checksums.txt').decode()
    match = next((line.split()[0] for line in checksums.splitlines() if len(line.split()) == 2 and line.split()[1].lstrip('*') == filename), None)
    if not match or not re.fullmatch(r'[a-fA-F0-9]{64}', match):
        raise RuntimeError('Release checksum entry not found. Download aborted.')
    archive = request(base + '/' + filename, timeout=180)
    if hashlib.sha256(archive).hexdigest().lower() != match.lower():
        raise RuntimeError('Release checksum mismatch. Download aborted.')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
        entries = [m for m in package.getmembers() if m.isfile() and Path(m.name).name == 'cli-proxy-api']
        if len(entries) != 1:
            raise RuntimeError('Unexpected release archive layout.')
        source = package.extractfile(entries[0])
        if source is None:
            raise RuntimeError('Missing proxy executable.')
        private_write(destination, source.read())
    destination.chmod(0o700)


def require_cc_closed():
    if platform.system() != 'Darwin':
        raise ValueError('Automatic CC Switch integration is supported on macOS only.')
    result = subprocess.run(['pgrep', '-x', 'cc-switch'], capture_output=True)
    if result.returncode != 1:
        raise ValueError('Quit CC Switch before changing its configuration.')


def sqlite_copy(source, destination):
    deadline = time.monotonic() + 15
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RuntimeError('Database is busy; close other database clients and retry.')
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256, progress=progress)
        dst.commit()
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def create_snapshot(state_dir, paths):
    for path in paths:
        if path.is_symlink():
            raise ValueError(f'Symlinked managed files are not supported: {path.name}. No configuration changed.')
    folder = state_dir / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3))
    folder.mkdir(parents=True, mode=0o700)
    items = []
    for index, path in enumerate(paths):
        item = {'path': str(path.resolve()), 'file': str(index), 'exists': path.exists()}
        if path.exists():
            item['mode'] = path.stat().st_mode & 0o777
            if path.suffix == '.db':
                sqlite_copy(path, folder / str(index))
                (folder / str(index)).chmod(0o600)
                item['sqlite'] = True
            else:
                private_write(folder / str(index), path.read_bytes())
        items.append(item)
    private_write(folder / 'manifest.json', json.dumps(items, indent=2))
    return folder


def restore_snapshot(folder):
    items = json.loads((folder / 'manifest.json').read_text())
    for item in items:
        target = Path(item['path'])
        if target.name == 'auth.json' or target.is_symlink() or not target.is_absolute() or not str(item['file']).isdigit():
            raise ValueError('Unsafe snapshot target; restore stopped before changes.')
    for item in items:
        target = Path(item['path'])
        if item['exists']:
            if item.get('sqlite'):
                sqlite_copy(folder / item['file'], target)
            else:
                private_write(target, (folder / item['file']).read_bytes())
            target.chmod(item.get('mode', 0o600))
        else:
            target.unlink(missing_ok=True)


def verify(state, all_models=False):
    base = f"http://127.0.0.1:{state['manifest']['port']}/v1"
    token = state['local_key']
    catalog = json.loads(request(base + '/models?client_version=0.155.0', token))
    entries = catalog.get('models', catalog.get('data', []))
    found = {m.get('slug', m.get('id')) for m in entries}
    expected = {m['id'] for m in state['manifest']['models']}
    if found != expected:
        raise RuntimeError('The local catalog does not match the configured model IDs.')
    print(f'PASS: model catalog contains {len(found)} configured models.')
    models = state['manifest']['models'] if all_models else [next(m for m in state['manifest']['models'] if m['id'] == state['manifest']['default_model'])]
    for model in models:
        body = {'model': model['id'], 'input': 'Reply OK.', 'stream': True, 'store': False, 'max_output_tokens': 256}
        if model['reasoning_efforts']:
            body['reasoning'] = {'effort': model['reasoning_efforts'][0]}
        if not response_succeeded(request(base + '/responses', token, body, timeout=90)):
            raise RuntimeError(f"Response did not complete for {model['id']}.")
        print(f"PASS: streaming response completed for {model['id']}.")


def start_service(state_dir, binary, no_service):
    config = state_dir / 'proxy-config.yaml'
    if no_service:
        raise ValueError('Use --no-service only with --dry-run; apply requires a managed service.')
    if platform.system() != 'Darwin':
        raise ValueError('Managed service installation currently supports macOS only.')
    agent = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    content = {'Label': LABEL, 'ProgramArguments': [str(binary), '-config', str(config)],
               'WorkingDirectory': str(state_dir), 'RunAtLoad': True, 'KeepAlive': True,
               'StandardOutPath': str(state_dir/'proxy.stdout.log'), 'StandardErrorPath': str(state_dir/'proxy.stderr.log')}
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    private_write(agent, plistlib.dumps(content))
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(agent)], check=True, capture_output=True)
    return agent


def stop_service():
    if platform.system() == 'Darwin':
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def setup(args):
    manifest = validate_manifest(json.loads(args.config.read_text()))
    state_dir = args.state_dir.expanduser().resolve()
    codex_dir = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).expanduser().resolve()
    cc_dir = Path.home()/'.cc-switch'
    print(f"Provider: {manifest['name']}; models: {len(manifest['models'])}; loopback port: {manifest['port']}")
    print('Personal auth.json will not be modified. Runtime credentials stay outside this repository.')
    print('CC Switch integration: ' + ('enabled' if args.with_cc_switch else 'not requested'))
    if args.dry_run:
        render_codex_config((codex_dir/'config.toml').read_text() if (codex_dir/'config.toml').exists() else '', args.activate, manifest, 'DRY_RUN_LOCAL_TOKEN')
        print('DRY RUN passed. No files changed and no model requests sent.')
        return
    if platform.system() != 'Darwin':
        raise ValueError('This release installs a macOS LaunchAgent. Use --dry-run to inspect on other systems.')
    previous = json.loads((state_dir/'state.json').read_text()) if (state_dir/'state.json').exists() else None
    with_cc_switch = args.with_cc_switch or bool(previous and previous.get('with_cc_switch'))
    if with_cc_switch:
        require_cc_closed()
    if with_cc_switch or (cc_dir/'cc-switch.db').exists():
        cc_schema(cc_dir/'cc-switch.db', (codex_dir/'config.toml').read_text() if (codex_dir/'config.toml').exists() else None)
    agent = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    if agent.exists():
        existing_agent = plistlib.loads(agent.read_bytes())
        if Path(existing_agent.get('WorkingDirectory', '')).resolve() != state_dir:
            raise ValueError('A gateway service is managed from a different state directory. Use that directory first.')
    try:
        with socket.create_connection(('127.0.0.1', manifest['port']), timeout=1):
            if not previous or previous['manifest']['port'] != manifest['port']:
                raise ValueError('The port is already in use. Choose another port; existing services are never taken over.')
            request(f"http://127.0.0.1:{manifest['port']}/v1/models", previous['local_key'], timeout=3)
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        if isinstance(exc, ValueError):
            raise
    if not args.yes:
        if input('Apply this setup and send a short verification request? [y/N] ').strip().lower() != 'y':
            print('Cancelled; no changes made.')
            return
    key = os.environ.get('GATEWAY_API_KEY', '')
    if args.key_file:
        if args.key_file.stat().st_mode & 0o077:
            raise ValueError('The key file must be private: chmod 600 <key-file>.')
        key = args.key_file.read_text().strip()
    if not key:
        if not sys.stdin.isatty():
            raise ValueError('Set GATEWAY_API_KEY, use --key-file, or run interactively for hidden key input.')
        key = getpass.getpass('Upstream API key (hidden): ').strip()
    if not key or '\n' in key or '\r' in key:
        raise ValueError('A non-empty single-line API key is required.')
    local_key = previous['local_key'] if previous else secrets.token_urlsafe(32)
    codex_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    state_dir.chmod(0o700)
    original = (codex_dir/'config.toml').read_text() if (codex_dir/'config.toml').exists() else ''
    # Use the first personal baseline on subsequent runs, rather than a gateway default.
    personal_base = (state_dir/'personal-baseline.toml').read_text() if (state_dir/'personal-baseline.toml').exists() else original
    manifest = personal_preferences(manifest, personal_base)
    profiles = {mode: render_codex_config(original, mode, manifest, local_key) for mode in CARD_IDS}
    binary = state_dir/'bin/cli-proxy-api'
    agent = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    paths = [codex_dir/'config.toml', codex_dir/'models_cache.json', state_dir/'proxy-config.yaml', state_dir/'state.json', state_dir/'personal-baseline.toml', binary, agent]
    if with_cc_switch:
        paths += [cc_dir/'cc-switch.db', cc_dir/'settings.json']
    backup = create_snapshot(state_dir, paths)
    print(f'Recovery snapshot: {backup}', flush=True)
    auth_path = codex_dir/'auth.json'
    auth_before = auth_path.read_bytes() if auth_path.exists() else None
    state = {'version': VERSION, 'manifest': manifest, 'local_key': local_key, 'codex_dir': str(codex_dir), 'with_cc_switch': with_cc_switch, 'backup': str(backup), 'active': args.activate}
    try:
        if args.proxy_binary:
            private_write(binary, args.proxy_binary.resolve().read_bytes()); binary.chmod(0o700)
        elif not binary.exists():
            print(f'Downloading CLIProxyAPI {PROXY_VERSION} and verifying its release checksum...')
            download_proxy(binary)
        proxy = proxy_config(manifest, key, local_key)
        proxy['auth-dir'] = str(state_dir/'auth')
        private_write(state_dir/'proxy-config.yaml', json.dumps(proxy, ensure_ascii=False, indent=2))
        private_write(state_dir/'personal-baseline.toml', personal_base)
        start_service(state_dir, binary, args.no_service)
        deadline = time.monotonic()+20
        while True:
            try:
                request(f"http://127.0.0.1:{manifest['port']}/v1/models", local_key, timeout=2)
                break
            except RuntimeError:
                if time.monotonic()>deadline: raise RuntimeError('Proxy did not start; inspect the private runtime logs.')
                time.sleep(.25)
        verify(state, args.verify_all)
        if with_cc_switch:
            integrate_cc_switch(cc_dir/'cc-switch.db', cc_dir/'settings.json', profiles['personal'], profiles['gateway'], args.activate)
        private_write(codex_dir/'config.toml', profiles[args.activate])
        (codex_dir/'models_cache.json').unlink(missing_ok=True)
        private_write(state_dir/'state.json', json.dumps(state, ensure_ascii=False, indent=2))
        assert (auth_path.read_bytes() if auth_path.exists() else None) == auth_before, 'Authentication unexpectedly changed.'
    except BaseException:
        stop_service()
        restore_snapshot(backup)
        if previous and agent.exists():
            subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(agent)],capture_output=True)
        raise
    print('Setup complete. Personal authentication preserved.')
    print(f'Backup: {backup}')
    print('Reopen CC Switch and Codex, then start a new conversation. Use the two Provider Kit cards for future switches.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    default_state = Path.home()/'.local/share/codex-provider-setup'
    install = sub.add_parser('setup', help='Preview or install a local gateway.')
    install.add_argument('--config', type=Path, required=True)
    install.add_argument('--state-dir', type=Path, default=default_state)
    install.add_argument('--key-file', type=Path)
    install.add_argument('--proxy-binary', type=Path, help='Use a trusted existing executable instead of downloading.')
    install.add_argument('--with-cc-switch', action='store_true')
    install.add_argument('--activate', choices=CARD_IDS, default='gateway')
    install.add_argument('--dry-run', action='store_true')
    install.add_argument('--yes', action='store_true')
    install.add_argument('--verify-all', action='store_true', help='Send one short paid request per configured model.')
    install.add_argument('--no-service', action='store_true', help=argparse.SUPPRESS)
    check = sub.add_parser('verify', help='Verify the local catalog and a short model response.')
    check.add_argument('--state-dir', type=Path, default=default_state)
    check.add_argument('--all', action='store_true')
    switch = sub.add_parser('switch', help='Switch without CC Switch; reopen Codex afterwards.')
    switch.add_argument('mode', choices=CARD_IDS)
    switch.add_argument('--state-dir', type=Path, default=default_state)
    rollback = sub.add_parser('rollback', help='Restore a setup snapshot; quit CC Switch first.')
    rollback.add_argument('--state-dir', type=Path, default=default_state)
    rollback.add_argument('--backup', type=Path)
    rollback.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'setup':
            setup(args)
            return 0
        state_dir = args.state_dir.expanduser().resolve()
        state_file = state_dir/'state.json'
        if args.command == 'rollback':
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            selected = args.backup or state.get('backup')
            if not selected:
                raise ValueError('Provide --backup with the recovery snapshot printed during setup.')
            backup = Path(selected).resolve()
            if not backup.is_relative_to(state_dir/'backups'):
                raise ValueError('Use a backup generated inside this installation.')
            items = json.loads((backup/'manifest.json').read_text())
            if any(Path(item['path']).name == 'cc-switch.db' for item in items): require_cc_closed()
            if not args.yes and input(f'Restore configuration snapshot {backup.name}? [y/N] ').lower() != 'y': return 0
            stop_service(); restore_snapshot(backup)
            agent = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
            if state_file.exists() and agent.exists():
                subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(agent)],check=True,capture_output=True)
            print('Configuration snapshot restored. Personal auth.json was not touched.')
            return 0
        state = json.loads(state_file.read_text())
        if args.command == 'verify':
            verify(state, args.all)
        elif args.command == 'switch':
            if state['with_cc_switch']:
                raise ValueError('This installation is managed by CC Switch. Use its cards to keep state consistent.')
            if args.mode == 'gateway': verify(state)
            codex_dir = Path(state['codex_dir'])
            original = (codex_dir/'config.toml').read_text()
            manifest = personal_preferences(state['manifest'], (state_dir/'personal-baseline.toml').read_text())
            create_snapshot(state_dir, [codex_dir/'config.toml', codex_dir/'models_cache.json'])
            private_write(codex_dir/'config.toml', render_codex_config(original,args.mode,manifest,state['local_key']))
            (codex_dir/'models_cache.json').unlink(missing_ok=True)
            state['active'] = args.mode
            private_write(state_dir/'state.json',json.dumps(state,indent=2))
            print(f'Switched to {args.mode}. Reopen Codex and start a new conversation.')
        return 0
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
