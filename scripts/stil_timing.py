"""Streaming STIL grammar and base execution primitives for stil_checks.

No pattern text/edge trace is retained. Declarations, small Loop/Macro bodies,
bounded data caches and edge histograms are retained; unsupported constructs fail
closed. The public timing check and optimized engine live in stil_checks; this
module never imports that engine or the CLI, avoiding circular dependencies.
"""
import ast
from collections import Counter
from fractions import Fraction
from functools import lru_cache
from io import StringIO
from pathlib import Path
import re


class StilError(ValueError):
    pass


MAX_TEXT = 4 * 1024 * 1024
MAX_DATA = 1_000_000
MAX_BODY = 100_000
TOKEN = re.compile(r'''\s+|//[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|\{\*[\s\S]*?\*\}|"[^"\\]*"|'[^']*'|\\r\d+|[\w.$]+|[{};=+/\[\]:#()\-]''')


def tokens(stream):
    """Chunk lexer, including multiline quotes/comments and gzip EOF/CRC read."""
    buf, pos, eof = '', 0, False
    while True:
        match = TOKEN.match(buf, pos)
        if ((buf.startswith('/*', pos) and buf.find('*/', pos + 2) < 0) or
                (buf.startswith('{*', pos) and buf.find('*}', pos + 2) < 0)):
            match = None
        if match is not None and (match.end() < len(buf) or eof):
            word = match[0]
            pos = match.end()
            if word.startswith('{*'):
                yield '<annotation>'
            elif not word.isspace() and not word.startswith(('//', '/*')):
                yield word
            continue
        if eof:
            if pos != len(buf):
                raise StilError('Unterminated quote/comment or unsupported token: ' + repr(buf[pos:pos + 80]))
            return
        buf = buf[pos:]
        if len(buf) > MAX_TEXT:
            raise StilError('Token exceeds streaming size limit')
        part = stream.read(65536)
        buf += part
        pos, eof = 0, not part


class Reader:
    def __init__(self, stream):
        self.iterator = iter(tokens(stream))
        self.current = next(self.iterator, None)

    def pop(self, expected=None):
        word = self.current
        if word is None:
            raise StilError('Truncated STIL: unexpected EOF')
        if expected is not None and word != expected:
            raise StilError(f'Expected {expected!r}, found {word!r}')
        self.current = next(self.iterator, None)
        return word

    def accept(self, word):
        if self.current == word:
            self.pop()
            return True
        return False

    def name(self):
        word = self.pop()
        if word.startswith('"'):
            word = word[1:-1]
        elif not re.fullmatch(r'[A-Za-z_][\w.$]*', word):
            raise StilError('Expected a name: ' + word)
        if self.accept('['):
            index = self.pop()
            if not index.isdecimal():
                raise StilError('Only individual numeric bus indices are supported')
            self.pop(']')
            word += '[' + str(int(index)) + ']'
        return word

    def expression(self):
        word = self.pop()
        if not word.startswith("'"):
            raise StilError('Expected single-quoted expression')
        return word[1:-1]

    def block_name(self):
        name = '' if self.current == '{' else self.name()
        self.pop('{')
        return name


def _expand_data(raw):
    """Expand waveform characters, not levels. Repeat only the following WFC."""
    if raw == '#':
        return raw
    chunks, size = [], 0
    words = iter(raw.split())
    for word in words:
        if word.startswith('\\r'):
            count = int(word[2:])
            char = next(words, '')
            if count < 1 or len(char) != 1 or not char.isalnum():
                raise StilError('Unsupported repeat data: ' + raw[:80])
            if size + count > MAX_DATA:
                raise StilError('Expanded data exceeds size limit')
            word = char * count
        elif not re.fullmatch(r'[A-Za-z0-9]+', word):
            raise StilError('Unsupported vector data: ' + word)
        size += len(word)
        if size > MAX_DATA:
            raise StilError('Expanded data exceeds size limit')
        chunks.append(word)
    if not chunks:
        raise StilError('Empty assignment')
    return ''.join(chunks)


