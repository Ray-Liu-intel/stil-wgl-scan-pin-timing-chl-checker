"""Executable STIL timing checks, using the streaming grammar in stil_timing.

Only the explicitly selected PatternExec/PatternBurst/Pattern is executed. The
parser consumes the entire stream, including the gzip footer, before PASS is
possible. No timing is inferred from an unused table, macro or Period alone.

Setup needs an observed TCK rise/fall/rise interval at 10 MHz (CCD) or 25 MHz
(IOD/DRD). Body needs at least two adjacent completed pulses at 100 MHz on each
declared ScanMasterClock, and every adjacent actual pulse-vector interval must
match 10 ns; chain needs both. Widths are measured, not assumed to be 50%.
Extended high/low holds are gaps only if the other plateau matches an observed
target-period pulse. For NRZ TCK (not scan), more than one consecutive non-target
complete rise-to-rise interval fails, whether the intervals are equal or differ.
Target intervals or unknown drive clear that run, not its maximum. For scan,
explicit inactive vectors may separate shift/capture pulses by repeated equal
or variable gaps; non-target run lengths are informational, not a requirement
that all pulse enables remain periodic across phases. Pulse-shape and adjacent
cadence checks still apply. A final static hold is not a frequency failure.
Unknown/high-impedance clock drive makes the result UNDETERMINED.

Storage depends on bounded declarations, Loop/Macro bodies, and small caches,
not executed vector count. Shift arguments are expanded in parallel, never
concatenated. Clock updates and selected waveform evidence are cached; repeated
non-clock channel data are validated once per call rather than once per bit.
Unsupported execution contexts fail closed. This is not a general STIL simulator.
"""
from collections import OrderedDict
import gzip

from stil_timing import Clock as BaseClock, Interpreter as SyntaxInterpreter, StilError


class BoundedCache:
    """Per-file LRU with both entry and approximate string-storage bounds."""

    def __init__(self, entries=256, text_limit=4 * 1024 * 1024):
        self.entries, self.text_limit = entries, text_limit
        self.items = OrderedDict()
        self.size = self.hits = self.misses = 0

    def get(self, key):
        if key not in self.items:
            self.misses += 1
            return None
        self.hits += 1
        value, _ = self.items[key]
        self.items.move_to_end(key)
        return value

    def put(self, key, value, size):
        if key in self.items:
            _, old_size = self.items.pop(key)
            self.size -= old_size
        if size > self.text_limit:
            return value
        while self.items and (len(self.items) >= self.entries or self.size + size > self.text_limit):
            _, (_, old_size) = self.items.popitem(last=False)
            self.size -= old_size
        self.items[key] = value, size
        self.size += size
        return value


