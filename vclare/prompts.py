"""LLM prompts used by the VClare framework.

This module contains the prompts that the framework sends to the language
model at the non-arbitration stages. The two human confirmation points
(``inconsistency_pair`` and ``behavior_choice``) are not LLM calls and therefore
have no prompt here; they are handled by :mod:`vclare.arbiter`.

Two prompt groups are provided:

* ``MINING_*``: semantic inconsistency mining, used by
  ``stage1_mine_inconsistency``.
* ``REPAIR_*``: targeted repair of a confirmed inconsistency, used by
  ``stage3_repair_spec``.
* ``VERILOG_*`` and ``RTL_4_SHOT_EXAMPLES``: Verilog RTL generation, used by
  ``stage4_generate_candidates``.
* ``TESTCASE_GENERATION_PROMPT``: testbench testcase generation, used by
  ``stage5_generate_testbench``.
* ``EXTRA_TESTCASE_PROMPT``: additional testcases appended after the base
  testbench, used by ``stage5e_add_extra_testcases``.

The blind-fix prompt (repairing a specification without any mined inconsistency)
is not part of this release because it belongs to the Blind Fix baseline rather
than to the VClare framework.

The Verilog generation prompts are adapted from VerilogCoder:

    @misc{ho2024verilogcoderautonomousverilogcoding,
      title={VerilogCoder: Autonomous Verilog Coding Agents with Graph-based
             Planning and Abstract Syntax Tree (AST)-based Waveform Tracing Tool},
      author={Chia-Tung Ho and Haoxing Ren and Brucek Khailany},
      year={2024},
      eprint={2408.08927},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2408.08927},
    }
"""

MINING_SYSTEM_PROMPT = (
    "You are a hardware specification analyst. "
    "Your job is to identify self-inconsistencies, contradictions, ambiguities, "
    "or omissions that exist WITHIN the given specification itself.\n\n"
    "Workflow:\n"
    "1. Reason step-by-step through the specification: read each section and note "
    "any statements that conflict with, omit details from, or are made vague "
    "relative to another statement elsewhere in the SAME document.\n"
    "2. Select the 3 most significant inconsistencies you found.\n"
    "3. For each inconsistency, quote the TWO relevant passages in full \u2014 do NOT "
    "truncate or paraphrase. Each quote must be a complete sentence or clause "
    "exactly as it appears in the specification.\n"
    "4. After your reasoning, output EXACTLY the following block (no other text after it):\n"
    "```json\n"
    '[{"source1": "<full verbatim quote A>", "source2": "<full verbatim quote B>"},\n'
    ' {"source1": "<full verbatim quote C>", "source2": "<full verbatim quote D>"},\n'
    ' {"source1": "<full verbatim quote E>", "source2": "<full verbatim quote F>"}]\n'
    "```"
)

MINING_USER_PROMPT = (
    "Identify exactly 3 potential self-inconsistencies in this hardware specification. "
    "Reason thoroughly, then emit the ```json block with full verbatim quotes.\n\n"
    "Specification:\n{spec}"
)

REPAIR_SYSTEM_PROMPT = (
    "You are a hardware specification engineer. "
    "Produce a corrected version of the given specification making only the minimal "
    "necessary changes to fix identified issues. "
    "Format: reasoning section first (outside any code blocks), then the corrected "
    "specification inside a ```md code block."
)

REPAIR_USER_PROMPT = (
    "The specification contains a known inconsistency.\n\n"
    "Inconsistency #{index}:\n"
    '  source 1: "{src1}"\n'
    '  source 2: "{src2}"\n\n'
    "Resolution: '{ans}' is correct.\n"
    '  Correct statement  : "{believed}"\n'
    '  Incorrect statement: "{rejected}"\n\n'
    "Fix the specification so it consistently uses the correct statement, "
    "making only the minimal necessary changes.\n\n"
    "Original specification:\n{defective_spec}\n\n"
    "Return reasoning then the corrected specification in a ```md code block."
)

VERILOG_SYSTEM_PROMPT = r"""
"Complete the following Verilog code based on the requirements. Generate a complete Verilog module, encapsulated in ```verilog\n ... ```."""

VERILOG_GENERATION_PROMPT = r"""
Please write a module in Verilog RTL language regarding to the given natural language specification.
Try to understand the requirements above and give reasoning steps in natural language to achieve it.
In addition, try to give advice to avoid syntax error.
An Verilog RTL module always starts with a line starting with the keyword 'module' followed by the module name.
It ends with the keyword 'endmodule'.

[Hints]:
For implementing kmap (Karnaugh map), you need to think step by step.
Carefully example how the kmap in input_spec specifies the order of the inputs.
Note that x[i] in x[N:1] means x[i-1] in x[N-1:0].
Then find the inputs corresponding to output=1, 0, and don't-care for each case.

Note in Verilog, for a signal "logic x[M:N]" where M > N, you CANNOT reversely select bits from it like x[1:2];
Instead, you should use concatations like {{x[1], x[2]}}.

The module interface should EXACTLY MATCH module_interface if given.
Otherwise, should EXACTLY MATCH with the description in input_spec.
(Including the module name, input/output ports names, and their types)


{examples_prompt}
<input_spec>
{input_spec}
</input_spec>
"""

