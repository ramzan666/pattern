"""Behavior fixtures against parsed production Pine, not a market backtest."""
import math, json
from .pine_runner import Runner, SOURCE, parse_source
from ..bridge.app import Store, validate

TREE = parse_source()
P_NAMES=next([x.id for x in n.target.elts] for n in TREE.body if type(n).__name__=='Assign' and type(n.target).__name__=='Tuple')
SECRET='test-secret-long-enough-for-webhook'


def packet(side=1,end=20*3600000,index=220,future=None):
    values=dict(prevFourEnd=end,prevFourClose=105. if side==1 else 95.,prevFast=100.,prevSlow=90. if side==1 else 110.,prevFastBefore=99. if side==1 else 101.,prevPh=None,prevPl=None,prevFourIndex=index)
    if future:
        fs,fe,fi=future
        values.update(fourEnd=fe,fourClose=105. if fs==1 else 95.,fourFast=100.,fourSlow=90. if fs==1 else 110.,fourFastBefore=99. if fs==1 else 101.,fourPh=None,fourPl=None,fourIndex=fi)
    return [values.get(name) for name in P_NAMES]

def warmed(overrides=None):
    r=Runner(TREE,overrides)
    r.packet=[None]*len(P_NAMES)
    for _ in range(20): r.candle(100,101,99,100)
    assert r.env['phase']==0 and r.env['trend']==0 and not r.alerts
    return r

def broken(side=1,overrides=None):
    r=warmed(overrides)
    if side==1: r.candle(104,108,103,107,packet=packet(side),realtime=True)
    else: r.candle(96,97,92,93,packet=packet(side),realtime=True)
    assert r.env['phase']==1 and r.env['side']==side
    assert not r.orders and r.env['planEntry'] is None
    return r

def retest(r,side=1):
    if side==1: r.candle(107,108,104,106,realtime=True)
    else: r.candle(93,96,92,94,realtime=True)
    return r

def armed(side=1,overrides=None):
    r=retest(broken(side,overrides),side)
    assert r.env['phase']==2
    return r

def events(r): return [json.loads(s) for s in r.alerts]
def kinds(r): return [e['event'] for e in events(r) if e['event']!='context']

def idle_bar(r,side=1,**extra):
    if side==1: return r.candle(106,107,105,106,realtime=True,**extra)
    return r.candle(94,95,93,94,realtime=True,**extra)

OVERRIDES={'Формат уведомлений':'Webhook / Telegram','Webhook-секрет (не токен Telegram)':SECRET}

# Actual helpers: valid money risk, leverage/exposure cap, contract rounding, invalid inputs.
r=warmed()
q,p=r.call('f_quantity',[100.,95.,10000.],{})
assert math.isclose(q,10.,abs_tol=1.1e-5) and math.isclose(p,.5,abs_tol=1e-6)
q,p=r.call('f_quantity',[100.,99.999,10000.],{})
assert math.isclose(q,99.) and p<=.5 and q*100<=10000*.99+1e-9
r.env['syminfo'].pointvalue=10.; r.env['syminfo'].mincontract=.3
q,p=r.call('f_quantity',[100.,95.,10000.],{})
assert math.isclose(q,.9) and q*5*10<=50 and p<=.5
for args in ([100.,100.,10000.],[0.,95.,10000.],[100.,95.,0.]):
    assert r.call('f_quantity',list(args),{})==[0.,0.]
assert r.call('f_obstacle',[[110.,120.],1,108.,117.],{})
assert r.call('f_obstacle',[[90.,80.],-1,92.,83.],{})
assert not r.call('f_obstacle',[[108.,117.],1,108.,117.],{})
for text in ('кавычка " и \\ путь\nновая\rстрока\tтаб','☃️ с пробелами'):
    assert json.loads(r.call('f_jsonText',[text],{}))==text
assert r.call('f_number',[None],{})=='null'

# Standard chart/timeframe and secret guards enforce readable failure.
for invalid in ('timeframe','chart','secret'):
    x=Runner(TREE,OVERRIDES if invalid=='secret' else {})
    x.packet=[None]*len(P_NAMES)
    if invalid=='timeframe':
        x.call_old=x.call
        x.call=lambda name,args,kwargs: 300 if name=='timeframe.in_seconds' else x.call_old(name,args,kwargs)
    if invalid=='chart': x.env['chart'].is_standard=False
    if invalid=='secret': x.overrides['Webhook-секрет (не токен Telegram)']='short'
    try: x.candle(100,101,99,100)
    except RuntimeError: pass
    else: raise AssertionError('Missing '+invalid+' guard')

# Only CLOSED 4H packet is accepted, native 4H EMA warmup is required.
x=warmed(); x.candle(100,101,99,100,packet=packet(1,index=198))
assert x.env['trend']==0
x=warmed(); p=packet(1,future=(-1,24*3600000,221))
x.candle(100,101,99,100,packet=p); assert x.env['trend']==1
x.candle(100,101,99,100); x.candle(100,101,99,100); assert x.env['trend']==1
x.candle(100,101,99,100); assert x.env['trend']==-1

