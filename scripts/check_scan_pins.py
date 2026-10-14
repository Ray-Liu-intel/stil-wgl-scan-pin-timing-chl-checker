"""STIL/WGL scan pin, timing and Channellink checker (Python 3.10+, stdlib only)."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from wgl_checks import check_wgl, timing_wgl
from stil_checks import timing_stil

DEFAULT_GROUPS = ('stm_scan_in', 'stm_scan_out', 'stm_scan_nonclk_dpin')
# Quoted strings are consumed whole so comment markers inside names remain literal.
LEX = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*|\{\*|\*/|\*\}')
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')
REF = re.compile(r'\s*(?:"([^"\n]+)"|([A-Za-z_][\w.$]*))\s*(?:\[\s*(\d+)\s*\])?\s*')


class ParseError(ValueError):
    pass


def uncomment(lines, pin_file=False):
    """Strip multiline comments, annotations and line comments; retain line numbers."""
    end_marker = None
    for number, line in enumerate(lines, 1):
        if pin_file:
            line = line.split('#', 1)[0] + ('\n' if '#' in line else '')
        pos = 0
        pieces = []
        while pos < len(line):
            if end_marker:
                end = line.find(end_marker, pos)
                if end < 0:
                    break
                pos = end + len(end_marker)
                end_marker = None
                pieces.append(' ')
                continue
            match = LEX.search(line, pos)
            if match is None:
                pieces.append(line[pos:])
                break
            pieces.append(line[pos:match.start()])
            token = match[0]
            pos = match.end()
            if token.startswith('"'):
                pieces.append(token)
            elif token.startswith('//'):
                break
            elif token in ('/*', '{*'):
                end_marker = '*/' if token == '/*' else '*}'
                pieces.append(' ')
            else:
                raise ParseError(f'Unexpected comment terminator at line {number}')
        yield number, ''.join(pieces).rstrip('\r\n') + '\n'
    if end_marker:
        raise ParseError('Unterminated comment or annotation')


def normalize(ref):
    match = REF.fullmatch(ref)
    if match is None:
        raise ParseError(f'Unsupported pin reference: {ref!r}')
    base = match[1] or match[2]
    # Support both "bus"[2] and "bus[2]".
    embedded = re.fullmatch(r'(.+)\[(\d+)\]', base)
    if embedded:
        if match[3] is not None:
            raise ParseError(f'Double pin index: {ref}')
        base, index = embedded.groups()
    else:
        index = match[3]
    return (base + ('_' + index if index is not None else '')).upper()


def load_groups(path, names):
    with Path(path).open(encoding='utf-8-sig') as stream:
        text = ''.join(line for _, line in uncomment(stream, pin_file=True))
    definitions = {}
    for match in re.finditer(r'\bGroup\s+(\w+)\s*\{([^{}]*)\}', text):
        if match[1] in definitions:
            raise ParseError(f'Duplicate group: {match[1]}')
        definitions[match[1]] = match[2]
    # Resource-level pin declarations are the terminals of nested groups.
    declared = set(re.findall(r'^\s*([A-Za-z_]\w*)\s*;', text, re.M))

    def expand(name, stack=()):
        if name in stack:
            raise ParseError(f'Group cycle: {stack + (name,)}')
        if name not in definitions:
            raise ParseError(f'Missing group: {name}')
        pins = set()
        for item in definitions[name].split(','):
            item = item.strip()
            if not item:
                continue
            if item in definitions:
                pins.update(expand(item, stack + (name,)))
            elif item in declared:
                pins.add(normalize(item))
            else:
                raise ParseError(f'Unknown or unsupported member {item!r} in {name}')
        if not pins:
            raise ParseError(f'Empty group: {name}')
        return pins

    return {name: expand(name) for name in names}


def scan_blocks(path):
    """Read every line, retain only ScanStructures blocks; validates gzip on EOF."""
    opener = gzip.open if str(path).lower().endswith('.gz') else open
    active = None
    buffer = []
    depth = 0
    opened = False
    with opener(path, 'rt', encoding='utf-8-sig') as stream:
        for number, line in uncomment(stream):
            mask = QUOTED.sub(lambda m: ' ' * len(m[0]), line)
            if active is None:
                if not re.match(r'^\s*ScanStructures\b', mask):
                    if re.search(r'\bScanStructures\b', mask):
                        raise ParseError(f'ScanStructures must start a line ({number})')
                    continue
                active = number
                depth = 0
                opened = False
            buffer.append(line)
            for char in mask:
                if char == '{':
                    depth += 1
                    opened = True
                elif char == '}':
                    depth -= 1
                    if depth < 0:
                        raise ParseError(f'Unbalanced braces at {number}')
            if opened and depth == 0:
                yield active, ''.join(buffer)
                active = None
                buffer = []
            if sum(map(len, buffer[-1:])) > 16 * 1024 * 1024 or len(buffer) > 200000:
                raise ParseError('ScanStructures exceeds supported size')
    if active is not None:
        raise ParseError('Unterminated ScanStructures')


def check_file(path, groups):
    # load_groups preserves --groups order: scan-in, scan-out, direction-free dpin.
    if len(groups) != 3:
        raise ParseError('Expected three distinct groups: scan-in, scan-out, nonclk dpin')
    in_group, out_group, dp_group = groups
    directional_groups = {'ScanIn': in_group, 'ScanOut': out_group}
    rows = []
    absent = []
    clocks = set()
    chains_count = 0
    blocks_count = 0
    try:
        for first, text in scan_blocks(path):
            blocks_count += 1
            header = re.match(r'\s*ScanStructures\s*(?:"[^"]*"|\w+)?\s*\{', text)
            if header is None:
                raise ParseError('Unsupported ScanStructures header')
            body = text[header.end():text.rfind('}')]
            chain_re = re.compile(r'\bScanChain\s+("[^"]+"|[^\s{]+)\s*\{([^{}]*)\}')
            matches = list(chain_re.finditer(body))
            if chain_re.sub('', body).strip():
                raise ParseError('Unsupported content inside ScanStructures')
            if not matches:
                raise ParseError('Empty ScanStructures')
            for chain in matches:
                chains_count += 1
                name = chain[1].strip('"')
                body_start = header.end() + chain.start(2)
                for role in ('ScanIn', 'ScanOut', 'ScanMasterClock'):
                    fields = list(re.finditer(r'\b' + role + r'\b\s*([^;{}]+);', chain[2]))
                    if len(fields) != len(re.findall(r'\b' + role + r'\b', chain[2])):
                        raise ParseError(f'Malformed {role} in {name}')
                    if len(fields) > 1:
                        raise ParseError(f'Duplicate {role} in {name}')
                    if not fields and role != 'ScanMasterClock':
                        absent.append({'chain': name, 'field': role})
                    for field in fields:
                        raw = field[1].strip()
                        if role == 'ScanMasterClock':
                            # STIL permits a list of scan master clocks (IOD/CCD).
                            offset = 0
                            while offset < len(raw):
                                clock = REF.match(raw, offset)
                                if clock is None:
                                    raise ParseError(f'Unsupported clock reference: {raw!r}')
                                clocks.add(normalize(clock[0]))
                                offset = clock.end()
                            continue
                        pin = normalize(raw)
                        hits = [g for g, pins in groups.items() if pin in pins]
                        expected = directional_groups[role]
                        direction_matches = expected in hits
                        passed = direction_matches or dp_group in hits
                        failure = None if passed else ('DIRECTION_MISMATCH' if hits else 'PIN_NOT_FOUND')
                        rows.append({'chain': name, 'role': role, 'original': raw,
                                     'pin': pin, 'groups': hits,
                                     'expected_group': expected, 'allowed_groups': [expected, dp_group],
                                     'direction_matches': direction_matches, 'passed': passed,
                                     'failure': failure,
                                     'line': first + text[:body_start + field.start()].count('\n')})
        status = 'FAIL' if any(not row['passed'] for row in rows) else ('PASS' if rows else 'UNDETERMINED')
        error = None if rows else 'No declared scan data pins found'
    except (OSError, EOFError, UnicodeError, ValueError) as exc:
        status = 'ERROR'
        error = str(exc)
    return {'file': str(path), 'status': status, 'error': error,
            'blocks': blocks_count, 'chains': chains_count, 'pins': rows,
            'absent_fields': absent, 'clocks_info_only': sorted(clocks)}


def pattern_parts(path):
    """Keep the original complete .stil/.wgl and optional .gz suffix."""
    name = Path(path).name
    length = next((len(ext) for ext in ('.stil.gz', '.wgl.gz', '.stil', '.wgl')
                   if name.lower().endswith(ext)), 0)
    if not length:
        raise ParseError('Unsupported pattern extension: ' + name)
    return name[:-length], name[-length:]


def generated_path(path):
    path = Path(path)
    stem, _ = pattern_parts(path)
    return (stem.upper().endswith('_CHL') or
            re.search(r'(?:^|_)sc_v\d+(?=_|$)', stem, re.I) is not None or
            any(p.lower().endswith('_channellink') for p in path.parts[:-1]))


def find_inputs(path):
    """Prune generated output folders, including on repeat recursive runs."""
    path = Path(path)
    if path.is_file():
        candidates = [path]
    else:
        candidates = []
        for folder, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not d.lower().endswith('_channellink')]
            candidates.extend(Path(folder) / f for f in files)
    return sorted(p for p in candidates if p.name.lower().endswith(('.stil', '.stil.gz', '.wgl', '.wgl.gz'))
                  and not generated_path(p))


def channel_link_plan(result, io_groups, *, die):
    if die == 'DRD':
        return {'eligible': None, 'action': 'NOT_CHECKED', 'destination': None,
                'reason': 'DRD policy: basic pin check only; Channellink is not evaluated'}
    if die not in ('IOD', 'CCD'):
        raise ParseError('Channellink requires an explicit IOD or CCD die')
    path = Path(result['file'])
    stem, suffix = pattern_parts(path)
    plan = {'eligible': False, 'action': 'SKIPPED', 'destination': None}
    if generated_path(path):
        reason = 'Already a Channellink output'
    elif not re.search(r'(?:^|_)(?:body|chain)(?:_|$)', stem, re.I):
        reason = 'Not a body or chain filename'
    elif re.search(r'(?:^|_)setup(?:_|$)', stem, re.I):
        reason = 'Ambiguous body/chain with setup filename'
    elif result['status'] != 'PASS' or not result['pins']:
        reason = 'Pin membership check did not PASS'
    elif not all(io_groups[0 if row['role'] == 'ScanIn' else 1] in row['groups']
                 for row in result['pins']):
        reason = 'At least one data pin lacks its direction-matched scan group (dpin cannot qualify for CHL)'
    else:
        plan['eligible'] = True
        versions = list(re.finditer(r'(?:^|_)(v\d+)(?=_|$)', stem, re.I))
        if len(versions) != 1:
            plan.update(action='ERROR', reason='Expected exactly one v<number> filename token')
            return plan
        version = versions[0]
        output_stem = stem[:version.start(1)] + 'sc_' + stem[version.start(1):]
        destination = path.parent / (version[1].lower() + '_Channellink') / (output_stem + suffix)
        plan.update(action='ELIGIBLE', destination=str(destination),
                    reason='Every ScanIn matches scan_in and every ScanOut matches scan_out')
        return plan
    plan['reason'] = reason
    return plan


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def export_channel_link(result, io_groups, dry_run=False, *, die):
    """Copy eligible originals byte-for-byte; never overwrite an existing destination."""
    plan = channel_link_plan(result, io_groups, die=die)
    if plan['action'] != 'ELIGIBLE':
        return plan
    if dry_run:
        plan['action'] = 'WOULD_COPY'
        return plan
    source = Path(result['file'])
    destination = Path(plan['destination'])
    created = False
    try:
        if source.is_symlink() or destination.parent.is_symlink() or destination.is_symlink():
            raise OSError('Refusing symlink source or output')
        expected = sha256(source)
        if destination.exists():
            if not destination.is_file() or sha256(destination) != expected:
                raise OSError('Destination exists with different content; not overwritten')
            plan.update(action='ALREADY_EXISTS', sha256=expected)
            return plan
        destination.parent.mkdir(exist_ok=True)
        with source.open('rb') as incoming, destination.open('xb') as outgoing:
            created = True
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        if sha256(destination) != expected or sha256(source) != expected:
            raise OSError('Copy verification failed or source changed during copy')
        plan.update(action='COPIED', sha256=expected)
    except OSError as exc:
        if created:
            destination.unlink(missing_ok=True)
        plan.update(action='ERROR', reason=str(exc))
    return plan


def resolve_pin(args):
    if args.pin_file:
        return args.pin_file
    if not (args.plt_dir and args.stage and args.die):
        raise ParseError('Use --pin-file or all of --plt-dir, --stage, --die')
    kind = 'class' if args.stage == 'class' else {'DRD': 'ddr', 'CCD': 'sort', 'IOD': 'pcie'}[args.die]
    candidates = [p for p in args.plt_dir.iterdir()
                  if p.suffix.lower() == '.pin' and kind in p.stem.lower().split('_')]
    if len(candidates) != 1:
        raise ParseError(f'Expected one {kind} pin file; found {len(candidates)}. Use --pin-file.')
    return candidates[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='STIL/WGL file or directory (recursive)')
    parser.add_argument('--pin-file', type=Path)
    parser.add_argument('--plt-dir', type=Path)
    parser.add_argument('--stage', choices=('sort', 'class'))
    parser.add_argument('--die', type=str.upper, choices=('DRD', 'CCD', 'IOD'), required=True,
                        help='DRD: basic only; IOD/CCD: basic plus Channellink eligibility')
    parser.add_argument('--groups', nargs=3, default=DEFAULT_GROUPS)
    parser.add_argument('--channel-link', action='store_true',
                        help='Opt in to IOD/CCD copies with _sc_v<number> names in source-parent/v<number>_Channellink')
    parser.add_argument('--dry-run', action='store_true', help='Preview --channel-link without copying')
    parser.add_argument('--timing', action='store_true',
                        help='Report conservative timing evidence; UNDETERMINED is not a timing pass')
    args = parser.parse_args(argv)
    if args.dry_run and not args.channel_link:
        parser.error('--dry-run requires --channel-link')
    if args.channel_link and args.die == 'DRD':
        parser.error('DRD is basic-only; omit --channel-link')
    try:
        pin_file = resolve_pin(args)
        groups = load_groups(pin_file, args.groups)
        paths = find_inputs(args.input)
        if not paths:
            raise ParseError('No .stil/.wgl files found')
        if args.channel_link and not args.timing and any('.wgl' in path.name.lower() for path in paths):
            raise ParseError('WGL Channellink export requires --timing; unvalidated WGL cannot be copied')
        results = [(check_wgl(path, groups, normalize) if '.wgl' in path.name.lower() else check_file(path, groups))
                   for path in paths]
        for result in results:
            if args.timing:
                if '_wgl_timing' in result and args.die != 'CCD':
                    result['timing'] = {'status': 'UNDETERMINED',
                                        'reason': 'WGL symbolic checks are defined only for CCD; die mismatch'}
                else:
                    result['timing'] = (timing_wgl(result, args.die) if '_wgl_timing' in result
                                        else timing_stil(result['file'], args.die))
            # Never export a file which timing explicitly failed or was not established.
            if args.channel_link and args.timing and result['timing']['status'] != 'PASS':
                result['channel_link'] = {'eligible': False, 'action': 'SKIPPED', 'destination': None,
                                          'reason': 'Timing is not proven PASS'}
            else:
                result['channel_link'] = (export_channel_link(result, args.groups[:2], args.dry_run, die=args.die)
                                          if args.channel_link else channel_link_plan(result, args.groups[:2], die=args.die))
            result.pop('_wgl_timing', None)
        counts = dict(Counter(row['status'] for row in results))
        print(json.dumps({'pin_file': str(pin_file), 'group_sizes': {g: len(p) for g, p in groups.items()},
                          'die': args.die, 'channel_link_checks': args.die in ('IOD', 'CCD'),
                          'normalization': 'uppercase; [n] -> _n', 'clock_checks': args.timing,
                          'timing_summary': (dict(Counter(r['timing']['status'] for r in results))
                                             if args.timing else None),
                          'direction_required': True, 'dpin_direction_exempt': True,
                          'channel_link_requested': args.channel_link, 'dry_run': args.dry_run,
                          'channel_link_summary': dict(Counter(r['channel_link']['action'] for r in results)),
                          'summary': counts, 'files': results}, indent=2))
        if args.channel_link and any(r['channel_link']['action'] == 'ERROR' for r in results):
            return 2
        if any(r['status'] in ('ERROR', 'UNDETERMINED') for r in results):
            return 2
        if args.timing and any(r['timing']['status'] in ('ERROR', 'UNDETERMINED') for r in results):
            return 2
        return 1 if counts.get('FAIL') or (args.timing and any(r['timing']['status'] == 'FAIL'
                                                          for r in results)) else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())