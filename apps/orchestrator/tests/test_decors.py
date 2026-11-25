"""v0.4 : décors IA, débit de voix ajusté, déplacements et nouvelles poses."""
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from contentmachine import images
from contentmachine.images import DecorError, FalDecor, decor_prompt, make_decors
from contentmachine.pipeline import FakeVoice, StoryTooLong, change_tempo, make_story, tempo_for
from contentmachine.render import actor_x, pose_angles, render_video, scenes_with_outro, wav_duration
from contentmachine.storyboard import MAX_WORDS, MIN_WORDS, validate, word_count

FIX = Path(__file__).parent / "fixtures" / "loto_v2.json"


@pytest.fixture
def story():
    return json.loads(FIX.read_text(encoding="utf-8"))


class FakeDecor:
    """Décors factices : un aplat de couleur par scène, sans réseau. `fail` = indices qui échouent."""
    unit_price = 0.003

    def __init__(self, fail=()):
        self.fail, self.generated, self.prompts = set(fail), 0, []

    def generate(self, prompt, out_png):
        self.prompts.append(prompt)
        if len(self.prompts) - 1 in self.fail:
            raise DecorError("panne simulée")
        Image.new("RGB", (720, 1280), (120 + len(self.prompts) * 9 % 120, 190, 230)).save(out_png)
        self.generated += 1
        return out_png


# --- storyboard v0.4 ----------------------------------------------------------------

def test_v2_fixture_is_valid(story):
    validate(story)
    assert MIN_WORDS <= word_count(story) <= MAX_WORDS
    assert len(story["scenes"]) >= 10


def test_decor_prompt_locks_style_and_forbids_people(story):
    p = decor_prompt(story["scenes"][0])
    assert p.startswith(images.STYLE) and "no people" in p and "no text" in p
    assert "Night time" in decor_prompt(story["scenes"][9])
    assert decor_prompt({"background": "room"}) is None


# --- durée de la voix ------------------------------------------------------------------

def test_tempo_rules():
    assert tempo_for(75) == 1.0
    assert tempo_for(95) == pytest.approx(95 / 85)
    assert tempo_for(50) == pytest.approx(0.92)
    with pytest.raises(StoryTooLong):
        tempo_for(110)


def test_change_tempo_shortens_audio(tmp_path):
    wav = FakeVoice().speak(" ".join(["mot"] * 26), tmp_path / "v.wav")   # 10 s
    fast = change_tempo(wav, 1.2)
    assert wav_duration(fast) == pytest.approx(wav_duration(wav) / 1.2, abs=0.15)


def test_long_voice_is_sped_up_instead_of_rejected(story, tmp_path):
    class SlowVoice(FakeVoice):           # ~1,7 mot/s, comme la voix Gemini « conteur »
        def speak(self, text, out_wav):
            out = super().speak(text, out_wav)
            return change_tempo(out, 0.65).replace(out)

    story["scenes"] = story["scenes"] + [dict(story["scenes"][5], id=13)]
    r = make_story(None, SlowVoice(), storyboard=story, out_dir=tmp_path)
    assert r.tempo > 1.0
    assert r.qa.passed, r.qa.summary()


# --- décors ----------------------------------------------------------------------------

def test_make_decors_survives_failures(story, tmp_path):
    scenes = scenes_with_outro(story)
    fake = FakeDecor(fail={2})
    out = make_decors(scenes, fake, tmp_path, workers=1)
    assert len(out) == len(story["scenes"]) - 1        # l'outro n'a pas de décor, la scène 3 a échoué
    assert 2 not in out and all(p.is_file() for p in out.values())
    assert make_decors(scenes, None, tmp_path) == {}


def _png_bytes():
    import io
    buf = io.BytesIO()
    Image.new("RGB", (720, 1280), (200, 220, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_fal_client_calls_api_and_caches(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.host == "fal.run":
            assert req.headers["Authorization"] == "Key secret-test"
            body = json.loads(req.content)
            assert body["image_size"] == {"width": 720, "height": 1280}
            return httpx.Response(200, json={"images": [{"url": "https://cdn.fal.test/a.png"}]})
        return httpx.Response(200, content=_png_bytes())

    fal = FalDecor("secret-test", httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path / "c")
    out = fal.generate("a kitchen", tmp_path / "d.png")
    assert Image.open(out).size == (720, 1280) and fal.generated == 1
    fal.generate("a kitchen", tmp_path / "d2.png")          # même prompt : servi par le cache
    assert len(seen) == 2 and fal.generated == 1


def test_fal_error_never_leaks_key(tmp_path):
    fal = FalDecor("secret-test", httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"detail": "bad key secret-test"}))), cache_dir=tmp_path)
    with pytest.raises(DecorError) as exc:
        fal.generate("x" * 20, tmp_path / "d.png")
    assert "secret-test" not in str(exc.value)


def test_fal_requires_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(DecorError):
        FalDecor()


# --- animation -------------------------------------------------------------------------

def test_actor_moves_and_turns():
    a = {"character": "toi", "pose": "run", "x": 0.8, "to_x": 0.2}
    x0, f0 = actor_x(a, 0, 4)
    x1, f1 = actor_x(a, 4, 4)
    assert x0 == pytest.approx(0.8) and x1 == pytest.approx(0.2) and f0 == f1 == "left"


@pytest.mark.parametrize("pose", ["dance", "talk", "surprised", "angry"])
def test_new_poses_animate(pose):
    a, b = pose_angles(pose, 0.1), pose_angles(pose, 0.4)
    assert a != b


def test_render_with_ai_decors(story, tmp_path):
    voice = FakeVoice()
    s = dict(story, scenes=story["scenes"][:3])
    audios = []
    from contentmachine.render import SceneAudio
    for i in range(4):
        wav = voice.speak("un deux trois quatre", tmp_path / f"{i}.wav")
        audios.append(SceneAudio(wav, wav_duration(wav)))
    decors = make_decors(scenes_with_outro(s), FakeDecor(), tmp_path)
    out = render_video(s, audios, tmp_path / "decor.mp4", tmp_path, min_total=0, decors=decors)
    assert out.stat().st_size > 10_000


def test_full_story_with_decors_passes_qa(story, tmp_path):
    fake = FakeDecor()
    r = make_story(None, FakeVoice(), storyboard=story, out_dir=tmp_path, decor=fake)
    assert r.qa.passed, r.qa.summary()
    assert r.decors == len(story["scenes"]) and r.cost_usd == pytest.approx(0.003 * r.decors)