class Clock(BaseClock):
    """Retain active-train evidence in addition to edge/width histograms."""

    def __init__(self, name, target, scan):
        super().__init__(name, target, scan)
        self.previous_interval = None
        self.interval_run = 0
        self.max_interval_runs = {}
        self.non_target_interval_run = 0
        self.max_consecutive_non_target_intervals = 0
        self.unknown_drive_vectors = 0
        self.completed_pulses = self.completed_streak = self.max_completed_streak = 0
        self.previous_completed_cycle = self.previous_completed_rise = None

    def event(self, time, level, cycle):
        if level is None:
            self.previous_interval = None
            self.interval_run = 0
            self.non_target_interval_run = 0
            self.completed_streak = 0
            self.previous_completed_cycle = self.previous_completed_rise = None
        elif level == 1 and self.level == 0 and self.rise is not None:
            interval = time - self.rise
            self.non_target_interval_run = self.non_target_interval_run + 1 if interval != self.target else 0
            self.max_consecutive_non_target_intervals = max(
                self.max_consecutive_non_target_intervals, self.non_target_interval_run)
            self.interval_run = self.interval_run + 1 if interval == self.previous_interval else 1
            self.previous_interval = interval
            if interval not in self.max_interval_runs and len(self.max_interval_runs) >= 256:
                raise StilError('Active pulse trains exceed histogram limit')
            self.max_interval_runs[interval] = max(self.max_interval_runs.get(interval, 0), self.interval_run)
        elif level == 0 and self.level == 1 and self.rise is not None:
            self.completed_pulses += 1
            if (self.previous_completed_cycle == self.last_rise_cycle - 1 and
                    self.previous_completed_rise == self.rise - self.target):
                self.completed_streak += 1
            else:
                self.completed_streak = 1
            self.max_completed_streak = max(self.max_completed_streak, self.completed_streak)
            self.previous_completed_cycle, self.previous_completed_rise = self.last_rise_cycle, self.rise
        super().event(time, level, cycle)

    def report(self):
        row = super().report()
        # Only NRZ TCK treats consecutive non-target intervals as a wrong train,
        # whether equal or variable. Scan enables may be explicitly gated between
        # phases: retain run diagnostics, but enforce shape/adjacency in BaseClock
        # plus completed-pulse evidence here, not periodic enables across gaps.
        wrong_trains = {period: count for period, count in self.max_interval_runs.items()
                if not self.scan and period != self.target and count >= 2}
        if ((not self.scan and self.max_consecutive_non_target_intervals > 1) or
            (self.scan and self.max_completed_streak < 2)) and row['status'] != 'UNDETERMINED':
            row['status'] = 'FAIL'
        row['max_active_train_intervals'] = dict(sorted(self.max_interval_runs.items()))
        row['wrong_frequency_trains'] = dict(sorted(wrong_trains.items()))
        row['max_consecutive_non_target_intervals'] = self.max_consecutive_non_target_intervals
        row['unknown_drive_vectors'] = self.unknown_drive_vectors
        row['completed_pulses'] = self.completed_pulses
        row['max_consecutive_complete_pulse_vectors'] = self.max_completed_streak
        return row


