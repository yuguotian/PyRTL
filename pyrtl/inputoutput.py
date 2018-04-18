"""
Helper functions for reading and writing hardware files.

Each of the functions in inputoutput take a block and a file descriptor.
The functions provided either read the file and update the Block
accordingly, or write information from the Block out to the file.
"""

from __future__ import print_function, unicode_literals
import re
import collections

from .pyrtlexceptions import PyrtlError, PyrtlInternalError
from .core import working_block, _NameSanitizer
from .wire import WireVector, Input, Output, Const, Register
from .corecircuits import concat


# -----------------------------------------------------------------
#            __       ___
#    | |\ | |__) |  |  |
#    | | \| |    \__/  |


def input_from_blif(blif, block=None, merge_io_vectors=True):
    """ Read an open blif file or string as input, updating the block appropriately

    Assumes the blif has been flattened and their is only a single module.
    Assumes that there is only one single shared clock and reset
    Assumes that output is generated by Yosys with formals in a particular order
    Ignores reset signal (which it assumes is input only to the flip flops)
    """
    import pyparsing
    import six
    from pyparsing import (Word, Literal, OneOrMore, ZeroOrMore,
                           Suppress, Group, Keyword)

    block = working_block(block)

    try:
        blif_string = blif.read()
    except AttributeError:
        if isinstance(blif, six.string_types):
            blif_string = blif
        else:
            raise PyrtlError('input_blif expecting either open file or string')

    def SKeyword(x):
        return Suppress(Keyword(x))

    def SLiteral(x):
        return Suppress(Literal(x))

    def twire(x):
        """ find or make wire named x and return it """
        s = block.get_wirevector_by_name(x)
        if s is None:
            s = WireVector(bitwidth=1, name=x)
        return s

    # Begin BLIF language definition
    signal_start = pyparsing.alphas + '$:[]_<>\\\/'
    signal_middle = pyparsing.alphas + pyparsing.nums + '$:[]_<>\\\/.'
    signal_id = Word(signal_start, signal_middle)
    header = SKeyword('.model') + signal_id('model_name')
    input_list = Group(SKeyword('.inputs') + OneOrMore(signal_id))('input_list')
    output_list = Group(SKeyword('.outputs') + OneOrMore(signal_id))('output_list')

    cover_atom = Word('01-')
    cover_list = Group(ZeroOrMore(cover_atom))('cover_list')
    namesignal_list = Group(OneOrMore(signal_id))('namesignal_list')
    name_def = Group(SKeyword('.names') + namesignal_list + cover_list)('name_def')

    # asynchronous Flip-flop
    dffas_formal = (SLiteral('C=') + signal_id('C') +
                    SLiteral('R=') + signal_id('R') +
                    SLiteral('D=') + signal_id('D') +
                    SLiteral('Q=') + signal_id('Q'))
    dffas_keyword = SKeyword('$_DFF_PN0_') | SKeyword('$_DFF_PP0_')
    dffas_def = Group(SKeyword('.subckt') + dffas_keyword + dffas_formal)('dffas_def')

    # synchronous Flip-flop
    dffs_def = Group(SKeyword('.latch') +
                     signal_id('D') +
                     signal_id('Q') +
                     SLiteral('re') +
                     signal_id('C'))('dffs_def')
    command_def = name_def | dffas_def | dffs_def
    command_list = Group(OneOrMore(command_def))('command_list')

    footer = SKeyword('.end')
    model_def = Group(header + input_list + output_list + command_list + footer)
    model_list = OneOrMore(model_def)
    parser = model_list.ignore(pyparsing.pythonStyleComment)

    # Begin actually reading and parsing the BLIF file
    result = parser.parseString(blif_string, parseAll=True)
    # Blif file with multiple models (currently only handles one flattened models)
    assert(len(result) == 1)
    clk_set = set([])
    ff_clk_set = set([])

    def extract_inputs(model):
        start_names = [re.sub(r'\[([0-9]+)\]$', '', x) for x in model['input_list']]
        name_counts = collections.Counter(start_names)
        for input_name in name_counts:
            bitwidth = name_counts[input_name]
            if input_name == 'clk':
                clk_set.add(input_name)
            elif not merge_io_vectors or bitwidth == 1:
                block.add_wirevector(Input(bitwidth=1, name=input_name))
            else:
                wire_in = Input(bitwidth=bitwidth, name=input_name, block=block)
                for i in range(bitwidth):
                    bit_name = input_name + '[' + str(i) + ']'
                    bit_wire = WireVector(bitwidth=1, name=bit_name, block=block)
                    bit_wire <<= wire_in[i]

    def extract_outputs(model):
        start_names = [re.sub(r'\[([0-9]+)\]$', '', x) for x in model['output_list']]
        name_counts = collections.Counter(start_names)
        for output_name in name_counts:
            bitwidth = name_counts[output_name]
            if not merge_io_vectors or bitwidth == 1:
                block.add_wirevector(Output(bitwidth=1, name=output_name))
            else:
                wire_out = Output(bitwidth=bitwidth, name=output_name, block=block)
                bit_list = []
                for i in range(bitwidth):
                    bit_name = output_name + '[' + str(i) + ']'
                    bit_wire = WireVector(bitwidth=1, name=bit_name, block=block)
                    bit_list.append(bit_wire)
                wire_out <<= concat(*bit_list)

    def extract_commands(model):
        # for each "command" (dff or net) in the model
        for command in model['command_list']:
            # if it is a net (specified as a cover)
            if command.getName() == 'name_def':
                extract_cover(command)
            # else if the command is a d flop flop
            elif command.getName() == 'dffas_def' or command.getName() == 'dffs_def':
                extract_flop(command)
            else:
                raise PyrtlError('unknown command type')

    def extract_cover(command):
        netio = command['namesignal_list']
        if len(command['cover_list']) == 0:
            output_wire = twire(netio[0])
            output_wire <<= Const(0, bitwidth=1, block=block)  # const "FALSE"
        elif command['cover_list'].asList() == ['1']:
            output_wire = twire(netio[0])
            output_wire <<= Const(1, bitwidth=1, block=block)  # const "TRUE"
        elif command['cover_list'].asList() == ['1', '1']:
            # Populate clock list if one input is already a clock
            if(netio[1] in clk_set):
                clk_set.add(netio[0])
            elif(netio[0] in clk_set):
                clk_set.add(netio[1])
            else:
                output_wire = twire(netio[1])
                output_wire <<= twire(netio[0])  # simple wire
        elif command['cover_list'].asList() == ['0', '1']:
            output_wire = twire(netio[1])
            output_wire <<= ~ twire(netio[0])  # not gate
        elif command['cover_list'].asList() == ['11', '1']:
            output_wire = twire(netio[2])
            output_wire <<= twire(netio[0]) & twire(netio[1])  # and gate
        elif command['cover_list'].asList() == ['00', '1']:
            output_wire = twire(netio[2])
            output_wire <<= ~ (twire(netio[0]) | twire(netio[1]))  # nor gate
        elif command['cover_list'].asList() == ['1-', '1', '-1', '1']:
            output_wire = twire(netio[2])
            output_wire <<= twire(netio[0]) | twire(netio[1])  # or gate
        elif command['cover_list'].asList() == ['10', '1', '01', '1']:
            output_wire = twire(netio[2])
            output_wire <<= twire(netio[0]) ^ twire(netio[1])  # xor gate
        elif command['cover_list'].asList() == ['1-0', '1', '-11', '1']:
            output_wire = twire(netio[3])
            output_wire <<= (twire(netio[0]) & ~ twire(netio[2])) \
                | (twire(netio[1]) & twire(netio[2]))   # mux
        elif command['cover_list'].asList() == ['-00', '1', '0-0', '1']:
            output_wire = twire(netio[3])
            output_wire <<= (~twire(netio[1]) & ~twire(netio[2])) \
                | (~twire(netio[0]) & ~twire(netio[2]))
        else:
            raise PyrtlError('Blif file with unknown logic cover set "%s"'
                             '(currently gates are hard coded)' % command['cover_list'])

    def extract_flop(command):
        if(command['C'] not in ff_clk_set):
            ff_clk_set.add(command['C'])

        # Create register and assign next state to D and output to Q
        regname = command['Q'] + '_reg'
        flop = Register(bitwidth=1, name=regname)
        flop.next <<= twire(command['D'])
        flop_output = twire(command['Q'])
        flop_output <<= flop

    for model in result:
        extract_inputs(model)
        extract_outputs(model)
        extract_commands(model)


