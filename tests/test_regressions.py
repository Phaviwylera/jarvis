"""Offline regressions: no real keys, network, or personal memory required."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MEMORY = tempfile.TemporaryDirectory()
os.environ['JARVIS_MEMORY_DIR'] = MEMORY.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
with patch('threading.Thread.start'):
    import brain
import main
from fastapi.testclient import TestClient


class Regressions(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        brain.AI = brain.AIBrain()
        brain._CACHE.clear()
        self.client = TestClient(main.app)

    def test_named_primary_key(self):
        self.assertTrue(brain.AI.available)
        self.assertEqual(brain.AI.chain[0]['key'], 'test-only')

    def test_cache_isolates_sessions_and_context(self):
        with patch.object(brain.AI, '_ask_gemini', side_effect=['first', 'other user', 'changed context']) as ask:
            self.assertEqual(brain.AI.answer('Which one?', 'a'), 'first')
            self.assertEqual(brain.AI.answer('Which one?', 'a'), 'first')
            self.assertEqual(brain.AI.answer('Which one?', 'b'), 'other user')
            brain.AI.histories['a'] = [{'role': 'user', 'content': 'Different options'}]
            self.assertEqual(brain.AI.answer('Which one?', 'a'), 'changed context')
            self.assertEqual(ask.call_count, 3)

    def test_cache_expiry(self):
        with patch.object(brain.time, 'time', return_value=100):
            brain._answer_cache_put('key', 'old')
        with patch.object(brain.time, 'time', return_value=100 + brain._CACHE_TTL + 1):
            self.assertIsNone(brain._answer_cache_get('key'))

    def test_original_questions_reach_ai(self):
        questions = ['What is time dilation?', 'Explain how weather affects flight',
                     'What is Thanksgiving?', 'What day did Apollo 11 land?',
                     'What is the date format ISO 8601?', 'Explain photosynthesis simply']
        with patch.object(brain, '_fallback', return_value='answer') as fallback:
            for question in questions:
                with self.subTest(question=question):
                    self.assertEqual(brain.handle(question, 'a')['replies'], ['answer'])
                    fallback.assert_called_with(question, 'a')

    def test_local_commands_record_context_and_repeat(self):
        first = brain.handle('calculate 2 + 3', 'a')
        self.assertIn('5', first['replies'][0])
        self.assertIn(first['replies'][0], brain.handle('repeat', 'a')['replies'][0])
        self.assertEqual(len(brain.AI.histories['a']), 4)
        self.assertNotIn('b', brain.AI.histories)

    def test_ai_turn_not_duplicated_and_full_reply_preserved(self):
        answer = 'A detailed answer. ' * 50
        with patch.object(brain, '_rag_augment', return_value=''), patch.object(brain.AI, '_ask_gemini', return_value=answer):
            result = brain.handle('Help plan a project', 'a')
        self.assertEqual(result['replies'], [answer.strip()])
        self.assertEqual(len(brain.AI.histories['a']), 2)
        self.assertEqual(result['provider'], 'gemini')
        local = brain.handle('time', 'a')
        self.assertEqual(local['provider'], 'local')
        self.assertFalse(local['stats']['cache'])

    def test_polite_open(self):
        result = brain.handle('Hey Jarvis, please open YouTube', 'a')
        self.assertEqual(result['actions'][0]['url'], 'https://www.youtube.com')

    def test_invalid_reminder(self):
        with patch.object(brain, '_schedule') as schedule:
            for value in ['25:00', '12:80', '13 pm']:
                self.assertIn('valid time', brain.handle('remind me to call at ' + value)['replies'][0])
            schedule.assert_not_called()

    def test_wikipedia_recursion_bounded(self):
        with patch.object(brain, 'http_get_json', return_value=['x', ['x']]) as get:
            self.assertIsNone(brain.wikipedia_summary('x'))
            self.assertLessEqual(get.call_count, 4)

    def test_unavailable_ai_does_not_invent_an_answer(self):
        with patch.object(brain.AI, 'answer', return_value=None), patch.object(brain, '_rag_augment', return_value=''), patch.object(brain, 'wikipedia_summary') as wiki:
            self.assertIn('could not get an AI answer', brain.handle('Help plan a project')['replies'][0])
            wiki.assert_not_called()

    def test_api_validation(self):
        for payload in [{'text': ' '}, {'text': 'x' * 4001}, {'text': 'hi', 'session_id': '../bad'}]:
            self.assertEqual(self.client.post('/api/command', json=payload).status_code, 422)

    def test_api_status_static_and_errors(self):
        self.assertEqual(self.client.get('/api/health').json()['version'], '2.1.1')
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/api/missing').status_code, 404)
        with patch.object(brain, 'handle', side_effect=RuntimeError('private error')):
            response = self.client.post('/api/command', json={'text': 'hello'})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private error', response.text)

    def test_existing_sentinel_commands_preserved(self):
        self.assertIn('6.2137', brain.handle('convert 10 km to miles')['replies'][0])
        with patch.object(brain, 'translate_text', return_value='Bonjour'):
            self.assertIn('Bonjour', ' '.join(brain.handle('translate hello to french')['replies']))


if __name__ == '__main__':
    unittest.main()
