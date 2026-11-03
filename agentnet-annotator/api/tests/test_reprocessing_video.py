"""Python 3.10+: python -m unittest discover -s api/tests -v (from agentnet-annotator).

Load the production modules with desktop/network dependencies isolated. OpenCV
is simulated to exercise codec and decode failures on either OS without a GUI.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

API = Path(__file__).resolve().parents[1]


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    return result


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, API / relative)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


# No actual desktop capture, model requests, or log-file initialization.
core = module('qa_core', __path__=[str(API / 'core')])
reduction = module('qa_core.action_reduction', __path__=[str(API / 'core/action_reduction')])
cv = module('cv2', CAP_PROP_FPS=1, CAP_PROP_FRAME_WIDTH=2,
            CAP_PROP_FRAME_HEIGHT=3, CAP_PROP_FRAME_COUNT=4,
            CAP_PROP_POS_FRAMES=5, VideoCapture=MagicMock(),
            VideoWriter=MagicMock(), VideoWriter_fourcc=lambda *args: 0)
utils = module('qa_core.utils')
for name in ('get_recordings_dir write_encrypted_jsonl read_encrypted_jsonl '
             'write_encrypt_line write_encrypted_json get_task_name_from_folder '
             'get_description_from_folder get_video_by_id read_encrypted_json '
             'check_recording_visualizable check_recording_broken').split():
    setattr(utils, name, MagicMock())
utils.RECORDING_DIR = ''
utils.REVIEW_RECORDING_DIR = ''
log = module('qa_core.logger', logger=MagicMock())
with patch.dict(sys.modules, {
    'qa_core': core, 'qa_core.action_reduction': reduction,
    'qa_core.logger': log, 'qa_core.utils': utils,
    'qa_core.ai_assistant': module('qa_core.ai_assistant', predict_targets=MagicMock()),
    'qa_core.a11y': module('qa_core.a11y', parse_element=MagicMock()),
    'cv2': cv, 'numpy': module('numpy'),
}):
    model = load('qa_core.model_prediction', 'core/model_prediction.py')
    state = load('qa_core.processing_state', 'core/processing_state.py')
    validation = load('qa_core.video_validation', 'core/video_validation.py')
    action_module = load('qa_core.action_reduction.action', 'core/action_reduction/action.py')
    # Platform DLL loading is outside these codec/queue tests.
    with patch('platform.system', return_value='Linux'):
        reducer_module = load('qa_core.action_reduction.reducer', 'core/action_reduction/reducer.py')
    with patch.dict(sys.modules, {
        'pyautogui': module('pyautogui'), 'core': core,
        'core.logger': log, 'core.utils': utils,
        'core.processing_state': state,
        'core.recorder': module('core.recorder', Recorder=MagicMock()),
        'core.action_reduction': module('core.action_reduction', Reducer=reducer_module.Reducer),
        'core.backend_func': module('core.backend_func', read_recording_status=MagicMock()),
        'core.constants': module('core.constants', SUCCEED='succeed', FAILED='failed'),
    }):
        service_module = load('qa_service', 'services/recording_service.py')


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='录像 test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'source')
        self.frame = types.SimpleNamespace(size=12)
        cv.VideoCapture.reset_mock(side_effect=True)
        cv.VideoWriter.reset_mock(side_effect=True)

    def capture(self, frames=3, declared=3, opened=True):
        cap = MagicMock()
        cap.isOpened.return_value = opened
        cap.get.side_effect = lambda prop: {1: 30, 2: 2, 3: 2, 4: declared}.get(prop, 0)
        cap.read.side_effect = [(True, self.frame)] * frames + [(False, None)]
        return cap

    def action(self):
        a = object.__new__(action_module.Action)
        a.id, a.action = 0, 'click'
        a._get_video_start_time = lambda: 0
        a._get_video_end_time = lambda: .1
        a.process_start_end_time = lambda start, end: (start, end)
        return a

    def writer(self, opened=True):
        writer = MagicMock()
        writer.isOpened.return_value = opened
        def make(path, *args):
            Path(path).write_bytes(b'encoded' if opened else b'')
            return writer
        cv.VideoWriter.side_effect = make
        return writer

    def test_empty_file_rejected_without_decoder(self):
        self.source.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'empty'):
            validation.validate_video(self.source)
        cv.VideoCapture.assert_not_called()

    def test_nonempty_unopenable_file_rejected_and_released(self):
        cap = self.capture(opened=False)
        cv.VideoCapture.return_value = cap
        with self.assertRaisesRegex(ValueError, 'Cannot open'):
            validation.validate_video(self.source)
        cap.release.assert_called_once()

    def test_no_decodable_frames_rejected(self):
        cv.VideoCapture.return_value = self.capture(frames=0)
        with self.assertRaisesRegex(ValueError, 'no decodable'):
            validation.validate_video(self.source)

    def test_truncated_video_rejected(self):
        cv.VideoCapture.return_value = self.capture(frames=1, declared=3)
        with self.assertRaisesRegex(ValueError, 'truncated'):
            validation.validate_video(self.source)

    def test_wrong_generated_frame_count_rejected(self):
        cv.VideoCapture.return_value = self.capture(frames=2, declared=2)
        with self.assertRaisesRegex(ValueError, 'expected 3'):
            validation.validate_video(self.source, expected_frames=3)

    def test_encoder_failure_does_not_publish_or_leave_temp(self):
        cap = self.capture()
        cv.VideoCapture.return_value = cap
        writer = self.writer(opened=False)
        with self.assertRaisesRegex(RuntimeError, 'avc1'):
            self.action().to_video(str(self.root), {'video_start_time': 0}, {})
        self.assertEqual(list((self.root / 'video_clips').iterdir()), [])
        cap.release.assert_called()
        writer.release.assert_called()

    def test_invalid_range_fails(self):
        a = self.action()
        a._get_video_end_time = lambda: -1
        with self.assertRaisesRegex(ValueError, 'Invalid time range'):
            a.to_video(str(self.root), {'video_start_time': 0}, {})

    def test_output_outside_source_fails(self):
        cv.VideoCapture.return_value = self.capture()
        a = self.action()
        a._get_video_start_time = lambda: 10
        a._get_video_end_time = lambda: 11
        with self.assertRaisesRegex(ValueError, 'outside'):
            a.to_video(str(self.root), {'video_start_time': 0}, {})
        cv.VideoWriter.assert_not_called()

    def test_success_validates_then_publishes_and_keeps_shared_metadata(self):
        cv.VideoCapture.side_effect = [self.capture(), self.capture()]
        self.writer()
        attrs = {'video_start_time': 0}
        self.action().to_video(str(self.root), attrs, {})
        self.assertTrue((self.root / 'video_clips/0_click.mp4').is_file())
        self.assertFalse((self.root / 'video_clips/0_click.part.mp4').exists())
        self.assertEqual(attrs, {'video_start_time': 0})

    def test_truncated_output_not_published(self):
        cv.VideoCapture.side_effect = [self.capture(), self.capture(frames=1)]
        self.writer()
        with self.assertRaisesRegex(ValueError, 'truncated'):
            self.action().to_video(str(self.root), {'video_start_time': 0}, {})
        self.assertEqual(list((self.root / 'video_clips').iterdir()), [])

    def test_retry_reuses_good_clip_and_regenerates_corrupt_clip(self):
        clips = self.root / 'video_clips'
        clips.mkdir()
        good, bad = clips / '0_click.mp4', clips / '1_click.mp4'
        good.write_bytes(b'good'); bad.write_bytes(b'corrupt')
        cv.VideoCapture.side_effect = [self.capture(), self.capture(), self.capture(opened=False), self.capture(), self.capture()]
        self.writer()
        replacement = self.action()
        replacement.id = 1
        replacement.to_video = MagicMock(wraps=replacement.to_video)
        good_action = self.action()
        good_action.to_video = MagicMock(wraps=good_action.to_video)
        actions = [good_action, replacement]
        reducer = object.__new__(reducer_module.Reducer)
        reducer.reduced_actions = actions
        reducer.process_actions_multithreaded(str(self.root), {'video_start_time': 0}, {})
        actions[0].to_video.assert_not_called()
        actions[1].to_video.assert_called_once()
        self.assertEqual(good.read_bytes(), b'good')
        self.assertEqual(bad.read_bytes(), b'encoded')

    def test_retry_regenerates_playable_clip_with_wrong_length(self):
        # A legacy encoder can finish a valid MP4 after a short source read.
        # Its own header agrees with its contents, so decode-only checks pass.
        for old_frames in (1, 4):
            with self.subTest(old_frames=old_frames):
                clip = self.root / 'video_clips/0_click.mp4'
                clip.parent.mkdir(exist_ok=True)
                clip.write_bytes(b'playable but wrong length')
                source = self.capture()
                cv.VideoCapture.side_effect = [
                    source, self.capture(frames=old_frames, declared=old_frames),
                    self.capture(), self.capture(),
                ]
                self.writer()
                action = self.action()
                action.to_video = MagicMock(wraps=action.to_video)
                reducer = object.__new__(reducer_module.Reducer)
                reducer.reduced_actions = [action]
                reducer.process_actions_multithreaded(
                    str(self.root), {'video_start_time': 0}, {},
                )
                action.to_video.assert_called_once()
                self.assertEqual(clip.read_bytes(), b'encoded')
                source.release.assert_called_once()

    def test_expected_length_includes_buffer_and_clamps_to_source(self):
        action = self.action()
        # Use the real short-action padding rather than the test identity stub.
        action.process_start_end_time = action_module.Action.process_start_end_time.__get__(action)
        self.assertEqual(action.expected_video_frames({
            'video_start_time': 0, 'fps': 30, 'total_frames': 30,
        }), 6)  # -0.3 .. 0.2 seconds, clamped to frame zero
        action._get_video_start_time = lambda: .95
        action._get_video_end_time = lambda: 1.1
        self.assertEqual(action.expected_video_frames({
            'video_start_time': 0, 'fps': 30, 'total_frames': 30,
        }), 11)  # padded end exceeds the source and is clamped to 30

    def test_expected_length_supports_fractional_fps_and_video_offset(self):
        action = self.action()
        action._get_video_start_time = lambda: 101
        action._get_video_end_time = lambda: 102
        self.assertEqual(action.expected_video_frames({
            'video_start_time': 100, 'fps': 29.97, 'total_frames': 300,
        }), 30)

    def test_worker_error_contains_action_and_root_cause(self):
        reducer = object.__new__(reducer_module.Reducer)
        reducer.reduced_actions = [types.SimpleNamespace(id=7, action='click', to_video=MagicMock(side_effect=RuntimeError('encoder unavailable')))]
        with self.assertRaisesRegex(RuntimeError, 'action 7: encoder unavailable'):
            reducer.process_actions_multithreaded(str(self.root), {}, {})

    def service(self):
        s = object.__new__(service_module.RecordingService)
        s.socketio = MagicMock()
        s._active_lock = threading.Lock()
        s._active_recordings = set()
        s.recorder_thread = None
        return s

    def test_worker_failure_persists_failed_and_emits_reason(self):
        s = self.service()
        s._build_reducer = MagicMock()
        s._build_reducer.return_value.reduce_pipeline.side_effect = RuntimeError('encoder unavailable')
        with patch.object(service_module, 'RECORDING_DIR', str(self.root.parent)):
            s._process_recording(self.root.name)
        self.assertEqual(state.read_state(self.root)['status'], 'failed')
        self.assertIn('encoder unavailable', state.read_state(self.root)['error'])
        self.assertIn('encoder unavailable', s.socketio.emit.call_args.args[1]['message'])

    def test_failed_reason_exposed_in_recording_list(self):
        state.write_state(self.root, status='failed', error='encoder unavailable')
        s = self.service()
        s._get_processing_status = lambda name: 'failed'
        s._get_recording_path = lambda name, reviewing: str(self.root)
        info = s._update_recording_info({}, self.root.name, False)
        self.assertEqual(info['processing_error'], 'encoder unavailable')

    def test_success_sets_done_only_after_pipeline_returns(self):
        s = self.service()
        s._build_reducer = MagicMock()
        with patch.object(service_module, 'RECORDING_DIR', str(self.root.parent)):
            s._process_recording(self.root.name)
        self.assertEqual(state.read_state(self.root)['status'], 'done')
        self.assertEqual(s.socketio.emit.call_args.args[1]['status'], 'succeed')


if __name__ == '__main__':
    unittest.main()
