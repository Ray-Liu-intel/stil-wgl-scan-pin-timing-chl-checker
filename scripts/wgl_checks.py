"""Streaming full-file checks for Tessent-style WGL; symbolic timing only, never inferred.

Clocks are identified from timeplate waveforms (return-format inputs), not
pin names. Executed cycles are counted from ``repeat``/``vector`` and from
``scan`` statements (longest referenced scanstate) and cross-checked against
``TesterCycle`` annotations. Unsupported syntax raises ERROR.
"""
from collections import Counter
import gzip
from pathlib import Path
import re

TIME_PS = {'ps': 1, 'ns': 1000, 'us': 1000000}
TARGET_PERIOD_PS = 10000  # 10 ns vector period
TCK_HALF_CYCLES = 5  # 10 MHz TCK at 10 ns/cycle: five 0 then five 1 (informational)
MIN_SCAN_CLOCK_RUN = 2  # body PASS needs >= 2 consecutive cycles of one scan clock at 1
QUOTED_PIN = re.compile(r'"([^"\n]+)"\s*(?:\[\s*(\d+)\s*\])?')
CHAIN = re.compile(r'^\s*([^\s\[\]]+)\s*\[\s*(.*?)\s*\]\s*;', re.S | re.M)
PERIOD = re.compile(r'^timeplate\s+(\w+)\s+period\s+(\w+|\d+(?:\.\d+)?\s*(?:ps|ns|us))$', re.I)
EQUATION = re.compile(r'^(\w+)\s*:=\s*([^;]+?)\s*;$')
WAVE = re.compile(r'^("[^"]+"\s*(?:\[\s*\d+\s*\])?)\s*:=\s*(input|output|bidir)\s*\[([^\]]*)\]\s*;$', re.I)
STATEMENT = re.compile(r'^(?:repeat\s+(\d+)\s+)?(vector|scan)\s*\(\s*[^,()]+,\s*(\w+)\s*\)\s*:=\s*\[', re.I)
STATE = re.compile(r'^(\w+)\s*:=[^(;]*\(')
SCAN_REF = re.compile(r'(input|output)\s*\[\s*(\w+)\s*:\s*(\w+)\s*\]', re.I)
PATTERN = re.compile(r'^pattern\s+\w+\s*\(', re.I)
TESTER_CYCLE = re.compile(r'\bTesterCycle\s*:?\s*(\d+)')
ANNOTATION = re.compile(r'\{[^{}]*\}')
TCK = re.compile(r'TCK(?:_IN)?', re.I)
SE_CMD = re.compile(r'SE_CMD\s+label:\w+')  # tool label marker; consumes no tester cycle


def open_text(path):
    return gzip.open(path, 'rt', encoding='utf-8-sig') if str(path).lower().endswith('.gz') else open(path, encoding='utf-8-sig')


def pin_result(chain, role, raw, number, groups, normalize):
    pin = normalize(raw)
    in_group, out_group, dp_group = groups
    expected = in_group if role == 'ScanIn' else out_group
    hits = [g for g, pins in groups.items() if pin in pins]
    good = expected in hits or dp_group in hits
    return {'chain': chain, 'role': role, 'original': raw, 'pin': pin,
            'groups': hits, 'expected_group': expected, 'allowed_groups': [expected, dp_group],
            'direction_matches': expected in hits, 'passed': good,
            'failure': None if good else 'DIRECTION_MISMATCH' if hits else 'PIN_NOT_FOUND',
            'line': number}


def _time(expr, equations):
    """Resolve 'N[ps|ns|us]', equation names and '+' sums to ps; None if unresolved."""
    total = 0
    for term in expr.split('+'):
        term = term.strip()
        literal = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(ps|ns|us)', term, re.I)
        if literal:
            total += round(float(literal[1]) * TIME_PS[literal[2].lower()])
        elif equations.get(term) is not None:
            total += equations[term]
        else:
            return None
    return total


def _symbols(text):
    return len(''.join(text.split()))