# Source rule must explicitly use prior candles; there is no lookahead_on or close fill.
text = SOURCE.read_text()
assert 'ta.highest(high[1], breakoutLength)' in text and 'ta.lowest(low[1], breakoutLength)' in text
assert 'lookahead = barmerge.lookahead_off' in text and 'process_orders_on_close = false' in text

# Both sides: confirmed break and retest, no creation candle activation, tick buffer, SL/TP frozen.
for side in (1,-1):
    x=armed(side,OVERRIDES); e,s,t=(x.env[k] for k in ('planEntry','planStop','planTarget'))
    assert (s<e<t) if side==1 else (t<e<s)
    assert abs(t-e)>=2*abs(e-s)-1e-8
    assert e>x.env['high'] if side==1 else e<x.env['low']
    assert len(x.orders)==len(x.exits)==1 and x.env['strategy'].position_size==0
    assert kinds(x)==['breakout','setup']
    frozen=(e,s,t,x.env['planQuantity'])
    idle_bar(x,side)
    assert x.env['phase']==2 and tuple(x.env[k] for k in ('planEntry','planStop','planTarget','planQuantity'))==frozen
    x.env['strategy'].position_size=1 if side==1 else -1
    idle_bar(x,side)
    assert x.env['phase']==3 and kinds(x)==['breakout','setup','trigger']
    assert tuple(x.env[k] for k in ('planEntry','planStop','planTarget','planQuantity'))==frozen
    idle_bar(x,side); assert kinds(x).count('trigger')==1

# Retest window is inclusive, deadline cancels when not confirmed.
x=broken(1,OVERRIDES)
for _ in range(4): x.candle(107,108,106,107,realtime=True); assert x.env['phase']==1
retest(x); assert x.env['phase']==2 and x.env['retestBar']-x.env['breakoutBar']==5
x=broken(1,OVERRIDES)
for _ in range(5): x.candle(107,108,106,107,realtime=True)
assert x.env['phase']==4 and kinds(x).count('cancel')==1 and not x.orders

# Last activation candle may fill before expiry; otherwise cancel exactly at its close.
x=armed(1,OVERRIDES); idle_bar(x); assert x.env['phase']==2
idle_bar(x); assert x.env['phase']==4 and x.cancels==['Entry','Exit']
d=x.env['latestDrawing']; frozen_right=d.reward['right']
x.candle(104,105,103,104,realtime=True); assert d.reward['right']==frozen_right
x=armed(1,OVERRIDES); idle_bar(x)
x.env['strategy'].position_size=1; idle_bar(x)
assert x.env['phase']==3 and not x.cancels

# Gap-through-stop-order execution is provided by native model; script consumes it once.
x=armed(1,OVERRIDES); x.env['strategy'].position_size=1
x.candle(110,112,109,111,realtime=True)
assert x.env['phase']==3 and kinds(x).count('trigger')==1

# Native same-bar entry+exit, then duplicate tick: trigger and exit exactly once, no expiry cancel.
x=armed(1,OVERRIDES); idle_bar(x)
x.env['strategy'].closedtrades=1; idle_bar(x)
assert x.env['phase']==5 and kinds(x)[-2:]==['trigger','exit'] and not x.cancels
before=list(x.alerts); x.env['barstate'].isconfirmed=False; x.block(x.source.body)
assert x.alerts==before

# Stop invalidation pending, trend cancellation, 4H obstruction, ignored countertrend direction.
x=armed(1,OVERRIDES); x.candle(106,107,103,104,realtime=True)
assert x.env['phase']==4 and 'SL' in x.env['statusReason']
x=armed(1,OVERRIDES)
x.candle(106,107,105,106,packet=packet(-1,end=23*3600000),realtime=True)
assert x.env['phase']==4 and '4H' in x.env['statusReason']
x=broken(1,OVERRIDES); x.env['resistance']=[110.]; retest(x)
assert x.env['phase']==4 and not x.orders and '4H' in x.env['statusReason']
x=broken(-1,OVERRIDES); x.env['support']=[90.]; retest(x,-1)
assert x.env['phase']==4 and not x.orders
x=warmed({'Направления':'Только LONG'})
x.candle(96,97,92,93,packet=packet(-1)); assert x.env['phase']==0

# Historical execution creates drawings/orders but never external alerts.
x=broken(1,OVERRIDES); x.alerts.clear(); x.env['barstate'].isrealtime=False
x.candle(107,108,104,106,realtime=False)
assert x.env['phase']==2 and not x.alerts

# Real f_emit JSON feeds the production bridge schema, including zero-risk observations.
collection=[broken(1,OVERRIDES),armed(1,OVERRIDES),x]
flat=[]
for source in collection:
    flat.extend(events(source))
