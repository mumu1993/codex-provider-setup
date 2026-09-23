import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provider_setup as kit


def manifest():
    return {
        'name': 'Team Gateway', 'port': 8317,
        'models': [
            {'id': 'reasoner-2026-01-01', 'protocol': 'responses', 'base_url': 'https://gateway.example.com/v1', 'reasoning_efforts': ['low', 'high']},
            {'id': 'chat/vision', 'protocol': 'chat', 'base_url': 'https://gateway.example.com/deployments/{model}', 'api_key_header': 'api-key'},
        ],
    }


class ManifestTests(unittest.TestCase):
    def test_routes_keep_exact_ids_and_isolate_credentials(self):
        m = kit.validate_manifest(manifest())
        p = kit.proxy_config(m, 'UPSTREAM_TEST_SECRET', 'LOCAL_TEST_SECRET')
        self.assertEqual(p['host'], '127.0.0.1')
        self.assertEqual(p['api-keys'], ['LOCAL_TEST_SECRET'])
        self.assertEqual(p['codex-api-key'][0]['models'][0]['name'], 'reasoner-2026-01-01')
        self.assertEqual(p['openai-compatibility'][0]['base-url'], 'https://gateway.example.com/deployments/chat%2Fvision')
        self.assertEqual(p['openai-compatibility'][0]['headers']['api-key'], 'UPSTREAM_TEST_SECRET')
        self.assertEqual(p['disable-image-generation'], 'passthrough')
        c = kit.render_codex_config('', 'gateway', m, 'LOCAL_TEST_SECRET')
        self.assertNotIn('UPSTREAM_TEST_SECRET', c)
        self.assertIn('LOCAL_TEST_SECRET', c)

    def test_rejects_duplicates_insecure_urls_and_bad_efforts(self):
        for mutate in [
            lambda m: m['models'].append(copy.deepcopy(m['models'][0])),
            lambda m: m['models'][0].update(base_url='http://remote.example.com/v1'),
            lambda m: m['models'][0].update(base_url='https://user:secret@gateway.example.com/v1'),
            lambda m: m['models'][0].update(reasoning_efforts=['invented']),
            lambda m: m['models'][0].update(protocol='unknown'),
        ]:
            m = manifest(); mutate(m)
            with self.assertRaises(ValueError): kit.validate_manifest(m)

    def test_gateway_and_personal_modes_preserve_unrelated_settings(self):
        original = '''# User preferences
model = "my-personal-model"
model_provider = "old_gateway"
model_catalog_json = "/old/catalog.json"
model_context_window = 900000
notify = ["/some path/notify"]
[features]
example = true
[plugins."example@personal"]
enabled = true
[model_providers.old_gateway]
name = "Other gateway"
base_url = "https://other.example.com/v1"
'''
        m = kit.validate_manifest(manifest())
        before = tomllib.loads(original)
        gateway = tomllib.loads(kit.render_codex_config(original, 'gateway', m, 'LOCAL_TEST_SECRET'))
        personal = tomllib.loads(kit.render_codex_config(original, 'personal', m, 'LOCAL_TEST_SECRET'))
        for c in [gateway, personal]:
            for k in ['features', 'plugins', 'notify']:
                self.assertEqual(c[k], before[k])
            self.assertEqual(c['model_providers']['old_gateway'], before['model_providers']['old_gateway'])
            self.assertNotIn('model_catalog_json', c)
            self.assertNotIn('model_context_window', c)
        self.assertEqual(gateway['model_provider'], 'provider_kit')
        self.assertFalse(gateway['model_providers']['provider_kit']['requires_openai_auth'])
        self.assertEqual(personal['model_provider'], 'openai')

    def test_http_200_error_is_not_a_success(self):
        self.assertFalse(kit.response_succeeded(b'{"error":{"message":"failed"}}'))
        self.assertFalse(kit.response_succeeded(b'data: {"type":"response.failed"}\n\n'))
        self.assertFalse(kit.response_succeeded(b'{"status":"incomplete","output":[]}'))
        self.assertTrue(kit.response_succeeded(b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'))


class CCSwitchTests(unittest.TestCase):
    def test_adds_only_owned_cards_and_preserves_foreign_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); dbfile = root/'cc-switch.db'; settings = root/'settings.json'
            db = sqlite3.connect(dbfile)
            db.execute('CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, meta TEXT, category TEXT, is_current INTEGER, PRIMARY KEY(id, app_type))')
            db.execute('CREATE TABLE proxy_config (app_type TEXT,enabled INTEGER,live_takeover_active INTEGER)')
            db.execute("INSERT INTO proxy_config VALUES ('codex',0,0)")
            db.execute("INSERT INTO providers VALUES ('foreign','claude','Keep me','{}','{}',NULL,1)")
            db.commit(); db.close()
            settings.write_text('{"language":"zh","preserveCodexOfficialAuthOnSwitch":true}')
            m = kit.validate_manifest(manifest())
            a = kit.render_codex_config('', 'personal', m, 'LOCAL_TEST_SECRET')
            b = kit.render_codex_config('', 'gateway', m, 'LOCAL_TEST_SECRET')
            kit.integrate_cc_switch(dbfile, settings, a, b, 'gateway')
            kit.integrate_cc_switch(dbfile, settings, a, b, 'gateway')
            db = sqlite3.connect(dbfile)
            self.assertEqual(db.execute('SELECT count(*) FROM providers').fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT name,is_current FROM providers WHERE id='foreign'").fetchone(), ('Keep me', 1))
            for (meta,) in db.execute("SELECT meta FROM providers WHERE app_type='codex'"):
                self.assertNotIn('localProxyRequestOverrides', json.loads(meta))
                self.assertNotIn('apiFormat', json.loads(meta))
            self.assertEqual(json.loads(settings.read_text())['language'], 'zh')
            db.close()

    def test_unknown_schema_fails_without_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); dbfile=root/'cc-switch.db'; settings=root/'settings.json'
            db=sqlite3.connect(dbfile); db.execute('CREATE TABLE unrelated (id TEXT)'); db.commit(); db.close()
            settings.write_text('{"keep":true}')
            before=dbfile.read_bytes()
            with self.assertRaises(ValueError):kit.integrate_cc_switch(dbfile,settings,'','', 'gateway')
            self.assertEqual(dbfile.read_bytes(),before)
            self.assertEqual(settings.read_text(),'{"keep":true}')


class LifecycleTests(unittest.TestCase):
    def test_sqlite_snapshot_includes_committed_wal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'state.db'; writer=sqlite3.connect(path)
            writer.execute('PRAGMA journal_mode=WAL'); writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE records (value TEXT)'); writer.execute("INSERT INTO records VALUES ('original')"); writer.commit()
            backup=kit.create_snapshot(root,[path])
            writer.execute("INSERT INTO records VALUES ('new')"); writer.commit(); writer.close()
            kit.restore_snapshot(backup)
            restored=sqlite3.connect(path)
            try:
                self.assertEqual(restored.execute('SELECT value FROM records').fetchall(),[('original',)])
            finally:restored.close()

    def test_symlink_snapshot_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); target=root/'dotfile'; target.write_text('original')
            link=root/'config.toml'; link.symlink_to(target)
            with self.assertRaises(ValueError):kit.create_snapshot(root,[link])
            self.assertTrue(link.is_symlink()); self.assertEqual(target.read_text(),'original')

    def test_explicit_rollback_works_without_completed_install_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); config=root/'config.toml'; config.write_text('original')
            backup=kit.create_snapshot(root,[config]); config.write_text('partial installation')
            with patch.object(kit,'stop_service'):
                result=kit.main(['rollback','--state-dir',str(root),'--backup',str(backup),'--yes'])
            self.assertEqual(result,0); self.assertEqual(config.read_text(),'original')

    def test_personal_switch_keeps_preferences_added_after_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); codex = root/'codex'; state = root/'state'
            codex.mkdir(); state.mkdir()
            m = kit.validate_manifest(manifest())
            baseline = 'model = "personal-model"\n[features]\noriginal = true\n'
            (state/'personal-baseline.toml').write_text(baseline)
            current = kit.render_codex_config(baseline,'gateway',m,'LOCAL_TEST_SECRET')
            current = current.replace('original = true', 'original = true\nnew_setting = true')
            (codex/'config.toml').write_text(current)
            (codex/'auth.json').write_bytes(b'UNCHANGED_AUTH')
            (state/'state.json').write_text(json.dumps({'manifest':m,'local_key':'LOCAL_TEST_SECRET','codex_dir':str(codex),'with_cc_switch':False}))
            self.assertEqual(kit.main(['switch','personal','--state-dir',str(state)]),0)
            restored = tomllib.loads((codex/'config.toml').read_text())
            self.assertTrue(restored['features'].get('new_setting'), 'Switching must not restore unrelated stale settings')
            self.assertEqual(restored['model'],'personal-model')
            self.assertEqual((codex/'auth.json').read_bytes(),b'UNCHANGED_AUTH')

    def test_setup_failure_rolls_back_configuration_and_keeps_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); codex = home/'.codex'; codex.mkdir()
            original = 'model = "personal-model"\n[plugins.example]\nenabled = true\n'
            (codex/'config.toml').write_text(original)
            (codex/'auth.json').write_bytes(b'UNCHANGED_AUTH')
            config = home/'gateway.json'; config.write_text(json.dumps(manifest()))
            binary = home/'trusted-binary'; binary.write_bytes(b'not-executed')
            state = home/'runtime'
            args = SimpleNamespace(config=config,state_dir=state,with_cc_switch=False,dry_run=False,yes=True,key_file=None,proxy_binary=binary,activate='gateway',no_service=False,verify_all=False)
            with patch.object(kit.platform,'system',return_value='Darwin'), patch.object(Path,'home',return_value=home), patch.dict(kit.os.environ,{'CODEX_HOME':str(codex),'GATEWAY_API_KEY':'UPSTREAM_TEST_SECRET'}), patch.object(kit.socket,'create_connection',side_effect=ConnectionRefusedError), patch.object(kit,'start_service',side_effect=RuntimeError('test startup failure')), patch.object(kit,'stop_service'):
                with self.assertRaisesRegex(RuntimeError,'test startup failure'):kit.setup(args)
            self.assertEqual((codex/'config.toml').read_text(),original)
            self.assertEqual((codex/'auth.json').read_bytes(),b'UNCHANGED_AUTH')
            self.assertFalse((state/'state.json').exists())
            self.assertFalse((state/'bin/cli-proxy-api').exists())

    def test_snapshot_restores_executable_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); executable=root/'bin'; executable.write_bytes(b'old'); executable.chmod(0o700)
            backup=kit.create_snapshot(root,[executable])
            executable.write_bytes(b'new'); executable.chmod(0o600)
            kit.restore_snapshot(backup)
            self.assertEqual(executable.read_bytes(),b'old')
            self.assertEqual(executable.stat().st_mode & 0o777,0o700)

    def test_keyboard_interrupt_rolls_back_first_install(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp); codex=home/'.codex'; codex.mkdir(); (codex/'config.toml').write_text('model = "personal"\n')
            (codex/'auth.json').write_bytes(b'UNCHANGED_AUTH'); config=home/'gateway.json';config.write_text(json.dumps(manifest()))
            binary=home/'binary';binary.write_bytes(b'not-executed');state=home/'runtime'
            args=SimpleNamespace(config=config,state_dir=state,with_cc_switch=False,dry_run=False,yes=True,key_file=None,proxy_binary=binary,activate='gateway',no_service=False,verify_all=False)
            with patch.object(kit.platform,'system',return_value='Darwin'),patch.object(Path,'home',return_value=home),patch.dict(kit.os.environ,{'CODEX_HOME':str(codex),'GATEWAY_API_KEY':'UPSTREAM_TEST_SECRET'}),patch.object(kit.socket,'create_connection',side_effect=ConnectionRefusedError),patch.object(kit,'start_service',side_effect=KeyboardInterrupt),patch.object(kit,'stop_service') as stop:
                with self.assertRaises(KeyboardInterrupt):kit.setup(args)
            self.assertFalse((state/'proxy-config.yaml').exists())
            stop.assert_called_once()

    def test_live_takeover_flag_blocks_cc_integration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); dbfile=root/'cc-switch.db'; db=sqlite3.connect(dbfile)
            db.execute('CREATE TABLE providers (id TEXT,app_type TEXT,name TEXT,settings_config TEXT,meta TEXT,category TEXT,is_current INTEGER)')
            db.execute('CREATE TABLE proxy_config (app_type TEXT,enabled INTEGER,live_takeover_active INTEGER)')
            db.execute("INSERT INTO proxy_config VALUES ('codex',0,1)");db.commit();db.close()
            with self.assertRaises(ValueError):kit.cc_schema(dbfile)


if __name__ == '__main__': unittest.main()
