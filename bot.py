import os, threading, hashlib, hmac, json, uuid, time, random
from urllib.parse import parse_qsl, quote
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import requests, psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN=os.environ['BOT_TOKEN']
ODDIWIRE_API_KEY=os.environ.get('FIELDFUNDED_API_KEY','')
DATABASE_URL=os.environ.get('DATABASE_URL','')
MINI_APP_URL=os.environ.get('MINI_APP_URL','https://proud-mode-0c06.tj6235138.workers.dev').rstrip('/')
SUPPORT_URL=os.environ.get('SUPPORT_URL','https://t.me/OKK25')
RECHARGE_URL=os.environ.get('RECHARGE_URL','')
ODDIWIRE_BASE_URL='https://oddiwire.com'

# TRON / TRC20-USDT 充值（只读监听，不需要私钥/助记词）
TRON_RECHARGE_ADDRESS=os.environ.get('TRON_RECHARGE_ADDRESS','TMMxjpwY2NzsWt8VvMpGRB9AnKm8YCuanb')
TRON_USDT_CONTRACT=os.environ.get('TRON_USDT_CONTRACT','TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t')
TRONGRID_API_BASE=os.environ.get('TRONGRID_API_BASE','https://api.trongrid.io').rstrip('/')
TRONGRID_API_KEY=os.environ.get('TRONGRID_API_KEY','')
RECHARGE_MIN_USDT=Decimal(os.environ.get('RECHARGE_MIN_USDT','1'))
RECHARGE_EXPIRE_MINUTES=int(os.environ.get('RECHARGE_EXPIRE_MINUTES','30'))
RECHARGE_POLL_SECONDS=max(10,int(os.environ.get('RECHARGE_POLL_SECONDS','20')))

web=Flask(__name__)

# Cloudflare Mini App + 旧 GitHub Pages + Telegram WebView
CORS(web,resources={r'/api/*':{
    'origins':[
        'https://proud-mode-0c06.tj6235138.workers.dev',
        'https://tj6235138-debug.github.io',
        'https://web.telegram.org'
    ],
    'allow_headers':['Content-Type','X-Telegram-Init-Data'],
    'methods':['GET','POST','OPTIONS']
}})

