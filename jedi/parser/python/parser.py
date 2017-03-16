import re

from jedi.parser.python import tree
from jedi.parser import tokenize
from jedi.parser.token import (DEDENT, INDENT, ENDMARKER, NEWLINE, NUMBER,
                               STRING, tok_name)
from jedi.parser.pgen2.parse import PgenParser
from jedi.parser.parser import ParserSyntaxError


class Parser(object):
    AST_MAPPING = {
        'expr_stmt': tree.ExprStmt,
        'classdef': tree.Class,
        'funcdef': tree.Function,
        'file_input': tree.Module,
        'import_name': tree.ImportName,
        'import_from': tree.ImportFrom,
        'break_stmt': tree.KeywordStatement,
        'continue_stmt': tree.KeywordStatement,
        'return_stmt': tree.ReturnStmt,
        'raise_stmt': tree.KeywordStatement,
        'yield_expr': tree.YieldExpr,
        'del_stmt': tree.KeywordStatement,
        'pass_stmt': tree.KeywordStatement,
        'global_stmt': tree.GlobalStmt,
        'nonlocal_stmt': tree.KeywordStatement,
        'print_stmt': tree.KeywordStatement,
        'assert_stmt': tree.AssertStmt,
        'if_stmt': tree.IfStmt,
        'with_stmt': tree.WithStmt,
        'for_stmt': tree.ForStmt,
        'while_stmt': tree.WhileStmt,
        'try_stmt': tree.TryStmt,
        'comp_for': tree.CompFor,
        'decorator': tree.Decorator,
        'lambdef': tree.Lambda,
        'old_lambdef': tree.Lambda,
        'lambdef_nocond': tree.Lambda,
    }

    def __init__(self, grammar, source, start_symbol='file_input',
                 tokens=None, start_parsing=True):
        # Todo Remove start_parsing (with False)

        self._used_names = {}

        self.source = source
        self._added_newline = False
        # The Python grammar needs a newline at the end of each statement.
        if not source.endswith('\n') and start_symbol == 'file_input':
            source += '\n'
            self._added_newline = True

        self._start_symbol = start_symbol
        self._grammar = grammar

        self._parsed = None

        if start_parsing:
            if tokens is None:
                tokens = tokenize.source_tokens(source, use_exact_op_types=True)
            self.parse(tokens)

    def parse(self, tokens):
        if self._parsed is not None:
            return self._parsed

        start_number = self._grammar.symbol2number[self._start_symbol]
        self.pgen_parser = PgenParser(
            self._grammar, self.convert_node, self.convert_leaf,
            self.error_recovery, start_number
        )

        self._parsed = self.pgen_parser.parse(tokens)

        if self._start_symbol == 'file_input' != self._parsed.type:
            # If there's only one statement, we get back a non-module. That's
            # not what we want, we want a module, so we add it here:
            self._parsed = self.convert_node(self._grammar,
                                             self._grammar.symbol2number['file_input'],
                                             [self._parsed])

        if self._added_newline:
            self.remove_last_newline()
        # The stack is empty now, we don't need it anymore.
        del self.pgen_parser
        return self._parsed

    def get_parsed_node(self):
        # TODO remove in favor of get_root_node
        return self._parsed

    def get_root_node(self):
        return self._parsed

    def error_recovery(self, grammar, stack, arcs, typ, value, start_pos, prefix,
                       add_token_callback):
        raise ParserSyntaxError('SyntaxError: invalid syntax', start_pos)

    def convert_node(self, grammar, type, children):
        """
        Convert raw node information to a Node instance.

        This is passed to the parser driver which calls it whenever a reduction of a
        grammar rule produces a new complete node, so that the tree is build
        strictly bottom-up.
        """
        symbol = grammar.number2symbol[type]
        try:
            return Parser.AST_MAPPING[symbol](children)
        except KeyError:
            if symbol == 'suite':
                # We don't want the INDENT/DEDENT in our parser tree. Those
                # leaves are just cancer. They are virtual leaves and not real
                # ones and therefore have pseudo start/end positions and no
                # prefixes. Just ignore them.
                children = [children[0]] + children[2:-1]
            return tree.Node(symbol, children)

    def convert_leaf(self, grammar, type, value, prefix, start_pos):
        # print('leaf', repr(value), token.tok_name[type])
        if type == tokenize.NAME:
            if value in grammar.keywords:
                return tree.Keyword(value, start_pos, prefix)
            else:
                name = tree.Name(value, start_pos, prefix)
                # Keep a listing of all used names
                arr = self._used_names.setdefault(name.value, [])
                arr.append(name)
                return name
        elif type == STRING:
            return tree.String(value, start_pos, prefix)
        elif type == NUMBER:
            return tree.Number(value, start_pos, prefix)
        elif type == NEWLINE:
            return tree.Newline(value, start_pos, prefix)
        elif type == ENDMARKER:
            return tree.EndMarker(value, start_pos, prefix)
        else:
            return tree.Operator(value, start_pos, prefix)

    def remove_last_newline(self):
        endmarker = self._parsed.children[-1]
        # The newline is either in the endmarker as a prefix or the previous
        # leaf as a newline token.
        prefix = endmarker.prefix
        if prefix.endswith('\n'):
            endmarker.prefix = prefix = prefix[:-1]
            last_end = 0
            if '\n' not in prefix:
                # Basically if the last line doesn't end with a newline. we
                # have to add the previous line's end_position.
                previous_leaf = endmarker.get_previous_leaf()
                if previous_leaf is not None:
                    last_end = previous_leaf.end_pos[1]
            last_line = re.sub('.*\n', '', prefix)
            endmarker.start_pos = endmarker.line - 1, last_end + len(last_line)
        else:
            newline = endmarker.get_previous_leaf()
            if newline is None:
                return  # This means that the parser is empty.

            assert newline.value.endswith('\n')
            newline.value = newline.value[:-1]
            endmarker.start_pos = \
                newline.start_pos[0], newline.start_pos[1] + len(newline.value)


