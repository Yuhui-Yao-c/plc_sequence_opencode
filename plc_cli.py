#!/usr/bin/env python3
"""Two isolated user workflows. Prediction never opens ground truth."""
import argparse, json, sys, hashlib
from pathlib import Path
from workflow import create, validate
from pair_evaluation import run as evaluate_candidates

def fingerprint(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
 return h.hexdigest()

def predict(args):
 src=Path(args.source).resolve()
 if not src.is_file():raise FileNotFoundError(f'PLC源码不存在：{src}')
 if src.suffix.lower() not in ('.xml','.smc2','.txt'):raise ValueError('源码需为XML/SMC2容器或其文本导出')
 out=Path(args.out).resolve()
 # Deliberately no --gt argument or gold-file discovery in prediction.
 if out==src.parent or out==src:raise ValueError('输出必须位于独立目录，不能覆盖源码所在目录')
 manifest=create(str(src),str(out),batch_size=args.batch_size,pattern=args.pattern,force=args.force)
 prov={'version':'9.13.0','source_format':json.loads((out/'candidate_audit.json').read_text(encoding='utf-8'))['format'],'pipeline_order':['source_parse_all','source_cross_fb_protocols','name_filter','boundary_normalization','action_signal_tail_composition','optional_process_review'],'mode':'prediction','source_sha256':fingerprint(src),'source_name':src.name,'pattern':args.pattern,'candidate_sha256':fingerprint(out/'sequence_candidates.csv'),'prediction_has_gt_access':False,'status':'prepared_needs_opencode_batches','workflow_manifest':'workflow_manifest.json'}
 prov['atomic_output_sha256']=fingerprint(out/'prediction_collection_pairs.csv')
 prov['atomic_archive_sha256']=fingerprint(out/'prediction_atomic_pairs.csv')
 prov['full_pair_archive_sha256']=fingerprint(out/'prediction_candidate_pairs.csv')
 (out/'prediction_provenance.json').write_text(json.dumps(prov,ensure_ascii=False,indent=2),encoding='utf-8')
 audit=json.loads((out/'candidate_audit.json').read_text(encoding='utf-8'))
 with (out/'prediction_candidate_pairs.csv').open(encoding='utf-8-sig',newline='') as f:
  import csv
  complete_pairs=sum(1 for _ in csv.DictReader(f))
 print(json.dumps({'mode':'predict','source_format':audit['format'],'output':str(out),'complete_candidate_pairs':complete_pairs,'main_prediction':str(out/'prediction_collection_pairs.csv'),'full_recall_archive':str(out/'prediction_candidate_pairs.csv'),'atomic_projection':json.loads((out/'prediction_atomic_summary.json').read_text(encoding='utf-8')),'batch_count':len(manifest['batches']),'next':'OpenCode逐批写result.json后运行 python plc_cli.py finish --run "目录"'},ensure_ascii=False,indent=2))

def finish(args):
 r=validate(args.run,write=True)
 print(json.dumps(r,ensure_ascii=False,indent=2))
 if not r['success']:sys.exit(2)

def evaluate(args):
 prediction=Path(args.prediction).resolve();truth=Path(args.gt).resolve()
 if not prediction.is_file() or not truth.is_file():raise FileNotFoundError('预测文件或GT不存在')
 out=Path(args.out).resolve()
 if out==prediction.parent or out==truth.parent:raise ValueError('评估输出请单独指定一个目录，避免覆盖预测或GT')
 relations=args.relations
 strict_graph=None
 transition_graph=getattr(args,'transition_graph',None)
 if not transition_graph and not getattr(args,'xml',None):
  sidecar=prediction.parent/'source_transition_graph.json'
  if sidecar.is_file():transition_graph=str(sidecar)
 if transition_graph:
  from boundary_normalizer import load_graph
  if Path(transition_graph).resolve() in (prediction,truth):raise ValueError('迁移图不得与预测/GT相同')
  strict_graph=load_graph(transition_graph)
 xml=getattr(args,'xml',None)
 if xml and transition_graph:raise ValueError('--xml 与 --transition-graph 二选一')
 if xml and relations:raise ValueError('--xml 和 --relations 请二选一，防止混用证据来源')
 if xml:
  xml_path=Path(xml).resolve()
  if not xml_path.is_file():raise ValueError('评价源码路径不存在')
  with xml_path.open('rb') as f:head=f.read(4096).lstrip(b'\xef\xbb\xbf \t\r\n')
  if not head.startswith(b'<'):raise ValueError('评价源码必须为XML文本容器；扩展名可以是.xml/.txt/.smc2')
  if xml_path in (prediction,truth):raise ValueError('XML不能与预测/GT文件相同')
  from successor_graph import write_graph as xml_scan
  evidence_dir=out/'xml_evidence';evidence_dir.mkdir(parents=True,exist_ok=True)
  strict_graph=xml_scan(xml_path,evidence_dir)
 if strict_graph is not None:
  provenance=prediction.parent/'prediction_provenance.json'
  if provenance.is_file():
   expected=json.loads(provenance.read_text(encoding='utf-8')).get('source_sha256')
   if expected and expected!=strict_graph.get('source_sha256'):raise ValueError('预测源码SHA256与评价迁移图来源不一致，拒绝混用工程')
 # Never build relations from GT; accept only explicit prediction-produced sidecar.
 if relations:
  rel=Path(relations).resolve()
  if not rel.is_file():raise FileNotFoundError(rel)
  if rel==truth:raise ValueError('关系文件不能与GT相同')
 from table_io import prediction_records
 sample=prediction_records(prediction)
 if sample and '原子对ID' in sample[0]:
  from atomic_evaluation import run as evaluate_atomic
  result=evaluate_atomic(str(prediction),str(truth),str(out),strict_graph,args.top_k)
 else:
  result=evaluate_candidates(str(prediction),str(truth),str(out),relations,args.top_k,graph=strict_graph)
 report={'mode':'evaluation','prediction_sha256':fingerprint(prediction),'gt_sha256':fingerprint(truth),'relations_sha256':fingerprint(relations) if relations else None,'evaluation_xml_sha256':fingerprint(xml) if xml else None,'transition_graph_sha256':fingerprint(transition_graph) if transition_graph else None,'gt_used_only_for_evaluation':True,'categories_not_all_successful':True}
 (out/'evaluation_provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 if not args.no_excel:
  try:
   from evaluation_excel import write_excel
   write_excel(out/'gt_match_summary.csv',out/'gt_match_hits.csv',out/'gt_match_metrics.json',out/'gt_match.xlsx')
   print('Excel:',out/'gt_match.xlsx')
  except ImportError as exc:
   print(f'Excel依赖未安装（CSV/JSON已生成）：{exc}',file=sys.stderr)
 print('评估文件：',out)
 return result

def main():
 p=argparse.ArgumentParser(description='PLC Agent v9.13，候选保全、逻辑后继统一、预测评价隔离')
 sub=p.add_subparsers(dest='mode',required=True)
 x=sub.add_parser('predict',help='源码+变量模式；不接受GT')
 x.add_argument('--source',required=True);x.add_argument('--pattern');x.add_argument('--out',required=True);x.add_argument('--batch-size',type=int,default=120);x.add_argument('--force',action='store_true');x.set_defaults(func=predict)
 x=sub.add_parser('evaluate',help='已有预测+GT；可读取XML独立核验，不修改预测')
 x.add_argument('--prediction',required=True);x.add_argument('--gt',required=True);x.add_argument('--out',required=True);x.add_argument('--relations');x.add_argument('--transition-graph',help='预测阶段生成的source_transition_graph.json，不接受GT作为证据');x.add_argument('--xml',help='仅评价读取PLCopen/SMC2/欧姆龙原生IEC61131-10 XML文本，生成独立控制流证据，绝不回写预测');x.add_argument('--top-k',type=int,default=5);x.add_argument('--no-excel',action='store_true');x.set_defaults(func=evaluate)
 x=sub.add_parser('finish',help='组装所有OpenCode批次');x.add_argument('--run',required=True);x.set_defaults(func=finish)
 a=p.parse_args();a.func(a)
if __name__=='__main__':main()