@lru_cache(maxsize=256)
def _small_data(raw):
    return _expand_data(raw)


def data_value(raw):
    # Cache typical pi/po/630-bit channel strings, not megabyte expansions.
    # 256 entries * <=16K decoded chars keeps retained decoded text bounded.
    if len(raw) <= 4096 and sum(int(n) for n in re.findall(r'\\r(\d+)', raw)) + len(raw) <= 16384:
        return _small_data(raw)
    return _expand_data(raw)


def assignments(r):
    r.pop('{')
    result = {}
    while not r.accept('}'):
        name = r.name()
        if name in result:
            raise StilError('Duplicate assignment: ' + name)
        r.pop('=')
        parts = []
        while r.current != ';':
            parts.append(r.pop())
            if len(parts) > MAX_BODY:
                raise StilError('Assignment exceeds size limit')
        r.pop(';')
        result[name] = data_value(' '.join(parts))
    return result


def statement(r, depth=0):
    if depth > 32:
        raise StilError('Execution nesting limit exceeded')
    op = r.pop()
    if r.current == ':' and (op.startswith('"') or re.fullmatch(r'[A-Za-z_][\w.$]*', op)):
        r.pop(':')
        return ('Label', op)  # No control flow; Goto/Call remain unsupported.
    if op == 'Ann':
        r.pop('<annotation>')
        return ('Ann',)
    if op == 'W':
        name = r.name()
        r.pop(';')
        return (op, name)
    if op == 'V':
        return (op, assignments(r))
    if op == 'Macro':
        return (op, r.name(), assignments(r))
    if op in ('Loop', 'Shift'):
        count = r.pop() if op == 'Loop' else None
        if count is not None and (not count.isdecimal() or not 0 < int(count) <= 100_000_000):
            raise StilError('Unsupported Loop count')
        r.pop('{')
        body = body_statements(r, depth + 1)
        return (op, int(count), body) if count is not None else (op, body)
    raise StilError('Unsupported executable STIL statement: ' + op)


def body_statements(r, depth=0):
    result = []
    while not r.accept('}'):
        result.append(statement(r, depth))
        if len(result) > MAX_BODY:
            raise StilError('Loop/Macro body exceeds bounded storage limit')
    return result


def expression_value(text, definitions, stack=()):
    """Exact arithmetic in ps; never eval source text."""
    text = re.sub(r'(\d+(?:\.\d+)?)\s*(ps|ns|us|ms|s)\b',
                  lambda m: '(' + m[1] + '*' + str({'ps': 1, 'ns': 1000, 'us': 10**6,
                                                   'ms': 10**9, 's': 10**12}[m[2]]) + ')', text)
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Fraction(str(node.value))
        if isinstance(node, ast.Name):
            if node.id in stack or node.id not in definitions:
                raise StilError('Unresolved/cyclic Spec parameter: ' + node.id)
            return expression_value(definitions[node.id], definitions, stack + (node.id,))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise StilError('Unsupported timing expression: ' + text)
    try:
        return visit(ast.parse(text.strip(), mode='eval').body)
    except (SyntaxError, ZeroDivisionError, RecursionError) as exc:
        raise StilError('Invalid timing expression: ' + text) from exc


def increment(hist, key):
    if key not in hist and len(hist) >= 256:
        raise StilError('Edge evidence exceeds histogram limit; cannot summarize safely')
    hist[key] += 1