VERILOG_EXTRA_ORDER_PROMPT = r"""
Other requirements:
1. Don't use state_t to define the parameter. Use `localparam` or Use 'reg' or 'logic' for signals as registers or Flip-Flops.
2. Declare all ports and signals as logic.
3. Not all the sequential logic need to be reset to 0 when reset is asserted,
    but these without-reset logic should be initialized to a known value with an initial block instead of being X.
4. For combinational logic with an always block do not explicitly specify the sensitivity list; instead use always @(*).
5. NEVER USE 'inside' operator in RTL code. Code like 'state inside {STATE_B, STATE_C, STATE_D}' should NOT be used.
6. Never USE 'unique' or 'unique0' keywords in RTL code. Code like 'unique case' should NOT be used.
"""

VERILOG_IF_PROMPT = r"""
The module interface is given below:
<module_interface>
{module_interface}
</module_interface>
"""

VERILOG_TB_CONTEXT_PROMPT = r"""
Another agent has generated a testbench regarding the given input_spec:
<testbench>
{testbench}
</testbench>
"""

RTL_4_SHOT_EXAMPLES = """
Here are some examples of RTL Verilog code:
Example 1:
<example>
    <input_spec>
        Implement the Verilog module based on the following description.
        Assume that sigals are positive clock/clk triggered unless otherwise stated.

        The module should implement a XOR gate.
    </input_spec>
    <module>
    ```verilog
        module TopModule(
            input  logic in0,
            input  logic in1,
            output logic out
        );

            assign out = in0 ^ in1;

        endmodule
    ```
    </module>
</example>
Example 2:
<example>
    <input_spec>
        Implement the Verilog module based on the following description.
        Assume that sigals are positive clock/clk triggered unless otherwise stated.

        The module should implement an 8-bit registered incrementer.
        The 8-bit input is first registered and then incremented by one on the next cycle.
        The reset input is active high synchronous and should reset the output to zero.
    </input_spec>
    <module>
    ```verilog
        module TopModule(
            input  logic       clk,
            input  logic       reset,
            input  logic [7:0] in_,
            output logic [7:0] out
        );

            // Sequential logic
            logic [7:0] reg_out;
            always @( posedge clk ) begin
                if ( reset )
                reg_out <= 0;
                else
                reg_out <= in_;
            end

            // Combinational logic
            logic [7:0] temp_wire;
            always @(*) begin
                temp_wire = reg_out + 1;
            end

            // Structural connections
            assign out = temp_wire;

        endmodule
    </module>
    ```
</example>
Example 3:
<example>
    <input_spec>
        Implement the Verilog module based on the following description.
        Assume that sigals are positive clock/clk triggered unless otherwise stated.

        The module should implement an n-bit registered incrementer where the bitwidth is specified by the parameter nbits.
        The n-bit input is first registered and then incremented by one on the next cycle.
        The reset input is active high synchronous and should reset the output to zero.
    </input_spec>
    <module>
    ```verilog
        module TopModule #(
            parameter nbits
        )(
            input  logic             clk,
            input  logic             reset,
            input  logic [nbits-1:0] in_,
            output logic [nbits-1:0] out
        );

            // Sequential logic
            logic [nbits-1:0] reg_out;
            always @( posedge clk ) begin
                if ( reset )
                reg_out <= 0;
                else
                reg_out <= in_;
            end

            // Combinational logic
            logic [nbits-1:0] temp_wire;
            always @(*) begin
                temp_wire = reg_out + 1;
            end

            // Structural connections
            assign out = temp_wire;

        endmodule
    ```
    </module>
</example>
Example 4:
<example>
    <input_spec>
        Implement the Verilog module based on the following description.
        Assume that sigals are positive clock/clk triggered unless otherwise stated.

        Build a finite-state machine that takes as input a serial bit stream,
            and outputs a one whenever the bit stream contains two consecutive one's.
        The output is one on the cycle _after_ there are two consecutive one's.
        The reset input is active high synchronous,
            and should reset the finite-state machine to an appropriate initial state.
    </input_spec>
    <module>
    ```verilog
        module TopModule(
            input  logic clk,
            input  logic reset,
            input  logic in_,
            output logic out
        );

            // State enum
            localparam STATE_A = 2'b00;
            localparam STATE_B = 2'b01;
            localparam STATE_C = 2'b10;

            // State register
            logic [1:0] state;
            logic [1:0] state_next;
            always @(posedge clk) begin
                if ( reset )
                state <= STATE_A;
                else
                state <= state_next;
            end

            // Next state combinational logic
            always @(*) begin
                state_next = state;
                case ( state )
                STATE_A: state_next = ( in_ ) ? STATE_B : STATE_A;
                STATE_B: state_next = ( in_ ) ? STATE_C : STATE_A;
                STATE_C: state_next = ( in_ ) ? STATE_C : STATE_A;
                endcase
            end

            // Output combinational logic
            always @(*) begin
                out = 1'b0;
                case ( state )
                STATE_A: out = 1'b0;
                STATE_B: out = 1'b0;
                STATE_C: out = 1'b1;
                endcase
            end

        endmodule
        ```
    </module>
</example>
"""