# ----------------------------------------------------------------
#    __       ___  __       ___
#   /  \ |  |  |  |__) |  |  |
#   \__/ \__/  |  |    \__/  |
#

def _trivialgraph_default_namer(thing, is_edge=True):
    """ Returns a "good" string for thing in printed graphs. """
    if is_edge:
        if thing.name is None or thing.name.startswith('tmp'):
            return ''
        else:
            return '/'.join([thing.name, str(len(thing))])
    elif isinstance(thing, Const):
        return str(thing.val)
    elif isinstance(thing, WireVector):
        return thing.name or '??'
    else:
        try:
            return thing.op + str(thing.op_param or '')
        except AttributeError:
            raise PyrtlError('no naming rule for "%s"' % str(thing))


def net_graph(block=None, split_state=False):
    """ Return a graph representation of the current block.

    Graph has the following form:
        { node1: { nodeA: edge1A, nodeB: edge1B},
          node2: { nodeB: edge2B, nodeC: edge2C},
          ...
        }

    aka: edge = graph[source][dest]

    Each node can be either a logic net or a WireVector (e.g. an Input, and Output, a
    Const or even an undriven WireVector (which acts as a source or sink in the network)
    Each edge is a WireVector or derived type (Input, Output, Register, etc.)
    Note that inputs, consts, and outputs will be both "node" and "edge".
    WireVectors that are not connected to any nets are not returned as part
    of the graph.
    """
    # FIXME: make it not try to add unused wires (issue #204)
    block = working_block(block)
    from .wire import Register
    # self.sanity_check()
    graph = {}

    # add all of the nodes
    for net in block.logic:
        graph[net] = {}

    wire_src_dict, wire_dst_dict = block.net_connections()
    dest_set = set(wire_src_dict.keys())
    arg_set = set(wire_dst_dict.keys())
    dangle_set = dest_set.symmetric_difference(arg_set)
    for w in dangle_set:
        graph[w] = {}
    if split_state:
        for w in block.wirevector_subset(Register):
            graph[w] = {}

    # add all of the edges
    for w in (dest_set & arg_set):
        try:
            _from = wire_src_dict[w]
        except Exception:
            _from = w
        if split_state and isinstance(w, Register):
            _from = w

        try:
            _to_list = wire_dst_dict[w]
        except Exception:
            _to_list = [w]

        for _to in _to_list:
            graph[_from][_to] = w

    return graph


