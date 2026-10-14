"""Synthetic timing fixtures only; never read or modify production patterns."""
import gzip
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from stil_checks import BoundedCache, timing_stil
from stil_timing import Reader, StilError, data_value, tokens


HEADER = '''STIL 1.0;
Header { Title "synthetic"; History { Ann {* ignored } { *} } }
Signals { "DATA"[2] In; TCK_IN In; A In; B In; RESULT Out; }
SignalGroups {
 clocks = '"A" + "B"';
 _pi_ = '"DATA[2]" + TCK_IN + clocks';
 input0 = '"DATA"[2] + TCK_IN';
 channel = '"DATA"[2]' { ScanIn 4; }
 output = 'RESULT' { ScanOut 4; }
}
Spec other { Category WRONG { period = '20ns'; force = '0ps'; pulse = '3ns'; width = '5ns'; } }
Spec chosen { Category RIGHT { period = '10000ps'; force = '0ps'; pulse = '3000ps'; width = '5000ps'; } }
Timing wrong { WaveformTable tp { Period '20ns'; Waveforms {
 input0 { 01Z { '0ps' D/U/Z; } }
 clocks { 01 { '0ps' D; '3ns' D/U; '8ns' D; } }
 output { X { '2ns' X; } }
} } }
Timing selected { WaveformTable tp { Period 'period'; Waveforms {
 input0 { 01Z { 'force' D/U/Z; } }
 clocks { 01 { 'force' D; 'pulse' D/U; 'pulse+width' D; } }
 output { X { '2ns' X; } }
} } }
ScanStructures { ScanChain c { ScanLength 4; ScanIn "DATA"[2]; ScanOut RESULT; ScanMasterClock A B; } }
MacroDefs { shift {
 W tp;
 Shift { V { channel = #; output = #; clocks = 11; } }
 W tp;
} }
PatternBurst burst { PatList { test; } }
PatternExec { Category RIGHT; Timing selected; PatternBurst burst; }
Pattern test {
 W tp;
 V { _pi_ = 0000; output = X; }
'''
TCK = '''Loop 3 {
 V { TCK_IN = 0; }
 Loop 4 { V {} }
 V { TCK_IN = 1; }
 Loop 4 { V {} }
}
'''
SHIFT = '''Macro shift { channel = \\r2 0
 11; output = \\r4 X; }
'''


class StilTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def check(self, text=None, kind='chain', die='CCD', gz=False):
        path = self.root / ('synthetic_' + kind + '_v1.stil' + ('.gz' if gz else ''))
        text = HEADER + TCK + SHIFT + '}' if text is None else text
        path.write_bytes(gzip.compress(text.encode()) if gz else text.encode())
        return timing_stil(path, die)

    def test_named_spec_timing_and_table_selection(self):
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['selection']['Category'], 'RIGHT')
        self.assertEqual(result['periods_ps'], [10000])
        self.assertEqual(result['clocks']['TCK_IN']['rising_intervals_ps'][100000], 2)
        self.assertEqual(result['clocks']['A']['rising_intervals_ps'], {10000: 3})
        self.assertEqual(result['clocks']['A']['high_widths_ps'], {5000: 4})

    def test_group_mapping_external_and_internal_bus_indices(self):
        result = self.check()
        self.assertEqual(result['clock_group_positions']['_pi_'], {'TCK_IN': 1, 'A': 2, 'B': 3})
        reordered = HEADER.replace("'\"DATA[2]\" + TCK_IN + clocks'", "'clocks + TCK_IN + \"DATA\"[2]'")
        result = self.check(reordered + TCK + SHIFT + '}')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clock_group_positions']['_pi_'], {'A': 0, 'B': 1, 'TCK_IN': 2})

    def test_vector_inheritance_executes_empty_vectors(self):
        result = self.check(HEADER + 'V { clocks = 11; } Loop 3 { V {} } }', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['A']['rising_edges'], 4)
        self.assertEqual(result['cycles'], 5)

    def test_actual_shift_parallel_lengths_and_multiline_repeat(self):
        result = self.check(HEADER + SHIFT + '}', 'body', gz=True)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['shift_cycles'], 4)  # Not 4+4 channel bits.
        self.assertEqual(result['cycles'], 5)
        self.assertEqual(result['macro_calls'], 1)

    def test_unused_macro_does_not_count(self):
        result = self.check(HEADER + 'V {} }', 'body')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['shift_cycles'], 0)
        self.assertEqual(result['clocks']['A']['rising_edges'], 0)

    def test_labels_are_not_execution_or_control_flow(self):
        result = self.check(HEADER + '"pattern 0": ' + TCK + 'scan_label: ' + SHIFT + '}')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['cycles'], 35)
        self.assertEqual(self.check(HEADER + SHIFT + 'Goto "pattern 0"; }', 'body')['status'], 'ERROR')

    def test_executed_shift_with_flat_waveform_fails(self):
        result = self.check(HEADER.replace('clocks = 11;', 'clocks = 00;') + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'FAIL')

    def test_parallel_lengths_must_match(self):
        result = self.check(HEADER + SHIFT.replace('\\r4 X', '\\r3 X') + '}', 'body')
        self.assertEqual(result['status'], 'ERROR')
        self.assertIn('lengths differ', result['reason'])

    def test_shift_arguments_must_match_definition(self):
        for shift in (SHIFT.replace('output = \\r4 X;', ''), SHIFT.replace('output =', 'RESULT =')):
            self.assertEqual(self.check(HEADER + shift + '}', 'body')['status'], 'ERROR')

    def test_multivector_shift_rejected(self):
        text = HEADER.replace('clocks = 11; } }', 'clocks = 11; } V {} }')
        self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'ERROR')

    def test_single_scan_pulse_not_enough(self):
        self.assertEqual(self.check(HEADER + 'V {clocks=11;} }', 'body')['status'], 'FAIL')

    def test_all_declared_scan_clocks_are_required(self):
        result = self.check(HEADER.replace('clocks = 11;', 'clocks = 10;') + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['clocks']['A']['status'], 'PASS')
        self.assertEqual(result['clocks']['B']['status'], 'FAIL')

    def test_flat_tck_fails_even_with_good_period(self):
        self.assertEqual(self.check(HEADER + 'Loop 20 {V {}} }', 'setup')['status'], 'FAIL')

    def test_one_tck_toggle_not_frequency_evidence(self):
        self.assertEqual(self.check(HEADER + 'V {TCK_IN=1;} }', 'setup')['status'], 'FAIL')

    def test_wrong_period_and_frequency(self):
        result = self.check(HEADER.replace("'10000ps'", "'20000ps'") + TCK + SHIFT + '}')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['clocks']['A']['wrong_consecutive_intervals'], 3)
        self.assertEqual(result['clocks']['TCK_IN']['target_intervals'], 0)

    def test_wrong_tck_frequency_with_correct_vector_period(self):
        result = self.check(HEADER + TCK.replace('Loop 4', 'Loop 3') + '}', 'setup')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['clocks']['TCK_IN']['rising_intervals_ps'], {80000: 2})

    def test_iod_drd_tck_target(self):
        for die in ('IOD', 'DRD'):
            result = self.check(HEADER + TCK.replace('Loop 4', 'Loop 1') + '}', 'setup', die)
            self.assertEqual(result['status'], 'PASS', result)
            self.assertEqual(result['clocks']['TCK_IN']['target_intervals'], 2)

    def test_idle_low_high_gaps_and_final_held_level(self):
        idle = '''V {TCK_IN=0;} Loop 20 {V {}}
                  V {TCK_IN=1;} Loop 4 {V {}}
                  V {TCK_IN=0;} Loop 4 {V {}}
                  V {TCK_IN=1;} Loop 20 {V {}}
                  V {TCK_IN=0;} Loop 4 {V {}}
                  V {TCK_IN=1;} Loop 50 {V {}}'''
        result = self.check(HEADER + TCK + idle + '}', 'setup')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['TCK_IN']['idle_gap_intervals'], 2)
        self.assertEqual(result['clocks']['TCK_IN']['final_level'], 1)

    def test_scan_idle_gap_does_not_fail(self):
        result = self.check(HEADER + SHIFT + 'V {clocks=00;} Loop 40 {V {}}' + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['A']['idle_gap_intervals'], 1)

    def check_gated_scan_intervals(self, intervals):
        # WFC0 is explicitly low; empty vectors inherit it, not pulsing WFC1.
        # Each sequence separates two four-pulse Shift bursts with capture pulses.
        gated = ''.join('V {clocks=00;} Loop %d {V {}} V {clocks=11;}'
                        % (period // 10 - 2) for period in intervals)
        for kind in ('body', 'chain'):
            for gz in (False, True):
                with self.subTest(intervals=intervals, kind=kind, gz=gz):
                    result = self.check(HEADER + TCK + SHIFT +
                                        (gated + SHIFT) * 2 + '}', kind, gz=gz)
                    self.assertEqual(result['status'], 'PASS', result)
                    self.assertTrue(result['full_file_checked'])
                    for pin in ('A', 'B'):
                        clock = result['clocks'][pin]
                        expected = {10000: 11}
                        for period in intervals:
                            expected[period * 1000] = expected.get(period * 1000, 0) + 2
                        self.assertEqual(clock['status'], 'PASS')
                        self.assertEqual(clock['rising_intervals_ps'], expected)
                        self.assertEqual(clock['high_widths_ps'], {5000: 18})
                        self.assertEqual(clock['completed_pulses'], 18)
                        self.assertEqual(clock['idle_gap_intervals'], 6)
                        self.assertEqual(clock['max_consecutive_non_target_intervals'], 3)
                        self.assertEqual(clock['max_active_train_intervals'][intervals[0] * 1000],
                                         3 if len(set(intervals)) == 1 else 1)
                        self.assertEqual(clock['wrong_frequency_trains'], {})
                        self.assertEqual(clock['inconsistent_intervals'], 0)
                        self.assertEqual(clock['consecutive_pulse_intervals'], 11)
                        self.assertEqual(clock['wrong_consecutive_intervals'], 0)
                        self.assertEqual(clock['max_consecutive_complete_pulse_vectors'], 5)
                        self.assertEqual(clock['unknown_assignment_vectors'], 0)

    def test_repeated_variable_gated_scan_intervals_pass(self):
        self.check_gated_scan_intervals((140, 160, 260))

    def test_repeated_equal_gated_scan_intervals_pass(self):
        self.check_gated_scan_intervals((140, 140, 140))

    def test_gated_scan_without_adjacent_complete_pulses_fails(self):
        result = self.check(HEADER + 'Loop 4 {V {clocks=11;} V {clocks=00;}} }', 'body')
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertEqual(result['clocks']['A']['completed_pulses'], 4)
        self.assertEqual(result['clocks']['A']['max_consecutive_complete_pulse_vectors'], 1)

    def test_scan_unknown_idle_not_erased_by_valid_bursts(self):
        text = HEADER.replace('clocks { 01 {', "clocks { Z { '0ps' Z; } 01 {")
        result = self.check(text + SHIFT + 'V {clocks=ZZ;} V {clocks=00;}' + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'UNDETERMINED', result)
        self.assertEqual(result['clocks']['A']['unknown_drive_vectors'], 1)
        self.assertEqual(result['clocks']['A']['max_consecutive_complete_pulse_vectors'], 4)

    def test_wrong_frequency_late_in_file_not_hidden(self):
        result = self.check(HEADER + TCK + TCK.replace('Loop 4', 'Loop 9') + '}', 'setup')
        self.assertEqual(result['status'], 'FAIL')
        self.assertGreater(result['clocks']['TCK_IN']['inconsistent_intervals'], 0)

    def test_waveform_character_is_not_logic_level(self):
        text = HEADER.replace('clocks { 01 {', 'clocks { 0P {').replace('clocks = 11;', 'clocks = PP;')
        result = self.check(text + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['A']['rising_edges'], 4)

    def test_active_table_changes_inherited_character(self):
        extra = '''WaveformTable slow { Period '20ns'; Waveforms {
         input0 { 01Z { '0ps' D/U/Z; } }
         clocks { 01 { '0ps' D; '3ns' D/U; '8ns' D; } }
        } }'''
        text = HEADER.replace('Timing selected {', 'Timing selected {' + extra)
        result = self.check(text + SHIFT + 'W slow; V {} V {} }', 'body')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['active_table_cycles']['slow'], 2)
        clock = result['clocks']['A']
        # The 5 ns high and extended low pass the shape/gap check, and there
        # is only one non-target interval: adjacent_bad must still reject it.
        self.assertEqual(clock['inconsistent_intervals'], 0)
        self.assertEqual(clock['idle_gap_intervals'], 1)
        self.assertEqual(clock['max_consecutive_non_target_intervals'], 1)
        self.assertEqual(clock['wrong_consecutive_intervals'], 1)
        self.assertEqual(clock['completed_pulses'], 6)
        self.assertEqual(clock['max_consecutive_complete_pulse_vectors'], 5)

    def test_missing_table_never_guessed(self):
        self.assertEqual(self.check(HEADER + 'W unknown; V {} }', 'body')['status'], 'ERROR')
        text = HEADER.replace(' W tp;\n V { _pi_', ' V { _pi_')
        self.assertEqual(self.check(text + '}', 'body')['status'], 'ERROR')

    def test_period_alone(self):
        result = self.check("STIL 1.0; Timing { WaveformTable w { Period '10ns'; } }", 'body')
        self.assertEqual(result['status'], 'UNDETERMINED')
        self.assertEqual(result['periods_ps'], [10000])

    def test_unsupported_executable_and_top_level_syntax(self):
        for tail in ('Call shift;', 'Goto x;', 'Stop;', 'MatchLoop 4 {}', 'Ann W tp;', 'V {clocks=\\h FF;}'):
            with self.subTest(tail=tail):
                self.assertEqual(self.check(HEADER + SHIFT + tail + '}', 'body')['status'], 'ERROR')
        self.assertEqual(self.check(HEADER + SHIFT + '} Include "other.stil";', 'body')['status'], 'ERROR')

    def test_truncation_after_good_evidence(self):
        for tail in ('', 'V {', 'V { _pi_=0000;', '/* open', 'Ann {* open', 'V {output="open'):
            with self.subTest(tail=tail):
                result = self.check(HEADER + TCK + SHIFT + tail)
                self.assertEqual(result['status'], 'ERROR')
                self.assertFalse(result['full_file_checked'])

    def test_truncated_gzip_footer(self):
        path = self.root / 'test_chain.stil.gz'
        path.write_bytes(gzip.compress((HEADER + TCK + SHIFT + '}').encode())[:-8])
        self.assertEqual(timing_stil(path, 'CCD')['status'], 'ERROR')

    def test_comments_quotes_across_chunk_boundary(self):
        for comment in ('/*' + 'x' * 70000 + '*/', 'Ann {*' + 'x' * 70000 + '*}'):
            result = self.check(HEADER + comment + TCK + SHIFT + '}')
            self.assertEqual(result['status'], 'PASS', result)

    def test_unknown_clock_char_and_bad_width(self):
        for value in ('111', 'QQ'):
            self.assertEqual(self.check(HEADER + 'V {clocks=' + value + ';} }', 'body')['status'], 'ERROR')

    def test_missing_required_clock_declaration(self):
        text = HEADER.replace('ScanMasterClock A B;', '')
        self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'UNDETERMINED')
        text = HEADER.replace('TCK_IN', 'OTHER')
        self.assertEqual(self.check(text + SHIFT + '}', 'chain')['status'], 'UNDETERMINED')

    def test_unassigned_clock_not_assumed_zero(self):
        text = HEADER.replace('_pi_ = 0000;', 'channel = 0;')
        self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'UNDETERMINED')

    def test_group_cycle_and_unknown_references(self):
        for expr in ('missing', 'clocks'):
            text = HEADER.replace("clocks = '\"A\" + \"B\"'", "clocks = '" + expr + "'")
            self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'ERROR')

    def test_duplicate_or_overlapping_assignments(self):
        for vector in ('V {A=0; A=1;}', 'V {clocks=00; A=1;}'):
            self.assertEqual(self.check(HEADER + vector + '}', 'body')['status'], 'ERROR')

    def test_invalid_spec_and_event_bounds(self):
        for change in ("'missing'", "'period'", "'0ps'", "'1/0'"):
            text = HEADER.replace("period = '10000ps'", 'period = ' + change)
            self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'ERROR')

    def test_selected_burst_and_category_must_exist(self):
        for old, new in (('Category RIGHT;', 'Category missing;'), ('PatternBurst burst;', 'PatternBurst missing;')):
            self.assertEqual(self.check(HEADER.replace(old, new) + SHIFT + '}', 'body')['status'], 'ERROR')

    def test_repeat_validation_and_limits(self):
        self.assertEqual(data_value('\\r3 0 1'), '0001')
        for value in ('\\r0 0', '\\r2 01', '\\r1000001 0', '\\r2', '0 #'):
            with self.assertRaises((StilError, ValueError)):
                data_value(value)

    def test_main_delegates_to_executable_engine(self):
        import check_scan_pins
        self.assertIs(check_scan_pins.timing_stil, timing_stil)
        self.assertEqual(self.check()['status'], 'PASS')

    def test_plain_compressed_evidence_parity(self):
        self.assertEqual(self.check(gz=False), self.check(gz=True))

    def test_gzip_crc_corruption_after_good_evidence(self):
        path = self.root / 'crc_chain.stil.gz'
        data = bytearray(gzip.compress((HEADER + TCK + SHIFT + '}').encode()))
        data[-8] ^= 1
        path.write_bytes(data)
        result = timing_stil(path, 'CCD')
        self.assertEqual(result['status'], 'ERROR')
        self.assertFalse(result['full_file_checked'])

    def test_unknown_drive_cannot_be_erased_by_later_good_timing(self):
        result = self.check(HEADER + 'V {TCK_IN=Z;}' + TCK + '}', 'setup')
        self.assertEqual(result['status'], 'UNDETERMINED')
        self.assertEqual(result['clocks']['TCK_IN']['unknown_drive_vectors'], 1)
        self.assertGreater(result['clocks']['TCK_IN']['target_intervals'], 0)

    def test_unknown_drive_inside_clock_waveform(self):
        text = HEADER.replace("'pulse+width' D;", "'pulse+width' Z;")
        result = self.check(text + SHIFT + '}', 'body')
        self.assertEqual(result['status'], 'UNDETERMINED')
        self.assertGreater(result['clocks']['A']['unknown_drive_vectors'], 0)

    def test_sustained_slow_train_not_excused_as_repeated_idle_gaps(self):
        # The high plateau still matches 10 MHz, but repeated 200 ns cycles
        # are a 5 MHz active train, not isolated idle holds.
        slower = 'Loop 4 {V {TCK_IN=0;} Loop 14 {V {}} V {TCK_IN=1;} Loop 4 {V {}}}'
        result = self.check(HEADER + TCK + slower + '}', 'setup')
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertGreaterEqual(result['clocks']['TCK_IN']['wrong_frequency_trains'][200000], 2)

    def test_alternating_slow_tck_intervals_fail_without_equal_period_train(self):
        # 50 ns highs match the target pulse, but alternating 200/210 ns
        # rise-to-rise intervals must not masquerade as isolated idle gaps.
        slow = ''.join('V {TCK_IN=0;} Loop %d {V {}} '
                       'V {TCK_IN=1;} Loop 4 {V {}}' % count
                       for count in (14, 15))
        for repeats in (1, 2):
            for kind in ('setup', 'chain', 'body'):
                with self.subTest(repeats=repeats, kind=kind):
                    result = self.check(HEADER + TCK + slow * repeats + TCK + SHIFT + '}', kind)
                    self.assertEqual(result['status'], 'PASS' if kind == 'body' else 'FAIL', result)
                    clock = result['clocks']['TCK_IN']
                    self.assertEqual(clock['status'], 'FAIL')
                    self.assertEqual(clock['wrong_frequency_trains'], {})
                    self.assertEqual(clock['max_active_train_intervals'][200000], 1)
                    self.assertEqual(clock['max_active_train_intervals'][210000], 1)
                    self.assertEqual(clock['max_consecutive_non_target_intervals'], 2 * repeats)
                    self.assertEqual(clock['rising_intervals_ps'][200000], repeats)
                    self.assertEqual(clock['rising_intervals_ps'][210000], repeats)
                    self.assertEqual(clock['high_widths_ps'], {50000: 5 + 2 * repeats})
                    self.assertEqual(clock['inconsistent_intervals'], 0)
                    self.assertTrue(result['full_file_checked'])

    def test_isolated_hold_between_target_runs_passes(self):
        for level in (0, 1):
            with self.subTest(level=level):
                hold = 'V {TCK_IN=%d;} Loop 20 {V {}}' % level
                result = self.check(HEADER + TCK + hold + TCK + hold + TCK + '}', 'setup')
                self.assertEqual(result['status'], 'PASS', result)
                clock = result['clocks']['TCK_IN']
                self.assertEqual(clock['idle_gap_intervals'], 2)
                self.assertEqual(clock['max_consecutive_non_target_intervals'], 1)
                self.assertEqual(clock['wrong_frequency_trains'], {})

    def test_trailing_static_tck_does_not_complete_non_target_interval(self):
        for level in (0, 1):
            with self.subTest(level=level):
                hold = 'V {TCK_IN=%d;} Loop 1000 {V {}}' % level
                result = self.check(HEADER + TCK + hold + '}', 'setup')
                self.assertEqual(result['status'], 'PASS', result)
                clock = result['clocks']['TCK_IN']
                self.assertEqual(clock['rising_intervals_ps'], {100000: 2})
                self.assertEqual(clock['max_consecutive_non_target_intervals'], 0)
                self.assertEqual(clock['final_level'], level)

    def test_unknown_drive_breaks_non_target_interval_run(self):
        slow = 'V {TCK_IN=0;} Loop 14 {V {}} V {TCK_IN=1;} Loop 4 {V {}}'
        # No target interval between these two wrong intervals: only unknown
        # drive and a fresh rise, which cannot complete a cross-unknown cycle.
        restart = 'V {TCK_IN=Z;} V {TCK_IN=0;} V {TCK_IN=1;} Loop 4 {V {}}'
        result = self.check(HEADER + TCK + slow + restart + slow + '}', 'setup')
        self.assertEqual(result['status'], 'UNDETERMINED', result)
        clock = result['clocks']['TCK_IN']
        self.assertEqual(clock['max_consecutive_non_target_intervals'], 1)
        self.assertEqual(clock['rising_intervals_ps'][200000], 2)
        self.assertEqual(clock['wrong_frequency_trains'], {})
        self.assertEqual(clock['unknown_drive_vectors'], 1)

    def test_tck_no_assumed_fifty_percent_duty_cycle(self):
        asymmetric = 'Loop 4 {V {TCK_IN=0;} Loop 2 {V {}} V {TCK_IN=1;} Loop 6 {V {}}}'
        result = self.check(HEADER + asymmetric + '}', 'setup')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['TCK_IN']['complete_pulse_pairs'],
                         [{'high_ps': 70000, 'low_ps': 30000, 'count': 3}])

    def test_wrong_event_windows_fail_closed(self):
        for value in ('-1ps', '10000ps', '10001ps', '2000ps'):
            text = HEADER.replace("'pulse+width'", "'" + value + "'")
            with self.subTest(value=value):
                self.assertEqual(self.check(text + SHIFT + '}', 'body')['status'], 'ERROR')

    def test_simple_macro_execution_and_inheritance(self):
        extra = 'MacroDefs { pulse { W tp; V {clocks=11;} Loop 2 { V {} } } }\n'
        text = HEADER.replace('PatternBurst burst', extra + 'PatternBurst burst', 1)
        result = self.check(text + 'Macro pulse {} V {} }', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['cycles'], 5)
        self.assertEqual(result['clocks']['A']['rising_edges'], 4)

    def test_recursive_macro_fails_closed(self):
        extra = 'MacroDefs { recurse { Macro recurse {} } }\n'
        text = HEADER.replace('PatternBurst burst', extra + 'PatternBurst burst', 1)
        result = self.check(text + SHIFT + 'Macro recurse {} }', 'body')
        self.assertEqual(result['status'], 'ERROR')
        self.assertIn('Recursive Macro/Loop execution limit', result['reason'])

    def test_parallel_shift_argument_can_drive_a_tracked_clock(self):
        text = HEADER.replace('clocks = 11;', 'A = #; B = 1;')
        call = SHIFT.replace('channel =', 'A = 1111; channel =')
        result = self.check(text + call + 'V {} }', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['clocks']['A']['rising_edges'], 5)
        self.assertEqual(result['shift_cycles'], 4)
        bad = call.replace('A = 1111;', 'A = 1Q11;')
        self.assertEqual(self.check(text + bad + '}', 'body')['status'], 'ERROR')

    def test_clock_channel_cannot_overlap_fixed_assignment(self):
        text = HEADER.replace('clocks = 11;', 'clocks = 11; A = #;')
        call = SHIFT.replace('channel =', 'A = 1111; channel =')
        self.assertEqual(self.check(text + call + '}', 'body')['status'], 'ERROR')

    def test_loop_reexecutes_macro_and_counts_parallel_cycles(self):
        result = self.check(HEADER + 'Loop 1000 {' + SHIFT + '} }', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['cycles'], 4001)
        self.assertEqual(result['shift_cycles'], 4000)
        self.assertEqual(result['macro_calls'], 1000)
        self.assertEqual(result['clocks']['A']['rising_intervals_ps'], {10000: 3999})

    def test_million_inherited_vectors_use_bounded_caches(self):
        result = self.check(HEADER + 'V {clocks=11;} Loop 1000000 {V {}} }', 'body')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['cycles'], 1000002)
        self.assertEqual(result['clocks']['A']['rising_edges'], 1000001)
        self.assertEqual(result['clocks']['A']['rising_intervals_ps'], {10000: 1000000})
        self.assertLessEqual(result['execution_cache']['vector_decode_entries'], 3)
        self.assertLessEqual(result['execution_cache']['waveform_entries'], 2)
        self.assertGreaterEqual(result['execution_cache']['waveform_hits'], 1000000)

    def test_lru_enforces_text_and_entry_limits(self):
        cache = BoundedCache(entries=2, text_limit=5)
        cache.put('one', 1, 3)
        cache.put('two', 2, 3)
        self.assertIsNone(cache.get('one'))
        self.assertEqual(cache.get('two'), 2)
        cache.put('large', 3, 6)
        self.assertIsNone(cache.get('large'))
        self.assertLessEqual(cache.size, 5)
        cache.put('small', 4, 1)
        cache.put('last', 5, 1)
        self.assertLessEqual(len(cache.items), 2)

    def test_tck_output_declaration_is_not_drive_evidence(self):
        text = HEADER.replace('TCK_IN In;', 'TCK_IN Out;')
        self.assertEqual(self.check(text + TCK + '}', 'setup')['status'], 'ERROR')

    def test_two_scan_rises_but_only_one_complete_pulse_is_insufficient(self):
        table = '''WaveformTable held { Period '10ns'; Waveforms {
          input0 { 01Z { '0ps' D/U/Z; } }
          clocks { 01 { '0ps' D; '3ns' D/U; } }
        } }'''
        text = HEADER.replace('Timing selected {', 'Timing selected {' + table)
        result = self.check(text + 'V {clocks=11;} W held; V {} }', 'body')
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertEqual(result['clocks']['A']['rising_edges'], 2)
        self.assertEqual(result['clocks']['A']['completed_pulses'], 1)

    def test_cache_replacement_does_not_double_count_storage(self):
        cache = BoundedCache(entries=2, text_limit=5)
        cache.put('key', 1, 3)
        cache.put('key', 2, 3)
        self.assertEqual(cache.size, 3)
        self.assertEqual(cache.get('key'), 2)
        cache.put('key', 3, 6)
        self.assertEqual(cache.size, 0)
        self.assertIsNone(cache.get('key'))


if __name__ == '__main__':
    unittest.main()