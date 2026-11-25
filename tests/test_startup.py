import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


START_SCRIPT = Path(__file__).resolve().parents[1] / 'start.sh'


class StartupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'Project with spaces'
        self.root.mkdir()
        self.root = self.root.resolve()
        shutil.copyfile(START_SCRIPT, self.root / 'start.sh')
        self.environment = dict(os.environ)
        self.environment.pop('OPENHOMEKTV_VENV', None)

    def fake_venv(self, relative='.venv'):
        venv = self.root / relative
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/activate').write_text('export VIRTUAL_ENV=' + "'" + str(venv) + "'\n", encoding='utf-8')
        python = venv / 'bin/python'
        python.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"venv":os.environ.get("VIRTUAL_ENV")}))\n',
            encoding='utf-8',
        )
        python.chmod(0o755)
        return venv

    def test_activates_local_venv_from_other_directory_and_preserves_arguments(self):
        venv = self.fake_venv()
        result = subprocess.run(
            ['bash', str(self.root / 'start.sh'), '--media-path', '/Volumes/中文 files/KTV', '--port', '5240'],
            cwd=self.temporary.name, env=self.environment, text=True, capture_output=True, check=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output['cwd'], str(self.root))
        self.assertEqual(output['venv'], str(venv))
        self.assertEqual(output['args'], [str(self.root / 'app.py'), '--media-path', '/Volumes/中文 files/KTV', '--port', '5240'])

    def test_custom_relative_virtual_environment(self):
        venv = self.fake_venv('another env')
        self.environment['OPENHOMEKTV_VENV'] = 'another env'
        result = subprocess.run(['bash', str(self.root / 'start.sh'), '--help'], cwd='/', env=self.environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['venv'], str(venv))

    def test_missing_venv_has_actionable_error_and_does_not_install_anything(self):
        result = subprocess.run(['bash', str(self.root / 'start.sh')], cwd='/', env=self.environment,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('python3 -m venv', result.stderr)
        self.assertFalse((self.root / '.venv').exists())


if __name__ == '__main__':
    unittest.main()