for payload in flat:
    clean=validate(payload,payload['event_time'])
    assert 'secret' not in clean and payload['secret']==SECRET
    assert payload['schema_version']==1
assert flat and len({e['event_id'] for e in events(collection[1])})==len(events(collection[1]))
print('PASS: actual Pine AST helpers/state/JSON integration: sizing and cap, guards, closed 4H/no-lookahead/EMA warmup, long+short break/retest, next-bar stop entry, 2R frozen levels, inclusive retest/activation deadlines, native fill precedence/one trigger, same-bar roundtrip dedup, cancellations, 4H obstacles, historical alert silence, production bridge schema. Native broker emulator NOT exercised; no market profitability claim.')

# Bootstrap snapshots once intrabar, distinct next close heartbeat, actual downstream ingestion.
x=warmed(OVERRIDES)
x.packet=packet(1)
x.env.update(bar_index=20,time=20*3600000,time_close=21*3600000,timenow=20*3600000+1000)
x.env['barstate'].isrealtime=True; x.env['barstate'].isconfirmed=False
x.env['barstate'].isfirst=False; x.block(TREE.body)
assert len(x.alerts)==1 and '|bootstrap|' in json.loads(x.alerts[0])['event_id']
x.block(TREE.body); assert len(x.alerts)==1
x.env['barstate'].isconfirmed=True; x.env['timenow']=21*3600000; x.block(TREE.body)
assert len(x.alerts)==2 and json.loads(x.alerts[-1])['event_id'].endswith('|close')
x.block(TREE.body); assert len(x.alerts)==2
assert len({e['event_id'] for e in events(x)})==2

# Restarting an alert inside one chart candle must produce a fresh bootstrap ID.
restart=warmed(OVERRIDES)
restart.packet=packet(1)
restart.env.update(bar_index=20,time=20*3600000,time_close=21*3600000,timenow=20*3600000+2000)
restart.env['barstate'].isrealtime=True; restart.env['barstate'].isconfirmed=False
restart.env['barstate'].isfirst=False; restart.block(TREE.body)
first=events(x)[0]; fresh=events(restart)[0]
assert first['bar_time']==fresh['bar_time']
assert first['event_time']!=fresh['event_time'] and first['event_id']!=fresh['event_id']
restart.block(TREE.body); assert len(restart.alerts)==1
store=Store(':memory:')
for event in (first,fresh):
    clean=validate(event,event['event_time'])
    assert store.ingest(clean,event['event_time'])=='accepted'
    assert store.ingest(clean,event['event_time'])=='duplicate'

# Altered strategy settings namespace setup/closed-context IDs at the same candle.
base=armed(1,OVERRIDES)
changed=armed(1,{**OVERRIDES,'Плановое отношение прибыль / риск':3.0})
assert base.env['retestBar']==changed.env['retestBar']
assert base.env['setupId']!=changed.env['setupId']
assert {e['event_id'] for e in events(base)}.isdisjoint(e['event_id'] for e in events(changed))

# All lifecycle events emitted by Pine can pass backend validation/storage; setup doesn't resurrect.
lifecycle=armed(1,OVERRIDES)
lifecycle.env['strategy'].position_size=1
idle_bar(lifecycle)
lifecycle.env['strategy'].position_size=0; lifecycle.env['strategy'].closedtrades=1
lifecycle.candle(104,105,103,104,realtime=True)
assert lifecycle.env['phase']==5
canceled=armed(1,OVERRIDES); idle_bar(canceled); idle_bar(canceled)
assert canceled.env['phase']==4
for stream in (x,lifecycle,canceled):
    store=Store(':memory:')
    for event in events(stream):
        clean=validate(event,event['event_time'])
        assert store.ingest(clean,event['event_time'])=='accepted', (event,store.state(event['symbol']))
        assert store.ingest(clean,event['event_time'])=='duplicate'
    final=events(stream)[-1]
    state=store.state(final['symbol'])
    assert state['stage']==final['stage'] and 'secret' not in state

# Live active drawing expands; finished drawing endpoint freezes on later ticks/bar closes.
x=armed(1,OVERRIDES); x.env['strategy'].position_size=1; idle_bar(x)
d=x.env['latestDrawing']; old_right=d.reward['right']
for _ in range(12): idle_bar(x)
assert d.reward['right']>old_right
x.env['strategy'].position_size=0; x.env['strategy'].closedtrades=1
x.candle(104,105,103,104,realtime=True); frozen=d.reward['right']
for _ in range(3): x.candle(104,105,103,104,realtime=True)
assert d.reward['right']==frozen
print('PASS: first live bootstrap once, closed heartbeat distinct/deduplicated, all real lifecycle JSON→bridge schema+SQLite duplicate/terminal state, active drawing extension/final freeze.')