class Clock:
    def __init__(self, name, target, scan):
        self.name, self.target, self.scan = name, target, scan
        self.level = None
        self.rise = self.fall = None
        self.last_rise_cycle = None
        self.streak = self.max_streak = self.rises = self.falls = 0
        self.intervals, self.pairs, self.highs, self.lows = Counter(), Counter(), Counter(), Counter()
        self.adjacent_bad = self.adjacent_good = self.unknown_vectors = 0
        self.examples = []

    def event(self, time, level, cycle):
        if level not in (0, 1):
            self.level = self.rise = self.fall = self.last_rise_cycle = None
            self.streak = 0
            return
        old = self.level
        self.level = level
        if old is None or old == level:
            return
        if len(self.examples) < 8:
            self.examples.append({'time_ps': time, 'edge': 'rise' if level else 'fall', 'cycle': cycle})
        if level:
            self.rises += 1
            if self.fall is not None:
                increment(self.lows, time - self.fall)
            if self.rise is not None:
                interval = time - self.rise
                increment(self.intervals, interval)
                if self.fall is not None:
                    increment(self.pairs, (self.fall - self.rise, time - self.fall))
                if self.last_rise_cycle == cycle - 1:
                    self.streak += 1
                    if interval == self.target:
                        self.adjacent_good += 1
                    else:
                        self.adjacent_bad += 1
                else:
                    self.streak = 1
            else:
                self.streak = 1
            self.max_streak = max(self.max_streak, self.streak)
            self.rise, self.last_rise_cycle = time, cycle
            self.fall = None
        else:
            self.falls += 1
            if self.rise is not None:
                increment(self.highs, time - self.rise)
            self.fall = time

    def report(self):
        nominal = [(high, low) for high, low in self.pairs if high + low == self.target]
        # A stretched single plateau is an idle hold, not a slower clock. A
        # different complete duty/period pair is not silently called a gap.
        bad, gaps = 0, 0
        for (high, low), count in self.pairs.items():
            if high + low == self.target:
                continue
            if high + low > self.target and any((high == h and low >= l) or
                                                (low == l and high >= h) for h, l in nominal):
                gaps += count
            else:
                bad += count
        status = 'PASS'
        if self.unknown_vectors:
            status = 'UNDETERMINED'
        elif not nominal or bad or (self.scan and (not self.adjacent_good or self.adjacent_bad)):
            status = 'FAIL'
        return {'status': status, 'target_period_ps': self.target,
                'rising_edges': self.rises, 'falling_edges': self.falls,
                'rising_intervals_ps': dict(sorted(self.intervals.items())),
                'high_widths_ps': dict(sorted(self.highs.items())),
                'low_widths_ps': dict(sorted(self.lows.items())),
                'complete_pulse_pairs': [{'high_ps': h, 'low_ps': l, 'count': n}
                                        for (h, l), n in sorted(self.pairs.items())],
                'target_intervals': self.intervals[self.target], 'idle_gap_intervals': gaps,
                'inconsistent_intervals': bad, 'consecutive_pulse_intervals': self.adjacent_good,
                'wrong_consecutive_intervals': self.adjacent_bad,
                'max_consecutive_pulse_vectors': self.max_streak,
                'unknown_assignment_vectors': self.unknown_vectors, 'final_level': self.level,
                'edge_examples': self.examples}


