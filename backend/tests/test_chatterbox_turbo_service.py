from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.chatterbox_turbo_service.app as service


class ChatterboxTurboServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_generate_wave = service._generate_wave
        self._original_encode_wav = service._encode_wav

    def tearDown(self) -> None:
        service._generate_wave = self._original_generate_wave
        service._encode_wav = self._original_encode_wav

    def test_speech_endpoint_returns_raw_wav_audio(self) -> None:
        service._generate_wave = lambda request: ("fake-wave", 24000)
        service._encode_wav = lambda wav, sample_rate: b"RIFFfake-wav"

        response = TestClient(service.app).post(
            "/v1/audio/speech",
            json={
                "input": "Hey. I am here.",
                "response_format": "wav",
                "expression": "teasing",
                "paralinguistic_tags": ["[chuckle]"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.content, b"RIFFfake-wav")

    def test_speech_endpoint_rejects_non_wav_format(self) -> None:
        response = TestClient(service.app).post(
            "/v1/audio/speech",
            json={"input": "Hey. I am here.", "response_format": "mp3"},
        )

        self.assertEqual(response.status_code, 400)

    def test_encode_wav_writes_windows_pcm_audio(self) -> None:
        class FakeWave:
            ndim = 1

            def detach(self) -> "FakeWave":
                return self

            def cpu(self) -> "FakeWave":
                return self

            def unsqueeze(self, dim: int) -> "FakeWave":
                self.unsqueeze_dim = dim
                return self

        calls = {}

        def fake_save(buffer, wav, sample_rate, **kwargs):
            calls["sample_rate"] = sample_rate
            calls.update(kwargs)
            buffer.write(b"RIFFpcm")

        with patch.dict("sys.modules", {"torchaudio": type("FakeTorchaudio", (), {"save": fake_save})}):
            audio = service._encode_wav(FakeWave(), 24000)

        self.assertEqual(audio, b"RIFFpcm")
        self.assertEqual(calls["format"], "wav")
        self.assertEqual(calls["encoding"], "PCM_S")
        self.assertEqual(calls["bits_per_sample"], 16)
        self.assertEqual(calls["sample_rate"], 24000)


if __name__ == "__main__":
    unittest.main()
