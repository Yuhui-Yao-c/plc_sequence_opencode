"""Discover CASE states independently of strict ST parsing.

Discovery is not a CFG proof. Recovered writes always remain partial; callers
must preserve unknown-writer blockers and must never set guard_complete=True.
"""
from __future__ import annotations
import re
from boundary_logic import IDENT, integer, symbol


def discover_cases(code):
    from successor_graph import st_tokens
    tokens = st_tokens(code)
    stack, observations, regions = [], [], []
    opening = {'IF': 'END_IF', 'FOR': 'END_FOR', 'WHILE': 'END_WHILE', 'REPEAT': 'UNTIL'}
    closing = set(opening.values())
    i, parentheses = 0, 0
    while i < len(tokens):
        word = tokens[i].upper()
        if word == '(':
            parentheses += 1
        elif word == ')':
            parentheses = max(0, parentheses - 1)
        if parentheses:
            i += 1
            continue
        if word == 'CASE':
            j = i + 1
            while j < len(tokens) and tokens[j].upper() != 'OF' and tokens[j] not in (';', ':'):
                j += 1
            if j < len(tokens) and tokens[j].upper() == 'OF':
                variable = symbol(' '.join(tokens[i + 1:j]))
                stack.append({'end': 'END_CASE', 'variable': variable if re.fullmatch(IDENT, variable) else None,
                              'start': i, 'body': j + 1})
                i = j + 1
                continue
        elif word in opening:
            stack.append({'end': opening[word]})
        elif word in closing or word == 'END_CASE':
            if stack and stack[-1]['end'] == word:
                item = stack.pop()
                if word == 'END_CASE' and item.get('variable'):
                    regions.append({**item, 'stop': i + 1, 'text': ' '.join(tokens[item['start']:i + 1])})
        elif (stack and stack[-1]['end'] == 'END_CASE' and stack[-1].get('variable')
              and (integer(tokens[i]) is not None or '..' in tokens[i])):
            # Numeric branch labels only, outside nested flow and call arguments.
            j = i
            while j < len(tokens) and tokens[j] not in (':', ';'):
                if tokens[j].upper() in ('IF', 'CASE', 'ELSE', 'END_CASE'):
                    break
                j += 1
            if j < len(tokens) and tokens[j] == ':':
                values = []
                for part in ' '.join(tokens[i:j]).split(','):
                    if '..' in part:
                        pieces = part.split('..')
                        if len(pieces) != 2:
                            values = []; break
                        a, b = map(integer, pieces)
                        if a is None or b is None or not 0 <= b - a <= 256:
                            values = []; break
                        values.extend(range(a, b + 1))
                    else:
                        n = integer(part)
                        if n is None:
                            values = []; break
                        values.append(n)
                for value in values:
                    observations.append({'variable': stack[-1]['variable'], 'value': value,
                                         'kind': 'case_selector', 'token': i, 'case_token': stack[-1]['start']})
                if values:
                    i = j + 1
                    continue
        i += 1
    return observations, regions


def recover_guarded_writes(code, parser=None, condition_parser=None):
    """Recover already parsed prefixes and independently bounded CASE fragments.

    Outer flow, preceding writes and opaque side effects are unresolved. These
    records are review candidates only, even when a fragment parses cleanly.
    """
    from successor_graph import STParser, WireError
    from boundary_logic import BoundarySyntaxError, comparisons
    recovered, seen = [], set()
    states, regions = discover_cases(code)
    selectors = {s['variable'].casefold() for s in states}

    def collect(p, offset, origin):
        for variable, dest, guard, position, rhs in p.writes:
            if (dest is None or variable.casefold() not in selectors
                    or not any(a.kind == 'eq' and a.args[0].casefold() == variable.casefold()
                               for a in comparisons(guard))):
                continue
            key = (variable.casefold(), dest, guard.key())
            if key not in seen:
                seen.add(key)
                recovered.append({'variable': variable, 'to': dest, 'guard': guard,
                                  'token': offset + position, 'origin': origin})

    if parser is not None:
        collect(parser, 0, 'parsed_prefix')
    for region in regions:
        partial = STParser(region['text'], condition_parser=condition_parser)
        try:
            partial.seq()
        except (WireError, BoundarySyntaxError):
            pass
        collect(partial, region['start'], 'case_fragment_outer_flow_unresolved')
    return recovered
