import struct

import numpy as np
import pytest

from engine.speech_gate import OnlineSpeechGate
from engine.settings import EngineError


def pcm(samples, amplitude=1000):
    return struct.pack('<h', amplitude) * samples


class FakeSession:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.inputs = []

    def run(self, names, inputs):
        assert names is None
        self.inputs.append({name: value.copy() for name, value in inputs.items()})
        return np.array([[next(self.probabilities)]], dtype=np.float32), inputs['h'] + 1, inputs['c'] + 2


def test_20ms_packets_keep_512_sample_state_and_real_64_sample_context():
    session = FakeSession([.8, .2])
    gate = OnlineSpeechGate(session)
    assert gate(pcm(320, 1000)) is False
    assert session.inputs == []
    assert gate(pcm(320, 2000)) is True
    assert gate(pcm(320, 3000)) is True
    assert gate(pcm(64, 4000)) is False
    assert len(session.inputs) == 2
    first, second = session.inputs
    assert set(first) == {'input', 'h', 'c'}
    assert first['input'].shape == (1, 576)
    np.testing.assert_array_equal(first['input'][0, :64], np.zeros(64))
    np.testing.assert_array_equal(second['input'][0, :64], first['input'][0, -64:])
    np.testing.assert_array_equal(second['h'], np.ones((1, 1, 128)))
    np.testing.assert_array_equal(second['c'], np.full((1, 1, 128), 2))
    # Frame boundaries never zero, drop, or rearrange speech/music samples.
    delivered = np.concatenate([item['input'][0, 64:] for item in session.inputs])
    original = np.frombuffer(pcm(320,1000)+pcm(320,2000)+pcm(320,3000)+pcm(64,4000), dtype='<i2') / 32768
    np.testing.assert_array_equal(delivered, original)


def test_hysteresis_uses_enter_and_exit_thresholds():
    gate = OnlineSpeechGate(FakeSession([.49, .5, .4, .35, .34]))
    assert [gate(pcm(512)) for _ in range(5)] == [False, True, True, True, False]


def test_low_rms_is_immediately_quiet_even_without_a_new_model_frame():
    gate = OnlineSpeechGate(FakeSession([.9, .9]))
    assert gate(pcm(512))
    assert not gate(pcm(320, 0))
    assert len(gate.session.inputs) == 1
    assert not gate(pcm(320, 0))
    assert len(gate.session.inputs) == 2


def test_streams_share_session_but_not_recurrent_state_or_audio_context():
    session = FakeSession([.8, .8])
    one, two = OnlineSpeechGate(session), OnlineSpeechGate(session)
    one(pcm(512, 1000))
    two(pcm(512, 2000))
    np.testing.assert_array_equal(session.inputs[1]['h'], np.zeros((1,1,128)))
    np.testing.assert_array_equal(session.inputs[1]['input'][0,:64], np.zeros(64))
    assert one._pending is not two._pending


def test_reset_clears_pending_audio_probability_and_model_state():
    gate = OnlineSpeechGate(FakeSession([.9, .1]))
    gate(pcm(600))
    gate.reset()
    assert not gate.speaking and gate.probability == 0
    assert len(gate._pending) == 0
    gate(pcm(512))
    np.testing.assert_array_equal(gate.session.inputs[-1]['h'], np.zeros((1,1,128)))


@pytest.mark.parametrize('probability', [float('nan'), -1, 1.1])
def test_invalid_model_probability_fails_explicitly(probability):
    gate = OnlineSpeechGate(FakeSession([probability]))
    with pytest.raises(EngineError) as exc:
        gate(pcm(512))
    assert exc.value.code == 'speech_gate_failed'


@pytest.mark.parametrize('bad', [b'\x00', 'not pcm', pcm(16001)], ids=['odd','type','too-long'])
def test_invalid_pcm_cannot_accumulate(bad):
    gate = OnlineSpeechGate(FakeSession([]))
    with pytest.raises(ValueError):
        gate(bad)
    assert not gate._pending


def test_subframe_final_packet_is_consumed_without_padding_or_forcing_a_call():
    gate = OnlineSpeechGate(FakeSession([.8]))
    gate(pcm(480))
    assert not gate(pcm(16))
    assert gate(pcm(16))
    assert not gate._pending
