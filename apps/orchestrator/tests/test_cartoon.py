"""v0.5 : histoire en 2 parties, voix d'un bloc, images et animations fal (simulées), montage."""
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image

from contentmachine import cartoon, fal as falmod, story2
from contentmachine.cartoon import Overlay, make_two_parts, time_shots
from contentmachine.fal import FalClient, FalError, frames_for, video_cost
from contentmachine.pipeline import FakeVoice
from contentmachine.qa import run_qa
from contentmachine.story2 import StoryError, check_scenes, clean_oral, norm, plan_scenes, write_story

STORY = json.loads((Path(__file__).parent / "fixtures" / "mensonge_story.json").read_text(encoding="utf-8"))


def split_plan(text: str) -> list[dict]:
    """Découpage « idéal » qu'un bon LLM renverrait : 1 à 2 phrases par plan, texte intact."""
    sentences = re.findall(r"[^.!?…]+[.!?…]+", text)
    scenes, cur = [], ""
    for s in sentences:
        cur = f"{cur} {s.strip()}".strip()
        if len(cur.split()) >= 8:
            scenes.append(cur)
            cur = ""
    if cur:
        scenes[-1] += " " + cur
    return [{"text": t, "image": f"scene {i}", "motion": "the character jumps"} for i, t in enumerate(scenes)]


class FakeLLM:
    def __init__(self, story=None, bad_first=False):
        self.story, self.bad_first, self.calls = story or STORY, bad_first, []

    def generate_json(self, prompt):
        self.calls.append(prompt)
        if "Découpe ce texte" in prompt:
            part = 1 if "(partie 1)" in prompt else 2
            plan = split_plan(self.story[f"part{part}"])
            if self.bad_first and len([c for c in self.calls if "Découpe" in c]) == 1:
                plan[0]["text"] = "Texte reformulé par erreur."
            return {"scenes": plan}
        return dict(self.story)


class FakeFal:
    def __init__(self, fail_images=(), fail_videos=()):
        self.fail_images, self.fail_videos = set(fail_images), set(fail_videos)
        self.n, self.spent, self.submitted = 0, 0.0, []

    def image(self, prompt, out, seed=None):
        self.n += 1
        if self.n in self.fail_images:
            raise FalError("panne simulée")
        Image.new("RGB", (576, 1024), (40 + self.n * 13 % 200, 150, 220)).save(out)
        self.spent += 0.003
        return out, f"https://fal.test/{out.name}"

    def submit_video(self, url, prompt, frames):
        self.submitted.append((url, prompt, frames))
        return {"frames": frames, "url": url}

    def poll_videos(self, jobs, out_dir, timeout_s=0):
        done = {}
        for k, job in jobs.items():
            if k in self.fail_videos:
                continue
            out = out_dir / f"anim_{k}.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"testsrc2=s=576x1024:r=24:d={job['frames'] / 24:.3f}", "-pix_fmt", "yuv420p",
                            str(out)], check=True)
            done[k] = out
            self.spent += video_cost(job["frames"])
        return done


# --- écriture ---------------------------------------------------------------------------

def test_fixture_story_respects_rules():
    assert story2.check_story(STORY) == []


def test_write_story_retries_with_errors():
    short = dict(STORY, part1="Et si demain, chaque mensonge te rapportait cent euros ? Fin.")

    class Llm:
        n = 0

        def generate_json(self, prompt):
            Llm.n += 1
            if Llm.n == 2:
                assert "REFUSÉE" in prompt and "part1" in prompt
            return dict(short if Llm.n == 1 else STORY)

    st = write_story(Llm(), "mensonges")
    assert st["title"] == STORY["title"] and Llm.n == 2


def test_write_story_gives_up():
    class Llm:
        def generate_json(self, prompt):
            return {"title": "x"}

    with pytest.raises(StoryError):
        write_story(Llm(), None, attempts=2)


def test_clean_oral_removes_what_voice_would_read():
    assert clean_oral("Partie 1 : Tu ris (rire) *fort* 😂 !") == "Tu ris fort !"


def test_plan_must_keep_text_verbatim():
    llm = FakeLLM(bad_first=True)
    scenes = plan_scenes(llm, STORY, 1)
    assert norm(" ".join(s["text"] for s in scenes)) == norm(STORY["part1"])
    assert sum("REFUSÉE" in c for c in llm.calls) == 1
    errs = check_scenes([{"text": "autre chose", "image": "x", "motion": "y"}], STORY["part1"])
    assert any("mot pour mot" in e for e in errs)


def test_prompts_lock_style_and_mascot():
    p = story2.image_prompt({"image": "an office"}, STORY)
    assert story2.STYLE in p and "orange cap" in p and "blue tie" in p
    assert "Keep the flat cartoon style" in story2.motion_prompt({"motion": "he jumps"})


# --- calage ----------------------------------------------------------------------------

