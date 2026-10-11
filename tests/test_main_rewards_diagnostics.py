import json
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from naruto_daily.common import main_screen


ROOT = Path(__file__).parents[1]
ARCHIVE = Path('.data/diagnostics/main-screen/rejected-rewards.zip')
MODEL = dict(base_url='https://example.test/v1', model='vision', timeout_seconds=30,
             max_calls=2, api_key='private-config-key')
TARGET = dict(name='奖励', main_menu='rewards', multimodal=MODEL)


@pytest.fixture
def asset_root(tmp_path):
    (tmp_path / 'assets').mkdir()
    for name in ('main_reward_label.png', 'main_reward_label_red_dot.png'):
        copyfile(ROOT / 'assets' / name, tmp_path / 'assets' / name)
    return tmp_path


def make_recognizer(monkeypatch, asset_root, response, requests):
    def complete(png, prompt, **kwargs):
        requests.append((png, prompt))
        return response
    monkeypatch.setattr(main_screen, 'MultimodalClient',
                        lambda *args, **kwargs: SimpleNamespace(complete_json=complete))
    return main_screen.MainScreenRecognition(MODEL, asset_root)


def raw_frame():
    return (ROOT / 'tests/fixtures/main_rewards_right_hud.png').read_bytes()


def result(candidate=6, **kwargs):
    return dict(page='main', target='rewards', found=True, candidate_id=candidate, **kwargs)


def test_rejected_reward_archive_contains_exact_frame_request_response_and_metadata(monkeypatch, asset_root, caplog):
    response, requests = result(explanation='实际选择的编号'), []
    recognizer = make_recognizer(monkeypatch, asset_root, response, requests)
    png = raw_frame()
    with caplog.at_level('INFO', logger='naruto_daily'):
        center, score = recognizer.match(png, TARGET)
    assert center is None
    assert len(requests) == 1
    with ZipFile(asset_root / ARCHIVE) as archive:
        assert set(archive.namelist()) == {'input.png', 'candidates.png', 'prompt.txt', 'result.json', 'metadata.json'}
        assert archive.read('input.png') == png
        assert archive.read('candidates.png') == requests[0][0]
        assert archive.read('prompt.txt').decode('utf-8') == requests[0][1]
        assert json.loads(archive.read('result.json')) == response
        metadata = json.loads(archive.read('metadata.json'))
        assert metadata['candidate_id'] == 6 and metadata['roi'] == [1770, 310, 1920, 465]
        assert metadata['score'] == score and metadata['width'] == 1920 and metadata['height'] == 1080
        assert metadata['recorded_at_utc']
        assert not any(b'private-config-key' in archive.read(name) for name in archive.namelist())
    assert ARCHIVE.as_posix() in caplog.text and '候选 6' in caplog.text


def test_only_latest_rejection_archive_is_retained(monkeypatch, asset_root):
    requests = []
    first = make_recognizer(monkeypatch, asset_root, result(6), requests)
    first.match(raw_frame(), TARGET)
    second = make_recognizer(monkeypatch, asset_root, result(8), requests)
    second.match(raw_frame(), TARGET)
    assert list((asset_root / ARCHIVE).parent.iterdir()) == [asset_root / ARCHIVE]
    with ZipFile(asset_root / ARCHIVE) as archive:
        assert json.loads(archive.read('result.json'))['candidate_id'] == 8
        assert json.loads(archive.read('metadata.json'))['roi'] == [1770, 610, 1920, 780]


@pytest.mark.parametrize('response', [result(7), dict(result(), found=False), dict(result(), page='other')])
def test_accepted_or_unconfirmed_model_results_do_not_save_rejection(monkeypatch, asset_root, response):
    recognizer = make_recognizer(monkeypatch, asset_root, response, [])
    recognizer.match(raw_frame(), TARGET)
    assert not (asset_root / ARCHIVE).exists()


def test_rejection_is_preserved_when_diagnostic_directory_cannot_be_created(monkeypatch, asset_root, caplog):
    (asset_root / '.data').write_text('existing file', encoding='utf-8')
    recognizer = make_recognizer(monkeypatch, asset_root, result(), [])
    assert recognizer.match(raw_frame(), TARGET)[0] is None
    assert 'FileExistsError' in caplog.text


def test_failed_atomic_replace_keeps_previous_archive_and_cleans_own_temporary_file(monkeypatch, asset_root, caplog):
    recognizer = make_recognizer(monkeypatch, asset_root, result(), [])
    recognizer.match(raw_frame(), TARGET)
    destination = asset_root / ARCHIVE
    previous = destination.read_bytes()
    attempted_sources = []

    def fail_replace(source, target):
        assert source != destination and source.parent == destination.parent
        assert target == destination and destination.read_bytes() == previous
        with ZipFile(source) as archive:
            assert json.loads(archive.read('result.json'))['candidate_id'] == 8
        attempted_sources.append(source)
        raise PermissionError('private-error-content')

    monkeypatch.setattr(Path, 'replace', fail_replace)
    second = make_recognizer(monkeypatch, asset_root, result(8), [])
    assert second.match(raw_frame(), TARGET)[0] is None
    assert attempted_sources and all(not path.exists() for path in attempted_sources)
    assert destination.read_bytes() == previous
    assert list(destination.parent.iterdir()) == [destination]
    assert 'PermissionError' in caplog.text and 'private-error-content' not in caplog.text