def output_to_trivialgraph(file, namer=_trivialgraph_default_namer, block=None):
    """ Walk the block and output it in trivial graph format to the open file. """
    graph = net_graph(block)
    node_index_map = {}  # map node -> index

    # print the list of nodes
    for index, node in enumerate(graph):
        print('%d %s' % (index, namer(node, is_edge=False)), file=file)
        node_index_map[node] = index

    print('#', file=file)

    # print the list of edges
    for _from in graph:
        for _to in graph[_from]:
            from_index = node_index_map[_from]
            to_index = node_index_map[_to]
            edge = graph[_from][_to]
            print('%d %d %s' % (from_index, to_index, namer(edge)), file=file)


def _graphviz_default_namer(thing, is_edge=True, is_to_splitmerge=False):
    """ Returns a "good" graphviz label for thing. """
    if is_edge:
        if (thing.name is None or
                thing.name.startswith('tmp') or
                isinstance(thing, (Input, Output, Const, Register))):
            name = ''
        else:
            name = '/'.join([thing.name, str(len(thing))])
        penwidth = 2 if len(thing) == 1 else 6
        arrowhead = 'none' if is_to_splitmerge else 'normal'
        return '[label="%s", penwidth="%d", arrowhead="%s"]' % (name, penwidth, arrowhead)

    elif isinstance(thing, Const):
        return '[label="%d", shape=circle, fillcolor=lightgrey]' % thing.val
    elif isinstance(thing, (Input, Output)):
        return '[label="%s", shape=circle, fillcolor=none]' % thing.name
    elif isinstance(thing, Register):
        return '[label="%s", shape=square, fillcolor=gold]' % thing.name
    elif isinstance(thing, WireVector):
        return '[label="", shape=circle, fillcolor=none]'
    else:
        try:
            if thing.op == '&':
                return '[label="and"]'
            elif thing.op == '|':
                return '[label="or"]'
            elif thing.op == '^':
                return '[label="xor"]'
            elif thing.op == '~':
                return '[label="not"]'
            elif thing.op == 'x':
                return '[label="mux"]'
            elif thing.op in 'sc':
                return '[label="", height=.1, width=.1]'
            elif thing.op == 'r':
                name = thing.dests[0].name or ''
                return '[label="%s.next", shape=square, fillcolor=gold]' % name
            elif thing.op == 'w':
                return '[label="buf"]'
            else:
                return '[label="%s"]' % (thing.op + str(thing.op_param or ''))
        except AttributeError:
            raise PyrtlError('no naming rule for "%s"' % str(thing))


