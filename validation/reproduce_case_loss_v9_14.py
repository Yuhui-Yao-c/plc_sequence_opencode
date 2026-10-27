"""Reproduce CASE candidate loss using generated native XML, without project data."""
from pathlib import Path
from collections import Counter
import argparse
import csv
import json
import sys
from lxml import etree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--expected', choices=('fixed', 'baseline'), default='fixed')
    args = parser.parse_args()
    repo, root = args.repo.resolve(), args.out.resolve()
    if root == repo or root in repo.parents:
        parser.error('--out must be a separate output directory')
    sys.path.insert(0, str(repo))
    from workflow import create
    from candidate_filter import match_boundary, glob_regex

    root.mkdir(parents=True, exist_ok=True)
    step = 'stEM100.ProcessCtrl.iCmdStep'
    cmd = 'stEM100.ProcessCtrl.iCmdNo'
    prefix = '\n'.join(f'IF {cmd} = {i} THEN Scratch := {i}; END_IF;' for i in range(1, 30))
    body = f'''CASE {step} OF
10: IF Done THEN {step} := 20; END_IF;
20: IF ResetPermit THEN {step} := 10; END_IF;
END_CASE;'''

    def source(code, dest):
        ns = 'www.iec.ch/public/TC65SC65BWG7TF10'
        xsi = 'http://www.w3.org/2001/XMLSchema-instance'
        project = etree.Element('{' + ns + '}Project', nsmap={None: ns, 'xsi': xsi})
        program = etree.SubElement(project, 'Program', name='Demo')
        main_body = etree.SubElement(program, 'MainBody')
        content = etree.SubElement(main_body, 'BodyContent', {'{' + xsi + '}type': 'ST', 'name': 'Logic'})
        content.text = etree.CDATA(code)
        dest.write_bytes(etree.tostring(project))

    def rows(path):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    results = []
    for name, code in [('clean', prefix + '\n' + body),
                       ('opaque_call', prefix + '\n' + body + '\nHousekeeping();')]:
        src = root / (name + '.xml')
        source(code, src)
        out = root / name
        create(src, out, pattern='stEM*.ProcessCtrl.iCmdStep', force=True)
        graph = json.loads((out / 'source_transition_graph.json').read_text(encoding='utf-8'))
        pool = rows(out / 'sequence_candidates.csv')
        results.append({
            'fixture': name,
            'step_inventory_kinds': dict(Counter(x['kind'] for x in graph['state_observations'] if x['variable'] == step)),
            'step_transitions': sum(x['variable'] == step for x in graph['transitions']),
            'proven_step_transitions': sum(x['variable'] == step and x['guard_complete'] for x in graph['transitions']),
            'step_candidates': sum(step in (x.get('开始标签', '') + x.get('结束标签', '')) for x in pool),
            'command_candidates': sum(cmd in (x.get('开始标签', '') + x.get('结束标签', '')) for x in pool),
            'filtered_candidates': len(rows(out / 'sequence_candidates_filtered.csv')),
            'collection_pairs': len(rows(out / 'prediction_collection_pairs.csv')),
            'atomic_review_rows': len(rows(out / 'prediction_atomic_review.csv')),
            'parse_warnings': graph['warnings'],
        })

    patterns = [
        {'input': pattern, 'compiled': glob_regex(pattern).pattern,
         'matches': match_boundary(step + '=10', step + '=20', pattern)}
        for pattern in ['stEM*.ProcessCtrl.iCmdStep', r'stEM*\.ProcessCtrl\.iCmdStep',
                        r're:stEM.*\.ProcessCtrl\.iCmdStep']
    ]
    clean, opaque = results
    checks = {
        'clean_counts': (clean['step_candidates'], clean['command_candidates'], clean['filtered_candidates'],
                         clean['collection_pairs'], clean['proven_step_transitions']) == (4, 29, 4, 2, 2),
        'opaque_counts': (opaque['step_candidates'], opaque['command_candidates'], opaque['filtered_candidates'],
                          opaque['collection_pairs'], opaque['proven_step_transitions'], opaque['atomic_review_rows'])
                         == ((4, 29, 4, 2, 0, 4) if args.expected == 'fixed' else (0, 29, 0, 0, 0, 0)),
        'pattern_semantics': [p['matches'] for p in patterns] == [True, False, True],
    }
    if args.expected == 'fixed':
        collection = rows(root / 'opaque_call' / 'prediction_collection_pairs.csv')
        graph = json.loads((root / 'opaque_call' / 'source_transition_graph.json').read_text(encoding='utf-8'))
        checks['partial_evidence_preserved'] = all('待核验' in row['证据等级'] for row in collection)
        checks['unknown_writer_blocked'] = any(row['variable'] == step for row in graph['blockers'])
    report = {'expected_version': args.expected, 'fixtures': results,
              'pattern_checks': patterns, 'checks': checks, 'success': all(checks.values())}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    (root / 'results.json').write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
