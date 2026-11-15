import pytest

from contentmachine import whitelist as wl

YAML = """
campaigns:
  - id: cod-fr
    platform: whop
    rate_per_1k_usd: 3
    channels:
      - https://www.youtube.com/@ChaineAutorisee
  - id: old
    channels: [https://youtube.com/@Ancienne]
    active: false
"""


@pytest.fixture
def campaigns(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(YAML, encoding="utf-8")
    return wl.load(p)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@ChaineAutorisee",
    "https://youtube.com/@chaineautorisee/videos",
    "https://m.youtube.com/@ChaineAutorisee/",
])
def test_allowed(campaigns, url):
    assert wl.assert_allowed(url, campaigns).id == "cod-fr"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@ChaineAutoriseeFake",   # même préfixe, autre chaîne
    "https://www.youtube.com/@Ancienne/videos",        # campagne inactive
    "https://evil.com/youtube.com/@ChaineAutorisee",
    "https://www.youtube.com/watch?v=abc",
])
def test_refused(campaigns, url):
    with pytest.raises(wl.SourceNotAllowed):
        wl.assert_allowed(url, campaigns)


def test_duplicate_ids_rejected(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text("campaigns:\n  - {id: a, channels: []}\n  - {id: a, channels: []}\n")
    with pytest.raises(ValueError):
        wl.load(p)


def test_repo_whitelist_is_valid():
    wl.load()  # le fichier du repo doit toujours se charger