def output_to_graphviz(file, namer=_graphviz_default_namer, block=None):
    """ Walk the block and output it in graphviz format to the open file. """
    print(block_to_graphviz_string(block, namer), file=file)


def block_to_graphviz_string(block=None, namer=_graphviz_default_namer):
    """ Return a graphviz string for the block. """
    graph = net_graph(block, split_state=True)
    node_index_map = {}  # map node -> index

    rstring = """\
              digraph g {\n
              graph [splines="spline"];
              node [shape=circle, style=filled, fillcolor=lightblue1,
                    fontcolor=grey, fontname=helvetica, penwidth=0,
                    fixedsize=true];
              edge [labelfloat=false, penwidth=2, color=deepskyblue, arrowsize=.5];
              """

    # print the list of nodes
    for index, node in enumerate(graph):
        label = namer(node, is_edge=False)
        rstring += '    n%s %s;\n' % (index, label)
        node_index_map[node] = index

    # print the list of edges
    for _from in graph:
        for _to in graph[_from]:
            from_index = node_index_map[_from]
            to_index = node_index_map[_to]
            edge = graph[_from][_to]
            is_to_splitmerge = True if hasattr(_to, 'op') and _to.op in 'cs' else False
            label = namer(edge, is_to_splitmerge=is_to_splitmerge)
            rstring += '   n%d -> n%d %s;\n' % (from_index, to_index, label)

    rstring += '}\n'
    return rstring


def block_to_svg(block=None):
    """ Return an SVG for the block. """
    block = working_block(block)
    try:
        from graphviz import Source
        return Source(block_to_graphviz_string())._repr_svg_()
    except ImportError:
        raise PyrtlError('need graphviz installed (try "pip install graphviz")')


