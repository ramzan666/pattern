"""Limited execution of the real Pine AST. It is not TradingView's broker emulator."""
from pathlib import Path
import math, operator
from types import SimpleNamespace
from antlr4 import InputStream, CommonTokenStream
from antlr4.atn.PredictionMode import PredictionMode
from pynescript.ast.grammar.antlr4.lexer import PinescriptLexer
from pynescript.ast.grammar.antlr4.parser import PinescriptParser
from pynescript.ast.grammar.antlr4.error_listener import PinescriptErrorListener
from pynescript.ast.builder import PinescriptASTBuilder

SOURCE = Path(__file__).resolve().parents[1] / 'trend_retest.pine'

def parse_source():
    lexer=PinescriptLexer(InputStream(SOURCE.read_text()))
    lexer.removeErrorListeners(); lexer.addErrorListener(PinescriptErrorListener.INSTANCE)
    parser=PinescriptParser(CommonTokenStream(lexer))
    parser.removeErrorListeners(); parser.addErrorListener(PinescriptErrorListener.INSTANCE)
    parser._interp.predictionMode=PredictionMode.SLL
    tree=PinescriptASTBuilder().visit(parser.start_script())
    print('PASS: whole Pine source parsed with third-party parser (not official compilation).')
    return tree

class Runner:
    def __init__(self,tree,overrides=None):
        self.source=tree; self.overrides=(overrides or {}).copy(); self.functions={}; self.types={}
        self.env={'na':None,'bar_index':0,'time':0,'time_close':3600000,'open':100.,'high':101.,'low':99.,'close':100.,'volume':10.,
            'barstate':SimpleNamespace(isconfirmed=True,isrealtime=False,islast=False,isnew=True),
            'syminfo':SimpleNamespace(mintick=.01,pointvalue=1.,ticker='BTCUSDT',tickerid='BINANCE:BTCUSDT',basecurrency='BTC',currency='USDT',type='crypto',mincontract=.00001),
            'timeframe':SimpleNamespace(period='60',isminutes=True,multiplier=60),
            'chart':SimpleNamespace(is_standard=True),
            'strategy':SimpleNamespace(position_size=0.,position_avg_price=None,closedtrades=0,equity=10000.,netprofit=0.,opentrades=0),
            'timenow':0}
        self.hist=[]; self.alerts=[]; self.orders=[]; self.cancels=[]; self.exits=[]; self.objects=[]
        self.packet=None; self.rangeHigh=105.; self.rangeLow=95.; self.atr=2.; self.pivotHigh=None; self.pivotLow=None
    def name(self,n):
        if type(n).__name__=='Name': return n.id
        if type(n).__name__=='Attribute': return self.name(n.value)+'.'+n.attr
        if type(n).__name__=='Specialize': return self.name(n.value)
        return None
    def call(self,name,args,kwargs):
        if name in self.functions:
            f=self.functions[name]; saved=self.env.copy()
            self.env.update({p.name:v for p,v in zip(f.args,args)}); self.env.update(kwargs)
            try: return self.block(f.body)
            finally: self.env=saved
        if name.startswith('input.'): return self.overrides.get(args[1] if len(args)>1 else kwargs.get('title'),args[0] if args else kwargs['defval'])
        if name=='na': return args[0] is None
        if name=='nz': return args[0] if args[0] is not None else (args[1] if len(args)>1 else 0)
        if name in ('int','float','bool','string'): return {'int':int,'float':float,'bool':bool,'string':str}[name](args[0]) if args[0] is not None else None
        if name=='ta.atr': return self.atr
        if name in ('ta.highest','ta.lowest'): return self.rangeHigh if name=='ta.highest' else self.rangeLow
        if name=='ta.pivothigh': return self.pivotHigh
        if name=='ta.pivotlow': return self.pivotLow
        if name=='ta.ema': return args[0]
        if name=='timeframe.in_seconds': return 3600 if not args else int(args[0])*60
        if name=='request.security': return self.packet
        if name=='math.max': return None if None in args else max(args)
        if name=='math.min': return None if None in args else min(args)
        if name=='math.floor': return None if args[0] is None else math.floor(args[0])
        if name=='math.ceil': return None if args[0] is None else math.ceil(args[0])
        if name=='math.round': return None if args[0] is None else round(args[0])
        if name=='math.abs': return None if args[0] is None else abs(args[0])
        if name=='str.tostring':
            if args[0] is None: return 'NaN'
            if isinstance(args[0],bool): return str(args[0]).lower()
            return str(args[0])
        if name=='str.replace_all': return args[0].replace(args[1],args[2])
        if name=='str.length': return len(args[0])
        if name=='str.upper': return args[0].upper()
        if name=='strategy.closedtrades.exit_comment': return 'TP'
        if name=='str.format': return args[0].format(*args[1:])
        if name.startswith('array.new'): return []
        if name=='array.size': return len(args[0])
        if name=='array.push': args[0].append(args[1]); return
        if name=='array.get': return args[0][args[1]]
        if name=='array.shift': return args[0].pop(0)
        if name=='array.remove': return args[0].pop(args[1])
        if name=='array.clear': args[0].clear(); return
        if name in ('strategy','indicator','plot','plotshape','bgcolor','barcolor','alertcondition','hline'): return
        if name=='alert': self.alerts.append(args[0]); return
        if name=='strategy.entry': self.orders.append((args,kwargs)); return
        if name=='strategy.exit': self.exits.append((args,kwargs)); return
        if name=='strategy.cancel': self.cancels.append(args[0]); return
        if name=='strategy.cancel_all': self.cancels.append('*'); return
        if name=='strategy.close': return
        if name=='strategy.close_all': return
        if name=='color.rgb': return tuple(args)
        if name=='color.new': return args[0]
        if name.endswith('.new') and name[:-4] in self.types: return SimpleNamespace(**dict(zip(self.types[name[:-4]],args)))
        if name in ('line.new','label.new','box.new','table.new'):
            obj=dict(kind=name.split('.')[0],args=args,**kwargs,deleted=False); self.objects.append(obj); return obj
        if name in ('line.delete','label.delete','box.delete'):
            if args[0] is not None: args[0]['deleted']=True
            return
        if name.startswith(('line.set_','label.set_','box.set_')):
            if args[0] is not None: args[0][name.split('set_')[1]]=args[1:]
            return
        if name in ('table.cell','table.clear'): return
        if name=='runtime.error': raise RuntimeError(args[0])
        raise RuntimeError(('unsupported call',name,args,kwargs))
    def expr(self,n):
        if n is None: return None
        k=type(n).__name__
        if k=='Constant': return n.value
        if k=='Name': return self.env.get(n.id,n.id)
        if k=='Attribute':
            v=self.expr(n.value)
            return getattr(v,n.attr) if isinstance(v,SimpleNamespace) and hasattr(v,n.attr) else self.name(n)
        if k=='Specialize': return self.expr(n.value)
        if k in ('Tuple','List'): return [self.expr(x) for x in n.elts]
        if k=='Call': return self.call(self.name(n.func),[self.expr(a.value) for a in n.args if a.name is None],{a.name:self.expr(a.value) for a in n.args if a.name is not None})
        if k=='Subscript':
            index=self.expr(n.slice)
            if index==0: return self.expr(n.value)
            if index>len(self.hist): return None
            original=self.env; self.env=self.hist[-index]
            try: return self.expr(n.value)
            finally: self.env=original
        if k=='Conditional': return self.expr(n.body if self.expr(n.test) else n.orelse)
        if k=='BoolOp': return all(bool(self.expr(v)) for v in n.values) if type(n.op).__name__=='And' else any(bool(self.expr(v)) for v in n.values)
        if k=='Compare':
            left=self.expr(n.left)
            for op,other in zip(n.ops,n.comparators):
                right=self.expr(other)
                if left is None or right is None: return False
                if not {'Eq':operator.eq,'NotEq':operator.ne,'Lt':operator.lt,'LtE':operator.le,'Gt':operator.gt,'GtE':operator.ge}[type(op).__name__](left,right): return False
                left=right
            return True
        if k=='UnaryOp':
            value=self.expr(n.operand)
            return None if value is None and type(n.op).__name__!='Not' else {'Not':operator.not_,'USub':operator.neg,'UAdd':operator.pos}[type(n.op).__name__](value)
        if k=='BinOp':
            a,b=self.expr(n.left),self.expr(n.right)
            return None if a is None or b is None else {'Add':operator.add,'Sub':operator.sub,'Mult':operator.mul,'Div':operator.truediv,'Mod':operator.mod,'Pow':operator.pow}[type(n.op).__name__](a,b)
        if k=='If': return self.block(n.body if self.expr(n.test) else n.orelse)
        if k=='ForTo':
            start,end=self.expr(n.start),self.expr(n.end); step=1 if start<=end else -1
            for i in range(start,end+step,step): self.env[n.target.id]=i; self.block(n.body)
            return
        if k=='While':
            count=0
            while self.expr(n.test):
                self.block(n.body); count+=1
                if count>1000: raise RuntimeError('loop runaway')
            return
        raise RuntimeError(('unsupported expression',k))
    def block(self,body):
        result=None
        for n in body:
            k=type(n).__name__
            if k=='FunctionDef': self.functions[n.name]=n; continue
            if k=='TypeDef': self.types[n.name]=[x.target.id for x in n.body]; continue
            if k in ('Assign','ReAssign'):
                if k=='Assign' and n.mode is not None and type(n.target).__name__=='Name' and n.target.id in self.env: continue
                result=self.expr(n.value)
                tk=type(n.target).__name__
                if tk=='Tuple':
                    if len(result)!=len(n.target.elts): raise RuntimeError('tuple arity')
                    self.env.update({v.id:a for v,a in zip(n.target.elts,result)})
                elif tk=='Attribute': setattr(self.expr(n.target.value),n.target.attr,result)
                else: self.env[n.target.id]=result
            elif k=='Expr': result=self.expr(n.value)
            else: raise RuntimeError(('unsupported statement',k))
        return result
    def candle(self,o,h,l,c,*,realtime=False,confirmed=True,packet=None,**env):
        assert l<=min(o,c)<=max(o,c)<=h
        now=len(self.hist); self.packet=packet or self.packet
        self.env.update(bar_index=now,time=now*3600000,time_close=(now+1)*3600000,timenow=(now+1)*3600000,open=o,high=h,low=l,close=c,
            barstate=SimpleNamespace(isconfirmed=confirmed,isrealtime=realtime,islast=realtime,isnew=True,isfirst=now==0),**env)
        self.block(self.source.body)
        self.hist.append(self.env.copy())
        return self.env