class Interpreter(SyntaxInterpreter):
    def __init__(self, die):
        super().__init__(die)
        self.update_cache = BoundedCache()
        self.wave_cache = BoundedCache(entries=256)
        self.macro_plans = {}

    def prepare(self):
        super().prepare()
        self.clocks = {pin: Clock(pin, clock.target, clock.scan) for pin, clock in self.clocks.items()}
        if any(self.signals[p] not in ('In', 'InOut') for p in self.clocks):
            raise StilError('Required clock must be a declared input or InOut')

    def updates(self, values):
        key = tuple(values.items())
        cached = self.update_cache.get(key)
        if cached is not None:
            return cached
        result = super().updates(values)
        return self.update_cache.put(key, result, sum(len(k) + len(v) for k, v in key))

    def vector(self, updates):
        if self.table not in self.tables:
            raise StilError('Missing/unknown active WaveformTable: ' + str(self.table))
        self.state.update(updates)
        key = self.table, tuple(self.state.get(pin) for pin in self.clocks)
        evidence = self.wave_cache.get(key)
        period, waveforms = self.tables[self.table]
        if evidence is None:
            evidence = []
            for pin, clock in self.clocks.items():
                char = self.state.get(pin)
                if char is None:
                    evidence.append((clock, (), True, False))
                    continue
                events = waveforms.get((pin, char))
                if events is None:
                    raise StilError(f'Undefined clock waveform {pin}={char} in {self.table}')
                # Repeated drives of the same level inside one waveform carry
                # no extra edge evidence. Keep the initial drive to establish
                # the actual cross-vector transition, even for static WFCs.
                edges = tuple(event for i, event in enumerate(events)
                              if not i or event[1] != events[i - 1][1])
                unknown = any(level is None for _, level in edges)
                evidence.append((clock, edges, unknown, unknown))
            self.wave_cache.put(key, evidence, len(self.table) + len(self.clocks) * 32)
        self.used_tables[self.table] += 1
        for clock, events, unknown, undriven in evidence:
            if unknown:
                clock.unknown_vectors += 1
                clock.unknown_drive_vectors += int(undriven)
            for offset, level in events:
                # Avoid a method call for millions of redundant static drives.
                if level is None or clock.level != level:
                    clock.event(self.time + offset, level, self.cycles)
        self.cycles += 1
        self.time += period

    def macro_plan(self, name):
        if name in self.macro_plans:
            return self.macro_plans[name]
        if name not in self.macros:
            raise StilError('Undefined macro: ' + name)
        body = self.macros[name]
        shifts = [child for child in body if child[0] == 'Shift']
        if not shifts:
            plan = body, None, None, None
        else:
            if len(shifts) != 1 or any(child[0] not in ('Ann', 'Label', 'W', 'V', 'Shift') for child in body):
                raise StilError('Supported Macro requires exactly one top-level Shift')
            shift = [child for child in shifts[0][1] if child[0] not in ('Ann', 'Label')]
            if len(shift) != 1 or shift[0][0] != 'V':
                raise StilError('Supported Shift requires exactly one V')
            fields = shift[0][1]
            channels = {key for key, value in fields.items() if value == '#'}
            if not channels or any(self.plans.get(key, (0,))[0] != 1 for key in channels):
                raise StilError('Only scalar parallel Shift # channels are supported')
            # Validate widths and overlap once, including non-clock channels.
            self.updates({key: '0' if value == '#' else value for key, value in fields.items()})
            fixed = self.updates({key: value for key, value in fields.items() if key not in channels})
            tracked = {key: self.plans[key][1][0][1] for key in channels if self.plans[key][1]}
            plan = body, channels, fixed, tracked
        self.macro_plans[name] = plan
        return plan

    def execute(self, stmt, depth=0):
        if depth > 32:
            raise StilError('Recursive Macro/Loop execution limit')
        if stmt[0] != 'Macro':
            return super().execute(stmt, depth)
        self.calls += 1
        body, channels, fixed, tracked = self.macro_plan(stmt[1])
        supplied = stmt[2]
        if channels is None:
            if supplied:
                raise StilError('Arguments to a Macro without a Shift are unsupported')
            for child in body:
                self.execute(child, depth + 1)
            return
        if channels != set(supplied):
            raise StilError('Macro arguments must match Shift # channels exactly')
        lengths = {len(data) for data in supplied.values()}
        if len(lengths) != 1 or '#' in supplied.values():
            raise StilError('Parallel Shift channel lengths differ or contain #')
        count = lengths.pop()
        for child in body:
            if child[0] != 'Shift':
                self.execute(child, depth + 1)
                continue
            if tracked:
                for index in range(count):
                    updates = dict(fixed)
                    updates.update((pin, supplied[key][index]) for key, pin in tracked.items())
                    self.vector(updates)
            else:
                for _ in range(count):
                    self.vector(fixed)
            self.shift_cycles += count

    def report(self, path):
        result = super().report(path)
        result['gap_policy'] = (
            'A long rise/fall/rise interval is an idle hold only when one plateau matches an '
            'observed target-period pulse and the other is extended. For NRZ TCK (not scan), '
            'more than one consecutive non-target complete rise-to-rise interval fails, '
            'whether equal or variable. Target intervals or unknown drive clear the current '
            'non-target run, not its maximum. For scan, explicit inactive vectors may separate '
            'shift/capture pulses by repeated equal or variable gaps; max_active_train_intervals '
            'and max_consecutive_non_target_intervals are informational, and wrong_frequency_trains '
            'applies only to NRZ TCK. Scan still requires measured pulse-shape evidence, every '
            'adjacent actual pulse-vector interval at 10 ns, and at least two consecutive complete '
            '100 MHz pulses. Unknown drive remains UNDETERMINED. Final incomplete cycles/static '
            'levels are not frequency failures.')
        result['execution_cache'] = {
            'vector_decode_hits': self.update_cache.hits,
            'vector_decode_misses': self.update_cache.misses,
            'waveform_hits': self.wave_cache.hits,
            'waveform_misses': self.wave_cache.misses,
            'vector_decode_entries': len(self.update_cache.items),
            'waveform_entries': len(self.wave_cache.items),
            'vector_decode_text_bytes': self.update_cache.size,
        }
        return result


def timing_stil(path, die):
    """Return evidence only after successful parsing/execution through EOF."""
    interpreter = Interpreter(die)
    try:
        if die not in ('CCD', 'IOD', 'DRD'):
            raise StilError('Unsupported die')
        opener = gzip.open if str(path).lower().endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8-sig') as stream:
            interpreter.parse(stream)
        return interpreter.report(path)
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError) as exc:
        return {'status': 'ERROR', 'reason': str(exc), 'full_file_checked': False,
                'cycles_before_error': interpreter.cycles}