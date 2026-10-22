"""Native configuration workflows, including address/key isolation. No real API calls."""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

from desktop_app.storage import Store
from desktop_app.service import Service
from desktop_app.model_presets import PRESETS, initial_profile
from desktop_app.model_settings import ModelSettings


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='paper-audit-settings-')
        self.root = tk.Tk();self.root.withdraw()
        self.service = Service(Store(Path(self.tmp.name)))
        self.app = SimpleNamespace(root=self.root, service=self.service, api=None, status=tk.StringVar())
        self.dialogs = []

    def tearDown(self):
        for d in self.dialogs:
            if d.window.winfo_exists():d.close()
        self.root.destroy();self.tmp.cleanup()

    def dialog(self):
        d = ModelSettings(self.app);d.window.withdraw();self.root.update_idletasks();self.dialogs.append(d);return d

    def select(self, d, provider):
        d.provider.set(PRESETS[provider]['label']);d.change_provider()

    def test_old_configuration_keeps_custom_values_and_disabled_vision(self):
        p = initial_profile({'base_url':'https://proxy.example/v1','text_model':'my-model','vision_model':''})
        self.assertEqual(p['provider_id'], 'custom')
        self.assertEqual(p['text_model'], 'my-model')
        self.assertEqual(p['vision_model'], '')
        self.assertEqual(initial_profile({'base_url':'https://api.moonshot.cn/v1'})['provider_id'], 'kimi')

    def test_provider_drafts_do_not_reuse_other_provider_key(self):
        d = self.dialog();d.replace('key', 'TEST-DEEPSEEK')
        self.select(d, 'kimi');self.assertEqual(d.entries['key'].get(), '')
        d.replace('base_url', 'https://kimi-proxy.example/v1');d.replace('key', 'TEST-KIMI')
        d.entries['text_model'].set('my-kimi-model')
        self.select(d, 'deepseek');self.assertEqual(d.entries['key'].get(), 'TEST-DEEPSEEK')
        self.select(d, 'kimi');self.assertEqual(d.entries['key'].get(), 'TEST-KIMI')
        self.assertEqual(d.entries['base_url'].get(), 'https://kimi-proxy.example/v1')
        self.assertEqual(d.entries['text_model'].get(), 'my-kimi-model')
        d.replace('base_url', 'https://another.example/v1')
        self.assertEqual(d.entries['key'].get(), '')

    def test_custom_url_and_model_round_trip_without_saving_secret(self):
        d = self.dialog();self.select(d, 'qwen')
        d.replace('base_url', 'https://workspace.example/compatible-mode/v1/')
        d.entries['text_model'].set('custom-qwen');d.entries['vision_model'].set('')
        d.replace('key', 'TEST-KEY');d.save()
        self.assertFalse(d.window.winfo_exists())
        p = self.service.store.setting('model_profile')
        self.assertEqual(p['base_url'], 'https://workspace.example/compatible-mode/v1')
        self.assertEqual(p['text_model'], 'custom-qwen')
        self.assertEqual(p['provider_id'], 'qwen')
        self.assertIsNone(self.service.store.setting('model_key_ciphertext'))
        again = self.dialog()
        self.assertEqual(again.entries['base_url'].get(), p['base_url'])
        self.assertEqual(again.entries['vision_model'].get(), '')
        self.assertEqual(again.entries['key'].get(), '')

    def test_endpoint_change_rejects_old_key_and_does_not_mutate_saved_profile(self):
        self.service.save_model_profile({'base_url':'https://api.deepseek.com/','text_model':'deepseek-flash'}, 'TEST-ORIGINAL', False)
        d = self.dialog();d.replace('base_url', 'https://different.example/v1')
        d.save()
        self.assertTrue(d.window.winfo_exists())
        self.assertEqual(self.service.store.setting('model_profile')['base_url'], 'https://api.deepseek.com')
        self.assertEqual(self.service.secrets.get_model_key(), 'TEST-ORIGINAL')
        self.assertIn('Key', d.feedback.get())

    def test_stale_model_list_cannot_replace_new_provider_choices(self):
        d = self.dialog();old = d.request_id
        self.select(d, 'doubao')
        d.responses.put((old, ['WRONG-PROVIDER-MODEL'], ''))
        d.window.after_cancel(d.poll_id);d.poll()
        self.assertNotIn('WRONG-PROVIDER-MODEL', d.entries['text_model']['values'])
        self.assertEqual(d.entries['text_model'].get(), PRESETS['doubao']['models'][0])

    def test_selecting_text_model_preserves_explicitly_disabled_image_review(self):
        d = self.dialog();self.select(d, 'kimi')
        d.entries['vision_model'].set('')
        d.entries['text_model'].set('kimi-k3');d.change_model()
        self.assertEqual(d.entries['vision_model'].get(), '')

    def test_advanced_options_start_collapsed_and_remain_accessible_at_scaling(self):
        for scaling in (1.0, 1.5, 2.0):
            with self.subTest(scaling=scaling):
                self.root.tk.call('tk', 'scaling', scaling)
                d = self.dialog()
                self.assertEqual(d.advanced.winfo_manager(), '')
                self.assertEqual(str(d.entries['base_url']['state']), 'normal')
                self.assertEqual(len(d.provider['values']), 5)
                d.toggle_advanced();self.root.update_idletasks()
                self.assertEqual(d.advanced.winfo_manager(), 'pack')
                self.assertGreater(d.form.winfo_reqheight(), 0)
                d.toggle_advanced();d.close()


if __name__ == '__main__':unittest.main()
