from contextlib import redirect_stderr, redirect_stdout
import gzip
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('checker', Path(__file__).parents[1] / 'scripts/check_scan_pins.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

PIN = '''Version 1.0;
PinDescription { Resource DPin {
 DATA_0;
 RESULT;
 AUX;
 Group nested { DATA_0 }
 Group stm_scan_in { nested }
 Group stm_scan_out { RESULT }
 Group stm_scan_nonclk_dpin { AUX }
} }
'''
STIL = '''STIL 1.0;
// ScanStructures { fake }
Ann {* ignored ScanStructures { } *}
ScanStructures {
 ScanChain c {
  ScanIn "data"[0];
  ScanOut "RESULT";
  ScanMasterClock "CLK";
 }
}
'''
WGL = '''waveform TEST
 signal
 "DATA" : input;
 "RESULT" : output;
 "TCK_IN" : input;
 "SCAN_CLK" : input;
 "SC_CLK_RX_IN_X" : input;
 end
 scanchain
 c["data"[0], "internal/cell", "RESULT"];
 end
 equationsheet test
 exprset cat
 Time_period := 10000ps;
 Time_pulse := 3ns;
 Time_back := Time_pulse + 5ns;
 end
 end
 timeplate tp period Time_period
 "DATA" := input[0ps:P, 0ps:S];
 "RESULT" := output[0ps:X, 2ns:Q];
 "TCK_IN" := input[0ps:P, 0ps:S];
 "SCAN_CLK" := input[0ps:D, Time_pulse:S, Time_back:D];
 "SC_CLK_RX_IN_X" := input[0ps:P, 0ps:S];
 end
 scanstate
 s_in := g c (0
 1);
 s_out := g c (HL);
 end
 pattern scan("DATA", "RESULT", "TCK_IN", "SCAN_CLK", "SC_CLK_RX_IN_X")
 { Pattern 0 Vector 0 TesterCycle 0 }
 repeat 5 { Vector type: SETUP, TesterCycle: 0 }
 vector(+,tp) := [0 X 0 0 1];
 { TesterCycle: 5 }
 repeat 5 vector(+,tp) := [0 X 1 0 1];
 loop 2
 repeat 5 vector(+,tp) := [0 X 0 0 1];
 repeat 5 vector(+,tp) := [0 X 1 0 1];
 end
 { TesterCycle: 30 }
 scan(+,tp) := [0 X 0 0 1],
   output[c: s_out],
   input[c: s_in];
 { TesterCycle: 32 }
 vector(+,tp) := [0 X 0 0
   1];
 end
end
'''
BODY_WGL = WGL.replace('scan(+,tp) := [0 X 0 0 1]', 'scan(+,tp) := [0 X 0 1 1]')


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pin = self.root / 'example_ddr_rev0.pin'
        self.pin.write_text(PIN)
        self.groups = checker.load_groups(self.pin, checker.DEFAULT_GROUPS)

    def run_stil(self, text=STIL, compressed=False):
        path = self.root / ('sample.stil.gz' if compressed else 'sample.stil')
        if compressed:
            with gzip.open(path, 'wt') as stream:
                stream.write(text)
        else:
            path.write_text(text)
        return checker.check_file(path, self.groups)

    def run_wgl(self, text=WGL, name='sample_setup_v1.wgl.gz'):
        path = self.root / name
        if name.endswith('.gz'):
            path.write_bytes(gzip.compress(text.encode()))
        else:
            path.write_text(text)
        result = checker.check_wgl(path, self.groups, checker.normalize)
        return result

    def test_wgl_pin_direction_and_clock_symbols(self):
        result = self.run_wgl()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual([r['role'] for r in result['pins']], ['ScanIn', 'ScanOut'])
        info = result['_wgl_timing']
        self.assertEqual((info['cycles'], info['statements'], info['loops']), (33, 6, 1))
        self.assertEqual(info['tester_cycles_verified'], 4)
        self.assertEqual(info['clock_names'], ['SCAN_CLK'])  # SC_CLK_RX_IN_X is NRZ data
        self.assertEqual(info['clock_windows_ps'], {'SCAN_CLK': [3000, 8000]})
        timing = checker.timing_wgl(result, 'CCD')
        self.assertEqual(timing['status'], 'PASS', timing['reason'])
        self.assertEqual(timing['tck']['max_consecutive_periods'], 3)
        bad = self.run_wgl(WGL.replace('c["data"[0]', 'c["RESULT"'))
        self.assertEqual(bad['pins'][0]['failure'], 'DIRECTION_MISMATCH')

    def test_wgl_setup_tck_runs_are_checked_to_end_of_file(self):
        short = self.run_wgl(WGL.replace(' repeat 5 vector(+,tp) := [0 X 1 0 1];\n end',
                                         ' repeat 4 vector(+,tp) := [0 X 1 0 1];\n end')
                             .replace(' { TesterCycle: 30 }\n', '').replace(' { TesterCycle: 32 }\n', ''))
        timing = checker.timing_wgl(short, 'CCD')
        self.assertEqual(timing['status'], 'PASS', timing['reason'])  # non-5/5 runs are informational
        self.assertIn('high_run_4_cycles', timing['tck']['violations'])
        flat = self.run_wgl(WGL.replace('[0 X 1 0 1]', '[0 X 0 0 1]'))
        timing = checker.timing_wgl(flat, 'CCD')
        self.assertEqual(timing['status'], 'FAIL')
        self.assertIn('never toggles', timing['reason'])
        self.assertEqual(self.run_wgl(WGL)['_wgl_timing']['tck']['toggles'], 6)
        mismatch = self.run_wgl(WGL.replace('TesterCycle: 30', 'TesterCycle: 31'))
        self.assertEqual(mismatch['status'], 'ERROR')
        self.assertIn('TesterCycle', mismatch['error'])

    def test_wgl_body_channellink_and_timing(self):
        result = self.run_wgl(BODY_WGL, 'sample_body_p1_v1.wgl.gz')
        self.assertEqual(result['status'], 'PASS')
        timing = checker.timing_wgl(result, 'CCD')
        self.assertEqual(timing['status'], 'PASS', timing['reason'])
        self.assertEqual(timing['scan_clock_one_cycles'], 2)
        self.assertEqual(timing['scan_clock_max_consecutive_one'], {'SCAN_CLK': 2})
        single = checker.timing_wgl(self.run_wgl(WGL.replace('[0 X 0 0\n   1]', '[0 X 0 1\n   1]'),
                                                 'single_body_v1.wgl'), 'CCD')
        self.assertEqual(single['status'], 'FAIL')
        self.assertIn('consecutive', single['reason'])
        plan = checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='CCD')
        self.assertEqual(Path(plan['destination']).name, 'sample_body_p1_sc_v1.wgl.gz')
        self.assertEqual(checker.timing_wgl(self.run_wgl(BODY_WGL.replace('10000ps', '20000ps'),
                                                         'wrong_body_v1.wgl'), 'CCD')['status'], 'FAIL')
        no_pulse = checker.timing_wgl(self.run_wgl(WGL, 'idle_body_v1.wgl'), 'CCD')
        self.assertEqual(no_pulse['status'], 'FAIL')
        self.assertIn('No scan clock 1', no_pulse['reason'])

    def test_wgl_chain_requires_both_tck_and_scan_clock(self):
        timing = checker.timing_wgl(self.run_wgl(BODY_WGL, 'x_chain_CP_CCD_G2_v1.wgl.gz'), 'CCD')
        self.assertEqual(timing['status'], 'PASS', timing['reason'])
        self.assertEqual(timing['classification'], 'chain(setup+body)')
        no_clock = checker.timing_wgl(self.run_wgl(WGL, 'x_chain_v1.wgl'), 'CCD')
        self.assertEqual(no_clock['status'], 'FAIL')
        self.assertIn('No scan clock 1', no_clock['reason'])
        no_tck = checker.timing_wgl(self.run_wgl(BODY_WGL.replace('TCK_IN', 'TMS_IN'), 'x_chain_v1.wgl'), 'CCD')
        self.assertEqual(no_tck['status'], 'UNDETERMINED')
        bad_tck = BODY_WGL.replace('[0 X 1 0 1]', '[0 X 0 0 1]')
        self.assertEqual(checker.timing_wgl(self.run_wgl(bad_tck, 'x_chain_v1.wgl'), 'CCD')['status'], 'FAIL')

    def test_wgl_multiline_annotation(self):
        text = BODY_WGL.replace(' loop 2\n', ' {  Next Cycle Shift-out <X X\n   X X>\n   First Bit <0 0>  }\n loop 2\n')
        result = self.run_wgl(text, 'x_chain_v1.wgl')
        self.assertEqual(result['status'], 'PASS', result['error'])
        self.assertEqual(checker.timing_wgl(result, 'CCD')['status'], 'PASS')
        self.assertEqual(self.run_wgl(text.split('   X X>')[0], 'x_chain_v1.wgl')['status'], 'ERROR')
        self.assertEqual(self.run_wgl(text.replace('<0 0>  }', '<0 0> } vector'), 'x_chain_v1.wgl')['status'],
                         'ERROR')

    def test_wgl_se_cmd_label_marker_only(self):
        result = self.run_wgl(BODY_WGL.replace(' loop 2\n', 'SE_CMD label:select_x_begin\n loop 2\n'),
                              'x_chain_v1.wgl')
        self.assertEqual(result['status'], 'PASS', result['error'])
        self.assertEqual(result['_wgl_timing']['se_cmd_labels'], 1)
        self.assertEqual(result['_wgl_timing']['cycles'], 33)
        bad = self.run_wgl(BODY_WGL.replace(' loop 2\n', 'SE_CMD label:x extra\n loop 2\n'), 'x_chain_v1.wgl')
        self.assertEqual(bad['status'], 'ERROR')

    def test_wgl_missing_clock_and_unsupported_vector_cannot_pass_timing(self):
        result = self.run_wgl(WGL.replace('TCK_IN', 'TMS_IN'), 'no_clock_setup_v1.wgl')
        self.assertEqual(checker.timing_wgl(result, 'CCD')['status'], 'UNDETERMINED')
        result = self.run_wgl(WGL.replace('[0 X 1 0 1]', '[0 X 1 0]'))
        self.assertEqual(result['status'], 'ERROR')
        result = self.run_wgl(WGL.replace(' loop 2\n', ' loop 2\n jump 3;\n'))
        self.assertEqual(result['status'], 'ERROR')
        self.assertIn('Unsupported WGL pattern statement', result['error'])

    def test_wgl_truncated_after_chains_or_complete_vector_is_error(self):
        before_pattern = WGL.split(' pattern scan', 1)[0]
        result = self.run_wgl(before_pattern)
        self.assertEqual(result['chains'], 1)
        self.assertEqual(result['status'], 'ERROR')
        self.assertIn('Truncated', result['error'])
        self.assertEqual(checker.timing_wgl(result, 'CCD')['status'], 'ERROR')
        for text in (WGL.rsplit(' end', 1)[0], WGL.rsplit('end', 1)[0], WGL[:WGL.index('   1];')]):
            result = self.run_wgl(text)
            self.assertEqual(result['status'], 'ERROR')
            self.assertEqual(checker.timing_wgl(result, 'CCD')['status'], 'ERROR')

    def test_cli_wgl_timing_preview_only(self):
        result = self.run_wgl(BODY_WGL, 'sample_body_v1.wgl.gz')
        code, report = self.run_cli(result['file'], '--die', 'CCD', '--timing', '--channel-link', '--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(report['timing_summary'], {'PASS': 1})
        self.assertEqual(report['channel_link_summary'], {'WOULD_COPY': 1})
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_cli_timing_cannot_silently_approve_uncertain_or_failed_clock(self):
        source = self.run_wgl(WGL.replace('TCK_IN', 'TMS_IN'))
        code, report = self.run_cli(source['file'], '--die', 'CCD', '--timing')
        self.assertEqual(code, 2)
        self.assertEqual(report['timing_summary'], {'UNDETERMINED': 1})
        body = self.run_wgl(BODY_WGL.replace('10000ps', '20000ps'), 'wrong_body_v1.wgl.gz')
        code, report = self.run_cli(body['file'], '--die', 'CCD', '--timing', '--channel-link')
        self.assertEqual(code, 1)
        self.assertEqual(report['timing_summary'], {'FAIL': 1})
        self.assertEqual(report['channel_link_summary'], {'SKIPPED': 1})
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_wgl_copy_requires_timing_and_ccd_die(self):
        source = self.run_wgl(BODY_WGL, 'sample_body_v1.wgl.gz')
        code, report = self.run_cli(source['file'], '--die', 'CCD', '--channel-link')
        self.assertEqual(code, 2)
        self.assertIn('requires --timing', report['error'])
        code, report = self.run_cli(source['file'], '--die', 'IOD', '--timing')
        self.assertEqual(code, 2)
        self.assertEqual(report['timing_summary'], {'UNDETERMINED': 1})
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_stil_frequency_not_proved_by_period_alone(self):
        source = self.body_result(STIL + "Timing { WaveformTable w { Period '10ns'; } }")
        timing = checker.timing_stil(source['file'], 'CCD')
        self.assertEqual(timing['status'], 'UNDETERMINED')
        self.assertEqual(timing['periods_ps'], [10000])

    def test_plain_and_gzip(self):
        for compressed in (False, True):
            result = self.run_stil(compressed=compressed)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['pins'][0]['line'], 6)
            self.assertEqual(result['clocks_info_only'], ['CLK'])

    def test_multiple_scan_master_clocks(self):
        for compressed in (False, True):
            result = self.run_stil(STIL.replace('ScanMasterClock "CLK";',
                                  'ScanMasterClock "CLK" "bus"[2] OTHER;'), compressed)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['clocks_info_only'], ['BUS_2', 'CLK', 'OTHER'])
            self.assertEqual(len(result['pins']), 2)

    def test_invalid_scan_master_clock_list(self):
        result = self.run_stil(STIL.replace('ScanMasterClock "CLK";',
                              'ScanMasterClock "CLK" + "OTHER";'))
        self.assertEqual(result['status'], 'ERROR')

    def test_scanin_wrong_direction_fails(self):
        result = self.run_stil(STIL.replace('"data"[0]', '"RESULT"').replace('ScanOut "RESULT"', 'ScanOut "AUX"'))
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['pins'][0]['failure'], 'DIRECTION_MISMATCH')
        self.assertTrue(result['pins'][1]['passed'])

    def test_scanout_wrong_direction_fails(self):
        result = self.run_stil(STIL.replace('ScanOut "RESULT"', 'ScanOut "data"[0]'))
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['pins'][1]['failure'], 'DIRECTION_MISMATCH')

    def test_dpin_exempts_both_directions(self):
        result = self.run_stil(STIL.replace('"data"[0]', '"AUX"').replace('"RESULT"', '"AUX"'))
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(all(row['passed'] and not row['direction_matches'] for row in result['pins']))

    def test_custom_group_names_direction_order(self):
        self.groups = dict(zip(('custom_in', 'custom_out', 'custom_dp'), self.groups.values()))
        result = self.run_stil(STIL.replace('ScanOut "RESULT"', 'ScanOut "data"[0]'))
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['pins'][1]['expected_group'], 'custom_out')

    def test_swapped_directions_block_channellink(self):
        text = STIL.replace('ScanIn "data"[0]', 'ScanIn "RESULT"').replace('ScanOut "RESULT"', 'ScanOut "data"[0]')
        result = self.body_result(text)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(all(row['failure'] == 'DIRECTION_MISMATCH' for row in result['pins']))
        self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='IOD')['action'], 'SKIPPED')
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_wrong_direction_with_dpin_passes_but_not_chl(self):
        self.groups['stm_scan_nonclk_dpin'].add('RESULT')
        result = self.body_result(STIL.replace('ScanIn "data"[0]', 'ScanIn "RESULT"'))
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['eligible'])

    def test_both_direction_groups_allow_correct_role(self):
        self.groups['stm_scan_out'].add('DATA_0')
        result = self.body_result()
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['eligible'])

    def test_missing_pin(self):
        result = self.run_stil(STIL.replace('"RESULT"', '"MISSING"'))
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['pins'][1]['groups'], [])
        self.assertEqual(result['pins'][1]['failure'], 'PIN_NOT_FOUND')

    def test_absent_scanin(self):
        result = self.run_stil(STIL.replace('  ScanIn "data"[0];\n', ''))
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['absent_fields'], [{'chain': 'c', 'field': 'ScanIn'}])

    def test_no_pins_not_pass(self):
        self.assertEqual(self.run_stil('STIL 1.0;\n')['status'], 'UNDETERMINED')

    def test_multiple_blocks(self):
        self.assertEqual(self.run_stil(STIL + STIL)['chains'], 2)

    def test_comments_and_braces_in_quotes(self):
        text = STIL.replace('ScanChain c', 'ScanChain "c{foo}"').replace('ScanIn', '/* fake { } */ ScanIn')
        self.assertEqual(self.run_stil(text)['status'], 'PASS')

    def test_malformed_declaration(self):
        self.assertEqual(self.run_stil(STIL.replace('"data"[0];', '"data"[0]'))['status'], 'ERROR')

    def test_unclosed_block(self):
        self.assertEqual(self.run_stil(STIL.rsplit('}', 1)[0])['status'], 'ERROR')

    def test_unclosed_comment(self):
        self.assertEqual(self.run_stil(STIL + '/*')['status'], 'ERROR')

    def test_normalization(self):
        for value in ('"data"[0]', '"data[0]"', 'data[0]'):
            self.assertEqual(checker.normalize(value), 'DATA_0')
        with self.assertRaises(checker.ParseError):
            checker.normalize('"data"[0..3]')

    def test_missing_group(self):
        with self.assertRaises(checker.ParseError):
            checker.load_groups(self.pin, ('missing',))

    def test_group_cycle(self):
        self.pin.write_text(PIN.replace('Group nested { DATA_0 }', 'Group nested { stm_scan_in }'))
        with self.assertRaises(checker.ParseError):
            checker.load_groups(self.pin, checker.DEFAULT_GROUPS)

    def test_unknown_group_member(self):
        self.pin.write_text(PIN.replace('Group nested { DATA_0 }', 'Group nested { UNKNOWN }'))
        with self.assertRaises(checker.ParseError):
            checker.load_groups(self.pin, checker.DEFAULT_GROUPS)

    def test_truncated_gzip(self):
        path = self.root / 'bad.stil.gz'
        path.write_bytes(gzip.compress(STIL.encode())[:-8])
        self.assertEqual(checker.check_file(path, self.groups)['status'], 'ERROR')

    def body_result(self, text=STIL, filename='sample_body_v1_TC100.stil.gz'):
        path = self.root / filename
        path.write_bytes(gzip.compress(text.encode()) if filename.lower().endswith('.gz') else text.encode())
        return checker.check_file(path, self.groups)

    def run_cli(self, path, *arguments):
        with redirect_stdout(StringIO()) as output:
            code = checker.main([str(path), '--pin-file', str(self.pin), *arguments])
        return code, json.loads(output.getvalue())

    def test_channel_copy_and_repeat(self):
        result = self.body_result()
        source = Path(result['file'])
        original = source.read_bytes()
        plan = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='IOD')
        destination = self.root / 'v1_Channellink/sample_body_sc_v1_TC100.stil.gz'
        self.assertEqual(plan['action'], 'COPIED')
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='IOD')['action'], 'ALREADY_EXISTS')
        self.assertEqual(checker.find_inputs(self.root), [source])

    def test_nonclk_only_is_not_channel_link(self):
        result = self.body_result(STIL.replace('"RESULT"', '"AUX"'))
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['eligible'])

    def test_overlapping_groups_allow_channel_link(self):
        self.groups['stm_scan_nonclk_dpin'].add('DATA_0')
        result = self.body_result()
        self.assertTrue(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['eligible'])

    def test_channel_link_skip_non_candidate_and_failed_inputs(self):
        cases = [('sample_setup_v1.stil', STIL),
                 ('sample_body_setup_v1.stil', STIL),
                 ('sample_chain_setup_v1.stil', STIL),
                 ('sample_scan_chainlike_v1.stil', STIL),
                 ('sample_body_v1.stil', STIL.replace('"RESULT"', '"MISSING"')),
                 ('empty_body_v1.stil', 'STIL 1.0;'),
                 ('bad_body_v1.stil', STIL + '/*')]
        for filename, text in cases:
            with self.subTest(filename=filename):
                result = self.body_result(text, filename)
                self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='IOD')['action'], 'SKIPPED')
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_channel_link_dry_run(self):
        result = self.body_result()
        self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], True, die='IOD')['action'], 'WOULD_COPY')
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_chain_formats_follow_body_copy_path(self):
        for suffix in ('.stil', '.stil.gz', '.wgl', '.wgl.gz'):
            name = 'sample_CHAIN_V2' + suffix
            result = (self.run_wgl(BODY_WGL, name) if '.wgl' in suffix
                      else self.body_result(filename=name))
            source = Path(result['file'])
            original = source.read_bytes()
            for die in ('IOD', 'CCD'):
                with self.subTest(suffix=suffix, die=die):
                    plan = checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die=die)
                    destination = self.root / 'v2_Channellink' / ('sample_CHAIN_sc_V2' + suffix)
                    self.assertEqual(plan['action'], 'ELIGIBLE')
                    self.assertEqual(Path(plan['destination']), destination)
                    preview = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], True, die=die)
                    self.assertEqual(preview['action'], 'WOULD_COPY')
                    self.assertFalse(destination.exists())
            copied = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='CCD')
            self.assertEqual(copied['action'], 'COPIED')
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='CCD')['action'],
                             'ALREADY_EXISTS')
            self.assertNotIn(destination, checker.find_inputs(self.root))

    def test_chain_requires_directional_groups_in_both_formats(self):
        for suffix in ('.stil.gz', '.wgl.gz'):
            for pin in ('AUX', 'MISSING', 'data[0]'):
                with self.subTest(suffix=suffix, pin=pin):
                    name = 'sample_chain_v1' + suffix
                    result = (self.run_wgl(BODY_WGL.replace('"RESULT"', '"' + pin + '"'), name)
                              if '.wgl' in suffix else
                              self.body_result(STIL.replace('"RESULT"', '"' + pin + '"'), name))
                    self.assertEqual(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='CCD')['action'],
                                     'SKIPPED')
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_chain_versions_generated_outputs_and_drd_policy(self):
        for name, action in (('sample_chain.stil', 'ERROR'),
                             ('sample_chain_v1_v2.stil', 'ERROR'),
                             ('sample_chain_sc_v1.stil', 'SKIPPED'),
                             ('sample_chain_v1_CHL.stil', 'SKIPPED')):
            result = self.body_result(filename=name)
            self.assertEqual(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='CCD')['action'], action)
            self.assertEqual(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='DRD')['action'],
                             'NOT_CHECKED')

    def test_cli_chain_check_only_and_wgl_timing_gate(self):
        stil = self.body_result(filename='sample_chain_v1.stil.gz')
        wgl = self.run_wgl(BODY_WGL, 'sample_chain_v1.wgl.gz')
        before = set(self.root.rglob('*'))
        for result in (stil, wgl):
            code, report = self.run_cli(result['file'], '--die', 'CCD')
            self.assertEqual(code, 0)
            self.assertEqual(report['channel_link_summary'], {'ELIGIBLE': 1})
            self.assertFalse(report['channel_link_requested'])
        code, report = self.run_cli(wgl['file'], '--die', 'CCD', '--channel-link')
        self.assertEqual(code, 2)
        self.assertIn('requires --timing', report['error'])
        code, report = self.run_cli(wgl['file'], '--die', 'CCD', '--timing', '--channel-link', '--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(report['channel_link_summary'], {'WOULD_COPY': 1})
        failed = self.run_wgl(WGL, 'sample_chain_v1.wgl.gz')
        code, report = self.run_cli(failed['file'], '--die', 'CCD', '--timing', '--channel-link')
        self.assertEqual(code, 1)
        self.assertEqual(report['channel_link_summary'], {'SKIPPED': 1})
        code, report = self.run_cli(stil['file'], '--die', 'CCD', '--timing', '--channel-link')
        self.assertEqual(code, 2)  # Pin-only fixture cannot prove STIL timing.
        self.assertEqual(report['channel_link_summary'], {'SKIPPED': 1})
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_channel_link_conflict_never_overwrites(self):
        result = self.body_result()
        plan = checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')
        destination = Path(plan['destination'])
        destination.parent.mkdir()
        destination.write_bytes(b'keep me')
        self.assertEqual(checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='IOD')['action'], 'ERROR')
        self.assertEqual(destination.read_bytes(), b'keep me')

    def test_channel_link_plain_stil_and_absent_scanin(self):
        result = self.body_result(STIL.replace('  ScanIn "data"[0];\n', ''), 'sample_body_V2.stil')
        plan = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], die='CCD')
        self.assertEqual(plan['action'], 'COPIED')
        self.assertEqual(Path(plan['destination']).name, 'sample_body_sc_V2.stil')
        self.assertEqual(Path(plan['destination']).parent.name, 'v2_Channellink')

    def test_channel_link_missing_or_ambiguous_version(self):
        for name in ('sample_body.stil', 'sample_body_v1_v2.stil'):
            result = self.body_result(filename=name)
            self.assertEqual(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['action'], 'ERROR')

    def test_channel_link_generated_outputs_excluded(self):
        for name in ('sample_body_v1_CHL.stil', 'sample_body_sc_v1.stil',
                     'sample_body_SC_V2_TC100.STIL.GZ', 'sc_v3_sample_body.stil'):
            result = self.body_result(filename=name)
            self.assertFalse(checker.channel_link_plan(result, checker.DEFAULT_GROUPS[:2], die='IOD')['eligible'])
        self.assertEqual(checker.find_inputs(self.root), [])

    def test_channel_link_sc_insertion_and_output_location(self):
        parent = self.root / 'release_batch' / 'v1'
        parent.mkdir(parents=True)
        cases = [('sample_body_p1_v1.stil.gz', 'sample_body_p1_sc_v1.stil.gz', 'v1'),
                 ('sample_body_v1_p12.stil.gz', 'sample_body_sc_v1_p12.stil.gz', 'v1'),
                 ('sample_body_V2_extra.STIL.GZ', 'sample_body_sc_V2_extra.STIL.GZ', 'v2'),
                 ('v3_sample_body.stil', 'sc_v3_sample_body.stil', 'v3'),
                 ('sample_sc_body_v1.stil', 'sample_sc_body_sc_v1.stil', 'v1')]
        for name, output_name, version in cases:
            with self.subTest(name=name):
                source = parent / name
                original = gzip.compress(STIL.encode()) if name.lower().endswith('.gz') else STIL.encode()
                source.write_bytes(original)
                result = checker.check_file(source, self.groups)
                plan = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], True, die='IOD')
                self.assertEqual(plan['action'], 'WOULD_COPY')
                self.assertEqual(Path(plan['destination']), parent / (version + '_Channellink') / output_name)
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(Path(plan['destination']).parent.exists())
        self.assertFalse((parent.parent / 'v1_Channellink').exists())

    def test_drd_plan_and_export_are_not_checked(self):
        for name in ('sample_body_v1.stil', 'sample_body.stil', 'sample_body_v1_v2.stil'):
            result = self.body_result(filename=name)
            for dry_run in (False, True):
                with self.subTest(name=name, dry_run=dry_run), patch.object(checker, 'sha256') as digest:
                    plan = checker.export_channel_link(result, checker.DEFAULT_GROUPS[:2], dry_run, die='DRD')
                self.assertEqual(plan['action'], 'NOT_CHECKED')
                self.assertIsNone(plan['eligible'])
                self.assertIsNone(plan['destination'])
                digest.assert_not_called()
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_channel_link_invalid_die_is_rejected(self):
        with self.assertRaises(checker.ParseError):
            checker.channel_link_plan(self.body_result(), checker.DEFAULT_GROUPS[:2], die='OTHER')

    def test_cli_requires_die(self):
        result = self.body_result()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            self.run_cli(result['file'])
        self.assertEqual(error.exception.code, 2)

    def test_cli_drd_runs_basic_only(self):
        result = self.body_result()
        before = set(self.root.rglob('*'))
        with patch.object(checker, 'export_channel_link') as exporter:
            code, report = self.run_cli(result['file'], '--die', 'drd')
        exporter.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(report['summary'], {'PASS': 1})
        self.assertEqual(report['die'], 'DRD')
        self.assertFalse(report['channel_link_checks'])
        self.assertEqual(report['channel_link_summary'], {'NOT_CHECKED': 1})
        self.assertIsNone(report['files'][0]['channel_link']['eligible'])
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_cli_drd_rejects_copy_and_preview(self):
        result = self.body_result()
        for flags in (('--channel-link',), ('--channel-link', '--dry-run')):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                self.run_cli(result['file'], '--die', 'DRD', *flags)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / 'v1_Channellink').exists())

    def test_cli_iod_ccd_classify_without_writing(self):
        result = self.body_result()
        before = set(self.root.rglob('*'))
        for die in ('IOD', 'CCD'):
            with self.subTest(die=die), patch.object(checker, 'export_channel_link') as exporter:
                code, report = self.run_cli(result['file'], '--die', die)
            exporter.assert_not_called()
            self.assertEqual(code, 0)
            self.assertTrue(report['channel_link_checks'])
            self.assertFalse(report['channel_link_requested'])
            self.assertEqual(report['channel_link_summary'], {'ELIGIBLE': 1})
            self.assertEqual(Path(report['files'][0]['channel_link']['destination']).name,
                             'sample_body_sc_v1_TC100.stil.gz')
            self.assertEqual(set(self.root.rglob('*')), before)

    def test_cli_iod_ccd_preview_without_writing(self):
        result = self.body_result()
        before = set(self.root.rglob('*'))
        for die in ('IOD', 'CCD'):
            code, report = self.run_cli(result['file'], '--die', die, '--channel-link', '--dry-run')
            self.assertEqual(code, 0)
            self.assertEqual(report['channel_link_summary'], {'WOULD_COPY': 1})
            self.assertEqual(set(self.root.rglob('*')), before)

    def test_cli_iod_ccd_setup_is_not_eligible(self):
        result = self.body_result(filename='sample_setup_v1.stil')
        for die in ('IOD', 'CCD'):
            code, report = self.run_cli(result['file'], '--die', die)
            self.assertEqual(code, 0)
            self.assertEqual(report['channel_link_summary'], {'SKIPPED': 1})

    def test_cli_basic_dpin_pass_does_not_qualify(self):
        result = self.body_result(STIL.replace('"RESULT"', '"AUX"'))
        code, report = self.run_cli(result['file'], '--die', 'IOD')
        self.assertEqual(code, 0)
        self.assertEqual(report['summary'], {'PASS': 1})
        self.assertEqual(report['channel_link_summary'], {'SKIPPED': 1})

    def test_cli_basic_failure_is_not_hidden_by_die_policy(self):
        result = self.body_result(STIL.replace('ScanIn "data"[0]', 'ScanIn "RESULT"'))
        for die, action in (('DRD', 'NOT_CHECKED'), ('IOD', 'SKIPPED'), ('CCD', 'SKIPPED')):
            code, report = self.run_cli(result['file'], '--die', die)
            self.assertEqual(code, 1)
            self.assertEqual(report['summary'], {'FAIL': 1})
            self.assertEqual(report['channel_link_summary'], {action: 1})

    def test_cli_copy_is_explicit_and_preserves_original(self):
        result = self.body_result()
        source = Path(result['file'])
        original = source.read_bytes()
        code, report = self.run_cli(source, '--die', 'CCD', '--channel-link')
        self.assertEqual(code, 0)
        self.assertEqual(report['channel_link_summary'], {'COPIED': 1})
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(Path(report['files'][0]['channel_link']['destination']).read_bytes(), original)


if __name__ == '__main__':
    unittest.main()