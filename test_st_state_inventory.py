"""CASE discovery survives unsupported ST without upgrading partial evidence."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from lxml import etree as E
from st_state_inventory import discover_cases, recover_guarded_writes
from workflow import create
from diagnose_labels import diagnose
from process_contract import review
from boundary_normalizer import Normalizer, RESOLVED


VARIABLE = 'Cell.Process.Stage'
CODE = f'''CASE {VARIABLE} OF
10: IF Done THEN {VARIABLE}:=20; END_IF;
20: IF ResetPermit THEN {VARIABLE}:=10; END_IF;
END_CASE;'''


def write_source(path, code, fmt):
    if fmt == 'native':
        ns='www.iec.ch/public/TC65SC65BWG7TF10';xsi='http://www.w3.org/2001/XMLSchema-instance'
        root=E.Element('{'+ns+'}Project',nsmap={None:ns,'xsi':xsi})
        p=E.SubElement(root,'Program',name='Demo');main=E.SubElement(p,'MainBody')
        body=E.SubElement(main,'BodyContent',{'{'+xsi+'}type':'ST','name':'Logic'})
        body.text=E.CDATA(code)
    elif fmt == 'plcopen':
        root=E.Element('project');p=E.SubElement(root,'pou',name='Demo')
        body=E.SubElement(E.SubElement(p,'body'),'ST');body.text=E.CDATA(code)
    else:
        root=E.Element('smc2');entry=E.SubElement(root,'entry',name='Main',type='json')
        entry.text=E.CDATA(json.dumps({'CLs':[{'__type':'IST','X':0,'Y':0,'Ix':1,'TXT':code}],'VLs':[]}))
    path.write_bytes(E.tostring(root))


def rows(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


class DiscoveryTests(unittest.TestCase):
    def test_two_case_states_need_no_explicit_comparison(self):
        states,_=discover_cases(CODE)
        self.assertEqual({x['value'] for x in states},{10,20})

    def test_comments_and_strings_are_not_states(self):
        states,_=discover_cases("(* CASE Fake OF 9: X:=1; END_CASE; *)\nText:='CASE Ghost OF 8: END_CASE;';")
        self.assertEqual(states,[])

    def test_ranges_and_multiple_labels(self):
        states,_=discover_cases('CASE Machine.Mode OF INT#1, 3..5: Flag:=TRUE; END_CASE;')
        self.assertEqual({x['value'] for x in states},{1,3,4,5})

    def test_nested_cases(self):
        states,_=discover_cases('CASE Outer.Stage OF 1: IF Run THEN CASE Inner.Stage OF 2: X:=TRUE; END_CASE; END_IF; 3: X:=FALSE; END_CASE;')
        self.assertEqual({(x['variable'],x['value']) for x in states},{('Outer.Stage',1),('Outer.Stage',3),('Inner.Stage',2)})

    def test_unsupported_loop_does_not_hide_selector(self):
        states,_=discover_cases('FOR i:=0 TO 2 DO '+CODE+' END_FOR;')
        self.assertEqual({x['value'] for x in states},{10,20})

    def test_computed_selector_is_not_invented(self):
        self.assertEqual(discover_cases('CASE Step+1 OF 1: X:=2; END_CASE;')[0],[])

    def test_assignment_only_is_not_a_controller(self):
        self.assertEqual(discover_cases('Counter:=10; Counter:=20;')[0],[])
        self.assertEqual(recover_guarded_writes('Counter:=10; Counter:=20;'),[])

    def test_fragment_after_unknown_call_remains_discoverable(self):
        found=recover_guarded_writes('Opaque(); '+CODE)
        self.assertEqual({(x['variable'],x['to']) for x in found},{(VARIABLE,10),(VARIABLE,20)})

    def test_incomplete_case_has_no_standalone_recovery(self):
        self.assertEqual(recover_guarded_writes('CASE Step OF 1: Step:=2;'),[])


class PipelineTests(unittest.TestCase):
    def run_case(self, code=CODE, fmt='native'):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);src=root/'source.xml';out=root/'run'
        write_source(src,code,fmt);create(src,out,pattern=VARIABLE)
        graph=json.loads((out/'source_transition_graph.json').read_text())
        return out,graph

    def assert_partial(self, out, graph):
        edges=[e for e in graph['transitions'] if e['variable']==VARIABLE]
        self.assertTrue(edges)
        self.assertTrue(all(e['guard_complete'] is False for e in edges))
        self.assertTrue(any(b['variable']==VARIABLE for b in graph['blockers']))
        pool=rows(out/'prediction_candidate_pairs.csv')
        self.assertTrue(any(r['开始标签']==VARIABLE+'=10' and r['结束标签']==VARIABLE+'=20' for r in pool))
        atomic=rows(out/'prediction_collection_pairs.csv')
        self.assertEqual({(r['开始标签'],r['结束标签']) for r in atomic},{(VARIABLE+'=10',VARIABLE+'=20'),(VARIABLE+'=20',VARIABLE+'=10')})
        self.assertTrue(all('待核验' in r['证据等级'] for r in atomic))
        self.assertTrue(rows(out/'prediction_atomic_review.csv'))
        self.assertNotIn(Normalizer(graph).normalize({'开始标签':VARIABLE+'=10','结束标签':'NOT ('+VARIABLE+'=10)'})['边界规范化状态'],RESOLVED)

    def test_native_clean_case_still_has_proven_edges(self):
        _,g=self.run_case()
        self.assertEqual(sum(e['variable']==VARIABLE and e['guard_complete'] for e in g['transitions']),2)

    def test_native_unknown_call_after_case(self):
        out,g=self.run_case(CODE+' Opaque();');self.assert_partial(out,g)

    def test_native_unknown_call_before_case(self):
        out,g=self.run_case('Opaque(); '+CODE);self.assert_partial(out,g)

    def test_native_unknown_rhs_keeps_blocker(self):
        out,g=self.run_case(CODE+' Param:=UnknownFunction();');self.assert_partial(out,g)

    def test_native_loop_keeps_global_proof_blocked(self):
        out,g=self.run_case('FOR i:=0 TO 2 DO '+CODE+' END_FOR;');self.assert_partial(out,g)

    def test_native_action_only_case_is_not_lost(self):
        out,g=self.run_case(f'CASE {VARIABLE} OF 10: Motor.Execute:=TRUE; 20: Motor.Execute:=FALSE; END_CASE;')
        self.assertEqual({s['value'] for s in g['state_observations'] if s['kind']=='case_selector'},{10,20})
        self.assertTrue(rows(out/'prediction_candidate_pairs.csv'))
        self.assertEqual(rows(out/'prediction_collection_pairs.csv'),[])

    def test_unknown_inside_first_branch_keeps_discovery(self):
        out,g=self.run_case(f'CASE {VARIABLE} OF 10: Opaque(); {VARIABLE}:=20; 20: {VARIABLE}:=10; END_CASE;')
        self.assertTrue(rows(out/'prediction_candidate_pairs.csv'))
        self.assertFalse(any(e['guard_complete'] for e in g['transitions']))

    def test_plcopen_small_case_is_admitted(self):
        out,g=self.run_case(fmt='plcopen')
        self.assertTrue(rows(out/'source_state_inventory.csv'))
        self.assertEqual(len(rows(out/'prediction_collection_pairs.csv')),2)

    def test_plcopen_unknown_after_case(self):
        out,g=self.run_case(CODE+' Opaque();',fmt='plcopen');self.assert_partial(out,g)

    def test_plcopen_unknown_before_case(self):
        out,g=self.run_case('Opaque(); '+CODE,fmt='plcopen');self.assert_partial(out,g)

    def test_smc2_failed_case_still_has_review_candidates(self):
        out,g=self.run_case(CODE+' Opaque();',fmt='smc2')
        self.assertTrue(rows(out/'prediction_candidate_pairs.csv'))
        self.assertTrue(rows(out/'prediction_atomic_review.csv'))
        self.assertFalse(any(e['guard_complete'] for e in g['transitions']))

    def test_legacy_adjacency_is_preserved_after_case_discovery(self):
        code=' '.join(f'IF {VARIABLE}={i} THEN Scratch:={i}; END_IF;' for i in (10,30,50))
        code+=f' CASE {VARIABLE} OF 20: Motor.Execute:=TRUE; END_CASE;'
        out,_=self.run_case(code,fmt='plcopen')
        pairs={(r['开始标签'],r['结束标签']) for r in rows(out/'sequence_candidates.csv') if r['状态']=='adjacency_hypothesis'}
        self.assertIn((VARIABLE+'=10',VARIABLE+'=30'),pairs)
        self.assertIn((VARIABLE+'=30',VARIABLE+'=50'),pairs)

    def test_filter_reports_glob_regex_confusion(self):
        from candidate_filter import filter_csv
        out,_=self.run_case()
        report=filter_csv(out/'sequence_candidates.csv',out/'bad_pattern.csv',r'Cell\.Process\.Stage')
        self.assertEqual(report['after'],0)
        self.assertEqual(report['pattern_mode'],'glob')
        self.assertTrue(report['pattern_warnings'])

    def test_diagnostic_reports_blockers_and_error_position(self):
        out,g=self.run_case(CODE+' Opaque();')
        result=diagnose(out,VARIABLE+'=10',graph=g)
        self.assertTrue(result['源码阻断项']);self.assertTrue(result['ST解析问题'])
        self.assertIsInstance(result['ST解析问题'][0]['error_token'],int)

    def test_partial_candidate_cannot_become_process_recommendation(self):
        out,g=self.run_case(CODE+' Opaque();')
        pool=rows(out/'indexed_candidates.csv');candidates={r['候选ID']:r for r in pool}
        chosen=next(r for r in pool if r['状态']=='native_connected_transition_unverified')
        row={'过程类型':'机械动作','统计对象':'Device','起点含义':'开始','终点含义':'结束',
             '工艺依据':'source','边界依据':'fragment','开始标签':chosen['开始标签'],'结束标签':chosen['结束标签'],
             '证据候选ID':[chosen['候选ID']]}
        self.assertEqual(review(row,candidates,{})['工艺契约结果'],'待补充证据')


if __name__=='__main__':
    unittest.main()