def trace_to_html(simtrace, trace_list=None, sortkey=None):
    """ Return a HTML block showing the trace. """

    from .simulation import SimulationTrace, _trace_sort_key
    if not isinstance(simtrace, SimulationTrace):
        raise PyrtlError('first arguement must be of type SimulationTrace')

    trace = simtrace.trace
    if sortkey is None:
        sortkey = _trace_sort_key

    if trace_list is None:
        trace_list = sorted(trace, key=sortkey)

    wave_template = (
        """\

        <script src="http://wavedrom.com/skins/default.js" type="text/javascript"></script>
        <script src="http://wavedrom.com/WaveDrom.js" type="text/javascript"></script>
        <script type="WaveDrom">
        { signal : [
        %s
        ]}
        </script>
        """
        )

    def extract(w):
        wavelist = []
        datalist = []
        last = None
        for i, value in enumerate(trace[w]):
            if last == value:
                wavelist.append('.')
            else:
                if len(w) == 1:
                    wavelist.append(str(value))
                else:
                    wavelist.append('=')
                    datalist.append(value)
                last = value

        wavestring = ''.join(wavelist)
        datastring = ', '.join(['"%d"' % data for data in datalist])
        if len(w) == 1:
            return bool_signal_template % (w, wavestring)
        else:
            return int_signal_template % (w, wavestring, datastring)

    bool_signal_template = '{ name: "%s",  wave: "%s" },'
    int_signal_template = '{ name: "%s",  wave: "%s", data: [%s] },'
    signals = [extract(w) for w in trace_list]
    all_signals = '\n'.join(signals)
    wave = wave_template % all_signals
    # print(wave)
    return wave


# ----------------------------------------------------------------
#         ___  __          __   __
#   \  / |__  |__) | |    /  \ / _`
#    \/  |___ |  \ | |___ \__/ \__>
#


class _VerilogSanitizer(_NameSanitizer):
    _ver_regex = '[_A-Za-z][_a-zA-Z0-9\$]*$'

    _verilog_reserved = \
        """always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos
        config deassign default defparam design disable edge else end endcase endconfig
        endfunction endgenerate endmodule endprimitive endspecify endtable endtask
        event for force forever fork function generate genvar highz0 highz1 if ifnone
        incdir include initial inout input instance integer join large liblist library
        localparam macromodule medium module nand negedge nmos nor noshowcancelledno
        not notif0 notif1 or output parameter pmos posedge primitive pull0 pull1
        pulldown pullup pulsestyle_oneventglitch pulsestyle_ondetectglitch remos real
        realtime reg release repeat rnmos rpmos rtran rtranif0 rtranif1 scalared
        showcancelled signed small specify specparam strong0 strong1 supply0 supply1
        table task time tran tranif0 tranif1 tri tri0 tri1 triand trior trireg unsigned
        use vectored wait wand weak0 weak1 while wire wor xnor xor
        """

    def __init__(self, internal_prefix='_sani_temp', map_valid_vals=True):
        self._verilog_reserved_set = frozenset(self._verilog_reserved.split())
        super(_VerilogSanitizer, self).__init__(self._ver_regex, internal_prefix,
                                                map_valid_vals, self._extra_checks)

    def _extra_checks(self, str):
        return(str not in self._verilog_reserved_set and  # is not a Verilog reserved keyword
               len(str) <= 1024)                          # not too long to be a Verilog id


def _verilog_vector_decl(w):
    return '' if len(w) == 1 else '[%d:0]' % (len(w) - 1)


def _verilog_vector_pow_decl(w):
    return '' if len(w) == 1 else '[%d:0]' % (2 ** len(w) - 1)


