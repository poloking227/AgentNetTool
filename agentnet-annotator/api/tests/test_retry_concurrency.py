"""Deterministic duplicate Retry and queue failure regressions."""
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue, Full
from unittest.mock import MagicMock, patch
from test_reprocessing_video import service_module, state

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'recording';self.path.mkdir()
        for name in ['events.jsonl','metadata.json','raw.mp4']:(self.path/name).write_text('raw')
        state.write_state(self.path,status='failed',stage='match',error='old')
        self.service=object.__new__(service_module.RecordingService)
        self.service.recorder_thread=None;self.service._active_lock=threading.Lock()
        self.service._active_recordings=set();self.service.reducer_queue=Queue()

    def test_duplicate_retry_cannot_overwrite_worker_result(self):
        entered=threading.Event();release=threading.Event();second_started=threading.Event()
        original=service_module.write_state;results=[]
        def slow_write(*args,**kwargs):
            if kwargs.get('status')=='pending':
                entered.set()
                if not release.wait(3):raise RuntimeError('test timeout')
            return original(*args,**kwargs)
        def immediate_completion(recording_id):
            state.write_state(self.path,status='done',stage='done')
        self.service.reducer_queue.put=MagicMock(side_effect=immediate_completion)
        def call(second=False):
            if second:second_started.set()
            results.append(self.service.reprocess_recording('recording')[0])
        with patch.object(service_module,'RECORDING_DIR',str(self.path.parent)),patch.object(service_module,'write_state',side_effect=slow_write):
            first=threading.Thread(target=call);first.start()
            self.assertTrue(entered.wait(3))
            second=threading.Thread(target=call,args=(True,));second.start();self.assertTrue(second_started.wait(3))
            release.set();first.join(3);second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(sorted(results),['failed','succeed'])
        self.assertEqual(state.read_state(self.path)['status'],'done')
        self.service.reducer_queue.put.assert_called_once_with('recording')

    def test_queue_failure_releases_reservation_and_allows_retry(self):
        with patch.object(service_module,'RECORDING_DIR',str(self.path.parent)):
            with patch.object(self.service.reducer_queue,'put',side_effect=Full):
                result,_=self.service.reprocess_recording('recording')
            self.assertEqual(result,'failed');self.assertEqual(self.service._active_recordings,set())
            self.assertEqual(state.read_state(self.path)['status'],'failed')
            self.assertEqual(self.service.reprocess_recording('recording')[0],'succeed')
        self.assertEqual(self.service.reducer_queue.get_nowait(),'recording')

    def test_state_write_failure_does_not_queue_or_reserve(self):
        with patch.object(service_module,'RECORDING_DIR',str(self.path.parent)),patch.object(service_module,'write_state',side_effect=PermissionError('test')):
            self.assertEqual(self.service.reprocess_recording('recording')[0],'failed')
        self.assertTrue(self.service.reducer_queue.empty());self.assertEqual(self.service._active_recordings,set())

    def test_normal_enqueue_also_rolls_back_failed_put(self):
        with patch.object(self.service.reducer_queue,'put',side_effect=Full):
            with self.assertRaises(Full):self.service._enqueue('recording')
        self.assertEqual(self.service._active_recordings,set())
        self.service._enqueue('recording');self.assertEqual(self.service.reducer_queue.get_nowait(),'recording')

if __name__=='__main__':unittest.main()