def money(v): return Decimal(str(v)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
def price4(v): return Decimal(str(v)).quantize(Decimal('0.0001'),rounding=ROUND_HALF_UP)

def get_db():
    if not DATABASE_URL: raise RuntimeError('DATABASE_URL 未配置')
    return psycopg.connect(DATABASE_URL,autocommit=False)

def init_database():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS users(telegram_id BIGINT PRIMARY KEY,username TEXT,first_name TEXT,last_name TEXT,photo_url TEXT,balance NUMERIC(18,2) NOT NULL DEFAULT 0.00,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
            cur.execute('ALTER TABLE users ALTER COLUMN balance SET DEFAULT 0.00')
            cur.execute("CREATE TABLE IF NOT EXISTS bets(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,event_id TEXT,sport TEXT,league TEXT,home_team TEXT,away_team TEXT,market TEXT,selection TEXT,odds NUMERIC(12,4) NOT NULL,stake NUMERIC(18,2) NOT NULL,potential_return NUMERIC(18,2) NOT NULL,status TEXT NOT NULL DEFAULT 'pending',result TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),settled_at TIMESTAMPTZ)")
            for sql in [
                'ALTER TABLE bets ADD COLUMN IF NOT EXISTS market_id TEXT',
                'ALTER TABLE bets ADD COLUMN IF NOT EXISTS selection_id TEXT',
                'ALTER TABLE bets ADD COLUMN IF NOT EXISTS idempotency_key TEXT',
                'ALTER TABLE bets ADD COLUMN IF NOT EXISTS provider_id TEXT',
                'ALTER TABLE bets ADD COLUMN IF NOT EXISTS canonical TEXT'
            ]: cur.execute(sql)
            cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_bets_idempotency ON bets(telegram_id,idempotency_key) WHERE idempotency_key IS NOT NULL')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_bets_telegram_id ON bets(telegram_id)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_bets_event_id ON bets(event_id)')
            cur.execute('CREATE TABLE IF NOT EXISTS wallet_ledger(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,entry_type TEXT NOT NULL,amount NUMERIC(18,2) NOT NULL,balance_after NUMERIC(18,2) NOT NULL,reference_type TEXT,reference_id TEXT,idempotency_key TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
            cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_idempotency ON wallet_ledger(telegram_id,idempotency_key) WHERE idempotency_key IS NOT NULL')
            cur.execute("""CREATE TABLE IF NOT EXISTS recharge_orders(
                id BIGSERIAL PRIMARY KEY,
                order_no TEXT NOT NULL UNIQUE,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                requested_amount NUMERIC(18,2) NOT NULL,
                payable_amount NUMERIC(18,6) NOT NULL,
                address TEXT NOT NULL,
                network TEXT NOT NULL DEFAULT 'TRC20',
                status TEXT NOT NULL DEFAULT 'pending',
                txid TEXT UNIQUE,
                from_address TEXT,
                received_amount NUMERIC(18,6),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                confirmed_at TIMESTAMPTZ
            )""")
            cur.execute('CREATE INDEX IF NOT EXISTS idx_recharge_user ON recharge_orders(telegram_id,id DESC)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_recharge_pending ON recharge_orders(status,expires_at)')
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
    except Exception as e:
        print('Telegram身份验证失败:',e); return None

def get_request_user():
    d=request.headers.get('X-Telegram-Init-Data','')
    if not d and request.is_json:d=(request.get_json(silent=True) or {}).get('init_data','')
    return verify_telegram_init_data(d)

def upsert_user(user):
    vals=(int(user['id']),user.get('username'),user.get('first_name'),user.get('last_name'),user.get('photo_url'))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO users(telegram_id,username,first_name,last_name,photo_url) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,photo_url=EXCLUDED.photo_url,updated_at=NOW() RETURNING telegram_id,username,first_name,last_name,photo_url,balance,created_at',vals)
            r=cur.fetchone()
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
def health():return jsonify({'ok':True,'name':'姜天电竞 API','status':'online','database':bool(DATABASE_URL),'mini_app_url':MINI_APP_URL})

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
                cur.execute('SELECT id,event_id,sport,league,home_team,away_team,market,selection,odds,stake,potential_return,status,result,created_at,settled_at,market_id,selection_id FROM bets WHERE telegram_id=%s ORDER BY id DESC LIMIT 100',(int(u['id']),)); rows=cur.fetchall()
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
    latest=price4(s['price']); expected=accepted if accepted is not None else displayed
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
                live2,err2,code2=get_live_selection(event_id,market_id,selection_id)
                if err2:conn.rollback();return jsonify({'ok':False,'error':err2,'code':'MARKET_CLOSED'}),code2
                m2,s2=live2['market'],live2['selection']
                if str(m2.get('status','')).upper()!='OPEN' or str(s2.get('status','OPEN')).upper() not in ('OPEN','ACTIVE','') or s2.get('price') is None:
                    conn.rollback();return jsonify({'ok':False,'error':'盘口刚刚封盘或暂停，投注未成交，余额未扣除。','code':'MARKET_CLOSED'}),409
                latest2=price4(s2['price'])
                if latest2!=latest:
                    conn.rollback();return jsonify({'ok':False,'error':'赔率再次变化，请重新确认。','code':'ODDS_CHANGED','old_odds':float(latest),'latest_odds':float(latest2),'stake':float(stake),'potential_return':float(money(stake*latest2)),'idempotency_key':idem}),409
                cur.execute('UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE telegram_id=%s RETURNING balance',(stake,tid)); newbal=money(cur.fetchone()[0])
                home=str(b.get('home_team',''))[:300]; away=str(b.get('away_team',''))[:300]
                market_name=str(m2.get('market') or b.get('market',''))[:300]; selection_name=str(s2.get('name') or b.get('selection',''))[:300]
                cur.execute("INSERT INTO bets(telegram_id,event_id,sport,league,home_team,away_team,market,selection,odds,stake,potential_return,status,market_id,selection_id,idempotency_key,provider_id,canonical) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s) RETURNING id,created_at",(tid,event_id,str(live2['event'].get('sport') or b.get('sport',''))[:100],str(live2['event'].get('league') or b.get('league',''))[:300],home,away,market_name,selection_name,latest2,stake,potential,market_id,selection_id,idem,str(s2.get('provider_id') or '')[:300],str(m2.get('canonical') or '')[:100])); br=cur.fetchone()
                cur.execute("INSERT INTO wallet_ledger(telegram_id,entry_type,amount,balance_after,reference_type,reference_id,idempotency_key) VALUES(%s,'BET_DEBIT',%s,%s,'BET',%s,%s)",(tid,-stake,newbal,str(br[0]),'bet:'+idem))
            conn.commit()
        return jsonify({'ok':True,'message':'投注成功','bet':{'id':br[0],'event_id':event_id,'market':market_name,'selection':selection_name,'odds':float(latest2),'stake':float(stake),'potential_return':float(potential),'status':'pending','created_at':br[1].isoformat()},'balance':float(newbal)})
    except Exception as e:print('提交投注失败:',e);return jsonify({'ok':False,'error':'提交投注失败，请稍后重试'}),500


def trongrid_headers():
    h={'Accept':'application/json'}
    if TRONGRID_API_KEY:
        h['TRON-PRO-API-KEY']=TRONGRID_API_KEY
    return h

def tron_recent_usdt_transfers(limit=200):
    """读取充值地址最近已确认的 TRC20-USDT 转入记录。"""
    url=f"{TRONGRID_API_BASE}/v1/accounts/{TRON_RECHARGE_ADDRESS}/transactions/trc20"
    params={
        'only_confirmed':'true',
        'only_to':'true',
        'contract_address':TRON_USDT_CONTRACT,
        'limit':min(max(int(limit),1),200),
        'order_by':'block_timestamp,desc'
    }
    r=requests.get(url,headers=trongrid_headers(),params=params,timeout=15)
    r.raise_for_status()
    payload=r.json()
    return payload.get('data',[]) if isinstance(payload,dict) else []

def parse_usdt_transfer(t):
    try:
        token=t.get('token_info') or {}
        if str(token.get('address') or '') != TRON_USDT_CONTRACT:
            return None
        if str(t.get('to') or '') != TRON_RECHARGE_ADDRESS:
            return None
        decimals=int(token.get('decimals') or 6)
        amount=Decimal(str(t.get('value') or '0'))/(Decimal(10) ** decimals)
        txid=str(t.get('transaction_id') or '')
        if not txid or amount<=0:
            return None
        return {
            'txid':txid,
            'from_address':str(t.get('from') or ''),
            'amount':amount.quantize(Decimal('0.000001')),
            'block_timestamp':t.get('block_timestamp')
        }
    except Exception:
        return None

def expire_old_recharge_orders():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE recharge_orders SET status='expired' WHERE status='pending' AND expires_at<=NOW()")
        conn.commit()

def credit_recharge(order_id,tx):
    """原子入账：订单锁 + TxID唯一 + 余额更新 + 不可重复账变。"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,order_no,telegram_id,requested_amount,payable_amount,status
                           FROM recharge_orders WHERE id=%s FOR UPDATE""",(order_id,))
            order=cur.fetchone()
            if not order or order[5] != 'pending':
                conn.rollback(); return False
            if tx['amount'] != Decimal(str(order[4])).quantize(Decimal('0.000001')):
                conn.rollback(); return False

            cur.execute('SELECT id FROM recharge_orders WHERE txid=%s',(tx['txid'],))
            if cur.fetchone():
                conn.rollback(); return False

            tid=int(order[2])
            credit=money(order[3])  # 唯一尾数只用于识别订单；账户按用户申请金额入账
            cur.execute('SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE',(tid,))
            row=cur.fetchone()
            if not row:
                conn.rollback(); return False
            newbal=money(row[0])+credit
            cur.execute('UPDATE users SET balance=%s,updated_at=NOW() WHERE telegram_id=%s',(newbal,tid))
            cur.execute("""UPDATE recharge_orders
                           SET status='confirmed',txid=%s,from_address=%s,received_amount=%s,confirmed_at=NOW()
                           WHERE id=%s""",
                        (tx['txid'],tx['from_address'],tx['amount'],order_id))
            cur.execute("""INSERT INTO wallet_ledger
                           (telegram_id,entry_type,amount,balance_after,reference_type,reference_id,idempotency_key)
                           VALUES(%s,'RECHARGE_CREDIT',%s,%s,'RECHARGE',%s,%s)""",
                        (tid,credit,newbal,str(order[1]),'recharge:'+tx['txid']))
        conn.commit()
    return True

def scan_recharges_once():
    expire_old_recharge_orders()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,payable_amount FROM recharge_orders
                           WHERE status='pending' AND expires_at>NOW()
                           ORDER BY id ASC LIMIT 500""")
            pending=cur.fetchall()
    if not pending:
        return 0
    transfers=tron_recent_usdt_transfers(200)
    by_amount={}
    for raw in transfers:
        tx=parse_usdt_transfer(raw)
        if tx:
            by_amount.setdefault(tx['amount'],[]).append(tx)
    credited=0
    for oid,payable in pending:
        amount=Decimal(str(payable)).quantize(Decimal('0.000001'))
        for tx in by_amount.get(amount,[]):
            try:
                if credit_recharge(oid,tx):
                    credited+=1
                    break
            except Exception as e:
                print('充值入账失败:',oid,e)
    return credited

def recharge_listener():
    print('TRC20-USDT充值监听已启动:',TRON_RECHARGE_ADDRESS)
    while True:
        try:
            n=scan_recharges_once()
            if n: print('本轮自动入账:',n,'笔')
        except Exception as e:
            print('TRC20充值监听异常:',e)
        time.sleep(RECHARGE_POLL_SECONDS)

@web.post('/api/recharge/create')
def api_recharge_create():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败'}),401
    b=request.get_json(silent=True) or {}
    try:
        requested=money(b.get('amount',0))
    except (InvalidOperation,TypeError,ValueError):
        return jsonify({'ok':False,'error':'充值金额格式错误'}),400
    if requested<RECHARGE_MIN_USDT or requested>Decimal('1000000'):
        return jsonify({'ok':False,'error':f'充值金额需在 {RECHARGE_MIN_USDT} - 1000000 USDT'}),400

    profile=upsert_user(u); tid=int(profile['telegram_id'])
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE recharge_orders SET status='expired' WHERE telegram_id=%s AND status='pending' AND expires_at<=NOW()",(tid,))
            # 同一时间每个用户只保留一个待支付订单，避免多订单混淆。
            cur.execute("""SELECT id,order_no,requested_amount,payable_amount,address,status,created_at,expires_at
                           FROM recharge_orders
                           WHERE telegram_id=%s AND status='pending' AND expires_at>NOW()
                           ORDER BY id DESC LIMIT 1""",(tid,))
            old=cur.fetchone()
            if old:
                conn.commit()
                return jsonify({'ok':True,'existing':True,'order':{
                    'id':old[0],'order_no':old[1],'requested_amount':float(old[2]),
                    'payable_amount':float(old[3]),'address':old[4],'network':'TRC20',
                    'status':old[5],'created_at':old[6].isoformat(),'expires_at':old[7].isoformat()
                }})
            order_no='RC'+uuid.uuid4().hex[:18].upper()
            # 0.000001~0.009999 唯一尾数，用于单地址自动归属。
            for _ in range(50):
                suffix=Decimal(random.randint(1,9999))/Decimal('1000000')
                payable=(requested+suffix).quantize(Decimal('0.000001'))
                cur.execute("SELECT 1 FROM recharge_orders WHERE status='pending' AND payable_amount=%s AND expires_at>NOW()",(payable,))
                if not cur.fetchone(): break
            else:
                conn.rollback()
                return jsonify({'ok':False,'error':'暂时无法生成充值订单，请稍后重试'}),503
            cur.execute("""INSERT INTO recharge_orders
                           (order_no,telegram_id,requested_amount,payable_amount,address,expires_at)
                           VALUES(%s,%s,%s,%s,%s,NOW()+(%s || ' minutes')::interval)
                           RETURNING id,created_at,expires_at""",
                        (order_no,tid,requested,payable,TRON_RECHARGE_ADDRESS,str(RECHARGE_EXPIRE_MINUTES)))
            rr=cur.fetchone()
        conn.commit()
    return jsonify({'ok':True,'order':{
        'id':rr[0],'order_no':order_no,'requested_amount':float(requested),
        'payable_amount':float(payable),'address':TRON_RECHARGE_ADDRESS,'network':'TRC20',
        'status':'pending','created_at':rr[1].isoformat(),'expires_at':rr[2].isoformat(),
        'notice':'请严格按应付金额转入 TRC20-USDT；唯一尾数用于自动识别订单。'
    }})

@web.get('/api/recharges')
def api_recharges():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败'}),401
    tid=int(u['id']); upsert_user(u)
    try:
        expire_old_recharge_orders()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,order_no,requested_amount,payable_amount,address,status,txid,
                                      from_address,received_amount,created_at,expires_at,confirmed_at
                               FROM recharge_orders WHERE telegram_id=%s ORDER BY id DESC LIMIT 100""",(tid,))
                rows=cur.fetchall()
        items=[{
            'id':r[0],'order_no':r[1],'requested_amount':float(r[2]),'payable_amount':float(r[3]),
            'address':r[4],'network':'TRC20','status':r[5],'txid':r[6],'from_address':r[7],
            'received_amount':float(r[8]) if r[8] is not None else None,
            'created_at':r[9].isoformat() if r[9] else None,
            'expires_at':r[10].isoformat() if r[10] else None,
            'confirmed_at':r[11].isoformat() if r[11] else None
        } for r in rows]
        return jsonify({'ok':True,'count':len(items),'orders':items})
    except Exception as e:
        print('读取充值记录失败:',e)
        return jsonify({'ok':False,'error':'读取充值记录失败'}),500

