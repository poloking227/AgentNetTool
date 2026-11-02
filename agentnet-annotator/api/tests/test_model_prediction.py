"""Offline tests for model errors, retry and cache behavior (requests required)."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests
from test_reprocessing_video import model, reducer_module, service_module, state

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.reducer = object.__new__(reducer_module.Reducer)
        self.reducer.recording_path = str(self.path)
        self.reducer.window_attrs = {"width":64,"height":48}
        self.events = [{'id':0,'timestamp':1}, {'id':1,'timestamp':2}]
        self.cache = self.path/'gpt_targets_cache.json'

    def test_valid_response(self):
        response = MagicMock(); response.json.return_value = {'results':[{'title':'test'}]}
        with patch('requests.post',return_value=response) as post:
            self.assertEqual(model.request_predictions('http://test',{},1),[{'title':'test'}])
            self.assertEqual(post.call_args.kwargs['timeout'],(10,60))

    def test_network_errors_are_safe_and_actionable(self):
        for error, message in [(requests.Timeout('private'),'timed out'),(requests.ConnectionError('private'),'connect')]:
            with self.subTest(error=type(error)),patch('requests.post',side_effect=error):
                with self.assertRaises(model.ModelPredictionError) as caught:model.request_predictions('http://test',{},1)
                self.assertIn(message,str(caught.exception));self.assertNotIn('private',str(caught.exception))
                self.assertIn('preserved',str(caught.exception))

    def test_http_status(self):
        response=MagicMock();response.status_code=503
        response.raise_for_status.side_effect=requests.HTTPError('private',response=response)
        with patch('requests.post',return_value=response):
            with self.assertRaisesRegex(model.ModelPredictionError,'HTTP 503'):model.request_predictions('http://test',{},1)

    def test_non_json_response(self):
        response=MagicMock();response.json.side_effect=ValueError('<html>private</html>')
        with patch('requests.post',return_value=response):
            with self.assertRaisesRegex(model.ModelPredictionError,'valid JSON'):model.request_predictions('http://test',{},1)

    def test_invalid_shapes(self):
        for body in ['html',[],{}, {'error':'private'}, {'status':'failed'}, {'results':[]}, {'results':[None]}, {'results':[{}]}, {'results':['html']}, {'results':[{'error':'private'}]}, {'results':[{'success':False}]}, {'results':[{'status':'failed'}]}, {'results':[{'message':'Internal server error'}]}, {'results':[{'title':None}]}, {'results':[{'description':12}]}]:
            response=MagicMock();response.json.return_value=body
            with self.subTest(body=body),patch('requests.post',return_value=response):
                with self.assertRaises(model.ModelPredictionError):model.request_predictions('http://test',{},1)

    def test_retry_reuses_success_cache(self):
        self.cache.write_text(json.dumps({'0:1':{'title':'existing'},'1:2':None}))
        with patch.object(reducer_module,'predict_targets',return_value=[{'title':'new'}]) as predict:
            result=self.reducer._predict_targets_cached(self.events)
        self.assertEqual(predict.call_args.args[1],[self.events[1]])
        self.assertEqual(result,[{'title':'existing'},{'title':'new'}])
        with patch.object(reducer_module,'predict_targets') as predict:
            self.assertEqual(self.reducer._predict_targets_cached(self.events),result);predict.assert_not_called()

    def test_invalid_response_not_cached(self):
        self.cache.write_text(json.dumps({'0:1':{'title':'existing'}}))
        with patch.object(reducer_module,'predict_targets',return_value=[None]):
            with self.assertRaises(model.ModelPredictionError):self.reducer._predict_targets_cached(self.events)
        self.assertEqual(json.loads(self.cache.read_text()),{'0:1':{'title':'existing'}})

    def test_invalid_old_cache_can_retry_after_failure(self):
        for cache in [[], {'0:1':None}, {'0:1':'bad'}, {'0:1':{'error':'bad'}}]:
            self.cache.write_text(json.dumps(cache))
            with patch.object(reducer_module,'predict_targets',side_effect=model.ModelPredictionError('failed')):
                with self.assertRaises(model.ModelPredictionError):self.reducer._predict_targets_cached(self.events)
            with patch.object(reducer_module,'predict_targets',return_value=[{'title':'a'},{'title':'b'}]):
                self.assertEqual(len(self.reducer._predict_targets_cached(self.events)),2)

    def test_target_text_schema(self):
        for value in [{'message':'Internal server error'}, {'title':None},
                      {'title':''}, {'role':'  '}, {'value':42},
                      {'title':['test']}, {'description':True},
                      {'title':'valid','role':{}}]:
            with self.subTest(value=value):self.assertFalse(model.is_valid_prediction(value))
        for key in model.TARGET_TEXT_FIELDS:
            self.assertTrue(model.is_valid_prediction({key:'usable target'}))
        self.assertTrue(model.is_valid_prediction({'title':None,'description':'usable'}))

    def test_old_error_message_cache_is_replaced(self):
        self.cache.write_text(json.dumps({'0:1':{'message':'Internal server error'}}))
        with patch.object(reducer_module,'predict_targets',return_value=[{'title':'a'},{'role':'button'}]) as predict:
            results=self.reducer._predict_targets_cached(self.events)
        self.assertEqual(predict.call_args.args[1],self.events)
        self.assertEqual(json.loads(self.cache.read_text()),{'0:1':results[0],'1:2':results[1]})

    def test_match_failure_is_not_swallowed(self):
        self.reducer.reduced_actions=[types.SimpleNamespace(action='click',axtree=None,target=None,start_time=1,description='test',coordinate={'x':1,'y':1})]
        with patch.object(self.reducer,'_predict_targets_cached',side_effect=model.ModelPredictionError('Model service unavailable')):
            with self.assertRaisesRegex(model.ModelPredictionError,'unavailable'):self.reducer.match_element()

    def test_pipeline_failure_stays_failed_at_match(self):
        r=self.reducer;r.event_buffer=[];r.flatten=False;r.generate_window_a11y=False;r.generate_element_a11y=True;r.reduced_actions=[]
        for name in ['compress','reduce_all','transform','finish']:setattr(r,name,MagicMock())
        r.match_element=MagicMock(side_effect=model.ModelPredictionError('Model timed out; original recording preserved. Retry.'))
        service=object.__new__(service_module.RecordingService);service.socketio=MagicMock();service._build_reducer=lambda *args:r
        raw=self.path/'events.jsonl';raw.write_text('raw input preserved')
        with patch.object(service_module,'RECORDING_DIR',str(self.path.parent)):service._process_recording(self.path.name)
        saved=state.read_state(self.path)
        self.assertEqual(saved['status'],'failed');self.assertEqual(saved['stage'],'match');self.assertEqual(saved['error_kind'],'model');self.assertIn('Model timed out',saved['error'])
        self.assertEqual(raw.read_text(),'raw input preserved');self.assertEqual(service.socketio.emit.call_args.args[1]['status'],'failed')

    def test_manual_retry_keeps_config_and_clip_fingerprint(self):
        import threading
        for name in ['events.jsonl', 'metadata.json', 'raw.mp4']:
            (self.path/name).write_text('test input')
        state.write_state(self.path, status='failed', stage='match', attempts=2,
                          error='Model timed out', clip_fingerprint='keep',
                          configs={'generate_element_a11y':True})
        service=object.__new__(service_module.RecordingService)
        service.recorder_thread=None;service._active_lock=threading.Lock();service._active_recordings=set()
        from queue import Queue
        service.reducer_queue=Queue()
        with patch.object(service_module,'RECORDING_DIR',str(self.path.parent)):
            status,_=service.reprocess_recording(self.path.name)
        saved=state.read_state(self.path)
        self.assertEqual(status,'succeed');self.assertEqual(saved['status'],'pending')
        self.assertIsNone(saved['error']);self.assertEqual(saved['attempts'],0)
        self.assertEqual(saved['clip_fingerprint'],'keep');self.assertTrue(saved['configs']['generate_element_a11y'])
        self.assertEqual(service.reducer_queue.get_nowait(),self.path.name)

    def test_disabled_model_does_not_block_local_pipeline(self):
        r=self.reducer;r.event_buffer=[];r.flatten=False;r.generate_window_a11y=False;r.generate_element_a11y=False;r.reduced_actions=[]
        for name in ['compress','reduce_all','transform','finish','complete_dump','vis_dump']:
            setattr(r,name,MagicMock())
        r.match_element=MagicMock(side_effect=AssertionError('Disabled model should not run'))
        (self.path/'metadata.json').write_text(json.dumps({'video_start_timestamp':0}))
        (self.path/'raw.mp4').write_bytes(b'test fixture')
        r.reduce_pipeline()
        r.match_element.assert_not_called()

if __name__=='__main__':unittest.main()
