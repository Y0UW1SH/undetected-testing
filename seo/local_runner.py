"""Run collection and monitoring locally; no GitHub account or hosted jobs."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import webbrowser
import zipfile

from seo import local_state as state

JOBS = ('collect', 'census', 'subnets', 'seo', 'research')
DEFAULTS = {
    'collect': ['--addresses-only', '--subnets', '1', '--top', '1',
                '--new-wallets', '2', '--history-pages', '4',
                '--history-charts', '2', '--delay', '2-4'],
    'census': ['--pages', '8', '--page-size', '100', '--delay', '2-4',
               '--sample-last'],
    'subnets': ['--threshold', '10'],
    'seo': ['--per-session', '3', '--on-challenge', 'stop'],
    'research': ['--threshold', '10', '--per-session', '3',
                 '--on-challenge', 'skip'],
}
DATABASES = dict(collect='wallets.sqlite', census='accounts.sqlite',
                 subnets='tao.sqlite', seo='seo.sqlite',
                 research='research.sqlite')


@contextmanager
def directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def initialize(root):
    root = Path(root)
    for name in ('config', 'reports', 'runs',
                 'status', 'try_tao_out', 'snapshots'):
        (root / name).mkdir(parents=True, exist_ok=True)
    config = root / 'config/local.json'
    if not config.exists():
        with config.open('x', encoding='utf8') as out:
            json.dump(DEFAULTS, out, indent=2)
    examples = Path(__file__).parent / 'config'
    for name in ('keywords', 'research', 'tao'):
        target = root / 'config' / (name + '.json')
        if not target.exists():
            with target.open('xb') as out:
                out.write((examples / (name + '.example.json')).read_bytes())


def invoke(module, argv):
    """In-process entry preserves the lease until database writes stop."""
    return importlib.import_module(module).main(argv) or 0


def _options(root, job, extra):
    config = json.loads((root / 'config/local.json').read_text('utf8'))
    if not isinstance(config, dict):
        raise state.StateError('Local configuration must be a JSON object.')
    values = config.get(job, DEFAULTS[job])
    if not isinstance(values, list) or not all(isinstance(v, str)
                                               for v in values):
        raise state.StateError('Job configuration must be a list of strings.')
    args = [*values, *extra]
    forbidden = {'--db', '--out', '--dir', '--alerts-out', '--config'}
    if any(arg.split('=', 1)[0] in forbidden for arg in args):
        raise state.StateError(
            'Output/config paths are managed by local runner.')
    return args


def _commands(root, staging, job, options):
    db = str(root / DATABASES[job])
    if job == 'collect':
        price = root / 'tao.sqlite'
        if price.is_file() and '--price-history' not in options:
            options = [*options, '--price-history', str(price)]
        return [('seo.tao_cli', ['--db', db, 'collect', *options,
                                 '--out', str(staging / 'tao_collect.json')])]
    if job == 'census':
        return [('seo.sources.tao_census', ['--db', db, *options, '--out',
                                            str(staging
                                                / 'account-census.json')])]
    if job == 'subnets':
        base = ['--db', db]
        return [('seo.tao_cli', [*base, 'watch', *options, '--config',
                                 str(root / 'config/tao.json'), '--alerts-out',
                                 str(staging / 'tao_alerts.json')]),
                ('seo.tao_cli', [*base, 'report', '--out',
                                 str(staging / 'tao_report.html')]),
                ('seo.tao_cli', [*base, 'export', '--dir', str(staging)])]
    module = 'seo.cli' if job == 'seo' else 'seo.research_cli'
    base = ['--db', db]
    config = 'keywords' if job == 'seo' else 'research'
    command = 'batch' if job == 'seo' else 'track'
    report_name = 'seo_report.html' if job == 'seo' else 'research_report.html'
    report_folder = staging / 'data' if job == 'seo' else staging
    return [(module, [*base, command, *options,
                      '--config', str(root / 'config' / (config + '.json'))]),
            (module, [*base, 'report', '--out',
             str(report_folder / report_name)]),
            (module, [*base, 'export', '--dir', str(staging)])]


def _publish(root, staging, job):
    files = [path for folder in (staging, staging / 'data')
             for suffix in ('json', 'html', 'csv')
             for path in folder.glob('*.' + suffix)]
    if len({path.name for path in files}) != len(files):
        raise state.StateError('Ambiguous output names; prior reports kept.')
    required = {'collect': 'tao_collect.json', 'census': 'account-census.json',
                'subnets': 'tao_report.html', 'seo': 'seo_report.html',
                'research': 'research_report.html'}[job]
    if not any(path.name == required for path in files):
        raise state.StateError('No usable report produced; prior report kept.')
    # Validate all files before replacing any current view.
    for path in files:
        if path.suffix == '.json':
            value = json.loads(path.read_text('utf8'))
            if path.name == 'tao_collect.json':
                from seo.tao_view import load_snapshot
                load_snapshot(path)
            if path.name == 'account-census.json' and not isinstance(
                    value, dict):
                raise state.StateError('Invalid census report.')
    if job == 'collect':
        ident = ''.join(c for c in state.stamp() if c.isdigit())
        state.atomic_write(root / 'snapshots' / ident / 'tao_collect.json',
                           (staging / 'tao_collect.json').read_bytes())
    for path in files:
        state.atomic_write(root / 'reports' / path.name, path.read_bytes())
    return len(files)


def run_job(root, job, extra, wait=0):
    root = Path(root).resolve()
    initialize(root)
    try:
        options = _options(root, job, extra)
        with state.lease(root, timeout=wait):
            staging = root / 'runs' / (state.stamp() + '-' + job)
            staging.mkdir()
            status = dict(job=job, started_at=state.stamp(), status='running',
                          run=staging.name)
            status_path = root / 'status' / (job + '.json')
            state.atomic_write(status_path, json.dumps(status, indent=2))
            result = 0
            published = False
            try:
                backup = state.backup(root)
                status['backup'] = backup.name
                with (staging / 'run.log').open('w', encoding='utf8') as log:
                    with directory(staging), redirect_stdout(log), (
                            redirect_stderr(log)):
                        for module, args in _commands(
                                root, staging, job, options):
                            try:
                                result = max(result, invoke(module, args))
                            except SystemExit as exc:
                                result = max(result, int(exc.code or 0))
                _publish(root, staging, job)
                published = True
                if job == 'collect':
                    data = json.loads((staging / 'tao_collect.json').read_text(
                        'utf8'))
                    coverage = list(data.get('coverage', {}).values())
                    if coverage and any(not any(c.get(tab) == 'complete'
                                                or c.get(tab + '_rows', 0)
                                                for c in coverage)
                                        for tab in
                                        ('transactions', 'transfers')):
                        result = max(result, 1)
            except KeyboardInterrupt:
                result = 130
            except Exception as exc:
                # Browser exceptions may contain a private proxy/endpoint.
                status['error_type'] = type(exc).__name__
                result = max(result, 1)
            finally:
                status.update(finished_at=state.stamp(), exit_code=result,
                              status=('complete' if result == 0 else
                                      'partial' if published else 'failed'))
                state.atomic_write(status_path, json.dumps(status, indent=2))
            print('%s: %s. Details: %s' % (job, status['status'], staging))
            return result
    except (state.StateError, ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, state.StateError) else
              'Cannot read local configuration or state.', file=sys.stderr)
        return 2


def watch(root, extra):
    forbidden = {'--refresh-snapshots', '--cached-report', '--report',
                 '--out', '--view', '--movement-archive', '--snapshot-cache'}
    if any(arg.split('=', 1)[0] in forbidden for arg in extra):
        raise state.StateError('Use local reports and managed watcher paths.')
    report = root / 'reports/tao_collect.json'
    args = ['--out', str(root / 'try_tao_out/local_wallet_activity.json'),
            '--view', str(root / 'snapshots/wallets.html'),
            '--movement-archive', str(root / 'try_tao_out/movements.sqlite')]
    if not report.exists():
        raise state.StateError('Collect or import a report before starting '
                               'the local watcher.')
    args += ['--report',
             str(report),
             '--snapshot-cache',
             str(root / 'snapshots')]
    if '--once' not in extra and '--prompt' not in extra:
        args += ['--prompt']
    endpoint = root / 'dwellir.txt'
    if endpoint.exists() and '--endpoint-file' not in extra:
        args += ['--endpoint-file', str(endpoint)]
    with directory(root):
        return invoke('seo.tao_local_watch', [*args, *extra])


def view(root):
    from seo.tao_view import _archive, load_snapshot, newest_cached, render
    report = load_snapshot(root / 'reports/tao_collect.json')
    feed_path = root / 'try_tao_out/local_wallet_activity.json'
    feed = json.loads(feed_path.read_text(
        'utf8')) if feed_path.exists() else None
    path = root / 'snapshots/wallets.html'
    html = render(report, feed)
    cache = root / 'snapshots'
    source = newest_cached(cache)
    if source:
        html = _archive(html, report, source, cache)
    state.atomic_write(path, html)
    webbrowser.open(path.as_uri())
    print(path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--root', type=Path, default=state.default_root())
    parser.add_argument('--wait', type=float, default=0,
                        help='seconds to wait for another collector')
    sub = parser.add_subparsers(dest='command', required=True)
    for job in (*JOBS, 'watch'):
        sub.add_parser(
            job,
            add_help=False,
            help='pass --help for module options')
    for name in ('init', 'backup', 'status', 'view',
                 'check', 'edit', 'directory'):
        sub.add_parser(name)
    from seo import local_api
    api = sub.add_parser('api-collect', help='bounded TAO.com observations')
    api.add_argument('--netuid', type=local_api.bounded(0, 65535), default=1)
    api.add_argument('--address', type=local_api.address_arg)
    api.add_argument('--pages', type=local_api.bounded(1, 5), default=1)
    api.add_argument('--page-size', type=local_api.bounded(1, 100), default=10)
    imp = sub.add_parser('import-artifact')
    imp.add_argument('zip', type=Path)
    compare = sub.add_parser('compare-artifacts')
    compare.add_argument('left', type=Path)
    compare.add_argument('right', type=Path)
    compare.add_argument('--to', type=Path, required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--to', type=Path, required=True)
    census = sub.add_parser('restore-census')
    census.add_argument('checkpoint', type=Path)
    args, extra = parser.parse_known_args(argv)
    if extra and args.command not in (*JOBS, 'watch'):
        parser.error('unrecognized arguments: ' + ' '.join(extra))
    if not 0 <= args.wait <= 86400:
        parser.error('--wait must be between 0 and 86400 seconds')
    root = args.root.expanduser().resolve()
    try:
        if args.command in (*JOBS, 'watch') and extra == ['--help']:
            module = {
                'collect': 'seo.tao_cli', 'subnets': 'seo.tao_cli',
                'seo': 'seo.cli', 'research': 'seo.research_cli',
                'census': 'seo.sources.tao_census',
                'watch': 'seo.tao_local_watch'}[args.command]
            command = {'collect': 'collect', 'subnets': 'watch',
                       'seo': 'batch', 'research': 'track'}.get(args.command)
            try:
                return invoke(module, ([command] if command else []) + extra)
            except SystemExit as exc:
                return int(exc.code or 0)
        if args.command == 'api-collect':
            return local_api.run(root, netuid=args.netuid,
                                 address=args.address,
                                 pages=args.pages, page_size=args.page_size,
                                 wait=args.wait)
        if args.command in JOBS:
            return run_job(root, args.command, extra, wait=args.wait)
        if args.command == 'compare-artifacts':
            from seo.local_reconcile import preserve_and_compare
            preserve_and_compare(args.left, args.right, args.to)
            print('Both originals preserved and compared in %s. '
                  'No database merged or selected.' % args.to)
            return 0
        if args.command == 'restore':
            state.restore(args.backup, args.to.expanduser().resolve())
            print('Restored into %s' % args.to)
            return 0
        initialize(root)
        if args.command == 'init':
            print(
                'Local data: %s\nEdit config/local.json before collecting.' %
                root)
        elif args.command == 'import-artifact':
            print('Imported; original backup: %s' % state.import_artifact(
                args.zip, root))
        elif args.command == 'restore-census':
            from tools.tao_census_workflow import restore as restore_census
            with state.lease(root):
                restore_census(args.checkpoint, root / 'accounts.sqlite')
            print('Census restored; source checkpoint unchanged.')
        elif args.command == 'backup':
            with state.lease(root):
                print(state.backup(root))
        elif args.command == 'watch':
            return watch(root, extra)
        elif args.command == 'edit':
            with directory(root):
                return invoke('seo.tao_watchlist', ['--open'])
        elif args.command == 'directory':
            path = root / 'snapshots/accounts.html'
            code = invoke('seo.tao_directory_view', [
                '--census', str(root / 'accounts.sqlite'),
                '--cache', str(root / 'snapshots'), '--out', str(path)])
            if code == 0:
                webbrowser.open(path.as_uri())
            return code
        elif args.command == 'view':
            return view(root)
        elif args.command == 'status':
            print('Local data: %s' % root)
            print(
                'Free disk: %.1f GiB' %
                (shutil.disk_usage(root).free / 2**30))
            for path in sorted((root / 'status').glob('*.json')):
                print(path.read_text('utf8'))
        elif args.command == 'check':
            return invoke('seo.cli', ['--db', str(root / 'seo.sqlite'),
                                      'doctor', '--config',
                                      str(root / 'config/keywords.json')])
        return 0
    except (state.StateError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile):
        print('Could not read or write local state. Originals kept.',
              file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
