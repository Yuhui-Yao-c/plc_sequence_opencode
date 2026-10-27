"""Source-only state transition graph for first-exit boundary normalization.

Supported: explicit PLCopen LD/FBD EQ/NE, contacts, AND/OR/NOT, MOVE EN,
output-variable connections, and a strict ST IF/ELSIF/ELSE/CASE subset.
Unknown paths/writers are blockers, not guessed transitions. No GT import.
A complete local path is NOT a proof of scan visibility, liveness or physical completion.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, re
from pathlib import Path
from lxml import etree
from boundary_logic import (Expr,TRUE,FALSE,parse,combine,negate,integer,symbol,
                            comparisons,compatible,BoundarySyntaxError,IDENT)

def tag(e): return etree.QName(e).localname if isinstance(e.tag,str) else ''
def sha256(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()
def text(e): return ''.join(e.itertext()).strip() if e is not None else ''
def own_expr(e):
    if e is None:return ''
    for n in e:
        if tag(n) in ('expression','variable'):return text(n)
    if tag(e)=='expression':return text(e)
    # PLCopen/CODESYS exports may bind data outputs inside the output port.
    # Inspect direct ports only: never search another block's expressions.
    values=[text(z) for c in e if tag(c) in ('connectionPointIn','connectionPointOut')
            for z in c if tag(z)=='expression' and text(z)]
    return values[0] if len(values)==1 else ''
def pins(e,section):
    if e is None:return []
    return [v for s in e if tag(s)==section for v in s if tag(v)=='variable']
def pin(e,section,name):
    return next((v for v in pins(e,section) if v.get('formalParameter','').upper()==name.upper()),None)
def connections(e):
    if e is None:return []
    return [(n.get('refLocalId'),n.get('formalParameter','')) for n in e.iter()
            if tag(n)=='connection' and n.get('refLocalId')]
def owner(e):
    names=[];p=e.getparent();pou=None
    while p is not None:
        if tag(p) in ('pou','action'):names.append(p.get('name') or '?')
        if tag(p)=='pou':pou=p
        p=p.getparent()
    return '::'.join(reversed(names)) or '<anonymous>',pou

def binding(variable,scope,pou):
    root=re.split(r'[.\[]',variable)[0].casefold()
    local_names=set()
    if pou is not None:
        for section in pou:
            if tag(section)!='interface':continue
            for group in section:
                if tag(group) in ('localVars','inputVars','outputVars','inOutVars','tempVars'):
                    local_names.update(v.get('name','').casefold() for v in group if tag(v)=='variable')
    return scope.split('::')[0] if root in local_names or '.' not in variable else ''

class WireError(ValueError): pass
class WireBody:
    def __init__(self,body):
        self.body=body;self.nodes={n.get('localId'):n for n in body if n.get('localId')}
        self.seen=set();self.stack=set();self.memo={}
        self._guard_sizes={};self.max_guard_nodes=2048
        self.duplicate_ids=len(self.nodes)!=sum(bool(n.get('localId')) for n in body)
    def guard_size(self,x):
        # A DAG with shared OR prefixes can expand exponentially when flattened.
        # Bound expansion, report a blocker, and keep recall candidates instead
        # of hanging or silently treating a truncated guard as complete.
        k=id(x)
        if k in self._guard_sizes and self._guard_sizes[k][0] is x:return self._guard_sizes[k][1]
        size=1
        for a in x.args:
            if isinstance(a,Expr):
                size+=self.guard_size(a)
                if size>self.max_guard_nodes:break
        self._guard_sizes[k]=(x,size)
        return size
    def value(self,p):
        direct=own_expr(p)
        if direct:return direct
        refs=connections(p)
        if len(refs)!=1:raise WireError('data input must have exactly one explicit source')
        ref,port=refs[0];n=self.nodes.get(ref)
        if n is None:raise WireError('missing_ref:'+ref)
        if tag(n) in ('inVariable','inOutVariable'):return own_expr(n)
        # Values produced by arithmetic/functions are not silently treated as literals.
        raise WireError('computed_data:'+ref+':'+port)
    def cp(self,p,default=None):
        refs=connections(p)
        if not refs:
            direct=own_expr(p)
            if direct:return parse(direct)
            if default is not None:return default
            raise WireError('missing_control_input')
        x=combine('or',*(self.resolve(ref,port) for ref,port in refs))
        if p is not None and p.get('negated','').lower()=='true':x=negate(x)
        return x
    def resolve(self,ref,port=''):
        key=(ref,port.upper())
        if key in self.memo:return self.memo[key]
        if key in self.stack:raise WireError('control_cycle:'+ref)
        if self.duplicate_ids:raise WireError('duplicate_localId')
        n=self.nodes.get(ref)
        if n is None:raise WireError('missing_ref:'+ref)
        self.seen.add(ref);self.stack.add(key)
        try:
            kind=tag(n)
            if kind=='leftPowerRail':x=TRUE
            elif kind in ('inVariable','inOutVariable'):x=parse(own_expr(n))
            elif kind=='contact':
                if n.get('edge','none').lower() not in ('','none'):raise WireError('edge_contact:'+ref)
                v=own_expr(n)
                if not v:raise WireError('contact_variable_missing:'+ref)
                x=parse(v)
                if n.get('negated','').lower()=='true':x=negate(x)
                cp=next((c for c in n if tag(c)=='connectionPointIn'),None)
                x=combine('and',self.cp(cp),x)
            elif kind in ('connector','continuation'):
                if kind=='continuation':
                    matches=[m for m in self.nodes.values() if tag(m)=='connector' and m.get('name')==n.get('name')]
                    if len(matches)!=1:raise WireError('ambiguous_connector:'+ref)
                    x=self.resolve(matches[0].get('localId'))
                else:x=self.cp(n)
            elif kind=='block':
                typ=n.get('typeName','').upper();ps=pins(n,'inputVariables');en=pin(n,'inputVariables','EN')
                gate=self.cp(en,TRUE)
                data=[v for v in ps if v.get('formalParameter','').upper() not in ('EN','ENO')]
                if typ in ('EQ','NE','=','<>','==','!='):
                    if port.upper()=='ENO':raise WireError('comparison_ENO_not_predicate:'+ref)
                    if len(data)!=2:raise WireError('nonbinary_comparison:'+ref)
                    a,b=self.value(data[0]),self.value(data[1])
                    pred=parse(a+(' = ' if typ in ('EQ','=','==') else ' <> ')+b)
                    x=combine('and',gate,pred)
                elif typ in ('AND','OR','NOT'):
                    if not data or (typ=='NOT' and len(data)!=1):raise WireError('boolean_arity:'+ref)
                    args=[self.cp(v) for v in data]
                    pred=negate(args[0]) if typ=='NOT' else combine(typ.lower(),*args)
                    x=combine('and',gate,pred)
                else:raise WireError('unsupported_control_block:'+typ+':'+ref+':'+port)
                output=next((v for v in pins(n,'outputVariables') if v.get('formalParameter','').upper()==port.upper()),None)
                if output is not None and output.get('negated','').lower()=='true':x=negate(x)
            else:raise WireError('unsupported_control_node:'+kind+':'+ref)
            if self.guard_size(x)>self.max_guard_nodes:raise WireError('guard_expression_budget_exceeded:'+ref)
            self.memo[key]=x;return x
        finally:self.stack.remove(key)
    def targets(self,move):
        targets=[];mid=move.get('localId')
        for p in pins(move,'outputVariables'):
            if p.get('formalParameter','').upper() not in ('OUT',''):continue
            v=own_expr(p)
            if v:targets.append(v)
        for n in self.nodes.values():
            if tag(n) not in ('outVariable','inOutVariable'):continue
            refs=connections(n)
            if any(r==mid and p.upper() in ('','OUT') for r,p in refs):
                if len(refs)!=1:raise WireError('multiple_outvariable_drivers')
                v=own_expr(n)
                if v:targets.append(v)
        return list(dict.fromkeys(symbol(v) for v in targets if re.fullmatch(IDENT,v)))

# ST subset parser. Parse failures block the whole body, never degrade to proximity matching.
ST_TOKEN=re.compile(r"\s+|//[^\n]*|\(\*.*?\*\)|'(?:''|[^'])*'|:=|\.\.|<>|>=|<=|[();,:=<>]|[^\s();,:=<>]+",re.S)
def st_tokens(s):
    ts=[]
    for m in ST_TOKEN.finditer(s):
        t=m.group()
        if t.isspace() or t.startswith('//') or t.startswith('(*'):continue
        if t.startswith("'"):t='__STRING_UNSUPPORTED__'
        ts.append(t)
    return ts

class STParser:
    def __init__(self,s,condition_parser=None):
        self.ts=st_tokens(s);self.i=0;self.writes=[];self.actions=[];self.condition_parser=condition_parser or parse
    def is_(self,*words):return self.i<len(self.ts) and self.ts[self.i].upper() in words
    def pop(self,word=None):
        if self.i>=len(self.ts) or (word and not self.is_(word)):raise WireError('ST expected '+str(word))
        v=self.ts[self.i];self.i+=1;return v
    def until(self,word):
        start=self.i
        while self.i<len(self.ts) and not self.is_(word):self.i+=1
        if self.i==len(self.ts):raise WireError('ST missing '+word)
        s=' '.join(self.ts[start:self.i]);self.pop(word);return s
    def case_label(self):
        j=self.i;parts=[]
        while j<len(self.ts) and self.ts[j] not in (':',';'):
            if self.ts[j].upper() in ('IF','END_CASE','ELSE'):return None
            parts.append(self.ts[j]);j+=1
        if j==len(self.ts) or self.ts[j]!=':':return None
        vals=[]
        for p in ' '.join(parts).split(','):
            if '..' in p:
                a,b=map(integer,p.split('..',1))
                if a is None or b is None or not 0<=b-a<=256:raise WireError('ST CASE range unsupported')
                vals.extend(range(a,b+1))
            else:
                n=integer(p)
                if n is None:return None
                vals.append(n)
        return j+1,vals
    def seq(self,guard=TRUE,stops=(),case_context=False):
        while self.i<len(self.ts) and not self.is_(*stops):
            if case_context and self.case_label():return
            if self.is_(';'):self.pop();continue
            if self.is_('IF'):
                self.pop();cond=self.condition_parser(self.until('THEN'));seen=cond
                self.seq(combine('and',guard,cond),('ELSIF','ELSE','END_IF'))
                while self.is_('ELSIF'):
                    self.pop();cond=self.condition_parser(self.until('THEN'))
                    self.seq(combine('and',guard,negate(seen),cond),('ELSIF','ELSE','END_IF'))
                    seen=combine('or',seen,cond)
                if self.is_('ELSE'):self.pop();self.seq(combine('and',guard,negate(seen)),('END_IF',))
                self.pop('END_IF')
            elif self.is_('CASE'):
                self.pop();var=symbol(self.until('OF'))
                if not re.fullmatch(IDENT,var):raise WireError('ST computed CASE selector')
                seen=FALSE
                while not self.is_('ELSE','END_CASE'):
                    lab=self.case_label()
                    if not lab:raise WireError('ST invalid CASE label')
                    self.i,vals=lab;cond=combine('or',*(Expr('eq',(var,v)) for v in vals));seen=combine('or',seen,cond)
                    self.seq(combine('and',guard,cond),('ELSE','END_CASE'),True)
                if self.is_('ELSE'):self.pop();self.seq(combine('and',guard,negate(seen)),('END_CASE',))
                self.pop('END_CASE')
            elif self.is_('FOR','WHILE','REPEAT','JMP','RETURN','EXIT','CONTINUE','GOTO'):
                raise WireError('ST unsupported flow '+self.pop())
            else:
                begin=self.i;stmt=self.until(';')
                if ':=' not in stmt:raise WireError('ST opaque call/statement may write state')
                lhs,rhs=stmt.split(':=',1);lhs=symbol(lhs);rhs=rhs.strip()
                if not re.fullmatch(IDENT,lhs):raise WireError('ST invalid assignment target')
                if rhs.upper() in ('TRUE','FALSE'):
                    self.actions.append({'variable':lhs,'value':rhs.upper(),'guard':guard.text(),'token':begin})
                else:self.writes.append((lhs,integer(rhs),guard,begin,rhs))


def collect(source):
    source=Path(source);transitions=[];blockers=[];observations=[];warnings=[];bodies=0
    from source_format import detect_source_format
    fmt=detect_source_format(source)
    if fmt=='omron_iec61131_10':
        from omron_native import collect_native
        return collect_native(source)
    def block(scope,var,reason,states=None,bscope=''):
        for s in states or [None]:blockers.append({'scope':scope,'binding_scope':bscope,'variable':var,'from':s,'reason':reason})
    def emit(scope,pou,body_id,var,dest,guard,evidence,path):
        bscope=binding(var,scope,pou)
        if dest is None:
            block(scope,var,'dynamic_state_assignment',None,bscope);return
        atoms=[a for a in comparisons(guard) if a.kind=='eq' and a.args[0].casefold()==var.casefold()]
        states=sorted({a.args[1] for a in atoms if compatible(guard,var,a.args[1])})
        all_values={a.args[1] for a in comparisons(guard) if a.args[0].casefold()==var.casefold()}
        other=(max(all_values)+1) if all_values else 0
        if states and compatible(guard,var,other):
            block(scope,var,'guard_has_unconstrained_source_branch',None,bscope)
        # Without a positive source state, a reset/default/unconditional writer may apply anywhere.
        if not states:
            if guard!=FALSE:block(scope,var,'writer_without_positive_source_state',None,bscope)
            return
        for src in states:
            transitions.append({'id':f'T{len(transitions)+1:07d}','scope':scope,'binding_scope':bscope,
                'body':body_id,'variable':var,'from':src,'to':dest,'guard':guard.text(),
                'guard_complete':True,'evidence':evidence,'path':path,
                'timing_proof':'local_control_only; scan visibility and writer ordering not proved'})
    if fmt=='smc2':
        from smc2_controlflow import collect_smc2
        model=collect_smc2(source)
        return {'schema':'plc.state_transitions.v1','adapter':'smc2','source_sha256':sha256(source),
                'source_name':source.name,'source_size':source.stat().st_size,**model,
                'limits':['SMC2 CLs/VLs exported-grid local control model; not vendor runtime certification.',
                          'Unknown function outputs, edge contacts, dynamic writes and malformed networks block normalization.',
                          'External library side effects, task/scan ordering and physical completion are unresolved.',
                          'Comment state numbers are diagnostic only and NEVER executable state evidence.']}
    else:
        for _,body in etree.iterparse(str(source),events=('end',),tag=('{*}LD','{*}FBD','{*}ST'),huge_tree=True,resolve_entities=False,no_network=True):
            bodies+=1;scope,pou=owner(body);body_id=f'{scope}/{tag(body)}/{bodies}'
            if tag(body)=='ST':
                code=text(body);parser=STParser(code)
                try:
                    parser.seq()
                    for var,dest,guard,pos,rhs in parser.writes:emit(scope,pou,body_id,var,dest,guard,'st_structured_guard',[f'token:{pos}'])
                    observations.extend({**a,'scope':scope,'source':'ST_bool_assignment'} for a in parser.actions)
                except (WireError,BoundarySyntaxError) as e:
                    vars_=set(re.findall(rf'({IDENT})\s*:=',code))
                    for v in vars_:block(scope,symbol(v),'unsupported_ST_body:'+str(e),None,binding(symbol(v),scope,pou))
                    warnings.append(body_id+': '+str(e))
            else:
                wire=WireBody(body)
                for n in body:
                    if tag(n)!='block' or n.get('typeName','').upper()!='MOVE':continue
                    try:targets=wire.targets(n)
                    except WireError as e:
                        warnings.append(body_id+': '+str(e));continue
                    for var in targets:
                        dest=None;guard=None;wire.seen=set();wire.memo.clear()
                        try:
                            inp=pin(n,'inputVariables','IN')
                            if inp is None:
                                data=[v for v in pins(n,'inputVariables') if v.get('formalParameter','').upper()!='EN']
                                if len(data)==1:inp=data[0]
                            dest=integer(wire.value(inp))
                            if dest is None:raise WireError('non_literal_MOVE_target')
                            en=pin(n,'inputVariables','EN')
                            if en is None:raise WireError('MOVE_EN_missing')
                            guard=wire.cp(en)
                            emit(scope,pou,body_id,var,dest,guard,'plcopen_wired_guard',sorted(wire.seen))
                        except (WireError,BoundarySyntaxError) as e:
                            block(scope,var,str(e),None,binding(var,scope,pou))
                # Other explicit state writes via outVariable/coil are blockers unless fed by MOVE.
                for n in body:
                    if tag(n) not in ('outVariable','inOutVariable'):continue
                    v=own_expr(n)
                    if not re.fullmatch(IDENT,v or ''):continue
                    refs=connections(n)
                    if refs and all(r in wire.nodes and wire.nodes[r].get('typeName','').upper()=='MOVE' for r,p in refs):continue
                    if refs:block(scope,symbol(v),'non_MOVE_writer',None,binding(symbol(v),scope,pou))
            body.clear()
            while body.getprevious() is not None:del body.getparent()[0]
    return {'schema':'plc.state_transitions.v1','adapter':fmt,'source_sha256':sha256(source),
            'source_name':source.name,'source_size':source.stat().st_size,'transitions':transitions,
            'blockers':blockers,'observations':observations,'warnings':warnings,
            'summary':{'bodies':bodies,'transitions':len(transitions),'blockers':len(blockers)},
            'limits':['Supported local source transitions only; not a whole-program temporal proof.',
                      'External libraries/indirect writes/tasks/scan acquisition are not resolved.',
                      'No numeric-adjacency or co-location edge is treated as proved.']}

def write_graph(source,out,model=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);graph=model if model is not None else collect(source)
    (out/'source_transition_graph.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[{**r,'path':';'.join(r['path'])} for r in graph['transitions']]
    fields=['id','scope','binding_scope','body','variable','from','to','guard','guard_complete','evidence','path','timing_proof']
    with (out/'source_transition_edges.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    if graph.get('adapter') in ('smc2','omron_iec61131_10'):
        from boundary_normalizer import write_csv
        write_csv(out/'source_state_inventory.csv',graph.get('state_observations',[]))
        write_csv(out/'source_comment_state_audit.csv',graph.get('comment_state_diagnostics',[]))
        write_csv(out/'source_stage_candidates.csv',graph.get('stage_records',[]))
        audit_name='native_parse_audit.json' if graph.get('adapter')=='omron_iec61131_10' else 'smc2_parse_audit.json'
        audit=graph.get('native_parse_audit',{}) if graph.get('adapter')=='omron_iec61131_10' else graph.get('smc2_audit',{})
        (out/audit_name).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    return graph
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('-o','--out',required=True)
    a=p.parse_args();print(json.dumps(write_graph(a.source,a.out)['summary'],ensure_ascii=False))