@web.get('/api/ledger')
def api_ledger():
    u=get_request_user()
    if not u:return jsonify({'ok':False,'error':'Telegram身份验证失败'}),401
    tid=int(u['id']); upsert_user(u)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,entry_type,amount,balance_after,reference_type,reference_id,created_at
                               FROM wallet_ledger WHERE telegram_id=%s ORDER BY id DESC LIMIT 100""",(tid,))
                rows=cur.fetchall()
        items=[{'id':r[0],'entry_type':r[1],'amount':float(r[2]),'balance_after':float(r[3]),
                'reference_type':r[4],'reference_id':r[5],
                'created_at':r[6].isoformat() if r[6] else None} for r in rows]
        return jsonify({'ok':True,'count':len(items),'entries':items})
    except Exception as e:
        print('读取账变失败:',e)
        return jsonify({'ok':False,'error':'读取账变记录失败'}),500


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

PROMO_CAPTION="""⚡ 专业电竞赛事中心

🎯 实时赛事
📊 即时赔率
🔥 热门电竞全覆盖

CS2 · LOL · 无畏契约
更多精彩赛事持续更新

━━━━━━━━━━━━━━
👇 点击下方进入赛事中心"""

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if u:ensure_bot_user(u)
    image_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),'ig2018.png')
    if os.path.exists(image_path):
        with open(image_path,'rb') as photo:
            await update.message.reply_photo(photo=photo,caption=PROMO_CAPTION,reply_markup=home_keyboard())
    else:
        await update.message.reply_text(PROMO_CAPTION,reply_markup=home_keyboard())

async def post_init(app:Application):
    await app.bot.set_my_commands([BotCommand('start','开始')])

async def callback_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer()
    if q.data=='recharge':await q.message.reply_text('💰 请进入「姜天电竞」→「个人中心」→「充值」创建 TRC20-USDT 充值订单。')
    elif q.data=='support':await q.message.reply_text('👤 客服：@OKK25')

def run_web():web.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False,use_reloader=False)

def main():
    print('正在初始化姜天电竞正式版...')
    print('Mini App URL:',MINI_APP_URL)
    init_database()
    threading.Thread(target=recharge_listener,daemon=True).start()
    threading.Thread(target=run_web,daemon=True).start()
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print('姜天电竞 Telegram Bot 已启动')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=='__main__':main()
