"""State corruption recovery checks, isolated from desktop and real recordings."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from test_reprocessing_video import state, service_module

class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def recording(self,name='recording',content=None):
        p=self.root/name;p.mkdir(exist_ok=True)
        for f in ['events.jsonl','metadata.json','raw.mp4','reduced_events_vis.jsonl']:
            (p/f).write_text('original input')
        (p/'video_clips').mkdir(exist_ok=True)
        if content is not None:(p/'processing_state.json').write_text(content)
        return p

    def service(self):
        s=object.__new__(service_module.RecordingService);s.recorder_thread=None
        s._active_lock=threading.Lock();s._active_recordings=set();s._enqueue=MagicMock()
        from queue import Queue
        s.reducer_queue=Queue()
        return s

    def test_absent_state_keeps_legacy_completion(self):
        p=self.recording();self.assertIsNone(state.read_state(p));self.assertEqual(state.get_processing_status(p),'done')

    def test_invalid_json_never_falls_back_to_done(self):
        p=self.recording(content='{broken')
        self.assertEqual(state.get_processing_status(p),'failed')
        self.assertEqual(state.read_state(p)['error_kind'],'state')
        self.assertEqual((p/'processing_state.json').read_text(),'{broken')
        s=self.service()
        with patch.object(service_module,'RECORDING_DIR',str(self.root)):
            self.assertEqual(s._get_processing_status(p.name),'failed')
            info=s._update_recording_info({},p.name,False)
        self.assertIn('Retry',info['processing_error']);self.assertEqual(info['processing_error_kind'],'state')

    def test_non_object_json_is_failed(self):
        for i,value in enumerate([[], 'text', 1, True, None]):
            p=self.recording(str(i),json.dumps(value))
            self.assertEqual(state.read_state(p)['status'],'failed')

    def test_invalid_fields_are_failed(self):
        values=[{}, {'status':'unknown'}, {'status':'failed','error':[]}, {'status':'failed','error_kind':{}}, {'status':'pending','attempts':'2'},
                {'status':'pending','attempts':True}, {'status':'pending','attempts':-1},
                {'status':'pending','stage':[]}, {'status':'pending','configs':[]},
                {'status':'pending','configs':{'generate_element_a11y':'yes'}},
                {'status':'pending','window_attrs':[]},
                {'status':'pending','window_attrs':{'width':0,'height':20}},
                {'status':'pending','window_attrs':{'width':True,'height':20}}]
        for i,value in enumerate(values):
            with self.subTest(value=value):
                p=self.recording(str(i),json.dumps(value));self.assertEqual(state.read_state(p)['status'],'failed')

    def test_unreadable_state_is_failed(self):
        p=self.recording(content='{"status":"done"}')
        with patch('builtins.open',side_effect=PermissionError('test')):
            self.assertEqual(state.read_state(p)['status'],'failed')

    def test_state_inspection_denied_is_not_absent(self):
        with patch.object(state.os,'lstat',side_effect=PermissionError('test')):
            self.assertEqual(state.read_state(self.root)['status'],'failed')

    def test_directory_and_dangling_symlink_fail_closed(self):
        p=self.recording('directory');(p/'processing_state.json').mkdir()
        self.assertEqual(state.read_state(p)['status'],'failed')
        p=self.recording('symlink');(p/'processing_state.json').symlink_to(p/'missing')
        self.assertEqual(state.read_state(p)['status'],'failed')

    def test_invalid_encoding_is_failed(self):
        p=self.recording();(p/'processing_state.json').write_bytes(b'\xff\xfe')
        self.assertEqual(state.read_state(p)['status'],'failed')

    def test_startup_skips_bad_states_and_continues_valid_recording(self):
        for i,content in enumerate(['[]','1','"text"','{broken','{"status":"pending","attempts":"bad"}']):
            self.recording(str(i),content)
        self.recording('good','{"status":"pending","attempts":0}')
        s=self.service()
        with patch.object(service_module,'RECORDING_DIR',str(self.root)):
            s._resume_interrupted_processing()
        s._enqueue.assert_called_once_with('good')

    def test_unreadable_raw_folder_does_not_block_other_recovery(self):
        self.recording('bad','{"status":"pending"}');self.recording('good','{"status":"pending"}')
        s=self.service()
        def raw(path):
            if Path(path).name=='bad':raise PermissionError('test')
            return True
        with patch.object(service_module,'RECORDING_DIR',str(self.root)),patch.object(service_module,'has_raw_data',side_effect=raw):
            s._resume_interrupted_processing()
        s._enqueue.assert_called_once_with('good')

    def test_manual_retry_repairs_corrupted_state_without_touching_raw_data(self):
        for i,content in enumerate(['[]','{broken','{"status":"done","attempts":"bad"}']):
            p=self.recording(str(i),content);s=self.service()
            with patch.object(service_module,'RECORDING_DIR',str(self.root)):
                status,_=s.reprocess_recording(p.name)
            self.assertEqual(status,'succeed');self.assertEqual(s.reducer_queue.get_nowait(),p.name)
            saved=state.read_state(p)
            self.assertEqual(saved['status'],'pending');self.assertIsNone(saved['error']);self.assertIsNone(saved['error_kind'])
            self.assertEqual(saved['attempts'],0)
            self.assertEqual((p/'events.jsonl').read_text(),'original input')

    def test_valid_state_preserves_config_and_clip_fingerprint(self):
        value={'status':'failed','stage':'match','attempts':1,'configs':{'generate_window_a11y':False,'generate_element_a11y':True},'window_attrs':{'width':64,'height':48},'clip_fingerprint':'keep'}
        p=self.recording(content=json.dumps(value));self.assertEqual(state.read_state(p),value)
        state.write_state(p,status='pending',error=None)
        self.assertEqual(state.read_state(p)['clip_fingerprint'],'keep')

    def test_stage_only_initial_write_remains_readable(self):
        p=self.recording();state.write_state(p,stage='reduce')
        self.assertEqual(state.read_state(p)['status'],'pending')

if __name__=='__main__':unittest.main()