class OutputToVerilog(object):
    def __init__(self, dest_file, block=None):
        """ A class to walk the block and output it in verilog format to the open file """

        self.block = working_block(block)
        self.file = dest_file
        self.internal_names = _VerilogSanitizer('_verout_tmp_')
        for wire in self.block.wirevector_set:
            self.internal_names.make_valid_string(wire.name)
        self._to_verilog_comment()
        self._to_verilog_header()
        self._to_verilog_combinational()
        self._to_verilog_sequential()
        self._to_verilog_footer()

    def _varname(self, wire):
        """ Converts WireVectors to internal names """
        return self.internal_names[wire.name]

    def _to_verilog_comment(self):
        print('// Generated automatically via PyRTL', file=self.file)
        print('// As one initial test of synthesis, map to FPGA with:', file=self.file)
        print('//   yosys -p "synth_xilinx -top toplevel" thisfile.v\n', file=self.file)

    def _to_verilog_header(self):
        io_list = [self._varname(w) for w in self.block.wirevector_subset((Input, Output))]
        io_list.append('clk')
        io_list_str = ', '.join(io_list)
        print('module toplevel(%s);' % io_list_str, file=self.file)

        inputs = self.block.wirevector_subset(Input)
        outputs = self.block.wirevector_subset(Output)
        registers = self.block.wirevector_subset(Register)
        wires = self.block.wirevector_subset() - (inputs | outputs | registers)
        wire_regs = set()
        for net in self.block.logic:
            if net.op == 'm':
                wire_regs.add(net.dests[0])
        memory_nets = self.block.logic_subset(('m', '@'))
        memories = set()

        # Create a set of nets representitive of all memories (eliminating
        # duplicates caused by multiple ports).
        for m in memory_nets:
            if not any(m.op_param[0] == x.op_param[0] for x in memories):
                memories.add(m)

        for w in inputs:
            print('    input%s %s;' % (_verilog_vector_decl(w),
                                       self._varname(w)), file=self.file)
        print('    input clk;', file=self.file)
        for w in outputs:
            print('    output%s %s;' % (_verilog_vector_decl(w),
                                        self._varname(w)), file=self.file)
        print('', file=self.file)

        for w in registers:
            print('    reg%s %s;' % (_verilog_vector_decl(w),
                                     self._varname(w)), file=self.file)
        for w in wires:
            type = 'reg' if w in wire_regs else 'wire'
            print('    %s%s %s;' % (type, _verilog_vector_decl(w),
                                    self._varname(w)), file=self.file)
        print('', file=self.file)

        for w in memories:
            if w.op == 'm':
                print('    reg%s mem_%s%s;' % (_verilog_vector_decl(w.dests[0]),
                                               w.op_param[0],
                                               _verilog_vector_pow_decl(w.args[0])),
                      file=self.file)
            elif w.op == '@':
                print('    reg%s mem_%s%s;' % (_verilog_vector_decl(w.args[1]),
                                               w.op_param[0],
                                               _verilog_vector_pow_decl(w.args[0])),
                      file=self.file)

        print('', file=self.file)

        # Generate the initial block for those memories that need it (such as ROMs).
        # FIXME: Right now, the memblock is the only place where those rom values are stored
        # which is bad form (it means the functionality of tne hardware is not completely
        # contained in "core".
        mems_with_initials = [w for w in memories if hasattr(w.op_param[1], 'initialdata')]
        for w in mems_with_initials:
            print('    initial begin', file=self.file)
            for i in range(2**len(w.args[0])):
                print("        mem_%s[%d]=%d'h%x;" % (
                    w.op_param[0], i, len(w), w.op_param[1]._get_read_data(i)), file=self.file)
            print('    end', file=self.file)
            print('', file=self.file)

    def _to_verilog_combinational(self):
        for const in self.block.wirevector_subset(Const):
                print('    assign %s = %d;' % (self._varname(const), const.val), file=self.file)

        for net in self.block.logic:
            if net.op in set('w~'):  # unary ops
                opstr = '' if net.op == 'w' else net.op
                t = (self._varname(net.dests[0]), opstr, self._varname(net.args[0]))
                print('    assign %s = %s%s;' % t, file=self.file)
            elif net.op in '&|^+-*<>':  # binary ops
                t = (self._varname(net.dests[0]), self._varname(net.args[0]),
                     net.op, self._varname(net.args[1]))
                print('    assign %s = %s %s %s;' % t, file=self.file)
            elif net.op == '=':
                t = (self._varname(net.dests[0]), self._varname(net.args[0]),
                     self._varname(net.args[1]))
                print('    assign %s = %s == %s;' % t, file=self.file)
            elif net.op == 'x':
                # note that the argument order for 'x' is backwards from the ternary operator
                t = (self._varname(net.dests[0]), self._varname(net.args[0]),
                     self._varname(net.args[2]), self._varname(net.args[1]))
                print('    assign %s = %s ? %s : %s;' % t, file=self.file)
            elif net.op == 'c':
                catlist = ', '.join([self._varname(w) for w in net.args])
                t = (self._varname(net.dests[0]), catlist)
                print('    assign %s = {%s};' % t, file=self.file)
            elif net.op == 's':
                # someone please check if we need this special handling for scalars
                catlist = ', '.join([self._varname(net.args[0]) + '[%s]' % str(i)
                                     if len(net.args[0]) > 1 else self._varname(net.args[0])
                                     for i in reversed(net.op_param)])
                t = (self._varname(net.dests[0]), catlist)
                print('    assign %s = {%s};' % t, file=self.file)
            elif net.op == 'r':
                pass  # do nothing for registers
            elif net.op == 'm':  # use always block and assign as Verilog register
                print('    always @( posedge clk )', file=self.file)
                print('    begin', file=self.file)
                t = (self._varname(net.dests[0]), net.op_param[0], self._varname(net.args[0]))
                print('        %s <= mem_%s[%s];' % t, file=self.file)
                print('    end', file=self.file)
            elif net.op == '@':
                pass
            else:
                raise PyrtlInternalError("nets with op '{}' not supported".format(net.op))
        print('', file=self.file)

    def _to_verilog_sequential(self):
        print('    always @( posedge clk )', file=self.file)
        print('    begin', file=self.file)
        for net in self.block.logic:
            if net.op == 'r':
                t = (self._varname(net.dests[0]), self._varname(net.args[0]))
                print('        %s <= %s;' % t, file=self.file)
            elif net.op == '@':
                t = (self._varname(net.args[2]), net.op_param[0],
                     self._varname(net.args[0]), self._varname(net.args[1]))
                print(('        if (%s) begin\n'
                       '                mem_%s[%s] <= %s;\n'
                       '        end') % t, file=self.file)
        print('    end', file=self.file)

    def _to_verilog_footer(self):
        print('endmodule\n', file=self.file)