class Interpreter:
    def __init__(self, die):
        self.die = die
        self.signals, self.groups, self.resolved = {}, {}, {}
        self.categories, self.timings, self.macros, self.bursts = {}, {}, {}, {}
        self.scan_clocks = set()
        self.selection = None
        self.literal_periods = set()
        self.engine_ready = False
        self.clocks, self.state, self.tables = {}, {}, {}
        self.table = None
        self.cycles = self.time = self.v_statements = self.calls = self.shift_cycles = 0
        self.used_tables = Counter()
        self.patterns = []

    @staticmethod
    def unique(mapping, name, value):
        if name in mapping:
            raise StilError('Duplicate/ambiguous declaration: ' + name)
        if len(mapping) >= MAX_BODY:
            raise StilError('Declaration count exceeds bounded storage limit')
        mapping[name] = value

    def members(self, name, stack=()):
        if name in self.signals:
            return (name,)
        if name in self.resolved:
            return self.resolved[name]
        if name not in self.groups or name in stack:
            raise StilError('Unknown/cyclic signal group: ' + name)
        values = []
        for member in self.groups[name]:
            child = self.members(member, stack + (name,))
            if len(values) + len(child) > MAX_DATA:
                raise StilError('Expanded signal group exceeds size limit: ' + name)
            values.extend(child)
        values = tuple(values)
        if not values or len(values) != len(set(values)):
            raise StilError('Empty/duplicate/oversized signal group: ' + name)
        self.resolved[name] = values
        return values

    def read_header(self, r):
        r.pop('{')
        while not r.accept('}'):
            kind = r.pop()
            if kind in ('Title', 'Date', 'Source'):
                if not r.pop().startswith('"'):
                    raise StilError('Invalid Header string')
                r.pop(';')
            elif kind == 'History':
                r.pop('{')
                while not r.accept('}'):
                    r.pop('Ann')
                    r.pop('<annotation>')
            elif kind == 'Ann':
                r.pop('<annotation>')
            else:
                raise StilError('Unsupported Header entry: ' + kind)

    def read_signals(self, r):
        r.pop('{')
        while not r.accept('}'):
            name, direction = r.name(), r.pop()
            if direction not in ('In', 'Out', 'InOut', 'Supply', 'Pseudo'):
                raise StilError('Unsupported signal direction: ' + direction)
            r.pop(';')
            self.unique(self.signals, name, direction)

    def read_groups(self, r):
        r.pop('{')  # Named SignalGroups domains are intentionally not guessed.
        while not r.accept('}'):
            name = r.name()
            r.pop('=')
            expr = Reader(StringIO(r.expression()))
            members = [expr.name()]
            while expr.accept('+'):
                members.append(expr.name())
            if expr.current is not None:
                raise StilError('Unsupported signal group expression')
            self.unique(self.groups, name, members)
            if r.accept('{'):
                role = r.pop()
                if role not in ('ScanIn', 'ScanOut') or not r.pop().isdecimal():
                    raise StilError('Unsupported SignalGroups attributes')
                r.pop(';')
                r.pop('}')
            else:
                r.pop(';')

    def read_spec(self, r):
        r.block_name()
        while not r.accept('}'):
            r.pop('Category')
            category = r.name()
            r.pop('{')
            values = {}
            while not r.accept('}'):
                name = r.name()
                r.pop('=')
                self.unique(values, name, r.expression())
                r.pop(';')
            self.unique(self.categories, category, values)

    def read_timing(self, r):
        name = r.block_name()
        tables = {}
        while not r.accept('}'):
            r.pop('WaveformTable')
            table = r.name()
            r.pop('{')
            r.pop('Period')
            period = r.expression()
            try:
                value = expression_value(period, {})
                if value.denominator == 1:
                    self.literal_periods.add(int(value))
            except StilError:
                pass
            r.pop(';')
            waves = []
            if r.accept('Waveforms'):
                r.pop('{')
                while not r.accept('}'):
                    group = r.name()
                    r.pop('{')
                    while not r.accept('}'):
                        chars = r.pop()
                        if not re.fullmatch(r'[A-Za-z0-9]+', chars) or len(set(chars)) != len(chars):
                            raise StilError('Unsupported waveform character list')
                        r.pop('{')
                        events = []
                        while not r.accept('}'):
                            time = r.expression()
                            levels = [r.pop()]
                            while r.accept('/'):
                                levels.append(r.pop())
                            r.pop(';')
                            if len(levels) not in (1, len(chars)) or any(x not in 'DUZLHXTlhxt' or len(x) != 1 for x in levels):
                                raise StilError('Unsupported waveform events')
                            events.append((time, levels))
                        waves.append((group, chars, events))
                r.pop('}')  # WaveformTable
            else:
                r.pop('}')
            self.unique(tables, table, (period, waves))
        self.unique(self.timings, name, tables)

    def read_scan(self, r):
        r.block_name()
        while not r.accept('}'):
            r.pop('ScanChain')
            r.name()
            r.pop('{')
            seen = set()
            while not r.accept('}'):
                kind = r.pop()
                if kind in seen:
                    raise StilError('Duplicate ScanChain field: ' + kind)
                seen.add(kind)
                if kind == 'ScanMasterClock':
                    while r.current != ';':
                        self.scan_clocks.add(r.name())
                elif kind in ('ScanIn', 'ScanOut'):
                    r.name()
                elif kind in ('ScanLength', 'ScanInversion'):
                    if not r.pop().isdecimal():
                        raise StilError('Unsupported ScanChain number')
                else:
                    raise StilError('Unsupported ScanChain field: ' + kind)
                r.pop(';')

    def prepare(self):
        if not self.selection:
            raise StilError('Pattern execution requires explicit PatternExec selection')
        if set(self.signals) & set(self.groups):
            raise StilError('Ambiguous signal/group name')
        for name in self.groups:
            self.members(name)
        for pin in self.scan_clocks:
            if pin not in self.signals or self.signals[pin] not in ('In', 'InOut'):
                raise StilError('ScanMasterClock is not a declared input: ' + pin)
        tcks = [p for p in self.signals if p.upper() == 'TCK_IN']
        if len(tcks) > 1:
            raise StilError('Ambiguous TCK_IN')
        self.clocks = {p: Clock(p, 10000, True) for p in sorted(self.scan_clocks)}
        for pin in tcks:
            if pin in self.clocks:
                raise StilError('TCK is also a scan clock; roles are ambiguous')
            self.clocks[pin] = Clock(pin, 100000 if self.die == 'CCD' else 40000, False)
        category = self.selection.get('Category')
        if category is not None and category not in self.categories:
            raise StilError('Unknown selected Spec Category: ' + category)
        definitions = self.categories.get(category, {})
        domain = self.selection.get('Timing', '')
        if domain not in self.timings:
            raise StilError('Unknown selected Timing domain: ' + domain)
        def ps(text):
            value = expression_value(text, definitions)
            if value.denominator != 1:
                raise StilError('Sub-picosecond timing is unsupported')
            return int(value)
        for table, (raw_period, waves) in self.timings[domain].items():
            period = ps(raw_period)
            if period <= 0:
                raise StilError('Non-positive waveform period')
            mapping = {}
            for group, chars, raw_events in waves:
                members = self.members(group)
                times = [ps(expr) for expr, _ in raw_events]
                if not times or times != sorted(set(times)) or min(times) < 0 or max(times) >= period:
                    raise StilError('Waveform events must be ordered and within Period')
                for index, char in enumerate(chars):
                    events = tuple((time, levels[0 if len(levels) == 1 else index])
                                   for time, (_, levels) in zip(times, raw_events))
                    for pin in members:
                        if pin not in self.clocks:
                            continue
                        if any(level not in ('D', 'U', 'Z') for _, level in events):
                            raise StilError('Clock waveform has unsupported drive events: ' + pin)
                        self.unique(mapping, (pin, char), tuple((time, {'D': 0, 'U': 1, 'Z': None}[level])
                                                               for time, level in events))
            self.tables[table] = period, mapping
        self.plans = {name: (len(self.members(name)), tuple((i, pin) for i, pin in enumerate(self.members(name))
                                                         if pin in self.clocks))
                      for name in (*self.signals, *self.groups)}
        self.engine_ready = True

    def updates(self, values):
        result, assigned = {}, set()
        for name, data in values.items():
            if name not in self.plans:
                raise StilError('Unknown assignment group/signal: ' + name)
            width, positions = self.plans[name]
            if data == '#' or len(data) != width:
                raise StilError(f'Assignment width mismatch or unresolved #: {name}: {len(data)} != {width}')
            # Reject overlapping assignments even on untracked signals.
            members = self.members(name)
            if len(values) > 1:
                if assigned.intersection(members):
                    raise StilError('Overlapping vector assignments')
                assigned.update(members)
            result.update((pin, data[i]) for i, pin in positions)
        return result

    def vector(self, updates):
        if self.table not in self.tables:
            raise StilError('Missing/unknown active WaveformTable: ' + str(self.table))
        self.state.update(updates)
        period, waveforms = self.tables[self.table]
        self.used_tables[self.table] += 1
        for pin, clock in self.clocks.items():
            char = self.state.get(pin)
            if char is None:
                clock.unknown_vectors += 1
                continue
            events = waveforms.get((pin, char))
            if events is None:
                raise StilError(f'Undefined clock waveform {pin}={char} in {self.table}')
            for offset, level in events:
                clock.event(self.time + offset, level, self.cycles)
        self.cycles += 1
        self.time += period

    def execute(self, stmt, depth=0):
        if depth > 32:
            raise StilError('Recursive Macro/Loop execution limit')
        op = stmt[0]
        if op in ('Ann', 'Label'):
            return
        if op == 'W':
            if stmt[1] not in self.tables:
                raise StilError('Unknown selected waveform table: ' + stmt[1])
            self.table = stmt[1]
        elif op == 'V':
            self.v_statements += 1
            self.vector(self.updates(stmt[1]))
        elif op == 'Loop':
            for _ in range(stmt[1]):
                for child in stmt[2]:
                    self.execute(child, depth + 1)
        elif op == 'Macro':
            self.calls += 1
            if stmt[1] not in self.macros:
                raise StilError('Undefined macro: ' + stmt[1])
            body, supplied = self.macros[stmt[1]], stmt[2]
            shifts = [child for child in body if child[0] == 'Shift']
            if len(shifts) != 1 or any(child[0] not in ('Ann', 'W', 'V', 'Shift') for child in body):
                raise StilError('Supported Macro requires exactly one top-level Shift')
            shift = [child for child in shifts[0][1] if child[0] != 'Ann']
            if len(shift) != 1 or shift[0][0] != 'V':
                raise StilError('Supported Shift requires exactly one V')
            fields = shift[0][1]
            channels = {key for key, value in fields.items() if value == '#'}
            if not channels or channels != set(supplied):
                raise StilError('Macro arguments must match Shift # channels exactly')
            if any(self.plans.get(key, (0,))[0] != 1 for key in channels):
                raise StilError('Only scalar parallel Shift channels are supported')
            lengths = {len(data) for data in supplied.values()}
            if len(lengths) != 1 or '#' in supplied.values():
                raise StilError('Parallel Shift channel lengths differ or contain #')
            count = lengths.pop()
            for child in body:
                if child[0] != 'Shift':
                    self.execute(child, depth + 1)
                    continue
                fixed = {key: value for key, value in fields.items() if key not in channels}
                for index in range(count):
                    values = dict(fixed)
                    values.update((key, supplied[key][index]) for key in channels)
                    self.vector(self.updates(values))
                self.shift_cycles += count
        else:
            raise StilError('Shift outside executed Macro is unsupported')

    def parse(self, stream):
        r = Reader(stream)
        r.pop('STIL')
        r.pop('1.0')
        r.pop(';')
        while r.current is not None:
            kind = r.pop()
            if kind == 'Ann':
                r.pop('<annotation>')
                continue
            if self.patterns and kind != 'Pattern':
                raise StilError('Declarations/trailing syntax after Pattern are unsupported: ' + kind)
            handler = {'Header': self.read_header, 'Signals': self.read_signals,
                       'SignalGroups': self.read_groups, 'Spec': self.read_spec,
                       'Timing': self.read_timing, 'ScanStructures': self.read_scan}.get(kind)
            if handler:
                handler(r)
            elif kind == 'MacroDefs':
                if r.block_name():
                    raise StilError('Named MacroDefs domains are unsupported')
                while not r.accept('}'):
                    name = r.name()
                    r.pop('{')
                    self.unique(self.macros, name, body_statements(r))
            elif kind == 'PatternBurst':
                name = r.name()
                r.pop('{')
                r.pop('PatList')
                r.pop('{')
                pattern = r.name()
                r.pop(';')
                r.pop('}')
                r.pop('}')
                self.unique(self.bursts, name, pattern)
            elif kind == 'PatternExec':
                if self.selection is not None:
                    raise StilError('Multiple PatternExec selections are unsupported')
                r.block_name()
                self.selection = {}
                while not r.accept('}'):
                    key = r.pop()
                    if key not in ('Category', 'Timing', 'PatternBurst'):
                        raise StilError('Unsupported PatternExec selector: ' + key)
                    self.unique(self.selection, key, r.name())
                    r.pop(';')
            elif kind == 'Pattern':
                if self.patterns:
                    raise StilError('Multiple Patterns are unsupported')
                self.prepare()
                name = r.name()
                if self.bursts.get(self.selection.get('PatternBurst')) != name:
                    raise StilError('Pattern does not match selected PatternBurst/PatList')
                r.pop('{')
                self.patterns.append(name)
                while not r.accept('}'):
                    self.execute(statement(r))
            else:
                raise StilError('Unsupported top-level STIL construct: ' + kind)

    def report(self, path):
        kinds = set(re.findall(r'(?:^|_)(setup|body|chain)(?=_|\.)', Path(path).name.lower()))
        classification = next(iter(kinds)) if len(kinds) == 1 else None
        rows = {pin: clock.report() for pin, clock in self.clocks.items()}
        required = [pin for pin, clock in self.clocks.items()
                    if (clock.scan and classification in ('body', 'chain')) or
                    (not clock.scan and classification in ('setup', 'chain'))]
        missing = (classification in ('body', 'chain') and not self.scan_clocks or
                   classification in ('setup', 'chain') and not any(not c.scan for c in self.clocks.values()))
        status = 'PASS'
        reason = 'Executed waveform edges match target cadence; stretched idle holds and final levels are allowed'
        if not self.patterns or not self.cycles or not classification or missing or not required:
            status, reason = 'UNDETERMINED', 'Missing execution, classification or required clock declarations; Period alone is insufficient'
        elif any(rows[p]['status'] == 'UNDETERMINED' for p in required):
            status, reason = 'UNDETERMINED', 'Required clock assignments are not fully known'
        elif any(rows[p]['status'] == 'FAIL' for p in required):
            status, reason = 'FAIL', 'Required clocks lack target edge intervals/consecutive scan pulses or have inconsistent cadence'
        return {'status': status, 'reason': reason, 'classification': classification,
                'full_file_checked': True, 'selection': self.selection,
                'periods_ps': sorted({self.tables[t][0] for t in self.used_tables} or self.literal_periods),
                'jtag_target_mhz': 10 if self.die == 'CCD' else 25, 'scan_target_mhz': 100,
                'cycles': self.cycles, 'duration_ps': self.time, 'vector_executions_outside_shift': self.v_statements,
                'macro_calls': self.calls, 'shift_cycles': self.shift_cycles,
                'active_table_cycles': dict(self.used_tables), 'required_clocks': required, 'clocks': rows,
                'clock_group_positions': {name: {pin: i for i, pin in enumerate(members) if pin in self.clocks}
                                          for name, members in self.resolved.items() if any(p in self.clocks for p in members)},
                'gap_policy': 'Long complete cycles qualify as idle holds only when one plateau matches a measured target cycle and the other is extended; adjacent scan pulse vectors must match 10 ns exactly. Final incomplete cycles are not frequency failures.'}