class _TckRuns:
    """Streaming run-length analysis of TCK 0/1 symbols over executed cycles."""

    def __init__(self):
        self.value, self.length = None, 0
        self.high, self.low = Counter(), Counter()
        self.violations = Counter()
        self.pending_low = None
        self.pulses = self.unknown = self.streak = self.best = self.toggles = 0
        self.last = None
        self.preview = []

    def add(self, value, count):
        if value == self.value:
            self.length += count
        else:
            self._close()
            self.value, self.length = value, count

    def _close(self):
        value, length = self.value, self.length
        if value is None:
            return
        if value in ('0', '1') and self.last in ('0', '1') and value != self.last:
            self.toggles += 1
        self.last = value
        if len(self.preview) < 12:
            self.preview.append(f'{value}x{length}')
        if value == '0':
            self.pending_low = length
            return
        if value != '1':
            self.unknown += length
            self.pending_low = None
            self.streak = 0
            return
        before, self.pending_low = self.pending_low, None
        if before is not None:
            self.low[('leading' if not self.pulses else 'interior', before)] += 1
        self.pulses += 1
        self.high[length] += 1
        if length != TCK_HALF_CYCLES:
            self.violations[f'high_run_{length}_cycles'] += 1
        if before is None:
            self.violations['high_without_preceding_low'] += 1
        elif before < TCK_HALF_CYCLES:
            self.violations[f'low_run_{before}_cycles'] += 1
        if length == TCK_HALF_CYCLES and before is not None and before >= TCK_HALF_CYCLES:
            self.streak = self.streak + 1 if before == TCK_HALF_CYCLES and self.streak else 1
        else:
            self.streak = 0
        self.best = max(self.best, self.streak)

    def finish(self):
        self._close()
        self.value = None
        if self.pending_low is not None:
            self.low[('trailing' if self.pulses else 'constant', self.pending_low)] += 1
            self.pending_low = None
        return {'pulses': self.pulses, 'toggles': self.toggles, 'unknown_cycles': self.unknown,
                'max_consecutive_periods': self.best,
                'high_run_lengths': dict(self.high.most_common(10)),
                'low_run_lengths': {f'{kind}:{length}': count
                                    for (kind, length), count in self.low.most_common(10)},
                'violations': dict(self.violations.most_common(10)),
                'first_runs': self.preview}


