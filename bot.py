import os, threading, hashlib, hmac, json, uuid
from datetime import datetime
from urllib.parse import parse_qsl, quote
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import requests, psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN=os.environ['BOT_TOKEN']
ODDIWIRE_API_KEY=os.environ.get('FIELDFUNDED_API_KEY','')
DATABASE_URL=os.environ.get('DATABASE_URL','')
MINI_APP_URL=os.environ.get('MINI_APP_URL','https://tj6235138-debug.github.io/jiangtian-esports/')
SUPPORT_URL=os.environ.get('SUPPORT_URL','')
RECHARGE_URL=os.environ.get('RECHARGE_URL','')
ODDIWIRE_BASE_URL='https://oddiwire.com'
web=Flask(__name__)
CORS(web,resources={r'/api/*':{'origins':['https://tj6235138-debug.github.io','https://web.telegram.org']}})

def money(v): return Decimal(str(v)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
def price4(v): return Decimal(str(v)).quantize(Decimal('0.0001'),rounding=ROUND_HALF_UP)
def get_db():
    if not DATABASE_URL: raise RuntimeError('DATABASE_URL 未配置')
    return psycopg.connect(DATABASE_URL,autocommit=False)

def init_database():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS users(telegram_id BIGINT PRIMARY KEY,username TEXT,first_name TEXT,last_name TEXT,photo_url TEXT,balance NUMERIC(18,2) NOT NULL DEFAULT 0.00,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
            cur.execute("ALTER TABLE users ALTER COLUMN balance SET DEFAULT 0.00")
            cur.execute('''CREATE TABLE IF NOT EXISTS bets(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,event_id TEXT,sport TEXT,league TEXT,home_team TEXT,away_team TEXT,market TEXT,selection TEXT,odds NUMERIC(12,4) NOT NULL,stake NUMERIC(18,2) NOT NULL,potential_return NUMERIC(18,2) NOT NULL,status TEXT NOT NULL DEFAULT 'pending',result TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),settled_at TIMESTAMPTZ)''')
            for sql in [
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS market_id TEXT",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS selection_id TEXT",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS provider_id TEXT",
                "ALTER TABLE bets ADD COLUMN IF NOT EXISTS canonical TEXT"]: cur.execute(sql)
            cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_bets_idempotency ON bets(telegram_id,idempotency_key) WHERE idempotency_key IS NOT NULL')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_bets_telegram_id ON bets(telegram_id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_bets_event_id ON bets(event_id)')
            cur.execute('''CREATE TABLE IF NOT EXISTS wallet_ledger(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,entry_type TEXT NOT NULL,amount NUMERIC(18,2) NOT NULL,balance_after NUMERIC(18,2) NOT NULL,reference_type TEXT,reference_id TEXT,idempotency_key TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
            cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_idempotency ON wallet_ledger(telegram_id,idempotency_key) WHERE idempotency_key IS NOT NULL')
            cur.execute('CREATE TABLE IF NOT EXISTS system_migrations(migration_key TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
            cur.execute("SELECT 1 FROM system_migrations WHERE migration_key='formal_v1_clear_demo_data'")
            if not cur.fetchone():
                cur.execute('DELETE FROM bets')
                cur.execute('DELETE FROM wallet_ledger')
                cur.execute('UPDATE users SET balance=0.00,updated_at=NOW()')
                cur.execute("INSERT INTO system_migrations(migration_key) VALUES('formal_v1_clear_demo_data')")
        conn.commit()
    print('数据库初始化完成：正式版迁移已检查')

def verify_telegram_init_data(init_data):
    if not init_data:return None
    try:
        values=dict(parse_qsl(init_data,keep_blank_values=True)); received=values.pop('hash',None)
        if not received:return None
        check='\n'.join(f'{k}={values[k]}' for k in sorted(values))
        secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest()
        calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc,received):return None
        user=json.loads(values.get('user','{}'))
        return user if user.get('id') else None
    except Exception as e: print('Telegram身份验证失败:',e); return None

def get_request_user():
    d=request.headers.get('X-Telegram-Init-Data','')
    if not d and request.is_json:d=(request.get_json(silent=True) or {}).get('init_data','')
    return verify_telegram_init_data(d)

def upsert_user(user):
    vals=(int(user['id']),user.get('username'),user.get('first_name'),user.get('last_name'),user.get('photo_url'))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO users(telegram_id,username,first_name,last_name,photo_url) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,photo_url=EXCLUDED.photo_url,updated_at=NOW() RETURNING telegram_id,username,first_name,last_name,photo_url,balance,created_at''',vals); r=cur.fetchone()
        conn.commit()
    return {'telegram_id':r[0],'username':r[1],'first_name':r[2],'last_name':r[3],'photo_url':r[4],'balance':float(r[5]),'created_at':r[6].isoformat() if r[6] else None}

def ensure_bot_user(u):
    try:return upsert_user({'id':u.id,'username':u.username,'first_name':u.first_name,'last_name':u.last_name,'photo_url':None})
    except Exception as e:print('创建用户失败:',e);return None

def oddiwire_get(path,params=None):
    if not ODDIWIRE_API_KEY:return None,'Oddiwire API Key 未配置',500
    try:
        r=requests.get(ODDIWIRE_BASE_URL+path,headers={'x-api-key':ODDIWIRE_API_KEY,'Accept':'application/json'},params=params or {},timeout=15)
        if r.status_code==401:return None,'Oddiwire API Key 无效',502
        if r.status_code==404:return None,'赛事已离开盘口或不存在',404
        if r.status_code==429:return None,'赔率服务请求过于频繁，请稍后重试',429
        r.raise_for_status();return r.json(),None,200
    except requests.RequestException as e:return None,f'赔率服务连接失败: {e}',502
    except ValueError:return None,'赔率服务返回格式异常',502

def fixture_id(f):return str(f.get('event_id') or f.get('id') or '')
def selection_key(s):return str(s.get('selection_id') or s.get('provider_id') or '')

def get_live_selection(event_id,market_id,selection_id):
    data,err,code=oddiwire_get('/v1/fixtures/'+quote(str(event_id),safe=''),{'include':'closed,suspended,resulted','tier':3})
    if err:return None,err,code
    event=data.get('event',data) if isinstance(data,dict) else None
    if not isinstance(event,dict):return None,'赛事数据无效',502
    for m in event.get('markets') or []:
        if str(m.get('market_id') or '') != str(market_id):continue
        for s in m.get('selections') or []:
            if selection_key(s)==str(selection_id):return {'event':event,'market':m,'selection':s},None,200
        return None,'所选盘口选项已不存在',409
    return None,'所选盘口已不存在或已封盘',409

@web.get('/')
def health():return jsonify({'ok':True,'name':'姜天电竞 API','status':'online','database':bool(DATABASE_URL)})
@web.get('/api/status')
def api_status():return jsonify({'ok':True,'service':'Jiangtian Esports','oddiwire':bool(ODDIWIRE_API_KEY)})
@web.route('/api/user',methods=['GET','POST'])
def api_user():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败，请从机器人重新进入。'}),401
    try:return jsonify({'ok':True,'user':upsert_user(u)})
    except Exception as e:print(e);return jsonify({'ok':False,'error':'读取用户账户失败'}),500

@web.get('/api/bets')
def api_bets():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败'}),401
    try:
        upsert_user(u)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT id,event_id,sport,league,home_team,away_team,market,selection,odds,stake,potential_return,status,result,created_at,settled_at,market_id,selection_id FROM bets WHERE telegram_id=%s ORDER BY id DESC LIMIT 100''',(int(u['id']),)); rows=cur.fetchall()
        bets=[{'id':r[0],'event_id':r[1],'sport':r[2],'league':r[3],'home_team':r[4],'away_team':r[5],'market':r[6],'selection':r[7],'odds':float(r[8]),'stake':float(r[9]),'potential_return':float(r[10]),'status':r[11],'result':r[12],'created_at':r[13].isoformat() if r[13] else None,'settled_at':r[14].isoformat() if r[14] else None,'market_id':r[15],'selection_id':r[16]} for r in rows]
        return jsonify({'ok':True,'count':len(bets),'bets':bets})
    except Exception as e:print('读取投注失败:',e);return jsonify({'ok':False,'error':'读取投注记录失败'}),500

@web.post('/api/bet')
def api_place_bet():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败'}),401
    b=request.get_json(silent=True) or {}; tid=int(u['id'])
    try: stake=money(b.get('stake',0)); displayed=price4(b.get('odds',0))
    except (InvalidOperation,TypeError,ValueError):return jsonify({'ok':False,'error':'投注金额或赔率格式错误'}),400
    if stake<=0 or stake>Decimal('1000000'):return jsonify({'ok':False,'error':'投注金额无效'}),400
    event_id=str(b.get('event_id',''))[:200]; market_id=str(b.get('market_id',''))[:300]; selection_id=str(b.get('selection_id',''))[:300]
    if not all([event_id,market_id,selection_id]):return jsonify({'ok':False,'error':'缺少赛事/盘口/选项ID，请刷新赛事后重试','code':'STALE_CLIENT'}),400
    idem=str(b.get('idempotency_key') or uuid.uuid4())[:100]
    accepted=b.get('accepted_odds')
    try: accepted=price4(accepted) if accepted is not None else None
    except: accepted=None
    live,err,code=get_live_selection(event_id,market_id,selection_id)
    if err:return jsonify({'ok':False,'error':err,'code':'MARKET_CLOSED'}),code
    m,s=live['market'],live['selection']
    if str(m.get('status','')).upper()!='OPEN' or str(s.get('status','OPEN')).upper() not in ('OPEN','ACTIVE','') or s.get('price') is None:
        return jsonify({'ok':False,'error':'该盘口当前已封盘或暂停，投注未成交。','code':'MARKET_CLOSED'}),409
    latest=price4(s['price'])
    expected=accepted if accepted is not None else displayed
    if latest!=expected:
        return jsonify({'ok':False,'error':'赔率已变化，请确认最新赔率。','code':'ODDS_CHANGED','old_odds':float(expected),'latest_odds':float(latest),'stake':float(stake),'potential_return':float(money(stake*latest)),'idempotency_key':idem}),409
    potential=money(stake*latest); upsert_user(u)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id,odds,stake,potential_return,status FROM bets WHERE telegram_id=%s AND idempotency_key=%s',(tid,idem)); old=cur.fetchone()
                if old:
                    cur.execute('SELECT balance FROM users WHERE telegram_id=%s',(tid,)); bal=cur.fetchone()[0]; conn.rollback()
                    return jsonify({'ok':True,'duplicate':True,'bet':{'id':old[0],'odds':float(old[1]),'stake':float(old[2]),'potential_return':float(old[3]),'status':old[4]},'balance':float(bal)})
                cur.execute('SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE',(tid,)); row=cur.fetchone()
                if not row:return jsonify({'ok':False,'error':'用户不存在'}),404
                balance=money(row[0])
                if balance<stake:conn.rollback();return jsonify({'ok':False,'error':'余额不足','balance':float(balance),'code':'INSUFFICIENT_BALANCE'}),400
                # 锁余额后再做最后一次实时核验；任何变化都不扣款
                live2,err2,code2=get_live_selection(event_id,market_id,selection_id)
                if err2:conn.rollback();return jsonify({'ok':False,'error':err2,'code':'MARKET_CLOSED'}),code2
                m2,s2=live2['market'],live2['selection']
                if str(m2.get('status','')).upper()!='OPEN' or s2.get('price') is None:
                    conn.rollback();return jsonify({'ok':False,'error':'盘口刚刚封盘，投注未成交，余额未扣除。','code':'MARKET_CLOSED'}),409
                latest2=price4(s2['price'])
                if latest2!=latest:
                    conn.rollback();return jsonify({'ok':False,'error':'赔率再次变化，请重新确认。','code':'ODDS_CHANGED','old_odds':float(latest),'latest_odds':float(latest2),'stake':float(stake),'potential_return':float(money(stake*latest2)),'idempotency_key':idem}),409
                cur.execute('UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE telegram_id=%s RETURNING balance',(stake,tid)); newbal=money(cur.fetchone()[0])
                teams=live2['event'].get('competitors') or []; home=str(b.get('home_team',''))[:300]; away=str(b.get('away_team',''))[:300]
                market_name=str(m2.get('market') or b.get('market',''))[:300]; selection_name=str(s2.get('name') or b.get('selection',''))[:300]
                cur.execute('''INSERT INTO bets(telegram_id,event_id,sport,league,home_team,away_team,market,selection,odds,stake,potential_return,status,market_id,selection_id,idempotency_key,provider_id,canonical) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s) RETURNING id,created_at''',(tid,event_id,str(live2['event'].get('sport') or b.get('sport',''))[:100],str(b.get('league',''))[:300],home,away,market_name,selection_name,latest2,stake,potential,market_id,selection_id,idem,str(s2.get('provider_id') or '')[:300],str(m2.get('canonical') or '')[:100])); br=cur.fetchone()
                cur.execute("INSERT INTO wallet_ledger(telegram_id,entry_type,amount,balance_after,reference_type,reference_id,idempotency_key) VALUES(%s,'BET_DEBIT',%s,%s,'BET',%s,%s)",(tid,-stake,newbal,str(br[0]),'bet:'+idem))
            conn.commit()
        return jsonify({'ok':True,'message':'投注成功','bet':{'id':br[0],'event_id':event_id,'market':market_name,'selection':selection_name,'odds':float(latest2),'stake':float(stake),'potential_return':float(potential),'status':'pending','created_at':br[1].isoformat()},'balance':float(newbal)})
    except Exception as e:print('提交投注失败:',e);return jsonify({'ok':False,'error':'提交投注失败，请稍后重试'}),500

@web.get('/api/fixtures')
def api_fixtures():
    et=request.args.get('event_type','live').lower()
    if et not in ('live','prematch'):return jsonify({'ok':False,'error':'event_type必须是live或prematch'}),400
    try:limit=max(1,min(int(request.args.get('limit','300')),1000))
    except:limit=300
    params={'event_type':et,'limit':limit,'include':request.args.get('include','closed,suspended')}
    for k in ('tier','sport','status','canonical'):
        if request.args.get(k):params[k]=request.args.get(k)
    data,err,code=oddiwire_get('/v1/fixtures',params)
    if err:return jsonify({'ok':False,'error':err}),code
    events=data.get('events',[]) if isinstance(data,dict) else (data if isinstance(data,list) else [])
    return jsonify({'ok':True,'event_type':et,'count':len(events),'events':events})

def home_keyboard():
    rows=[[InlineKeyboardButton('🎮 进入姜天电竞',web_app=WebAppInfo(url=MINI_APP_URL))]]
    rb=InlineKeyboardButton('💰 充值',url=RECHARGE_URL) if RECHARGE_URL else InlineKeyboardButton('💰 充值',callback_data='recharge')
    sb=InlineKeyboardButton('👤 联系客服',url=SUPPORT_URL) if SUPPORT_URL else InlineKeyboardButton('👤 联系客服',callback_data='support')
    rows.append([rb,sb]);return InlineKeyboardMarkup(rows)
async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user; a=ensure_bot_user(u) if u else None; name=u.first_name if u else '玩家'; bal=f"{a['balance']:,.2f}" if a else '--'
    await update.message.reply_text(f'🎮 <b>姜天电竞</b>\n\n欢迎你，{name}\n\n💰 余额：<b>{bal} USDT</b>\n\n点击下方按钮进入赛事中心。',parse_mode='HTML',reply_markup=home_keyboard())
async def callback_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer()
    if q.data=='recharge':await q.message.reply_text('💰 充值入口暂未配置，请联系客服。')
    elif q.data=='support':await q.message.reply_text('👤 客服入口暂未配置。')
def run_web():web.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False,use_reloader=False)
def main():
    print('正在初始化姜天电竞正式版...');init_database();threading.Thread(target=run_web,daemon=True).start();app=Application.builder().token(BOT_TOKEN).build();app.add_handler(CommandHandler('start',start));app.add_handler(CallbackQueryHandler(callback_handler));print('姜天电竞 Telegram Bot 已启动');app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__':main()