def test_cuts_snap_to_pauses():
    texts = ["a" * 10, "b" * 10, "c" * 10]
    sil = [(0.0, 0.3), (3.6, 3.9), (6.9, 7.2), (9.8, 10.0)]
    bounds = time_shots(texts, 10.0, sil)
    assert bounds[0][0] == 0 and bounds[-1][1] == 10.0
    assert bounds[0][1] == pytest.approx(3.75) and bounds[1][1] == pytest.approx(7.05)


def test_cuts_without_pauses_are_proportional():
    bounds = time_shots(["aa", "aa"], 4.0, [])
    assert bounds == [(0.0, 2.0), (2.0, 4.0)]


# --- fal --------------------------------------------------------------------------------

def test_frames_and_cost():
    assert frames_for(5.0) == 121 and frames_for(9.0) == 121 and frames_for(2.0) == 49
    assert video_cost(121) == pytest.approx(576 * 1024 * 121 / 1e6 * 0.001205)


def _png():
    import io
    b = io.BytesIO()
    Image.new("RGB", (576, 1024)).save(b, "PNG")
    return b.getvalue()


def test_fal_image_and_queue(tmp_path):
    calls = {"status": 0}

    def handler(req: httpx.Request):
        u = str(req.url)
        if "cdn.test" in u:
            assert "Authorization" not in req.headers       # la clé ne part jamais vers le CDN
        else:
            assert req.headers["Authorization"] == "Key k-test"
        if u.startswith("https://fal.run/"):
            assert json.loads(req.content)["image_size"] == {"width": 576, "height": 1024}
            return httpx.Response(200, json={"images": [{"url": "https://cdn.test/i.png"}]})
        if u == "https://cdn.test/i.png":
            return httpx.Response(200, content=_png())
        if u.startswith("https://queue.fal.run/"):
            body = json.loads(req.content)
            assert body["generate_audio"] is False and body["num_frames"] == 121
            return httpx.Response(200, json={"request_id": "r1", "status_url": "https://q.test/s",
                                             "response_url": "https://q.test/r"})
        if u == "https://q.test/s":
            calls["status"] += 1
            return httpx.Response(200, json={"status": "IN_QUEUE" if calls["status"] == 1 else "COMPLETED"})
        if u == "https://q.test/r":
            return httpx.Response(200, json={"video": {"url": "https://cdn.test/v.mp4"}})
        if u == "https://cdn.test/v.mp4":
            return httpx.Response(200, content=b"fake-mp4")
        return httpx.Response(404)

    client = FalClient("k-test", httpx.Client(transport=httpx.MockTransport(handler)))
    client.poll_s = 0
    path, url = client.image("a cat", tmp_path / "i.png", seed=3)
    assert path.is_file() and url == "https://cdn.test/i.png"
    job = client.submit_video(url, "move", 121)
    done = client.poll_videos({"a": job}, tmp_path)
    assert done["a"].read_bytes() == b"fake-mp4" and calls["status"] == 2
    assert client.spent == pytest.approx(falmod.IMAGE_PRICE + video_cost(121))


def test_fal_errors_hide_key(tmp_path):
    client = FalClient("k-secret", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(FalError) as exc:
        client.image("x", tmp_path / "i.png")
    assert "k-secret" not in str(exc.value)


# --- budget -----------------------------------------------------------------------------

def test_budget_limits_animated_shots(monkeypatch):
    monkeypatch.setenv("MAX_COST_PER_PART", "0.2")
    shots = [[cartoon.Shot("t", "i", "m", 0, 5, image_url="u") for _ in range(5)]]
    assert len(cartoon.plan_animation(shots)) == 2        # 2 x 0,086 $ <= 0,20 $
    monkeypatch.setenv("ANIMATION", "off")
    assert cartoon.plan_animation(shots) == []


# --- de bout en bout ----------------------------------------------------------------------

def test_two_parts_end_to_end(tmp_path):
    fal = FakeFal(fail_images={3}, fail_videos={"1_01"})
    res = make_two_parts(FakeLLM(), FakeVoice(), fal, "mensonges", out_dir=tmp_path)
    assert [p.part for p in res.parts] == [1, 2]
    for p in res.parts:
        assert p.qa.passed, p.qa.summary()
        assert p.shots >= story2.MIN_SCENES
    assert res.parts[0].animated == res.parts[0].shots - 1     # un plan non animé -> zoom de repli
    assert res.cost_usd == pytest.approx(fal.spent) and res.cost_usd > 0
    assert all("Keep the flat cartoon style" in prompt for _, prompt, _ in fal.submitted)


def test_overlay_draws_hook_and_part_banner():
    from contentmachine.render import Word
    ov = Overlay(1, STORY["hook"], [Word("Et", 0.0, 0.4), Word("si", 0.4, 0.8)], 70.0)
    a = Image.new("RGB", (720, 1280), (90, 160, 220))
    ov.draw(a, 0.2)
    b = Image.new("RGB", (720, 1280), (90, 160, 220))
    ov.draw(b, 40.0)
    assert a.tobytes() != b.tobytes()                 # hook visible au début seulement