class _Parser:
    def __init__(self, groups, normalize):
        self.groups, self.normalize = groups, normalize
        self.rows, self.equations, self.periods, self.waves, self.states = [], {}, {}, {}, {}
        self.order, self.tck_idx, self.clock_idx = [], (), ()
        self.tables = set()
        self.cycles = self.statements = self.verified = self.patterns = 0
        self.pending_tc = self.pending_repeat = None
        self.closed = False
        self.clock_one_cycles = 0
        self.clocks_pulsed = set()
        self.clock_run, self.clock_best = {}, {}  # per scan clock: current / max consecutive 1 cycles
        self.tck = _TckRuns()
        self.kind, self.repeat, self.tck_value, self.clock_hits, self.tail = None, 1, None, [], []
        self.state_name, self.state_len = None, 0
        self.loops = []  # open loop frames: [count, items, cycles_per_iteration]
        self.loop_count = 0
        self.se_cmd_labels = 0

    def run(self, stream):
        mode, buf, size, depth, chain_line = None, [], 0, 0, 0
        for number, line in enumerate(stream, 1):
            # Fast paths for bulk vector data and scan-state data (no regex).
            if mode == 'values':
                end = line.find(']')
                if end < 0:
                    buf.append(line)
                    continue
                buf.append(line[:end])
                mode = self.values_done(''.join(buf), line[end + 1:], number)
                buf = []
                continue
            if mode == 'state_data':
                end = line.find(')')
                self.state_len += _symbols(line if end < 0 else line[:end])
                if end >= 0:
                    self.states[self.state_name] = self.state_len
                    mode = 'scanstate'
                continue
            if mode == 'tail':
                self.tail.append(ANNOTATION.sub(' ', line))
                if ';' in line:
                    self.scan_done(number)
                    mode = 'pattern'
                continue
            if mode == 'comment':
                end = line.find('}')
                if end < 0:
                    continue
                if line[end + 1:].strip():
                    raise ValueError(f'Unsupported text after multi-line WGL annotation at line {number}')
                mode = resume
                continue
            if '{' in line:
                tc = TESTER_CYCLE.search(line) if mode == 'pattern' else None
                if tc:
                    self.pending_tc = int(tc[1])
                line = ANNOTATION.sub(' ', line)
                if mode in ('pattern', 'header') and ('{' in line or '}' in line):
                    start = line.find('{')
                    # Only a standalone comment opening a multi-line annotation is supported.
                    if start < 0 or line[:start].strip() or '}' in line:
                        raise ValueError(f'Unsupported multi-line WGL annotation at line {number}')
                    if tc and TESTER_CYCLE.search(line):
                        raise ValueError(f'TesterCycle inside multi-line WGL annotation at line {number}')
                    resume, mode = mode, 'comment'
                    continue
            text = line.strip()
            if not text:
                continue
            low = text.lower()
            if mode == 'chain':
                if low == 'end':
                    self.chains_done(''.join(buf), chain_line)
                    mode, buf = None, []
                else:
                    buf.append(line)
                    size += len(line)
                    if size > 64 * 1024 * 1024:
                        raise ValueError('WGL scanchain exceeds supported size')
                continue
            if mode == 'scanstate':
                if low == 'end':
                    mode = None
                    continue
                match = STATE.match(text)
                if not match:
                    raise ValueError(f'Unsupported WGL scanstate syntax at line {number}')
                rest = text[match.end():]
                end = rest.find(')')
                self.state_name, self.state_len = match[1], _symbols(rest if end < 0 else rest[:end])
                if end >= 0:
                    self.states[match[1]] = self.state_len
                else:
                    mode = 'state_data'
                continue
            if mode == 'equations':
                if low.startswith('exprset'):
                    depth += 1
                elif low == 'end':
                    if depth:
                        depth -= 1
                    else:
                        mode = None
                else:
                    match = EQUATION.match(text)
                    if match:
                        self.equations[match[1]] = _time(match[2], self.equations)
                continue
            if mode == 'timeplate':
                if low == 'end':
                    mode = None
                    continue
                match = WAVE.match(text)
                if not match:
                    raise ValueError(f'Unsupported WGL timeplate entry at line {number}')
                self.add_wave(match, number)
                continue
            if mode == 'header':
                end = line.find(')')
                if end < 0:
                    buf.append(line)
                    size += len(line)
                    if size > 8 * 1024 * 1024:
                        raise ValueError('WGL pattern header exceeds supported size')
                    continue
                buf.append(line[:end])
                self.header_done(''.join(buf))
                mode, buf = 'pattern', []
                continue
            if mode == 'pattern':
                if low == 'end':
                    if self.pending_repeat is not None:
                        raise ValueError(f'Dangling WGL repeat at line {number}')
                    if self.loops:
                        self.loop_done(number)
                        continue
                    self.patterns += 1
                    mode = None
                    continue
                match = re.fullmatch(r'repeat\s+(\d+)', text, re.I)
                if match:
                    self.pending_repeat = int(match[1])
                    continue
                match = re.fullmatch(r'loop\s+(\d+)', text, re.I)
                if match:
                    if self.pending_repeat is not None or int(match[1]) < 1:
                        raise ValueError(f'Unsupported WGL loop at line {number}')
                    self.loops.append([int(match[1]), [], 0])
                    self.loop_count += 1
                    continue
                if SE_CMD.fullmatch(text):
                    if self.pending_repeat is not None:
                        raise ValueError(f'SE_CMD label after repeat at line {number}')
                    self.se_cmd_labels += 1
                    continue
                match = STATEMENT.match(text)
                if not match:
                    raise ValueError(f'Unsupported WGL pattern statement at line {number}: {text[:60]}')
                self.start_statement(match, number)
                rest = text[match.end():]
                end = rest.find(']')
                if end < 0:
                    mode, buf = 'values', [rest + '\n']
                else:
                    mode = self.values_done(rest[:end], rest[end + 1:], number)
                continue
            # Top level: section starts; signal lists and other declarations are ignored.
            if low == 'scanchain':
                mode, buf, size, chain_line = 'chain', [], 0, number
            elif low == 'scanstate':
                mode = 'scanstate'
            elif low.startswith('equationsheet'):
                mode, depth = 'equations', 0
            elif low.startswith('timeplate'):
                match = PERIOD.match(text)
                if not match:
                    raise ValueError(f'Unsupported WGL timeplate header at line {number}')
                self.periods[match[1]] = match[2]
                mode = 'timeplate'
            elif PATTERN.match(text):
                rest = text[PATTERN.match(text).end():]
                if ')' in rest:
                    self.header_done(rest[:rest.index(')')])
                    mode = 'pattern'
                else:
                    mode, buf, size = 'header', [rest + '\n'], 0
            elif low == 'end' and self.patterns:
                self.closed = True
        if mode is not None or not self.patterns or not self.cycles or not self.closed:
            raise ValueError('Truncated or incomplete WGL (section, pattern, statement or final end missing)')

    def chains_done(self, text, number):
        chains = list(CHAIN.finditer(text))
        if not chains or CHAIN.sub('', text).strip():
            raise ValueError('Unsupported WGL scanchain syntax')
        for chain in chains:
            endpoints = QUOTED_PIN.findall(chain[2])
            if len(endpoints) < 2:
                raise ValueError('WGL scanchain lacks input/output endpoints')
            for role, (name, index) in zip(('ScanIn', 'ScanOut'), (endpoints[0], endpoints[-1])):
                raw = '"' + name + '"' + ('[' + index + ']' if index else '')
                self.rows.append(pin_result(chain[1], role, raw, number, self.groups, self.normalize))

    def add_wave(self, match, number):
        pin, direction = self.normalize(match[1]), match[2].lower()
        events = []
        for item in match[3].split(','):
            when, _, code = item.strip().rpartition(':')
            if not when or not code.strip():
                raise ValueError(f'Unsupported WGL waveform event at line {number}')
            events.append((_time(when, self.equations), code.strip().upper()))
        codes = [code for _, code in events]
        # Return-format input: drives vector data (S) then returns to a fixed level.
        clock = (direction == 'input' and 'S' in codes
                 and any(code in ('D', 'U') for code in codes[codes.index('S') + 1:]))
        old = self.waves.get(pin)
        if old and old['clock'] != clock:
            raise ValueError(f'Inconsistent waveform class for {pin} across timeplates')
        self.waves[pin] = {'direction': direction, 'clock': clock, 'events': events}

    def header_done(self, text):
        self.order = [self.normalize('"' + name + '"' + ('[' + idx + ']' if idx else ''))
                      for name, idx in QUOTED_PIN.findall(text)]
        if not self.order or len(set(self.order)) != len(self.order):
            raise ValueError('Empty or duplicate WGL pattern pin order')
        self.tck_idx = tuple(i for i, pin in enumerate(self.order) if TCK.fullmatch(pin)
                             and self.waves.get(pin, {}).get('direction') == 'input')
        self.clock_idx = tuple(i for i, pin in enumerate(self.order) if self.waves.get(pin, {}).get('clock'))

    def start_statement(self, match, number):
        if self.pending_tc is not None:
            if self.pending_tc != self.cycles:
                raise ValueError(f'TesterCycle annotation {self.pending_tc} disagrees with '
                                 f'computed cycle {self.cycles} at line {number}')
            self.verified += 1
            self.pending_tc = None
        self.repeat = int(match[1]) if match[1] else (self.pending_repeat or 1)
        self.pending_repeat = None
        if self.repeat < 1:
            raise ValueError(f'Invalid WGL repeat count at line {number}')
        self.kind = match[2].lower()
        self.tables.add(match[3])

    def values_done(self, text, after, number):
        values = text.split()
        if len(values) != len(self.order):
            raise ValueError(f'WGL vector width {len(values)} does not match pattern order '
                             f'{len(self.order)} at line {number}')
        tck = {values[i] for i in self.tck_idx}
        value = tck.pop() if len(tck) == 1 else '?'
        self.tck_value = (value if value in ('0', '1') else '?') if self.tck_idx else None
        self.clock_hits = [self.order[i] for i in self.clock_idx if values[i] == '1']
        after = ANNOTATION.sub(' ', after).strip()
        if self.kind == 'vector':
            if after != ';':
                raise ValueError(f'Unsupported text after WGL vector at line {number}')
            self.account(self.repeat)
            return 'pattern'
        if not after.startswith(','):
            raise ValueError(f'Unsupported WGL scan statement at line {number}')
        self.tail = [after]
        if ';' in after:
            self.scan_done(number)
            return 'pattern'
        return 'tail'

    def scan_done(self, number):
        text = ''.join(self.tail)
        refs = SCAN_REF.findall(text)
        if not refs or SCAN_REF.sub('', text).replace(',', ' ').strip() != ';':
            raise ValueError(f'Unsupported WGL scan data references at line {number}')
        missing = [state for _, _, state in refs if state not in self.states]
        if missing:
            raise ValueError(f'Undefined WGL scanstate {missing[0]} at line {number}')
        if self.repeat != 1:
            raise ValueError(f'Unsupported repeat on WGL scan statement at line {number}')
        self.account(max(self.states[state] for _, _, state in refs))

    def account(self, count):
        # Cycles advance immediately (first loop iteration) so TesterCycle checks stay exact.
        self.cycles += count
        self.statements += 1
        item = (self.tck_value, count, tuple(self.clock_hits))
        if self.loops:
            self.loops[-1][1].append(item)
            self.loops[-1][2] += count
        else:
            self.apply([item], 1)

    def loop_done(self, number):
        times, items, cycles = self.loops.pop()
        if not items:
            raise ValueError(f'Empty WGL loop at line {number}')
        self.cycles += (times - 1) * cycles
        if self.loops:
            self.loops[-1][1].append((times, items))
            self.loops[-1][2] += times * cycles
        else:
            self.apply(items, times)

    def apply(self, items, times):
        """Replay executed statements (with nested loops) into TCK/clock statistics."""
        for _ in range(times):
            for item in items:
                if len(item) == 2:
                    self.apply(item[1], item[0])
                    continue
                tck_value, count, clocks = item
                if tck_value is not None:
                    self.tck.add(tck_value, count)
                for name in self.clock_run:
                    if name not in clocks:
                        self.clock_run[name] = 0
                if clocks:
                    self.clock_one_cycles += count
                    self.clocks_pulsed.update(clocks)
                    for name in clocks:
                        run = self.clock_run[name] = self.clock_run.get(name, 0) + count
                        if run > self.clock_best.get(name, 0):
                            self.clock_best[name] = run