def output_verilog_testbench(dest_file, simulation_trace=None, block=None):
    """Output a verilog testbanch for the block/inputs used in the simulation trace."""
    block = working_block(block)
    inputs = block.wirevector_subset(Input)
    outputs = block.wirevector_subset(Output)
    ver_name = _VerilogSanitizer('_ver_out_tmp_')
    for wire in block.wirevector_set:
        ver_name.make_valid_string(wire.name)

    # Output header
    print('module tb();', file=dest_file)

    # Declare all block inputs as reg
    print('    reg clk;', file=dest_file)
    for w in inputs:
        print('    reg {:s} {:s};'.format(_verilog_vector_decl(w), ver_name[w.name]),
              file=dest_file)

    # Declare all block outputs as wires
    for w in outputs:
        print('    wire {:s} {:s};'.format(_verilog_vector_decl(w), ver_name[w.name]),
              file=dest_file)
    print('', file=dest_file)

    # Instantiate logic block
    io_list = [ver_name[w.name] for w in block.wirevector_subset((Input, Output))]
    io_list.append('clk')
    io_list_str = ['.{0:s}({0:s})'.format(w) for w in io_list]
    print('    toplevel block({:s});\n'.format(', '.join(io_list_str)), file=dest_file)

    # Generate clock signal
    print('    always', file=dest_file)
    print('        #0.5 clk = ~clk;\n', file=dest_file)

    # Move through all steps of trace, writing out input assignments per cycle
    print('    initial begin', file=dest_file)
    print('        $dumpfile ("waveform.vcd");', file=dest_file)
    print('        $dumpvars;\n', file=dest_file)
    print('        clk = 0;', file=dest_file)

    for i in range(len(simulation_trace)):
        for w in inputs:
            print('        {:s} = {:s}{:d};'.format(
                ver_name[w.name],
                "{:d}'d".format(len(w)),
                simulation_trace.trace[w][i]), file=dest_file)
        print('\n        #2', file=dest_file)

    # Footer
    print('        $finish;', file=dest_file)
    print('    end', file=dest_file)
    print('endmodule', file=dest_file)
