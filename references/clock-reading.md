# Understanding and checking scan clocks in STIL

This reference preserves the taught interpretation using **synthetic pin/chain names**. The optional `--timing` STIL checker now implements this evidence path for its supported execution subset. The numerical example itself is not a period specification.

## Evidence chain

1. Read each `ScanChain`'s `ScanMasterClock` to identify its scan clock. Multiple clocks are possible; do not hard-code one pin name.
2. Resolve the pin's position in ordered `SignalGroups` (for example `_pi_`), including referenced groups if present. Being an input does not prove a fixed index. If a particular `_pi_` ends with `... + "SCAN_CLK" + "AUX_OUT"`, SCAN_CLK is second from last **only in that group**.
3. Expand data notation such as `\r538 0` (538 repetitions of 0), then map waveform characters to ordered group members. Preserve X/Z and other waveform characters; they are not simply binary levels.
4. Track the active `W <table>` and applicable `Timing`/`Spec Category` selection. Resolve `Period` and waveform expressions against the correct category rather than selecting a name such as RETARGET unconditionally.
5. Resolve the clock's waveform group and the character assigned by the current vector or macro. A waveform character `1` may encode an entire pulse, not a constant high level.

## Vector inheritance

`V { ... }` is a vector execution. `V {}` also executes a vector, inheriting applicable prior waveform-character assignments. Pins omitted from a vector retain their assignments under the applicable STIL execution context. An empty vector does not imply the clock stops: an inherited pulsing character can execute again. Track the active waveform table as well; inheritance of a character does not by itself prove unchanged edge timing.

## MacroDefs, Macro and Shift

Synthetic example:

```stil
MacroDefs {
  "scan_shift" {
    W scan_table;
    Shift { V { "scan_channel" = #; "SCAN_CLK" = 1; } }
    W scan_table;
  }
}
```

The `Macro "scan_shift" { ... }` call supplies data to `#` in the corresponding definition. In this simple one-vector Shift body, the supplied channel bit strings expand **in parallel** to shift vectors: one position from each channel per shift iteration, not all channels concatenated. Validate the actual Shift body, lengths and padding rules for other cases; not every macro has this structure.

Here `SCAN_CLK = 1` is assigned explicitly in the repeated Shift vector. It is not merely a one-time initialization. Each expanded shift vector executes waveform character 1 for that clock. A Macro call only implies clock toggling when its actual definition, execution and selected waveform establish this; unrelated macros may not toggle any clock.

## Numerical example (not a requirement)

Suppose the clock belongs to `input_time_gen_1` and the applicable Category specifies:

| Parameter | Value |
|---|---:|
| Time_force_pi | 0 ps |
| Time_measure_po | 2000 ps |
| Time_pulse_1 | 3000 ps |
| Time_pulse_2 | 5000 ps |
| Time_period | 10000 ps |
| strobe_window_tmp | 1 ps |

```stil
Period 'Time_period';
input_time_gen_1 {
  01 { 'Time_force_pi' D; 'Time_pulse_1' D/U;
       'Time_pulse_1+Time_pulse_2' D; }
}
```

The `01` characters map positionally to D/U at Time_pulse_1. For character `1`:

- 0 ns: drive low (D).
- 3 ns: drive high (U).
- 8 ns: drive low (D).
- 10 ns: vector period ends.

Thus consecutive shift vectors using character 1 produce one pulse per 10 ns vector, with 5 ns high time. Character 0 stays low in this example. Distinguish pulse width from repetition period. A 10 ns WaveformTable alone does not prove a clock toggles every 10 ns: assignments, macros, loops and selected tables matter.

## Reporting guardrails

- Explain actual clock identity and transitions from the inspected file; do not infer from unrelated TX/RX names.
- Do not label the numerical example as required timing.
- With `--timing`, setup checks executed TCK cadence (CCD 10 MHz, IOD/DRD 25 MHz); body checks consecutive completed 100 MHz pulses on each ScanMasterClock; chain requires both. Pin membership remains a separate check.
- Full-file parsing and supported execution are required for timing PASS. Unknown clock drive, unsupported syntax or unresolved context cannot PASS. Idle holds and trailing static levels are distinguished from repeated wrong-frequency trains; measured widths and intervals are reported.
- For NRZ TCK, consecutive non-target intervals fail even with varying lengths. Scan clocks may have explicit inactive vectors between shift/capture operations; frequency checks apply to adjacent pulsing vectors, not the spacing across gated-off operations.
- This knowledge can be used to answer clock questions even though pin checking has no clock PASS/FAIL result.