def check_wgl(path, groups, normalize):
    """Check WGL chain endpoints and stream every executed statement to EOF.

    WGL chain endpoints are input (first) and output (last); internal cells
    are not tester pins. Vector values are mapped by the pattern's declared
    pin order, which is independent of signal declaration order.
    """
    path = Path(path)
    parser = _Parser(groups, normalize)
    error = None
    try:
        with open_text(path) as stream:
            parser.run(stream)
    except (OSError, EOFError, UnicodeError, ValueError, gzip.BadGzipFile) as exc:
        error = str(exc)
    rows = parser.rows
    tck_names = sorted(parser.order[i] for i in parser.tck_idx)
    clock_names = sorted(parser.order[i] for i in parser.clock_idx)
    clock_windows = {}
    for name in clock_names:
        events = parser.waves[name]['events']
        codes = [code for _, code in events]
        start = codes.index('S')
        stop = next(i for i in range(start + 1, len(codes)) if codes[i] in ('D', 'U'))
        clock_windows[name] = [events[start][0], events[stop][0]]
    status = 'ERROR' if error else 'FAIL' if any(not p['passed'] for p in rows) else 'PASS' if rows else 'UNDETERMINED'
    return {'file': str(path), 'status': status, 'error': error or (None if rows else 'No WGL scan chain endpoints found'),
            'blocks': int(bool(rows)), 'chains': len(rows) // 2, 'pins': rows,
            'absent_fields': [], 'clocks_info_only': sorted(set(clock_names) | set(tck_names)),
            '_wgl_timing': {'periods': parser.periods, 'equations': parser.equations,
                            'tables': sorted(parser.tables), 'tck_names': tck_names,
                            'clock_names': clock_names, 'clock_windows_ps': clock_windows,
                            'tck': parser.tck.finish(), 'clock_one_cycles': parser.clock_one_cycles,
                            'clocks_pulsed': sorted(parser.clocks_pulsed),
                            'clock_max_consecutive_one': dict(sorted(parser.clock_best.items())),
                            'cycles': parser.cycles,
                            'statements': parser.statements, 'loops': parser.loop_count,
                            'se_cmd_labels': parser.se_cmd_labels,
                            'tester_cycles_verified': parser.verified}}


