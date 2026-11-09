"""Discovery checks do not start Chrome or inspect the user's profile."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gemini_web


class ChromeDiscoveryTests(unittest.TestCase):
    def test_override_precedes_all_discovery_even_when_missing(self):
        with mock.patch.dict(os.environ, {'GEMINI_WEB_CHROME': '/configured/chrome'}, clear=True), \
                mock.patch.object(gemini_web.shutil, 'which') as which:
            self.assertEqual(gemini_web.discover_chrome(), Path('/configured/chrome'))
            which.assert_not_called()

    def test_each_windows_install_root_is_supported(self):
        for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as directory:
                candidate = Path(directory) / 'Google/Chrome/Application/chrome.exe'
                candidate.parent.mkdir(parents=True)
                candidate.touch()
                with mock.patch.dict(os.environ, {variable: directory}, clear=True), \
                        mock.patch.object(gemini_web.shutil, 'which') as which:
                    self.assertEqual(gemini_web.discover_chrome(), candidate)
                    which.assert_not_called()

    def test_path_install_is_supported(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(gemini_web.shutil, 'which', return_value='/custom/chrome'):
            self.assertEqual(gemini_web.discover_chrome(), Path('/custom/chrome'))


if __name__ == '__main__':
    unittest.main()