class ParserWithRecovery(Parser):
    """
    This class is used to parse a Python file, it then divides them into a
    class structure of different scopes.

    :param grammar: The grammar object of pgen2. Loaded by load_grammar.
    :param source: The codebase for the parser. Must be unicode.
    :param module_path: The path of the module in the file system, may be None.
    :type module_path: str
    """
    def __init__(self, grammar, source, module_path=None, tokens=None,
                 start_parsing=True):
        self.syntax_errors = []

        self._omit_dedent_list = []
        self._indent_counter = 0
        self._module_path = module_path

        # TODO do print absolute import detection here.
        # try:
        #     del python_grammar_no_print_statement.keywords["print"]
        # except KeyError:
        #     pass  # Doesn't exist in the Python 3 grammar.

        # if self.options["print_function"]:
        #     python_grammar = pygram.python_grammar_no_print_statement
        # else:
        super(ParserWithRecovery, self).__init__(
            grammar, source,
            tokens=tokens,
            start_parsing=start_parsing
        )

    def parse(self, tokenizer):
        root_node = super(ParserWithRecovery, self).parse(self._tokenize(tokenizer))
        self.module = root_node
        self.module.used_names = self._used_names
        self.module.path = self._module_path
        return root_node

    def error_recovery(self, grammar, stack, arcs, typ, value, start_pos, prefix,
                       add_token_callback):
        """
        This parser is written in a dynamic way, meaning that this parser
        allows using different grammars (even non-Python). However, error
        recovery is purely written for Python.
        """
        def current_suite(stack):
            # For now just discard everything that is not a suite or
            # file_input, if we detect an error.
            for index, (dfa, state, (type_, nodes)) in reversed(list(enumerate(stack))):
                # `suite` can sometimes be only simple_stmt, not stmt.
                symbol = grammar.number2symbol[type_]
                if symbol == 'file_input':
                    break
                elif symbol == 'suite' and len(nodes) > 1:
                    # suites without an indent in them get discarded.
                    break
                elif symbol == 'simple_stmt' and len(nodes) > 1:
                    # simple_stmt can just be turned into a Node, if there are
                    # enough statements. Ignore the rest after that.
                    break
            return index, symbol, nodes

        index, symbol, nodes = current_suite(stack)
        if symbol == 'simple_stmt':
            index -= 2
            (_, _, (type_, suite_nodes)) = stack[index]
            symbol = grammar.number2symbol[type_]
            suite_nodes.append(tree.Node(symbol, list(nodes)))
            # Remove
            nodes[:] = []
            nodes = suite_nodes
            stack[index]

        # print('err', token.tok_name[typ], repr(value), start_pos, len(stack), index)
        if self._stack_removal(grammar, stack, arcs, index + 1, value, start_pos):
            add_token_callback(typ, value, start_pos, prefix)
        else:
            if typ == INDENT:
                # For every deleted INDENT we have to delete a DEDENT as well.
                # Otherwise the parser will get into trouble and DEDENT too early.
                self._omit_dedent_list.append(self._indent_counter)
            else:
                error_leaf = tree.ErrorLeaf(tok_name[typ].lower(), value, start_pos, prefix)
                stack[-1][2][1].append(error_leaf)

    def _stack_removal(self, grammar, stack, arcs, start_index, value, start_pos):
        failed_stack = []
        found = False
        all_nodes = []
        for dfa, state, (typ, nodes) in stack[start_index:]:
            if nodes:
                found = True
            if found:
                symbol = grammar.number2symbol[typ]
                failed_stack.append((symbol, nodes))
                all_nodes += nodes
        if failed_stack:
            stack[start_index - 1][2][1].append(tree.ErrorNode(all_nodes))

        stack[start_index:] = []
        return failed_stack

    def _tokenize(self, tokenizer):
        for typ, value, start_pos, prefix in tokenizer:
            # print(tokenize.tok_name[typ], repr(value), start_pos, repr(prefix))
            if typ == DEDENT:
                # We need to count indents, because if we just omit any DEDENT,
                # we might omit them in the wrong place.
                o = self._omit_dedent_list
                if o and o[-1] == self._indent_counter:
                    o.pop()
                    continue

                self._indent_counter -= 1
            elif typ == INDENT:
                self._indent_counter += 1

            yield typ, value, start_pos, prefix

    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, self.module)
