"""Offline local job orchestration with injectable module entry points."""
import json
from pathlib import Path

from seo import local_runner as runner


def snapshot():
    return {'checked_at': '2026-11-08T00:00:00+00:00',
            'coverage': {'example': {'transactions': 'complete',
                                     'transfers': 'complete'}},
            'performance': {'wallets': []}}


def test_init_is_repeatable_and_preserves_user_configuration(tmp_path):
    runner.initialize(tmp_path)
    config = tmp_path / 'config/local.json'
    original = json.loads(config.read_text())
    assert '--addresses-only' in original['collect']
    config.write_text('{"collect": ["--subnets", "4"]}')
    runner.initialize(tmp_path)
    assert json.loads(config.read_text()) == {'collect': ['--subnets', '4']}


def test_collect_publishes_valid_snapshot_and_returns_partial_failure(
        tmp_path, monkeypatch):
    runner.initialize(tmp_path)

    def fake(module, argv):
        assert module == 'seo.tao_cli'
        assert Path(argv[argv.index('--db') + 1]
                    ) == tmp_path / 'wallets.sqlite'
        out = Path(argv[argv.index('--out') + 1])
        out.write_text(json.dumps(snapshot()))
        return 1
    monkeypatch.setattr(runner, 'invoke', fake)
    assert runner.run_job(tmp_path, 'collect', []) == 1
    assert json.loads((tmp_path / 'reports/tao_collect.json').read_text()) == (
        snapshot())
    status = json.loads((tmp_path / 'status/collect.json').read_text())
    assert status['status'] == 'partial'
    assert status['exit_code'] == 1


def test_invalid_report_preserves_last_good_snapshot(tmp_path, monkeypatch):
    runner.initialize(tmp_path)
    report = tmp_path / 'reports/tao_collect.json'
    report.write_text(json.dumps(snapshot()))
    before = report.read_bytes()

    def fake(module, argv):
        Path(argv[argv.index('--out') + 1]).write_text('{half')
        return 0
    monkeypatch.setattr(runner, 'invoke', fake)
    assert runner.run_job(tmp_path, 'collect', []) != 0
    assert report.read_bytes() == before


def test_managed_output_flags_cannot_escape_private_root(
        tmp_path, monkeypatch):
    runner.initialize(tmp_path)
    calls = []
    monkeypatch.setattr(runner, 'invoke', lambda *args: calls.append(args))
    assert runner.run_job(tmp_path, 'collect', ['--out=/tmp/elsewhere']) == 2
    assert not calls


def test_default_census_is_one_resumable_chunk(tmp_path, monkeypatch):
    runner.initialize(tmp_path)
    calls = []

    def fake(module, argv):
        calls.append((module, argv))
        Path(argv[argv.index('--out') + 1]).write_text(json.dumps({
            'error': None, 'status': 'partial', 'next_page': 9}))
        return 0
    monkeypatch.setattr(runner, 'invoke', fake)
    assert runner.run_job(tmp_path, 'census', []) == 0
    assert len(calls) == 1
    assert calls[0][0] == 'seo.sources.tao_census'
    assert calls[0][1][calls[0][1].index('--db') + 1] == str(
        tmp_path / 'accounts.sqlite')


def test_watch_dispatch_never_refreshes_github(tmp_path, monkeypatch):
    runner.initialize(tmp_path)
    (tmp_path / 'reports/tao_collect.json').write_text(json.dumps(snapshot()))
    calls = []
    monkeypatch.setattr(
        runner,
        'invoke',
        lambda *args: calls.append(args) or 0)
    assert runner.main(['--root', str(tmp_path), 'watch', '--once']) == 0
    module, argv = calls[0]
    assert module == 'seo.tao_local_watch'
    assert '--refresh-snapshots' not in argv
    assert '--report' in argv and '--once' in argv


def test_no_hosted_workflows_remain():
    repo = Path(__file__).resolve().parents[1]
    assert not list((repo / '.github/workflows').glob('*.y*ml'))


def test_watch_requires_snapshot_before_starting(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, 'invoke', lambda *args: calls.append(args))
    assert runner.main(['--root', str(tmp_path), 'watch']) == 2
    assert not calls


def test_scheduler_can_wait_for_other_collectors(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, 'run_job', lambda *args, **kw:
                        calls.append(kw) or 0)
    assert runner.main(['--root', str(tmp_path), '--wait', '21600',
                        'collect']) == 0
    assert calls == [{'wait': 21600}]


def test_all_job_commands_parse_with_existing_clis(tmp_path):
    runner.initialize(tmp_path)
    staging = tmp_path / 'runs/example'
    staging.mkdir()
    for job in runner.JOBS:
        commands = runner._commands(tmp_path, staging, job,
                                    runner.DEFAULTS[job])
        for module, argv in commands:
            if module == 'seo.sources.tao_census':
                continue  # CLI is tested via bounded-census tests.
            cli = __import__(module, fromlist=['build_parser'])
            parsed = cli.build_parser().parse_args(argv)
            assert parsed.db == str(tmp_path / runner.DATABASES[job])


def test_seo_heatmaps_stay_with_report_and_are_published(
        tmp_path, monkeypatch):
    runner.initialize(tmp_path)

    def fake(module, argv):
        if 'batch' in argv:
            data = Path.cwd() / 'data'
            data.mkdir()
            (data / 'grid_business.html').write_text('<html>heatmap</html>')
        if 'report' in argv:
            out = Path(argv[argv.index('--out') + 1])
            assert (out.parent / 'grid_business.html').is_file()
            out.write_text('<a href="grid_business.html">heatmap</a>')
        return 0

    monkeypatch.setattr(runner, 'invoke', fake)
    assert runner.run_job(tmp_path, 'seo', []) == 0
    assert (tmp_path / 'reports/grid_business.html').is_file()
    assert 'grid_business.html' in (
        tmp_path / 'reports/seo_report.html').read_text()


def test_non_object_config_fails_without_starting_collection(
        tmp_path, monkeypatch):
    runner.initialize(tmp_path)
    (tmp_path / 'config/local.json').write_text('[]')
    monkeypatch.setattr(runner, 'invoke', lambda *args:
                        (_ for _ in ()).throw(AssertionError('must not run')))
    assert runner.run_job(tmp_path, 'collect', []) == 2


def test_compare_command_preserves_both_without_initializing_default_root(
        tmp_path, monkeypatch):
    from tests.test_local_reconcile import artifact
    left = artifact(tmp_path, 'left', [('old', '{}')])
    right = artifact(tmp_path, 'right', [('new', '{}')])
    unused = tmp_path / 'unused'
    monkeypatch.setenv('TAO_LOCAL_HOME', str(unused))
    dest = tmp_path / 'comparison'
    assert runner.main(['compare-artifacts', str(left), str(right),
                        '--to', str(dest)]) == 0
    assert (dest / 'manifest.json').is_file()
    assert not unused.exists()