def timing_wgl(result, die):
    """Check user-specified WGL symbolic criteria, not physical edge simulation."""
    if result['status'] == 'ERROR':
        return {'status': 'ERROR', 'reason': 'WGL parse failed; timing not validated: ' + str(result['error']),
                'criteria': 'WGL symbolic vector check only; no physical clock-edge/frequency proof'}
    info = result['_wgl_timing']
    resolved = {name: _time(info['periods'].get(name, ''), info['equations']) for name in info['tables']}
    reasons, fails = [], []
    if not resolved or any(period is None for period in resolved.values()):
        reasons.append('Missing or unresolved active WGL timeplate period')
    elif any(period != TARGET_PERIOD_PS for period in resolved.values()):
        fails.append('Active WGL timeplate period is not 10ns')
    name = Path(result['file']).name
    # A chain pattern contains both setup (TCK) and body (scan clock) content; both rules must pass.
    chain = bool(re.search(r'(?:^|_)chain(?:_|$)', name, re.I))
    setup = chain or bool(re.search(r'(?:^|_)setup(?:_|$)', name, re.I))
    body = chain or bool(re.search(r'(?:^|_)body(?:_|$)', name, re.I))
    kind = 'chain(setup+body)' if chain else '+'.join(k for k, v in (('setup', setup), ('body', body)) if v) or None
    tck = info['tck']
    if setup:
        if not info['tck_names']:
            reasons.append('No input TCK/TCK_IN in the WGL timeplate and pattern order')
        elif not tck['toggles']:
            fails.append('TCK never toggles between 0 and 1')
    if body:
        windows = info['clock_windows_ps']
        best = max(info['clock_max_consecutive_one'].values(), default=0)
        if not info['clock_names']:
            reasons.append('No return-format (pulse) input clock in the WGL timeplate')
        elif not info['clock_one_cycles']:
            fails.append('No scan clock 1 in any executed body cycle')
        elif best < MIN_SCAN_CLOCK_RUN:
            fails.append(f'No scan clock 1 for >= {MIN_SCAN_CLOCK_RUN} consecutive cycles')
        elif any(None in window or not 0 <= window[0] < window[1] <= TARGET_PERIOD_PS
                 for window in windows.values()):
            reasons.append(f'Scan clock pulse window unresolved or outside the period: {windows}')
    if not (body or setup):
        reasons.append('No setup/body/chain classification for user-specified clock-symbol rules')
    status = 'FAIL' if fails else 'UNDETERMINED' if reasons else 'PASS'
    return {'status': status, 'classification': kind,
            'period_ps': resolved, 'target_period_ps': TARGET_PERIOD_PS,
            'jtag_target_mhz': 10 if die == 'CCD' else 25, 'scan_target_mhz': 100,
            'cycles': info['cycles'], 'statements': info['statements'], 'loops': info['loops'],
            'se_cmd_labels': info['se_cmd_labels'],
            'tester_cycles_verified': info['tester_cycles_verified'],
            'tck_pins': info['tck_names'], 'tck': tck,
            'scan_clock_pins': info['clock_names'], 'scan_clock_windows_ps': info['clock_windows_ps'],
            'scan_clock_one_cycles': info['clock_one_cycles'], 'scan_clocks_pulsed': info['clocks_pulsed'],
            'scan_clock_max_consecutive_one': info['clock_max_consecutive_one'],
            'criteria': ('Full-file WGL symbolic check: 10ns period; setup TCK toggles 0/1 at least once '
                         '(five 0/five 1 run stats are informational); body has one scan clock at 1 '
                         f'for >= {MIN_SCAN_CLOCK_RUN} consecutive cycles; chain = setup + body (both required). '
                         'Not an analog/physical edge measurement.'),
            'reason': '; '.join(fails + reasons) if fails or reasons else 'Specified WGL symbolic criteria satisfied'}