TESTCASE_GENERATION_PROMPT = """I will give you a circuit specification and the header of the 'DUT'(design under test), you need to understand the function of this circuit first, then give me some testcases to check the function of this circuit, if it's a sequential circuit, each testcases should begin with reset and end with output check. Here's specification:"{spec}", here's header:"{header}". You should consider these cases:
1.Reset Testing: If it is a sequential circuit, when the module initializes, it should reset the signals.
2.Core Case Testing: Generate essential core test cases based on the problem description.

If this is a combinational circuit, your output format should be as follows:
case x: input_port1 = xx, input_port2 = xx, input_port3 = xx, key_variable1 = xx, output_port1 = xx
For example:
case 1: sel = 0, b = 1, a = 0, out = 0
case 2: sel = 1, b = 0, a = 1, out = 0

If this is a sequential circuit, your output format should be as follows:
case x: 
time_line0: input_port1 = xx, input_port2 = xx, output_port1 = xx
time_line1: input_port1 = xx, input_port2 = xx, output_port1 = xx

You should put all testcases between ```verilog and ```.
NOTE: only output the parts related to the testcases, no need to embed them in the testbench template."""

TESTCASE_SYSTEM_PROMPT = (
    "You are an expert in Verilog testbench generation. "
    "Generate comprehensive test cases."
)

EXTRA_TESTCASE_SYSTEM_PROMPT = (
    "You are an expert in Verilog testbench generation. "
    "Provide only additional testcase blocks."
)

EXTRA_TESTCASE_PROMPT = (
    "I will give you a circuit specification and a Verilog testbench containing multiple testcases.\n"
    "The original module specification may be flawed or incomplete, or have contradictory information. Please examine the testcases in the testbench\n"
    "Insert 2-4 additional testcases that follow the same structure as existing ones. Reply ONLY with task definitions (no initial/module/endmodule and no task calls),"
    " and name tasks using raw_testcases numbering starting from {next_raw_name}."
    " Do NOT use task names testcases1/testcases2/etc."
    " Do NOT change other parts of the testbench. For sequential circuits ensure each added testcase begins with `reset_dut();` if applicable.\n\n"
    "Each task MUST end with a single $display line that prints inputs first, then outputs.\n"
    "The display format MUST follow this pattern (use % as format placeholder):\n"
    "$display(\"[check] <IN1>: %<fmt>, <IN2>: %<fmt>, <OUT1>: %<fmt>, ...\", IN1, IN2, OUT1, ...);\n"
    "Example: $display(\"[check] y: %h, w: %b, Y2: %b, Y4: %b\", y, w, Y2, Y4);\n"
    "Use %b, %h, etc. as appropriate for each signal.\n"
    "Only reply with Verilog code (the testcases) between ```verilog and ```.\n\n"
    "Specification:\n"
    "{specification}\n\n"
    "Current testbench:\n"
    "{tb_content}\n"
)

# Appended by PipelineFull.stagex1_direct_testcases_generation after the base
# testcase prompt. It forces the generated tasks into the shape that the
# iverilog post-processing expects.
TESTCASE_OUTPUT_CONTRACT = (
    "\n\nStrict output contract for integration:\n"
    "1) Output ONLY task definitions between ```verilog and ```.\n"
    "2) Do NOT output module/endmodule/initial blocks.\n"
    "3) Name each task as raw_testcases1, raw_testcases2, ...\n"
    "4) Do NOT use task names testcases1/testcases2/etc.\n"
    "5) For sequential circuits, each task must begin with reset_dut();\n"
    "6) Each task MUST end with a single $display line that prints inputs first, then outputs.\n"
    "   The display format MUST follow this pattern (use % as format placeholder):\n"
    "   $display(\"[check] <IN1>: %<fmt>, <IN2>: %<fmt>, <OUT1>: %<fmt>, ...\", IN1, IN2, OUT1, ...);\n"
    "   Example: $display(\"[check] y: %h, w: %b, Y2: %b, Y4: %b\", y, w, Y2, Y4);\n"
    "   Use %b, %h, etc. as appropriate for each signal.\n"
)
