import base64
import copy
import json
import wave
from pathlib import Path

import httpx
import pytest

from contentmachine import api, gemini
from contentmachine.gemini import GeminiClient, GeminiError
from contentmachine.pipeline import FakeVoice, make_story
from contentmachine.render import SceneAudio, chunk_words, render_video, time_words, wav_duration
from contentmachine.scriptgen import build_prompt, write_story
from contentmachine.storyboard import MIN_WORDS, StoryboardError, validate, word_count

FIX = Path(__file__).parent / "fixtures" / "loto.json"


@pytest.fixture
def story():
    return json.loads(FIX.read_text(encoding="utf-8"))


# --- storyboard ---------------------------------------------------------------

def test_fixture_is_valid(story):
    assert validate(story) is story
    assert MIN_WORDS <= word_count(story)


def test_unknown_character(story):
    story["scenes"][0]["actors"][0]["character"] = "fantome"
    with pytest.raises(StoryboardError, match="fantome"):
        validate(story)


def test_schema_error_is_readable(story):
    story["scenes"][0]["actors"][0]["pose"] = "moonwalk"
    with pytest.raises(StoryboardError) as exc:
        validate(story)
    assert "pose" in str(exc.value)


def test_too_short(story):
    story["scenes"] = story["scenes"][:4]
    with pytest.raises(StoryboardError, match="mots"):
        validate(story)


# --- écriture du scénario par le LLM -----------------------------------------------

class FakeLLM:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def generate_json(self, prompt, temperature=1.0):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_retries_with_errors_then_succeeds(story):
    bad = copy.deepcopy(story)
    bad["scenes"][1]["actors"][0]["pose"] = "moonwalk"
    llm = FakeLLM([bad, story])
    assert write_story(llm, "loto")["title"] == story["title"]
    assert "INVALIDE" in llm.prompts[1] and "pose" in llm.prompts[1]


def test_gives_up_after_attempts(story):
    bad = copy.deepcopy(story)
    bad["hook"] = "court"
    with pytest.raises(StoryboardError):
        write_story(FakeLLM([bad, bad, bad]), "x")


def test_repairs_small_mistakes(story):
    s = copy.deepcopy(story)
    s["story_id"] = "Et si tu gagnais 100 Millions !"
    s["scenes"][0]["id"] = 7
    s["scenes"][0]["actors"][0]["x"] = 1.4
    s["scenes"][0]["props"] = ["money", "licorne"]
    out = write_story(FakeLLM([s]))
    assert out["story_id"] == "et-si-tu-gagnais-100-millions"
    assert out["scenes"][0]["id"] == 1 and out["scenes"][0]["actors"][0]["x"] == 0.9
    assert out["scenes"][0]["props"] == ["money"]


def test_prompt_mentions_subject_and_avoid_list():
    p = build_prompt("et si les chats parlaient", avoid=["Loto"])
    assert "et si les chats parlaient" in p and "Loto" in p and str(MIN_WORDS) in p


# --- client Gemini ----------------------------------------------------------------

def gemini_client(handler, key="cle-secrete-123"):
    return GeminiClient(api_key=key, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_generate_json_strips_fences():
    def h(req):
        assert req.headers["x-goog-api-key"] == "cle-secrete-123"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '```json\n{"a": 1}\n```'}]}}]})
    assert gemini_client(h).generate_json("p") == {"a": 1}


def test_error_message_never_contains_key():
    def h(req):
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})
    with pytest.raises(GeminiError) as exc:
        gemini_client(h).generate_json("p")
    assert "cle-secrete-123" not in str(exc.value)


def test_speak_falls_back_to_second_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_TTS_MODEL", "modele-principal")
    pcm = (b"\x00\x10" * 24000)  # 1 s de PCM 16 bits à 24 kHz
    seen = []

    def h(req):
        seen.append(req.url.path)
        if "modele-principal" in req.url.path:
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    out = gemini_client(h).speak("Bonjour", tmp_path / "v.wav")
    assert len(seen) == 2 and gemini.tts_models()[1] in seen[1]
    with wave.open(str(out)) as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
    assert abs(wav_duration(out) - 1.0) < 0.05


# --- sous-titres ------------------------------------------------------------------

def test_word_timing_is_ordered_and_fits():
    words = time_words("Et si demain tu gagnais cent millions ? Incroyable.", 4.0)
    assert words[0].start >= 0 and words[-1].end <= 4.0
    assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))
    chunks = chunk_words(words)
    assert sum(len(c) for c in chunks) == len(words)
    assert all(len(" ".join(w.text for w in c)) <= 26 or len(c) == 1 for c in chunks)


# --- rendu ------------------------------------------------------------------------

def test_render_short_video(story, tmp_path):
    voice = FakeVoice()
    scenes = story["scenes"][:3]
    s = dict(story, scenes=scenes)
    audios = []
    for i in range(len(scenes) + 1):
        wav = voice.speak("un deux trois quatre", tmp_path / f"{i}.wav")
        audios.append(SceneAudio(wav, wav_duration(wav)))
    out = render_video(s, audios, tmp_path / "court.mp4", tmp_path, min_total=0)
    assert out.stat().st_size > 10_000


def test_full_story_passes_quality_control(story, tmp_path):
    """Bout en bout avec voix factice : la vidéo produite doit passer le contrôle qualité « story »."""
    r = make_story(None, FakeVoice(), storyboard=story, out_dir=tmp_path)
    assert r.qa.passed, r.qa.summary()


# --- commande /histoire --------------------------------------------------------------

def test_histoire_command_parses_subject(monkeypatch):
    from fastapi.testclient import TestClient

    from contentmachine.telegram import webhook_secret
    from tests_helpers import FakeTelegram, TOKEN, CHAT

    fake = FakeTelegram()
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setattr(api, "_client", fake.client())
    monkeypatch.setattr(api, "send_story_review", lambda chat, subject: calls.append((chat, subject)))
    with TestClient(api.app) as c:
        c.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret(TOKEN)},
               json={"message": {"text": "/histoire et si les chats parlaient", "chat": {"id": int(CHAT)}}})
    assert calls == [(int(CHAT), "et si les chats parlaient")]
    assert fake.methods() == ["sendMessage"]
