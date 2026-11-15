import pytest

from contentmachine.qa import QAError, run_qa


def failed(report):
    return {c.name for c in report.failures}


def test_good_clip_passes(videos):
    r = run_qa(videos["good"], "clip")
    assert r.passed, r.summary()


@pytest.mark.parametrize("name,expected", [
    ("horizontal", {"format_vertical"}),
    ("no_audio", {"piste_audio"}),
    ("black_start", {"noir_au_debut", "noir_total"}),
    ("silence", {"silence"}),
    ("quiet", {"volume_lufs"}),
    ("too_short", {"duree"}),
    ("freeze", {"image_figee"}),
])
def test_each_defect_is_caught(videos, name, expected):
    r = run_qa(videos[name], "clip")
    assert not r.passed
    assert expected <= failed(r), r.summary()


def test_story_profile_requires_more_than_a_minute(videos):
    r = run_qa(videos["good"], "story")
    assert failed(r) == {"duree"}


def test_missing_file_raises(tmp_path):
    with pytest.raises(QAError):
        run_qa(tmp_path / "absent.mp4")


def test_unreadable_file_raises(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"pas une video")
    with pytest.raises(QAError):
        run_qa(bad)


def test_unknown_profile():
    with pytest.raises(ValueError):
        run_qa("x.mp4", "inconnu")
